"""Redis-backed shared runtime state with an in-memory development fallback."""
from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from loguru import logger


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


class MemoryStateBackend:
    """Process-local backend used only when Redis is not configured."""

    is_shared = False

    def __init__(self, prefix: str = "a_stock") -> None:
        self.prefix = prefix
        self._values: Dict[str, tuple[str, float]] = {}
        self._lists: Dict[str, List[str]] = {}
        self._locks: Dict[str, tuple[str, float]] = {}
        self._mutex = threading.RLock()

    def _key(self, key: str) -> str:
        return f"{self.prefix}:{key}"

    def get_json(self, key: str) -> Any:
        full = self._key(key)
        with self._mutex:
            item = self._values.get(full)
            if not item:
                return None
            raw, expires_at = item
            if expires_at and expires_at <= time.time():
                self._values.pop(full, None)
                return None
        return json.loads(raw)

    def set_json(self, key: str, value: Any, ttl_seconds: int = 0) -> None:
        expires_at = time.time() + ttl_seconds if ttl_seconds > 0 else 0.0
        with self._mutex:
            self._values[self._key(key)] = (_json_dumps(value), expires_at)

    def delete(self, key: str) -> None:
        full = self._key(key)
        with self._mutex:
            self._values.pop(full, None)
            self._lists.pop(full, None)

    def append_list(self, key: str, value: str, max_items: int = 10000) -> int:
        full = self._key(key)
        with self._mutex:
            rows = self._lists.setdefault(full, [])
            rows.append(str(value))
            if max_items > 0 and len(rows) > max_items:
                del rows[:-max_items]
            return len(rows)

    def read_list(self, key: str, start: int = 0) -> tuple[List[str], int]:
        with self._mutex:
            rows = list(self._lists.get(self._key(key), []))
        start = max(int(start or 0), 0)
        return rows[start:], len(rows)

    def clear_list(self, key: str) -> None:
        with self._mutex:
            self._lists.pop(self._key(key), None)

    def acquire_lock(self, key: str, ttl_seconds: int = 60) -> Optional[str]:
        full = self._key(f"lock:{key}")
        now = time.time()
        token = uuid.uuid4().hex
        with self._mutex:
            current = self._locks.get(full)
            if current and current[1] > now:
                return None
            self._locks[full] = (token, now + max(int(ttl_seconds), 1))
        return token

    def refresh_lock(self, key: str, token: str, ttl_seconds: int = 60) -> bool:
        full = self._key(f"lock:{key}")
        with self._mutex:
            current = self._locks.get(full)
            if not current or current[0] != token:
                return False
            self._locks[full] = (token, time.time() + max(int(ttl_seconds), 1))
        return True

    def release_lock(self, key: str, token: str) -> bool:
        full = self._key(f"lock:{key}")
        with self._mutex:
            current = self._locks.get(full)
            if not current or current[0] != token:
                return False
            self._locks.pop(full, None)
        return True


class RedisStateBackend:
    """Small redis-py adapter; values remain JSON for language-neutral inspection."""

    is_shared = True
    _RELEASE_SCRIPT = """
    if redis.call('get', KEYS[1]) == ARGV[1] then
      return redis.call('del', KEYS[1])
    end
    return 0
    """
    _REFRESH_SCRIPT = """
    if redis.call('get', KEYS[1]) == ARGV[1] then
      return redis.call('expire', KEYS[1], ARGV[2])
    end
    return 0
    """

    def __init__(self, url: str, prefix: str, timeout_seconds: float = 2.0) -> None:
        import redis  # type: ignore

        self.prefix = prefix
        self.client = redis.Redis.from_url(
            url,
            decode_responses=True,
            socket_connect_timeout=max(float(timeout_seconds), 0.2),
            socket_timeout=max(float(timeout_seconds), 0.2),
            health_check_interval=30,
        )
        self.client.ping()

    def _key(self, key: str) -> str:
        return f"{self.prefix}:{key}"

    def get_json(self, key: str) -> Any:
        raw = self.client.get(self._key(key))
        return json.loads(raw) if raw else None

    def set_json(self, key: str, value: Any, ttl_seconds: int = 0) -> None:
        kwargs = {"ex": max(int(ttl_seconds), 1)} if ttl_seconds > 0 else {}
        self.client.set(self._key(key), _json_dumps(value), **kwargs)

    def delete(self, key: str) -> None:
        self.client.delete(self._key(key))

    def append_list(self, key: str, value: str, max_items: int = 10000) -> int:
        full = self._key(key)
        pipe = self.client.pipeline(transaction=True)
        pipe.rpush(full, str(value))
        if max_items > 0:
            pipe.ltrim(full, -max_items, -1)
        result = pipe.execute()
        return int(result[0] or 0)

    def read_list(self, key: str, start: int = 0) -> tuple[List[str], int]:
        full = self._key(key)
        start = max(int(start or 0), 0)
        pipe = self.client.pipeline(transaction=False)
        pipe.lrange(full, start, -1)
        pipe.llen(full)
        rows, size = pipe.execute()
        return list(rows or []), int(size or 0)

    def clear_list(self, key: str) -> None:
        self.client.delete(self._key(key))

    def acquire_lock(self, key: str, ttl_seconds: int = 60) -> Optional[str]:
        token = uuid.uuid4().hex
        ok = self.client.set(
            self._key(f"lock:{key}"), token, nx=True, ex=max(int(ttl_seconds), 1)
        )
        return token if ok else None

    def refresh_lock(self, key: str, token: str, ttl_seconds: int = 60) -> bool:
        return bool(self.client.eval(
            self._REFRESH_SCRIPT, 1, self._key(f"lock:{key}"), token, max(int(ttl_seconds), 1)
        ))

    def release_lock(self, key: str, token: str) -> bool:
        return bool(self.client.eval(
            self._RELEASE_SCRIPT, 1, self._key(f"lock:{key}"), token
        ))


_BACKEND: MemoryStateBackend | RedisStateBackend | None = None
_BACKEND_LOCK = threading.Lock()


def get_shared_state_backend(*, reset: bool = False):
    """Return one backend per process; Redis is selected only when REDIS_URL is set."""
    global _BACKEND
    with _BACKEND_LOCK:
        if reset:
            _BACKEND = None
        if _BACKEND is not None:
            return _BACKEND
        from config.settings import REDIS_KEY_PREFIX, REDIS_SOCKET_TIMEOUT_SECONDS, REDIS_URL

        if REDIS_URL:
            try:
                _BACKEND = RedisStateBackend(
                    REDIS_URL, REDIS_KEY_PREFIX, REDIS_SOCKET_TIMEOUT_SECONDS
                )
                logger.info("[SharedState] Redis 已连接，实时缓存与任务状态使用共享存储")
                return _BACKEND
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[SharedState] Redis 不可用，降级进程内存: {exc}")
        _BACKEND = MemoryStateBackend(REDIS_KEY_PREFIX)
        return _BACKEND


@dataclass
class TaskLease:
    task_name: str
    token: str
    ttl_seconds: int
    backend: Any
    _stop: threading.Event
    _thread: threading.Thread

    @classmethod
    def acquire(cls, task_name: str, ttl_seconds: int) -> Optional["TaskLease"]:
        backend = get_shared_state_backend()
        token = backend.acquire_lock(f"task:{task_name}", ttl_seconds)
        if not token:
            return None
        stop = threading.Event()

        def heartbeat() -> None:
            interval = max(min(ttl_seconds / 3.0, 60.0), 1.0)
            while not stop.wait(interval):
                try:
                    if not backend.refresh_lock(f"task:{task_name}", token, ttl_seconds):
                        break
                except Exception as exc:  # noqa: BLE001
                    logger.error(f"[SharedState] 任务锁续期失败 {task_name}: {exc}")
                    break

        thread = threading.Thread(target=heartbeat, daemon=True, name=f"lease-{task_name}")
        thread.start()
        return cls(task_name, token, ttl_seconds, backend, stop, thread)

    def release(self) -> None:
        self._stop.set()
        self._thread.join(timeout=0.2)
        try:
            self.backend.release_lock(f"task:{self.task_name}", self.token)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[SharedState] 任务锁释放失败 {self.task_name}: {exc}")


class TaskStateStore:
    """Shared metadata and incremental log storage for one task family."""

    def __init__(self, task_name: str, *, max_log_lines: int = 20000) -> None:
        self.task_name = task_name
        self.max_log_lines = max(int(max_log_lines), 100)
        self.backend = get_shared_state_backend()
        self.fallback = MemoryStateBackend(f"task_fallback:{task_name}")

    @property
    def shared(self) -> bool:
        return bool(self.backend.is_shared)

    def save(self, state: Dict[str, Any]) -> None:
        payload = dict(state)
        payload["storage"] = "redis" if self.shared else "memory"
        payload["heartbeat_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        key = f"task:{self.task_name}:state"
        self.fallback.set_json(key, payload, ttl_seconds=86400 * 7)
        try:
            self.backend.set_json(key, payload, ttl_seconds=86400 * 7)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[SharedState] 任务状态写入 Redis 失败，保留本地镜像: {exc}")

    def load(self) -> Dict[str, Any]:
        key = f"task:{self.task_name}:state"
        try:
            remote = self.backend.get_json(key)
            if remote:
                return dict(remote)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[SharedState] 任务状态读取 Redis 失败，使用本地镜像: {exc}")
        return dict(self.fallback.get_json(key) or {})

    def clear_logs(self) -> None:
        key = f"task:{self.task_name}:logs"
        self.fallback.clear_list(key)
        try:
            self.backend.clear_list(key)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[SharedState] 任务日志清理 Redis 失败: {exc}")

    def append_log(self, line: str) -> int:
        key = f"task:{self.task_name}:logs"
        local_size = self.fallback.append_list(key, line, self.max_log_lines)
        try:
            return self.backend.append_list(key, line, self.max_log_lines)
        except Exception:
            return local_size

    def read_logs(self, since: int = 0) -> tuple[List[str], int]:
        key = f"task:{self.task_name}:logs"
        try:
            return self.backend.read_list(key, since)
        except Exception:
            return self.fallback.read_list(key, since)


__all__ = [
    "MemoryStateBackend",
    "RedisStateBackend",
    "TaskLease",
    "TaskStateStore",
    "get_shared_state_backend",
]
