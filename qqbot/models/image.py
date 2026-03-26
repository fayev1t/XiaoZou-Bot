"""Image record model for multimodal memory."""

from sqlalchemy import Column, DateTime, Integer, String, Text

from qqbot.core.time import china_now
from qqbot.models.base import Base


class ImageRecord(Base):
    """图片记录表 (image_records)

    全局唯一表，按图片的 file_hash 存储，避免相同图片重复解析
    """

    __tablename__ = "image_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    file_hash = Column(String(255), unique=True, nullable=False, index=True)
    url = Column(Text, nullable=True)  # 最新的下载URL（可能过期，仅供临时使用）
    local_path = Column(Text, nullable=True)
    description = Column(Text, nullable=True)  # GPT 视觉解析出的群聊图片描述
    created_at = Column(DateTime, nullable=False, default=china_now)
    updated_at = Column(DateTime, nullable=False, default=china_now, onupdate=china_now)

    def __repr__(self) -> str:
        return f"<ImageRecord(hash={self.file_hash}, has_desc={bool(self.description)})>"
