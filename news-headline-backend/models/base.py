"""ORM 基类和可复用字段定义。"""

from datetime import datetime

from sqlalchemy import DateTime
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """所有 ORM 模型共用同一份 MetaData，确保跨模块外键可以解析。"""


class TimestampMixin:
    """为原有带审计时间的表保留创建和更新时间字段。"""

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, onupdate=datetime.now, nullable=False
    )
