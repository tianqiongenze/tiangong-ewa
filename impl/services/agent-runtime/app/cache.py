"""多级缓存（L1 进程内 TTL + L2 可选外部）。

对标工业实践（MateCloud 二级缓存）：
- L1：进程内 dict + TTL，零依赖，命中路径无 IO
- L2：可选（Redis 等同步客户端），接口预留，本期不引入重依赖

纪律：
- 缓存 key 必须带租户/基地维度（多租户安全）
- 命中率/未命中/驱逐计数对外暴露（可观测）
- 只缓存**只读查询**；写路径直接穿透并失效相关前缀
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any


@dataclass
class _Entry:
    value: Any
    expires_at: float


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    evictions: int = 0
    writes: int = 0

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0


class MultiLevelCache:
    """两级缓存：L1 进程内（必选）+ L2 外部（可选）。"""

    def __init__(self, l2: Any | None = None, default_ttl: float = 5.0,
                 max_entries: int = 10_000) -> None:
        self._l1: dict[str, _Entry] = {}
        self._l2 = l2                      # 同步客户端：get/set/delete
        self._default_ttl = default_ttl
        self._max = max_entries
        self._lock = threading.Lock()
        self.stats = CacheStats()

    # ---------------------------------------------------------------- 读
    def get(self, key: str) -> tuple[bool, Any]:
        """返回 (命中, 值)。"""
        now = time.monotonic()
        with self._lock:
            e = self._l1.get(key)
            if e is not None:
                if e.expires_at > now:
                    self.stats.hits += 1
                    return True, e.value
                del self._l1[key]
                self.stats.evictions += 1
        if self._l2 is not None:
            try:
                v = self._l2.get(key)
            except Exception:
                v = None
            if v is not None:
                self.set(key, v)
                with self._lock:
                    self.stats.hits += 1
                return True, v
        with self._lock:
            self.stats.misses += 1
        return False, None

    def get_or_load(self, key: str, loader: Callable[[], Any],
                    ttl: float | None = None) -> Any:
        """缓存穿透加载：miss → loader → 回填。"""
        ok, v = self.get(key)
        if ok:
            return v
        v = loader()
        self.set(key, v, ttl)
        return v

    # ---------------------------------------------------------------- 写
    def set(self, key: str, value: Any, ttl: float | None = None) -> None:
        ttl = self._default_ttl if ttl is None else ttl
        with self._lock:
            if len(self._l1) >= self._max and key not in self._l1:
                # 简单驱逐：最早过期的键
                oldest = min(self._l1, key=lambda k: self._l1[k].expires_at)
                del self._l1[oldest]
                self.stats.evictions += 1
            self._l1[key] = _Entry(value, time.monotonic() + ttl)
            self.stats.writes += 1
        if self._l2 is not None:
            # L2 故障不得影响主链路（降级纪律）
            with suppress(Exception):
                self._l2.set(key, value, ttl)

    def invalidate_prefix(self, prefix: str) -> int:
        """前缀失效（写操作后调用）。"""
        n = 0
        with self._lock:
            for k in [k for k in self._l1 if k.startswith(prefix)]:
                del self._l1[k]
                n += 1
        return n

    def clear(self) -> None:
        with self._lock:
            self._l1.clear()
