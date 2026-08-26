"""Config loader：读 YAML -> 解析 -> 对值解析 ${ENV:-default} -> 校验。

解析优先（非文本优先）：先 yaml.safe_load 原始文本，再遍历已解析的数据，
对字符串值解析 ${ENV:-default}。这镜像 jiuwenswarm 的 common/config.py，
并避开文本优先的坑：未加引号的空默认值 `dir: ${VAR:-}` 解析成 `dir: `
会被 YAML 当成 null（导致 pydantic str 字段失败）。解析优先下，未加引号的
`dir: ${VAR:-}` 被解析为纯标量字符串 "${VAR:-}"，再解析为 ""——不会
出 null，也无需加引号。

${VAR:-default} 语义：非空真实环境变量优先；空/未设环境变量回退到默认值；
无默认值且无环境变量的 ${VAR} 得到 ""。

_load_env_file() 先从仓库根目录的 .env 填充 os.environ（真实环境变量仍
通过 setdefault 优先），故 ${TWINKLE_LLM_API_KEY} 可从 .env 解析。

路径说明：本模块位于 twinkle/config/loader.py，故
  Path(__file__).parent.parent       -> twinkle/   （resources/config.yaml 所在）
  Path(__file__).parent.parent.parent -> 仓库根目录（.env 所在）
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

from .schema import TwinkleConfig

# Config YAML 作为包数据文件随 twinkle/resources/ 发布（在本包上一级）。
CONFIG_YAML_PATH = Path(__file__).resolve().parent.parent / "resources" / "config.yaml"

_ENV_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)(?::-([^}]*))?\}")


def _load_env_file() -> None:
    """从仓库根目录的 .env 填充 os.environ。

    真实环境变量优先（setdefault），故 .env 是便利默认值而非覆盖。
    逐字镜像原始 twinkle/config.py 解析器。
    """
    env_path = Path(__file__).resolve().parent.parent.parent / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


def _resolve_env_vars(text: str) -> str:
    """用 os.environ 替换 ${VAR:-default} / ${VAR}（空环境变量 -> 默认值）。"""

    def _replace(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(2)
        val = os.environ.get(name)
        if val:  # 非空真实环境变量优先；空值穿透
            return val
        return default if default is not None else ""

    return _ENV_RE.sub(_replace, text)


def _resolve_env_vars_in_data(obj):
    """递归解析已解析 YAML 结构（dict/list/scalar）中每个字符串值里的 ${ENV:-default}。
    非字符串叶子原样返回。dict 的键不解析（键里放环境变量不是此处的用例）。"""
    if isinstance(obj, str):
        return _resolve_env_vars(obj)
    if isinstance(obj, dict):
        return {k: _resolve_env_vars_in_data(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve_env_vars_in_data(v) for v in obj]
    return obj


def load_config(config_path: str | Path | None = None) -> TwinkleConfig:
    """读取 config_path 处的 YAML（默认：随包发布的 resources/config.yaml），
    解析它，解析字符串值里的 ${ENV:-default}，并校验为 TwinkleConfig。
    缺失文件或非法 config（错误的 tier/mode -> pydantic ValidationError）时抛错。"""
    _load_env_file()  # 以便 ${TWINKLE_LLM_API_KEY} 等从 .env 解析
    path = Path(config_path) if config_path else CONFIG_YAML_PATH
    raw = path.read_text(encoding="utf-8")
    data = yaml.safe_load(raw) or {}
    data = _resolve_env_vars_in_data(data)
    return TwinkleConfig(**data)
