"""并发写控制：乐观锁前置校验。

SQLAlchemy 的 ``version_id_col`` 已经保证「并发写不会静默覆盖」（冲突时 409），
但那是**提交时**才发现。这里再提供一次**写入前**的显式校验：

- 前端把上次读到的 ``row_version`` 一起提交，若已过期立刻返回 409，
  省掉一次无谓的写入与事务回滚；
- 响应里带上最新的 ``current_row_version``，前端可以直接用新版本重试或提示刷新。

不传 ``row_version`` 时保持旧行为（兼容既有客户端与脚本）。
"""
from __future__ import annotations

from typing import Any

from app.core.errors import StaleWriteError


def check_row_version(entity: Any, expected: int | None) -> None:
    """比较客户端提交的版本号与库中当前值；不一致抛 409。"""
    if expected is None:
        return
    current = getattr(entity, "row_version", None)
    if current is None or int(current) == int(expected):
        return
    raise StaleWriteError(
        "内容已被他人修改，请刷新后重试",
        detail={
            "reason": "stale_write",
            "expected_row_version": int(expected),
            "current_row_version": int(current),
        },
    )
