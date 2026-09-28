"""所有宿主可见时间戳的单一时钟源：北京时间(UTC+8)。

runner 容器时区不受控(通常 UTC)，日志面板/事件流/遥测各写各的 gmtime
曾让同一事件在用户眼里差 8 小时；故统一从这里取时间，禁止散落 gmtime/
utcnow。用固定偏移而非 localtime——Asia/Shanghai 无夏令时，固定 +8
恒等北京时间，容器时区如何设置都不受影响。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

BEIJING = timezone(timedelta(hours=8), "Asia/Shanghai")


def stamp() -> str:
    """日志戳：2026-09-28 11:18:04"""
    return datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M:%S")


def compact() -> str:
    """目录名戳：20260928-111804"""
    return datetime.now(BEIJING).strftime("%Y%m%d-%H%M%S")


def iso() -> str:
    """机器可读戳：2026-09-28T11:18:04.123456+08:00（带偏移，可逆）"""
    return datetime.now(BEIJING).isoformat()
