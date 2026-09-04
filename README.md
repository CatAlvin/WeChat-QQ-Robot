# Neko AI — 微信 / QQ AI 托管中枢

面向个人 Windows 电脑的本地消息托管系统，包含 Web 控制后台、消息管线、多模型网关、联系人记忆和可控发送策略。

*A local Windows message-assistant hub for WeChat and QQ, with a web console, multi-model routing, contact memory, and deterministic delivery controls.*

## 版本与进度

- 当前版本：**1.0.0**
- 数据库迁移：**20260831_0016**
- 状态：本地核心、SIMULATION 与 SHADOW 已实现；LIVE 门禁已完成，QQ 官方 200 条真实验收仍待执行
- 通道：QQ Official、实验性 NapCat、AutoWx 入站草稿

## 核心功能

- FastAPI 后端与 React/Next.js 管理后台。
- DeepSeek、Kimi、Qwen、OpenAI 与本机 Ollama 的可排序模型队列。
- 联系人白名单、独立记忆、人工接管、回复时段、频率限制与全局急停。
- QQ 回调、持久化收件箱、消息去重和发送前最终复检。
- 图片、语音和文档的本机保存与可选理解。
- 会话、联系人、模型、用量、日志、任务和运行状态管理。

个人微信官方自动化当前未开放；AutoWx 只接收入站并生成草稿，不执行自动发送。

## 使用方式

需要 Windows、Python 3.12+、Node.js 22+ 和 pnpm。默认使用 SQLite，无需额外配置数据库。

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\setup.ps1
.\scripts\start.ps1
```

打开 <http://127.0.0.1:3000>，首次进入时创建管理员账号，然后在后台添加模型与消息通道。

停止服务：

```powershell
.\scripts\stop.ps1
```

## AI 辅助

运行时 AI 负责生成回复、媒体理解和可选的任务处理；联系人权限、发送门禁、去重与最终发送判断由确定性代码执行。开发过程使用 AI 辅助需求分析、实现和测试，最终策略由作者确认。

## 验证

```powershell
.\scripts\test.ps1
```

早期 V1 验收记录见 [V1 验收报告](docs/ACCEPTANCE_V1.md)，未完成的外部验证见 [验收矩阵](docs/ACCEPTANCE_MATRIX_V1.md)。
