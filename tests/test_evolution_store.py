"""测试 EvolutionStore — append/merge、原子写、索引块渲染、sidecar、pristine 剥离。"""
import json
from pathlib import Path

import pytest

from twinkle.agentserver.evolution.store import EvolutionStore
from twinkle.agentserver.evolution.types import (
    EvolutionRecord, EvolutionPatch, EvolutionLog,
)


@pytest.fixture
def store(tmp_path):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    return EvolutionStore(str(skills_dir))


def _make_skill(store: EvolutionStore, name: str, content: str = "# Test Skill\n\nSome content.\n"):
    """创建一个 skill 目录 + SKILL.md。"""
    skill_dir = store._skill_dir(name)
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(content, encoding="utf-8")


def _make_record(source="execution_failure", section="Troubleshooting",
                 content="如果遇到 timeout，请重试。", summary="timeout retry"):
    patch = EvolutionPatch(section=section, action="append", content=content, summary=summary)
    return EvolutionRecord.make(source=source, context="test context", change=patch, summary=summary)


def test_read_empty_evolution_log(store):
    _make_skill(store, "test")
    log = store._read_evolution_log("test")
    assert log.entries == []


def test_save_and_read_evolution_log(store):
    _make_skill(store, "test")
    rec = _make_record()
    store.save_evolution_log("test", [rec])

    log = store._read_evolution_log("test")
    assert len(log.entries) == 1
    assert log.entries[0].id == rec.id
    assert log.entries[0].change.section == "Troubleshooting"


def test_append_record(store):
    _make_skill(store, "test")
    rec1 = _make_record(section="Troubleshooting", content="fix 1")
    rec2 = _make_record(section="Examples", content="example 2")

    store.append_record("test", rec1)
    store.append_record("test", rec2)

    log = store._read_evolution_log("test")
    assert len(log.entries) == 2


def test_append_record_merge(store):
    _make_skill(store, "test")
    rec1 = _make_record(section="Troubleshooting", content="old content")
    store.append_record("test", rec1)

    # merge: 改写 rec1 的内容
    rec2 = _make_record(section="Troubleshooting", content="updated content")
    rec2.change.merge_target = rec1.id
    store.append_record("test", rec2)

    log = store._read_evolution_log("test")
    assert len(log.entries) == 1  # merge 不增加条目
    assert log.entries[0].change.content == "updated content"


def test_get_records_by_score(store):
    _make_skill(store, "test")
    rec1 = _make_record()
    rec1.score = 0.9
    rec2 = _make_record()
    rec2.score = 0.3
    rec3 = _make_record()
    rec3.score = 0.7

    store.save_evolution_log("test", [rec1, rec2, rec3])

    top = store.get_records_by_score("test", min_score=0.5, limit=2)
    assert len(top) == 2
    assert top[0].id == rec1.id  # 0.9
    assert top[1].id == rec3.id  # 0.7


def test_render_evolution_markdown_writes_index_block(store):
    _make_skill(store, "test", content="# Test Skill\n\nSome content.\n")
    rec = _make_record(content="如果遇到 timeout，请重试 3 次。", summary="timeout retry")

    store.render_evolution_markdown("test", [rec])

    skill_md = store._skill_md_path("test")
    content = skill_md.read_text(encoding="utf-8")
    assert "<!-- evolution-index-start -->" in content
    assert "<!-- evolution-index-end -->" in content
    assert "timeout retry" in content
    # 原始内容保留
    assert "# Test Skill" in content
    assert "Some content." in content


def test_render_evolution_markdown_replaces_existing_index(store):
    _make_skill(store, "test", content="# Test\n\n<!-- evolution-index-start -->\nOLD\n<!-- evolution-index-end -->\n")
    rec = _make_record(content="new content", summary="new summary")

    store.render_evolution_markdown("test", [rec])

    content = store._skill_md_path("test").read_text(encoding="utf-8")
    assert "OLD" not in content
    assert "new summary" in content


def test_render_evolution_markdown_creates_sidecar(store):
    _make_skill(store, "test")
    rec = _make_record(section="Troubleshooting", content="详细排查步骤...", summary="排查指南")

    store.render_evolution_markdown("test", [rec])

    sidecar = store._evolution_dir("test") / "Troubleshooting.md"
    assert sidecar.exists()
    sidecar_content = sidecar.read_text(encoding="utf-8")
    assert "详细排查步骤" in sidecar_content
    assert rec.id in sidecar_content


def test_pristine_strips_index_block(store):
    _make_skill(store, "test", content="# Test\n\n<!-- evolution-index-start -->\nINDEX\n<!-- evolution-index-end -->\n\nMore content.")
    pristine = store.read_pristine_skill_content("test")
    assert "<!-- evolution-index-start -->" not in pristine
    assert "INDEX" not in pristine
    assert "# Test" in pristine
    assert "More content." in pristine


def test_atomic_write_survives(store):
    """验原子写入：evolutions.json 不会出现半截文件。"""
    _make_skill(store, "test")
    recs = [_make_record() for _ in range(5)]
    store.save_evolution_log("test", recs)

    path = store._evolution_log_path("test")
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert len(data["entries"]) == 5
