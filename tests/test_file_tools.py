import asyncio
import json

import pytest

from twinkle.agentserver.tools.builtin import file_tools
from twinkle.agentserver.tools.errors import ToolError


@pytest.fixture
def ws(monkeypatch, tmp_path):
    """Point file_tools at a tmp workspace with a fixed session id."""
    monkeypatch.setattr(file_tools, "WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setattr(file_tools, "get_plan_todo_session_id", lambda: "test-session-key")
    file_tools._registry.clear("test-session-key")
    return tmp_path


def _invoke(tool, **args):
    return asyncio.run(tool.invoke(args))


# --- _resolve_file_path ---

def test_resolve_relative_under_workspace(ws):
    p = file_tools._resolve_file_path("a/b.txt")
    assert p == (ws / "a" / "b.txt").resolve()


def test_resolve_absolute_inside_workspace(ws):
    p = file_tools._resolve_file_path(str(ws / "c.txt"))
    assert p == (ws / "c.txt").resolve()


def test_resolve_rejects_relative_escape(ws):
    with pytest.raises(ValueError):
        file_tools._resolve_file_path("../../escape.txt")


def test_resolve_rejects_absolute_escape(ws):
    with pytest.raises(ValueError):
        file_tools._resolve_file_path(str(ws.parent / "outside.txt"))


# --- _is_binary ---

def test_is_binary_by_extension(ws):
    (ws / "x.png").write_bytes(b"not really png")
    assert file_tools._is_binary(ws / "x.png") is True


def test_is_binary_by_null_byte(ws):
    (ws / "x.dat").write_bytes(b"abc\x00def")
    assert file_tools._is_binary(ws / "x.dat") is True


def test_is_text_not_binary(ws):
    (ws / "x.txt").write_text("hello world", encoding="utf-8")
    assert file_tools._is_binary(ws / "x.txt") is False


# --- FileReadRegistry ---

def test_registry_mark_and_has_read():
    reg = file_tools.FileReadRegistry()
    reg.mark_read("s1", "/p/a")
    assert reg.has_read("s1", "/p/a") is True
    assert reg.has_read("s1", "/p/b") is False


def test_registry_session_isolation():
    reg = file_tools.FileReadRegistry()
    reg.mark_read("s1", "/p/a")
    assert reg.has_read("s2", "/p/a") is False


def test_registry_clear():
    reg = file_tools.FileReadRegistry()
    reg.mark_read("s1", "/p/a")
    reg.clear("s1")
    assert reg.has_read("s1", "/p/a") is False


# --- read_file ---

def test_read_file_returns_content_and_marks_registry(ws):
    (ws / "a.txt").write_text("line1\nline2\nline3", encoding="utf-8")
    out = _invoke(file_tools.read_file, file_path="a.txt")
    assert out == "line1\nline2\nline3"
    assert file_tools._registry.has_read("test-session-key", str((ws / "a.txt").resolve())) is True


def test_read_file_not_found(ws):
    with pytest.raises(ToolError, match="file not found"):
        _invoke(file_tools.read_file, file_path="missing.txt")


def test_read_file_binary_rejected(ws):
    (ws / "b.png").write_bytes(b"\x89PNG\r\n\x00")
    with pytest.raises(ToolError, match="binary or unsupported"):
        _invoke(file_tools.read_file, file_path="b.png")


def test_read_file_pagination(ws):
    (ws / "p.txt").write_text("\n".join(f"l{i}" for i in range(50)), encoding="utf-8")
    out = _invoke(file_tools.read_file, file_path="p.txt", offset=10, limit=5)
    assert "l10" in out and "l14" in out
    assert "l15" not in out
    assert "truncated" in out and "50 total lines" in out


def test_read_file_escape_rejected(ws):
    with pytest.raises(ToolError, match="outside the project workspace"):
        _invoke(file_tools.read_file, file_path="../../outside.txt")


def test_read_file_empty_path(ws):
    with pytest.raises(ToolError, match="file_path is required"):
        _invoke(file_tools.read_file, file_path="")


# --- write_file ---

def test_write_file_creates_new(ws):
    out = _invoke(file_tools.write_file, file_path="new.txt", content="hello")
    payload = json.loads(out)
    assert payload["type"] == "create"
    assert payload["bytes_written"] == 5
    assert (ws / "new.txt").read_text(encoding="utf-8") == "hello"


def test_write_file_overwrite_requires_prior_read(ws):
    (ws / "e.txt").write_text("existing", encoding="utf-8")
    with pytest.raises(ToolError, match="must read_file before overwriting"):
        _invoke(file_tools.write_file, file_path="e.txt", content="new")


def test_write_file_overwrite_after_read(ws):
    (ws / "e.txt").write_text("existing", encoding="utf-8")
    _invoke(file_tools.read_file, file_path="e.txt")
    out = _invoke(file_tools.write_file, file_path="e.txt", content="new")
    payload = json.loads(out)
    assert payload["type"] == "update"
    assert (ws / "e.txt").read_text(encoding="utf-8") == "new"


def test_write_file_too_large(ws):
    with pytest.raises(ToolError, match="content too large"):
        _invoke(file_tools.write_file, file_path="big.txt", content="x" * (5 * 1024 * 1024 + 1))


def test_write_file_creates_parent_dirs(ws):
    _invoke(file_tools.write_file, file_path="sub/dir/n.txt", content="x")
    assert (ws / "sub" / "dir" / "n.txt").exists()


def test_write_file_escape_rejected(ws):
    with pytest.raises(ToolError, match="outside the project workspace"):
        _invoke(file_tools.write_file, file_path="../../out.txt", content="x")


# --- edit_file ---

def test_edit_file_requires_prior_read(ws):
    (ws / "e.txt").write_text("foo bar foo", encoding="utf-8")
    with pytest.raises(ToolError, match="must read_file before editing"):
        _invoke(file_tools.edit_file, file_path="e.txt", old_string="foo", new_string="baz")


def test_edit_file_single_replace(ws):
    (ws / "e.txt").write_text("foo bar baz", encoding="utf-8")
    _invoke(file_tools.read_file, file_path="e.txt")
    out = _invoke(file_tools.edit_file, file_path="e.txt", old_string="foo", new_string="X")
    payload = json.loads(out)
    assert payload["replacements"] == 1
    assert (ws / "e.txt").read_text(encoding="utf-8") == "X bar baz"


def test_edit_file_multiple_without_replace_all_rejected(ws):
    (ws / "e.txt").write_text("foo bar foo", encoding="utf-8")
    _invoke(file_tools.read_file, file_path="e.txt")
    with pytest.raises(ToolError, match="matches 2 times"):
        _invoke(file_tools.edit_file, file_path="e.txt", old_string="foo", new_string="baz")


def test_edit_file_replace_all(ws):
    (ws / "e.txt").write_text("foo bar foo", encoding="utf-8")
    _invoke(file_tools.read_file, file_path="e.txt")
    out = _invoke(file_tools.edit_file, file_path="e.txt", old_string="foo", new_string="baz", replace_all=True)
    payload = json.loads(out)
    assert payload["replacements"] == 2
    assert (ws / "e.txt").read_text(encoding="utf-8") == "baz bar baz"


def test_edit_file_old_string_not_found(ws):
    (ws / "e.txt").write_text("hello", encoding="utf-8")
    _invoke(file_tools.read_file, file_path="e.txt")
    with pytest.raises(ToolError, match="old_string not found"):
        _invoke(file_tools.edit_file, file_path="e.txt", old_string="zzz", new_string="y")


def test_edit_file_empty_old_string_rejected(ws):
    (ws / "e.txt").write_text("hello", encoding="utf-8")
    _invoke(file_tools.read_file, file_path="e.txt")
    with pytest.raises(ToolError, match="use write_file to create"):
        _invoke(file_tools.edit_file, file_path="e.txt", old_string="", new_string="y")


def test_edit_file_chain_after_write(ws):
    _invoke(file_tools.write_file, file_path="w.txt", content="aabbcc")
    out = _invoke(file_tools.edit_file, file_path="w.txt", old_string="bb", new_string="BB")
    payload = json.loads(out)
    assert payload["replacements"] == 1
    assert (ws / "w.txt").read_text(encoding="utf-8") == "aaBBcc"


# --- list_files ---

def test_list_files_lists_dir(ws):
    (ws / "a.txt").write_text("x")
    (ws / "b.py").write_text("y")
    (ws / "sub").mkdir()
    out = _invoke(file_tools.list_files, path=".")
    payload = json.loads(out)
    names = {e["name"] for e in payload["entries"]}
    assert names == {"a.txt", "b.py", "sub"}
    types = {e["name"]: e["type"] for e in payload["entries"]}
    assert types["a.txt"] == "file"
    assert types["sub"] == "dir"


def test_list_files_hidden_filtered_by_default(ws):
    (ws / ".hidden").write_text("x")
    (ws / "visible.txt").write_text("y")
    out = _invoke(file_tools.list_files, path=".")
    payload = json.loads(out)
    names = {e["name"] for e in payload["entries"]}
    assert "visible.txt" in names
    assert ".hidden" not in names


def test_list_files_show_hidden(ws):
    (ws / ".hidden").write_text("x")
    out = _invoke(file_tools.list_files, path=".", show_hidden=True)
    payload = json.loads(out)
    assert ".hidden" in {e["name"] for e in payload["entries"]}


def test_list_files_not_a_dir(ws):
    (ws / "f.txt").write_text("x")
    with pytest.raises(ToolError, match="not a directory"):
        _invoke(file_tools.list_files, path="f.txt")


def test_list_files_escape_rejected(ws):
    with pytest.raises(ToolError, match="outside the project workspace"):
        _invoke(file_tools.list_files, path="../../")


# --- glob ---

def test_glob_matches_non_recursive(ws):
    (ws / "a.py").write_text("x")
    (ws / "b.txt").write_text("y")
    (ws / "sub").mkdir()
    (ws / "sub" / "c.py").write_text("z")
    out = _invoke(file_tools.glob, pattern="*.py")
    payload = json.loads(out)
    assert payload["matches"] == ["a.py"]


def test_glob_recursive(ws):
    (ws / "a.py").write_text("x")
    (ws / "sub").mkdir()
    (ws / "sub" / "c.py").write_text("z")
    out = _invoke(file_tools.glob, pattern="**/*.py")
    payload = json.loads(out)
    assert payload["matches"]  # non-empty
    assert any("c.py" in m for m in payload["matches"])  # recursion reached the subdir


def test_glob_rejects_dotdot(ws):
    with pytest.raises(ToolError, match=r"must not contain '\.\.'"):
        _invoke(file_tools.glob, pattern="../**")


def test_glob_escape_base_rejected(ws):
    with pytest.raises(ToolError, match="outside the project workspace"):
        _invoke(file_tools.glob, pattern="*.py", path="../../")


def test_glob_absolute_pattern_returns_error(ws):
    # Path.glob raises NotImplementedError on absolute patterns (Py 3.14);
    # the tool must catch it and raise a clean ToolError, not leak.
    with pytest.raises(ToolError, match="glob failed"):
        _invoke(file_tools.glob, pattern="/abs/*")
