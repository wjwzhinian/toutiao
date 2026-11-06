"""管理员自然语言运营入口的受限解析器。"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from agent.actions import (
    AgentActionError,
    get_auto_publish_stats,
    get_collection_jobs,
    get_event_alerts,
    get_recent_collection_failures,
    propose_manual_review_category,
    propose_pause_category,
    propose_re_review_blocked,
)


def _category(command: str, markers: tuple[str, ...]) -> str | None:
    """从明确的运营句式中提取分类名，避免把整句直接交给数据库。"""
    for marker in markers:
        match = re.search(rf"{re.escape(marker)}([\u4e00-\u9fffA-Za-z0-9_-]{{1,50}})", command)
        if match:
            return match.group(1).strip()
    return None


def _pause_target(command: str) -> str | None:
    """识别“暂停科技来源”一类句式，只提取已有分类名称。"""
    match = re.search(r"暂停(.+?)(?:新闻来源|来源|新闻源|分类)(?:$|[，。！! ])", command)
    return match.group(1).strip() if match else None


def _manual_review_target(command: str) -> str | None:
    """识别“把金融新闻全部改为人工审核”一类句式。"""
    match = re.search(r"(?:把|将)(.+?)(?:新闻)?(?:全部)?改为人工审核(?:$|[，。！! ])", command)
    return match.group(1).strip() if match else None


async def interpret_admin_command(user_id: int, command: str) -> dict[str, Any]:
    """解析五类固定运营意图；写操作只返回待确认提案。"""
    command = command.strip()
    if not command or len(command) > 500:
        raise AgentActionError("invalid_command")
    if "失败" in command and ("采集" in command or "任务" in command):
        return {"status": "completed", "intent": "collection_failures", "data": {
            "failures": await get_recent_collection_failures(10),
            "jobs": await get_collection_jobs(20, "failed"),
        }}
    if "事件" in command and ("更正" in command or "更新" in command or "复核" in command):
        return {"status": "completed", "intent": "event_alerts", "data": await get_event_alerts(10)}
    if "统计" in command and "自动发布" in command:
        end = date.today()
        return {"status": "completed", "intent": "auto_publish_stats", "data": await get_auto_publish_stats((end - timedelta(days=6)).isoformat(), end.isoformat())}
    if "重新审核" in command and ("拦截" in command or "被拦" in command):
        end = date.today()
        action = await propose_re_review_blocked(user_id, (end - timedelta(days=1)).isoformat(), end.isoformat())
        return {"status": "awaiting_confirmation", "intent": "re_review_blocked", "pending_action": action}
    if "暂停" in command and "来源" in command:
        category = _pause_target(command) or _category(command, ("暂停分类",))
        if not category:
            raise AgentActionError("category_required")
        return {"status": "awaiting_confirmation", "intent": "pause_category_sources", "pending_action": await propose_pause_category(user_id, category)}
    if "人工审核" in command and ("全部" in command or "改为" in command):
        category = _manual_review_target(command)
        if not category:
            raise AgentActionError("category_required")
        return {"status": "awaiting_confirmation", "intent": "disable_auto_publish_category", "pending_action": await propose_manual_review_category(user_id, category)}
    raise AgentActionError("unsupported_command")
