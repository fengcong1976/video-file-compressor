"""规范运行时支撑：0 联网自检。

注意：NetworkAudit 是运行时连接计数，不是沙箱。它与静态事实（本包不导入
urllib / requests / http.client，核心只调用本地 ffmpeg 子进程）共同构成
「0 联网」证据。
"""
from __future__ import annotations

import socket


class NetworkAudit:
    """统计本次运行中的主动外连尝试；界面据此显示联网次数。"""

    def __init__(self):
        self.attempts = 0
        self._patched = False
        self._orig_connect = None
        self._orig_create = None

    def __enter__(self):
        audit = self
        try:
            self._orig_connect = socket.socket.connect
            self._orig_create = socket.create_connection
            orig_connect, orig_create = self._orig_connect, self._orig_create

            def counting_connect(sock, address):
                audit.attempts += 1
                return orig_connect(sock, address)

            def counting_create(*args, **kwargs):
                audit.attempts += 1
                return orig_create(*args, **kwargs)

            socket.socket.connect = counting_connect
            socket.create_connection = counting_create
            self._patched = True
        except (AttributeError, TypeError):
            self._patched = False
        return self

    def __exit__(self, *exc):
        if self._patched:
            socket.socket.connect = self._orig_connect
            socket.create_connection = self._orig_create
        return False

    def summary(self) -> str:
        """自检没挂上时如实说「不可用」，不拿一个没验证过的 0 当证据。"""
        if not self._patched:
            return "本次未发起任何连接（计数自检不可用：当前环境无法挂接 socket）"
        if self.attempts == 0:
            return "本次 0 次联网 / 未发起任何连接"
        return f"本次 {self.attempts} 次外连尝试（本工具无需联网，请核对来源）"
