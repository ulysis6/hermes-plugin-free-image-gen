# hermes-plugin-free-image-gen

[English](README.md) | **中文**

**给 [Hermes Agent](https://github.com/NousResearch/hermes-agent) 用的免费生图后端** —— 一个插件，四个 provider，全部免费。

Hermes 自带八个生图后端（`openai`、`xai`、`fal`、`krea`、`deepinfra`、`openrouter`、`nous`、`openai-codex`），**而其中每一个都硬绑自己的付费接口**。想让 Hermes 走免费生图 API，只能自己写插件。这个插件就是干这个的，收了三个实测能用得起来的免费渠道，外加一个自动兜底的调度后端。

| Provider | 模型 | 免费性质 | API Key |
|---|---|---|---|
| **`cogview`** | `cogview-3-flash` | **永久免费模型**（智谱官方文档明写） | `ZHIPU_API_KEY` |
| **`pollinations`** | `sana` | **免费，完全不需要 key** | *无* |
| **`agnes`** | `agnes-image-2.0/2.1/2.5-flash` | 促销价 **$0**（原价划线） | `AGNES_API_KEY` |
| **`free-any`** | 上面三个的调度层 | 继承所配后端的免费性质 | 看链条 |

用 `config.yaml` 里的 `image_gen.provider` 选一个。全部**端到端真实验证过**（真出图，不是"理论上应该能用"），2026-09 于 Hermes Agent 实测。

---

## ⚠️ 用之前先看这三条

这些是实测出来的，不是抄的宣传页。三条很要紧：

**1. 三个后端里有两个会在右下角盖水印 —— Agnes 不盖。**

- **智谱 CogView-3-Flash** → 一个深色圆角胶囊，写着 **「AI生成」**（判断依据：图片文件落在智谱 `maas-**watermark**-prod-new...` 这个 CDN 桶里，名字就写着 watermark）
- **Pollinations** → **`pollinations.ai`** logo + 文字（实测它自己的 `nologo=true` 参数压不住）
- **Agnes** → **干净无水印**（文生图和图片编辑两种模式都实测确认过）

如果你拿它做博客封面或缩略图，且设计上右下角要留位置（比如放头像），**智谱和 Pollinations 的水印正好压在你的留白上**。要么后期裁掉，要么在这种"右下角必须干净"的场景改用 `agnes`。

**2. Pollinations 比看起来窄得多，而且它的拒绝规则不稳定 —— 这是最要紧的一条。**

- 它的 `/models` 接口只返回 `["sana"]` 一个。但请求 `model=flux` 和 `model=turbo` 在 512×512 下**也返回了 HTTP 200**，而且三个响应的**字节数完全相同** —— 说明 `model` 参数被忽略了，给的是同一张缓存图。所以那个模型列表并不代表你实际会拿到什么。
- **`402` + 空 `{}` 响应体是它的通用拒绝形状，不是"这个模型收费了"的意思。** 同一个请求在不同时间结果会变，三次独立观察（都是间隔 1–2 秒逐个请求）：

  | 观察 | 结果 |
  |---|---|
  | **A**（13 次） | 方形 ≥1024（`1024x1024`/`1280x1280`/`1440x1440`）→ **200**；方形 <1024（`512x512`/`640x640`/`768x768`）→ **402**；**所有非方形 → 402** |
  | **B**（几分钟后） | `1280x720` → **200**，实际交付 `1024x576` |
  | **C**（再几分钟后） | **全部 402**，连方形也拒 |

  也就是说 402 至少覆盖三种我们没能完全拆开的原因：**画布小于 1024px、画布非方形、匿名额度被限流/用尽**。别把它当成稳定的尺寸规则。
- **它还会偷偷改尺寸。** 请求 `1024x1024` 有一次实际给回来 `768x768`（这个"缩水"现象在早期版本就记录过）。所以本插件响应里的 `canvas` 字段是**从成品图片文件里读出来的真实像素**，不是照抄请求参数。

  **实际后果：这个后端的输出形状和可用性都不能保证。** 适配器的做法是：先按比例请求应得的画布，**遇到 402 就自动退回方形重试一次**，并把 `canvas` / `retried_square` / `aspect_ratio_honored` 一起报给你。放在 `free-any` 链里的话，**建议把它排在最后**。

**3. 三个里只有智谱是真的耐用。**

`cogview-3-flash` 被智谱官方文档列为*免费模型*（免费图像生成模型）—— 这是产品级免费，不是促销。Agnes 的 $0 是**促销价**，其定价文档写了促销结束时间随时可能调整，而且它的更新日志显示免费档限速在 **2026-09-23 被砍半**。把 Agnes 当临时资源看。

---

## 环境要求

- Hermes Agent，且启用了 `image_gen` 工具集（多数平台默认开）
- `requests`（Hermes 自带依赖）
- 可选 API Key，见下文

## 安装

### 方式 A —— 直接拷目录（最稳）

```bash
git clone https://github.com/<you>/hermes-plugin-free-image-gen /tmp/fig
mkdir -p ~/.hermes/plugins/image_gen
cp -r /tmp/fig/image_gen/free-image-gen ~/.hermes/plugins/image_gen/free-image-gen
hermes plugins enable free-image-gen
```

Windows 下的目标是 `%LOCALAPPDATA%\hermes\plugins\image_gen\free-image-gen\`。

### 方式 B —— 用 Hermes 自带的安装器

`hermes plugins install` **支持指定仓库子目录**，这里必须这么用，因为生图后端必须落在 `~/.hermes/plugins/image_gen/<name>/`：

```bash
hermes plugins install <you>/hermes-plugin-free-image-gen/image_gen/free-image-gen --no-enable
hermes plugins enable free-image-gen
```

直接装仓库根目录的话，文件会被丢到后端扫描器根本不看的位置。

两种方式装完都这样验证：

```bash
hermes plugins show free-image-gen     # 期望看到 Status: enabled, Source: user
hermes plugins doctor "<路径>/plugins/image_gen/free-image-gen"   # 期望 OK ... exit 0
```

> `hermes plugins doctor` 要给路径（或完整 key），不能只给插件名。

## 配置

密钥放 Hermes 的 `.env`（`hermes config env-path` 查位置），**不要写进 `config.yaml`**。只用 Pollinations 的话，一个 key 都不用配。

```
ZHIPU_API_KEY=...      # https://www.bigmodel.cn/       → cogview
AGNES_API_KEY=...      # https://platform.agnes-ai.com/  → agnes
```

然后选后端：

```bash
hermes config set image_gen.provider cogview        # 或 pollinations | agnes | free-any

# 可选的单后端覆盖
hermes config set image_gen.cogview.model cogview-3-flash
hermes config set image_gen.agnes.model agnes-image-2.5-flash
hermes config set image_gen.agnes.size 2K           # 1K | 2K | 3K | 4K
hermes config set image_gen.pollinations.model sana
```

> 这些 `image_gen.<后端>.xxx` 属于插件自定义键，`hermes config set` 会提示 "not a recognized config key" —— **它照样写进去了，插件也照样读得到**，忽略即可，或者加 `--force` 免提示。

### 切换后端到底要不要重开会话？

实测结论（别被网上说法带偏）：

| 操作 | 要不要新开会话 |
|---|---|
| 在**已加载**的 provider 之间切换（`cogview` ⇄ `pollinations` ⇄ `agnes` ⇄ `free-any`） | **不用**。配置缓存按文件 mtime/size 失效，改完下一次调用立即生效 |
| 往 `.env` **新增** API Key | **要**。`.env` 是会话启动时载入 `os.environ` 的 |
| 新装 / 新启用一个插件 | **要**。插件在启动时扫描加载 |

### 画布（尺寸）—— 按各模型实际支持的来配

Hermes 只会给 provider 三种 `aspect_ratio`：`landscape` / `square` / `portrait`。但**每个模型能出什么形状，是这个模型自己的能力边界**，不是你想当然的通用需求 —— 老路做博客封面要 16:9，不代表别人也要 16:9。所以本插件的做法是：**每个适配器带一份它那个模型实际支持的画布作为默认值，你想改就按层覆盖。**

**配置优先级，越具体越优先：**

| 层 | 写法 | 作用 |
|---|---|---|
| ① 按比例（推荐） | `image_gen.<后端>.sizes.<比例>` | 只改某一个比例，另外两个走默认 |
| ② 全比例 | `image_gen.<后端>.size` | 三个比例统一用这一个画布 |
| ③ 适配器默认 | 不用配 | 该模型支持的画布 |

```bash
# 只把横版改成 2:1（竖版/方版不受影响）
hermes config set image_gen.cogview.sizes.landscape 1440x720
# 三个比例统一钉成 1024x1024
hermes config set image_gen.cogview.size 1024x1024
# Agnes 的档位和比例都能改
hermes config set image_gen.agnes.sizes.landscape 4K
hermes config set image_gen.agnes.ratios.landscape 4:3
```

点号路径可以直接写进嵌套结构，`hermes config set image_gen.cogview.sizes.landscape 1440x720` 实测能正确落地成 YAML 嵌套 map。

**⚠️ 故意不认全局 `image_gen.size`。** 一个全局值会把横版和竖版压成同一个画布 —— 那正是按比例配置要避免的事。

**各家模型实际支持的画布：**

| 后端 | 该模型支持什么 | 默认映射（landscape / square / portrait） |
|---|---|---|
| `cogview` 智谱 | **7 种官方枚举**：`1024x1024` `768x1344` `864x1152` `1344x768` `1152x864` `1440x720` `720x1440`；也收自定义（每边 512–2048px、能被 16 整除、总像素 ≤ 2²¹） | `1344x768` / `1024x1024` / `768x1344` |
| `agnes` | **尺寸档 + 比例**两段式：档位 `1K`/`2K`/`3K`/`4K`，比例自成一套（16:9、1:1、9:16…）—— **2K 及以上支持真正的非方形** | `2K`+`16:9` → **2624x1472**，`2K`+`1:1` → **2048x2048**，`2K`+`9:16` → **1472x2624**（均为实测值） |
| `pollinations` | 接受任意 `宽x高`，但**只是有时照做** —— 既会 402 拒绝，也会偷偷缩水（见上文第 2 条）；**形状和可用性都没有保证** | `1024x576` / `1024x1024` / `576x1024`（16:9 一对 + 方形） |

**响应里会如实告诉你实际出了什么：**

| 字段 | 含义 |
|---|---|
| `canvas` | **真实画布，从成品图片文件里读出来的**（如 `1344x768`）—— 不是照抄请求参数 |
| `canvas_source` | `config` = 用上了你的配置；`default` = 走的模型默认 |
| `aspect_ratio_honored` | 这个模型到底有没有做到你要的比例。`false` 基本意味着**这个模型做不到**（比如让 pollinations 出 landscape） |
| `supported_canvases` / `supported_size_tiers` | 该后端支持的画布清单，照着配就行 |

> `aspect_ratio_honored` 是**陈述事实，不是叫你裁图**。它把两种情况区分开：「这个模型压根做不到 16:9」和「你自己配成了 4:3」—— 至于怎么处理，由调用方决定。

### 关掉智谱的水印

智谱文档里这个接口有 `watermark_enabled` 参数：设为 `false` 会同时去掉可见的「AI生成」胶囊和隐式数字水印，**但只对已签署免责声明的账号生效**（签署路径：个人中心 → 安全管理 → 去水印管理）。

```bash
hermes config set image_gen.cogview.watermark_enabled false
```

没签的账号智谱会拒绝或照样带水印。默认**不发送**这个参数，所以不配就等于保持原样。

---

## 图片写到哪里

生成的图**直接落在你这次会话的工作目录**里 —— 也就是 Hermes 当前运行所在的那个文件夹（桌面版一般就是你打开的项目目录），图就放在你的工作旁边，而不是藏在某个缓存目录里。

解析顺序：

1. `image_gen.output_dir`（配置）—— 显式指定优先
2. `HERMES_IMAGE_OUTPUT_DIR`（环境变量）
3. 会话工作目录（先看 `TERMINAL_CWD`，再看进程 `cwd`）
4. Windows 下 `D:\hermes`，其它系统 `~`

```bash
hermes config set image_gen.output_dir "D:/my-project/covers"   # 固定到某处
```

文件命名是 `<后端>_<年月日>_<时分秒>_<随机>.<扩展名>`，比如 `agnes_20260930_204323_4ae6495e.png` —— 一眼就能看出是谁出的、哪一次出的。扩展名按文件头 magic bytes 嗅探，所以 CDN 拿 `application/octet-stream` 糊弄时（智谱就是这么干的），出图是 JPEG 就会老老实实叫 `.jpg`，不会骗你一个 `.png`。

如果目标目录不可写，图会退回存到 `$HERMES_HOME/cache/images/` —— 只读目录也不会让你丢图。

---

## 图片编辑 / 图生图 —— 只有 Agnes 支持

**智谱 CogView-3-Flash 和 Pollinations 都是纯文生图**（适配器报 `max_reference_images: 0`）。想要"给一张图改风格 / 保持构图重画"，只有 `agnes` 能做，最多 4 张参考图：

```python
# Hermes 会把本地路径传进来；插件负责转成 Agnes 要的 base64 data URI
generate(prompt, aspect_ratio, image_url="C:/path/to/source.png")
generate(prompt, aspect_ratio, reference_image_urls=["a.png", "b.png"])
```

**实测**：拿一张 2624×1472 的咖啡杯照片 + 提示词"转成黑白铅笔素描、保持构图"，Agnes 保留了原图的构图（木桌、两只窗户、木椅位置都没变）并成功转成素描风格。

> **踩过的坑（已在 v1.3.0 修掉）**：Agnes 的 `extra_body.image` **只接受 data URI 或 http(s) URL**，直接传本地路径会报 `extra_body.image is not a valid image base64`。插件现在会自动读文件、嗅探类型、编码成 `data:image/png;base64,...` 再发。

---

## `free-any` —— 自动兜底后端

### 为什么需要它

Hermes 的调度逻辑是**一次只激活一个 provider，而且失败不降级**。源码里 `get_active_provider()` 写得很明确：显式配了后端就直接返回它，**连 `is_available()` 都不检查**，设计意图是「宁可给你一个精准的报错，也不偷偷换后端」。

结果就是：你配了 cogview，它一旦返回 **429（限速）/ 402（要付费）/ 401（key 失效）/ 额度用完**，这次调用就直接失败了 —— 明明 pollinations 还能用，但 Hermes 不会去试。

`free-any` 把这个兜底补上：它按你配的顺序，依次调用其它已注册的 provider，**谁先出图就用谁**。

### 怎么用

```bash
hermes config set image_gen.provider free-any
hermes config set image_gen.free-any.order "[cogview, pollinations, agnes]"
```

不想记 `--force` 的话，也可以直接手写 `config.yaml`（`hermes config path` 查位置）：

```yaml
image_gen:
  provider: free-any
  free-any:
    order: [agnes, cogview, pollinations]   # 从左到右依次尝试
```

`order` 不配时默认就是 `[agnes, cogview, pollinations]` —— **好用的排前面，最不稳的排最后**（agnes 出 2K 无水印、还支持图片编辑；pollinations 见上文第 2 条，既会拒绝也会偷偷缩水）。名字可以写任意已注册的后端（包括 Hermes 自带的，比如 `fal`）。

### 行为细节

- **按顺序串行尝试**，第一个成功就返回，后面的不再调用。
- **跳过的三种情况**都会被记进响应的 `attempts` 里：后端没注册、后端没凭据（`is_available()` 为假）、后端抛异常。真正出错的后端会把它的 `error` / `error_type` 一起记下来。
- **全员失败时给聚合错误**，把每个后端为什么失败列全，而不是只报最后一个 —— 排查时这点很有用：

  ```
  All 2 backend(s) in the free-any chain failed —
  cogview: 智谱 returned HTTP 429: {"error":{"code":"1302"...}};
  pollinations: Request to Pollinations failed: HTTPSConnectionPool(...)
  ```

- **出图响应里的 `provider` 字段仍然是真正出图的那个后端**（比如 `cogview`），不会伪装成 `free-any`。同时多了几个字段方便你确认到底走了谁：

  | 字段 | 含义 |
  |---|---|
  | `requested_provider` | 固定是 `free-any`（你请求的） |
  | `provider` | **实际出图的后端** |
  | `fallbacks_used` | 前面失败/跳过了几个（0 = 第一个就成功） |
  | `chain` | 本次生效的完整链路 |
  | `attempts` | 逐个后端的尝试结果明细 |

- **显式传的 `model` 会转发给链上每个后端**。只有某一个后端认识的模型名照样能用：其它后端会因为不认识而报错，链自动往下走。所以 `free-any` 下不用纠结模型名。
- **自引用防护**：`order` 里写了自己（`free-any`）会被剔除并记 warning，不会无限递归。
- **`is_available()` 是"链上任一后端可用即为真"**。因为 pollinations 不需要 key 永远可用，所以只要它在链里，`free-any` 就是可用的。
- **`list_models()` 返回链上所有模型的并集**，每个都用 `· 后端名` 标出来源。

### 什么时候别用它

- 你需要**固定画布**：链上不同后端的画布不一样（见上面的画布表），`free-any` 兜到谁就是谁的画布。
- 你需要**图生图稳定可用**：只有 Agnes 支持 `extra_body.image[]`。虽然 `capabilities()` 会声明链上最强的能力，但真兜到 cogview/pollinations 时它俩不支持，会直接报错跳过 —— 也就是说图生图最终还是会落到 Agnes，或者失败。
- 你**要卡死成本**：链条里别放付费后端。

---

## 为什么写三个适配器而不是一个通用客户端

三个渠道都号称"OpenAI 兼容生图"，但**各自的意思都不一样**：

| | 端点 | 尺寸怎么表达 | 图生图 |
|---|---|---|---|
| 智谱 | `POST /paas/v4/images/generations` | `size: "1024x1024"` | 本插件不支持 |
| Agnes | `POST /v1/images/generations` | `size: "2K"` + `ratio: "16:9"` + `extra_body` | `extra_body.image[]` |
| Pollinations | **`GET`** `/prompt/{text}?width=&height=&model=` | URL 查询参数 | 不支持 |

Pollinations 甚至连 JSON 都不返回 —— 它直接吐原始图片字节，且不要任何鉴权头。一个通用请求构造器盖不住这些差异；所以每个适配器各约 60 行，共用底层管道（宽高比映射、按 magic bytes 嗅探真实文件扩展名、把成品图写进你的工作目录、统一的错误响应结构）。

## 排错

| 现象 | 原因 / 处理 |
|---|---|
| `ZHIPU_API_KEY not set` | Key 不在 `~/.hermes/.env` 里。加上，**新开会话**。 |
| `智谱 returned HTTP 401` | Key 错或过期。去 bigmodel.cn 重新生成。 |
| `智谱 returned HTTP 429` | 触发限速或额度用尽。**换成 `free-any`**，让它自动兜到下一个。 |
| `Pollinations 返回 402` | 两种原因：画布非方形（或小于 1024px）—— 免费档只稳出 1:1；或者匿名额度被限流。空 `{}` 响应体意味着"拒绝"，不等于"收费"。改成方形重试，或者这个 16:9 换别的后端出。 |
| Pollinations 返回 HTML/JSON 而不是图片 | 上游偶发错误，重试。适配器会拒绝非 `image/*` 响应，不会把垃圾存成文件。 |
| 工具报"没有可用的生图 provider" | `image_gen.provider` 不是 `cogview`/`pollinations`/`agnes`/`free-any` 之一，或插件不在 `plugins.enabled` 里。 |
| `free-any has no usable backend` | `image_gen.free-any.order` 里的名字一个都没注册上（拼错 / 对应插件没启用）。 |
| 同时启用了本插件和独立的 `agnes` 插件 | 两个都注册了 `agnes`，后注册的覆盖先注册的。只留一个。 |
| `hermes config set` 提示 "not a recognized config key" | 这些是插件自定义键，提示归提示，值**已经写进去了**，插件读得到。加 `--force` 可免提示。 |

## 出处

由 [老路 (lusdaily.com)](https://lusdaily.com) 编写。上文每一条免费声明和每一条限制，都是对着各家的官方文档核过、并在开发过程中**真实发起 API 调用**验证的 —— 包括用三个适配器各自真出图、以及让 `free-any` 在首个后端失败时真的兜到下一个。

Hermes Agent 由 Nous Research 以 MIT 协议开源。本插件为独立项目，与 Hermes、Nous Research、智谱、Pollinations、Agnes AI 均无关联。

## 许可

MIT —— 见 [LICENSE](LICENSE)。
