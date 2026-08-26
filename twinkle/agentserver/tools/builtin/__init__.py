"""具体 tool 实现,与框架层分开存放。

存放叶子工具(web / shell / todo)。框架层 —— ``Tool`` /
``ToolCard`` / ``LocalFunction`` / ``@tool`` / ``ToolManager`` —— 留在
父级 :mod:`twinkle.agentserver.tools` 层。在此处新增一个 tool 时,写成一个
``*_tools.py`` 模块,然后在 :func:`twinkle.agentserver.tools.tool_manager` 中注册。

对照 openjiuwen 把 ``core/foundation/tool/``(引擎)与应用各领域 tool 文件
分开的做法,但故意不采用 jiuwenswarm 的 ``@harness_element`` catalog + provider
间接层 —— 注册仍是单跳 ``ToolManager.register()``。

本包有意不 re-export 任何符号:tool 单例保持模块属性访问
(``web_fetch.web_fetch``),以便测试能 monkeypatch 内部 helper —— 与搬迁前
相同的约定;只改了 import 路径。
"""
