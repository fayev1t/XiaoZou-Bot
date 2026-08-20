"""raw_events —— 原文绕库一圈。

憨写，不记状态。插入成功后由入口网关广播内存里的对象。
满 100 行直接清除表格。运行路径不 SELECT 这张表驱动控制流。
"""

from sqlalchemy import Column, DateTime, Text
from sqlalchemy.dialects.postgresql import JSONB

from qqbot.models.base import Base


class RawEvent(Base):
    __tablename__ = "raw_events"

    raw_id = Column(Text, primary_key=True)
    # 谁丢进来的。现役只写 external（NapCat）。model / tool / system 预留。
    channel = Column(Text, nullable=False)
    received_at = Column(DateTime(timezone=True), nullable=False)
    raw_payload = Column(JSONB, nullable=False)

    def __repr__(self) -> str:
        return f"<RawEvent({self.raw_id} channel={self.channel})>"
