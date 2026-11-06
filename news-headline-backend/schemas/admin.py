from typing import Literal

from pydantic import BaseModel


class RoleUpdateRequest(BaseModel):
    role: Literal["admin", "editor", "reviewer", "viewer", "user"]


class UserActiveUpdateRequest(BaseModel):
    is_active: bool


class AdminOperationRequest(BaseModel):
    """管理员自然语言运营命令；服务端只接受有限意图，不执行任意模型代码。"""

    command: str
