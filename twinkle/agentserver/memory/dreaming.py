"""Dreaming — 后台记忆整理（B 方案 spec §4）。

后台 asyncio task 周期整理 MEMORY.md：扫 daily_memory 非空行 → claimHash 跨文件
聚合 → 确定性门槛晋升进 MEMORY.md → 单次 LLM 整合删冗余/矛盾行 → 容量预算
compact。daily append-only 只读。晋升门零 LLM（确定性门槛），整合 LLM 在后台不进
写入关键路径（agent 直接写 MEMORY.md 是快通道，dreaming 是慢通道）。
"""
from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
import logging

from twinkle.agentserver.llm_client import TextDelta

log = logging.getLogger("twinkle.memory.dreaming")


_CONSOLIDATE_PROMPT = """你是记忆去重整合器。下面是【MEMORY.md 当前的非空行，已编号】。

找出其中的：
- 语义重复行（同一事实、不同措辞）→ 保留更完整/更明确的那条，删冗余的。
- 矛盾行（同一实体的单一取值属性、不同取值，如"用 Windows" vs "用 Mac"）→ 保留更后写入（编号更大）的那条，删旧值。

硬约束：
1. 只删行，绝不改写任何行的原文（保留的行逐字不动）。
2. 删除行数不得超过总行数的 25%。
3. 不得新增行、不得新增内容。
4. 只输出 JSON，禁止非 JSON 文本（不要代码块、不要解释）：
{{"delete":[行号, 行号, ...]}}

【MEMORY.md 编号行】
{numbered_lines}"""


class DreamingOrchestrator:
    """后台记忆整理编排器。单实例由 AgentServer 生命周期管理（main 起 task）。

    get_inflight: callable -> int，返回当前在途请求数（busy-backoff 判据）。
    llm: LLMClient 实例（None → 不跑，测试/无 key 场景）。
    """

    def __init__(self, llm, get_inflight) -> None:
        self.llm = llm
        self._get_inflight = get_inflight

    async def run_loop(self) -> None:
        """后台循环：start_delay 后，每 interval 跑一次 dream。
        dream 出异常 fail-soft 吞掉（不崩循环）。被 start_dreaming 包成
        asyncio.Task；外部 cancel 即停。
        """
        from twinkle.config import (
            MEMORY_DREAMING_START_DELAY_SECONDS, MEMORY_DREAMING_INTERVAL_SECONDS)
        await asyncio.sleep(MEMORY_DREAMING_START_DELAY_SECONDS)
        while True:
            try:
                await self.dream()
            except Exception:
                log.exception("dreaming run failed (fail-soft)")
            await asyncio.sleep(MEMORY_DREAMING_INTERVAL_SECONDS)

    async def dream(self) -> None:
        """跑一轮整理（config 门 + busy-backoff）。门卫挡住直接返回；否则扫 daily
        拼够格候选进 MEMORY.md + 存 sidecar + 单次 LLM 整合 + 容量预算 compact。
        异常穿出交 run_loop fail-soft。纯副作用，不返回统计。
        """
        from twinkle.config import MEMORY_DREAMING_ENABLED
        if not MEMORY_DREAMING_ENABLED or self.llm is None:
            return
        if self._get_inflight() > 0:
            return  # 前台忙 → 跳过本轮
        from twinkle.agentserver.memory import get_memory_manager
        mgr = get_memory_manager()
        sidecar_path = mgr.memory_dir / "dreaming_state.json"
        # ① 抽候选:扫 daily_memory 非空行,按 md5 跨文件聚合(同行=同候选)
        claims = self._scan_claims(mgr)
        # ② 读历史已晋升集(sidecar),作不重复搬的判据
        promotion_state = self._load_state(sidecar_path)
        # ③ 门槛筛:够格(≥min_distinct_files 日复现)且未已晋升的候选(纯函数,无副作用)
        candidates = self._filter_promotable(claims, promotion_state)
        if candidates:
            # ④ 写 MEMORY.md(append)+ 记进已晋升集
            self._append_promotions(mgr, candidates, promotion_state)
            # ⑤ 落盘已晋升集(下次 _load_state 能读到,防下 tick 重搬)
            self._save_state(promotion_state, sidecar_path)
            # ⑥ 整合:单次 LLM 删 MEMORY.md 内语义重复/矛盾行(≤25%),fail-soft
            await self._consolidate(mgr)
        # ⑦ 容量预算:无论晋升与否都跑——手写行膨胀不靠新晋升,compact 独立兜底
        self._compact_if_over_budget(mgr, promotion_state)

    @staticmethod
    def _nonempty_lines(text: str) -> list[str]:
        """取文本非空行（每非空行一条目）。"""
        return [line for line in text.splitlines() if line.strip()]

    @staticmethod
    def _scan_claims(mgr) -> dict:
        """扫 daily_memory/*.md 非空行,按 claimHash(md5(line.strip()))跨文件聚合。

        返回 {hash: {"text": stripped_line, "source_files": set[str], "first_path": str}}。
        同文(去空白后同)跨文件 → 同 hash → 1 claim,source_files 累计出现的 daily
        文件(set 去重,跨日复现计数=晋升门判据);first_path = 排序后首个含此 claim
        的文件(供 sidecar source_path 记录)。daily append-only 只读,不动文件。
        """
        claims: dict[str, dict] = {}
        daily_files = [f for f in mgr.list_files() if f.startswith("daily_memory/")]
        for daily_path in daily_files:
            text = mgr.read(daily_path)
            if text.startswith("Error:"):
                continue
            for line in DreamingOrchestrator._nonempty_lines(text):
                stripped = line.strip()
                claim_hash = hashlib.md5(stripped.encode("utf-8")).hexdigest()
                claim = claims.get(claim_hash)
                if claim is None:
                    claims[claim_hash] = {
                        "text": stripped,
                        "source_files": {daily_path},
                        "first_path": daily_path,
                    }
                else:
                    claim["source_files"].add(daily_path)
        return claims

    @staticmethod
    def _load_state(sidecar_path) -> dict:
        """读 sidecar dreaming_state.json。不存在/坏 JSON/形状错 → {"version":1,"promoted":{}}。

        promoted 集只增不减(once promoted 不重晋,哪怕 consolidate 删了该行)——
        跟 openclaw recall store 的 promotedAt 同理。sidecar 由 dreaming 原子自管。
        """
        try:
            raw = sidecar_path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError, TypeError):
            return {"version": 1, "promoted": {}}
        if not isinstance(data, dict) or not isinstance(data.get("promoted"), dict):
            return {"version": 1, "promoted": {}}
        return data

    @staticmethod
    def _save_state(promotion_state: dict, sidecar_path) -> None:
        """原子写 sidecar(tempfile + rename 同目录),成功后不留 .tmp。

        失败清 .tmp 后抛出(交 run_loop fail-soft;此时 sidecar 漏记本轮晋升,
        下 tick 会重晋→ MEMORY.md 出重复行→ consolidate 清理,append-only 容错)。"""
        sidecar_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = sidecar_path.parent / (sidecar_path.name + ".tmp")
        try:
            tmp.write_text(json.dumps(promotion_state, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            tmp.replace(sidecar_path)
        except OSError:
            tmp.unlink(missing_ok=True)  # 别留 .tmp 残留
            raise

    @staticmethod
    def _filter_promotable(claims: dict, promotion_state: dict) -> list[dict]:
        """门槛筛:claims 中 source_files ≥ min_distinct_files 且 hash 不在已晋升集
        的候选。纯函数无副作用(不读盘不写盘不改 promotion_state),返回够格候选列表
        [{"hash":..., "text":..., "source_path":...}, ...]。空列表 → 本轮无新晋升。

        daily append-only 不动(claims 是其只读快照)。防同字面重复晋靠 hash 查已晋升集
        (只增不减,哪怕 consolidate 删了该行也不重晋)。语义重复留给 _consolidate LLM 清。"""
        from twinkle.config import MEMORY_DREAMING_MIN_DISTINCT_FILES
        candidates: list[dict] = []
        for claim_hash, claim in claims.items():
            if len(claim["source_files"]) < MEMORY_DREAMING_MIN_DISTINCT_FILES:
                continue  # 跨日复现不足 → 不够格
            if claim_hash in promotion_state["promoted"]:
                continue  # 已晋升 → 不重晋(哪怕 consolidate 删了该行)
            candidates.append({
                "hash": claim_hash,
                "text": claim["text"],
                "source_path": claim["first_path"],
            })
        return candidates

    @staticmethod
    def _append_promotions(mgr, candidates: list[dict], promotion_state: dict) -> None:
        """候选批量 append 进 MEMORY.md(一次写)+ 记进 promotion_state["promoted"]
        (ts/text/source_path)。零 LLM。幂等靠 promotion_state 已晋升集
        (由 _filter_promotable 已筛掉已晋升),append 本身不防重。promotion_state
        由调用方 _save_state 落盘。

        批量一次写而非逐条:每次 mgr.write 都触发 _index_file 全量重索引 MEMORY.md
        (删旧 chunks+重分块+重插);N 条候选逐条写 = N 次重索引,批量拼好一次写 = 1 次。"""
        if not candidates:
            return
        bulk = "".join(cand["text"] + "\n" for cand in candidates)
        mgr.write("MEMORY.md", bulk, append=True)
        for cand in candidates:
            promotion_state["promoted"][cand["hash"]] = {
                "ts": datetime.datetime.now().isoformat(),
                "text": cand["text"],
                "source_path": cand["source_path"],
            }

    async def _consolidate(self, mgr) -> None:
        """单次 LLM 整合:MEMORY.md 非空行编号 → LLM 出删行号列表 → 验证(比例 ≤
        max_delete_fraction)→ mgr.replace 留存行。任一步失败 fail-soft(append-only 版
        留着,等价 openclaw append-only fallback)。LLM 全程不碰文本原文,只出行号。

        只在晋升步搬了新内容后跑(dream body 据 _filter_promotable 候选非空判定)。
        """
        from twinkle.config import MEMORY_DREAMING_MAX_DELETE_FRACTION
        text = mgr.read("MEMORY.md")
        if text.startswith("Error:"):
            return  # 无 MEMORY.md → 无可整合
        lines = self._nonempty_lines(text)
        if len(lines) < 2:
            return  # 不足 2 行 → 无可合并
        numbered = "".join(f"{i}: {line}\n" for i, line in enumerate(lines, 1))
        raw = await self._ask_llm(_CONSOLIDATE_PROMPT.format(numbered_lines=numbered))
        if not raw:
            return  # LLM 失败/空 → fail-soft(append-only 版留着)
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            log.warning("dreaming consolidate: bad JSON, skip (append-only stays): %r", raw)
            return
        if not isinstance(data, dict) or not isinstance(data.get("delete"), list):
            log.warning("dreaming consolidate: 'delete' not a list, skip: %r", raw)
            return
        delete_set: set[int] = set()
        for n in data["delete"]:
            # bool 是 int 子类,但非合法行号 → 拦;范围 [1, len(lines)]
            if isinstance(n, bool) or not isinstance(n, int) or not (1 <= n <= len(lines)):
                log.warning("dreaming consolidate: invalid line number %r, skip", n)
                return  # 一个坏号 → 整体不删(保守,不部分应用)
            delete_set.add(n)
        if not delete_set:
            return  # 无可删 → 不必重写
        if len(delete_set) / len(lines) > MEMORY_DREAMING_MAX_DELETE_FRACTION:
            log.warning("dreaming consolidate: delete fraction %.2f > budget %.2f, skip",
                        len(delete_set) / len(lines), MEMORY_DREAMING_MAX_DELETE_FRACTION)
            return
        kept = [line for i, line in enumerate(lines, 1) if i not in delete_set]
        mgr.replace("MEMORY.md", "\n".join(kept) + "\n")

    @staticmethod
    def _compact_if_over_budget(mgr, promotion_state: dict) -> None:
        """MEMORY.md 超 max_memory_chars → 分阶段丢行 + reconcile 清孤儿,末尾落盘 sidecar。

        reconcile:promoted 只留 text 仍在 MEMORY.md 的记录(清 consolidate 删行/edit 改行
        产生的孤儿),防 sidecar 膨胀。未超预算也 reconcile + save。

        分阶段(手写不记 ts,机械兜底;consolidate LLM 删冗余在前兜智能):
        阶段1 丢 promotion 行(按 sidecar ts 最老→新)——episodic 该让位。
        阶段2 丢光 promotion 仍超 → 丢非 promotion 行(手写/未追踪,无 ts)按文件位置
        头部先(append-only 头部≈最早写)。未追踪行(迁移旧文件)同按位置丢头部。"""
        from twinkle.config import MEMORY_DREAMING_MAX_MEMORY_CHARS
        text = mgr.read("MEMORY.md")
        if text.startswith("Error:"):
            return  # 无 MEMORY.md → 无可 compact
        sidecar_path = mgr.memory_dir / "dreaming_state.json"
        lines = DreamingOrchestrator._nonempty_lines(text)
        file_text_set = {line.strip() for line in lines}
        # reconcile:promoted 只留 text 仍在文件的(清孤儿)
        promoted = promotion_state.get("promoted", {})
        reconciled = {h: rec for h, rec in promoted.items()
                      if isinstance(rec, dict)
                      and rec.get("text", "").strip() in file_text_set}
        reconciled_changed = len(reconciled) != len(promoted)  # 清了孤儿?
        promotion_state["promoted"] = reconciled
        budget = MEMORY_DREAMING_MAX_MEMORY_CHARS
        if len("\n".join(lines)) <= budget:
            if reconciled_changed:
                DreamingOrchestrator._save_state(promotion_state, sidecar_path)  # 清孤儿落盘
            return  # 未超 → 不丢行(只 reconcile 若有孤儿)
        # text → (hash, ts) 映射(reconcile 后的 promotion 行;text 唯一因 hash=md5(text))
        text_to_hash_ts = {
            rec["text"].strip(): (h, rec.get("ts", ""))
            for h, rec in reconciled.items() if isinstance(rec, dict)}
        keep = [True] * len(lines)
        # 阶段1:丢 promotion 行(按 ts 升序,最老先丢)
        promo_drop = sorted(
            ((i, text_to_hash_ts[line.strip()][1]) for i, line in enumerate(lines)
             if line.strip() in text_to_hash_ts),
            key=lambda x: x[1])
        for i, _ in promo_drop:
            kept = [line for j, line in enumerate(lines) if keep[j]]
            if len("\n".join(kept)) <= budget:
                break  # 已在预算内 → 不再丢
            keep[i] = False  # 丢这条最老的仍存在 promotion 行
            promotion_state["promoted"].pop(text_to_hash_ts[lines[i].strip()][0], None)
        # 阶段2:丢光 promotion 仍超 → 丢非 promotion 行按文件位置头部先(append-only 头部≈最早)
        if len("\n".join([line for j, line in enumerate(lines) if keep[j]])) > budget:
            for i in range(len(lines)):
                if not keep[i] or lines[i].strip() in text_to_hash_ts:
                    continue  # 已丢 或 promotion 行(阶段1 处理)
                kept = [line for j, line in enumerate(lines) if keep[j]]
                if len("\n".join(kept)) <= budget:
                    break
                keep[i] = False  # 丢非 promotion 头部
        if all(keep):
            if reconciled_changed:
                DreamingOrchestrator._save_state(promotion_state, sidecar_path)
            return  # 无可丢(reconcile 若有孤儿清)
        kept_lines = [line for j, line in enumerate(lines) if keep[j]]
        mgr.replace("MEMORY.md", "\n".join(kept_lines) + "\n")
        DreamingOrchestrator._save_state(promotion_state, sidecar_path)

    async def _ask_llm(self, prompt: str) -> str:
        """调 LLM 单轮（无 tools），收集文本。异常 fail-soft 返回空串。"""
        try:
            chunks: list[str] = []
            async for ev in self.llm.stream(
                    messages=[{"role": "user", "content": prompt}], tools=[]):
                if isinstance(ev, TextDelta):
                    chunks.append(ev.content)
            return "".join(chunks)
        except Exception:
            log.exception("dreaming LLM call failed (fail-soft)")
            return ""


def start_dreaming(llm, get_inflight):
    """起后台 Dreaming task。enabled + llm 都满足才起；否则返回 None。
    需在运行中的 event loop 内调用（asyncio.create_task）。由 AgentServer
    main() 调用，返回的 task 随进程生命周期（main 退出即终止）。
    """
    from twinkle.config import MEMORY_DREAMING_ENABLED
    if not MEMORY_DREAMING_ENABLED or llm is None:
        return None
    orch = DreamingOrchestrator(llm, get_inflight)
    return asyncio.create_task(orch.run_loop())
