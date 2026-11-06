"""不调用外部模型的 Agent 回归评测集，保证核心安全门槛不会退化。"""

import json
from pathlib import Path

from services.orchestration import next_job_state
from utils.permissions import has_permission


def test_agent_regression_cases():
    cases = json.loads((Path(__file__).parent / "evaluations" / "agent_cases.json").read_text(encoding="utf-8"))
    for case in cases:
        status, _ = next_job_state(
            current="running",
            error_code=case["input"]["error_code"],
            attempt_count=case["input"]["attempt_count"],
            max_attempts=case["input"]["max_attempts"],
        )
        assert status == case["expected"]["status"], case["name"]


def test_agent_regression_permission_boundary():
    """普通用户不能获得管理员工具权限。"""
    assert has_permission("user", "agent:use")
    assert not has_permission("user", "news:manage")
    assert has_permission("admin", "news:manage")
