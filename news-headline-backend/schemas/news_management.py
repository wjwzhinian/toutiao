from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


SourceType = Literal["official_original", "independent_media", "syndicated", "other"]


class FeedSourceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    url: str = Field(min_length=1, max_length=1000)
    category_id: int = Field(alias="categoryId", gt=0)
    source_type: SourceType = Field(default="other", alias="sourceType")
    publisher_group: str | None = Field(default=None, alias="publisherGroup", max_length=120)
    # 新来源默认停用，管理员先测试和审核 RSS，再显式启用定时任务。
    is_active: bool = Field(default=False, alias="isActive")
    auto_collect_enabled: bool = Field(default=False, alias="autoCollectEnabled")
    auto_collect_interval_minutes: int = Field(
        default=60, alias="autoCollectIntervalMinutes", ge=5, le=1440
    )
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    @field_validator("name")
    @classmethod
    def non_empty_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("name cannot be blank")
        return value

    @field_validator("url")
    @classmethod
    def valid_web_url(cls, value: str) -> str:
        value = value.strip()
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
            raise ValueError("url must be an HTTP(S) URL without embedded credentials")
        return value

    @field_validator("publisher_group")
    @classmethod
    def normalize_publisher_group(cls, value: str | None) -> str | None:
        value = value.strip().casefold() if value else None
        return value or None


class FeedSourceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    url: str | None = Field(default=None, min_length=1, max_length=1000)
    category_id: int | None = Field(default=None, alias="categoryId", gt=0)
    source_type: SourceType | None = Field(default=None, alias="sourceType")
    publisher_group: str | None = Field(default=None, alias="publisherGroup", max_length=120)
    is_active: bool | None = Field(default=None, alias="isActive")
    auto_collect_enabled: bool | None = Field(default=None, alias="autoCollectEnabled")
    auto_collect_interval_minutes: int | None = Field(
        default=None, alias="autoCollectIntervalMinutes", ge=5, le=1440
    )
    trusted_for_auto_publish: bool | None = Field(default=None, alias="trustedForAutoPublish")
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    @field_validator("name")
    @classmethod
    def non_empty_name(cls, value: str | None) -> str | None:
        if value is None:
            return value
        value = value.strip()
        if not value:
            raise ValueError("name cannot be blank")
        return value

    @field_validator("url")
    @classmethod
    def valid_web_url(cls, value: str | None) -> str | None:
        if value is None:
            return value
        value = value.strip()
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
            raise ValueError("url must be an HTTP(S) URL without embedded credentials")
        return value

    @field_validator("publisher_group")
    @classmethod
    def normalize_publisher_group(cls, value: str | None) -> str | None:
        value = value.strip().casefold() if value else None
        return value or None


class NewsContentUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=500)
    content: str | None = Field(default=None, min_length=1)
    image: str | None = Field(default=None, max_length=255)
    author: str | None = Field(default=None, max_length=50)
    category_id: int | None = Field(default=None, alias="categoryId", gt=0)
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    @field_validator("title", "content")
    @classmethod
    def non_empty_text(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("text cannot be blank")
        return value.strip() if value is not None else value


class NewsContentCreate(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=500)
    content: str = Field(min_length=1)
    image: str | None = Field(default=None, max_length=255)
    author: str | None = Field(default=None, max_length=50)
    category_id: int = Field(alias="categoryId", gt=0)
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    @field_validator("title", "content")
    @classmethod
    def non_empty_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("text cannot be blank")
        return value


class NewsStatusUpdate(BaseModel):
    status: Literal["draft", "published", "archived"]


class NewsReviewAssistRequest(BaseModel):
    # 审核接口接受有限长度的稿件文本，避免超大请求直接进入模型调用。
    news_id: int | None = Field(default=None, alias="newsId", gt=0)
    title: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=500)
    content: str = Field(min_length=1, max_length=30_000)
    category_id: int | None = Field(default=None, alias="categoryId", gt=0)
    source_url: str | None = Field(default=None, alias="sourceUrl", max_length=1000)
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    @field_validator("title", "content")
    @classmethod
    def non_empty_review_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("text cannot be blank")
        return value


class AutoPublishCategoryUpdate(BaseModel):
    enabled: bool


class AutoPublishWithdrawRequest(BaseModel):
    reason: str = Field(default="管理员撤回", min_length=1, max_length=500)

    @field_validator("reason")
    @classmethod
    def non_empty_reason(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("reason cannot be blank")
        return value


class NewsReviewFeedbackCreate(BaseModel):
    label: Literal["accepted", "rejected", "modified"]
    safe_to_publish: bool = Field(alias="safeToPublish")
    evidence_accurate: bool = Field(alias="evidenceAccurate")
    reason: str = Field(min_length=1, max_length=1000)
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    @field_validator("reason")
    @classmethod
    def non_empty_reason(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("reason cannot be blank")
        return value
