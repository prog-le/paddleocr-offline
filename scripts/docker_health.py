#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""容器健康检查：只依赖标准库，避免为 healthcheck 额外装 curl。

退出码：0 = 容器存活（200 或有响应的 503）；1 = 无响应。
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

URL = "http://127.0.0.1:8000/api/health"


def main() -> int:
    try:
        with urllib.request.urlopen(URL, timeout=5) as resp:
            body = resp.read().decode("utf-8", "replace")
            print(body[:400])
            return 0
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        print(f"HTTP {exc.code}: {body[:400]}")
        # 503 = 服务在跑但降级，容器本身没问题
        return 0 if exc.code == 503 else 1
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"status": "down", "error": str(exc)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
