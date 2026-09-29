# astrbot_plugin_model_watcher

监控官方或自定义 API 的模型目录，检测新增、下架和属性变化，并向各条目配置的 UMO 白名单推送图片卡片。

- 作者：Futureppo
- 版本：0.0.4
- AstrBot：4.26.7 或更高的 4.x 版本
- 支持 Windows、macOS、Linux，推送能力由对应平台适配器提供。

## 安装与启用

在 AstrBot 插件管理中选择通过链接安装，填写：

```text
https://github.com/Futureppo/astrbot_plugin_model_watcher
```

将本目录放入 `data/plugins/astrbot_plugin_model_watcher`，然后重启 AstrBot 或在插件管理中重新加载插件。通过插件管理安装时，AstrBot 会处理 `requirements.txt`；手动安装时请在 AstrBot 的 Python 环境安装这些依赖。

进入插件配置，在“提供商配置表”点击“添加条目”，选择模板，填写 API Key 和推送目标后保存。同一模板可以添加多次，例如监控不同账号可见的模型。配置保存后 AstrBot 会重载插件。

默认没有监控条目。新条目第一次成功请求只建立基线，不推送已有模型；以后才推送变化。插件不调用模型进行可用性测试。

## 提供商模板

内置 19 个提供商/区域模板，以及 1 个自定义模板。同一模板可以重复添加。

| 模板 | 默认基础 API | 实际请求地址 |
| --- | --- | --- |
| OpenRouter | `https://openrouter.ai/api` | `https://openrouter.ai/api/v1/models` |
| OpenAI | `https://api.openai.com` | `https://api.openai.com/v1/models` |
| xAI | `https://api.x.ai` | `https://api.x.ai/v1/models` |
| Kimi / Moonshot | `https://api.moonshot.cn` | `https://api.moonshot.cn/v1/models` |
| DeepSeek | `https://api.deepseek.com` | `https://api.deepseek.com/v1/models` |
| Kimi / Moonshot（国际站） | `https://api.moonshot.ai` | `https://api.moonshot.ai/v1/models` |
| Groq | `https://api.groq.com/openai` | `https://api.groq.com/openai/v1/models` |
| Mistral AI | `https://api.mistral.ai` | `https://api.mistral.ai/v1/models` |
| Together AI | `https://api.together.ai` | `https://api.together.ai/v1/models`，列表路径已设为 `$` |
| Cerebras | `https://api.cerebras.ai` | `https://api.cerebras.ai/v1/models` |
| SambaNova | `https://api.sambanova.ai` | `https://api.sambanova.ai/v1/models` |
| NVIDIA NIM | `https://integrate.api.nvidia.com` | `https://integrate.api.nvidia.com/v1/models` |
| 硅基流动（中国站） | `https://api.siliconflow.cn` | `https://api.siliconflow.cn/v1/models` |
| SiliconFlow（国际站） | `https://api.siliconflow.com` | `https://api.siliconflow.com/v1/models` |
| 阶跃星辰 StepFun | `https://api.stepfun.com` | `https://api.stepfun.com/v1/models` |
| Novita AI | `https://api.novita.ai` | 完整 API 已设为 `https://api.novita.ai/v3/openai/models` |
| DeepInfra | `https://api.deepinfra.com` | 完整 API 已设为 `https://api.deepinfra.com/v1/openai/models` |
| Hugging Face Inference Providers | `https://router.huggingface.co` | `https://router.huggingface.co/v1/models` |
| Chutes | `https://llm.chutes.ai` | `https://llm.chutes.ai/v1/models` |
| 自定义 | 留空 | 使用你填写的地址 |

OpenRouter、SambaNova、NVIDIA、Novita、DeepInfra、Hugging Face、Chutes 的公开模型目录当前可免密钥获取，提供商后续可能调整认证要求。其他模板需要对应提供商及区域的 API Key，最终可见的模型以接口和账号权限为准。模型目录可公开查询不代表模型推理服务免费。

Together AI 的返回值是根数组，模板已预填模型列表路径 `$`；Novita 和 DeepInfra 使用特殊路径，模板已预填完整 API，其优先级高于基础 API。上述模板仍可修改地址、解析路径和密钥。

## 条目设置

| 设置 | 说明 |
| --- | --- |
| 条目名称、启用 | 自定义显示名称，独立启停 |
| 基础 API | 自动补充 `/v1/models`，已有 `/v1` 时只补充 `/models` |
| 完整 API | 优先级高于基础 API，按原地址发送 GET |
| 模型列表路径 | 留空为 `data`；根数组填写 `$` |
| 模型 ID 路径 | 留空为 `id`；字符串数组直接使用字符串 |
| API Key | 留空不认证，填写后发送 Bearer 认证头 |
| 更新间隔 | 默认 30 秒，每轮完成后等待此间隔；非法值按 30 秒处理 |
| 代理地址 | 留空直连，不使用系统代理；支持 HTTP、HTTPS、SOCKS5、SOCKS5H |
| 推送 UMO 白名单 | 每个条目独立，多选群聊或私聊；留空仅监控和保存快照 |
| 忽略属性路径 | 排除不关心的属性；留空比较全部属性 |

每个条目独立使用 HTTP 客户端和轮询任务，单次 HTTP 请求最多等待 15 秒。完整 API 只支持 GET 和可选 Bearer 认证，不提供其他认证头或 POST 请求体配置。

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

`links.next` 分页链接会被继续获取，必须与起始 API 同源；最多读取 100 页，重复链接或任一页失败都会放弃本轮结果。其他分页协议需要提供返回完整目录的接口。HTTP 重定向不会自动跟随，请填写最终 API 地址。

### 属性变化

默认比较每个模型的全部 JSON 属性，忽略模型列表顺序和对象键顺序。属性中的数组保留顺序语义。字段增加、移除、类型或值变化会在卡片中显示旧值与新值。

例如在忽略属性路径中分别添加：

```text
updated_at
pricing.prompt
```

填写 `pricing` 会忽略整个价格对象；填写 `$` 则仅监控模型新增和下架。忽略路径相对于每个模型对象，而不是整个响应。修改忽略规则会同时应用于新旧快照。

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

- 使用 AstrBot 插件 KV 存储保存原始模型快照、待发送消息及每个目标的分页进度。
- 条目有自动生成的隐藏 ID，改名、修改轮询间隔、修改代理和调整顺序均不会重置基线。
- 更换有效请求地址、API Key 或解析路径会建立新基线，并清除旧来源的待发送消息。
- 禁用条目停止轮询并清除该条目的待发送消息，保留基线；删除条目会清除它的状态。
- 超时、HTTP 错误、JSON 格式或模型 ID 错误保留原基线；合法的空数组会产生模型下架通知。
- 一次变化保存为通知后按目标发送，失败目标下轮重试；API 暂时失败时仍会重试旧通知。
- 正常运行和重启恢复时跳过已记录成功的分页。平台接受消息后、进度保存前发生进程崩溃，或平台超时但实际已发送时，仍可能重复一页。
- 日志不打印 API Key、请求认证头或完整请求地址。配置文件中的密钥遵循 AstrBot 原有的配置存储方式。

## 图片、字体与时间

卡片以浅色布局显示“提供商配置表”中的“条目名称”，下一行“网址”显示该条目填写的“基础 API”。即使配置了“完整 API”，卡片仍显示基础 API 原地址；基础 API 留空时显示“未填写基础 API”。下方显示检测时间、模型数量和变更内容，底部显示插件仓库地址。

长文本自动换行，批量变化分页发送，不截断旧值或新值。时间跟随 AstrBot 全局时区，未配置或时区无效时使用系统时区。

优先读取 AstrBot 数据目录下的 `font.ttf`，然后查找微软雅黑、苹方、Noto Sans CJK 或文泉驿等系统中文字体。缺少中文字体或图片渲染失败时，自动发送同内容的分页文本。Linux 无中文字体时可安装 Noto Sans CJK，或在数据目录提供可用的中文字体。

## 开发验证

在 AstrBot 项目根目录运行：

```sh
python -m pytest data/plugins/astrbot_plugin_model_watcher/tests -q
ruff format data/plugins/astrbot_plugin_model_watcher
ruff check data/plugins/astrbot_plugin_model_watcher
```

插件源代码使用 Python 3.10 兼容语法；运行时仍须满足当前 AstrBot 的 Python 版本要求。
