-- Apply after the base database.sql on both new and existing installations.
-- Agent sessions and messages are isolated by the authenticated user_id.


CREATE TABLE IF NOT EXISTS agent_sessions (
  id CHAR(36) NOT NULL,
  user_id INT UNSIGNED NOT NULL,
  title VARCHAR(120) NOT NULL,
  status VARCHAR(20) NOT NULL DEFAULT 'active',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  INDEX idx_agent_sessions_user_updated (user_id, updated_at),
  CONSTRAINT fk_agent_sessions_user
    FOREIGN KEY (user_id) REFERENCES `user` (id)
    ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS agent_messages (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  session_id CHAR(36) NOT NULL,
  run_id CHAR(36) NULL,
  role VARCHAR(20) NOT NULL,
  content MEDIUMTEXT NOT NULL,
  citations_json MEDIUMTEXT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  INDEX idx_agent_messages_session_created (session_id, id),
  CONSTRAINT fk_agent_messages_session
    FOREIGN KEY (session_id) REFERENCES agent_sessions (id)
    ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS agent_runs (
  id CHAR(36) NOT NULL,
  session_id CHAR(36) NOT NULL,
  status VARCHAR(20) NOT NULL,
  provider VARCHAR(80) NOT NULL,
  model VARCHAR(120) NOT NULL,
  error_code VARCHAR(80) NULL,
  latency_ms INT UNSIGNED NULL,
  started_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  finished_at DATETIME NULL,
  PRIMARY KEY (id),
  INDEX idx_agent_runs_session_started (session_id, started_at),
  CONSTRAINT fk_agent_runs_session
    FOREIGN KEY (session_id) REFERENCES agent_sessions (id)
    ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS agent_tool_calls (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  run_id CHAR(36) NOT NULL,
  tool_name VARCHAR(80) NOT NULL,
  arguments_json MEDIUMTEXT NOT NULL,
  result_json MEDIUMTEXT NOT NULL,
  status VARCHAR(20) NOT NULL,
  duration_ms INT UNSIGNED NOT NULL DEFAULT 0,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  INDEX idx_agent_tool_calls_run (run_id),
  CONSTRAINT fk_agent_tool_calls_run
    FOREIGN KEY (run_id) REFERENCES agent_runs (id)
    ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Agent 工具只允许提出管理操作；后端执行白名单操作前必须由管理员确认。
CREATE TABLE IF NOT EXISTS agent_pending_actions (
  action_id CHAR(36) NOT NULL,
  user_id INT UNSIGNED NOT NULL,
  action_type VARCHAR(40) NOT NULL,
  parameters_json MEDIUMTEXT NOT NULL,
  summary VARCHAR(500) NOT NULL,
  status VARCHAR(20) NOT NULL DEFAULT 'pending',
  result_json MEDIUMTEXT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  expires_at DATETIME NOT NULL,
  confirmed_at DATETIME NULL,
  completed_at DATETIME NULL,
  PRIMARY KEY (action_id),
  INDEX idx_agent_pending_actions_user_status (user_id, status, expires_at),
  CONSTRAINT fk_agent_pending_actions_user
    FOREIGN KEY (user_id) REFERENCES `user` (id)
    ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 自动发布必须同时满足可信来源和管理员维护的分类白名单。
CREATE TABLE IF NOT EXISTS news_auto_publish_categories (
  category_id INT UNSIGNED NOT NULL,
  enabled TINYINT(1) NOT NULL DEFAULT 1,
  enabled_at DATETIME NOT NULL,
  updated_by INT UNSIGNED NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (category_id),
  CONSTRAINT fk_auto_publish_category
    FOREIGN KEY (category_id) REFERENCES news_category (id)
    ON DELETE CASCADE ON UPDATE CASCADE,
  CONSTRAINT fk_auto_publish_category_user
    FOREIGN KEY (updated_by) REFERENCES `user` (id)
    ON DELETE SET NULL ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 保存每篇自动审核稿件的决策、证据、发布和撤回记录，供管理员追溯。
CREATE TABLE IF NOT EXISTS news_auto_review_logs (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  news_id INT UNSIGNED NULL,
  source_id INT UNSIGNED NULL,
  source_name VARCHAR(100) NULL,
  category_id INT UNSIGNED NULL,
  category_name VARCHAR(50) NULL,
  title_snapshot VARCHAR(255) NOT NULL,
  decision VARCHAR(32) NOT NULL,
  reasons_json TEXT NOT NULL,
  review_result_json MEDIUMTEXT NULL,
  attempts INT NOT NULL DEFAULT 1,
  last_attempt_at DATETIME NOT NULL,
  published_at DATETIME NULL,
  withdrawn_at DATETIME NULL,
  withdrawn_by INT UNSIGNED NULL,
  withdraw_reason VARCHAR(500) NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE INDEX uq_auto_review_news (news_id),
  INDEX idx_auto_review_decision_attempt (decision, last_attempt_at),
  INDEX idx_auto_review_published_at (published_at),
  CONSTRAINT fk_auto_review_news
    FOREIGN KEY (news_id) REFERENCES news (id)
    ON DELETE SET NULL ON UPDATE CASCADE,
  CONSTRAINT fk_auto_review_withdrawer
    FOREIGN KEY (withdrawn_by) REFERENCES `user` (id)
    ON DELETE SET NULL ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 保存编辑对审核结论、发布安全性和证据引用的人工标注。
CREATE TABLE IF NOT EXISTS news_review_feedback (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  review_log_id BIGINT UNSIGNED NOT NULL,
  reviewer_id INT UNSIGNED NOT NULL,
  label VARCHAR(20) NOT NULL,
  safe_to_publish TINYINT(1) NOT NULL,
  evidence_accurate TINYINT(1) NOT NULL,
  reason TEXT NOT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE INDEX uq_news_review_feedback_log_reviewer (review_log_id, reviewer_id),
  INDEX idx_news_review_feedback_label (label, created_at),
  CONSTRAINT fk_news_review_feedback_log
    FOREIGN KEY (review_log_id) REFERENCES news_auto_review_logs (id)
    ON DELETE CASCADE ON UPDATE CASCADE,
  CONSTRAINT fk_news_review_feedback_reviewer
    FOREIGN KEY (reviewer_id) REFERENCES `user` (id)
    ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Collection retries and state transitions for administrator visibility.
CREATE TABLE IF NOT EXISTS news_collection_jobs (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  source_id INT UNSIGNED NOT NULL,
  `trigger` VARCHAR(20) NOT NULL DEFAULT 'scheduled',
  status VARCHAR(20) NOT NULL DEFAULT 'queued',
  attempt_count INT NOT NULL DEFAULT 0,
  max_attempts INT NOT NULL DEFAULT 3,
  next_retry_at DATETIME NULL,
  started_at DATETIME NULL,
  finished_at DATETIME NULL,
  error_code VARCHAR(100) NULL,
  result_json TEXT NULL,
  requested_by INT UNSIGNED NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  INDEX idx_collection_job_status_retry (status, next_retry_at),
  INDEX idx_collection_job_source_created (source_id, created_at),
  CONSTRAINT fk_collection_job_source FOREIGN KEY (source_id)
    REFERENCES news_feed_source (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Latest explainable quality score used as one of the automatic-publication gates.
CREATE TABLE IF NOT EXISTS news_source_quality_scores (
  source_id INT UNSIGNED NOT NULL,
  score FLOAT NOT NULL,
  tier VARCHAR(20) NOT NULL,
  metrics_json TEXT NOT NULL,
  evaluated_at DATETIME NOT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (source_id),
  CONSTRAINT fk_quality_score_source FOREIGN KEY (source_id)
    REFERENCES news_feed_source (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Event groups are generated from title similarity; they never modify or withdraw articles.
CREATE TABLE IF NOT EXISTS news_events (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  event_key VARCHAR(64) NOT NULL,
  canonical_title VARCHAR(255) NOT NULL,
  latest_news_id INT UNSIGNED NULL,
  first_seen_at DATETIME NOT NULL,
  last_seen_at DATETIME NOT NULL,
  update_count INT NOT NULL DEFAULT 0,
  needs_review TINYINT(1) NOT NULL DEFAULT 0,
  correction_summary TEXT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE INDEX uq_news_event_key (event_key),
  INDEX idx_news_event_review (needs_review, last_seen_at),
  CONSTRAINT fk_news_event_latest FOREIGN KEY (latest_news_id)
    REFERENCES news (id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS news_event_items (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  event_id BIGINT UNSIGNED NOT NULL,
  news_id INT UNSIGNED NOT NULL,
  relation_type VARCHAR(20) NOT NULL DEFAULT 'related',
  similarity FLOAT NOT NULL DEFAULT 1.0,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE INDEX uq_news_event_item_news (news_id),
  INDEX idx_news_event_item_event (event_id, created_at),
  CONSTRAINT fk_news_event_item_event FOREIGN KEY (event_id)
    REFERENCES news_events (id) ON DELETE CASCADE,
  CONSTRAINT fk_news_event_item_news FOREIGN KEY (news_id)
    REFERENCES news (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS news_event_alerts (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  event_id BIGINT UNSIGNED NOT NULL,
  news_id INT UNSIGNED NULL,
  alert_type VARCHAR(30) NOT NULL,
  status VARCHAR(20) NOT NULL DEFAULT 'pending',
  summary VARCHAR(500) NOT NULL,
  evidence_json TEXT NOT NULL,
  resolved_by INT UNSIGNED NULL,
  resolved_at DATETIME NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  INDEX idx_news_event_alert_status (status, created_at),
  CONSTRAINT fk_news_event_alert_event FOREIGN KEY (event_id)
    REFERENCES news_events (id) ON DELETE CASCADE,
  CONSTRAINT fk_news_event_alert_news FOREIGN KEY (news_id)
    REFERENCES news (id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


-- 管理操作审计日志；details_json 只保存脱敏后的参数和结果摘要。
CREATE TABLE IF NOT EXISTS audit_logs (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  actor_id INT UNSIGNED NULL,
  action VARCHAR(80) NOT NULL,
  resource_type VARCHAR(40) NOT NULL,
  resource_id VARCHAR(80) NULL,
  outcome VARCHAR(20) NOT NULL DEFAULT 'success',
  details_json TEXT NOT NULL,
  request_id VARCHAR(80) NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  INDEX idx_audit_actor_created (actor_id, created_at),
  INDEX idx_audit_action_created (action, created_at),
  CONSTRAINT fk_audit_actor FOREIGN KEY (actor_id) REFERENCES `user` (id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 新闻向量索引待办表；独立于新闻外键，确保新闻删除后仍能清理向量库。
CREATE TABLE IF NOT EXISTS news_vector_index_state (
  news_id INT UNSIGNED NOT NULL,
  status VARCHAR(20) NOT NULL DEFAULT 'pending',
  content_fingerprint CHAR(64) NULL,
  embedding_version VARCHAR(120) NULL,
  source_updated_at DATETIME NULL,
  attempts INT NOT NULL DEFAULT 0,
  next_retry_at DATETIME NULL,
  last_error VARCHAR(100) NULL,
  indexed_at DATETIME NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (news_id),
  INDEX idx_news_vector_state_retry (status, next_retry_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
