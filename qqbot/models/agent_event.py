"""统一事件流单表 (agent_events) ORM model.

契约源头：开发文档/v2.0/事件系统设计.md §3
入站层：开发文档/v2.0/EventIngest契约.md §4

约束摘要：
- 单表 append-only，禁止 UPDATE / DELETE。
- 三维度分类：origin (external/agent/runtime) × scope (system/group/private) × visibility。
- 因果链双字段：correlation_id（同一 tick 共享）+ causation_id（直接前因）。
- idempotency_key 仅 origin=external 非空，用于 napcat 报文去重。
- search_text 是 STORED GENERATED 列，从 payload->>'raw_message' 抽出，
  供 search_history 工具关键字检索；配合 pg_trgm GIN 索引接近 O(1)。
"""

from sqlalchemy import BigInteger, Column, Computed, DateTime, Index, Text
from sqlalchemy.dialects.postgresql import JSONB

from qqbot.models.base import Base


class AgentEvent(Base):
    __tablename__ = "agent_events"

    event_id = Column(Text, primary_key=True)
    occurred_at = Column(DateTime(timezone=True), nullable=False)
    origin = Column(Text, nullable=False)
    type = Column(Text, nullable=False)
    scope = Column(Text, nullable=False)
    group_id = Column(BigInteger, nullable=True)
    user_id = Column(BigInteger, nullable=True)
    visibility = Column(Text, nullable=False)
    correlation_id = Column(Text, nullable=True)
    causation_id = Column(Text, nullable=True)
    idempotency_key = Column(Text, nullable=True, unique=True)
    payload = Column(JSONB, nullable=False)
    raw = Column(JSONB, nullable=True)
    # STORED GENERATED：每行物化一次抽取结果，给 GIN trgm 索引拍扁到 text。
    # JSONB 表达式 `payload->>'raw_message'` 对消息事件天然有值；对 agent.*
    # / runtime.* 等没有 raw_message 字段的事件值为 NULL，查询自动跳过。
    search_text = Column(
        Text,
        Computed("payload->>'raw_message'", persisted=True),
        nullable=True,
    )

    __table_args__ = (
        Index("agent_events_scope_time_idx", "scope", "group_id", "occurred_at"),
        Index("agent_events_corr_idx", "correlation_id"),
        Index("agent_events_caus_idx", "causation_id"),
        Index("agent_events_type_time_idx", "type", "occurred_at"),
        # pg_trgm GIN 索引：搜 `search_text ILIKE '%...%'` 走索引。
        # 需要数据库已 CREATE EXTENSION pg_trgm —— init_db 负责保证。
        Index(
            "agent_events_search_trgm_idx",
            "search_text",
            postgresql_using="gin",
            postgresql_ops={"search_text": "gin_trgm_ops"},
        ),
    )

    def __repr__(self) -> str:
        return f"<AgentEvent({self.event_id} {self.type} scope={self.scope})>"
