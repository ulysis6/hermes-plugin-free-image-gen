"""Free image-generation backends for Hermes Agent.

Provider ids registered by this plugin:

* ``cogview``      — 智谱 CogView-3-Flash (permanently free model, official docs)
* ``pollinations`` — Pollinations (no API key at all)
* ``agnes``        — Agnes AI (agnes-image-2.x-flash, promotional $0)

Pick one with ``image_gen.provider`` in config.yaml.

Why one plugin with three adapters: these backends all advertise "free images"
but their request shapes have nothing in common —

* 智谱    : POST /paas/v4/images/generations, ``size: "1024x1024"`` + ``quality``
* Pollinations : GET  /prompt/{text}?width=&height=&model=  → raw image bytes, no auth
* Agnes   : POST /v1/images/generations, ``size: "2K"`` + ``ratio`` + ``extra_body``

So each backend gets its own adapter, while the provider plumbing (aspect-ratio
mapping, local image caching, error wrapping) is shared.

Config (all optional; secrets belong in .env, not here):

    image_gen:
      provider: cogview          # or: pollinations | agnes
      cogview:
        model: cogview-3-flash
        # size: "1344x768"       # optional: force ONE canvas for every request
      agnes:
        base_url: https://apihub.agnes-ai.com/v1
        # size: "2K"             # 1K | 2K | 3K | 4K
      pollinations:
        model: sana

The canvas is derived from the requested ``aspect_ratio``:
landscape → 16:9-ish, square → 1:1, portrait → 9:16-ish. There is deliberately
no global ``image_gen.size`` override — one shared value would flatten
landscape and portrait into the same canvas.

Environment variables: ``ZHIPU_API_KEY``, ``AGNES_API_KEY``.
"""

from __future__ import annotations

import base64
import logging
import os
import urllib.parse
from typing import Any, Dict, List, Optional

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

# 智谱 CogView-3-Flash supported canvases (per official docs). 1344x768 is the
# closest to 16:9 and 768x1344 to 9:16.
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


def _save_bytes(data: bytes, prefix: str, content_type: str = "") -> str:
    """Cache raw image bytes under $HERMES_HOME/cache/images/."""
    ext = "png"
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    if ct in ("image/jpeg", "image/jpg"):
        ext = "jpg"
    elif ct == "image/webp":
        ext = "webp"
    elif ct == "image/gif":
        ext = "gif"
    elif data[:2] == b"\xff\xd8":
        ext = "jpg"
    return str(save_b64_image(base64.b64encode(data).decode(), prefix=prefix, extension=ext))


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
            "tag": "官方免费文生图模型，1024x1024 等 7 种分辨率（注意：出图带 AI生成 水印）",
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

        # Explicit WxH wins; otherwise map the Hermes aspect onto a supported canvas.
        # Only the per-provider key is honoured here — a global image_gen.size
        # would silently flatten landscape/portrait into one canvas.
        explicit = _first(_sub_cfg("cogview").get("size"))
        if explicit and "x" in explicit:
            size = explicit
        else:
            size = _COGVIEW_SIZES.get(aspect, "1024x1024")

        body: Dict[str, Any] = {"model": model_id, "prompt": prompt, "size": size}

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

        return success_response(image=image, model=model_id, prompt=prompt,
                                aspect_ratio=aspect, provider=self.name, modality="text",
                                extra={"size": size, "request_id": payload.get("request_id"),
                                       "watermark": True})


# ---------------------------------------------------------------------------
# Pollinations — no API key
# ---------------------------------------------------------------------------


class PollinationsProvider(ImageGenProvider):
    """Pollinations: GET-only, no auth, currently one free model (``sana``)."""

    TEMPLATE = "https://image.pollinations.ai/prompt/{prompt}"
    SIZE_PX = {"landscape": (1024, 576), "square": (1024, 1024), "portrait": (576, 1024)}

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

        w, h = self.SIZE_PX.get(aspect, (1024, 1024))
        url = (self.TEMPLATE.format(prompt=urllib.parse.quote(prompt, safe=""))
               + "?width=%d&height=%d&model=%s&nologo=true" % (w, h, urllib.parse.quote(model_id)))

        try:
            resp = requests.get(url, timeout=REQUEST_TIMEOUT)
        except Exception as exc:
            return error_response(error=f"Request to Pollinations failed: {exc}",
                                  error_type=type(exc).__name__, provider=self.name,
                                  model=model_id, prompt=prompt, aspect_ratio=aspect)

        if resp.status_code == 402:
            return error_response(
                error=("Pollinations returned 402 for model '%s' — that model is paid now. "
                       "Only 'sana' is free." % model_id),
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

        return success_response(image=image, model=model_id, prompt=prompt, aspect_ratio=aspect,
                                provider=self.name, modality="text",
                                extra={"requested": "%dx%d" % (w, h), "bytes": len(resp.content)})


# ---------------------------------------------------------------------------
# Agnes AI (promotional $0)
# ---------------------------------------------------------------------------


class AgnesProvider(ImageGenProvider):
    """Agnes AI image models — OpenAI-shaped path with Agnes' own size dialect."""

    DEFAULT_BASE_URL = "https://apihub.agnes-ai.com/v1"
    DEFAULT_MODEL = "agnes-image-2.5-flash"
    DEFAULT_SIZE = "2K"

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
        sources = sources[:4]
        modality = "image" if sources else "text"

        size = _first(os.environ.get("AGNES_IMAGE_SIZE"), sub.get("size"),
                      self.DEFAULT_SIZE) or self.DEFAULT_SIZE
        size = size.upper()
        if size not in ("1K", "2K", "3K", "4K"):
            size = self.DEFAULT_SIZE

        base_url = (_first(os.environ.get("AGNES_BASE_URL"), sub.get("base_url"),
                           self.DEFAULT_BASE_URL) or self.DEFAULT_BASE_URL).rstrip("/")

        extra_body: Dict[str, Any] = {"response_format": "url"}
        if sources:
            extra_body["image"] = sources

        body = {"model": model_id, "prompt": prompt, "size": size,
                "ratio": _RATIO.get(aspect, "16:9"), "extra_body": extra_body}

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
                image = str(save_b64_image(b64, prefix=self.name, extension="png"))
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

        return success_response(image=image, model=model_id, prompt=prompt, aspect_ratio=aspect,
                                provider=self.name, modality=modality,
                                extra={"size": size, "ratio": _RATIO.get(aspect, "16:9")})


def register(ctx) -> None:
    """Plugin entry point — registers every free backend in one go."""
    for provider in (CogViewProvider(), PollinationsProvider(), AgnesProvider()):
        ctx.register_image_gen_provider(provider)
