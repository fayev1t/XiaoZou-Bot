"""API Lab 入口 —— 观测 napcat/OneBot 真实响应结构（待办 #7，调试专用）。

与生产入口的关系：**替代运行，绝不共存**。生产路径 = ``nb run``（nb-cli 读
pyproject ``[tool.nonebot]`` 注册 OneBot V11 适配器并整目录加载
``qqbot/plugins/``）；本入口 = ``python -m qqbot.main_test``（自己
``register_adapter``，不依赖 nb-cli），只加载 ``qqbot.plugins_test.api_lab``
一个调试插件：

- 不加载 startup / v2_main —— 无 DB、无 EventIngest、无 AgentLoop、无 LLM：
  实验动作零入库，bot 不会自主回应任何消息；
- napcat 反向 WS 指向 .env 的同一端口（``PORT``）：**先停生产实例**再启动
  本入口，napcat 掉线后会自动重连到实验台；测完停掉、重启生产即可；
- 一切入站事件与 API 调用的完整出入参：loguru 日志 + ``runtime_data/api_lab/``
  按天 JSONL（SFTP 工作区可直接读）。命令用法见 api_lab 模块 docstring。
"""

import nonebot
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter

from qqbot.core.logging import logger

logger.info("🧪 Starting API LAB (qqbot.main_test) — no DB / no agent loop")

nonebot.init()

# 生产靠 nb run 读 pyproject [tool.nonebot] 注册适配器；本入口独立于 nb-cli，
# 必须自己注册，否则 napcat 的反向 WS 连不进来。
nonebot.get_driver().register_adapter(OneBotV11Adapter)

nonebot.load_plugin("qqbot.plugins_test.api_lab")

if __name__ == "__main__":
    nonebot.run()
