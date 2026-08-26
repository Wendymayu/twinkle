"""Workspace 引导 —— ensure_workspace_dir + 播种示例 skill。

从 config.py 移出，使 config 保持纯常量；这是唯一的运行时副作用模块
（在 server 启动时调用，绝不在 import 时调用——使通过 env +
importlib.reload(twinkle.config) 重指 WORKSPACE_DIR 的测试对 host 无副作用）。

调用时从 ``twinkle.config`` 动态读取 WORKSPACE_DIR / SKILLS_DIR（非顶层
快照），使重载 twinkle.config 把 workspace 指向它处的测试能看到新值——
这镜像旧版 in-config 的 ensure_workspace_dir 在 reload 后读自身模块全局
的做法。随包资源路径从本文件位置解析（twinkle/workspace.py 直接位于
twinkle/ 下，与旧 config.py 相同，故 `resources/skills` 解析一致）。
"""
import os
import shutil
from pathlib import Path

from twinkle import config as _cfg


def ensure_workspace_dir() -> str:
    """缺失时创建 WORKSPACE_DIR + SKILLS_DIR（幂等），首次启动播种示例 skill。
    在 server 启动时调用，使 read/list/glob 在全新 ~/.twinkle 上工作而不报
    "not found"。不在 import 时调用，以使（重指 WORKSPACE_DIR 的）测试对
    host 无副作用。
    """
    os.makedirs(_cfg.WORKSPACE_DIR, exist_ok=True)
    os.makedirs(_cfg.SKILLS_DIR, exist_ok=True)
    workflows_dir = os.path.join(_cfg.WORKSPACE_DIR, "workflows")
    os.makedirs(workflows_dir, exist_ok=True)
    os.makedirs(_cfg.MEMORY_DIR, exist_ok=True)
    os.makedirs(os.path.join(_cfg.MEMORY_DIR, "daily_memory"), exist_ok=True)
    _seed_example_skills(_cfg.SKILLS_DIR)
    _seed_bundled_workflows(workflows_dir)
    return _cfg.WORKSPACE_DIR


def _seed_example_skills(skills_dir: str) -> None:
    """首次启动：把随包示例 skill（twinkle/resources/skills/*）复制到
    <WORKSPACE>/skills。目标已存在则跳过（保留用户改动）。无随包资源时
    no-op。"""
    src = Path(__file__).resolve().parent / "resources" / "skills"
    if not src.is_dir():
        return
    for skill_dir in src.iterdir():
        if not skill_dir.is_dir() or not (skill_dir / "SKILL.md").is_file():
            continue
        dst = Path(skills_dir) / skill_dir.name
        if dst.exists():
            continue  # 用户已有(可能改过),不覆盖
        shutil.copytree(skill_dir, dst)


def _seed_bundled_workflows(workflows_dir: str) -> None:
    """首次启动：把随包 workflow（twinkle/agentserver/workflow/<name>/root.py）
    复制到 <WORKSPACE>/workflows/<name>/。目标已存在则跳过（保留用户改动）。
    随包 workflow 是 engine 包中含 root.py 的子目录；这把随包 workflow 保持在
    twinkle 包内（更接近打包安装）而非散落的仓库根目录 workflow/ dir。
    镜像 _seed_example_skills。"""
    src_root = Path(__file__).resolve().parent / "agentserver" / "workflow"
    if not src_root.is_dir():
        return
    for wf_dir in src_root.iterdir():
        if not wf_dir.is_dir() or not (wf_dir / "root.py").is_file():
            continue
        dst = Path(workflows_dir) / wf_dir.name
        if dst.exists():
            continue  # 用户已有(可能改过),不覆盖
        shutil.copytree(wf_dir, dst)
