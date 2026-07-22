"""调试专用插件目录 —— 仅由 ``qqbot.main_test`` 入口显式加载。

故意不放 ``qqbot/plugins/``：pyproject ``[tool.nonebot]`` 的 ``plugin_dirs``
指向那里，``nb run`` 会把该目录整体加载进生产实例；本目录不在
``plugin_dirs`` 中，生产路径永远不会自动带上这里的任何插件。
"""
