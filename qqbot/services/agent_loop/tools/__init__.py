"""V2 内置工具集中注册。

每个文件实现一个 Tool（满足 qqbot.services.agent_loop.tool_registry.Tool
协议，继承 BaseTool 拿默认属性）。`build_default_registry()` 把所有内置
工具无参注册到一个新的 ToolRegistry 实例返回，plugin 启动时调用并注入
LoopSupervisor / LLMPlanner。

工具不再有构造依赖：系统级依赖（session_factory 写/查 agent_events、触发身份
triggered_by_event_id / bot_role 等）一律由 ToolWorker 在 run() 的 context 里
统一注入。这样新增工具只要 register 一行，系统也不必按名字特判任何工具。

napcat 动作工具集（kick / ban / recall / get_* / ...）把 OneBot V11 能对 QQ
做的事进一步工具化，公共出站约定收敛在 `_onebot_common.py`：群操作的
group_id 从 scope_key 注入（隔离契约 §9，不让 LLM 跨群）、经 get_bot() 取
Bot、napcat 动作失败经 call_action 折成 upstream_action_failed **返回**（全程
无 raise）。可见性靠 `allowed_scopes`（catalog 按 scope 过滤）；scope / 发起人
tier（**实时**查群角色）/ bot 自身角色的判定全在工具内 execute() 首行的
enforce_access，AgentLoop 不再闸门。详见各文件 docstring 与
`任务与决策契约.md` §2.2、§7.2。

不复用 v1 qqbot/services/web_search.py 等业务实现 —— v2 工具从零写。
"""

from __future__ import annotations

from qqbot.services.agent_loop.tool_registry import ToolRegistry
from qqbot.services.agent_loop.tools.ban import BanTool
from qqbot.services.agent_loop.tools.emoji_like import EmojiLikeTool
from qqbot.services.agent_loop.tools.get_group_honor import GetGroupHonorTool
from qqbot.services.agent_loop.tools.get_group_info import GetGroupInfoTool
from qqbot.services.agent_loop.tools.get_member_info import GetMemberInfoTool
from qqbot.services.agent_loop.tools.get_member_list import GetMemberListTool
from qqbot.services.agent_loop.tools.get_stranger_info import GetStrangerInfoTool
from qqbot.services.agent_loop.tools.group_notice import GroupNoticeTool
from qqbot.services.agent_loop.tools.kick import KickTool
from qqbot.services.agent_loop.tools.leave_group import LeaveGroupTool
from qqbot.services.agent_loop.tools.poke import PokeTool
from qqbot.services.agent_loop.tools.recall import RecallTool
from qqbot.services.agent_loop.tools.respond_to_group_join_request import (
    RespondToGroupJoinRequestTool,
)
from qqbot.services.agent_loop.tools.save_meme import SaveMemeTool
from qqbot.services.agent_loop.tools.search_history import SearchHistoryTool
from qqbot.services.agent_loop.tools.send_meme import SendMemeTool
from qqbot.services.agent_loop.tools.send_message import SendMessageTool
from qqbot.services.agent_loop.tools.set_admin import SetAdminTool
from qqbot.services.agent_loop.tools.set_card import SetCardTool
from qqbot.services.agent_loop.tools.set_essence import SetEssenceTool
from qqbot.services.agent_loop.tools.set_group_avatar import SetGroupAvatarTool
from qqbot.services.agent_loop.tools.set_group_name import SetGroupNameTool
from qqbot.services.agent_loop.tools.set_title import SetTitleTool
from qqbot.services.agent_loop.tools.wait import WaitTool
from qqbot.services.agent_loop.tools.websearch import WebsearchTool
from qqbot.services.agent_loop.tools.whole_ban import WholeBanTool


def build_default_registry() -> ToolRegistry:
    # 2026-07-01：应用户要求，暂时只保留最基础的 send_message 工具，其余工具整体下架。
    # 原因：现有 napcat 动作 / websearch / search_history 的实现「太粗」，先全部
    # 停用，待逐个重做后再逐一恢复注册。工具类、sibling .md、各自的契约测试都仍
    # 留在仓库里——恢复某个工具时，把它对应的 registry.register(...) 行取消注释
    # 即可，无需改别处。（respond_to_request 已于 2026-07-03 拆分删除，见下。）
    registry = ToolRegistry()
    # ── 基础能力（当前在用）──
    registry.register(SendMessageTool())
    # wait：模型的时间自主权（自我延迟唤醒），2026-07-02 新增。
    registry.register(WaitTool())
    # 入群申请审批（2026-07-03 拆分自已删除的 respond_to_request）：group.add
    # 事件进目标群 timeline，管理员明确授权后由群内 LLM 调它回执；好友申请 /
    # 邀请入群不经工具，由 plugin 层 request_auto_approval 自动同意。
    registry.register(RespondToGroupJoinRequestTool())
    # 表情包收发（2026-07-03 新增）：save_meme 收录 timeline 图片（描述由
    # 工具内 caption LLM 调用生成，见 meme_caption.py），send_meme 按 hash
    # 发送收藏——收藏夹经 <saved-memes> 每 tick 注入 prompt。
    registry.register(SaveMemeTool())
    registry.register(SendMemeTool())
    # ── 以下工具暂时下架（2026-07-01），重做后逐一恢复 ──
    # registry.register(WebsearchTool())
    # registry.register(SearchHistoryTool())
    # napcat 动作工具：消息操作
    # registry.register(RecallTool())
    # registry.register(SetEssenceTool())
    # registry.register(EmojiLikeTool())
    # napcat 动作工具：互动
    # registry.register(PokeTool())
    # registry.register(GroupNoticeTool())
    # napcat 动作工具：群成员管理
    # registry.register(KickTool())
    # registry.register(BanTool())
    # registry.register(SetCardTool())
    # registry.register(SetAdminTool())
    # registry.register(SetTitleTool())
    # napcat 动作工具：群设置 / 退群
    # registry.register(WholeBanTool())
    # registry.register(SetGroupNameTool())
    # registry.register(SetGroupAvatarTool())
    # registry.register(LeaveGroupTool())
    # napcat 动作工具：查询（GUEST，给 LLM 感知能力）
    # registry.register(GetMemberInfoTool())
    # registry.register(GetMemberListTool())
    # registry.register(GetGroupInfoTool())
    # registry.register(GetGroupHonorTool())
    # registry.register(GetStrangerInfoTool())
    return registry


__all__ = [
    "build_default_registry",
    "BanTool",
    "EmojiLikeTool",
    "GetGroupHonorTool",
    "GetGroupInfoTool",
    "GetMemberInfoTool",
    "GetMemberListTool",
    "GetStrangerInfoTool",
    "GroupNoticeTool",
    "KickTool",
    "LeaveGroupTool",
    "PokeTool",
    "RecallTool",
    "RespondToGroupJoinRequestTool",
    "SaveMemeTool",
    "SearchHistoryTool",
    "SendMemeTool",
    "SendMessageTool",
    "SetAdminTool",
    "SetCardTool",
    "SetEssenceTool",
    "SetGroupAvatarTool",
    "SetGroupNameTool",
    "SetTitleTool",
    "WaitTool",
    "WebsearchTool",
    "WholeBanTool",
]
