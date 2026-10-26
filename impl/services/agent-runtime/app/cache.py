"""多级缓存（L1 进程内 TTL + L2 可选外部）。

对标工业实践（MateCloud 二级缓存）：
- L1：进程内 dict + TTL，零依赖，命中路径无 IO
- L2：Redis（redis-py 同步客户端）；未配置 `TIANGONG_REDIS_URL` 时自动关闭
- 支持**预热**：外部（tools/warmup_cache.py）按同一 key 约定预写 L2，
  进程重启后首个请求即可命中（冷启动保护）

纪律：
- 缓存 key 必须带租户/基地维度（多租户安全）
- 命中率/未命中/驱逐计数对外暴露（可观测），并区分 L1/L2 命中
- 只缓存**只读查询**；写路径直接穿透并失效相关前缀
- L2 任何异常都被吞掉并降级为 L1（安全与可用性优先）
"""

from __future__ import annotations

import json
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
    l1_hits: int = 0
    l2_hits: int = 0

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0


class RedisL2:
    """Redis 二级缓存（redis-py 同步客户端）。

    - 值统一 JSON 序列化（跨语言可读，便于人工排查与预热脚本对齐）
    - 异常不吞：向上抛，由 MultiLevelCache 统一降级
    - 前缀失效用 SCAN（不用 KEYS，避免阻塞 Redis 主线程）
    """

    def __init__(self, url: str = "redis://127.0.0.1:6379/0",
                 prefix: str = "tg:", timeout: float = 0.3,
                 client: Any | None = None) -> None:
        if client is not None:          # 测试注入（fakeredis 等）
            self._r = client
        else:
            import redis  # 延迟导入：未启用 L2 时不引入依赖

            self._r = redis.Redis.from_url(
                url, socket_timeout=timeout, socket_connect_timeout=timeout,
                decode_responses=True)
        self._prefix = prefix

    def _k(self, key: str) -> str:
        return f"{self._prefix}{key}"

    def get(self, key: str) -> Any:
        raw = self._r.get(self._k(key))
        return None if raw is None else json.loads(raw)

    def set(self, key: str, value: Any, ttl: float | None = None) -> None:
        raw = json.dumps(value, ensure_ascii=False, default=str)
        if ttl and ttl > 0:
            self._r.set(self._k(key), raw, ex=max(1, int(ttl)))
        else:
            self._r.set(self._k(key), raw)

    def delete_prefix(self, prefix: str) -> int:
        n = 0
        for k in self._r.scan_iter(match=f"{self._k(prefix)}*", count=500):
            n += int(self._r.delete(k))
        return n

    def ping(self) -> bool:
        return bool(self._r.ping())

    def dbsize(self) -> int:
        return int(self._r.dbsize())


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
                    self.stats.l1_hits += 1
                    return True, e.value
                del self._l1[key]
                self.stats.evictions += 1
        if self._l2 is not None:
            try:
                v = self._l2.get(key)
            except Exception:
                v = None
            if v is not None:
                # 只回填 L1：**不得**用 L1 的短 TTL 覆盖 L2 的长 TTL，
                # 否则预热键被一次读操作"降级"成 5s，随后集体过期（实测踩坑）。
                self._set_l1(key, v, self._default_ttl)
                with self._lock:
                    self.stats.hits += 1
                    self.stats.l2_hits += 1
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
    def _set_l1(self, key: str, value: Any, ttl: float) -> None:
        with self._lock:
            if len(self._l1) >= self._max and key not in self._l1:
                # 简单驱逐：最早过期的键
                oldest = min(self._l1, key=lambda k: self._l1[k].expires_at)
                del self._l1[oldest]
                self.stats.evictions += 1
            self._l1[key] = _Entry(value, time.monotonic() + ttl)
            self.stats.writes += 1

    def set(self, key: str, value: Any, ttl: float | None = None) -> None:
        ttl = self._default_ttl if ttl is None else ttl
        self._set_l1(key, value, ttl)
        if self._l2 is not None:
            # L2 故障不得影响主链路（降级纪律）
            with suppress(Exception):
                self._l2.set(key, value, ttl)

    def invalidate_prefix(self, prefix: str) -> int:
        """前缀失效（写操作后调用）：L1 与 L2 同时失效。"""
        n = 0
        with self._lock:
            for k in [k for k in self._l1 if k.startswith(prefix)]:
                del self._l1[k]
                n += 1
        if self._l2 is not None and hasattr(self._l2, "delete_prefix"):
            with suppress(Exception):
                self._l2.delete_prefix(prefix)   # L2 故障不影响主链路
        return n

    def clear(self) -> None:
        with self._lock:
            self._l1.clear()
        if self._l2 is not None and hasattr(self._l2, "delete_prefix"):
            with suppress(Exception):
                self._l2.delete_prefix("")

    @property
    def l2_available(self) -> bool:
        return self._l2 is not None
