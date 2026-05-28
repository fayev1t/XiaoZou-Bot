"""Services for QQ Bot (v2).

v2 子包：
- qqbot.services.event_ingest  napcat 事件入口流水线
- qqbot.services.agent_loop    决策循环 + workers + 工具

显式 import 通过子包路径完成；本 __init__ 不再 re-export。
"""
