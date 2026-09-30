# hermes-plugin-free-image-gen

**English** | [中文](README.zh-CN.md)

**Free image-generation backends for [Hermes Agent](https://github.com/NousResearch/hermes-agent)** — one plugin, four providers, the base three all free.

Hermes ships eight built-in image backends (`openai`, `xai`, `fal`, `krea`, `deepinfra`, `openrouter`, `nous`, `openai-codex`) and **every single one is hard-wired to its own paid endpoint**. Pointing Hermes at a free image API meant writing a plugin. This is that plugin, for the three free options that actually work — plus a dispatcher that falls back between them when one fails.

| Provider | Model(s) | Free status | API key |
|---|---|---|---|
| **`cogview`** | `cogview-3-flash` | **Permanently free model** (per 智谱's own docs) | `ZHIPU_API_KEY` |
| **`pollinations`** | `sana` | **Free, no key at all** | *none* |
| **`agnes`** | `agnes-image-2.0/2.1/2.5-flash` | Promotional **$0** (list price struck through) | `AGNES_API_KEY` |
| **`free-any`** | whatever the chain resolves to | inherits its members' | per member |

Pick one with `image_gen.provider` in `config.yaml`. Verified working end-to-end (real images generated, not just "should work") on Hermes Agent, 2026-09.

---

## ⚠️ Read this before you rely on any of them

These were measured, not copied from marketing pages. Three findings matter:

**1. Both free backends stamp a watermark in the bottom-right corner.**

- **智谱 CogView-3-Flash** → a dark rounded pill reading **「AI生成」**
- **Pollinations** → a **`pollinations.ai`** logo + text (its `nologo=true` parameter did not suppress it when tested)

The file lives in 智谱's `maas-**watermark**-prod-new...` CDN bucket, which is the tell. If you generate blog covers or thumbnails with a design that reserves the bottom-right (e.g. for an avatar), **the watermark will land on top of it.** Crop downstream, or use this backend only for base art.

**2. Pollinations is much narrower than it looks, and its refusal rule is not stable — this is the important one.**

- Its `/models` endpoint returns exactly `["sana"]`. But requesting `model=flux` and `model=turbo` also came back **HTTP 200** at 512×512, with byte-identical bodies — i.e. the `model` parameter is being ignored and a cached render served. So the model list is not a reliable guide to what you actually get.
- **`402` with an empty `{}` body is its generic refusal, not a paywall message.** The same request gives different answers at different times. Three independent observations (each request 1–2s apart):

  | observation | result |
  |---|---|
  | **A** (13 requests) | square ≥1024 (`1024x1024`/`1280x1280`/`1440x1440`) → **200**; square <1024 (`512x512`/`640x640`/`768x768`) → **402**; **every non-square → 402** |
  | **B** (minutes later) | `1280x720` → **200**, delivering `1024x576` |
  | **C** (minutes after that) | **everything → 402**, square included |

  So 402 covers at least three causes we could not fully separate: **canvas under 1024px, non-square canvas, and a throttled/exhausted anonymous quota.** Do not treat it as a stable size rule.
- **It also silently resizes.** A `1024x1024` request once came back as `768x768` (that downscale was already on record in an earlier release). This is why the plugin's `canvas` field is **read out of the finished image file**, not copied from the request.

  **Net effect: neither the shape nor the availability of this backend is guaranteed.** The adapter asks for the aspect-appropriate canvas, **retries once as a square on 402**, and reports `canvas` / `retried_square` / `aspect_ratio_honored` so you can see what happened. In a `free-any` chain, **put it last**.

**3. 智谱 is the only genuinely durable one here.**

`cogview-3-flash` is documented by 智谱 as a *free model* (免费图像生成模型) — a product-tier free, not a promo. Agnes' $0 is a **promotional price**; their pricing docs state the promotional end date is subject to change, and their changelog shows free-tier rate limits being **halved** on 2026-09-23. Treat Agnes as temporary.

---

## Requirements

- Hermes Agent with the `image_gen` toolset enabled (default on most platforms)
- `requests` (already a Hermes dependency)
- Optional keys, see below

## Install

### Route A — copy the folder (always works)

```bash
git clone https://github.com/<you>/hermes-plugin-free-image-gen /tmp/fig
mkdir -p ~/.hermes/plugins/image_gen
cp -r /tmp/fig/image_gen/free-image-gen ~/.hermes/plugins/image_gen/free-image-gen
hermes plugins enable free-image-gen
```

On Windows the target is `%LOCALAPPDATA%\hermes\plugins\image_gen\free-image-gen\`.

### Route B — Hermes' own installer

`hermes plugins install` accepts a **repo subdirectory**, which is required here because image-gen backends must live at `~/.hermes/plugins/image_gen/<name>/`:

```bash
hermes plugins install <you>/hermes-plugin-free-image-gen/image_gen/free-image-gen --no-enable
hermes plugins enable free-image-gen
```

Installing the bare repo root would drop the files at a path the backend scanner never looks at.

Verify either route with:

```bash
hermes plugins show free-image-gen     # expect: Status: enabled, Source: user
hermes plugins doctor "<path-to>/plugins/image_gen/free-image-gen"   # expect: OK ... exit 0
```

> `hermes plugins doctor` needs a path (or full key), not the bare plugin name.

## Configure

Secrets go in the Hermes `.env` (`hermes config env-path`), never in `config.yaml`. None of them are required if you only use Pollinations.

```
ZHIPU_API_KEY=...      # https://www.bigmodel.cn/       → cogview
AGNES_API_KEY=...      # https://platform.agnes-ai.com/  → agnes
```

Then pick a backend:

```bash
hermes config set image_gen.provider cogview        # or pollinations | agnes

# optional per-provider overrides
hermes config set image_gen.cogview.model cogview-3-flash
hermes config set image_gen.agnes.model agnes-image-2.5-flash
hermes config set image_gen.agnes.size 2K           # 1K | 2K | 3K | 4K
hermes config set image_gen.pollinations.model sana
```

Do you need a new session after changing the backend? Measured, not guessed:

| Change | New session? |
|---|---|
| Switch between **already-loaded** providers (`cogview` ⇄ `pollinations` ⇄ `agnes` ⇄ `free-any`) | **No** — the config cache is keyed on the file's mtime/size, so the very next call picks it up |
| Add an API key to `.env` | **Yes** — `.env` is loaded into `os.environ` at startup |
| Install / enable a new plugin | **Yes** — plugins are scanned at startup |

> Those `image_gen.<backend>.*` keys are plugin-defined, so `hermes config set` warns "not a recognized config key". **The value is still written and this plugin still reads it** — ignore the notice, or add `--force`.

### Canvas selection — configured to what each model actually supports

Hermes only ever asks a provider for three `aspect_ratio` values: `landscape`, `square`, `portrait`. **What shape a model can produce is that model's own limit — not a universal expectation.** The author of this plugin needs 16:9 for blog covers; that does not make 16:9 everyone's requirement. So each adapter ships the canvas set its model actually supports as a default, and you override as needed.

**Precedence, most specific first:**

| Tier | Key | Effect |
|---|---|---|
| ① per aspect (recommended) | `image_gen.<backend>.sizes.<aspect>` | change one aspect; the other two keep their defaults |
| ② all aspects | `image_gen.<backend>.size` | one canvas for all three |
| ③ adapter default | — | the set that model supports |

```bash
# landscape to 2:1 only (portrait/square untouched)
hermes config set image_gen.cogview.sizes.landscape 1440x720
# pin all three to 1024x1024
hermes config set image_gen.cogview.size 1024x1024
# Agnes takes a tier AND a ratio
hermes config set image_gen.agnes.sizes.landscape 4K
hermes config set image_gen.agnes.ratios.landscape 4:3
```

Dotted paths write straight into nested maps — `hermes config set image_gen.cogview.sizes.landscape 1440x720` lands as a real YAML map (verified).

**⚠️ A global `image_gen.size` is deliberately not honoured.** One shared value would flatten landscape and portrait into the same canvas, which is precisely what the per-aspect key exists to prevent.

**What each model actually supports:**

| Backend | Its canvas vocabulary | Default per aspect (landscape / square / portrait) |
|---|---|---|
| `cogview` 智谱 | **7 documented enum values**: `1024x1024` `768x1344` `864x1152` `1344x768` `1152x864` `1440x720` `720x1440`; custom values allowed (each side 512–2048px, divisible by 16, ≤ 2²¹ px total) | `1344x768` / `1024x1024` / `768x1344` |
| `agnes` | **size tier + ratio**: tier `1K`/`2K`/`3K`/`4K`, ratio its own vocabulary (`16:9`, `1:1`, `9:16`, …) — **real non-square output** | `2K`+`16:9` / `2K`+`1:1` / `2K`+`9:16` |
| `pollinations` | accepts any `WxH` but **only sometimes honours it** — refusals (402) and silent downscales both happen (finding 2); **no guaranteed shape or availability** | `1024x576` / `1024x1024` / `576x1024` (the 16:9 pair + square) |

**Responses state what actually came out:**

| Field | Meaning |
|---|---|
| `canvas` | **the real canvas** (e.g. `1344x768`, `2K 16:9`) |
| `canvas_source` | `config` = your setting applied; `default` = the model's built-in mapping |
| `aspect_ratio_honored` | whether the model could actually deliver the requested shape. `false` almost always means **this model can't** (e.g. pollinations asked for landscape) |
| `supported_canvases` / `supported_size_tiers` | the backend's own list, so you know what to configure |

> `aspect_ratio_honored` **states a fact; it is not an instruction to crop.** It separates "this model cannot do 16:9" from "you configured 4:3 yourself" — what to do about it is the caller's call.

### Turning off the 智谱 watermark

The endpoint documents `watermark_enabled`: setting it to `false` drops both the visible 「AI生成」 pill and the invisible watermark — **but only for accounts that signed the waiver** (个人中心 → 安全管理 → 去水印管理).

```bash
hermes config set image_gen.cogview.watermark_enabled false
```

An account without the waiver will either be refused or watermarked anyway. The parameter is **not sent by default**, so leaving it alone changes nothing.

## `free-any` — the auto-fallback backend

### Why it exists

Hermes activates exactly ONE provider and **never falls back**. `get_active_provider()` returns the configured backend even when `is_available()` is False, deliberately preferring "a precise downstream error message rather than a silent backend switch".

The consequence: you point Hermes at `cogview`, it returns **429 (rate limit) / 402 (payment required) / 401 (dead key) / quota exhausted**, and the call simply fails — even though `pollinations` was right there and working. Hermes will not try it.

`free-any` restores that fallback. It re-dispatches to the other registered providers in a configurable order and returns the first image that works.

### Use

```bash
hermes config set image_gen.provider free-any
hermes config set image_gen.free-any.order "[cogview, pollinations, agnes]"
```

Or write it by hand in `config.yaml` (`hermes config path` shows the location):

```yaml
image_gen:
  provider: free-any
  free-any:
    order: [cogview, pollinations, agnes]   # tried left to right
```

Omit `order` and it defaults to `[cogview, pollinations, agnes]`. Any registered provider name works, including Hermes' own built-ins (e.g. `fal`).

**Order it by reliability, not by preference.** Pollinations (finding 2) both refuses and silently downscales, so it belongs at the end — as a last resort rather than a first try:

```yaml
    order: [cogview, agnes, pollinations]
```

### Behaviour

- **Serial, in order.** The first success wins; later backends are never called.
- **Three skip reasons** are recorded in the response's `attempts`: not registered, no credentials (`is_available()` false), or raised an exception. A backend that really errored also records its `error` / `error_type`.
- **All-fail returns an aggregate error** naming every backend's reason, not just the last one — which is what you actually need for debugging:

  ```
  All 3 backend(s) in the free-any chain failed —
  cogview: unavailable (no credentials?);
  pollinations: Pollinations returned 402 for model 'sana' — ...;
  agnes: unavailable (no credentials?)
  ```

- **The winner's response is passed through untouched** — `provider` still names the backend that really produced the image, never `free-any`. These fields are added on top:

  | Field | Meaning |
  |---|---|
  | `requested_provider` | always `free-any` |
  | `provider` | **the backend that actually served the image** |
  | `fallbacks_used` | how many were skipped/failed first (0 = first try) |
  | `chain` | the effective chain for this call |
  | `attempts` | per-backend outcome detail |

- **A passed `model` is forwarded to every backend.** A name only one backend understands still works: the others reject it and the chain moves on. So with `free-any` you don't have to pick a model.
- **Self-reference guard**: listing `free-any` in its own `order` is dropped with a warning instead of recursing forever.
- **`is_available()` is true when any chain member is available.** Since `pollinations` needs no credentials, `free-any` is always available as long as it's in the chain.
- **`list_models()` returns the union** of the chain's models, each labelled `· <backend>`.

### When not to use it

- You need a **pinned canvas** — members use different canvases (see the canvas table), so whichever backend wins decides the size.
- You need **reliable image-to-image** — only Agnes supports `extra_body.image[]`. `capabilities()` advertises the chain's best member, but if the chain lands on cogview/pollinations (which don't support it) that request fails and the chain moves on. In practice image-to-image ends up on Agnes or fails.
- You're **capping spend** — keep paid backends out of the chain.

## Why three adapters instead of one generic client

All three advertise "OpenAI-compatible images", and all three mean something different:

| | endpoint | size encoding | image-to-image |
|---|---|---|---|
| 智谱 | `POST /paas/v4/images/generations` | `size: "1024x1024"` | not supported here |
| Agnes | `POST /v1/images/generations` | `size: "2K"` + `ratio: "16:9"` + `extra_body` | `extra_body.image[]` |
| Pollinations | **`GET`** `/prompt/{text}?width=&height=&model=` | URL query params | not supported |

Pollinations doesn't even return JSON — it returns raw image bytes with no auth header. A single generic request builder cannot cover these; each adapter is ~60 lines and shares the provider plumbing (aspect mapping, magic-byte sniffing for correct file extensions, local caching under `$HERMES_HOME/cache/images/`, uniform error responses).

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `ZHIPU_API_KEY not set` | Key absent from `~/.hermes/.env`. Add it, start a new session. |
| `智谱 returned HTTP 401` | Bad/expired key. Regenerate at bigmodel.cn. |
| `Pollinations returned 402` | Two causes: a non-square canvas (or one under 1024px) — the free tier is 1:1-only — or a throttled anonymous quota. The empty `{}` body means refusal, not necessarily "paid". Retry square, or use another backend for 16:9. |
| Pollinations returns HTML/JSON instead of an image | Transient upstream error; retry. The adapter rejects non-`image/*` responses rather than saving garbage. |
| Tool reports no image provider | `image_gen.provider` isn't one of `cogview`/`pollinations`/`agnes`/`free-any`, or the plugin isn't in `plugins.enabled`. |
| `free-any has no usable backend` | Nothing in `image_gen.free-any.order` resolved to a registered provider (typo, or that provider's plugin isn't enabled). |
| `All N backend(s) in the free-any chain failed` | Expected aggregate error — read the per-backend reasons in the message. Usually missing keys plus an upstream 429/402. |
| Enabled both this plugin and a standalone `agnes` plugin | Both register an `agnes` provider and the later one wins. Keep only one enabled. |
| `hermes config set` warns "not a recognized config key" | Plugin-defined keys. The value **is** written and read; add `--force` to silence the notice. |

## Provenance

Written by [LaoLU (lusdaily.com)](https://lusdaily.com). Every free-tier claim and every limitation above was checked against the providers' own docs and by making real API calls during development — including generating images with all three adapters.

Hermes Agent is MIT-licensed by Nous Research. This plugin is independent and not affiliated with Hermes, Nous Research, 智谱, Pollinations, or Agnes AI.

## License

MIT — see [LICENSE](LICENSE).

---

## 中文文档

完整中文说明见 **[README.zh-CN.md](README.zh-CN.md)** —— 含 `free-any` 自动兜底的用法与行为细节、实测出来的「切换后端要不要重开会话」对照表、画布对照表和排错表。
