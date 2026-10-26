"""启动 Redis 兼容缓存服务（RESP over TCP，端口默认 6379）。

为什么需要这个脚本：
- 本机既无 `redis-server` 二进制，也无运行中的 Docker daemon，
  但 L2 缓存必须能**真实验证**（TCP 连接、TTL、SCAN 前缀失效、命中率）。
- 因此用 `fakeredis.TcpFakeServer` 提供 RESP 协议的 TCP 服务：协议与命令子集
  与真 Redis 一致，`redis-py` 客户端无需任何改动。
- **生产替换**：把 `TIANGONG_REDIS_URL` 指向真 Redis（或云 Redis）即可，
  应用代码零改动（见 app/cache.py RedisL2）。

用法：
    python tools/redis_compat.py                 # 127.0.0.1:6379
    python tools/redis_compat.py --port 6380
"""

from __future__ import annotations

import argparse
import signal
import threading
import time

from fakeredis import TcpFakeServer


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=6379)
    args = ap.parse_args()

    srv = TcpFakeServer((args.host, args.port), server_type="redis")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"Redis 兼容服务已启动: redis://{args.host}:{args.port}/0 "
          f"(fakeredis TcpFakeServer, RESP)", flush=True)

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    while not stop.is_set():
        time.sleep(0.5)
    srv.shutdown()
    print("已停止", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
