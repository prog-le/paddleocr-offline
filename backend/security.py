"""管理后台鉴权：内存会话 + Cookie + 简单登录限流。

说明：
  * 站点本身对内网开放、无需登录即可用；仅 /api/admin/* 需要管理员登录。
  * 会话保存在进程内存，重启后需要重新登录（单进程轻量工具够用）。
  * 登录失败按客户端 IP 计数，超过阈值锁定一段时间，避免弱口令被爆破。
"""

from __future__ import annotations

import hmac
import secrets
import threading
import time
from typing import Dict, Optional, Tuple

from .settings import Settings

COOKIE_NAME = "pdx_admin"


class AuthError(Exception):
    pass


class AdminAuth:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.username = str(settings.get("server.admin_username", "admin"))
        self.password = str(settings.get("server.admin_password", ""))
        self.ttl = int(settings.get("server.session_ttl_sec", 7200) or 7200)
        self.max_attempts = int(settings.get("server.max_login_attempts", 10) or 10)
        self.lock_sec = int(settings.get("server.login_lock_sec", 300) or 300)
        self._sessions: Dict[str, Tuple[str, float]] = {}
        self._attempts: Dict[str, Tuple[int, float]] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ 登录
    def login(self, username: str, password: str, client_ip: str = "-") -> str:
        with self._lock:
            count, locked_until = self._attempts.get(client_ip, (0, 0.0))
            if locked_until > time.time():
                wait = int(locked_until - time.time())
                raise AuthError(f"登录失败次数过多，请 {wait} 秒后再试")

        ok = hmac.compare_digest(str(username or ""), self.username) and hmac.compare_digest(
            str(password or ""), self.password
        )
        if not ok or not self.password:
            with self._lock:
                count, _ = self._attempts.get(client_ip, (0, 0.0))
                count += 1
                locked = time.time() + self.lock_sec if count >= self.max_attempts else 0.0
                self._attempts[client_ip] = (count if not locked else 0, locked)
            raise AuthError("用户名或密码错误")

        token = secrets.token_urlsafe(32)
        with self._lock:
            self._attempts.pop(client_ip, None)
            self._sessions[token] = (self.username, time.time() + self.ttl)
        return token

    # ------------------------------------------------------------------ 会话
    def validate(self, token: Optional[str]) -> bool:
        if not token:
            return False
        now = time.time()
        with self._lock:
            item = self._sessions.get(token)
            if not item:
                return False
            if item[1] < now:
                self._sessions.pop(token, None)
                return False
            return True

    def destroy(self, token: Optional[str]) -> None:
        if not token:
            return
        with self._lock:
            self._sessions.pop(token, None)

    def purge_expired(self) -> None:
        now = time.time()
        with self._lock:
            for token, (_user, exp) in list(self._sessions.items()):
                if exp < now:
                    self._sessions.pop(token, None)
            for ip, (_count, locked_until) in list(self._attempts.items()):
                if locked_until and locked_until < now:
                    self._attempts.pop(ip, None)

    @property
    def cookie_max_age(self) -> int:
        return self.ttl
