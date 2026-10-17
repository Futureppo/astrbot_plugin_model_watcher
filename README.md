# astrbot_plugin_model_watcher

监控官方或自定义 API 的模型目录，检测新增、下架和属性变化，并向各条目配置的 UMO 白名单推送图片卡片。

- 作者：Futureppo
- 版本：0.0.8
- AstrBot：4.26.7 或更高的 4.x 版本
- 支持 Windows、macOS、Linux，推送能力由对应平台适配器提供。

更新记录见 [CHANGELOG.md](CHANGELOG.md)。

## 安装与启用

在 AstrBot 插件管理中选择通过链接安装，填写：

```text
https://github.com/Futureppo/astrbot_plugin_model_watcher
```

将本目录放入 `data/plugins/astrbot_plugin_model_watcher`，然后重启 AstrBot 或在插件管理中重新加载插件。通过插件管理安装时，AstrBot 会处理 `requirements.txt`；手动安装时请在 AstrBot 的 Python 环境安装这些依赖。

进入插件配置，在“提供商配置表”点击“添加条目”，选择模板，填写 API Key 和推送目标后保存。同一模板可以添加多次，例如监控不同账号可见的模型。配置保存后 AstrBot 会重载插件。

各内置提供商的 API Key 说明附有官网获取地址，复制到浏览器打开即可。国内站、国际站使用对应地址；Hugging Face 提供访问令牌地址，Vertex 提供服务账号管理地址。

默认没有监控条目。新条目第一次成功请求只建立基线，不推送已有模型；以后才推送变化。插件不调用模型进行可用性测试。

## 提供商模板

内置 16 个提供商/区域模板，以及 1 个自定义模板。同一模板可以重复添加。

| 模板 | 默认基础 API | 实际请求地址 |
| --- | --- | --- |
| OpenRouter | `https://openrouter.ai/api` | `https://openrouter.ai/api/v1/models?output_modalities=all` |
| OpenAI | `https://api.openai.com` | `https://api.openai.com/v1/models` |
| xAI | `https://api.x.ai` | `https://api.x.ai/v1/models` |
| Kimi / Moonshot | `https://api.moonshot.cn` | `https://api.moonshot.cn/v1/models` |
| DeepSeek | `https://api.deepseek.com` | `https://api.deepseek.com/v1/models` |
| Kimi / Moonshot（国际站） | `https://api.moonshot.ai` | `https://api.moonshot.ai/v1/models` |
| Groq | `https://api.groq.com/openai` | `https://api.groq.com/openai/v1/models` |
| Mistral AI | `https://api.mistral.ai` | `https://api.mistral.ai/v1/models` |
| Cerebras | `https://api.cerebras.ai` | `https://api.cerebras.ai/v1/models` |
| NVIDIA NIM | `https://integrate.api.nvidia.com` | `https://integrate.api.nvidia.com/v1/models` |
| 硅基流动（中国站） | `https://api.siliconflow.cn` | `https://api.siliconflow.cn/v1/models` |
| SiliconFlow（国际站） | `https://api.siliconflow.com` | `https://api.siliconflow.com/v1/models` |
| 阶跃星辰 StepFun | `https://api.stepfun.com` | `https://api.stepfun.com/v1/models` |
| Hugging Face Inference Providers | `https://router.huggingface.co` | `https://router.huggingface.co/v1/models` |
| Google Gemini | `https://generativelanguage.googleapis.com` | `/v1beta/models`，自动读取 `models` 和 `name` |
| Vertex AI / Model Garden | `https://us-central1-aiplatform.googleapis.com` | `/v1beta1/publishers/*/models`，读取所有厂商的目录和模型版本 |
| 自定义 | 留空 | 使用你填写的地址 |

OpenRouter、NVIDIA、Hugging Face 的公开模型目录当前可免密钥获取，提供商后续可能调整认证要求。其他模板需要对应提供商及区域的 API Key，最终可见的模型以接口和账号权限为准。模型目录可公开查询不代表模型推理服务免费。

### OpenRouter

默认读取全部渠道、全部模型类型，不按提供商或模型厂商筛选。标准模型目录会自动补充 `output_modalities=all`，包含文本、图像、视频、语音、Embedding、重排等接口公开的类型，并完整读取分页。[OpenRouter 官方文档](https://openrouter.ai/docs/api/api-reference/models/get-models)说明，不传此参数时默认只返回支持文本输出的模型。

此行为适用于 OpenRouter 官方地址和 OpenRouter 模板的标准 `/v1/models` 地址。自定义完整 API 已显式设置的 `output_modalities` 或其他筛选条件会保留；若要监控完整目录，请使用表中的无筛选地址。同一模型在多个托管渠道提供时，按 OR 的模型 ID 记录一次。

首次成功获取完整目录后仅保存基线，随后监控变化，不集中推送已有模型。

### Google Gemini

添加 **Google Gemini** 条目，将 Google AI Studio 生成的普通 API Key 填入 **API Key**。插件使用 `x-goog-api-key` 请求 Gemini API 模型目录，并自动跟随 `nextPageToken` 获取后续页面。

模板已填写完整 API `https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000`，模型列表路径为 `models`，模型 ID 路径为 `name`。完整 API 留空时，基础 API 自动补充 `/v1beta/models`；已有 `/v1` 或 `/v1beta` 时只补充 `/models`。

### Vertex AI / Model Garden

1. 添加 **Vertex AI / Model Garden** 条目。
2. 在本地用文本编辑器打开 Google Cloud 服务账号密钥 JSON 文件。
3. 将从 `{` 到 `}` 的完整 JSON 内容粘贴到 **API Key（服务账号 JSON）** 多行输入框。无需上传文件，也不要只复制 `private_key` 的内容。
4. 配置推送目标并保存。首次成功读取仍然只建立基线。

凭证应包含 `type: "service_account"`、`project_id`、`client_email` 和 `private_key` 等字段。插件从 JSON 读取项目 ID，在本地签名，通过 Google OAuth 换取访问令牌；令牌缓存于内存并在到期前刷新，遇到 401 时刷新后重试一次。认证和目录请求使用该条目的同一 HTTP 客户端及代理。

项目需启用 Vertex AI API，服务账号需具备读取模型目录的权限。插件通过 `X-Goog-User-Project` 指定凭证中的项目，服务账号还需该项目的 `serviceusage.services.use` 权限（例如 Service Usage Consumer 角色）。遇到 403 时请检查 API 是否启用及服务账号权限。

默认使用 `us-central1` 接口和 `publishers/*`，目录范围与 `gcloud ai model-garden models list` 一致，包含各厂商的 Model Garden 模型。模板预设 `listAllVersions=true` 及 `filter=is_hf_wildcard(false)`；Hugging Face 全站可部署模型搜索属于另一目录模式。每个版本以 `name@versionId` 识别，避免同名的不同版本互相覆盖。此列表表示目录收录情况，实际调用或部署仍取决于项目权限和模型要求。

服务账号 JSON 由 AstrBot 配置保存；私钥、JSON 原文和访问令牌不会写入卡片、模型快照或插件日志。替换凭证后重新建立基线。

官方说明：[Gemini 模型列表](https://ai.google.dev/api/models#method:-models.list)、[Model Garden 模型列表](https://docs.cloud.google.com/sdk/gcloud/reference/ai/model-garden/models/list)。

## 条目设置

| 设置 | 说明 |
| --- | --- |
| 条目名称、启用 | 自定义显示名称，独立启停 |
| 基础 API | 自动补充 `/v1/models`，已有 `/v1` 时只补充 `/models` |
| 完整 API | 优先级高于基础 API，按原地址发送 GET |
| 模型列表路径 | 普通模板留空为 `data`；Gemini 为 `models`，Vertex 为 `publisherModels`；根数组填写 `$` |
| 模型 ID 路径 | 普通模板留空为 `id`；Google 模板为 `name`；字符串数组直接使用字符串 |
| API Key | 普通模板使用 Bearer；Gemini 填普通密钥；Vertex 粘贴完整服务账号 JSON |
| 更新间隔 | 默认 30 秒，每轮完成后等待此间隔；非法值按 30 秒处理 |
| 代理地址 | 留空直连，不使用系统代理；支持 HTTP、HTTPS、SOCKS5、SOCKS5H |
| 推送 UMO 白名单 | 每个条目独立，多选群聊或私聊；留空仅监控和保存快照 |
| 检测项目（可多选，仅 OpenRouter） | 可组合选择新增/下架、价格、上下文、能力、停用日期等项目；全部取消则暂停变化通知 |
| 检查内容（其他渠道） | 保留“仅模型 ID”或“模型 ID 和属性”单选，默认比较模型 ID 和全部原始属性 |
| 忽略属性路径 | OpenRouter 在已选项目中排除属性；其他渠道在“模型 ID 和属性”模式中排除属性 |

每个条目独立使用 HTTP 客户端和轮询任务，单次目录请求（包含必要的令牌刷新）最多等待 15 秒。目录接口使用 GET，认证方式由模板确定；自定义模板使用可选 Bearer 认证。Vertex 换取令牌使用 OAuth POST，不调用模型推理接口。

### 非标准 JSON 示例

标准返回无需填写路径：

```json
{"data": [{"id": "model-a", "context_length": 128000}]}
```

下面的返回填写模型列表路径 `result.models`、模型 ID 路径 `name`：

```json
{"result": {"models": [{"name": "model-a", "pricing": {"prompt": "0.000001"}}]}}
```

根数组填写模型列表路径 `$`：

```json
["model-a", "model-b"]
```

路径按点号访问对象，数字访问数组下标，例如 `batches.0.models`。不支持过滤表达式、通配符或键名中包含点号的转义。模型 ID 必须是非空字符串且不能重复。

普通模板跟随同源的 `links.next` 分页链接，Google 模板使用 `nextPageToken` 并保留原请求参数。最多读取 100 页，重复链接或令牌、分页字段类型错误、任一页失败都会放弃本轮结果并保留基线。`links.next` 缺省、为 `null` 或空字符串时表示结束；布尔值、数字、数组或对象不作为结束标记。其他分页协议需要提供返回完整目录的接口。HTTP 重定向不会自动跟随，请填写最终 API 地址。

### 属性变化

**仅 OpenRouter 模板**将原“检查内容”改为“检测项目（可多选）”，可以同时勾选需要的项目，选中的项目显示为标签。以下分类依据 OR 返回的模型字段，不套用于其他渠道。同一轮的多个项目变化合并推送。

其他 16 个模板使用“检查内容”单选：“仅模型 ID”只检查新增/下架；“模型 ID 和属性”比较渠道返回的全部原始 JSON 属性，默认使用后者。支持忽略属性路径，数组顺序、价格字符串和供应商自定义字段均按原始值比较，不使用 OR 的字段分类或降噪规则。

| 检测项目 | 字段范围 |
| --- | --- |
| 模型新增、下架 | 模型 ID 集合的增加、移除 |
| 价格与计费规则 | `pricing`，包括缓存、图像、音频、搜索及条件计费规则 |
| 上下文与输出长度 | `context_length`、`top_provider.context_length`、`top_provider.max_completion_tokens`、`per_request_limits` |
| 输入、输出类型 | `architecture.modality`、`architecture.input_modalities`、`architecture.output_modalities` |
| 工具调用与支持参数 | `supported_parameters` |
| 推理能力与默认配置 | `reasoning`、`default_parameters`，包括默认采样参数 |
| 语音音色 | `supported_voices` |
| 停用日期 | `expiration_date` |
| 模型别名指向 | `alias_target.slug`、`canonical_slug` |
| 其他元数据（高级） | 未归入以上类别或评分的字段，如名称、描述、知识截止日期、分词器、审核信息和自定义字段 |
| 评分与排行榜 | `benchmarks` |

每项独立生效。例如仅选择价格与停用日期时，不通知模型新增/下架或介绍修改。“其他元数据”不会重新启用取消勾选的价格、能力或评分类别。

新建 OpenRouter 条目默认只勾选模型新增、下架。建议按需增加价格、上下文、能力、停用日期和别名指向，评分项目默认不选。OR 全部取消时清除待发通知，继续更新原始快照；恢复勾选后不补发暂停期间的历史变化。

OR 实际变更检测项目或忽略路径时清理旧规则的待发通知，保留快照；选择顺序或重复项不影响检测范围。

自定义模板即使填写 OR 官方地址，也使用自定义模板的单选配置；需要分类多选时使用 OpenRouter 模板。

OR 比较时忽略模型列表顺序、对象键顺序，以及模态、参数、音色、推理档位列表的顺序和重复项。其他数组仍保留顺序语义，尤其价格覆盖规则的优先级顺序。OR 列表通知展示新增/移除项，其他变化显示分类、字段路径和旧值/新值；从缺失或 `null` 补为有值时标注“信息补全”。这些是接口声明的变化，插件不执行模型能力测试。

OR 价格使用十进制数值比较，`0.000001` 与 `0.0000010` 不触发通知；缺失、`null`、零和实际费率变化仍区分。OpenRouter 的完整时段费率表保持不变、且新旧报价都出现在该表中时，不将既定时段切换重复推送为调价。阶梯规则、未知条件、费率表本身的变化和未列出的报价仍会通知。目录报价只代表接口给出的主要供应商摘要，不是逐渠道实时报价；图像/视频专用规格和按秒等 SKU 价格需使用对应专用目录，本次未扩展额外接口。

例如在忽略属性路径中分别添加：

```text
updated_at
pricing.prompt
```

填写 `pricing` 会忽略整个价格对象；填写 `$` 则忽略所有属性，OR 仅在勾选“模型新增、下架”时推送目录增减，其他渠道仍推送模型新增/下架。忽略路径相对于每个模型对象，而不是整个响应。修改忽略规则会同时应用于新旧快照。

## UMO 会话选择

UMO 下拉选择依赖 AstrBot WebUI 的 `_special: "select_umos"` 配置组件。使用已包含此组件的 WebUI 时，打开选择框即可读取 AstrBot 已知会话，按名称、平台或 UMO 搜索；可多选、删除，也可输入完整 UMO 后按 Enter 添加。

完整 UMO 格式：

```text
平台实例ID:GroupMessage:群会话ID
平台实例ID:FriendMessage:私聊会话ID
```

平台实例 ID 是 AstrBot 中配置的平台 ID，不是适配器类型名称。群聊和私聊支持情况、主动消息限制由平台适配器和平台服务决定。

普通 AstrBot WebUI 会将该字段显示为文本列表，可手动填写完整 UMO。会话下拉组件属于宿主 WebUI 扩展，本仓库提供插件本身。插件监控和推送在两种界面下均可使用。会话信息加载失败或平台离线不会清空已保存的选择。新增目标只接收之后的变化，删除目标会取消该目标尚未发送的消息。

## 持久化与失败处理

- 使用 AstrBot 插件 KV 存储保存原始模型快照、待发送消息及尚未送达的目标。
- 条目有自动生成的隐藏 ID，改名、修改轮询间隔、修改代理和调整顺序均不会重置基线。
- 更换有效请求地址、API Key 或解析路径会建立新基线，并清除旧来源的待发送消息。
- 禁用条目停止轮询并清除该条目的待发送消息，保留基线；删除条目会清除它的状态。
- 超时、HTTP 错误、JSON 格式或模型 ID 错误保留原基线；合法的空数组会产生模型下架通知。
- 一次变化保存为通知后按目标发送，失败目标下轮重试；API 暂时失败时仍会重试旧通知。
- 每个条目每轮向同一目标最多发送一条完整通知，积压消息按后续轮次处理，避免重启时连续集中推送。
- 正常运行和重启恢复时跳过已记录成功的目标。平台接受消息后、进度保存前发生进程崩溃，或平台超时但实际已发送时，仍可能重复一条。
- 获取失败的警告包含条目名称和 ID、API 类型、请求阶段（`catalog` 或 `oauth`；异常未附请求信息时为 `fetch`）、请求域名、是否配置代理、异常类型、HTTP 状态码及脱敏错误摘要。可据此区分具体条目，以及目录请求和 Google 令牌交换失败。
- 错误摘要提取接口的错误码、类型和说明，合并为单行并限制为 512 字符；HTML/XML 错误页仅提示响应类型，超过 64 KiB 的响应不记录正文。日志不打印完整请求地址、请求认证头或完整响应，已知密钥、查询参数值及代理凭证会脱敏。配置文件中的密钥遵循 AstrBot 原有的配置存储方式。

## 图片、字体与时间

卡片以浅色布局显示“提供商配置表”中的“条目名称”，下一行“网址”显示该条目填写的“基础 API”。即使配置了“完整 API”，卡片仍显示基础 API 原地址；基础 API 留空时显示“未填写基础 API”。下方显示检测时间、模型数量和变更内容，底部显示插件仓库地址。

每次变化生成一张长图，高度随内容增长，不分页、不显示页码。长文本自动换行，不截断旧值或新值。时间跟随 AstrBot 全局时区，未配置或时区无效时使用系统时区。

优先读取 AstrBot 数据目录下的 `font.ttf`，然后查找微软雅黑、苹方、Noto Sans CJK 或文泉驿等系统中文字体。缺少中文字体、图片渲染失败或图片超过 4000 万像素时，回退为一条包含完整内容的文本消息。图片尺寸与消息长度仍受平台限制，发送失败时保留待重试状态。Linux 无中文字体时可安装 Noto Sans CJK，或在数据目录提供可用的中文字体。

## 开发验证

在 AstrBot 项目根目录运行：

```sh
python -m pytest data/plugins/astrbot_plugin_model_watcher/tests -q
ruff format data/plugins/astrbot_plugin_model_watcher
ruff check data/plugins/astrbot_plugin_model_watcher
```

插件源代码使用 Python 3.10 兼容语法；运行时仍须满足当前 AstrBot 的 Python 版本要求。

现有测试使用模拟响应验证目录解析、检测规则、持久化、失败重试和通知渲染。Vertex 测试使用本地生成的测试密钥和模拟 OAuth 响应，不需要真实凭证。

本地测试通过不代表已完成所有提供商的真实认证和平台送达验证；实际接口可用性、账号权限和平台图片限制需要单独实测。
