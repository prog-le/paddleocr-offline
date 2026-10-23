"""匿名客户端会话（不登录也要隔离数据）。

背景：本工具前台不需要登录，但"不同浏览器/不同人不能互相看到上传的文件与识别结果"。
做法：为每个浏览器签发一个**不可猜测、带 HMAC 签名**的 session id（Cookie），
任务创建时记下 owner=session_id，公开接口只返回 owner 匹配的任务。

设计取舍：
  * 无服务端会话表：sid 用 session_secret 做 HMAC 签名，服务重启后仍然有效，
    伪造需要密钥，集群/多进程也不会漂移；
  * 不存用户身份，只做"浏览器实例"维度的隔离——清 Cookie 或换浏览器即看不到旧任务
    （数据本身仍按 storage.retention_hours 保留，管理员在后台可见）；
  * 管理员使用独立的 pdx_admin Cookie，走 /api/admin/*，可见全部任务。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Optional, Tuple

COOKIE_NAME = "pdx_sid"
SID_BYTES = 24  # token_urlsafe(24) -> 32 字符


class ClientSessions:
    """签发 / 校验匿名会话 Cookie。"""

    def __init__(self, secret: str, cookie_name: str = COOKIE_NAME, ttl_hours: float = 24):
        self.secret = (secret or "paddlex-ocr-web").encode("utf-8")
        self.cookie_name = cookie_name
        self.ttl_hours = max(float(ttl_hours or 24), 0.1)

    # ------------------------------------------------------------------ 签名
    def _sign(self, sid: str) -> str:
        return hmac.new(self.secret, sid.encode("utf-8"), hashlib.sha256).hexdigest()[:32]

    def cookie_value(self, sid: str) -> str:
        return f"{sid}.{self._sign(sid)}"

    def verify(self, value: Optional[str]) -> Optional[str]:
        """校验 Cookie 值，合法返回 sid，否则 None。"""
        if not value or "." not in value:
            return None
        sid, _, signature = value.rpartition(".")
        if not sid or not signature:
            return None
        if not hmac.compare_digest(self._sign(sid), signature):
            return None
        return sid

    # ------------------------------------------------------------------ 解析
    def resolve(self, cookie_value: Optional[str]) -> Tuple[str, bool]:
        """返回 (sid, 是否新建)。Cookie 缺失或被篡改时签发新 sid。"""
        sid = self.verify(cookie_value)
        if sid:
            return sid, False
        return secrets.token_urlsafe(SID_BYTES), True

    @property
    def max_age(self) -> int:
        return int(self.ttl_hours * 3600)

    @staticmethod
    def short(sid: Optional[str], length: int = 8) -> str:
        """给后台展示用的短标识。"""
        if not sid:
            return "-"
        return sid[:length]
