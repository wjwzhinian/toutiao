"""审计日志写入工具；日志内容只保存必要元数据，避免落库密钥和原始密码。"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def record_audit_event(
    db: AsyncSession,
    *,
    actor_id: int | None,
    action: str,
    resource_type: str,
    resource_id: str | None = None,
    outcome: str = "success",
    details: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> None:
    """记录管理操作，调用方可在同一个事务中提交。"""
    await db.execute(
        text(
            "INSERT INTO audit_logs "
            "(actor_id, action, resource_type, resource_id, outcome, details_json, request_id) "
            "VALUES (:actor_id, :action, :resource_type, :resource_id, :outcome, :details_json, :request_id)"
        ),
        {
            "actor_id": actor_id,
            "action": action[:80],
            "resource_type": resource_type[:40],
            "resource_id": resource_id[:80] if resource_id else None,
            "outcome": outcome[:20],
            "details_json": json.dumps(details or {}, ensure_ascii=False)[:10000],
            "request_id": request_id[:80] if request_id else None,
        },
    )
