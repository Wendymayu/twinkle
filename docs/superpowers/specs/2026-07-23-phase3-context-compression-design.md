# Phase 3 — 长会话上下文压缩 Design

- date: 2026-07-23
- status: approved (autonomous — user asleep, explicit opt-in to unattended loop)
- branch: `nightly/phase-3-6-4` (worktree `d:/code/opensource/github/twinkle-nightly`)
- roadmap: Phase 3 / milestone M4

## 1. 目标与验收

长会话不爆 token、不丢关键上下文。

- 构造 100 轮对话历史 → 压缩触发后喂 LLM 的 messages token 数下降
- 注入的关键事实字符串在压缩后上下文（summary 或 tail）仍可找到
- 不破坏 `tool_calls` / `tool_call_id` 配对完整性

## 2. 现状

- `SessionStore.get_messages(sid)` 返回完整 OpenAI-native messages（缓存命中或冷启动 hydrate 自 `history.json`）
- `AgentLoop.run_stream` 每步 `msgs = self._store.get_messages(sid)` 后**全量**喂 `self._llm.stream(messages=msgs, ...)`，零压缩 → 长会话线性增长，必爆 token
- worktree 的 `session_store.py` 是 main 的完整磁盘落盘版（`create_session`/`get_history`/`append` JSONL/`_record_to_openai` 全在）

## 3. 方案选择

| 方案 | 做法 | 优点 | 缺点 |
|---|---|---|---|
| A 纯滑窗 | 超阈值丢弃中间，保留首 system + 尾 K 轮 | 简单，不费额外 token | 丢中间事实 → 验收"关键事实不丢"在事实位于中间时失败 |
| **B 滑窗 + LLM 摘要中间** | 超阈值时把被丢弃的中间部分调 LLM 生成一条摘要，插 head/tail 之间 | 保事实，只费一次额外 LLM 调用 | 中等复杂；摘要质量依赖 LLM |
| C 全量 LLM 重写 | 每次把全部历史喂 LLM 压成摘要 | 压缩率最高 | 费 token 最多，每次都调，不推荐 |

**选 B**：平衡事实保留与 token 成本，满足 roadmap "关键事实不丢" 验收。

## 4. 设计

### 4.1 新模块 `twinkle/agentserver/context_compression.py`

```python
def estimate_tokens(msgs: list[dict]) -> int:
    """字符数估算 token（中文按字、英文 //4 折中；不引 tiktoken）。"""

async def compress_messages(
    msgs: list[dict],
    llm: LLMClient,
    *,
    token_threshold: int,
    keep_recent_pairs: int,
    summary_system_prompt: str,
) -> list[dict]:
    """超阈值则 head + [summary] + tail，否则原样返回（copy）。"""
```

逻辑：
1. `estimate_tokens(msgs) <= token_threshold` → 返回 `list(msgs)`（copy，不改原）
2. 超 → 分 `head` / `middle` / `tail`
   - `head` = 首条 system 消息（必保留，让模型知道 todo 工具用法/人设）+ 最早若干条
   - `tail` = 最近 `keep_recent_pairs` 对**完整轮**（含 `tool_calls` → 对应 `tool_call_id` result 配对，不切断）
   - `middle` = head 与 tail 之间，待摘要
3. 调 `llm` 把 `middle` 压成一段摘要文本，包成一条 `{"role":"system","content":"[prior context summary] " + summary}`
4. 返回 `head + [summary_msg] + tail`

工具函数 `_split_keep_tool_pairs(msgs, tail_msg_count) -> (head, middle, tail)`：
- 从尾部向前数 `tail_msg_count` 条，但若停在某个 `tool` result 消息上、其配对的 `tool_calls` assistant 消息落在 head/middle 侧，则把该 assistant 消息也并入 tail（向前回溯到配对 assistant），保证 tail 内 `tool_calls`/`tool_call_id` 闭合
- 同理 head/middle 边界不切断配对

### 4.2 `AgentLoop.run_stream` 集成

每步在 `get_messages` 之后、`llm.stream` 之前插压缩：

```python
msgs = self._store.get_messages(session_id)
msgs = await compress_messages(
    msgs, self._llm,
    token_threshold=CONTEXT_TOKEN_THRESHOLD,
    keep_recent_pairs=CONTEXT_KEEP_RECENT_PAIRS,
    summary_system_prompt=CONTEXT_SUMMARY_PROMPT,
)
async for ev in self._llm.stream(messages=msgs, tools=self._tools.schemas()):
    ...
```

压缩结果**不写回** `SessionStore`（`history.json` 保留原始无损；压缩只用于喂 LLM）。初版每步重算压缩（简单；缓存优化后置）。

### 4.3 `SessionStore`

不改核心。`compress` 逻辑独立放在 `context_compression.py`（因压缩需 LLM，而 `SessionStore` 不持有 `llm`；把压缩塞进 store 会破坏其单一职责 + 引入 llm 依赖）。

> **与 roadmap 字面的偏离**：roadmap 说"在 `session_store.py` 加 truncate/compress"。本设计把核心放独立模块 + `agent_loop` 集成，理由如上。`SessionStore` 的 `get_messages` 接口不变，压缩对 store 透明。此偏离已显式标注，醒来可 review。

### 4.4 配置（`config.py`）

- `TWINKLE_CONTEXT_TOKEN_THRESHOLD` 默认 `60000`（保守，留余量给回复 + 工具结果）
- `TWINKLE_CONTEXT_KEEP_RECENT_PAIRS` 默认 `6`（保留最近 6 轮原始上下文）
- `TWINKLE_CONTEXT_SUMMARY_PROMPT`（摘要用的 system 指令，默认中文模板）

## 5. 测试计划

### 单测（`tests/test_context_compression.py`，fake llm）

- `estimate_tokens`：空列表 0；单调随内容增长
- `compress_messages` 不超阈值：原样返回（且为 copy，不改原 list）
- `compress_messages` 超阈值：fake llm 被调用一次、返回结构为 `head + [summary] + tail`、`estimate_tokens(结果) < estimate_tokens(输入)`
- `tool_calls`/`tool_call_id` 配对不被切断：构造 `assistant(tool_calls) → tool(result)` 序列，断言 tail 起点不落在配对中间
- system 消息始终在 head：断言结果首条 `role == "system"`

**compress 调 llm 的方式**（定死，不留 plan）：`compress_messages` 内部调 `llm.stream(messages=[{role:"system","content":summary_system_prompt}, {role:"user","content": <middle rendered as text>}])`，收集所有 `TextDelta.content` 拼成摘要文本；**不新增 `LLMClient` 方法**（最小侵入，复用现有 `stream` 协议）。fake llm 是一个 stub，`stream()` yield 单条 `TextDelta("summary text")` + `Finish(finish_reason="stop")`。

### 端到端

- 构造 100 轮对话历史，每轮注入递增关键事实 `"FACTKEY_<i>_<desc>"`
- 跑 `compress_messages`（fake llm 返回含若干 FACTKEY 的摘要）
- 断言 `estimate_tokens(结果) < estimate_tokens(输入)` + 指定 FACTKEY 仍在结果（summary 或 tail）

### 真模型（glm-5.1，可选）

- 跑一次真摘要验证摘要质量（非阻塞，质量观察用）

## 6. 偏离与风险

- worktree 基于 main 的 `session_store`；主工作区 staged 的 `session_store` 小增量改动醒来 merge 时可能小冲突（预期可控，基础一致）
- compress 独立模块而非 `session_store` 方法（因需 llm）——与 roadmap 字面略偏离，理由见 §4.3
- token 估算用字符数（不引 tiktoken），对 glm 不精确，学习项目可接受
- 摘要调 LLM 增加每步成本与延迟；初版每步重算压缩，缓存优化后置
- **用户睡了，approaches/设计自治决定（选 B），醒来可调整**

## 7. 不做（YAGNI）

- tiktoken 精确计数
- 压缩结果持久化写回 history
- 压缩缓存（每步重算，后置）
- 跨会话压缩状态
- 摘要的摘要（多层压缩）
