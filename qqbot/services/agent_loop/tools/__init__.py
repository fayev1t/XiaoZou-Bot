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
from qqbot.services.agent_loop.tools.get_pending_join_requests import (
    GetPendingJoinRequestsTool,
)
from qqbot.services.agent_loop.tools.get_stranger_info import GetStrangerInfoTool
from qqbot.services.agent_loop.tools.group_notice import GroupNoticeTool
from qqbot.services.agent_loop.tools.kick import KickTool
from qqbot.services.agent_loop.tools.leave_group import LeaveGroupTool
from qqbot.services.agent_loop.tools.meme import MemeTool
from qqbot.services.agent_loop.tools.poke import PokeTool
from qqbot.services.agent_loop.tools.recall import RecallTool
from qqbot.services.agent_loop.tools.respond_to_group_join_request import (
    RespondToGroupJoinRequestTool,
)
from qqbot.services.agent_loop.tools.search_history import SearchHistoryTool
from qqbot.services.agent_loop.tools.send_message import SendMessageTool
from qqbot.services.agent_loop.tools.set_admin import SetAdminTool
from qqbot.services.agent_loop.tools.set_card import SetCardTool
from qqbot.services.agent_loop.tools.set_essence import SetEssenceTool
from qqbot.services.agent_loop.tools.set_group_avatar import SetGroupAvatarTool
from qqbot.services.agent_loop.tools.set_group_name import SetGroupNameTool
from qqbot.services.agent_loop.tools.set_title import SetTitleTool
from qqbot.services.agent_loop.tools.wait import WaitTool
from qqbot.services.agent_loop.tools.webfetch import WebfetchTool
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
    # 表情包一站式工具（2026-07-03 收发上线；2026-07-12 合并为单工具并新增
    # 收藏管理）：action 分发 save（收录，描述由工具内 caption LLM 调用生成，
    # 见 meme_caption.py）/ send（按 hash 发送收藏）/ delete（移除收藏，只删
    # 元数据不动磁盘文件）/ recaption（重新生成描述，模型只能换 context_note）。
    # 收藏夹经 <saved-memes> 每 tick 注入 prompt。
    registry.register(MemeTool())
    # ── 群信息查询（2026-07-07 重做后恢复 / 新增）──
    # 查询三件套按下架备注的路线重做后恢复：get_group_info（no_cache + 可选
    # 字段透传）、get_member_list（role 过滤 / include_activity / banned_until）、
    # get_member_info（时间字段 ISO 化 + banned_until）。
    registry.register(GetGroupInfoTool())
    registry.register(GetMemberListTool())
    registry.register(GetMemberInfoTool())
    # 待处理入群申请查询（2026-07-07 新增）：纯 napcat get_group_system_msg
    # 查询、不回查 agent_events；审批仍走 respond_to_group_join_request。
    registry.register(GetPendingJoinRequestsTool())
    # ── 群成员管理（2026-07-10 起重做后逐个恢复）──
    # kick：踢人。通用门禁（发起人 ADMIN 实时核验 + bot 须群管理员）之上，动手前
    # 实时查目标角色做层级前置判定（bot 须严格高于目标）+ 自踢防护；成功结果回显
    # reject_add_request / applied。
    registry.register(KickTool())
    # ── 网页搜索 / 抓取（2026-07-18 重做后恢复 / 新增）──
    # websearch：后端从自部署 SearXNG + Crawl4AI 容器切换为 Tavily API
    # （env TAVILY_API_KEY），正文降级链 raw_content → 进程内抓取；webfetch
    # 同日新增，读取指定 URL 正文，两者共用 _web_common 抓取层。
    registry.register(WebsearchTool())
    registry.register(WebfetchTool())
    # ── 以下工具暂时下架（2026-07-01），重做后逐一恢复 ──
    # registry.register(SearchHistoryTool())
    # napcat 动作工具：消息操作
    # registry.register(RecallTool())
    # registry.register(SetEssenceTool())
    # registry.register(EmojiLikeTool())
    # napcat 动作工具：互动
    # registry.register(PokeTool())
    # registry.register(GroupNoticeTool())
    # napcat 动作工具：群成员管理
    # ——kick 已于 2026-07-10 重做恢复（见上），其余成员管理工具继续停用。
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
    # ——查询三件套已于 2026-07-07 重做恢复（见上），这两个继续停用。
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
    "GetPendingJoinRequestsTool",
    "GetStrangerInfoTool",
    "GroupNoticeTool",
    "KickTool",
    "LeaveGroupTool",
    "MemeTool",
    "PokeTool",
    "RecallTool",
    "RespondToGroupJoinRequestTool",
    "SearchHistoryTool",
    "SendMessageTool",
    "SetAdminTool",
    "SetCardTool",
    "SetEssenceTool",
    "SetGroupAvatarTool",
    "SetGroupNameTool",
    "SetTitleTool",
    "WaitTool",
    "WebfetchTool",
    "WebsearchTool",
    "WholeBanTool",
]
