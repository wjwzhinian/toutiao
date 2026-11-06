"""轻量 Prometheus 指标实现，避免业务代码依赖具体监控厂商。"""

from __future__ import annotations

from collections import defaultdict
import json
from threading import Lock


def _escape_label(value: str) -> str:
    """按 Prometheus text 格式转义标签中的特殊字符。"""
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


class Metrics:
    """进程内计数器；Prometheus 定期抓取 /metrics 获取当前实例数据。"""

    def __init__(self) -> None:
        self._lock = Lock()
        self._counters: defaultdict[tuple[str, tuple[tuple[str, str], ...]], int] = defaultdict(int)

    def inc(self, name: str, value: int = 1, **labels: str) -> None:
        key = (name, tuple(sorted((key, str(val)) for key, val in labels.items())))
        with self._lock:
            self._counters[key] += value

    async def inc_shared(self, name: str, value: int = 1, **labels: str) -> None:
        """把 Worker 指标写入 Redis，使 API 的抓取端点也能看到后台结果。"""
        from config.cache_conf import redis_client

        field = json.dumps([name, sorted((key, str(val)) for key, val in labels.items())], separators=(",", ":"))
        try:
            await redis_client.hincrby("news:prometheus:counters", field, value)
        except Exception:
            # 监控不可用不应阻断采集或审核业务。
            return

    def render(self) -> str:
        """输出 Prometheus text exposition 格式。"""
        lines: list[str] = []
        with self._lock:
            values = list(self._counters.items())
        for (name, labels), value in sorted(values):
            label_text = ""
            if labels:
                label_text = "{" + ",".join(
                    f'{key}="{_escape_label(val)}"'
                    for key, val in labels
                ) + "}"
            lines.append(f"{name}{label_text} {value}")
        return "\n".join(lines) + "\n"

    async def render_with_shared(self) -> str:
        """合并 API 进程计数器和 Redis 中的 Worker 计数器。"""
        from config.cache_conf import redis_client

        lines = [self.render().rstrip("\n")]
        try:
            shared = await redis_client.hgetall("news:prometheus:counters")
        except Exception:
            # 指标端点在 Redis 暂时不可用时仍返回本进程的观测结果。
            shared = {}
        for field, raw_value in sorted(shared.items()):
            try:
                name, labels = json.loads(field)
                label_text = ""
                if labels:
                    label_text = "{" + ",".join(
                        f'{key}="{_escape_label(value)}"' for key, value in labels
                    ) + "}"
                # 独立 Worker 的全局计数使用单独名称，避免与 API 本地计数冲突。
                shared_name = (
                    f"{name[:-6]}_shared_total" if name.endswith("_total") else f"{name}_shared"
                )
                lines.append(f"{shared_name}{label_text} {int(raw_value)}")
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        return "\n".join(line for line in lines if line) + "\n"


metrics = Metrics()
