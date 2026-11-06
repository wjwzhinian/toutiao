"""Idempotent schema migrations for existing application databases."""

import asyncio
import logging

from sqlalchemy import text

from config.db_confing import AsyncSessionLocal

logger = logging.getLogger(__name__)


async def _column_exists(db, table: str, column: str) -> bool:
    result = await db.execute(
        text(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = :table "
            "AND column_name = :column"
        ),
        {"table": table, "column": column},
    )
    return bool(result.scalar_one())


async def _index_exists(db, table: str, index: str) -> bool:
    result = await db.execute(
        text(
            "SELECT COUNT(*) FROM information_schema.statistics "
            "WHERE table_schema = DATABASE() AND table_name = :table "
            "AND index_name = :index"
        ),
        {"table": table, "index": index},
    )
    return bool(result.scalar_one())


async def ensure_security_schema() -> None:
    async with AsyncSessionLocal() as db:
        if not await _column_exists(db, "user", "role"):
            await db.execute(
                text(
                    "ALTER TABLE `user` ADD COLUMN `role` VARCHAR(20) "
                    "NOT NULL DEFAULT 'user' COMMENT 'permission role'"
                )
            )
        if not await _column_exists(db, "user", "is_active"):
            await db.execute(
                text(
                    "ALTER TABLE `user` ADD COLUMN `is_active` TINYINT(1) "
                    "NOT NULL DEFAULT 1 COMMENT 'account enabled'"
                )
            )
        await db.execute(
            text(
                "CREATE TABLE IF NOT EXISTS `auth_refresh_sessions` ("
                "`session_id` CHAR(36) NOT NULL,"
                "`user_id` INT UNSIGNED NOT NULL,"
                "`token_hash` CHAR(64) NOT NULL,"
                "`expires_at` DATETIME NOT NULL,"
                "`revoked_at` DATETIME NULL,"
                "`created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,"
                "PRIMARY KEY (`session_id`),"
                "UNIQUE INDEX `uq_auth_refresh_token_hash` (`token_hash`),"
                "INDEX `idx_auth_refresh_user` (`user_id`),"
                "CONSTRAINT `fk_auth_refresh_user` FOREIGN KEY (`user_id`) "
                "REFERENCES `user` (`id`) ON DELETE CASCADE ON UPDATE CASCADE"
                ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
            )
        )
        await db.commit()


async def ensure_news_management_schema() -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(
            text(
                "CREATE TABLE IF NOT EXISTS `news_feed_source` ("
                "`id` INT UNSIGNED NOT NULL AUTO_INCREMENT,"
                "`name` VARCHAR(100) NOT NULL,"
                "`url` VARCHAR(1000) NOT NULL,"
                "`category_id` INT UNSIGNED NOT NULL,"
                "`source_type` VARCHAR(32) NOT NULL DEFAULT 'other',"
                "`publisher_group` VARCHAR(120) NULL,"
                "`is_active` TINYINT(1) NOT NULL DEFAULT 1,"
                "`auto_collect_enabled` TINYINT(1) NOT NULL DEFAULT 0,"
                "`auto_collect_interval_minutes` INT NOT NULL DEFAULT 60,"
                "`trusted_for_auto_publish` TINYINT(1) NOT NULL DEFAULT 0,"
                "`auto_publish_enabled_at` DATETIME NULL,"
                "`last_tested_at` DATETIME NULL,"
                "`last_test_error` VARCHAR(100) NULL,"
                "`last_fetched_at` DATETIME NULL,"
                "`last_error` VARCHAR(100) NULL,"
                "`collection_failures` INT NOT NULL DEFAULT 0,"
                "`next_retry_at` DATETIME NULL,"
                "`created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,"
                "`updated_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,"
                "PRIMARY KEY (`id`),"
                "INDEX `idx_news_feed_source_active` (`is_active`),"
                "CONSTRAINT `fk_news_feed_source_category` FOREIGN KEY (`category_id`) "
                "REFERENCES `news_category` (`id`) ON DELETE RESTRICT ON UPDATE CASCADE"
                ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
            )
        )

        feed_columns = {
            "source_type": "ALTER TABLE `news_feed_source` ADD COLUMN `source_type` VARCHAR(32) NOT NULL DEFAULT 'other'",
            "publisher_group": "ALTER TABLE `news_feed_source` ADD COLUMN `publisher_group` VARCHAR(120) NULL",
            "auto_collect_enabled": "ALTER TABLE `news_feed_source` ADD COLUMN `auto_collect_enabled` TINYINT(1) NOT NULL DEFAULT 0",
            "auto_collect_interval_minutes": "ALTER TABLE `news_feed_source` ADD COLUMN `auto_collect_interval_minutes` INT NOT NULL DEFAULT 60",
            "trusted_for_auto_publish": "ALTER TABLE `news_feed_source` ADD COLUMN `trusted_for_auto_publish` TINYINT(1) NOT NULL DEFAULT 0",
            "auto_publish_enabled_at": "ALTER TABLE `news_feed_source` ADD COLUMN `auto_publish_enabled_at` DATETIME NULL",
            "last_tested_at": "ALTER TABLE `news_feed_source` ADD COLUMN `last_tested_at` DATETIME NULL",
            "last_test_error": "ALTER TABLE `news_feed_source` ADD COLUMN `last_test_error` VARCHAR(100) NULL",
            "collection_failures": "ALTER TABLE `news_feed_source` ADD COLUMN `collection_failures` INT NOT NULL DEFAULT 0",
            "next_retry_at": "ALTER TABLE `news_feed_source` ADD COLUMN `next_retry_at` DATETIME NULL",
        }
        for column, statement in feed_columns.items():
            if not await _column_exists(db, "news_feed_source", column):
                await db.execute(text(statement))

        columns = {
            "status": "ALTER TABLE `news` ADD COLUMN `status` VARCHAR(20) NOT NULL DEFAULT 'published'",
            "feed_source_id": "ALTER TABLE `news` ADD COLUMN `feed_source_id` INT UNSIGNED NULL",
            "external_id": "ALTER TABLE `news` ADD COLUMN `external_id` VARCHAR(191) NULL",
            "content_hash": "ALTER TABLE `news` ADD COLUMN `content_hash` VARCHAR(64) NULL",
            "reviewed_by": "ALTER TABLE `news` ADD COLUMN `reviewed_by` INT UNSIGNED NULL",
            "reviewed_at": "ALTER TABLE `news` ADD COLUMN `reviewed_at` DATETIME NULL",
            "source_url": "ALTER TABLE `news` ADD COLUMN `source_url` VARCHAR(1000) NULL",
        }
        for column, statement in columns.items():
            if not await _column_exists(db, "news", column):
                await db.execute(text(statement))

        indexes = {
            "idx_news_status_publish_time": "CREATE INDEX `idx_news_status_publish_time` ON `news` (`status`, `publish_time`)",
            "idx_news_auto_review": "CREATE INDEX `idx_news_auto_review` ON `news` (`status`, `feed_source_id`, `created_at`)",
            "uq_news_feed_external_id": "CREATE UNIQUE INDEX `uq_news_feed_external_id` ON `news` (`feed_source_id`, `external_id`)",
            "uq_news_content_hash": "CREATE UNIQUE INDEX `uq_news_content_hash` ON `news` (`content_hash`)",
        }
        for index, statement in indexes.items():
            if not await _index_exists(db, "news", index):
                await db.execute(text(statement))

        # MySQL 8 的 ngram 解析器支持中文全文索引；创建失败时检索服务会回退到 LIKE。
        if not await _index_exists(db, "news", "ft_news_evidence_ngram"):
            try:
                await db.execute(
                    text(
                        "CREATE FULLTEXT INDEX `ft_news_evidence_ngram` "
                        "ON `news` (`title`, `description`, `content`) WITH PARSER ngram"
                    )
                )
            except Exception as exc:
                # 旧版本 MySQL 或存量表不兼容时不阻断新闻服务启动。
                await db.rollback()
                logger.warning(
                    "MySQL ngram FULLTEXT index unavailable; evidence search will use LIKE: %s",
                    type(exc).__name__,
                )

        if not await _index_exists(db, "news_feed_source", "idx_news_feed_schedule"):
            await db.execute(
                text(
                    "CREATE INDEX `idx_news_feed_schedule` ON `news_feed_source` "
                    "(`is_active`, `auto_collect_enabled`, `last_fetched_at`)"
                )
            )

        result = await db.execute(
            text(
                "SELECT COUNT(*) FROM information_schema.table_constraints "
                "WHERE constraint_schema = DATABASE() AND table_name = 'news' "
                "AND constraint_name = 'fk_news_feed_source'"
            )
        )
        if not result.scalar_one():
            await db.execute(
                text(
                    "ALTER TABLE `news` ADD CONSTRAINT `fk_news_feed_source` "
                    "FOREIGN KEY (`feed_source_id`) REFERENCES `news_feed_source` (`id`) "
                    "ON DELETE SET NULL ON UPDATE CASCADE"
                )
            )
        await db.commit()


async def ensure_agent_action_schema() -> None:
    # 持久化人工确认提案及其状态，服务重启后仍可查询并安全完成操作。
    async with AsyncSessionLocal() as db:
        await db.execute(
            text(
                "CREATE TABLE IF NOT EXISTS `agent_pending_actions` ("
                "`action_id` CHAR(36) NOT NULL,"
                "`user_id` INT UNSIGNED NOT NULL,"
                "`action_type` VARCHAR(40) NOT NULL,"
                "`parameters_json` MEDIUMTEXT NOT NULL,"
                "`summary` VARCHAR(500) NOT NULL,"
                "`status` VARCHAR(20) NOT NULL DEFAULT 'pending',"
                "`result_json` MEDIUMTEXT NULL,"
                "`created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,"
                "`expires_at` DATETIME NOT NULL,"
                "`confirmed_at` DATETIME NULL,"
                "`completed_at` DATETIME NULL,"
                "PRIMARY KEY (`action_id`),"
                "INDEX `idx_agent_pending_actions_user_status` (`user_id`, `status`, `expires_at`),"
                "CONSTRAINT `fk_agent_pending_actions_user` FOREIGN KEY (`user_id`) "
                "REFERENCES `user` (`id`) ON DELETE CASCADE ON UPDATE CASCADE"
                ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
            )
        )
        await db.commit()


async def ensure_auto_pipeline_schema() -> None:
    """Create category allow-list and durable per-article automated review audit."""
    async with AsyncSessionLocal() as db:
        await db.execute(
            text(
                "CREATE TABLE IF NOT EXISTS `news_auto_publish_categories` ("
                "`category_id` INT UNSIGNED NOT NULL,"
                "`enabled` TINYINT(1) NOT NULL DEFAULT 1,"
                "`enabled_at` DATETIME NOT NULL,"
                "`updated_by` INT UNSIGNED NULL,"
                "`created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,"
                "`updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,"
                "PRIMARY KEY (`category_id`),"
                "CONSTRAINT `fk_auto_publish_category` FOREIGN KEY (`category_id`) "
                "REFERENCES `news_category` (`id`) ON DELETE CASCADE ON UPDATE CASCADE,"
                "CONSTRAINT `fk_auto_publish_category_user` FOREIGN KEY (`updated_by`) "
                "REFERENCES `user` (`id`) ON DELETE SET NULL ON UPDATE CASCADE"
                ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
            )
        )
        await db.execute(
            text(
                "CREATE TABLE IF NOT EXISTS `news_auto_review_logs` ("
                "`id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,"
                "`news_id` INT UNSIGNED NULL,"
                "`source_id` INT UNSIGNED NULL,"
                "`source_name` VARCHAR(100) NULL,"
                "`category_id` INT UNSIGNED NULL,"
                "`category_name` VARCHAR(50) NULL,"
                "`title_snapshot` VARCHAR(255) NOT NULL,"
                "`decision` VARCHAR(32) NOT NULL,"
                "`reasons_json` TEXT NOT NULL,"
                "`review_result_json` MEDIUMTEXT NULL,"
                "`attempts` INT NOT NULL DEFAULT 1,"
                "`last_attempt_at` DATETIME NOT NULL,"
                "`published_at` DATETIME NULL,"
                "`withdrawn_at` DATETIME NULL,"
                "`withdrawn_by` INT UNSIGNED NULL,"
                "`withdraw_reason` VARCHAR(500) NULL,"
                "`created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,"
                "`updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,"
                "PRIMARY KEY (`id`),"
                "UNIQUE INDEX `uq_auto_review_news` (`news_id`),"
                "INDEX `idx_auto_review_decision_attempt` (`decision`, `last_attempt_at`),"
                "INDEX `idx_auto_review_published_at` (`published_at`),"
                "CONSTRAINT `fk_auto_review_news` FOREIGN KEY (`news_id`) "
                "REFERENCES `news` (`id`) ON DELETE SET NULL ON UPDATE CASCADE,"
                "CONSTRAINT `fk_auto_review_withdrawer` FOREIGN KEY (`withdrawn_by`) "
                "REFERENCES `user` (`id`) ON DELETE SET NULL ON UPDATE CASCADE"
                ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
            )
        )
        await db.execute(
            text(
                "CREATE TABLE IF NOT EXISTS `news_review_feedback` ("
                "`id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,"
                "`review_log_id` BIGINT UNSIGNED NOT NULL,"
                "`reviewer_id` INT UNSIGNED NOT NULL,"
                "`label` VARCHAR(20) NOT NULL,"
                "`safe_to_publish` TINYINT(1) NOT NULL,"
                "`evidence_accurate` TINYINT(1) NOT NULL,"
                "`reason` TEXT NOT NULL,"
                "`created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,"
                "`updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,"
                "PRIMARY KEY (`id`),"
                "UNIQUE INDEX `uq_news_review_feedback_log_reviewer` (`review_log_id`, `reviewer_id`),"
                "INDEX `idx_news_review_feedback_label` (`label`, `created_at`),"
                "CONSTRAINT `fk_news_review_feedback_log` FOREIGN KEY (`review_log_id`) "
                "REFERENCES `news_auto_review_logs` (`id`) ON DELETE CASCADE ON UPDATE CASCADE,"
                "CONSTRAINT `fk_news_review_feedback_reviewer` FOREIGN KEY (`reviewer_id`) "
                "REFERENCES `user` (`id`) ON DELETE CASCADE ON UPDATE CASCADE"
                ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
            )
        )
        await db.commit()


async def ensure_operations_schema() -> None:
    """创建任务编排、来源评分和事件跟踪所需的审计表。"""
    async with AsyncSessionLocal() as db:
        await db.execute(
            text(
                "CREATE TABLE IF NOT EXISTS `news_collection_jobs` ("
                "`id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,"
                "`source_id` INT UNSIGNED NOT NULL,"
                "`trigger` VARCHAR(20) NOT NULL DEFAULT 'scheduled',"
                "`status` VARCHAR(20) NOT NULL DEFAULT 'queued',"
                "`attempt_count` INT NOT NULL DEFAULT 0,"
                "`max_attempts` INT NOT NULL DEFAULT 3,"
                "`next_retry_at` DATETIME NULL,"
                "`started_at` DATETIME NULL,"
                "`finished_at` DATETIME NULL,"
                "`error_code` VARCHAR(100) NULL,"
                "`result_json` TEXT NULL,"
                "`requested_by` INT UNSIGNED NULL,"
                "`created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,"
                "`updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,"
                "PRIMARY KEY (`id`),"
                "INDEX `idx_collection_job_status_retry` (`status`, `next_retry_at`),"
                "INDEX `idx_collection_job_source_created` (`source_id`, `created_at`),"
                "CONSTRAINT `fk_collection_job_source` FOREIGN KEY (`source_id`) REFERENCES `news_feed_source` (`id`) ON DELETE CASCADE"
                ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
            )
        )
        await db.execute(
            text(
                "CREATE TABLE IF NOT EXISTS `news_source_quality_scores` ("
                "`source_id` INT UNSIGNED NOT NULL,"
                "`score` FLOAT NOT NULL,"
                "`tier` VARCHAR(20) NOT NULL,"
                "`metrics_json` TEXT NOT NULL,"
                "`evaluated_at` DATETIME NOT NULL,"
                "`created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,"
                "`updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,"
                "PRIMARY KEY (`source_id`),"
                "CONSTRAINT `fk_quality_score_source` FOREIGN KEY (`source_id`) REFERENCES `news_feed_source` (`id`) ON DELETE CASCADE"
                ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
            )
        )
        await db.execute(
            text(
                "CREATE TABLE IF NOT EXISTS `news_events` ("
                "`id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,"
                "`event_key` VARCHAR(64) NOT NULL,"
                "`canonical_title` VARCHAR(255) NOT NULL,"
                "`latest_news_id` INT UNSIGNED NULL,"
                "`first_seen_at` DATETIME NOT NULL,"
                "`last_seen_at` DATETIME NOT NULL,"
                "`update_count` INT NOT NULL DEFAULT 0,"
                "`needs_review` TINYINT(1) NOT NULL DEFAULT 0,"
                "`correction_summary` TEXT NULL,"
                "`created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,"
                "`updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,"
                "PRIMARY KEY (`id`), UNIQUE INDEX `uq_news_event_key` (`event_key`),"
                "INDEX `idx_news_event_review` (`needs_review`, `last_seen_at`),"
                "CONSTRAINT `fk_news_event_latest` FOREIGN KEY (`latest_news_id`) REFERENCES `news` (`id`) ON DELETE SET NULL"
                ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
            )
        )
        await db.execute(
            text(
                "CREATE TABLE IF NOT EXISTS `news_event_items` ("
                "`id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,"
                "`event_id` BIGINT UNSIGNED NOT NULL,"
                "`news_id` INT UNSIGNED NOT NULL,"
                "`relation_type` VARCHAR(20) NOT NULL DEFAULT 'related',"
                "`similarity` DECIMAL(5,3) NOT NULL DEFAULT 1.000,"
                "`created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,"
                "`updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,"
                "PRIMARY KEY (`id`), UNIQUE INDEX `uq_news_event_item_news` (`news_id`),"
                "INDEX `idx_news_event_item_event` (`event_id`, `created_at`),"
                "CONSTRAINT `fk_news_event_item_event` FOREIGN KEY (`event_id`) REFERENCES `news_events` (`id`) ON DELETE CASCADE,"
                "CONSTRAINT `fk_news_event_item_news` FOREIGN KEY (`news_id`) REFERENCES `news` (`id`) ON DELETE CASCADE"
                ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
            )
        )
        await db.execute(
            text(
                "CREATE TABLE IF NOT EXISTS `news_event_alerts` ("
                "`id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,"
                "`event_id` BIGINT UNSIGNED NOT NULL,"
                "`news_id` INT UNSIGNED NULL,"
                "`alert_type` VARCHAR(30) NOT NULL,"
                "`status` VARCHAR(20) NOT NULL DEFAULT 'pending',"
                "`summary` VARCHAR(500) NOT NULL,"
                "`evidence_json` TEXT NOT NULL,"
                "`resolved_by` INT UNSIGNED NULL,"
                "`resolved_at` DATETIME NULL,"
                "`created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,"
                "`updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,"
                "PRIMARY KEY (`id`), INDEX `idx_news_event_alert_status` (`status`, `created_at`),"
                "CONSTRAINT `fk_news_event_alert_event` FOREIGN KEY (`event_id`) REFERENCES `news_events` (`id`) ON DELETE CASCADE,"
                "CONSTRAINT `fk_news_event_alert_news` FOREIGN KEY (`news_id`) REFERENCES `news` (`id`) ON DELETE SET NULL"
                ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
            )
        )
        await db.commit()


async def ensure_audit_schema() -> None:
    """创建不可变管理审计日志表，兼容已有数据库的幂等启动。"""
    async with AsyncSessionLocal() as db:
        await db.execute(
            text(
                "CREATE TABLE IF NOT EXISTS `audit_logs` ("
                "`id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,"
                "`actor_id` INT UNSIGNED NULL,"
                "`action` VARCHAR(80) NOT NULL,"
                "`resource_type` VARCHAR(40) NOT NULL,"
                "`resource_id` VARCHAR(80) NULL,"
                "`outcome` VARCHAR(20) NOT NULL DEFAULT 'success',"
                "`details_json` TEXT NOT NULL,"
                "`request_id` VARCHAR(80) NULL,"
                "`created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,"
                "PRIMARY KEY (`id`),"
                "INDEX `idx_audit_actor_created` (`actor_id`, `created_at`),"
                "INDEX `idx_audit_action_created` (`action`, `created_at`),"
                "CONSTRAINT `fk_audit_actor` FOREIGN KEY (`actor_id`) REFERENCES `user` (`id`) ON DELETE SET NULL"
                ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
            )
        )
        await db.commit()


async def ensure_vector_index_schema() -> None:
    """建立新闻向量索引待办表，记录异步同步状态和失败重试时间。"""
    async with AsyncSessionLocal() as db:
        await db.execute(
            text(
                "CREATE TABLE IF NOT EXISTS `news_vector_index_state` ("
                "`news_id` INT UNSIGNED NOT NULL,"
                "`status` VARCHAR(20) NOT NULL DEFAULT 'pending',"
                "`content_fingerprint` CHAR(64) NULL,"
                "`embedding_version` VARCHAR(120) NULL,"
                "`source_updated_at` DATETIME NULL,"
                "`attempts` INT NOT NULL DEFAULT 0,"
                "`next_retry_at` DATETIME NULL,"
                "`last_error` VARCHAR(100) NULL,"
                "`indexed_at` DATETIME NULL,"
                "`created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,"
                "`updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,"
                "PRIMARY KEY (`news_id`),"
                "INDEX `idx_news_vector_state_retry` (`status`, `next_retry_at`)"
                ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
            )
        )
        await db.commit()


async def ensure_all_schema() -> None:
    await ensure_security_schema()
    await ensure_news_management_schema()
    await ensure_agent_action_schema()
    await ensure_auto_pipeline_schema()
    await ensure_operations_schema()
    await ensure_audit_schema()
    await ensure_vector_index_schema()


if __name__ == "__main__":
    asyncio.run(ensure_all_schema())
