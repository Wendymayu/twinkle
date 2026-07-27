import pytest
from pydantic import ValidationError

from twinkle.config.loader import _resolve_env_vars, load_config


# --- _resolve_env_vars ---


def test_env_value_wins_over_default(monkeypatch):
    monkeypatch.setenv("MY_VAR", "from-env")
    assert _resolve_env_vars("v: ${MY_VAR:-fallback}") == "v: from-env"


def test_default_when_unset(monkeypatch):
    monkeypatch.delenv("MY_VAR", raising=False)
    assert _resolve_env_vars("v: ${MY_VAR:-fallback}") == "v: fallback"


def test_empty_env_falls_to_default(monkeypatch):
    # mirrors current `os.getenv(X) or default` (empty string is falsy)
    monkeypatch.setenv("MY_VAR", "")
    assert _resolve_env_vars("v: ${MY_VAR:-fallback}") == "v: fallback"


def test_no_default_unset_yields_empty(monkeypatch):
    monkeypatch.delenv("MY_VAR", raising=False)
    assert _resolve_env_vars("v: '${MY_VAR}'") == "v: ''"


def test_plain_text_unchanged(monkeypatch):
    monkeypatch.delenv("MY_VAR", raising=False)
    assert _resolve_env_vars("just text: 18000") == "just text: 18000"


def test_multiple_occurrences(monkeypatch):
    monkeypatch.setenv("A", "1")
    monkeypatch.delenv("B", raising=False)
    assert _resolve_env_vars("${A:-0}/${B:-0}") == "1/0"


# --- load_config ---


def test_loads_packaged_defaults(monkeypatch):
    # hermetic: clear surviving env so packaged defaults apply
    for k in ("TWINKLE_AGENTSERVER_PORT", "TWINKLE_LLM_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    c = load_config()  # reads packaged resources/config.yaml
    assert c.agentserver.port == 18000
    assert c.permissions.enabled is False
    assert c.permissions.tools["command_exec"] == "require-approval"
    assert c.skills.mode == "all"


def test_env_override_in_yaml(monkeypatch):
    monkeypatch.setenv("TWINKLE_AGENTSERVER_PORT", "12345")
    c = load_config()  # ${TWINKLE_AGENTSERVER_PORT:-18000} -> 12345
    assert c.agentserver.port == 12345


def test_empty_env_uses_yaml_default(monkeypatch):
    monkeypatch.setenv("TWINKLE_AGENTSERVER_PORT", "")
    c = load_config()
    assert c.agentserver.port == 18000


def test_custom_path_overrides_packaged(tmp_path):
    custom = tmp_path / "config.yaml"
    custom.write_text(
        "agentserver:\n  host: 0.0.0.0\n  port: 9999\n"
        "permissions:\n  enabled: true\n  tools:\n    echo: deny\n",
        encoding="utf-8",
    )
    c = load_config(custom)  # other sections come from schema defaults
    assert c.agentserver.port == 9999
    assert c.permissions.enabled is True
    assert c.permissions.tools["echo"] == "deny"
    # schema defaults fill the gaps
    assert c.skills.mode == "all"


def test_bad_tier_in_yaml_raises(tmp_path):
    custom = tmp_path / "config.yaml"
    custom.write_text(
        "permissions:\n  tools:\n    command_exec: BOGUS\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_config(custom)


def test_unquoted_empty_default_not_null(tmp_path, monkeypatch):
    # parse-first: an UNQUOTED empty-default ${VAR:-} resolves to "" (str), not None.
    # (Under text-first this raised ValidationError because `api_key: ` parsed as null.)
    monkeypatch.delenv("MY_UNSET_VAR", raising=False)
    custom = tmp_path / "config.yaml"
    custom.write_text("llm:\n  api_key: ${MY_UNSET_VAR:-}\n", encoding="utf-8")
    c = load_config(custom)
    assert c.llm.api_key == ""
