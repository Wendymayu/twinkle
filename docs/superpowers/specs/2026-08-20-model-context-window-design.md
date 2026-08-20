# 模型上下文窗口感知（A+B）设计

- 日期：2026-08-20
- 状态：已落地（2026-08-20）
- 关联：对齐 `jiuwenswarm`（全局单值 128000 + 固定 trigger_total_tokens 预防 + 413 rail 兜底）与 `openclaw`（分层 catalog + `contextWindow − reserve` 预防触发 + 固定 keepRecentTokens 压缩目标）

## 背景

Twinkle 当前不知道所接模型的上下文窗口大小：

- `overflow_recovery.context_window_limit_tokens: 0`（`twinkle/resources/config.yaml:111`）= 不预设，撞 413 错误时才从错误信息被动解析（`twinkle/agentserver/hooks/builtin/context_overflow_recovery_hook.py:22-39` 的 `_parse_token_limits`）。
- 预防压缩用固定 `context_compression.token_threshold: 60000`（`config.yaml:26`，`estimate_tokens` 是 char//3 估算），与真实窗口解耦。

三家参考实现对比：

| 实现 | 窗口值来源 | 预防触发 | 413 恢复目标 |
|---|---|---|---|
| jiuwenswarm | 全局单值 128000（不按模型查表） | 固定 `trigger_total_tokens`（按模型手调：128k→100000≈78%） | ×0.85 是框架预留但全仓零调用的死机制，实际回退到 `trigger_total_tokens` |
| openclaw | 分层（per-provider catalog + live API + config per-model + 兜底 200000） | `contextWindow − reserve`（reserve≥20000，用到窗口，≈84% 起） | 固定 `keepRecentTokens=20000`（与窗口无关） |
| Twinkle 现状 | `0`=被动从 413 解析 | 固定 60000≈47%（对 128k 过早压，浪费窗口） | 解析到→`limit×0.85`（比 jiuwenswarm 更自适应）；解析不到→`0` 盲压（激进缺陷） |

## 目标

1. 让系统按当前模型「知道」上下文窗口：`resolve_context_window_limit()`，优先级 `config 手动覆盖 > 模型字典 > 128000 兜底`。
2. **A**：消除 413 恢复时解析不到 limit 的盲压 0 缺陷，改用 `resolved × 0.8` 兜底。
3. **B**：预防压缩触发从固定 60000 改为 `resolved × 0.8`（80%），按模型窗口自适应（128k→102400、1M→800000），替代过早的 47%。
4. 保留 413 被动解析（真实 limit 优先于字典，比字典更准）。

## 非目标

- 不改 `estimate_tokens`（char//3 保留）。B 的 80% 建立其上，中文低估风险靠 A 的 413 兜底吸收（双层防御，对齐 jiuwenswarm：预防估算 + 413 rail 兜底）。
- 不做前端用量统计（`web/src` 无任何 context/token usage 基础，从 0 搭成本高）。
- 不做 per-model config 覆盖（YAGNI；对齐 openclaw 的「config 可覆盖」精神但不抄其 per-model 表）。
- 不做 live API 发现（YAGNI）。
- 不改 `compress_messages` 的 `head+summary+tail` 压缩机制。
- 不改 `memory_flush_hook`（默认 `enabled: false`）。它仍读启动时常量 `CONTEXT_TOKEN_THRESHOLD = settings.context_compression.token_threshold`；若用户将 `token_threshold` 配为 0 又开启 memory_flush，其 `should_compress(token_threshold=0)` 行为会变，属边界、本期不处理。

## 设计

### 1. 新增 `twinkle/config/model_catalog.py`

`MODEL_CONTEXT_WINDOWS: dict[str, int]` 初版（值以官方文档为准，查不到→128000 兜底）：

```python
MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    "gpt-4o-mini": 128_000,
    "gpt-4o": 128_000,
    "gpt-4-turbo": 128_000,
    "claude-3-5-sonnet": 200_000,
    "claude-3-5-haiku": 200_000,
    "claude-3-opus": 200_000,
}
DEFAULT_CONTEXT_WINDOW_TOKENS = 128_000
```

`normalize_model(name: str) -> str`：`lowercase` + 去掉首个 `:` 及之后（如 `gpt-4o-mini:latest`→`gpt-4o-mini`；兼容 OpenAI/Ollama 带标签写法）。

`resolve_context_window_limit() -> int`：

1. `settings.overflow_recovery.context_window_limit_tokens > 0` → 返回它（手动覆盖，最高优先；复用现有字段，语义不变）
2. 否则前缀匹配：`m = normalize_model(settings.llm.model)`，在字典中找所有满足 `m.startswith(key)` 的 key，取**最长 key**对应的值（使 `gpt-4o-mini-2024-07-18`→匹配 `gpt-4o-mini` 而非 `gpt-4o`，且 `claude-3-5-sonnet-20240129`→`claude-3-5-sonnet`）
3. 无匹配 → `DEFAULT_CONTEXT_WINDOW_TOKENS`

### 2. A：overflow hook（`twinkle/agentserver/hooks/builtin/context_overflow_recovery_hook.py`）

- 删除 `_get_config_context_limit()`（原 line 195）：`resolve_context_window_limit()` 内部已含 config 手动覆盖层，wrapper 纯冗余（dead code）；`on_model_exception` 直接调 `resolve_context_window_limit()`。
- `on_model_exception` 中 threshold 分支（line 123-136）：
  - 413 解析到 `limit_tokens` → `threshold_override = int(limit_tokens × ratio)`（保留，真实值最准）
  - 解析不到 → `threshold_override = int(resolve_context_window_limit() × ratio)`
  - 消除 `config=0` 时 `threshold_override=0` 盲压到最小。
- `ratio` 改由 `_get_trigger_ratio()` 读 `context_compression.trigger_ratio`（见 §4）；删除 `_get_threshold_ratio()` 与对 `overflow_recovery.threshold_ratio` 的引用。
- 413 被动解析保留（`_parse_token_limits` 不动，真实 limit 优先于字典）。

### 3. B：预防压缩（`twinkle/agentserver/hooks/builtin/context_compression_hook.py` + config）

- config 新增 `context_compression.trigger_ratio: float = 0.8`（schema 加字段、config.yaml 加值）。
- `context_compression.token_threshold: int = 0`（默认翻 0，废固定 60000——60000≈47% 对 128k 过早压且与新窗口自适应机制冲突；用户决策「不要被老设计影响」）。`> 0` 仍为「手动绝对覆盖」（向后兼容老配置与显式绝对值场景），否则用动态值。
- `_get_token_threshold()`（line 40）改为：
  - `tt = settings.context_compression.token_threshold`
  - `if tt > 0: return tt`
  - `else: return int(resolve_context_window_limit() × settings.context_compression.trigger_ratio)`
- 效果：默认 128k → `128000×0.8=102400` 触发；1M → 800000；`token_threshold>0` 时仍可用绝对值覆盖。

### 4. 配置合并（一个比例，A+B 共用）

- 一个 `context_compression.trigger_ratio: 0.8`，A（overflow hook）与 B（compression hook）共用。
- 删除 `overflow_recovery.threshold_ratio`（`config.yaml:109`、`schema.py` 对应字段、`context_overflow_recovery_hook.py` 的 `_get_threshold_ratio()`）——A 改读 `context_compression.trigger_ratio`。删除前已确认该字段只 overflow hook 一处引用。
- `threshold` 只影响 `should_compress` 的触发判定；`compress_messages` 实际压成 `head+summary+tail`（tail 由 `keep_recent_pairs` 决定），不精确压到该比例 → 0.8 与原 0.85 合并对压缩结果无副作用；0.8 当恢复目标比 0.85 留更多余量（20%>15%），小改进。
- 跨域读取：A（overflow_recovery 域）读 `context_compression.trigger_ratio`。hook 跨域读 config 是 Twinkle 常态（overflow hook 现已跨读 `CONTEXT_SUMMARY_PROMPT` 等），可接受。

## 改动文件清单

| 文件 | 改动 |
|---|---|
| `twinkle/config/model_catalog.py` | 新建：`MODEL_CONTEXT_WINDOWS` + `normalize_model` + `resolve_context_window_limit` |
| `twinkle/config/__init__.py` | 导出 `resolve_context_window_limit`、`DEFAULT_CONTEXT_WINDOW_TOKENS` |
| `twinkle/config/schema.py` | 加 `context_compression.trigger_ratio: float = 0.8`；删 `overflow_recovery.threshold_ratio` 字段 |
| `twinkle/resources/config.yaml` | 加 `context_compression.trigger_ratio: 0.8`；删 `overflow_recovery.threshold_ratio: 0.85` |
| `twinkle/agentserver/hooks/builtin/context_overflow_recovery_hook.py` | `_get_config_context_limit`→`resolve`；ratio 改读 `trigger_ratio`；删 `_get_threshold_ratio` |
| `twinkle/agentserver/hooks/builtin/context_compression_hook.py` | `_get_token_threshold` 加 `token_threshold>0` 绝对覆盖 / 否则 `resolved×ratio` 动态分支 |
| `tests/` | 新增 resolve 优先级链 + hook 阈值测试 |

## 测试（TDD，先测后实现）

- `test_resolve_context_window_limit`：config 手动覆盖（>0）优先；字典前缀匹配取最长 key（`gpt-4o-mini-2024-07-18`→128000）；无匹配→128000；normalize `:latest`/`:区域` 后缀。
- `test_overflow_recovery_threshold`：413 解析到 limit→`limit×0.8`；解析不到→`resolved×0.8`；不再返回 0。
- `test_compression_threshold_dynamic`：`token_threshold>0` 用绝对值；`=0` 用 `resolved×0.8`；不同模型窗口触发值不同（128k→102400、200k→160000）。

## 验收标准

- `context_window_limit_tokens=0`（默认）时，overflow hook 撞 413 且解析不到 limit，压缩目标 = `resolved×0.8`（>0），不再盲压 0。
- 预防压缩在 128k 模型下于 ~102400 估算 token 触发（替代固定 60000）。
- 删除 `overflow_recovery.threshold_ratio` 后，现有 overflow recovery 测试仍绿（改读 trigger_ratio）；现有测试中硬编码 `0.85` 的断言同步改 `0.8`。
- 新增 resolve/threshold 测试全绿。
