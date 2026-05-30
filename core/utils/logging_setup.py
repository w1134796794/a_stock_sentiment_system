"""
日志配置工具（P3-7）

提供：
1. `setup_logging(level, log_dir, json_file=False)` 一行启动结构化日志。
2. `with_context(**kwargs)` 装饰器/上下文管理器，给当前作用域内的 logger 追加 bind 字段。

设计原则：
- 不强制改造既有 `loguru.logger.info(...)` 调用，靠 loguru 的 `bind()` 在调用方按需添加结构。
- 文件日志走 JSON serializer=True，方便后续 ELK/Loki 接入；控制台保持可读文本。
"""
from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

import loguru

logger = loguru.logger


def setup_logging(
    level: str = "INFO",
    log_dir: Optional[Path] = None,
    *,
    json_file: bool = True,
    console: bool = True,
    rotation: str = "1 day",
    retention: str = "30 days",
) -> None:
    """
    一次性初始化 loguru sinks。

    Args:
        level:      日志级别（DEBUG / INFO / WARNING / ERROR）
        log_dir:    日志文件目录。为 None 时禁用文件 sink。
        json_file:  文件 sink 是否走 JSON（serialize=True）格式
        console:    是否启用控制台 sink
        rotation:   文件 sink 切割频率
        retention:  文件保留时长
    """
    logger.remove()

    if console:
        logger.add(
            sys.stderr,
            level=level,
            format=(
                "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
                "<level>{level:<8}</level> | "
                "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
                "<level>{message}</level>"
            ),
            backtrace=False,
            diagnose=False,
            enqueue=False,
        )

    if log_dir is not None:
        log_dir = Path(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        file_sink = log_dir / "app_{time:YYYY-MM-DD}.log"
        if json_file:
            logger.add(
                str(file_sink),
                level=level,
                serialize=True,
                rotation=rotation,
                retention=retention,
                enqueue=True,
            )
        else:
            logger.add(
                str(file_sink),
                level=level,
                rotation=rotation,
                retention=retention,
                enqueue=True,
            )


@contextmanager
def with_context(**kwargs):
    """
    简易上下文管理器，使作用域内通过 `logger.bind(...)` 获得的字段自动附带 kwargs。

    用法：
        with with_context(layer="L3", stock="000001"):
            logger.info("scanning pattern")

    实现上利用 loguru 的 contextualize（其行为类似 thread-local stack）。
    """
    with logger.contextualize(**kwargs):
        yield
