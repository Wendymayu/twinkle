"""command_exec 的 deny 规则单一真源。

8 条来自原 command_exec blocklist(Windows-aware),9 条 verbatim 移植自
jiuwenswarm jiuwenclaw/resources/builtin_rules.yaml(git show enterprise_dev:
jiuwenclaw/resources/builtin_rules.yaml)。command_exec 与 PermissionPolicy
都引用本表,杜绝双份维护。
"""
from __future__ import annotations

import re

# (pattern, reason) — 命中即 DENY。
COMMAND_DENY_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # --- 原有 8 条(Windows-aware,保留作 defense-in-depth 与 disabled 模式守卫) ---
    (re.compile(r"\brm\s+-rf\b", re.IGNORECASE), "blocked pattern: rm -rf"),
    (re.compile(r"\bdel\s+/[a-z]*[fsq]", re.IGNORECASE), "blocked pattern: del /f /s /q"),
    (re.compile(r"\brd\s+/s\s+/q\b", re.IGNORECASE), "blocked pattern: rd /s /q"),
    (re.compile(r"\bformat\s+[a-z]:", re.IGNORECASE), "blocked pattern: format drive"),
    (re.compile(r"\bmkfs\b", re.IGNORECASE), "blocked pattern: mkfs"),
    (re.compile(r"\bshutdown\b", re.IGNORECASE), "blocked pattern: shutdown"),
    (re.compile(r"\breboot\b", re.IGNORECASE), "blocked pattern: reboot"),
    (re.compile(r"\bdiskpart\b", re.IGNORECASE), "blocked pattern: diskpart"),
    # --- 9 条 jiuwenswarm 系统级 deny(verbatim) ---
    (re.compile(r"(?i)(^|[\s;&|()])((mkfs(\.[A-Za-z0-9_]+)?|mke2fs|fdisk|parted|diskpart|format)\b|(dd\b[^;&|]*(\bof=/dev/|\\\\\.\\PhysicalDrive))|(>\s*/dev/(sd[a-z][0-9]*|vd[a-z][0-9]*|xvd[a-z][0-9]*|nvme[0-9]+n[0-9]+(p[0-9]+)?|disk[0-9]+)))"),
     "system deny: disk partition or raw device write"),
    (re.compile(r"(?i)(^|[\s;&|()])(((curl|wget|fetch|ftp)\b[^;&]*\|\s*(bash|sh|zsh|dash|ash|source)\b)|(iwr|irm|Invoke-WebRequest|Invoke-RestMethod)\b[^;&|]*\|\s*(iex|Invoke-Expression)\b|((bash|sh|zsh|pwsh|powershell)\b[^;&|]*<\s*<\s*\(?\s*(curl|wget)\b))"),
     "system deny: download and execute"),
    (re.compile(r"(?i)(^|[\s;&|()])((base64\s+(-d|--decode)\b[^;&|]*\|\s*(bash|sh|zsh|dash|ash)\b)|(certutil\s+-decode\b)|(-EncodedCommand\b|-[Ee]nc\b)|(\[Convert\]::FromBase64String\()|(eval\s+[`$])|(\b(iex|Invoke-Expression)\b)|((python3?|perl|ruby|node)\s+(-c|-e)\b[^;&|]*(socket|subprocess|exec|eval|child_process)))"),
     "system deny: obfuscated or dynamic execution"),
    (re.compile(r"(?i)(/dev/(tcp|udp)/|(^|[\s;&|()])(nc|ncat)\b[^;&|]*\s(-e|--exec)\s|\bsocat\b[^;&|]*(EXEC:|SYSTEM:|PTY)|\bbash\s+-i\b[^;&|]*/dev/tcp/|\bpython3?\b[^;&|]*(socket|pty\.spawn|subprocess)|\bperl\b[^;&|]*Socket)"),
     "system deny: reverse or bind shell"),
    (re.compile(r"(?i)(:\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:|(^|[\s;&|()])kill\s+-9\s+(-1|1)\b|(^|[\s;&|()])ulimit\s+-u\s+unlimited\b)"),
     "system deny: fork bomb or resource abuse"),
    (re.compile(r"(?i)(^|[\s;&|()])((shutdown|reboot|halt|poweroff)\b|(init|telinit)\s+(0|6)\b)"),
     "system deny: system shutdown or reboot"),
    (re.compile(r"(?i)(Get-StoredCredential|cmdkey\s+/|rundll32\.exe\s+keymgr\.dll|CredRead|CredEnumerate|Advapi32.*Cred|Winlogon|AutoAdminLogon|DefaultPassword)"),
     "system deny: credential access"),
    (re.compile(r"(?i)(SecureStringToBSTR|PtrToStringBSTR|ConvertFrom-SecureString|GetNetworkCredential\(\)\.Password|ProtectedData\]::Unprotect|CryptUnprotectData|\[PSCredential\]::new)"),
     "system deny: credential decrypt"),
    (re.compile(r"(?i)(Export-PfxCertificate|\.PrivateKey|Get-ChildItem\s+Cert:|\[System\.Security\.Cryptography\.X509Certificates\])"),
     "system deny: certificate key access"),
]


def matches(command: str) -> str | None:
    """Return the deny reason if *command* matches any pattern, else None."""
    command = command or ""
    for pattern, reason in COMMAND_DENY_PATTERNS:
        if pattern.search(command):
            return reason
    return None
