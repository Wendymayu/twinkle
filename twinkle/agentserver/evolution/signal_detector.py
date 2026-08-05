"""ConversationSignalDetector — 规则为主扫对话里的工具调用结果，归因到具体 skill。

不用 LLM——纯正则+路径匹配，便宜可复现。
"""
from __future__ import annotations

import re
from typing import Any

from twinkle.agentserver.evolution.types import FAILURE_KEYWORDS, ConversationSignal


class ConversationSignalDetector:
    """规则信号检测器：扫工具调用结果，检测 failure/script/user_intent 信号。"""

    def detect(self, messages: list[dict], skill_names: list[str],
               enabled_signals: set[str] | None = None) -> list[ConversationSignal]:
        """扫消息列表，返检测到的信号列表。

        *messages*: [{role, content, tool_calls?, tool_call_id?}, ...]
        *skill_names*: 当前可用的 skill 名列表（用于归因）
        *enabled_signals*: 启用的信号类型，默认只开 failure+script
        """
        if enabled_signals is None:
            enabled_signals = {"execution_failure", "script_artifact"}

        signals: list[ConversationSignal] = []
        skill_read_history = self._detect_skill_from_tool_calls(messages)

        for idx, msg in enumerate(messages):
            active_skill = self._resolve_active_skill(idx, skill_read_history)

            if msg.get("role") != "tool":
                # 用户消息检查 user_intent
                if "user_intent" in enabled_signals and msg.get("role") == "user":
                    signal = self._detect_user_intent(msg, active_skill, skill_names, idx)
                    if signal:
                        signals.append(signal)
                continue

            content = str(msg.get("content", ""))
            tool_name = msg.get("name", "")

            # 1. execution_failure
            if "execution_failure" in enabled_signals and self._is_failure(content):
                skill = active_skill or self._guess_skill_from_content(content, skill_names)
                if skill:
                    signals.append(ConversationSignal(
                        type="execution_failure", skill_name=skill,
                        context=self._truncate(content, 500), msg_index=idx,
                    ))

            # 2. script_artifact
            if "script_artifact" in enabled_signals and self._is_script_success(tool_name, content):
                skill = active_skill or self._guess_skill_from_content(content, skill_names)
                if skill:
                    signals.append(ConversationSignal(
                        type="script_artifact", skill_name=skill,
                        context=self._truncate(content, 1000), msg_index=idx,
                    ))

        return signals

    # --- 信号检测 ---

    def _is_failure(self, content: str) -> bool:
        """检查内容是否包含失败关键词。"""
        lower = content.lower()
        return any(kw in lower for kw in FAILURE_KEYWORDS)

    def _is_script_success(self, tool_name: str, content: str) -> bool:
        """检查是否是成功执行的代码工具调用。"""
        # 代码执行类工具名
        script_tools = {"command_exec", "run_code", "execute", "python", "bash", "shell"}
        if tool_name not in script_tools:
            return False
        return not self._is_failure(content) and len(content.strip()) > 20

    def _detect_user_intent(self, msg: dict, active_skill: str | None,
                            skill_names: list[str], idx: int) -> ConversationSignal | None:
        """检测用户纠正信号。"""
        content = str(msg.get("content", "")).lower()
        correction_phrases = ["wrong", "should be", "not that", "actually", "不对", "应该是", "不是这个"]
        if not any(phrase in content for phrase in correction_phrases):
            return None
        skill = active_skill or self._guess_skill_from_content(content, skill_names)
        if not skill:
            return None
        return ConversationSignal(
            type="user_intent", skill_name=skill,
            context=self._truncate(str(msg.get("content", "")), 300), msg_index=idx,
        )

    # --- skill 归因 ---

    def _detect_skill_from_tool_calls(self, messages: list[dict]) -> list[tuple[int, str]]:
        """扫消息列表，找工具调用中引用的 skill 路径/名称。

        返 [(msg_index, skill_name), ...] 按消息顺序排列。
        两条路：
        1. 工具参数路径匹配 .../<skill_name>/SKILL.md
        2. 工具名是 skill_tool 且参数含 skill_name
        """
        history: list[tuple[int, str]] = []
        skill_path_pattern = re.compile(r"(?:^|[\\/])([a-zA-Z0-9_-]+)/SKILL\.md", re.IGNORECASE)

        for idx, msg in enumerate(messages):
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    func = tc.get("function", {})
                    tool_name = func.get("name", "")
                    args_str = func.get("arguments", "{}")

                    # 路径 1: 正则扫参数里的 SKILL.md 路径
                    match = skill_path_pattern.search(args_str)
                    if match:
                        history.append((idx, match.group(1)))
                        continue

                    # 路径 2: skill_tool + skill_name 参数
                    if tool_name == "skill_tool":
                        try:
                            import json
                            args = json.loads(args_str) if isinstance(args_str, str) else args_str
                            skill_name = args.get("skill_name")
                            if skill_name:
                                history.append((idx, skill_name))
                        except Exception:
                            pass

        return history

    def _resolve_active_skill(self, msg_idx: int,
                              skill_read_history: list[tuple[int, str]]) -> str | None:
        """取最近一次读过的 skill（消息索引 ≤ msg_idx）。"""
        for idx, name in reversed(skill_read_history):
            if idx <= msg_idx:
                return name
        return None

    def _guess_skill_from_content(self, content: str, skill_names: list[str]) -> str | None:
        """从内容中猜测引用了哪个 skill（按名称匹配）。"""
        lower = content.lower()
        for name in sorted(skill_names, key=len, reverse=True):  # 长名优先
            if name.lower() in lower:
                return name
        return None

    @staticmethod
    def _truncate(text: str, max_len: int) -> str:
        if len(text) <= max_len:
            return text
        return text[:max_len] + "..."
