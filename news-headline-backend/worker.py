"""独立的新闻流水线 Worker 入口。

生产环境建议把它作为独立进程运行，避免 API 重启、扩容时重复启动定时任务。
本地开发仍可保留 FastAPI 内置调度，设置 ``AUTO_PIPELINE_ENABLED=false`` 后只运行本 Worker。
"""

from __future__ import annotations

import asyncio
import logging

from services.auto_pipeline import run_auto_pipeline_tick
from config.settings import get_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("news.pipeline.worker")


async def run_worker() -> None:
    """按配置周期执行一次完整采集、审核和发布流水线。"""
    settings = get_settings()
    logger.info("独立流水线 Worker 已启动，间隔=%s 秒", settings.auto_pipeline_interval_seconds)
    while True:
        try:
            stats = await run_auto_pipeline_tick()
            logger.info("流水线执行完成: %s", stats)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Worker 不因单次任务异常退出，下一周期继续运行。
            logger.exception("流水线执行失败")
        await asyncio.sleep(settings.auto_pipeline_interval_seconds)


if __name__ == "__main__":
    asyncio.run(run_worker())
