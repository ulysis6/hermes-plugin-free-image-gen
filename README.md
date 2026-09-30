# hermes-plugin-free-image-gen

**Free image-generation backends for [Hermes Agent](https://github.com/NousResearch/hermes-agent)** — one plugin, three providers, all of them free.

Hermes ships eight built-in image backends (`openai`, `xai`, `fal`, `krea`, `deepinfra`, `openrouter`, `nous`, `openai-codex`) and **every single one is hard-wired to its own paid endpoint**. Pointing Hermes at a free image API meant writing a plugin. This is that plugin, for the three free options that actually work.

| Provider | Model(s) | Free status | API key |
|---|---|---|---|
| **`cogview`** | `cogview-3-flash` | **Permanently free model** (per 智谱's own docs) | `ZHIPU_API_KEY` |
| **`pollinations`** | `sana` | **Free, no key at all** | *none* |
| **`agnes`** | `agnes-image-2.0/2.1/2.5-flash` | Promotional **$0** (list price struck through) | `AGNES_API_KEY` |

Pick one with `image_gen.provider` in `config.yaml`. Verified working end-to-end (real images generated, not just "should work") on Hermes Agent, 2026-09.

---

## ⚠️ Read this before you rely on any of them

These were measured, not copied from marketing pages. Three findings matter:

**1. Both free backends stamp a watermark in the bottom-right corner.**

- **智谱 CogView-3-Flash** → a dark rounded pill reading **「AI生成」**
- **Pollinations** → a **`pollinations.ai`** logo + text (its `nologo=true` parameter did not suppress it when tested)

The file lives in 智谱's `maas-**watermark**-prod-new...` CDN bucket, which is the tell. If you generate blog covers or thumbnails with a design that reserves the bottom-right (e.g. for an avatar), **the watermark will land on top of it.** Crop downstream, or use this backend only for base art.

**2. Pollinations is much narrower than it looks.**

Its `/models` endpoint returns exactly `["sana"]`. Requesting `model=flux` returns **HTTP 402 Payment Required** — that model is paid now. So "Pollinations is free" is true, but it's one model, and I measured it capping output at 768×768 when 1024×1024 was requested.

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

Start a **new Hermes session** afterwards — plugins and config are read at startup.

### Canvas selection

Hermes hands providers one of `landscape` / `square` / `portrait`. Each adapter maps that onto its own dialect:

| aspect | 智谱 CogView | Pollinations | Agnes |
|---|---|---|---|
| `landscape` | `1344x768` | `1024x576` | `2K` + `16:9` |
| `square` | `1024x1024` | `1024x1024` | `2K` + `1:1` |
| `portrait` | `768x1344` | `576x1024` | `2K` + `9:16` |

There is deliberately **no global `image_gen.size`** honoured by this plugin: one shared value would flatten landscape and portrait into the same canvas. Use the per-provider keys above if you want to pin one canvas.

To force an exact 智谱 canvas: `hermes config set image_gen.cogview.size 1440x720` (any of 智谱's supported sizes).

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
| `Pollinations returned 402` | You asked for a paid model. Only `sana` is free. |
| Pollinations returns HTML/JSON instead of an image | Transient upstream error; retry. The adapter rejects non-`image/*` responses rather than saving garbage. |
| Tool reports no image provider | `image_gen.provider` isn't one of `cogview`/`pollinations`/`agnes`, or the plugin isn't in `plugins.enabled`. |
| Enabled both this plugin and a standalone `agnes` plugin | Both register an `agnes` provider and the later one wins. Keep only one enabled. |

## Provenance

Written by [LaoLU (lusdaily.com)](https://lusdaily.com). Every free-tier claim and every limitation above was checked against the providers' own docs and by making real API calls during development — including generating images with all three adapters.

Hermes Agent is MIT-licensed by Nous Research. This plugin is independent and not affiliated with Hermes, Nous Research, 智谱, Pollinations, or Agnes AI.

## License

MIT — see [LICENSE](LICENSE).

---

## 中文快速开始

**三个免费生图后端，装一次全都有。**

1. 把 `image_gen/free-image-gen` 放到 `~/.hermes/plugins/image_gen/free-image-gen/`
2. `hermes plugins enable free-image-gen`
3. 想用智谱就在 `.env` 里加 `ZHIPU_API_KEY=你的key`（`bigmodel.cn` 免费注册）
4. `hermes config set image_gen.provider cogview`
5. **新开一个会话**，直接说"生成一张图：……"

**想完全不要 key**：`hermes config set image_gen.provider pollinations` 就能用（免费模型只有 `sana` 一个）。

**三点实测提醒**：

- ⚠️ **智谱出的图右下角有「AI生成」水印**，Pollinations 右下角有 `pollinations.ai` 水印。做封面要留右下角放头像的话，**记得裁掉**
- ⚠️ **Pollinations 只剩 `sana` 一个免费模型**，换 flux 会返回 402 要付费
- ⚠️ **智谱 CogView-3-Flash 是永久免费模型**（官方文档明写），Agnes 的 $0 是**促销价**，而且免费档限速已经在收紧了
