from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base, TimestampMixin


class Category(TimestampMixin, Base):
    __tablename__ = "news_category"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    def __repr__(self):
        return f"<Category(id={self.id}, name={self.name}, sort_order={self.sort_order})>"


class News(TimestampMixin, Base):
    __tablename__ = "news"
    __table_args__ = (
        Index("fk_news_category_idx", "category_id"),
        Index("idx_publish_time", "publish_time"),
        Index("idx_news_status_publish_time", "status", "publish_time"),
        Index("idx_news_auto_review", "status", "feed_source_id", "created_at"),
        Index("uq_news_feed_external_id", "feed_source_id", "external_id", unique=True),
        Index("uq_news_content_hash", "content_hash", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String(500))
    content: Mapped[str] = mapped_column(Text, nullable=False)
    image: Mapped[Optional[str]] = mapped_column(String(255))
    author: Mapped[Optional[str]] = mapped_column(String(50))
    category_id: Mapped[int] = mapped_column(Integer, ForeignKey("news_category.id"), nullable=False)
    views: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    publish_time: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    status: Mapped[str] = mapped_column(
        String(20), default="published", server_default="published", nullable=False
    )
    feed_source_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("news_feed_source.id", ondelete="SET NULL"), nullable=True
    )
    external_id: Mapped[Optional[str]] = mapped_column(String(191), nullable=True)
    content_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    reviewed_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    source_url: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)

    def __repr__(self):
        return f"<News(id={self.id}, title='{self.title}', status='{self.status}')>"


class NewsFeedSource(TimestampMixin, Base):
    __tablename__ = "news_feed_source"
    __table_args__ = (Index("idx_news_feed_source_active", "is_active"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    url: Mapped[str] = mapped_column(String(1000), nullable=False)
    category_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("news_category.id", ondelete="RESTRICT"), nullable=False
    )
    # 管理员登记来源谱系；publisher_group 用于识别同一稿源的转载与重复报道。
    source_type: Mapped[str] = mapped_column(String(32), default="other", server_default="other", nullable=False)
    publisher_group: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    is_active: Mapped[bool] = mapped_column(default=True, server_default="1", nullable=False)
    auto_collect_enabled: Mapped[bool] = mapped_column(default=False, server_default="0", nullable=False)
    auto_collect_interval_minutes: Mapped[int] = mapped_column(default=60, server_default="60", nullable=False)
    trusted_for_auto_publish: Mapped[bool] = mapped_column(default=False, server_default="0", nullable=False)
    auto_publish_enabled_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_tested_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_test_error: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    last_fetched_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    collection_failures: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    next_retry_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    def __repr__(self):
        return f"<NewsFeedSource(id={self.id}, name='{self.name}', is_active={self.is_active})>"


class AutoPublishCategory(TimestampMixin, Base):
    """管理员明确允许自动发布的分类名单。"""

    __tablename__ = "news_auto_publish_categories"

    category_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("news_category.id", ondelete="CASCADE"), primary_key=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1", nullable=False)
    enabled_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)


class AutoReviewLog(TimestampMixin, Base):
    """自动审核决策审计；新闻记录被删除后仍保留标题快照。"""

    __tablename__ = "news_auto_review_logs"
    __table_args__ = (
        Index("idx_auto_review_decision_attempt", "decision", "last_attempt_at"),
        Index("idx_auto_review_published_at", "published_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    news_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("news.id", ondelete="SET NULL"), unique=True, nullable=True
    )
    source_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    source_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    category_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    category_name: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    title_snapshot: Mapped[str] = mapped_column(String(255), nullable=False)
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    reasons_json: Mapped[str] = mapped_column(Text, nullable=False)
    review_result_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    last_attempt_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    withdrawn_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    withdrawn_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    withdraw_reason: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)


class NewsReviewFeedback(TimestampMixin, Base):
    """保存编辑对 Agent 审核的标注，供回归评估和人工复核追溯。"""

    __tablename__ = "news_review_feedback"
    __table_args__ = (
        Index("uq_news_review_feedback_log_reviewer", "review_log_id", "reviewer_id", unique=True),
        Index("idx_news_review_feedback_label", "label", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    review_log_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("news_auto_review_logs.id", ondelete="CASCADE"), nullable=False
    )
    reviewer_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("user.id", ondelete="CASCADE"), nullable=False
    )
    label: Mapped[str] = mapped_column(String(20), nullable=False)
    safe_to_publish: Mapped[bool] = mapped_column(Boolean, nullable=False)
    evidence_accurate: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)


class NewsVectorIndexState(TimestampMixin, Base):
    """MySQL 中的向量索引待办记录，用于重试并修复 MySQL/Qdrant 的最终一致性。"""

    __tablename__ = "news_vector_index_state"

    # 此表刻意不设置新闻外键：新闻删除后仍需保留待办，才能清理 Qdrant 中的旧向量。
    news_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    content_fingerprint: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    embedding_version: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    source_updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_retry_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    indexed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
