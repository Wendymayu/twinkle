"""EvolutionStore — 读写 evolutions.json、渲染索引块到 SKILL.md、管理 sidecar 文件。

扩展现有 skills/ 目录布局（不另起炉灶）。v1 只 4 个原语：
- append_record
- save_evolution_log
- render_evolution_markdown
- get_records_by_score
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict
from pathlib import Path

from twinkle.agentserver.evolution.types import EvolutionRecord, EvolutionLog

log = logging.getLogger("twinkle.evolution.store")

_EVOLUTION_FILENAME = "evolutions.json"
_EVOLUTION_DIR = "evolution"
_SCRIPTS_DIR = "scripts"

# 匹配 <!-- evolution-index-start -->...<!-- evolution-index-end -->
_EVOLUTION_INDEX_PATTERN = re.compile(
    r"<!-- evolution-index-start -->.*?<!-- evolution-index-end -->",
    re.DOTALL,
)


def _dataclass_to_dict(obj):
    """递归把 dataclass 转 dict，处理嵌套。"""
    if hasattr(obj, "__dataclass_fields__"):
        return {k: _dataclass_to_dict(v) for k, v in asdict(obj).items()}
    if isinstance(obj, list):
        return [_dataclass_to_dict(v) for v in obj]
    return obj


class EvolutionStore:
    """管理 skill 进化经验的持久化存储。"""

    def __init__(self, skills_dir: str) -> None:
        self._skills_dir = Path(skills_dir)

    # --- 路径辅助 ---

    def _skill_dir(self, name: str) -> Path:
        return self._skills_dir / name

    def _evolution_dir(self, name: str) -> Path:
        return self._skill_dir(name) / _EVOLUTION_DIR

    def _scripts_dir(self, name: str) -> Path:
        return self._evolution_dir(name) / _SCRIPTS_DIR

    def _evolution_log_path(self, name: str) -> Path:
        return self._skill_dir(name) / _EVOLUTION_FILENAME

    def skill_md_path(self, name: str) -> Path:
        return self._skill_dir(name) / "SKILL.md"

    # --- 读 ---

    def read_evolution_log(self, skill_name: str) -> EvolutionLog:
        """读 evolutions.json → EvolutionLog。文件不存在返空。"""
        path = self._evolution_log_path(skill_name)
        if not path.exists():
            return EvolutionLog()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            entries = []
            for item in data.get("entries", []):
                entries.append(self._dict_to_record(item))
            return EvolutionLog(entries=entries)
        except Exception:
            log.exception("failed to read %s", path)
            return EvolutionLog()

    def get_records_by_score(self, skill_name: str, min_score: float = 0.0, limit: int = 10) -> list[EvolutionRecord]:
        """按分降序取 top-N 经验记录。"""
        evolution_log = self.read_evolution_log(skill_name)
        filtered = [r for r in evolution_log.entries if r.score >= min_score]
        filtered.sort(key=lambda r: r.score, reverse=True)
        return filtered[:limit]

    def read_pristine_skill_content(self, skill_name: str) -> str:
        """读 SKILL.md 并剥掉索引块（上传 SkillHub 等跨用户分享场景用）。"""
        path = self.skill_md_path(skill_name)
        if not path.exists():
            return ""
        content = path.read_text(encoding="utf-8")
        return _EVOLUTION_INDEX_PATTERN.sub("", content).strip()

    # --- 写 ---

    def save_evolution_log(self, skill_name: str, entries: list[EvolutionRecord]) -> None:
        """原子写入 evolutions.json（temp-rename）。"""
        path = self._evolution_log_path(skill_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        log_data = {"entries": [_dataclass_to_dict(r) for r in entries]}
        json_text = json.dumps(log_data, ensure_ascii=False, indent=2)
        # 原子写：临时文件 + rename
        import tempfile
        temp_file = tempfile.NamedTemporaryFile(
            mode="w", dir=str(path.parent), delete=False,
            suffix=".tmp", encoding="utf-8",
        )
        try:
            temp_file.write(json_text)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        finally:
            temp_file.close()
        os.replace(temp_file.name, str(path))

    def append_record(self, skill_name: str, record: EvolutionRecord) -> None:
        """追加/合并一条经验记录到 evolutions.json。

        merge_target: 如果 record.change.merge_target 非空且指向已有记录，
        则改写该已有记录的 change.content（merge），否则 append。
        """
        evolution_log = self.read_evolution_log(skill_name)
        merge_target = record.change.merge_target

        if merge_target:
            # 合并模式：找到目标记录，改写其 content
            for idx, existing in enumerate(evolution_log.entries):
                if existing.id == merge_target:
                    existing.change.content = record.change.content
                    existing.change.summary = record.change.summary
                    existing.score = max(existing.score, record.score)
                    existing.timestamp = record.timestamp
                    break
            else:
                # merge_target 指向不存在 → 退化 append
                evolution_log.entries.append(record)
        else:
            evolution_log.entries.append(record)

        self.save_evolution_log(skill_name, evolution_log.entries)

    def render_evolution_markdown(self, skill_name: str, records: list[EvolutionRecord]) -> None:
        """往 SKILL.md 注入/替换索引块，正文写 sidecar 文件。

        索引块格式:
            <!-- evolution-index-start -->
            ## Evolution Experiences
            ...
            <!-- evolution-index-end -->

        正文写入 evolution/<section>.md，每条用 <a id="{record.id}"> 锚定。
        脚本工件写入 evolution/scripts/<filename>。
        """
        if not records:
            return

        evo_dir = self._evolution_dir(skill_name)
        scripts_dir = self._scripts_dir(skill_name)
        evo_dir.mkdir(parents=True, exist_ok=True)

        section_records = self._group_records_by_section(records, scripts_dir)
        index_lines = self._write_sidecars(section_records, evo_dir)
        self._upsert_index_block(skill_name, records, section_records, index_lines)

    def _group_records_by_section(
        self, records: list[EvolutionRecord], scripts_dir: Path
    ) -> dict[str, list[tuple[EvolutionRecord, str]]]:
        """按 section 分组记录，脚本工件写盘，返 (record, display_content) 分组。

        display_content 承接渲染态文本：脚本类记录源码写 evolution/scripts/<filename>，
        对外展示改为引用串（不改写输入 record.content——否则调用方持有的 record 被污染成
        引用串，后续 append/重渲染读到非源码，见 test_render_does_not_mutate_input_record_content）。
        skip 记录跳过。
        """
        section_records: dict[str, list[tuple[EvolutionRecord, str]]] = {}
        for r in records:
            if r.change.action == "skip":
                continue
            display_content = r.change.content
            if r.change.target == "script" and r.change.script_filename:
                scripts_dir.mkdir(parents=True, exist_ok=True)
                (scripts_dir / r.change.script_filename).write_text(r.change.content, encoding="utf-8")
                display_content = f"See `evolution/scripts/{r.change.script_filename}`"
            section = r.change.section or "General"
            section_records.setdefault(section, []).append((r, display_content))
        return section_records

    def _write_sidecars(
        self, section_records: dict[str, list[tuple[EvolutionRecord, str]]], evo_dir: Path
    ) -> list[str]:
        """每个 section 写一个 sidecar md（标题 + 锚点 + 正文），返索引行列表。"""
        index_lines: list[str] = []
        for section, recs in sorted(section_records.items()):
            lines: list[str] = [f"# {section} Experiences\n"]
            for r, display_content in recs:
                lines.append(f"### [{r.id}] {r.summary or r.change.section}")
                lines.append(f'<a id="{r.id}"></a>')
                lines.append("")
                lines.append(display_content)
                lines.append("")
                # 索引行
                index_lines.append(
                    f"- **[{r.id}]** ({r.source}, score={r.score:.2f}) "
                    f"— {r.summary or 'no summary'} "
                    f"[→](evolution/{section}.md#{r.id})"
                )
            (evo_dir / f"{section}.md").write_text("\n".join(lines), encoding="utf-8")
        return index_lines

    def _upsert_index_block(
        self, skill_name: str, records: list[EvolutionRecord],
        section_records: dict[str, list[tuple[EvolutionRecord, str]]],
        index_lines: list[str],
    ) -> None:
        """组装索引块字符串，插入或替换 SKILL.md 中的 evolution-index 块。"""
        total = len(records)
        last_updated = records[-1].timestamp if records else ""
        parts = ", ".join(
            f"{sec}({len(recs)})"
            for sec, recs in sorted(section_records.items())
        )
        index_block = "\n".join([
            "",
            "<!-- evolution-index-start -->",
            "## Evolution Experiences",
            f"This skill has accumulated **{total}** evolution experiences ({parts}).",
            *index_lines,
            f"*Last updated: {last_updated}*",
            "<!-- evolution-index-end -->",
        ])

        skill_md = self.skill_md_path(skill_name)
        content = skill_md.read_text(encoding="utf-8") if skill_md.exists() else ""
        if _EVOLUTION_INDEX_PATTERN.search(content):
            content = _EVOLUTION_INDEX_PATTERN.sub(index_block, content)
        else:
            content = content.rstrip() + "\n" + index_block + "\n"
        skill_md.write_text(content, encoding="utf-8")

    # --- 序列化辅助 ---

    @staticmethod
    def _dict_to_record(d: dict) -> EvolutionRecord:
        """从 JSON dict 反序列化 EvolutionRecord。"""
        from twinkle.agentserver.evolution.types import EvolutionPatch, UsageStats
        patch_data = d.get("change", {})
        patch = EvolutionPatch(
            section=patch_data.get("section", ""),
            action=patch_data.get("action", "append"),
            content=patch_data.get("content", ""),
            target=patch_data.get("target", "body"),
            skip_reason=patch_data.get("skip_reason"),
            merge_target=patch_data.get("merge_target"),
            script_filename=patch_data.get("script_filename"),
            script_language=patch_data.get("script_language"),
            script_purpose=patch_data.get("script_purpose"),
            keywords=patch_data.get("keywords"),
            summary=patch_data.get("summary"),
        )
        usage = d.get("usage_stats")
        usage_stats = None
        if usage:
            usage_stats = UsageStats(
                times_presented=usage.get("times_presented", 0),
                times_used=usage.get("times_used", 0),
                times_positive=usage.get("times_positive", 0),
                times_negative=usage.get("times_negative", 0),
                last_presented_at=usage.get("last_presented_at"),
                last_evaluated_at=usage.get("last_evaluated_at"),
            )
        return EvolutionRecord(
            id=d.get("id", ""),
            source=d.get("source", ""),
            timestamp=d.get("timestamp", ""),
            context=d.get("context", ""),
            change=patch,
            score=d.get("score", 0.6),
            usage_stats=usage_stats,
            skill_version=d.get("skill_version"),
            summary=d.get("summary"),
        )
