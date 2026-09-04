# Neko AI V1.0 验收报告

验收依据：《微信 / QQ AI 托管系统项目计划书 V1.0》  
验收日期：2026-08-25  
默认发布门禁：`SIMULATION`

## 结论

Neko Core V1 的本地核心、控制后台、安全管线、模拟环境和影子环境已完成，自动化测试 **393 项全部通过**，其中包含 **1000 条消息稳定性演练**与超过 100 组安全不变量组合。

项目当前结论为：

> **核心软件有条件通过；DeepSeek、Ollama 与 MySQL 已取得真实环境证据，QQ Official 和 OpenAI 仍待外部验收；NapCat 为 Experimental，个人微信官方通道保持 Blocked，AutoWx 仅为 Draft Only。**

不能把“代码存在”伪装成“真实平台已经验收”。MySQL 专用数据库、DeepSeek 与 Ollama 已完成真实环境验收；V1 Definition of Done 中仍待完成真实 QQ 200 条测试和 OpenAI 实际 Key 测试，全部取得外部证据后才能改为完全通过。

2026-08-25 新增两条不改变 V1 验收口径的实验策略：`QQ_NAPCAT` 可在用户明确确认非官方个人 QQ 自动化风险后，作为受限真实通道逐级进入 SHADOW / 单联系人 LIVE；`WECHAT_AUTOWX` 只接收入站并生成草稿，后端没有自动发送能力。二者均不替代 QQ Official Bot 200 条验收，也不把个人微信状态伪装成正式支持。

## 五大成功目标

| 目标 | 结果 | 证据 |
|---|---|---|
| G1 自动回复 | 通过（SIMULATION） | 白名单消息完成 Input → Policy → LLM → Guard → Scheduler → Simulator Send |
| G2 自动记录 | 通过 | Messages、Audit Logs、Incidents、Token、Memory、Summary 数据模型与 API |
| G3 可控 | 通过 | AUTO / SILENT / READ_ONLY / STOPPED、人工接管、恢复、Kill Switch |
| G4 安全 | 通过 | 组合不变量、群聊、夜间、正式事务、密钥、限频与错收件人测试 |
| G5 稳定 | 通过（模拟） | 1000 事件零丢失、零重复发送、零错误收件人、重复 webhook 去重 |

## 自动化结果

```text
393 passed
1000 inbound events recorded
500 allowed simulator replies sent to exact bound targets
500 non-whitelisted/manual-only events blocked
duplicate webhook replay: 0 additional sends
memory isolation: 100 / 100 passed
```

前端生产构建与检查：通过。  
后端健康检查：通过。  
首次启动管理员门禁：通过，无预设密码。

普通健康接口本机 200 次测量：P50 `1.21 ms`，P95 `1.70 ms`，最大 `10.03 ms`，满足非 LLM 后台请求 P95 `< 300 ms` 的目标。该结果是本机当前环境证据，不代表其他机器的性能承诺。

## MVP 验收表

### 自动回复

- [x] 白名单可以正常自动回复（Simulator）
- [x] 非白名单绝不发送
- [x] 夜间绝不发送
- [x] Manual Only 绝不发送
- [x] 群聊没有 @ 绝不发送

### AI

- [x] DeepSeek Provider 实现
- [x] OpenAI Provider 实现
- [x] Ollama Provider 实现
- [x] Provider 重试一次后 fallback，全部失败不发送
- [x] Token 统计落库并在 Dashboard 汇总
- [x] 独立 Usage 页面按最近 7 天及 Provider / 模型显示消息、输入、输出和总 Token
- [x] DeepSeek 真实 Key 完成连接、真实聊天与受控失败处理；错误模型返回 `ALL_PROVIDERS_FAILED` 且零发送，恢复正确模型后再次聊天成功
- [ ] OpenAI 真实 Key 连接测试（需要用户 Key）
- [x] Ollama 本机 `llama3.1:8b` 完成连接、真实聊天与受控失败处理；无效模型返回 `ALL_PROVIDERS_FAILED` 且零发送，恢复后再次由 Ollama 聊天成功

### Persona

- [x] Prompt 分层：Safety > Contact Rules > Persona > Memory > Message
- [x] 用户聊天风格配置
- [x] 轻微猫咪人格
- [x] 联系人显示名、关系、聊天风格与独立提示词可在后台完整维护
- [x] Guard 收敛机械重复“喵”
- [x] 金钱与正式事务承诺由代码拦截
- [x] IMPORTANT 联系人启用更严格的强承诺拦截与 400 字回复上限

### Memory

- [x] 联系人拥有独立 Memory
- [x] 100 次跨联系人 Memory Leak 测试为 0
- [x] 后台可删除 Memory
- [x] 联系人可关闭 Memory
- [x] 密码、API Key、Token、私钥、银行卡、验证码禁止写入
- [x] 会话摘要自动维护、只进入当前会话上下文，并隐藏常见密钥格式；情绪/态度只记录联系人明确表达，不自动猜测
- [x] 白名单联系人自动提取五类保守 Memory；去重、100 条上限、敏感内容硬拦截

### Dashboard

- [x] 微信状态（Blocked / Experimental）
- [x] QQ 状态
- [x] AI 与 Provider 状态
- [x] Active Conversations
- [x] Need Attention
- [x] Token Usage
- [x] Logs / Incidents
- [x] Simulator、联系人、群聊、账号、人格、Memory、发布门禁页面
- [x] 凭据可改名、轮换与删除；Provider 可完整编辑、测试、排序、启停与删除
- [x] WebSocket 自动重连并显示连接状态与 `Generating…` 实时生成状态
- [x] QQ 200 条真实验收进度、急停证据与系统违规判定面板
- [x] NapCat OneBot 11 与 AutoWx 草稿策略在“连接账号”中可配置；风险确认、凭据加密、默认关闭和接入说明均可见
- [x] 外部验收就绪清单逐项显示 MySQL、DeepSeek、OpenAI、Ollama 与 QQ 证据，不返回任何凭据
- [x] 首页独立显示 AI 运行状态；实时会话显示白名单、重要度、AI、模型、Memory 与最后活跃时间
- [x] 首页可统一或分类型控制白名单图片、语音、文件/视频落盘，配置单文件上限与纯媒体 AI 回复；会话页提供鉴权预览、播放和下载
- [x] 首页可独立控制本机媒体理解、图片/语音/文档解析器、视觉与 Whisper 模型、设备、显式首次下载和提取字符上限；会话页显示每个附件的识别结果或失败码

### Safety

- [x] Human Takeover
- [x] Silent
- [x] Read Only
- [x] Stopped
- [x] Global Kill Switch
- [x] 单联系人 / 连续 / 每日发送 Rate Limit
- [x] Duplicate Protection
- [x] 20 轮提醒 / 40 轮冷却
- [x] 重启取消历史 Queued 消息
- [x] LLM 超时只重试一次，全部失败不发送并生成 Incident
- [x] 同类模型或 Connector 故障在 10 分钟内达到 3 次后自动熔断到 SILENT
- [x] Connector 断连不重复发送；重复投递仍由 message_id 去重
- [x] READ_ONLY 只接收与展示，不生成摘要、不提取 Memory、不调用模型
- [x] 停用 QQ 同时关闭入站、取消待发消息与正在运行的 QQ 验收；禁用连接器不会发起网络请求
- [x] NapCat 只允许回环 HTTP(S) 地址、原生 HMAC `x-signature`/Bearer Token 入站和精确目标发送；远程 API 地址被拒绝
- [x] LIVE 时间门禁可在后台启停并设置普通/跨午夜时段；修改会取消待发消息，LIVE 每次发送前重新读取并复检，SHADOW 永不调用真实发送接口
- [x] AutoWx 在 LIVE 下也会在模型调用前按 `CHANNEL_DISABLED` 阻止，发送连接器固定返回 `AUTOWX_DRAFT_ONLY`
- [x] 联系人/群聊安全变更、人工接管、账号停用与发布门禁切换和最终发送串行化
- [x] 生成期间撤销白名单、停用账号或将 LIVE 降级，最终复检均取消消息且真实 Connector 调用为 0
- [x] 数据库连接池释放后可重新连接
- [x] 每分钟频控基于数据库中的 SENT / QUEUED 预约，跨请求、重启与并发重建仍生效
- [x] 6 条并发消息最多 5 条获得发送许可
- [x] WebSocket 登录鉴权、心跳与失效连接清理；事件触发后台刷新，心跳不触发数据轮询
- [x] 启动脚本只绑定本机回环地址，记录实际服务进程，同时等待前后端就绪；端口被占用时拒绝覆盖现有进程

### Logging

- [x] MESSAGE_RECEIVED
- [x] POLICY_PASS / POLICY_DENY
- [x] LLM_REQUEST / LLM_RESPONSE
- [x] SAFETY_PASS / SAFETY_DENY
- [x] MESSAGE_QUEUED / SENT / SHADOWED
- [x] ERROR / INCIDENT
- [x] Incident 表记录与 `INCIDENT` 审计事件同时生成
- [x] 原始消息独立保存 sender、receiver、平台事件时间、方向、类型、平台、会话与内容
- [x] NapCat 白名单私聊在 SHADOW / LIVE 均保存联系人入站、AI 结果与本人手动回复；开启 `reportSelfMessage` 后识别 `message_sent`，并把 Neko 自身 AI 发送回显关联回原消息，避免重复语料和误触发人工接管
- [x] NapCat Array 消息中的图片、语音、文件与视频均保存完整消息信封和附件元数据；默认受控落盘，语音优先转 MP3，失败/超限/关闭时保留明确状态；非白名单媒体不入库
- [x] NapCat 后台可保存多个账号配置并单活切换；切换在发送锁内停用旧账号并取消 NapCat 待发队列，账号地址和 Token 分别保存且不会返回明文
- [x] NapCat 联系人可单独授权每日续火；该权限不开放普通 AI 入站，真实发送仍受 LIVE、AUTO、急停、时间窗、绑定账号和每日总量门禁约束，并用每日持久化尝试标记防止崩溃重复发送
- [x] 白名单图片可经本机 Ollama `qwen3-vl:4b` 描述/OCR，语音可经 `faster-whisper small` 转写，文本/DOCX/PDF 可只读提取；纯媒体识别失败时不生成猜测性回复，附件指令不能覆盖系统规则，提取出的密钥/正式事务仍在回复模型前拦截
- [x] 日志不记录完整 API Key

## Release Gate

### SIMULATION — 通过

- 创建模拟联系人；
- 输入模拟消息；
- 完整生成与策略轨迹；
- Kill Switch 与去重生效；
- 1000 事件测试通过。

### SHADOW — 功能通过，等待真实消息观察

- 后台必须先确认 SIMULATION 验收；
- 生成内容进入 `SHADOWED`，不调用真实发送；
- QQ 官方 Webhook 已实现回调验证、Ed25519 请求验签、op 12 快速 ACK、C2C/群消息规范化与后台处理；
- 有效事件先进入持久化 Connector Inbox，再 ACK；重启恢复未完成事件，处理最多三次；
- SHADOW 不再等同于 SILENT：AI 会生成并记录 `SHADOWED`，但真实 Connector 不会被调用；
- 必须存在 QQ 官方或 NapCat 的真实入站、至少一条影子回复且用户确认后，才可通过 SHADOW；首次 LIVE 恰好允许所选通道的一个测试联系人；
- 选择官方路线时，用户仍需要使用自己的 QQ Bot、公开 HTTPS 回调入口和真实聊天环境观察后确认；选择 NapCat 时则必须使用专用测试账号与本机 OneBot 配置承担实验风险。

### LIVE — 门禁关闭

只有以下条件同时成立才能进入：

1. SIMULATION 已人工确认；
2. SHADOW 已人工确认；
3. QQ 官方 Bot 或 NapCat 中恰好一个通道已配置、启用并收到有效真实入站；
4. 如选择 NapCat，必须确认非官方个人 QQ 自动化风险，API 仅允许本机回环地址；
5. 从所选通道的一个测试联系人开始；
6. AutoWx 不允许进入自动发送。

这里的 NapCat LIVE 是用户选择的实验运行能力，不代表 V1.0 “QQ Official Bot 200 条 E2E”完成。

## 外部环境待验收

| 项目 | 当前状态 | 完成条件 |
|---|---|---|
| QQ Official Bot 200 条 E2E | 待执行 | 用户配置官方机器人凭据与公开 HTTPS 回调入口，按 1 个测试联系人启动后台验收器；唯一入站达到 200、急停测试生效、收件箱清空且系统违规为 0 |
| DeepSeek | 已通过（2026-08-24） | 真实连接测试成功；SIMULATION 中真实聊天两次成功并记录 Token；错误模型受控演练产生 `LLM_PROVIDER_FAILURE`、返回 `ALL_PROVIDERS_FAILED` 且零发送；恢复正确模型后再次通过 |
| OpenAI | 待执行 | 用户在后台添加真实 Key，完成连接、聊天和受控失败处理 |
| Ollama | 已通过（2026-08-24） | 本机 Ollama `0.32.14` 与 `llama3.1:8b` 连接成功；SIMULATION 完整管线由 `ollama` 生成并记录 Token；无效模型受控演练于 15:21:05 产生 `LLM_PROVIDER_FAILURE`、返回 `ALL_PROVIDERS_FAILED` 且未生成出站；恢复 `llama3.1:8b` 后于 15:21:51 再次由 Ollama 成功，DeepSeek fallback 同时恢复启用 |
| MySQL | 历史验收通过；新增 head 待完整复验 | 用户专用 MySQL 已于 2026-08-26 原位升级至 `20260826_0007 (head)`；2026-08-24 的空库迁移、核心 CRUD 与断开重连证据基于旧 head，仍需按 `0007` 重新执行完整验收 |
| 微信个人账号 | 官方通道 Blocked；AutoWx Draft Only | AutoWx 仅允许本机 Token 入站与影子草稿，自动发送硬性关闭；出现可接受官方授权渠道后再评估正式 Connector |
| NapCat 个人 QQ | Experimental / 未做真实外部测试 | 用户确认平台风险，配置本机 OneBot HTTP Server 与 HTTP Client，先完成 SHADOW 单联系人观察；不计入 QQ 官方 200 条验收 |

## 不提供的能力

- 自动加好友、自动拉群、营销群发、批量私聊；
- 删除消息或文件；
- 发邮件、操作日历、付款或财务操作；
- 微信/QQ 协议逆向、Hook、设备指纹修改与反检测；AutoWx 不执行自动点击发送；
- 由 LLM 直接调用发送、账号或系统工具。

## 本轮补充的结构证据

- QQ 官方直连回调：`POST /api/v1/connectors/qq/webhook`，OpenAPI 中可见；
- QQ 官方出站协议：自动获取 Access Token，C2C 使用 `/v2/users/{openid}/messages`，群聊使用 `/v2/groups/{group_openid}/messages`，回复绑定入站 `msg_id`；
- 官方签名规则：AppSecret 派生 Ed25519 密钥，验证 `timestamp + raw body`，回调验证签名 `event_ts + plain_token`；
- QQ 真实验收器：`/api/v1/acceptance/qq/*` 持久化验收批次，只统计能对应官方 Webhook 持久化收件箱的唯一入站；自建桥接或手工消息不计数，并自动判定重复发送、错收件人、急停违规和积压事件；
- 实时通信：WebSocket 对无效会话返回 `4401`，支持心跳，推送消息、模式、账号与 Incident 状态，并清理失效连接；
- 数据库版本：Alembic `20260826_0007 (head)`；独立空白 SQLite 从零升级成功，用户专用 MySQL 已原位升级；新增附件识别状态/模型/文本字段与本机媒体理解策略，旧 head 的 MySQL 空库/CRUD/重连证据需按 `0007` 完整复验；
- 故障演练：模型超时、Connector 断连、重复回调、进程重启、数据库重连、畸形事件，以及图片/语音/文件保存成功、关闭和失败降级均有自动化证据。
- 停止竞态演练：消息生成期间撤销白名单、停用 QQ 或降低发布门禁，发送前复检均从数据库获取最新状态并取消队列消息。
- 实验连接器边界：NapCat 出站只调用本机 OneBot 11 `/send_private_msg` / `/send_group_msg`，入站使用持久化 Inbox 与原生 HMAC `x-signature`（同时兼容 Bearer Token）；AutoWx 只接收入站并强制 Draft Only。

计划书逐项状态、技术偏差和最终外部完成条件见 [`ACCEPTANCE_MATRIX_V1.md`](ACCEPTANCE_MATRIX_V1.md)。
