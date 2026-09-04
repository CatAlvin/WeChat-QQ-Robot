# Neko AI V1.0 逐项验收矩阵

验收依据：《微信 / QQ AI 托管系统项目计划书 V1.0》  
证据日期：2026-08-25  
状态定义：`PASS_LOCAL`（本机证据通过）、`PASS_EXTERNAL`（用户真实外部环境证据通过）、`PENDING_EXTERNAL`（实现存在但必须用真实外部环境证明）、`DEFERRED_BY_PLAN`（计划书明确为后续阶段或不在 MVP）、`BLOCKED_COMPLIANT`（按安全边界关闭即为合规结果）。

## Definition of Done

| 项目 | 状态 | 权威证据 | 尚缺证据 |
|---|---|---|---|
| A. DeepSeek / OpenAI / Ollama 聊天 | PASS_EXTERNAL（DeepSeek、Ollama）/ PENDING_EXTERNAL（OpenAI） | 协议自动化全部通过；DeepSeek 与本机 Ollama 均完成真实连接、完整管线聊天、Token 落库、错误模型 `ALL_PROVIDERS_FAILED` 零发送及恢复后成功，并有 MySQL 审计证据 | OpenAI 真实 Key |
| B. 用户风格 + 猫咪人格 | PASS_LOCAL | `services/prompt.py` 的分层 Prompt；`test_ai_configuration.py` 和 Guard 测试 | 真实模型主观效果仍应在 SHADOW 人工观察，但不影响代码侧通过 |
| C. 联系人独立 Memory | PASS_LOCAL | 100 次跨联系人隔离、敏感内容阻断、删除/关闭、摘要隔离测试 | 无 |
| D. AUTO / SILENT / READ_ONLY / STOP / HUMAN | PASS_LOCAL | 控制 API、后台按钮、最终发送复检、模式语义与接管竞态测试 | 无 |
| E. Safety 100% | PASS_LOCAL | 当前全量自动化套件全部通过；超过 100 组安全不变量组合 | 真实 QQ 急停仍由 G 项共同验收 |
| F. 1000+ 稳定性 | PASS_LOCAL | 1000 入站演练；重复、错收件人、策略绕过、Memory Leak 均为 0 | 无 |
| G. 一个受支持真实 IM Connector E2E | PENDING_EXTERNAL | QQ Official Bot v2 出站、官方 Webhook、Ed25519 验签、持久化 Inbox、200 条验收器均已实现 | 用户 AppID/AppSecret、公开 HTTPS 回调入口和真实 QQ 200 条验收 |
| H. 微信安全边界 | BLOCKED_COMPLIANT | 官方个人微信 Connector 继续拒绝启用；AutoWx 仅提供经本机 Token 鉴权的入站与影子草稿，LIVE 自动发送硬性关闭 | 只有出现可接受官方授权方式后才改变正式支持状态 |

## 五大成功目标

| ID | 状态 | 证据结论 |
|---|---|---|
| G1 自动回复 | PASS_LOCAL / PENDING_EXTERNAL | Simulator 完整通过；真实 QQ 发送等待 G 项 |
| G2 自动记录 | PASS_LOCAL | 原始消息保存 sender、receiver、平台事件时间、类型、内容、完整信封与会话；白名单媒体附件保存元数据与受控本机副本；摘要、Memory、Audit、Incident、Token 均落库 |
| G3 可控 | PASS_LOCAL | 后台可查看、暂停、接管、恢复、停用账号与降低发布门禁；安全变更和发送串行化 |
| G4 安全 | PASS_LOCAL | 非白名单、正式关系、夜间、无 @ 群聊、密钥、承诺、频控、急停与停用竞态全部硬阻断 |
| G5 稳定 | PASS_LOCAL / PENDING_EXTERNAL | 模拟故障与恢复通过；真实平台长时间运行等待 QQ 200 条 |

## 阶段门禁

| Stage | 状态 | 说明 |
|---|---|---|
| 0 Simulator | PASS_LOCAL | 创建联系人、收消息、生成、记录、Kill Switch 均通过 |
| 1 Core Backend | PASS_LOCAL / PENDING_EXTERNAL_REVALIDATION | FastAPI、设置、数据模型、Audit、WebSocket 与自动化回归通过；SQLite 从空白迁移至 `20260826_0007 (head)`，用户专用 MySQL 已原位升级至 `0007`；历史空库、CRUD 与断线重连证据仍需按新增 head 完整复验 |
| 2 LLM System | PASS_LOCAL / PASS_EXTERNAL（DeepSeek、Ollama）/ PENDING_EXTERNAL（OpenAI） | 协议、重试、fallback、timeout、Token、凭据加密通过；DeepSeek 与本机 Ollama 的真实连接、聊天和受控失败处理通过，OpenAI 待测 |
| 3 Safety Engine | PASS_LOCAL | 自动化安全测试 100% 通过 |
| 4 Persona & Memory | PASS_LOCAL | Persona 分层与 100 次 Memory Leak=0 |
| 5 Admin Panel | PASS_LOCAL | Dashboard、会话、联系人、群聊、账号、AI、Memory、Usage、日志、发布门禁均无需改配置文件 |
| 6 QQ Official | PENDING_EXTERNAL | Connector 和验收器完成；真实 200 条未执行。NapCat 实验通道不计入本阶段证据 |
| 7 Voice | PASS_LOCAL（保存/播放/STT） | NapCat 白名单语音可经 `get_record` 转 MP3、落盘并在会话页播放；`faster-whisper small` 本机缓存就绪，并于 2026-08-26 使用 CUDA 成功转写已保存 AMR；每附件保存转写状态、模型、文本或失败码 |
| 8 Image | PASS_LOCAL（保存/预览/理解） | NapCat 白名单图片可即时落盘并在会话页预览；Ollama `qwen3-vl:4b` 于 2026-08-26 对已保存 JPEG 完成本机图片描述与可见文字提取；提取结果仅作为不可信联系人内容进入安全管线 |
| 9 NapCat 账号切换 | PASS_LOCAL | 多条账号配置独立保存本机地址与加密 Token；单活切换原子停用旧账号并取消待发队列。真正双进程并发仍未开放 |
| 10 每日续火 | PASS_LOCAL | 联系人级独立授权、账号绑定、每天一次持久化去重、本地独特模板，以及 LIVE/AUTO/急停/时段/总量复检已有自动化证据；尚未执行真实外部定时发送 |
| 9 Web Search / Weather | DEFERRED_BY_PLAN | 后续只读 Tool；不在 MVP Definition of Done |
| 10 WeChat Gate | BLOCKED_COMPLIANT | 官方个人微信保持 DISABLED；AutoWx 仅为 Draft Only 实验策略，不改变正式门禁结论 |

## MVP 最终验收表映射

- 自动回复：Simulator 五项全部通过；真实白名单回复并入 QQ 外部验收。
- AI：Provider 协议、失败与 Token 通过；三种真实环境仍待执行。
- Persona：个人风格、轻猫咪感、重复“喵”收敛、承诺阻断均有测试。
- Memory：隔离、无泄漏、删除、关闭均通过。
- Dashboard：首页展示微信、QQ、AI、Active Conversations、Need Attention 和 Token；独立 Usage 页提供七日及模型明细。
- Settings：外部验收就绪清单直接展示 MySQL、三种真实模型、QQ 配置/真实入站/200 条证据的最新状态，不暴露凭据。
- Safety：Human、Silent、Read Only、Stopped、Kill Switch、Rate Limit、Duplicate 全部通过。
- Logging：MESSAGE / POLICY / LLM / SAFETY / SEND / ERROR / INCIDENT 全部落库；7 天启动清理存在。

## 明确偏差与非阻断项

- 前端采用 Vinext 的 Vite 构建链，仍为 React + TypeScript；没有使用 shadcn 组件库。此项是实现技术偏差，不改变计划书的功能、安全或 Definition of Done 结果。
- 默认本地安全演练使用 SQLite；MySQL 驱动、连接 URL 和 Alembic 迁移已实现。用户专用 MySQL 已升级至媒体理解迁移 `20260826_0007`，但空库迁移、核心 CRUD 与断线重连仍须按该新增 head 重新跑完整外部验收，才可恢复“最新 head 已通过”的结论。
- `Knowledge` 在页面清单中出现，但计划书没有定义其 MVP 数据、操作或验收标准；当前未创建空壳页面，避免引入无验收依据的功能。
- 用户选择暂缓 QQ 官方公网回调排障后，新增 NapCat OneBot 11 受限策略与 AutoWx Draft Only 策略。NapCat 可用于实验性单联系人 SHADOW / LIVE，但不能替代 G 项 QQ Official 200 条证据；AutoWx 永远不能自动发送。

## 完全通过的最终条件

只有同时取得以下证据后，才能把项目状态从“核心软件有条件通过”改为“V1.0 完全通过”：

1. [ ] DeepSeek、OpenAI 和 Ollama 分别完成真实 `Test Connection + Chat + Failure Handling`；
2. [ ] MySQL 专用数据库升级至 `20260826_0006 (head)`，并重新完成空白迁移、17 张必需表、核心读写和重连验证；
3. [ ] QQ Official Bot 完成 200 条唯一真实入站、急停演练、收件箱清空，并保持系统重复发送、错误联系人发送和急停违规均为 0。

Provider 外部验收进度：DeepSeek `[x]`；OpenAI `[ ]`；Ollama `[x]`。
