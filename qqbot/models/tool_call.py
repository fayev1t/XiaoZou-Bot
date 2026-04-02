from sqlalchemy import Column, DateTime, Index, String, Text, text

from qqbot.core.time import china_now
from qqbot.models.base import Base


class ToolCallRecord(Base):
    __tablename__ = "tool_call_records"

    call_hash = Column(String(64), primary_key=True)
    msg_hash = Column(String(64), nullable=False)
    tool_name = Column(String(64), nullable=False)
    input_data = Column(Text, nullable=False)
    output_data = Column(Text, nullable=False)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=china_now,
        server_default=text("CURRENT_TIMESTAMP"),
    )

    __table_args__ = (
        Index("idx_tool_call_records_msg_hash", "msg_hash"),
        Index("idx_tool_call_records_lookup", "tool_name", "input_data"),
    )
