"""V2 内置工具集中注册。

每个文件实现一个 Tool（满足 qqbot.services.agent_loop.tool_registry.Tool
协议，继承 BaseTool 拿默认属性）。`build_default_registry()` 把所有内置
工具无参注册到一个新的 ToolRegistry 实例返回，plugin 启动时调用并注入
LoopSupervisor / LLMPlanner。

工具不再有构造依赖：系统级依赖（session_factory 写/查 agent_events、触发身份
triggered_by_event_id / bot_role 等）一律由执行层在 run() 的 context 里统一
注入；worker 工具由 ToolWorker 执行，inline 工具由 AgentLoop 当前 tick 执行。
这样新增工具只要 register 一行，系统也不必按名字特判任何工具。

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
from qqbot.services.agent_loop.tools.look_at_image import LookAtImageTool
from qqbot.services.agent_loop.tools.meme_collection import MemeCollectionTool
from qqbot.services.agent_loop.tools.poke import PokeTool
from qqbot.services.agent_loop.tools.recall import RecallTool
from qqbot.services.agent_loop.tools.reply import ReplyTool
from qqbot.services.agent_loop.tools.respond_to_group_join_request import (
    RespondToGroupJoinRequestTool,
)
from qqbot.services.agent_loop.tools.search_history import SearchHistoryTool
from qqbot.services.agent_loop.tools.send_messages import SendMessagesTool
from qqbot.services.agent_loop.tools.set_admin import SetAdminTool
from qqbot.services.agent_loop.tools.set_card import SetCardTool
from qqbot.services.agent_loop.tools.set_essence import SetEssenceTool
from qqbot.services.agent_loop.tools.set_group_avatar import SetGroupAvatarTool
from qqbot.services.agent_loop.tools.set_group_name import SetGroupNameTool
from qqbot.services.agent_loop.tools.set_title import SetTitleTool
from qqbot.services.agent_loop.tools.task import TaskTool
from qqbot.services.agent_loop.tools.wait import WaitTool
from qqbot.services.agent_loop.tools.webfetch import WebfetchTool
from qqbot.services.agent_loop.tools.websearch import WebsearchTool
from qqbot.services.agent_loop.tools.whole_ban import WholeBanTool


def build_default_registry() -> ToolRegistry:
    # 2026-07-19：reply 已取代 send_message，落 reply_task 后由独立执行器发送。
    # 原因：现有 napcat 动作 / websearch / search_history 的实现「太粗」，先全部
    # 停用，待逐个重做后再逐一恢复注册。工具类、sibling .md、各自的契约测试都仍
    # 留在仓库里——恢复某个工具时，把它对应的 registry.register(...) 行取消注释
    # 即可，无需改别处。（respond_to_request 已于 2026-07-03 拆分删除，见下。）
    registry = ToolRegistry()
    # ── 基础能力（当前在用）──
    # task：跨 tick 任务生命周期。它是 execution_mode="inline" 的普通工具，
    # AgentLoop 在当前拍 await 并原子写 called + task 事件 + terminal；create
    # 结果里的 task_ref 可供同拍后续 call_tool 解析。
    registry.register(TaskTool())
    # reply：发言的第一步——纯粹起一段等待（"我在输入，字还没发出去"）。
    # 2026-08-01 删除内容参数后普通分支只剩 hold_seconds；撤稿留在 action 分支
    # 里（曾评估拆成独立工具，因每个工具都要占一条 catalog 条目 + 整段 usage
    # 文档而否决）。
    registry.register(ReplyTool())
    # send_messages：发言的第二步（2026-07-31 删除 Replyer）——等待到点、
    # runtime.reply_task_completed 唤醒后，Planner 用它把最终措辞真正发出去。
    # 普通 worker 工具：始终可调（"完成事件之后才发言"是提示词纪律，不是
    # 权限），发送事实与时间线记录 = 它自己的 tool terminal receipts（调用行
    # 即发言记录，不派生 <my-reply>），见 send_messages.py docstring。
    registry.register(SendMessagesTool())
    # wait：模型的时间自主权（自我延迟唤醒），2026-07-02 新增。
    registry.register(WaitTool())
    # 入群申请审批（2026-07-03 拆分自已删除的 respond_to_request）：group.add
    # 事件进目标群 timeline，管理员明确授权后由群内 LLM 调它回执；好友申请 /
    # 邀请入群不经工具，由 plugin 层 request_auto_approval 自动同意。
    registry.register(RespondToGroupJoinRequestTool())
    # 表情包收藏管理：save（描述由 caption LLM 生成）/ delete / recaption。
    # 发送入口已并入 reply_task；Replyer 从 <saved-memes> 决定 0..1 张。
    # 2026-07-25 由 `meme` 改名 `meme_collection`：裸名词 `meme` 读起来像
    # "表情包能力"，而发送 2026-07-19 就不在它参数面上了，新名点明操作对象是
    # 收藏夹本身（历史事件的旧 tool_name 原样保留，投影 author index 仍认）。
    registry.register(MemeCollectionTool())
    # look_at_image：带着具体问题重看一张图（2026-07-28 新增）。同日 Planner/
    # Replyer 降级为纯文本模型、图片改由 ingest 期 VLM 转录成 desc= 进 timeline，
    # 本工具是那条无语境描述覆盖不到时的兜底 —— 没有它这次改动就是纯降级。
    # 明知每个注册工具都要占一条 catalog 条目 + 整段 usage 文档仍然收下它，
    # 理由就是这个能力天花板（对比 reply 拒绝拆分的取舍，见 reply.py）。
    registry.register(LookAtImageTool())
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
    # leave_group：极端定向人格侮辱 / 恶意辱骂下的自主安全出口。只退出当前群，
    # 永远固定 is_dismiss=false，不暴露解散能力；这不是辱骂者授权的群管操作，
    # 所以 GUEST 可触发，由 sibling usage 文档给 Planner 严格限定语义门槛。
    registry.register(LeaveGroupTool())
    # ── 网页搜索 / 抓取（2026-07-18 重做后恢复 / 新增）──
    # websearch：后端从自部署 SearXNG + Crawl4AI 容器切换为 Tavily API
    # （env TAVILY_API_KEY），正文降级链 raw_content → 进程内抓取；webfetch
    # 同日新增，读取指定 URL 正文，两者共用 _web_common 抓取层。
    registry.register(WebsearchTool())
    registry.register(WebfetchTool())
    # ── 历史检索（2026-07-23 重做后恢复）──
    # search_history：timeline 100 条渲染上限之外的按需检索。重做修了两处：
    # query 过滤改走 pg_trgm word_similarity（`<%` 算子）对 search_text 做模糊
    # 相似匹配（原 ILIKE 打在 payload->>'raw_message' 上，索引白建、且要求
    # LLM 猜中原话子串）；private scope 补齐按 user_id 过滤（此前只有 group
    # 分支按 group_id 过滤，private 分支不设防——因 PrivateAgentLoop 从未
    # 实例化而未被线上触发）。
    registry.register(SearchHistoryTool())
    # ── 以下工具暂时下架（2026-07-01），重做后逐一恢复 ──
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
    "LookAtImageTool",
    "MemeCollectionTool",
    "PokeTool",
    "RecallTool",
    "RespondToGroupJoinRequestTool",
    "SearchHistoryTool",
    "ReplyTool",
    "SendMessagesTool",
    "SetAdminTool",
    "SetCardTool",
    "SetEssenceTool",
    "SetGroupAvatarTool",
    "SetGroupNameTool",
    "SetTitleTool",
    "TaskTool",
    "WaitTool",
    "WebfetchTool",
    "WebsearchTool",
    "WholeBanTool",
]
