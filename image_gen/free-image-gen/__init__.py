"""Free image-generation backends for Hermes Agent.

Provider ids registered by this plugin:

* ``cogview``      — 智谱 CogView-3-Flash (permanently free model, official docs)
* ``pollinations`` — Pollinations (no API key at all)
* ``agnes``        — Agnes AI (agnes-image-2.x-flash, promotional $0)
* ``free-any``     — meta backend: tries the above in order, first success wins

Pick one with ``image_gen.provider`` in config.yaml.

``free-any`` exists because Hermes activates exactly ONE provider and never
falls back on failure: ``get_active_provider()`` returns the configured backend
even when it is unavailable, deliberately preferring "a precise error message
over a silent backend switch". So a 429, a 402, or a burned free quota on the
active backend is a hard failure. The meta backend restores that fallback by
re-dispatching to the other registered providers itself, in a configurable
order, until one returns an image.

Why one plugin with three adapters: these backends all advertise "free images"
but their request shapes have nothing in common —

* 智谱    : POST /paas/v4/images/generations, ``size: "1024x1024"`` + ``quality``
* Pollinations : GET  /prompt/{text}?width=&height=&model=  → raw image bytes, no auth
* Agnes   : POST /v1/images/generations, ``size: "2K"`` + ``ratio`` + ``extra_body``

So each backend gets its own adapter, while the provider plumbing (aspect-ratio
mapping, local image caching, error wrapping) is shared.

Config (all optional; secrets belong in .env, not here):

    image_gen:
      provider: cogview          # or: pollinations | agnes | free-any
      # output_dir: D:\hermes    # where finished images are written; default is
      #                          # the session's working directory, else D:\hermes
      free-any:
        order: [agnes, cogview, pollinations]   # tried left to right, first win
      cogview:
        model: cogview-3-flash
        # sizes: {landscape: 1440x720, portrait: 720x1440}
        # size: "1344x768"         # one canvas for every aspect
        # watermark_enabled: false # needs 智谱's waiver; drops the AI生成 pill
      agnes:
        base_url: https://apihub.agnes-ai.com/v1
        # sizes: {landscape: 4K}   # size tier: 1K | 2K | 3K | 4K
        # ratios: {landscape: 4:3} # Agnes' own ratio vocabulary
      pollinations:
        model: sana

Canvas precedence, most specific first — identical for every backend:

    1. ``image_gen.<backend>.sizes.<aspect>``  per-aspect, e.g. {landscape: 1440x720}
    2. ``image_gen.<backend>.size``            one canvas for all three aspects
    3. the adapter's default for that aspect   (the set that model supports)

Each adapter's default is what its model can actually produce, not a house
preference: 智谱 exposes 7 documented canvases, Agnes takes a size tier plus a
ratio, and Pollinations may refuse (402) or quietly downscale whatever it is
asked for. A global ``image_gen.size`` is deliberately NOT honoured — one shared
value would flatten landscape and portrait into the same canvas, which is the
whole reason the per-aspect key exists. Every response reports the canvas read
back out of the finished image alongside ``aspect_ratio_honored``, so "this model
can't do 16:9" is distinguishable from "you asked it for 4:3".

Environment variables: ``ZHIPU_API_KEY``, ``AGNES_API_KEY``.
"""

from __future__ import annotations

import base64
import logging
import os
import re
import struct
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

from agent.secret_scope import get_secret
from agent.image_gen_provider import (
    DEFAULT_ASPECT_RATIO,
    ImageGenProvider,
    error_response,
    normalize_reference_images,
    resolve_aspect_ratio,
    save_b64_image,
    success_response,
)

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 180.0

# Hermes normalises aspect_ratio to landscape|square|portrait.
_RATIO = {"landscape": "16:9", "square": "1:1", "portrait": "9:16"}

# 智谱's documented enum for every model other than ``glm-image`` —
# cogview-3-flash included (docs.bigmodel.cn → 图像生成 → ``size``):
#
#   1024x1024 (default), 768x1344, 864x1152, 1344x768, 1152x864, 1440x720, 720x1440
#
# Custom values are accepted too, but each side must be 512-2048, divisible by
# 16, and the total must stay under 2**21 pixels (2,097,152). Anything else is
# refused by the API, which is why the defaults below stick to the enum.
COGVIEW_CANVASES = ("1024x1024", "768x1344", "864x1152", "1344x768",
                    "1152x864", "1440x720", "720x1440")

# Closest *documented* canvas per Hermes aspect: 1344x768 is the landscape one
# (1.75:1, the nearest the enum gets to 16:9), 768x1344 its portrait twin, and
# 1024x1024 is 智谱's own default. Override per aspect with
# ``image_gen.cogview.sizes.<aspect>``, or pin one canvas for all three with
# ``image_gen.cogview.size``.
_COGVIEW_SIZES = {
    "landscape": "1344x768",
    "square": "1024x1024",
    "portrait": "768x1344",
}

# Pollinations keeps one free model; anything else now returns HTTP 402.
_POLLINATIONS_DEFAULT_MODEL = "sana"


def _image_gen_cfg() -> Dict[str, Any]:
    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
        section = cfg.get("image_gen") or {}
        return section if isinstance(section, dict) else {}
    except Exception as exc:  # pragma: no cover
        logger.debug("Could not load image_gen config: %s", exc)
        return {}


def _sub_cfg(name: str) -> Dict[str, Any]:
    sub = _image_gen_cfg().get(name) or {}
    return sub if isinstance(sub, dict) else {}


def _first(*candidates: Any) -> Optional[str]:
    for c in candidates:
        if isinstance(c, str) and c.strip():
            return c.strip()
    return None


# Hermes normalises aspect_ratio to landscape|square|portrait. These are the
# shapes those names *ask* for; an adapter may not be able to deliver them.
_TARGET_RATIO = {"landscape": 16 / 9, "square": 1.0, "portrait": 9 / 16}


def _canvas_from_cfg(backend: str, aspect: str) -> Optional[str]:
    """Return the user's canvas override for *backend* + *aspect*, or None.

    Precedence, most specific first::

        image_gen:
          <backend>:
            sizes:                       # per-aspect (recommended)
              landscape: 1440x720
              portrait: 720x1440
            size: 1024x1024              # one canvas for every aspect

    The value is in the backend's own notation — ``1344x768`` for 智谱 and
    Pollinations, ``2K`` for Agnes — because it is passed through verbatim.
    """
    sub = _sub_cfg(backend)
    sizes = sub.get("sizes")
    if isinstance(sizes, dict):
        value = sizes.get(aspect)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return _first(sub.get("size"))


def _ratio_honored(canvas: str, aspect: str) -> bool:
    """Does *canvas* actually match the shape *aspect* asked for?

    Accepts either notation an adapter deals in — ``1344x768`` or ``16:9``.
    A bare tier such as ``2K`` carries no shape information, so it is reported
    as honoured rather than guessed at. Tolerance is loose because the honest
    answer for e.g. 智谱's 1344x768 (1.75:1) against a 16:9 ask (1.78:1) is
    "yes, near enough" — while Pollinations' forced 1:1 is plainly "no".
    """
    target = _TARGET_RATIO.get(aspect, 1.0)
    try:
        text = (canvas or "").strip().lower()
        if "x" in text:
            w, h = (float(v) for v in text.split("x", 1))
        elif ":" in text:
            w, h = (float(v) for v in text.split(":", 1))
        else:
            return True
        if h <= 0 or w <= 0:
            return True
        return abs((w / h) - target) <= 0.06
    except Exception:
        return True


def _image_dimensions(source: Any) -> Optional[Tuple[int, int]]:
    """``(width, height)`` of a PNG or JPEG — from raw bytes or a local path.

    Read from the image itself rather than reported from the request, because
    the request is not always honoured: Pollinations accepts
    ``width=1024&height=1024`` and then hands back a **768x768** file. A
    ``canvas`` field copied from what we asked for would be a lie.
    """
    try:
        if isinstance(source, (bytes, bytearray)):
            data = bytes(source)
        elif isinstance(source, str) and os.path.exists(source):
            with open(source, "rb") as fh:
                data = fh.read(65536)
        else:
            return None

        if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
            return struct.unpack(">II", data[16:24])

        if data[:2] == b"\xff\xd8":  # JPEG: walk to the SOFn frame header
            i = 2
            while i < len(data) - 9:
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                              0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    height = int.from_bytes(data[i + 5:i + 7], "big")
                    width = int.from_bytes(data[i + 7:i + 9], "big")
                    return (width, height)
                segment = int.from_bytes(data[i + 2:i + 4], "big")
                if segment <= 0:
                    break
                i += 2 + segment
    except Exception as exc:  # pragma: no cover
        logger.debug("Could not read image dimensions: %s", exc)
    return None


DEFAULT_OUTPUT_DIR = r"D:\hermes"


def _output_dir() -> str:
    """Where a finished image is written.

    Hermes' own helpers always drop files in ``$HERMES_HOME/cache/images``. When
    you generate from a project directory you usually want the file beside your
    work instead, so resolve in this order:

    1. ``image_gen.output_dir`` (config) — explicit wins
    2. ``HERMES_IMAGE_OUTPUT_DIR`` (env)
    3. **the session's working directory** (``TERMINAL_CWD``, then ``os.getcwd()``)
    4. ``D:\\hermes`` on Windows, ``~`` elsewhere
    """
    for candidate in (_first(_image_gen_cfg().get("output_dir")),
                      _first(os.environ.get("HERMES_IMAGE_OUTPUT_DIR"))):
        if candidate:
            try:
                os.makedirs(candidate, exist_ok=True)
                return candidate
            except OSError as exc:
                logger.warning("image_gen.output_dir %r unusable (%s), falling back", candidate, exc)

    for candidate in (_first(os.environ.get("TERMINAL_CWD")), os.getcwd()):
        if candidate and os.path.isdir(candidate):
            return candidate

    fallback = DEFAULT_OUTPUT_DIR if os.name == "nt" else os.path.expanduser("~")
    try:
        os.makedirs(fallback, exist_ok=True)
    except OSError:
        pass
    return fallback


def _sniff_ext(data: bytes, content_type: str = "", hint: str = "") -> str:
    """File extension from the magic bytes first, then Content-Type, then a hint."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:2] == b"\xff\xd8":
        return "jpg"
    if data[:6] in (b"GIF87a", b"GIF89a") or data[:4] == b"GIF8":
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    if ct in ("image/jpeg", "image/jpg"):
        return "jpg"
    if ct == "image/webp":
        return "webp"
    if ct == "image/gif":
        return "gif"
    if ct == "image/png":
        return "png"
    return (hint or "png").lstrip(".").lower() or "png"


def _write_image(data: bytes, prefix: str, content_type: str = "", hint: str = "") -> str:
    """Write image bytes into :func:`_output_dir` and return the absolute path.

    Falls back to Hermes' own cache helper only if the target directory cannot
    be written to, so a read-only working directory never costs you the image.
    """
    import time

    ext = _sniff_ext(data, content_type, hint)
    name = "%s_%s_%s.%s" % (prefix, time.strftime("%Y%m%d_%H%M%S"), os.urandom(4).hex(), ext)
    target = os.path.join(_output_dir(), name)
    try:
        with open(target, "wb") as fh:
            fh.write(data)
        return target
    except OSError as exc:
        logger.warning("could not write %s (%s); caching under HERMES_HOME instead", target, exc)
        return str(save_b64_image(base64.b64encode(data).decode(), prefix=prefix, extension=ext))


def _save_bytes(data: bytes, prefix: str, content_type: str = "") -> str:
    """Persist raw image bytes (see :func:`_write_image` for the target dir)."""
    return _write_image(data, prefix, content_type)


_DATA_URI_MIME = {
    b"\x89PNG\r\n\x1a\n": "image/png",
    b"\xff\xd8": "image/jpeg",
    b"GIF8": "image/gif",
}


def _as_data_uri(src: str) -> str:
    """Normalise an image reference for APIs that want inline base64.

    Agnes (and most OpenAI-compatible editors) expect ``extra_body.image`` to
    hold **data URIs or http(s) URLs** — sending a bare local path gets back
    ``"extra_body.image is not a valid image base64"``. Hermes hands providers
    local file paths, so convert those here.

    ``http(s)://`` and existing ``data:`` URIs pass through untouched; a
    ``file://`` URL or plain path is read off disk and encoded.
    """
    if not isinstance(src, str):
        return src
    s = src.strip()
    if not s:
        return s
    low = s.lower()
    if low.startswith(("data:", "http://", "https://")):
        return s
    if low.startswith("file://"):
        s = urllib.parse.unquote(s[7:])
        if re.match(r"^/[A-Za-z]:", s):        # file:///C:/x -> C:/x
            s = s[1:]
    if not os.path.isfile(s):
        return src                              # not a file we can read; let the API judge

    with open(s, "rb") as fh:
        raw = fh.read()
    mime = "image/png"
    for magic, guess in _DATA_URI_MIME.items():
        if raw.startswith(magic):
            mime = guess
            break
    else:
        ext = os.path.splitext(s)[1].lower()
        mime = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
                ".gif": "image/gif", ".png": "image/png"}.get(ext, "image/png")
    return "data:%s;base64,%s" % (mime, base64.b64encode(raw).decode("ascii"))


def _cache_image_url(url: str, prefix: str) -> str:
    """Download an image URL and cache it with a *sniffed* extension.

    Hermes' own ``save_url_image`` falls back to ``.png`` whenever the CDN
    returns ``application/octet-stream`` — 智谱's CDN does exactly that while
    serving JPEG, which yields .png files holding JPEG bytes. Fetching here
    lets us sniff the magic bytes and name the file honestly.
    """
    import requests

    resp = requests.get(url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    data = resp.content
    ct = resp.headers.get("Content-Type", "")
    is_png = data[:8] == b"\x89PNG\r\n\x1a\n"
    is_jpg = data[:2] == b"\xff\xd8"
    if "image" not in ct.lower() and not (is_png or is_jpg):
        raise ValueError("response is not an image (Content-Type=%r)" % ct)
    return _save_bytes(data, prefix, ct)


# ---------------------------------------------------------------------------
# 智谱 CogView-3-Flash
# ---------------------------------------------------------------------------


class CogViewProvider(ImageGenProvider):
    """智谱 CogView-3-Flash — the only permanently-free model among these three."""

    BASE_URL = "https://open.bigmodel.cn/api/paas/v4/images/generations"
    DEFAULT_MODEL = "cogview-3-flash"

    @property
    def name(self) -> str:
        return "cogview"

    @property
    def display_name(self) -> str:
        return "智谱 CogView-3-Flash (free)"

    def is_available(self) -> bool:
        return bool(get_secret("ZHIPU_API_KEY"))

    def list_models(self) -> List[Dict[str, Any]]:
        return [
            {
                "id": "cogview-3-flash",
                "display": "CogView-3-Flash",
                "speed": "~5-15s",
                "strengths": "智谱官方免费图像生成模型，7 种分辨率，中文提示词友好",
                "price": "free",
            },
            {
                "id": "glm-image",
                "display": "GLM-Image (paid)",
                "speed": "~20-60s",
                "strengths": "智谱旗舰图像模型，文字渲染强；按量计费",
                "price": "paid",
            },
        ]

    def default_model(self) -> Optional[str]:
        return self.DEFAULT_MODEL

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "智谱 CogView-3-Flash",
            "badge": "free",
            "tag": "官方免费文生图模型，7 种官方画布：" + " / ".join(COGVIEW_CANVASES)
                   + "（默认带 AI生成 水印，可配 watermark_enabled: false 关闭）",
            "env_vars": [
                {
                    "key": "ZHIPU_API_KEY",
                    "prompt": "智谱开放平台 API Key",
                    "url": "https://www.bigmodel.cn/",
                },
            ],
        }

    def capabilities(self) -> Dict[str, Any]:
        return {"modalities": ["text"], "max_reference_images": 0}

    def generate(
        self,
        prompt: str,
        aspect_ratio: str = DEFAULT_ASPECT_RATIO,
        *,
        image_url: Optional[str] = None,
        reference_image_urls: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        prompt = (prompt or "").strip()
        aspect = resolve_aspect_ratio(aspect_ratio)
        model_id = _first(kwargs.get("model"), _sub_cfg("cogview").get("model"),
                          _image_gen_cfg().get("model"), self.DEFAULT_MODEL)

        if not prompt:
            return error_response(error="Prompt is required", error_type="invalid_argument",
                                  provider=self.name, model=model_id, aspect_ratio=aspect)

        api_key = get_secret("ZHIPU_API_KEY")
        if not api_key:
            return error_response(
                error="ZHIPU_API_KEY not set. Add it to ~/.hermes/.env (get a key at bigmodel.cn).",
                error_type="auth_required", provider=self.name, model=model_id,
                prompt=prompt, aspect_ratio=aspect,
            )

        try:
            import requests
        except ImportError:
            return error_response(error="requests not installed", error_type="missing_dependency",
                                  provider=self.name, model=model_id, prompt=prompt, aspect_ratio=aspect)

        # Canvas: per-aspect override > one pinned canvas > this model's own
        # default for that aspect. A global image_gen.size is deliberately NOT
        # honoured — one shared value would flatten landscape and portrait into
        # the same canvas, which is exactly what the per-aspect key exists to
        # avoid.
        override = _canvas_from_cfg("cogview", aspect)
        size = override if (override and "x" in override) \
            else _COGVIEW_SIZES.get(aspect, "1024x1024")

        body: Dict[str, Any] = {"model": model_id, "prompt": prompt, "size": size}

        # 智谱 documents watermark_enabled on this endpoint: false drops both the
        # visible 「AI生成」 pill and the invisible watermark — but only for accounts
        # that signed the waiver (个人中心 → 安全管理 → 去水印管理). Sent only when
        # explicitly disabled, so the default request shape is unchanged.
        wm = _sub_cfg("cogview").get("watermark_enabled")
        watermark = True
        if wm is False or (isinstance(wm, str)
                           and wm.strip().lower() in ("false", "0", "no", "off")):
            watermark = False
            body["watermark_enabled"] = False

        try:
            resp = requests.post(
                self.BASE_URL,
                headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
                json=body, timeout=REQUEST_TIMEOUT,
            )
        except Exception as exc:
            return error_response(error=f"Request to 智谱 failed: {exc}",
                                  error_type=type(exc).__name__, provider=self.name,
                                  model=model_id, prompt=prompt, aspect_ratio=aspect)

        if resp.status_code != 200:
            return error_response(error=f"智谱 returned HTTP {resp.status_code}: {(resp.text or '')[:300]}",
                                  error_type="provider_error", provider=self.name,
                                  model=model_id, prompt=prompt, aspect_ratio=aspect)

        try:
            payload = resp.json()
        except Exception as exc:
            return error_response(error=f"Bad JSON from 智谱: {exc}", error_type="parse_error",
                                  provider=self.name, model=model_id, prompt=prompt, aspect_ratio=aspect)

        url = None
        for item in (payload.get("data") or []):
            if isinstance(item, dict) and item.get("url"):
                url = item["url"]
                break
        if not url:
            return error_response(error=f"智谱 response had no image URL: {str(payload)[:250]}",
                                  error_type="empty_response", provider=self.name,
                                  model=model_id, prompt=prompt, aspect_ratio=aspect)

        try:
            image = _cache_image_url(url, self.name)
        except Exception as exc:
            logger.warning("Could not cache 智谱 image locally: %s", exc)
            image = url

        # Trust the file over the request and report the canvas that really landed.
        actual = _image_dimensions(image) if isinstance(image, str) else None
        canvas = "%dx%d" % actual if actual else size

        return success_response(
            image=image, model=model_id, prompt=prompt,
            aspect_ratio=aspect, provider=self.name, modality="text",
            extra={"canvas": canvas,
                   "requested_canvas": size,
                   "canvas_source": "config" if override else "default",
                   "aspect_ratio_honored": _ratio_honored(canvas, aspect),
                   "watermark": watermark,
                   "request_id": payload.get("request_id"),
                   "supported_canvases": list(COGVIEW_CANVASES)})


# ---------------------------------------------------------------------------
# Pollinations — no API key
# ---------------------------------------------------------------------------


class PollinationsProvider(ImageGenProvider):
    """Pollinations: GET-only, no auth, currently one free model (``sana``)."""

    TEMPLATE = "https://image.pollinations.ai/prompt/{prompt}"

    # What canvas to *ask* for, per Hermes aspect: the 16:9 pair for landscape
    # and portrait, square for square. These are requests, not guarantees.
    #
    # Measured 2026-09 — and the rule is NOT stable, which is itself the finding:
    #
    #   run A (13 requests, 2s apart)  1024x1024 / 1280x1280 / 1440x1440 -> 200
    #                                  512x512 / 640x640 / 768x768      -> 402
    #                                  every non-square                 -> 402
    #   run B (minutes later)          1280x720 -> 200, delivering 1024x576
    #   run C (minutes after that)     everything -> 402, square included
    #
    # So a 402 with an empty "{}" body is this tier's *generic* refusal, covering
    # at least three causes we could not fully separate: canvas under 1024px,
    # non-square canvas, and a throttled/exhausted anonymous quota. Never treat it
    # as a stable size rule — always fall back. What actually came out is read
    # back out of the image bytes, because the request is not always honoured
    # either (1024x1024 came back 768x768 in one run).
    SIZE_PX = {"landscape": (1024, 576), "square": (1024, 1024), "portrait": (576, 1024)}

    # The canvas the free tier is known to accept; also the 402 retry target.
    SQUARE = (1024, 1024)

    @property
    def name(self) -> str:
        return "pollinations"

    @property
    def display_name(self) -> str:
        return "Pollinations (free, no key)"

    def is_available(self) -> bool:
        # Deliberately no credential check — this backend needs no key.
        return True

    def list_models(self) -> List[Dict[str, Any]]:
        return [
            {
                "id": _POLLINATIONS_DEFAULT_MODEL,
                "display": "SANA",
                "speed": "~10-30s",
                "strengths": "完全免费、无需 API Key；目前是唯一免费模型（flux 已转为付费 402）",
                "price": "free",
            },
        ]

    def default_model(self) -> Optional[str]:
        return _POLLINATIONS_DEFAULT_MODEL

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "Pollinations",
            "badge": "no key",
            "tag": "无需任何 API Key，开箱即用；免费模型仅 sana",
            "env_vars": [],
        }

    def capabilities(self) -> Dict[str, Any]:
        return {"modalities": ["text"], "max_reference_images": 0}

    def generate(
        self,
        prompt: str,
        aspect_ratio: str = DEFAULT_ASPECT_RATIO,
        *,
        image_url: Optional[str] = None,
        reference_image_urls: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        prompt = (prompt or "").strip()
        aspect = resolve_aspect_ratio(aspect_ratio)
        model_id = _first(kwargs.get("model"), _sub_cfg("pollinations").get("model"),
                          _POLLINATIONS_DEFAULT_MODEL)

        if not prompt:
            return error_response(error="Prompt is required", error_type="invalid_argument",
                                  provider=self.name, model=model_id, aspect_ratio=aspect)

        try:
            import requests
        except ImportError:
            return error_response(error="requests not installed", error_type="missing_dependency",
                                  provider=self.name, model=model_id, prompt=prompt, aspect_ratio=aspect)

        def _fetch(cw: int, ch: int):
            u = (self.TEMPLATE.format(prompt=urllib.parse.quote(prompt, safe=""))
                 + "?width=%d&height=%d&model=%s&nologo=true"
                 % (cw, ch, urllib.parse.quote(model_id)))
            return requests.get(u, timeout=REQUEST_TIMEOUT)

        # Canvas: per-aspect override > one pinned canvas > this model's default.
        # The default is square for every aspect because that is genuinely all the
        # free tier serves — a model capability, not a preference of ours.
        override = _canvas_from_cfg("pollinations", aspect)
        w, h = self.SIZE_PX.get(aspect, self.SQUARE)
        if override and "x" in override.lower():
            try:
                ow, oh = (int(v) for v in override.lower().split("x", 1))
                if ow > 0 and oh > 0:
                    w, h = ow, oh
                else:
                    logger.debug("Pollinations: ignoring non-positive size %r", override)
            except ValueError:
                logger.debug("Pollinations: ignoring unparsable size %r", override)
        requested = (w, h)

        try:
            resp = _fetch(w, h)
        except Exception as exc:
            return error_response(error=f"Request to Pollinations failed: {exc}",
                                  error_type=type(exc).__name__, provider=self.name,
                                  model=model_id, prompt=prompt, aspect_ratio=aspect)

        downgraded = False
        if resp.status_code == 402 and (w, h) != self.SQUARE:
            # The free tier refuses anything that is not 1:1 >= 1024. Retry once
            # on the known-free canvas so the caller gets an image plus an
            # explicit downgrade flag instead of a bare failure.
            logger.warning("Pollinations 402 for %dx%d; retrying at %dx%d",
                           w, h, self.SQUARE[0], self.SQUARE[1])
            retry = None
            try:
                retry = _fetch(*self.SQUARE)
            except Exception as exc:
                logger.warning("Pollinations square retry failed: %s", exc)
            if retry is not None and retry.status_code == 200:
                resp, w, h, downgraded = retry, self.SQUARE[0], self.SQUARE[1], True

        if resp.status_code == 402:
            return error_response(
                error=("Pollinations returned 402 for %dx%d (model '%s'). The free tier serves "
                       "1:1 canvases of at least 1024px ONLY — any non-square or sub-1024 request "
                       "is refused with an empty '{}' body. Retry a square canvas, or use a "
                       "different backend for 16:9 / 9:16 output." % (w, h, model_id)),
                error_type="payment_required", provider=self.name,
                model=model_id, prompt=prompt, aspect_ratio=aspect)
        if resp.status_code != 200:
            return error_response(error=f"Pollinations returned HTTP {resp.status_code}: {(resp.text or '')[:200]}",
                                  error_type="provider_error", provider=self.name,
                                  model=model_id, prompt=prompt, aspect_ratio=aspect)

        ctype = resp.headers.get("Content-Type", "")
        if "image" not in ctype.lower():
            return error_response(error=f"Pollinations returned non-image content-type '{ctype}'",
                                  error_type="empty_response", provider=self.name,
                                  model=model_id, prompt=prompt, aspect_ratio=aspect)

        try:
            image = _save_bytes(resp.content, self.name, ctype)
        except Exception as exc:
            return error_response(error=f"Could not save Pollinations image: {exc}",
                                  error_type="io_error", provider=self.name,
                                  model=model_id, prompt=prompt, aspect_ratio=aspect)

        # Report what actually came out, read from the file itself — this backend
        # accepts a canvas it does not honour (1024x1024 came back 768x768).
        actual = _image_dimensions(resp.content)
        canvas = "%dx%d" % actual if actual else "%dx%d" % (w, h)

        return success_response(
            image=image, model=model_id, prompt=prompt, aspect_ratio=aspect,
            provider=self.name, modality="text",
            extra={
                "canvas": canvas,
                "requested_canvas": "%dx%d" % requested,
                "canvas_source": "config" if override else "default",
                "retried_square": downgraded,
                # Factual, not prescriptive: did the model deliver the shape that
                # was asked for? A "false" here almost always means this backend
                # cannot, and the caller decides what to do about it.
                "aspect_ratio_honored": _ratio_honored(canvas, aspect),
                "bytes": len(resp.content),
            })


# ---------------------------------------------------------------------------
# Agnes AI (promotional $0)
# ---------------------------------------------------------------------------


class AgnesProvider(ImageGenProvider):
    """Agnes AI image models — OpenAI-shaped path with Agnes' own size dialect."""

    DEFAULT_BASE_URL = "https://apihub.agnes-ai.com/v1"
    DEFAULT_MODEL = "agnes-image-2.5-flash"
    DEFAULT_SIZE = "2K"

    # Agnes expresses the canvas in two parts: a size TIER plus a RATIO. These
    # are the tiers its API documents (1K | 2K | 3K | 4K); its ratio vocabulary
    # is the usual 16:9 / 4:3 / 1:1 / 3:4 / 9:16 family, so the mapping below is
    # just the Hermes aspect translated — unlike Pollinations, Agnes really can
    # produce non-square output. Override the tier per aspect with
    # image_gen.agnes.sizes.<aspect> (or image_gen.agnes.size for all three) and
    # the ratio with image_gen.agnes.ratios.<aspect>.
    SIZE_TIERS = ("1K", "2K", "3K", "4K")

    @property
    def name(self) -> str:
        return "agnes"

    @property
    def display_name(self) -> str:
        return "Agnes AI (promotional $0)"

    def is_available(self) -> bool:
        return bool(get_secret("AGNES_API_KEY"))

    def list_models(self) -> List[Dict[str, Any]]:
        return [
            {"id": m, "display": d, "speed": "~15-40s", "strengths": s, "price": "free (promo)"}
            for m, d, s in [
                ("agnes-image-2.5-flash", "Agnes Image 2.5 Flash", "最新一代，质量最好"),
                ("agnes-image-2.1-flash", "Agnes Image 2.1 Flash", "高信息密度画面"),
                ("agnes-image-2.0-flash", "Agnes Image 2.0 Flash", "通用文生图/编辑"),
            ]
        ]

    def default_model(self) -> Optional[str]:
        return self.DEFAULT_MODEL

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "Agnes AI",
            "badge": "promo",
            "tag": "OpenAI 兼容文生图/图生图；当前价 $0 但属促销，随时可能结束",
            "env_vars": [
                {"key": "AGNES_API_KEY", "prompt": "Agnes AI API key",
                 "url": "https://platform.agnes-ai.com/"},
            ],
        }

    def capabilities(self) -> Dict[str, Any]:
        return {"modalities": ["text", "image"], "max_reference_images": 4}

    def generate(
        self,
        prompt: str,
        aspect_ratio: str = DEFAULT_ASPECT_RATIO,
        *,
        image_url: Optional[str] = None,
        reference_image_urls: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        prompt = (prompt or "").strip()
        aspect = resolve_aspect_ratio(aspect_ratio)
        sub = _sub_cfg("agnes")
        model_id = _first(kwargs.get("model"), sub.get("model"), self.DEFAULT_MODEL)

        if not prompt:
            return error_response(error="Prompt is required", error_type="invalid_argument",
                                  provider=self.name, model=model_id, aspect_ratio=aspect)

        api_key = get_secret("AGNES_API_KEY")
        if not api_key:
            return error_response(
                error="AGNES_API_KEY not set. Add it to ~/.hermes/.env (platform.agnes-ai.com).",
                error_type="auth_required", provider=self.name, model=model_id,
                prompt=prompt, aspect_ratio=aspect)

        try:
            import requests
        except ImportError:
            return error_response(error="requests not installed", error_type="missing_dependency",
                                  provider=self.name, model=model_id, prompt=prompt, aspect_ratio=aspect)

        sources: List[str] = []
        if isinstance(image_url, str) and image_url.strip():
            sources.append(image_url.strip())
        sources.extend(normalize_reference_images(reference_image_urls) or [])
        # Agnes wants data URIs or http(s) URLs, never a bare local path.
        sources = [_as_data_uri(s) for s in sources[:4]]
        modality = "image" if sources else "text"

        # Canvas, part 1 — the size tier: per-aspect override > one pinned tier >
        # the AGNES_IMAGE_SIZE env var > the adapter default. (The env var keeps
        # working for people who already set it; config wins over it because
        # config is the more deliberate signal.)
        override = _canvas_from_cfg("agnes", aspect)
        size = _first(override, os.environ.get("AGNES_IMAGE_SIZE"),
                      self.DEFAULT_SIZE) or self.DEFAULT_SIZE
        size = size.upper()
        if size not in self.SIZE_TIERS:
            logger.warning("Agnes: unsupported size tier %r (want %s), using %s",
                           size, "/".join(self.SIZE_TIERS), self.DEFAULT_SIZE)
            size = self.DEFAULT_SIZE

        # Canvas, part 2 — the ratio. Agnes *can* do real 16:9 / 9:16, so this
        # follows the requested aspect unless explicitly overridden.
        ratio = _RATIO.get(aspect, "16:9")
        ratio_override = False
        ratios = sub.get("ratios")
        if isinstance(ratios, dict):
            configured = ratios.get(aspect)
            if isinstance(configured, str) and configured.strip():
                ratio = configured.strip()
                ratio_override = True

        base_url = (_first(os.environ.get("AGNES_BASE_URL"), sub.get("base_url"),
                           self.DEFAULT_BASE_URL) or self.DEFAULT_BASE_URL).rstrip("/")

        extra_body: Dict[str, Any] = {"response_format": "url"}
        if sources:
            extra_body["image"] = sources

        body = {"model": model_id, "prompt": prompt, "size": size,
                "ratio": ratio, "extra_body": extra_body}

        try:
            resp = requests.post(f"{base_url}/images/generations",
                                 headers={"Authorization": "Bearer " + api_key,
                                          "Content-Type": "application/json"},
                                 json=body, timeout=REQUEST_TIMEOUT)
        except Exception as exc:
            return error_response(error=f"Request to Agnes failed: {exc}",
                                  error_type=type(exc).__name__, provider=self.name,
                                  model=model_id, prompt=prompt, aspect_ratio=aspect)

        if resp.status_code != 200:
            return error_response(error=f"Agnes returned HTTP {resp.status_code}: {(resp.text or '')[:300]}",
                                  error_type="provider_error", provider=self.name,
                                  model=model_id, prompt=prompt, aspect_ratio=aspect)

        try:
            payload = resp.json()
        except Exception as exc:
            return error_response(error=f"Bad JSON from Agnes: {exc}", error_type="parse_error",
                                  provider=self.name, model=model_id, prompt=prompt, aspect_ratio=aspect)

        url = b64 = None
        for item in (payload.get("data") or []):
            if not isinstance(item, dict):
                continue
            url = item.get("url") or item.get("image_url")
            b64 = item.get("b64_json") or item.get("base64")
            if url or b64:
                break
        if not url and not b64:
            return error_response(error=f"Agnes response had no image: {str(payload)[:250]}",
                                  error_type="empty_response", provider=self.name,
                                  model=model_id, prompt=prompt, aspect_ratio=aspect)

        if b64:
            try:
                image = _save_bytes(base64.b64decode(b64), self.name, "image/png")
            except Exception as exc:
                return error_response(error=f"Could not save Agnes b64 image: {exc}",
                                      error_type="io_error", provider=self.name,
                                      model=model_id, prompt=prompt, aspect_ratio=aspect)
        else:
            try:
                image = _cache_image_url(url, self.name)
            except Exception as exc:
                logger.warning("Could not cache Agnes image locally: %s", exc)
                image = url

        # Prefer the file's real dimensions; fall back to the requested tier+ratio
        # when the image stayed remote (caching failed) or is unparsable.
        actual = _image_dimensions(image) if isinstance(image, str) else None
        canvas = "%dx%d" % actual if actual else "%s %s" % (size, ratio)

        return success_response(
            image=image, model=model_id, prompt=prompt, aspect_ratio=aspect,
            provider=self.name, modality=modality,
            extra={"canvas": canvas,
                   "requested_canvas": "%s %s" % (size, ratio),
                   "size": size,
                   "ratio": ratio,
                   "canvas_source": "config" if (override or ratio_override) else "default",
                   "aspect_ratio_honored": _ratio_honored(canvas, aspect),
                   "supported_size_tiers": list(self.SIZE_TIERS)})


# ---------------------------------------------------------------------------
# free-any — meta backend that chains the others
# ---------------------------------------------------------------------------


def _provider_knows_model(provider: Any, model_id: str) -> bool:
    """Does ``provider`` list ``model_id``?

    Used by the ``free-any`` chain so a model configured for one backend is not
    forced onto another. A provider that publishes no model list (or whose list
    cannot be read) is treated as "yes" — never second-guess a backend we cannot
    interrogate; it will surface a clear error if the name really is wrong.
    """
    try:
        models = provider.list_models() or []
    except Exception:
        return True
    ids = set()
    for entry in models:
        if isinstance(entry, dict):
            candidate = entry.get("id") or entry.get("name") or entry.get("model")
        else:
            candidate = entry
        if isinstance(candidate, str) and candidate.strip():
            ids.add(candidate.strip().lower())
    if not ids:
        return True
    return model_id.strip().lower() in ids


class FreeAnyProvider(ImageGenProvider):
    """Try several backends in order and return the first image that works.

    Hermes resolves exactly ONE active provider and does not fall back: a 429,
    a 402, or a spent free quota on that backend fails the call outright. This
    adapter supplies the missing fallback by re-dispatching to the other
    registered providers itself.

    Configure the order in ``config.yaml``::

        image_gen:
          provider: free-any
          free-any:
            order: [cogview, agnes, pollinations]

    Names are tried left to right. Backends that are unregistered, missing
    credentials, or that raise/return an error are skipped and recorded in the
    response's ``attempts`` list.

    A ``model`` is only forwarded to backends that actually list it: a name set
    for one provider (say ``cogview-3-flash``) is dropped for the others, which
    then use their own default. Without that, whichever backend sits first in
    the chain would fail on a foreign model name and you would silently lose the
    ordering you configured. Skipped names are noted in ``attempts``.
    """

    # Default chain: highest quality first, least reliable last.
    #   agnes       — best output (2K, no watermark, supports image editing)
    #   cogview     — the durable free tier (official "free model", watermarked)
    #   pollinations— refuses and silently downscales, so it is the last resort
    DEFAULT_ORDER = ("agnes", "cogview", "pollinations")

    @property
    def name(self) -> str:
        return "free-any"

    @property
    def display_name(self) -> str:
        return "Free-Any (auto-fallback chain)"

    # -- chain resolution --------------------------------------------------

    @staticmethod
    def _lookup(provider_name: str) -> Optional[ImageGenProvider]:
        """Fetch a registered provider by name.

        Imported lazily: this module is loaded by the plugin manager during
        startup, and the registry import is not guaranteed to be safe then.
        """
        try:
            from agent.image_gen_registry import get_provider

            return get_provider(provider_name)
        except Exception as exc:  # pragma: no cover
            logger.debug("free-any: could not look up %r: %s", provider_name, exc)
            return None

    def _chain(self) -> List[str]:
        """Resolve the configured order to the names that are really registered."""
        # Accept ``free-any`` and ``free_any`` as the config key, and either a
        # YAML list or a comma/space separated string.
        raw = _sub_cfg("free-any").get("order") or _sub_cfg("free_any").get("order")
        names: List[str] = []
        if isinstance(raw, str):
            # ``hermes config set image_gen.free-any.order "[a, b]"`` stores the
            # literal STRING "[a, b]", not a YAML list — strip the brackets and
            # quotes so that command really works. A hand-written YAML list
            # ([a, b] unquoted) arrives as a real list and skips this branch.
            cleaned = raw.strip().strip("[]").strip()
            for ch in (",", "'", '"'):
                cleaned = cleaned.replace(ch, " ")
            raw = cleaned.split()
        if isinstance(raw, (list, tuple)):
            for item in raw:
                if isinstance(item, str) and item.strip():
                    names.append(item.strip())
        if not names:
            names = list(self.DEFAULT_ORDER)

        chain: List[str] = []
        for candidate in names:
            if candidate == self.name:
                # Guard against a self-referential chain — would recurse forever.
                logger.warning("free-any: ignoring '%s' in its own chain", candidate)
                continue
            if candidate in chain:
                continue
            if self._lookup(candidate) is None:
                logger.debug("free-any: '%s' is not registered, skipping", candidate)
                continue
            chain.append(candidate)
        return chain

    def _members(self) -> List[Tuple[str, ImageGenProvider]]:
        """Chain entries as ``(name, provider)`` pairs, preserving order."""
        pairs: List[Tuple[str, ImageGenProvider]] = []
        for entry in self._chain():
            provider = self._lookup(entry)
            if provider is not None:
                pairs.append((entry, provider))
        return pairs

    # -- ImageGenProvider surface -----------------------------------------

    def is_available(self) -> bool:
        """True when at least one backend in the chain can service a call."""
        for _, provider in self._members():
            try:
                if provider.is_available():
                    return True
            except Exception:  # pragma: no cover
                continue
        return False

    def list_models(self) -> List[Dict[str, Any]]:
        """Union of the chain's models, each labelled with its backend."""
        models: List[Dict[str, Any]] = []
        seen = set()
        for backend, provider in self._members():
            try:
                entries = provider.list_models() or []
            except Exception:
                continue
            for entry in entries:
                if not isinstance(entry, dict) or not entry.get("id"):
                    continue
                if entry["id"] in seen:
                    continue
                seen.add(entry["id"])
                merged = dict(entry)
                merged["display"] = "%s · %s" % (entry.get("display") or entry["id"], backend)
                models.append(merged)
        return models

    def get_setup_schema(self) -> Dict[str, Any]:
        rows = []
        for backend, provider in self._members():
            try:
                ok = bool(provider.is_available())
            except Exception:
                ok = False
            rows.append("%s%s" % (backend, "" if ok else "(no key)"))
        return {
            "name": "Free-Any auto-fallback",
            "badge": "chain",
            "tag": "Tries each backend in order, first success wins: "
                   + (" -> ".join(rows) if rows else
                      "(chain is empty; set image_gen.free-any.order)"),
            "env_vars": [],
        }

    def capabilities(self) -> Dict[str, Any]:
        # The chain is as capable as its most capable member: image-to-image
        # works whenever a backend that supports it is reachable.
        max_refs = 0
        for _, provider in self._members():
            try:
                caps = provider.capabilities() or {}
                max_refs = max(max_refs, int(caps.get("max_reference_images") or 0))
            except Exception:
                continue
        return {
            "modalities": ["text", "image"] if max_refs else ["text"],
            "max_reference_images": max_refs,
        }

    def generate(
        self,
        prompt: str,
        aspect_ratio: str = DEFAULT_ASPECT_RATIO,
        *,
        image_url: Optional[str] = None,
        reference_image_urls: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        prompt = (prompt or "").strip()
        aspect = resolve_aspect_ratio(aspect_ratio)

        if not prompt:
            return error_response(error="Prompt is required", error_type="invalid_argument",
                                  provider=self.name, aspect_ratio=aspect)

        chain = self._chain()
        if not chain:
            return error_response(
                error=("free-any has no usable backend. Set image_gen.free-any.order to a list of "
                       "registered provider names, e.g. [cogview, pollinations]."),
                error_type="misconfigured", provider=self.name,
                prompt=prompt, aspect_ratio=aspect)

        attempts: List[Dict[str, Any]] = []
        for backend in chain:
            provider = self._lookup(backend)
            if provider is None:
                attempts.append({"provider": backend, "skipped": "not registered"})
                continue

            try:
                available = bool(provider.is_available())
            except Exception as exc:
                attempts.append({"provider": backend,
                                 "skipped": "%s: %s" % (type(exc).__name__, exc)})
                continue
            if not available:
                attempts.append({"provider": backend, "skipped": "unavailable (no credentials?)"})
                continue

            try:
                backend_kwargs = dict(kwargs)
                wanted = backend_kwargs.get("model")
                if wanted and not _provider_knows_model(provider, wanted):
                    # A model name set for one backend must not be forced onto
                    # another — Agnes asked for "cogview-3-flash" just fails and
                    # burns a slot. Drop it here so this backend uses its own
                    # default instead of erroring out.
                    backend_kwargs.pop("model", None)
                    note = "ignored model %r (not in its model list); used its default" % (wanted,)
                    logger.info("free-any: %s %s", backend, note)
                else:
                    note = None
                result = provider.generate(prompt, aspect, image_url=image_url,
                                           reference_image_urls=reference_image_urls, **backend_kwargs)
                if note:
                    attempts.append({"provider": backend, "note": note})
            except Exception as exc:
                attempts.append({"provider": backend,
                                 "error": "%s: %s" % (type(exc).__name__, exc)})
                logger.warning("free-any: %s raised %s, trying next backend", backend, exc)
                continue

            if isinstance(result, dict) and result.get("success"):
                # Pass the winning backend's response through untouched apart
                # from chain bookkeeping, so ``provider`` keeps naming the
                # backend that actually produced the image.
                failures = sum(1 for a in attempts if "error" in a or "skipped" in a)
                result["requested_provider"] = self.name
                result["chain"] = list(chain)
                result["attempts"] = attempts + [{"provider": backend, "ok": True}]
                result["fallbacks_used"] = failures
                if failures:
                    logger.info("free-any: served by '%s' after %d failed backend(s)",
                                backend, failures)
                return result

            detail = result.get("error") if isinstance(result, dict) else repr(result)
            attempts.append({
                "provider": backend,
                "error": detail or "unknown error",
                "error_type": result.get("error_type") if isinstance(result, dict) else None,
            })
            logger.warning("free-any: %s failed (%s), trying next backend", backend, detail)

        summary = "; ".join(
            "%s: %s" % (a["provider"], a.get("error") or a.get("skipped") or "failed")
            for a in attempts
        )
        return error_response(
            error="All %d backend(s) in the free-any chain failed — %s" % (len(attempts), summary),
            error_type="all_backends_failed", provider=self.name,
            prompt=prompt, aspect_ratio=aspect,
        )


def register(ctx) -> None:
    """Plugin entry point — registers every free backend, plus the fallback chain."""
    for provider in (CogViewProvider(), PollinationsProvider(), AgnesProvider(), FreeAnyProvider()):
        ctx.register_image_gen_provider(provider)
