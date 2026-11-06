"""采集编排、来源评分和事件跟踪使用的持久化模型。"""

from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base, TimestampMixin
# 导入相关表模型，确保单独加载本模块时字符串外键也能解析。
from models.news import News, NewsFeedSource


class CollectionJob(TimestampMixin, Base):
    """记录一次采集任务的状态、重试计划和安全错误码。"""

    __tablename__ = "news_collection_jobs"
    __table_args__ = (
        Index("idx_collection_job_status_retry", "status", "next_retry_at"),
        Index("idx_collection_job_source_created", "source_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("news_feed_source.id", ondelete="CASCADE"), nullable=False
    )
    trigger: Mapped[str] = mapped_column(String(20), default="scheduled", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="queued", nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    next_retry_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    error_code: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    result_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    requested_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)


class SourceQualityScore(TimestampMixin, Base):
    """保存来源质量评分快照，供自动发布门禁和管理员查看。"""

    __tablename__ = "news_source_quality_scores"

    source_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("news_feed_source.id", ondelete="CASCADE"), primary_key=True
    )
    score: Mapped[float] = mapped_column(Float, nullable=False)
    tier: Mapped[str] = mapped_column(String(20), nullable=False)
    metrics_json: Mapped[str] = mapped_column(Text, nullable=False)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class NewsEvent(TimestampMixin, Base):
    """将同一事件的多篇报道归组，并记录最新报道。"""

    __tablename__ = "news_events"
    __table_args__ = (Index("idx_news_event_review", "needs_review", "last_seen_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    canonical_title: Mapped[str] = mapped_column(String(255), nullable=False)
    latest_news_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("news.id", ondelete="SET NULL"), nullable=True
    )
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    update_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    correction_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class NewsEventItem(TimestampMixin, Base):
    """事件与新闻的关联，保存匹配关系和相似度，便于解释分组结果。"""

    __tablename__ = "news_event_items"
    __table_args__ = (
        Index("uq_news_event_item_news", "news_id", unique=True),
        Index("idx_news_event_item_event", "event_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("news_events.id", ondelete="CASCADE"), nullable=False
    )
    news_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("news.id", ondelete="CASCADE"), nullable=False
    )
    relation_type: Mapped[str] = mapped_column(String(20), default="related", nullable=False)
    similarity: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)


class NewsEventAlert(TimestampMixin, Base):
    """事件出现官方更新或疑似更正时生成的人工复核告警。"""

    __tablename__ = "news_event_alerts"
    __table_args__ = (Index("idx_news_event_alert_status", "status", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("news_events.id", ondelete="CASCADE"), nullable=False
    )
    news_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("news.id", ondelete="SET NULL"), nullable=True
    )
    alert_type: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    summary: Mapped[str] = mapped_column(String(500), nullable=False)
    evidence_json: Mapped[str] = mapped_column(Text, nullable=False)
    resolved_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
