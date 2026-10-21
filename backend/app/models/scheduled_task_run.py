"""定时任务执行历史（每次执行落一条）。"""
from __future__ import annotations

from sqlalchemy import JSON, BigInteger, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.base import IdMixin, TenantMixin, TimestampMixin


class ScheduledTaskRun(Base, IdMixin, TimestampMixin, TenantMixin):
    __tablename__ = "scheduled_task_run"

    task_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    # running/success/failed/waiting（waiting=工作流停在审批节点，等人工处理）
    status: Mapped[str] = mapped_column(String(16), default="running")
    started_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # ms
    finished_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, default=1)  # 第几次尝试
    workflow_run_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # 本轮产物（工作流产出的文件）：[{file_key, name, mime, size}]，供通知附带
    attachments: Mapped[list | None] = mapped_column(JSON, nullable=True)
