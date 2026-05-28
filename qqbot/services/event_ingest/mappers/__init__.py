"""Built-in EventMappers — full set for napcat / OneBot V11.

事件清单与映射目标见 开发文档/v2.0/事件系统设计.md §4.1。
heartbeat 不入库（旁路文件原子写，EventIngest契约.md §7.1），不需要 mapper。
"""

from qqbot.services.event_ingest.mapper import MapperRegistry
from qqbot.services.event_ingest.mappers.bot_offline import BotOfflineMapper
from qqbot.services.event_ingest.mappers.emoji_like import EmojiLikeMapper
from qqbot.services.event_ingest.mappers.essence import EssenceMapper
from qqbot.services.event_ingest.mappers.friend_add import FriendAddMapper
from qqbot.services.event_ingest.mappers.friend_recall import FriendRecallMapper
from qqbot.services.event_ingest.mappers.friend_request import FriendRequestMapper
from qqbot.services.event_ingest.mappers.group_admin import GroupAdminMapper
from qqbot.services.event_ingest.mappers.group_ban import GroupBanMapper
from qqbot.services.event_ingest.mappers.group_card import GroupCardMapper
from qqbot.services.event_ingest.mappers.group_decrease import GroupDecreaseMapper
from qqbot.services.event_ingest.mappers.group_increase import GroupIncreaseMapper
from qqbot.services.event_ingest.mappers.group_message import GroupMessageMapper
from qqbot.services.event_ingest.mappers.group_recall import GroupRecallMapper
from qqbot.services.event_ingest.mappers.group_request import GroupRequestMapper
from qqbot.services.event_ingest.mappers.group_upload import GroupUploadMapper
from qqbot.services.event_ingest.mappers.honor import HonorMapper
from qqbot.services.event_ingest.mappers.input_status import InputStatusMapper
from qqbot.services.event_ingest.mappers.lifecycle import LifecycleMapper
from qqbot.services.event_ingest.mappers.lucky_king import LuckyKingMapper
from qqbot.services.event_ingest.mappers.poke import PokeMapper
from qqbot.services.event_ingest.mappers.private_message import PrivateMessageMapper

__all__ = [
    "GroupMessageMapper",
    "PrivateMessageMapper",
    "GroupRecallMapper",
    "GroupIncreaseMapper",
    "GroupDecreaseMapper",
    "GroupAdminMapper",
    "GroupBanMapper",
    "GroupUploadMapper",
    "PokeMapper",
    "LuckyKingMapper",
    "HonorMapper",
    "EssenceMapper",
    "EmojiLikeMapper",
    "GroupCardMapper",
    "FriendRecallMapper",
    "FriendAddMapper",
    "InputStatusMapper",
    "BotOfflineMapper",
    "FriendRequestMapper",
    "GroupRequestMapper",
    "LifecycleMapper",
    "build_default_registry",
]


def build_default_registry() -> MapperRegistry:
    """Wire up every shipped mapper. Order is irrelevant for correctness
    (the registry picks the matching mapper by `can_map`), but listing
    high-volume mappers first marginally speeds up registry.find().
    """
    registry = MapperRegistry()
    # 高频
    registry.register(GroupMessageMapper())
    registry.register(PrivateMessageMapper())
    # 群通知
    registry.register(GroupRecallMapper())
    registry.register(GroupIncreaseMapper())
    registry.register(GroupDecreaseMapper())
    registry.register(GroupAdminMapper())
    registry.register(GroupBanMapper())
    registry.register(GroupUploadMapper())
    registry.register(PokeMapper())
    registry.register(LuckyKingMapper())
    registry.register(HonorMapper())
    # napcat 扩展
    registry.register(EssenceMapper())
    registry.register(EmojiLikeMapper())
    registry.register(GroupCardMapper())
    registry.register(InputStatusMapper())
    registry.register(BotOfflineMapper())
    # 私聊通知
    registry.register(FriendRecallMapper())
    registry.register(FriendAddMapper())
    # 请求
    registry.register(FriendRequestMapper())
    registry.register(GroupRequestMapper())
    # 元事件
    registry.register(LifecycleMapper())
    return registry
