"""Dreaming 测试（B 方案：claimHash 晋升 + LLM 整合）。

组A 触发门卫（disabled / no-llm / busy / no-daily-noop）；组B start_dreaming task；
组C dream 空跑；组D 晋升门槛（单文件不晋升）；组E consolidate LLM fail-soft（dream 路径）；
组F config 字段；组G _scan_claims；组H sidecar；组I 晋升门；组J _consolidate；
组K _compact；组L dream 端到端。
"""
import asyncio

from twinkle.agentserver.llm_client import Finish, TextDelta
from twinkle.agentserver.memory.dreaming import DreamingOrchestrator
from twinkle.agentserver.memory.store import MemoryManager


def _mgr(tmp_path):
    return MemoryManager(str(tmp_path), embed_provider=None)


def _with_mgr(mgr):
    from twinkle.agentserver.memory import _set_memory_manager
    _set_memory_manager(mgr)
    return _set_memory_manager


class _FakeLLM:
    """记录调用次数；按顺序返回预设响应文本。"""
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    async def stream(self, messages, tools):
        idx = self.calls
        self.calls += 1
        text = self._responses[idx] if idx < len(self._responses) else "[]"
        yield TextDelta(text)
        yield Finish(finish_reason="stop",
                      assistant_message={"role": "assistant", "content": text})


class _FailingLLM:
    """stream 总抛异常，验 fail-soft。"""
    async def stream(self, messages, tools):
        raise RuntimeError("LLM down")
        yield  # unreachable


class _WriteSpy:
    """包真实 MemoryManager，计数 write 调用次数。

    验 _append_promotions 批量 append 只写一次盘（非逐条 N 次），减 _index_file
    全量重索引。其余方法透传给被包 mgr。"""
    def __init__(self, real):
        self._real = real
        self.write_calls = 0

    def write(self, path, content, append=False):
        self.write_calls += 1
        return self._real.write(path, content, append=append)

    def __getattr__(self, name):
        return getattr(self._real, name)


# --- 组A：骨架 + 触发（不调 LLM）---


def test_dreaming_disabled_noop(tmp_path, monkeypatch):
    """config 关 → 门卫挡在最前,daily 即便够格也不跑（不调 LLM、不落 sidecar）。

    预置 2 个同文 daily（够晋升门）→ 若 config 门失守,consolidate 会调 LLM;
    门在 → llm.calls==0 + 无 sidecar,证门卫先于一切。"""
    import twinkle.config
    monkeypatch.setattr(twinkle.config, "MEMORY_DREAMING_ENABLED", False)
    reset = _with_mgr(_mgr(tmp_path))
    try:
        from twinkle.agentserver.memory import get_memory_manager
        mgr = get_memory_manager()
        mgr.write("daily_memory/2026-08-14.md", "- 喜欢爬山运动\n", append=False)
        mgr.write("daily_memory/2026-08-15.md", "- 喜欢爬山运动\n", append=False)
        orch = DreamingOrchestrator(llm=_FakeLLM([]), get_inflight=lambda: 0)
        asyncio.run(orch.dream())
        assert orch.llm.calls == 0  # 门卫挡住 → consolidate 没跑
        assert not (mgr.memory_dir / "dreaming_state.json").exists()  # 也没落 sidecar
    finally:
        reset(None)


def test_dreaming_no_llm_noop(tmp_path, monkeypatch):
    """无 LLM（None）→ dream 不跑（返回 None）。"""
    import twinkle.config
    monkeypatch.setattr(twinkle.config, "MEMORY_DREAMING_ENABLED", True)
    reset = _with_mgr(_mgr(tmp_path))
    try:
        orch = DreamingOrchestrator(llm=None, get_inflight=lambda: 0)
        ran = asyncio.run(orch.dream())
        assert ran is None
    finally:
        reset(None)


def test_dreaming_busy_skips(tmp_path, monkeypatch):
    """inflight>0 → busy-backoff 门挡住,daily 即便够格也不跑（不调 LLM、不落 sidecar）。

    预置 2 个同文 daily（够晋升门）→ 若 busy 门失守,consolidate 会调 LLM;
    门在 → llm.calls==0 + 无 sidecar,证 busy-backoff 先于整理。"""
    import twinkle.config
    monkeypatch.setattr(twinkle.config, "MEMORY_DREAMING_ENABLED", True)
    reset = _with_mgr(_mgr(tmp_path))
    try:
        from twinkle.agentserver.memory import get_memory_manager
        mgr = get_memory_manager()
        mgr.write("daily_memory/2026-08-14.md", "- 喜欢爬山运动\n", append=False)
        mgr.write("daily_memory/2026-08-15.md", "- 喜欢爬山运动\n", append=False)
        orch = DreamingOrchestrator(llm=_FakeLLM([]), get_inflight=lambda: 5)  # busy
        asyncio.run(orch.dream())
        assert orch.llm.calls == 0  # busy 门挡住 → consolidate 没跑
        assert not (mgr.memory_dir / "dreaming_state.json").exists()
    finally:
        reset(None)


def test_dreaming_no_daily_files_noop(tmp_path, monkeypatch):
    """门过 + 无 daily → 无 claims → 无晋升 → 不跑 consolidate（不调 LLM、不落 sidecar）;
    compact 跑但 MEMORY.md 不存在 → noop（compact 不依赖新晋升,独立兜底）。"""
    import twinkle.config
    monkeypatch.setattr(twinkle.config, "MEMORY_DREAMING_ENABLED", True)
    reset = _with_mgr(_mgr(tmp_path))
    try:
        from twinkle.agentserver.memory import get_memory_manager
        mgr = get_memory_manager()
        orch = DreamingOrchestrator(llm=_FakeLLM([]), get_inflight=lambda: 0)
        asyncio.run(orch.dream())
        assert orch.llm.calls == 0  # 无晋升 → consolidate 没跑
        assert not (mgr.memory_dir / "dreaming_state.json").exists()  # 无晋升 → 不落 sidecar
    finally:
        reset(None)


# --- 组B：run_loop + start_dreaming（task 启动判定）---


def test_dreaming_disabled_no_task(tmp_path, monkeypatch):
    """config 关 → start_dreaming 不起 task（返回 None）。"""
    import twinkle.config
    monkeypatch.setattr(twinkle.config, "MEMORY_DREAMING_ENABLED", False)
    reset = _with_mgr(_mgr(tmp_path))
    try:
        async def _check():
            from twinkle.agentserver.memory.dreaming import start_dreaming
            task = start_dreaming(llm=_FakeLLM([]), get_inflight=lambda: 0)
            assert task is None
        asyncio.run(_check())
    finally:
        reset(None)


def test_dreaming_no_llm_no_task(tmp_path, monkeypatch):
    """无 LLM → start_dreaming 不起 task（返回 None）。"""
    import twinkle.config
    monkeypatch.setattr(twinkle.config, "MEMORY_DREAMING_ENABLED", True)
    reset = _with_mgr(_mgr(tmp_path))
    try:
        async def _check():
            from twinkle.agentserver.memory.dreaming import start_dreaming
            task = start_dreaming(llm=None, get_inflight=lambda: 0)
            assert task is None
        asyncio.run(_check())
    finally:
        reset(None)


def test_dreaming_enabled_starts_task(tmp_path, monkeypatch):
    """enabled + 有 LLM → start_dreaming 起后台 task（非 None，未完成）。"""
    import twinkle.config
    monkeypatch.setattr(twinkle.config, "MEMORY_DREAMING_ENABLED", True)
    monkeypatch.setattr(twinkle.config, "MEMORY_DREAMING_INTERVAL_SECONDS", 3600)
    monkeypatch.setattr(twinkle.config, "MEMORY_DREAMING_START_DELAY_SECONDS", 3600)
    reset = _with_mgr(_mgr(tmp_path))
    try:
        async def _check():
            from twinkle.agentserver.memory.dreaming import start_dreaming
            task = start_dreaming(llm=_FakeLLM([]), get_inflight=lambda: 0)
            assert task is not None
            assert not task.done()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        asyncio.run(_check())
    finally:
        reset(None)


# --- 组C：dream 门卫过 + 空跑（无 daily → 无晋升 → 不 consolidate）---


def test_dreaming_runs_when_idle(tmp_path, monkeypatch):
    """空闲 + 有 MEMORY.md（无 daily）→ 无晋升 → compact 跑（未超 → noop,不调 LLM）。"""
    import twinkle.config
    monkeypatch.setattr(twinkle.config, "MEMORY_DREAMING_ENABLED", True)
    reset = _with_mgr(_mgr(tmp_path))
    try:
        from twinkle.agentserver.memory import get_memory_manager
        mgr = get_memory_manager()
        mgr.write("MEMORY.md", "- 测试条目\n", append=False)
        orch = DreamingOrchestrator(llm=_FakeLLM([]), get_inflight=lambda: 0)
        asyncio.run(orch.dream())  # 无 daily → 无晋升 → 不抛
        result = mgr.read("MEMORY.md")
        assert "测试条目" in result  # 1 条还在
    finally:
        reset(None)


# --- 组D：晋升门槛（单 daily 文件 < min_distinct_files=2 → 不晋升）---


def test_dreaming_single_daily_file_not_promoted(tmp_path, monkeypatch):
    """claim 只在 1 个 daily 文件(< min_distinct_files=2)→ 不晋升：MEMORY.md 不变、
    不调 LLM、不落 sidecar。证门槛是"跨≥2 文件复现",非"出现 1 次"。"""
    import twinkle.config
    monkeypatch.setattr(twinkle.config, "MEMORY_DREAMING_ENABLED", True)
    reset = _with_mgr(_mgr(tmp_path))
    try:
        from twinkle.agentserver.memory import get_memory_manager
        mgr = get_memory_manager()
        mgr.write("MEMORY.md", "- 用 Windows 系统\n", append=False)
        mgr.write("daily_memory/2026-08-14.md", "- 喜欢爬山运动\n", append=False)  # 仅 1 文件
        orch = DreamingOrchestrator(llm=_FakeLLM([]), get_inflight=lambda: 0)
        asyncio.run(orch.dream())
        result = mgr.read("MEMORY.md")
        assert "爬山" not in result               # 不够门槛 → 没 append
        assert result.count("用 Windows 系统") == 1  # 原有逐字保留
        assert orch.llm.calls == 0                # 无晋升 → consolidate 没跑
        assert not (mgr.memory_dir / "dreaming_state.json").exists()
    finally:
        reset(None)


# --- 组E：consolidate LLM 失败 fail-soft（dream 路径：promote 留 append-only 版）---


def test_dreaming_llm_failure_fails_soft(tmp_path, monkeypatch):
    """dream 跑到 consolidate 时 LLM 抛异常 → _ask_llm fail-soft 返回空 →
    consolidate 早返不动 MEMORY.md → promote 的 append-only 版留下,不崩。

    预置 2 同文 daily（够晋升）+ MEMORY.md 1 行 → promote append 爬山(2 行)→
    consolidate 调 LLM(_FailingLLM 抛)→ fail-soft → 爬山+Windows 都在。"""
    import twinkle.config
    monkeypatch.setattr(twinkle.config, "MEMORY_DREAMING_ENABLED", True)
    reset = _with_mgr(_mgr(tmp_path))
    try:
        from twinkle.agentserver.memory import get_memory_manager
        mgr = get_memory_manager()
        mgr.write("MEMORY.md", "- 用 Windows 系统\n", append=False)
        mgr.write("daily_memory/2026-08-14.md", "- 喜欢爬山运动\n", append=False)
        mgr.write("daily_memory/2026-08-15.md", "- 喜欢爬山运动\n", append=False)
        orch = DreamingOrchestrator(llm=_FailingLLM(), get_inflight=lambda: 0)
        asyncio.run(orch.dream())  # promote → consolidate(LLM 抛)→ fail-soft,不崩
        result = mgr.read("MEMORY.md")
        assert "爬山" in result           # promote 的 append-only 版留下(consolidate 没删)
        assert "用 Windows 系统" in result  # 原有逐字保留
    finally:
        reset(None)


# --- 组F：B 方案 config 字段 ---


def test_dreaming_config_defaults():
    """B 方案新增 3 个 config 字段默认值。

    min_distinct_files=2（晋升门：同一事实须出现在 ≥N 个不同 daily 文件才搬进 MEMORY.md），
    max_memory_chars=10000（MEMORY.md 容量预算，超限 compact 丢最老提升行），
    max_delete_fraction=0.25（整合步单次删除行数上限比例，安全阀防 LLM 误删）。
    """
    from twinkle.config.schema import MemoryDreamingConfig
    assert MemoryDreamingConfig().enabled is True  # 默认开：盘上 MEMORY.md 周期 compact 兜底容量
    import twinkle.config
    assert twinkle.config.MEMORY_DREAMING_MIN_DISTINCT_FILES == 2
    assert twinkle.config.MEMORY_DREAMING_MAX_MEMORY_CHARS == 10000
    assert twinkle.config.MEMORY_DREAMING_MAX_DELETE_FRACTION == 0.25


# --- 组G：_scan_claims 跨文件去重 ---


def test_scan_claims_dedup_same_line(tmp_path):
    """同一非空行出现在 2 个 daily 文件 → 聚合成 1 个 claim,source_files 含两文件,
    first_path = 排序后首个含此 claim 的文件(供 sidecar source_path 记录)。

    claimHash=md5(line.strip()):跨文件同文(去空白后同)→ 同 hash → 同 claim,
    source_files 是 set 去重,跨日复现靠它计数(晋升门判据)。
    """
    mgr = _mgr(tmp_path)
    mgr.write("daily_memory/2026-08-14.md", "- 喜欢爬山运动\n", append=False)
    mgr.write("daily_memory/2026-08-15.md", "- 喜欢爬山运动\n", append=False)
    claims = DreamingOrchestrator._scan_claims(mgr)
    assert len(claims) == 1  # 同行跨文件 → 1 claim
    claim = next(iter(claims.values()))
    assert claim["text"] == "- 喜欢爬山运动"
    assert claim["source_files"] == {"daily_memory/2026-08-14.md",
                                     "daily_memory/2026-08-15.md"}
    assert claim["first_path"] == "daily_memory/2026-08-14.md"


def test_scan_claims_single_file(tmp_path):
    """行只出现在 1 个 daily 文件 → claim 在但 source_files 只有 1 个(不达晋升门)。"""
    mgr = _mgr(tmp_path)
    mgr.write("daily_memory/2026-08-14.md", "- 今天吃了火锅\n", append=False)
    claims = DreamingOrchestrator._scan_claims(mgr)
    assert len(claims) == 1
    claim = next(iter(claims.values()))
    assert claim["source_files"] == {"daily_memory/2026-08-14.md"}


# --- 组H：sidecar _load_state / _save_state ---


def test_load_state_missing_returns_empty(tmp_path):
    """sidecar 不存在 → 返回空状态 {"version":1,"promoted":{}}(冷启动)。"""
    sidecar = tmp_path / "dreaming_state.json"
    state = DreamingOrchestrator._load_state(sidecar)
    assert state == {"version": 1, "promoted": {}}


def test_load_state_bad_json_returns_empty(tmp_path):
    """sidecar 内容坏 JSON → 返回空状态(不崩)。"""
    sidecar = tmp_path / "dreaming_state.json"
    sidecar.write_text("{not valid json", encoding="utf-8")
    state = DreamingOrchestrator._load_state(sidecar)
    assert state == {"version": 1, "promoted": {}}


def test_save_load_roundtrip(tmp_path):
    """save → load 往返:promoted 记录(ts/text/source_path)原样存回。"""
    sidecar = tmp_path / "dreaming_state.json"
    state = {"version": 1, "promoted": {
        "abc123": {"ts": "2026-08-15T03:00:00", "text": "- 喜欢爬山运动",
                   "source_path": "daily_memory/2026-08-14.md"}}}
    DreamingOrchestrator._save_state(state, sidecar)
    loaded = DreamingOrchestrator._load_state(sidecar)
    assert loaded == state


def test_save_state_no_tmp_left(tmp_path):
    """原子写(tempfile+rename)成功后不留 .tmp 残留。"""
    sidecar = tmp_path / "dreaming_state.json"
    DreamingOrchestrator._save_state({"version": 1, "promoted": {}}, sidecar)
    assert sidecar.is_file()
    assert not list(tmp_path.rglob("*.tmp"))


# --- 组I：晋升门 _filter_promotable + _append_promotions ---


def test_filter_blocks_low_frequency(tmp_path):
    """claim 只在 1 个 daily 文件(< min_distinct_files=2)→ 不够格:_filter_promotable 返回空。"""
    mgr = _mgr(tmp_path)
    mgr.write("daily_memory/2026-08-14.md", "- 今天吃了火锅\n", append=False)
    claims = DreamingOrchestrator._scan_claims(mgr)
    promotion_state = {"version": 1, "promoted": {}}
    candidates = DreamingOrchestrator._filter_promotable(claims, promotion_state)
    assert candidates == []


def test_filter_blocks_already_promoted(tmp_path):
    """claim 够格(2 文件)但 hash 已在已晋升集 → _filter_promotable 不返回它(防重晋)。"""
    mgr = _mgr(tmp_path)
    mgr.write("daily_memory/2026-08-14.md", "- 喜欢爬山运动\n", append=False)
    mgr.write("daily_memory/2026-08-15.md", "- 喜欢爬山运动\n", append=False)
    claims = DreamingOrchestrator._scan_claims(mgr)
    claim_hash = next(iter(claims))
    promotion_state = {"version": 1, "promoted": {
        claim_hash: {"ts": "2026-08-15T03:00:00", "text": "- 喜欢爬山运动",
                     "source_path": "daily_memory/2026-08-14.md"}}}
    candidates = DreamingOrchestrator._filter_promotable(claims, promotion_state)
    assert candidates == []


def test_filter_passes_2_files(tmp_path):
    """claim 够格(2 文件)+ 未晋升 → _filter_promotable 返回它,带 hash/text/source_path。"""
    mgr = _mgr(tmp_path)
    mgr.write("daily_memory/2026-08-14.md", "- 喜欢爬山运动\n", append=False)
    mgr.write("daily_memory/2026-08-15.md", "- 喜欢爬山运动\n", append=False)
    claims = DreamingOrchestrator._scan_claims(mgr)
    claim_hash = next(iter(claims))
    promotion_state = {"version": 1, "promoted": {}}
    candidates = DreamingOrchestrator._filter_promotable(claims, promotion_state)
    assert len(candidates) == 1
    cand = candidates[0]
    assert cand["hash"] == claim_hash
    assert cand["text"] == "- 喜欢爬山运动"
    assert cand["source_path"] == "daily_memory/2026-08-14.md"


def test_append_writes_memory_and_records(tmp_path):
    """_append_promotions:候选 append 进 MEMORY.md + 记 ts/text/source_path 进已晋升集。"""
    mgr = _mgr(tmp_path)
    claim_hash = "abc123"
    candidates = [{"hash": claim_hash, "text": "- 喜欢爬山运动",
                  "source_path": "daily_memory/2026-08-14.md"}]
    promotion_state = {"version": 1, "promoted": {}}
    DreamingOrchestrator._append_promotions(mgr, candidates, promotion_state)
    assert "- 喜欢爬山运动" in mgr.read("MEMORY.md")
    rec = promotion_state["promoted"][claim_hash]
    assert rec["text"] == "- 喜欢爬山运动"
    assert rec["source_path"] == "daily_memory/2026-08-14.md"
    assert rec["ts"]  # 有时间戳(不 pin 具体值)


def test_append_empty_candidates_noop(tmp_path):
    """空候选列表 → _append_promotions 不写 MEMORY.md 不改已晋升集(无副作用)。"""
    mgr = _mgr(tmp_path)
    mgr.write("MEMORY.md", "- 已有条目\n", append=False)
    promotion_state = {"version": 1, "promoted": {}}
    DreamingOrchestrator._append_promotions(mgr, [], promotion_state)
    assert mgr.read("MEMORY.md") == "- 已有条目"
    assert promotion_state["promoted"] == {}


def test_append_batch_single_write(tmp_path):
    """多候选 → _append_promotions 批量 append 只写一次盘(非逐条 N 次)。

    逐条 append 会触发 N 次 _index_file(MEMORY.md) 全量重索引(删旧 chunks+
    重分块+重插);批量拼好一次写 → 1 次重索引。内容仍全进 MEMORY.md,已晋升集仍逐条记。"""
    real = _mgr(tmp_path)
    spy = _WriteSpy(real)
    candidates = [
        {"hash": "h1", "text": "- 喜欢爬山运动", "source_path": "daily_memory/2026-08-14.md"},
        {"hash": "h2", "text": "- 偏好中文", "source_path": "daily_memory/2026-08-15.md"},
        {"hash": "h3", "text": "- 用 Windows 系统", "source_path": "daily_memory/2026-08-16.md"},
    ]
    promotion_state = {"version": 1, "promoted": {}}
    DreamingOrchestrator._append_promotions(spy, candidates, promotion_state)
    assert spy.write_calls == 1  # 批量 → 一次写盘(非 3 次)
    memory = real.read("MEMORY.md")
    assert "- 喜欢爬山运动" in memory
    assert "- 偏好中文" in memory
    assert "- 用 Windows 系统" in memory
    assert len(promotion_state["promoted"]) == 3  # 3 条都记了 ts/text/source_path


# --- 组J：_consolidate LLM 删行号 ---


def test_consolidate_deletes_redundant(tmp_path):
    """语义冗余(4 行删 1 = 25%,卡预算边界)→ LLM 出 delete[2] → 留更完整那条,余逐字保留。"""
    import json
    mgr = _mgr(tmp_path)
    mgr.write("MEMORY.md", "- 用 Windows 系统\n- 用 Windows\n- 喜欢爬山运动\n- 偏好中文\n",
              append=False)
    orch = DreamingOrchestrator(llm=_FakeLLM([json.dumps({"delete": [2]})]),
                                get_inflight=lambda: 0)
    asyncio.run(orch._consolidate(mgr))
    assert mgr.read("MEMORY.md") == "- 用 Windows 系统\n- 喜欢爬山运动\n- 偏好中文"


def test_consolidate_resolves_conflict(tmp_path):
    """矛盾行(同属性不同值)→ 删旧值(低编号)留后写更新值(高编号)。"""
    import json
    mgr = _mgr(tmp_path)
    mgr.write("MEMORY.md",
              "- 操作系统是 Windows\n- 操作系统是 macOS\n- 喜欢爬山运动\n- 偏好中文\n",
              append=False)
    orch = DreamingOrchestrator(llm=_FakeLLM([json.dumps({"delete": [1]})]),
                                get_inflight=lambda: 0)
    asyncio.run(orch._consolidate(mgr))
    result = mgr.read("MEMORY.md")
    assert "macOS" in result        # 后写更新值留下
    assert "Windows" not in result  # 旧值删了(仅此行含 Windows)
    assert result.count("- ") == 3


def test_consolidate_loss_budget_fallback(tmp_path):
    """LLM 出 >25% 删除(2/4=50%)→ 验证拦 → MEMORY.md 不动(append-only 版留着)。"""
    import json
    mgr = _mgr(tmp_path)
    mgr.write("MEMORY.md", "- 用 Windows 系统\n- 偏好中文\n- 喜欢爬山运动\n- 用 macOS\n",
              append=False)
    orch = DreamingOrchestrator(llm=_FakeLLM([json.dumps({"delete": [1, 2]})]),
                                get_inflight=lambda: 0)
    asyncio.run(orch._consolidate(mgr))
    assert mgr.read("MEMORY.md").count("- ") == 4  # 没动


def test_consolidate_llm_fail_soft(tmp_path):
    """LLM stream 抛异常 → _ask_llm 返回空 → 跳过整合,MEMORY.md 不动。"""
    mgr = _mgr(tmp_path)
    mgr.write("MEMORY.md", "- 用 Windows 系统\n- 偏好中文\n- 喜欢爬山运动\n- 用 macOS\n",
              append=False)
    orch = DreamingOrchestrator(llm=_FailingLLM(), get_inflight=lambda: 0)
    asyncio.run(orch._consolidate(mgr))  # 不崩
    assert mgr.read("MEMORY.md").count("- ") == 4  # 不动


def test_consolidate_json_parse_fail_soft(tmp_path):
    """LLM 返回非 JSON → 解析失败 → 跳过,MEMORY.md 不动。"""
    mgr = _mgr(tmp_path)
    mgr.write("MEMORY.md", "- 用 Windows 系统\n- 偏好中文\n- 喜欢爬山运动\n- 用 macOS\n",
              append=False)
    orch = DreamingOrchestrator(llm=_FakeLLM(["not json at all"]),
                                get_inflight=lambda: 0)
    asyncio.run(orch._consolidate(mgr))  # 不崩
    assert mgr.read("MEMORY.md").count("- ") == 4  # 不动


# --- 组K：_compact_if_over_budget ---


def test_compact_drops_oldest_promotion(tmp_path, monkeypatch):
    """阶段1:超 max → 按 sidecar ts 升序丢最老的仍存在 promotion 行。本例丢 L2 即 ≤预算,
    不进阶段2(丢光 promotion 仍超才丢非 promotion 头部)。

    MEMORY.md 3 行:非 promo(L1)+ 老 promo(L2,ts 早)+ 新 promo(L3,ts 晚)。
    len 27 > max=24 → 丢 L2 → 18 ≤ 24 停。L2 丢、L1/L3 留。
    """
    import twinkle.config
    monkeypatch.setattr(twinkle.config, "MEMORY_DREAMING_MAX_MEMORY_CHARS", 24)
    mgr = _mgr(tmp_path)
    mgr.write("MEMORY.md",
              "- 用户条目不应删\n- 喜欢爬山运动\n- 喜欢跑步运动\n", append=False)
    state = {"version": 1, "promoted": {
        "h1": {"ts": "2026-08-15T03:00:00", "text": "- 喜欢爬山运动",
               "source_path": "daily_memory/2026-08-14.md"},
        "h2": {"ts": "2026-08-16T03:00:00", "text": "- 喜欢跑步运动",
               "source_path": "daily_memory/2026-08-15.md"}}}
    DreamingOrchestrator._compact_if_over_budget(mgr, state)
    result = mgr.read("MEMORY.md")
    assert "- 喜欢爬山运动" not in result   # 最老 promotion 丢了
    assert "- 喜欢跑步运动" in result       # 较新 promotion 留下
    assert "- 用户条目不应删" in result     # 非 promotion 用户行不动
    assert result.count("- ") == 2


def test_compact_under_budget_noop(tmp_path, monkeypatch):
    """未超 max → 不动 MEMORY.md(不重写、不丢行)。"""
    import twinkle.config
    monkeypatch.setattr(twinkle.config, "MEMORY_DREAMING_MAX_MEMORY_CHARS", 1000)
    mgr = _mgr(tmp_path)
    mgr.write("MEMORY.md", "- 短条目1\n- 短条目2\n", append=False)
    state = {"version": 1, "promoted": {
        "h1": {"ts": "2026-08-15T03:00:00", "text": "- 短条目1",
               "source_path": "daily_memory/2026-08-14.md"}}}
    DreamingOrchestrator._compact_if_over_budget(mgr, state)
    assert mgr.read("MEMORY.md") == "- 短条目1\n- 短条目2"  # 不变


# --- 组K2：compact 分阶段 + reconcile + 无晋升也 compact（手写不记 ts，机械兜底）---


def test_compact_phase2_drops_untracked_by_position(tmp_path, monkeypatch):
    """丢光 promotion 仍超预算 → 阶段2 丢非 promotion 行(手写/未追踪)按文件
    位置头部先(append-only 头部≈最早写,该让位)。手写不记 ts,机械丢头部。

    MEMORY.md: oldA(手写)+oldB(手写)+PROMO(promotion)+newC(手写),max=15。
    阶段1 丢 PROMO(唯一 promotion)→仍超 → 阶段2 丢 oldA(头部最老非 promo)→≤预算停。
    结果:PROMO+oldA 丢,oldB+newC 留。
    """
    import twinkle.config
    monkeypatch.setattr(twinkle.config, "MEMORY_DREAMING_MAX_MEMORY_CHARS", 15)
    mgr = _mgr(tmp_path)
    mgr.write("MEMORY.md", "- oldA\n- oldB\n- PROMO\n- newC\n", append=False)
    state = {"version": 1, "promoted": {
        "h1": {"ts": "2026-08-15T03:00:00", "text": "- PROMO",
               "source_path": "daily_memory/2026-08-14.md"}}}
    DreamingOrchestrator._compact_if_over_budget(mgr, state)
    result = mgr.read("MEMORY.md")
    assert "PROMO" not in result   # promotion 丢(阶段1)
    assert "oldA" not in result    # 手写头部丢(阶段2,append-only 头部=最早)
    assert "oldB" in result        # 较后手写留
    assert "newC" in result        # 最新手写留
    assert result.count("- ") == 2


def test_compact_no_candidates_still_runs(tmp_path, monkeypatch):
    """无晋升(无 daily)但 MEMORY.md 超预算 → dream() 仍跑 compact 丢 promotion
    (compact 不依赖新晋升,独立兜底容量)。"""
    import twinkle.config
    monkeypatch.setattr(twinkle.config, "MEMORY_DREAMING_ENABLED", True)
    monkeypatch.setattr(twinkle.config, "MEMORY_DREAMING_MAX_MEMORY_CHARS", 15)
    reset = _with_mgr(_mgr(tmp_path))
    try:
        from twinkle.agentserver.memory import get_memory_manager
        mgr = get_memory_manager()
        mgr.write("MEMORY.md", "- oldA\n- PROMO\n- newC\n", append=False)
        sidecar = mgr.memory_dir / "dreaming_state.json"
        DreamingOrchestrator._save_state({"version": 1, "promoted": {
            "h1": {"ts": "2026-08-15T03:00:00", "text": "- PROMO",
                   "source_path": "daily_memory/2026-08-14.md"}}}, sidecar)
        orch = DreamingOrchestrator(llm=_FakeLLM([]), get_inflight=lambda: 0)
        asyncio.run(orch.dream())  # 无 daily→无晋升→但 compact 仍跑
        result = mgr.read("MEMORY.md")
        assert "PROMO" not in result  # compact 丢了 promotion(无晋升也 compact)
    finally:
        reset(None)


def test_compact_reconciles_orphans(tmp_path, monkeypatch):
    """consolidate 删行后 sidecar 留孤儿(被删行 text 仍在 promoted)→ compact
    reconcile 清(text 不在文件的 promoted 记录删),防 sidecar 膨胀。未超预算也清。"""
    import twinkle.config
    monkeypatch.setattr(twinkle.config, "MEMORY_DREAMING_MAX_MEMORY_CHARS", 1000)
    mgr = _mgr(tmp_path)
    mgr.write("MEMORY.md", "- 保留行\n", append=False)  # consolidate 删了别的,只留这行
    state = {"version": 1, "promoted": {
        "h1": {"ts": "2026-08-15T03:00:00", "text": "- 保留行",
               "source_path": "daily_memory/2026-08-14.md"},
        "h2": {"ts": "2026-08-16T03:00:00", "text": "- 已删行",  # 孤儿(文件没了)
               "source_path": "daily_memory/2026-08-15.md"}}}
    DreamingOrchestrator._compact_if_over_budget(mgr, state)
    assert "h1" in state["promoted"]      # 文件还在→留
    assert "h2" not in state["promoted"]  # 孤儿→清


# --- 组L：dream() 端到端(新 B 模型) ---


def test_dream_promotes_across_two_daily_then_consolidates(tmp_path, monkeypatch):
    """端到端:2 daily 同行(够格)+ MEMORY.md 已有 1 行 → promote append + consolidate 跑一次 LLM。"""
    import json
    import twinkle.config
    monkeypatch.setattr(twinkle.config, "MEMORY_DREAMING_ENABLED", True)
    reset = _with_mgr(_mgr(tmp_path))
    try:
        from twinkle.agentserver.memory import get_memory_manager
        mgr = get_memory_manager()
        mgr.write("MEMORY.md", "- 用 Windows 系统\n", append=False)
        mgr.write("daily_memory/2026-08-14.md", "- 喜欢爬山运动\n", append=False)
        mgr.write("daily_memory/2026-08-15.md", "- 喜欢爬山运动\n", append=False)
        fake = _FakeLLM([json.dumps({"delete": []})])  # consolidate 无可删
        orch = DreamingOrchestrator(llm=fake, get_inflight=lambda: 0)
        asyncio.run(orch.dream())
        result = mgr.read("MEMORY.md")
        assert "- 喜欢爬山运动" in result    # claimHash 跨 2 文件 → 晋升
        assert "- 用 Windows 系统" in result  # 原有逐字保留
        assert fake.calls == 1                # consolidate 跑了一次 LLM
    finally:
        reset(None)


def test_dream_sidecar_idempotent_across_ticks(tmp_path, monkeypatch):
    """连跑两次 dream:tick1 晋升+consolidate+存 sidecar;tick2 sidecar 已记 hash →
    不重晋、不重跑 consolidate(fake.calls 仍 1);tick2 compact 跑(未超→noop,MEMORY.md 不变)。"""
    import json
    import twinkle.config
    monkeypatch.setattr(twinkle.config, "MEMORY_DREAMING_ENABLED", True)
    reset = _with_mgr(_mgr(tmp_path))
    try:
        from twinkle.agentserver.memory import get_memory_manager
        mgr = get_memory_manager()
        mgr.write("MEMORY.md", "- 用 Windows 系统\n", append=False)
        mgr.write("daily_memory/2026-08-14.md", "- 喜欢爬山运动\n", append=False)
        mgr.write("daily_memory/2026-08-15.md", "- 喜欢爬山运动\n", append=False)
        fake = _FakeLLM([json.dumps({"delete": []}), json.dumps({"delete": []})])
        orch = DreamingOrchestrator(llm=fake, get_inflight=lambda: 0)
        asyncio.run(orch.dream())  # tick1
        after_first = mgr.read("MEMORY.md")
        assert after_first.count("- 喜欢爬山运动") == 1  # 晋升一次
        asyncio.run(orch.dream())  # tick2
        assert mgr.read("MEMORY.md") == after_first       # 不重晋(幂等)
        assert fake.calls == 1  # 只 tick1 consolidate;tick2 无晋升→return→不 consolidate
    finally:
        reset(None)
