"""group_memories —— 一群一行的记忆正文，UPDATE 覆盖。"""

from sqlalchemy import BigInteger, Column, DateTime, Text

from qqbot.models.base import Base


class GroupMemory(Base):
    __tablename__ = "group_memories"

    group_id = Column(BigInteger, primary_key=True)
    content = Column(Text, nullable=False, default="")
    updated_at = Column(DateTime(timezone=True), nullable=False)

    def __repr__(self) -> str:
        return f"<GroupMemory(group_id={self.group_id})>"
