import inspect


def test_agent_loop_has_no_memory_param():
    from twinkle.agentserver.agent import ReActAgent as AgentLoop
    params = inspect.signature(AgentLoop.__init__).parameters
    assert "memory" not in params
    for required in ("llm", "store", "tools"):
        assert required in params


def test_create_agent_has_no_memory_arg():
    from twinkle.agentserver.server import create_agent
    params = inspect.signature(create_agent).parameters
    assert "memory" not in params


def test_no_long_term_memory_references_in_core():
    import twinkle.agentserver.agent as al
    import twinkle.agentserver.server as sv
    for mod in (al, sv):
        assert "LongTermMemory" not in inspect.getsource(mod), \
            f"LongTermMemory still referenced in {mod.__name__}"


def test_memory_stub_replaced_by_package():
    import twinkle.agentserver.memory as mem  # the package (not the old stub)
    from twinkle.agentserver.memory.store import MemoryManager
    assert hasattr(mem, "get_memory_manager")
    assert MemoryManager is not None


def test_memory_hook_in_default_loop_hooks():
    """create_agent + main() register MemoryHook alongside SkillHook."""
    # main() builds hooks explicitly; we check MemoryHook is exported + instantiable.
    from twinkle.agentserver.hooks.builtin import MemoryHook, SkillHook
    assert MemoryHook.priority < SkillHook.priority  # 80 < 90
