"""
并行 IO 工具（P3-8）

围绕 ThreadPoolExecutor 提供两个常用模式：
1. `parallel_map(fn, items, max_workers, ordered=True)`：按输入顺序返回结果
2. `parallel_fetch(items, fn, max_workers)`：返回 dict[item, result]

设计目标：
- 默认 4 个 worker；超过 32 强制截断（避免对 Tushare/AKShare 造成限流）
- 异常会被记录但不会中断其它请求 —— 失败项在结果中为 None / 异常对象
- 仅用于 IO 密集型任务（API 拉取、文件读取）

注：网络限流由各 DataManager 子类自己控制；本工具不负责限流。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Iterable, List, TypeVar

import loguru

logger = loguru.logger

T = TypeVar("T")
R = TypeVar("R")

_DEFAULT_MAX_WORKERS = 4
_HARD_MAX_WORKERS = 32


def _clamp_workers(n: int) -> int:
    return max(1, min(int(n), _HARD_MAX_WORKERS))


def parallel_map(fn: Callable[[T], R], items: Iterable[T],
                 max_workers: int = _DEFAULT_MAX_WORKERS,
                 *, log_errors: bool = True) -> List[R]:
    """
    并行执行 fn(item) 并按输入顺序返回结果。失败项以 None 占位。
    """
    items_list = list(items)
    if not items_list:
        return []
    workers = _clamp_workers(max_workers)
    results: List[Any] = [None] * len(items_list)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        future_to_idx = {ex.submit(fn, item): i for i, item in enumerate(items_list)}
        for fut in as_completed(future_to_idx):
            idx = future_to_idx[fut]
            try:
                results[idx] = fut.result()
            except Exception as e:
                if log_errors:
                    logger.warning(f"[parallel_map] item={items_list[idx]!r} 失败: {e}")
                results[idx] = None
    return results


def parallel_fetch(items: Iterable[T], fn: Callable[[T], R],
                   max_workers: int = _DEFAULT_MAX_WORKERS,
                   *, log_errors: bool = True) -> dict:
    """
    并行执行 fn(item) 并返回 {item: result} 字典。失败项不会出现在结果中。
    """
    items_list = list(items)
    if not items_list:
        return {}
    workers = _clamp_workers(max_workers)
    out: dict = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        future_to_key = {ex.submit(fn, item): item for item in items_list}
        for fut in as_completed(future_to_key):
            key = future_to_key[fut]
            try:
                out[key] = fut.result()
            except Exception as e:
                if log_errors:
                    logger.warning(f"[parallel_fetch] item={key!r} 失败: {e}")
    return out


__all__ = ["parallel_map", "parallel_fetch"]
