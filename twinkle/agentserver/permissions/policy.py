"""PermissionPolicy — 档位 + 规则 + allow_always override 合并。

决策合并序(对齐 spec §4.2 决策流,除「通道门」由 engine 包):
  1. allow_always override(运行时文件,mtime 缓存热重载)
  2. DENY 规则(command_exec 走 builtin_rules;用户 rules 走正则)
  3. 工具档位(tools[name])
  4. global_default
"""
from __future__ import annotations

import asyncio
import fnmatch
import json
import os
import re
from pathlib import Path
from typing import Any

from twinkle.agentserver.permissions.builtin_rules import matches as _cmd_matches
from twinkle.agentserver.permissions.models import PermissionDecision, PermissionLevel


# Shell 串联 / 混淆用的元字符。被 bless 的命令头部必须是单条简单命令;
# 若出现这些字符,拒绝通过 allow_always override bless,回退到 deny 规则 / 档位判定。
# 否则一条持久化的 "npm run *" pattern 会 bless 掉 "npm run build && rm -rf /"。
_SHELL_METACHARS = frozenset(";&|<>`$\n")


class PermissionPolicy:
    def __init__(
        self,
        tools: dict[str, str],
        rules: list[dict],
        approval_overrides: dict[str, Any],
        global_default: str,
        overrides_file: str | None,
    ) -> None:
        self._tools = dict(tools)
        self._rules = list(rules)
        self._global_default = global_default
        self._overrides_file = overrides_file
        self._mtime = -1.0
        self._cache: dict[str, Any] = dict(approval_overrides)

    # --- override 加载(mtime 缓存热重载) ---

    def _load_overrides(self) -> dict[str, Any]:
        if not self._overrides_file:
            return self._cache
        try:
            mt = os.path.getmtime(self._overrides_file)
        except OSError:
            return self._cache
        if mt != self._mtime:
            try:
                self._cache = json.loads(Path(self._overrides_file).read_text("utf-8"))
            except Exception:
                self._cache = {}
            self._mtime = mt
        return self._cache

    def _matches_override(self, tool: str, args: dict, ovr: dict) -> bool:
        if tool == "command_exec":
            patterns = ovr.get("command_exec", [])
            cmd = args.get("command", "")
            # 拒绝 bless 通过 shell 元字符串联 / 混淆的命令 —— 头部 pattern
            # 必须匹配单条简单命令,否则 deny 规则可能被绕过。
            if any(c in cmd for c in _SHELL_METACHARS):
                return False
            for p in patterns:
                if fnmatch.fnmatch(cmd, p) or cmd == p.rstrip(" *"):
                    return True
            return False
        return ovr.get(tool) == "allow"

    # --- check ---

    def check(self, tool: str, args: dict) -> PermissionDecision:
        ovr = self._load_overrides()
        if self._matches_override(tool, args, ovr):
            return PermissionDecision(level=PermissionLevel.ALLOW, reason="allow_always override",
                                      source="override")
        # command_exec 内置 deny 规则
        if tool == "command_exec":
            reason = _cmd_matches(args.get("command", ""))
            if reason:
                return PermissionDecision(level=PermissionLevel.DENY, reason=reason,
                                          source="rule", rule_id=reason,
                                          deny_message=f"[ERROR]: command rejected for safety ({reason}).")
        # 用户 deny 规则
        for r in self._rules:
            if r.get("tool") == tool:
                try:
                    if re.search(r["pattern"], str(args)):
                        return PermissionDecision(
                            level=PermissionLevel.DENY, reason=r.get("reason", "user deny rule"),
                            source="rule", rule_id=r.get("id"),
                            deny_message=f"[ERROR]: denied by rule {r.get('id')}: {r.get('reason','')}")
                except re.error:
                    continue
        # tier —— 将 "require-approval" 档位标签归一化为标准 ASK level
        tier = self._tools.get(tool, self._global_default)
        level = PermissionLevel.ASK if tier == "require-approval" else tier
        return PermissionDecision(level=level, reason=f"tier:{tier}", source="tier")

    # --- allow_always 持久化 ---

    async def persist_allow_always(self, decision_data: dict) -> None:
        tool = decision_data.get("tool")
        ovr = self._load_overrides()
        if tool == "command_exec":
            cmd = (decision_data.get("args") or {}).get("command", "")
            # 纯空白切分 —— 保留 Windows 反斜杠(shlex 会把
            # C:\Users 改坏成 C:Users);命令头部前几个 token 不含空格。
            tokens = cmd.split() if cmd else []
            head = " ".join(tokens[:2]) if len(tokens) >= 2 else (tokens[0] if tokens else "")
            if not head:
                # 无法解析 / 空命令绝不能变成全局
                # "*" allow,否则会 bless 掉如 rm -rf /。
                return
            pattern = head + " *"
            lst = ovr.setdefault("command_exec", [])
            if pattern not in lst:
                lst.append(pattern)
        else:
            ovr[tool] = "allow"
        if self._overrides_file:
            Path(self._overrides_file).parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(
                Path(self._overrides_file).write_text,
                json.dumps(ovr, ensure_ascii=False, indent=2), "utf-8")
        self._mtime = -1.0  # 下次 check 时强制重载
        self._cache = ovr
