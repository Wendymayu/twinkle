# Twinkle

一个学习型重写 [jiuwenclaw](../jiuwenclaw)（参考实现位于 `D:\opensource\gitcode\jiuwenclaw`）核心 agent 链路的个人助手。当前进度：**Phase 0 — 两进程骨架打通**。

- 路线与阶段：[roadmap.md](roadmap.md)
- Phase 0 设计文档：[docs/phase-0-design.md](docs/phase-0-design.md)

## 架构（Phase 0）

```
浏览器 ──ws── Gateway(:19000) ──ws── AgentServer(:18000)
                web_channel          agent_client     echo handler
            message_handler / channel_manager
```

- **AgentServer**（`twinkle/agentserver`）：ws server，Phase 0 内联 echo（流式 chunk + final）。
- **Gateway**（`twinkle/gateway`）：ws client 连 AgentServer + 浏览器 ws server；做 E2A 信封转换与 chat.delta/final 扇出。
- 信封：gateway↔agentserver 用 E2A 子集（`twinkle/e2a/models.py`）；browser↔gateway 用 `{type:req/res/event}`（`twinkle/schema/message.py`）。

## 依赖

用项目本地 venv（不污染系统 python）：

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[dev]"   # Windows
# 或 Unix: . .venv/bin/activate && pip install -e ".[dev]"
```

仅 `websockets>=14`、`pydantic>=2.11`（+ dev 的 `pytest>=8`）。后续命令里 `python` 均指 venv 里的解释器（`.venv/Scripts/python.exe` 或激活后直接 `python`）。

## 验收（自动）

```bash
python -m pytest tests/ -v
```

含三个测试：agent_client↔agentserver echo（流式+unary）、坏包错误帧、**端到端** 浏览器→gateway→agentserver→回流 echo。

## 手动验收

```bash
# 1. 起两进程（任选其一）
python scripts/start_services.py
#   或分两个终端：
python -m twinkle.agentserver
python -m twinkle.gateway

# 2. 起前端
cd web && npm install && npm run dev

# 3. 开 http://localhost:5173 ，输入消息，看到 "Echo: ..." 逐字流式返回
```

dev 模式下 Vite(:5173) 把 `/ws` 代理到 gateway(:19000)，浏览器保持同源。

## 配置（环境变量，均可选）

| 变量 | 默认 | 说明 |
|---|---|---|
| `TWINKLE_AGENTSERVER_HOST/PORT` | 127.0.0.1 / 18000 | AgentServer 监听 |
| `TWINKLE_GATEWAY_HOST/PORT` | 127.0.0.1 / 19000 | Gateway 浏览器 ws 监听 |

## 目录

```
twinkle/
  e2a/models.py            # E2A 信封子集
  schema/message.py        # Message + EventType
  agentserver/server.py     # echo ws server
  gateway/
    agent_client.py         # ws client（按 request_id demux）
    web_channel.py          # 浏览器 ws server
    channel_manager.py      # 出站 dispatch
    message_handler.py      # 入站转换 + 流式扇出
  web/                      # Vite + Vue 3 + TS 前端
tests/                      # echo + 端到端
scripts/start_services.py
```

## 下一阶段

Phase 1：接真模型 + agent loop 最小闭环 + 短期对话记忆（长期 memory 用 stub 打桩）。
