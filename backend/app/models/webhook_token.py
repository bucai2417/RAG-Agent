"""入站 Webhook 令牌：外部系统 POST /hooks/{token} 触发定时任务或工作流。

token 只存哈希（仿 ApiKey 的 key_hash 模式），明文仅创建时返回一次。

设计要点（勿凭直觉改动）：

1. **一个令牌表服务两类目标**（`kind` 区分）。工作流与定时任务的触发语义不同
   （工作流要同步返回结果），但令牌生命周期、租户隔离、启停开关完全一致，
   拆两张表只会带来重复的 CRUD。

2. **`path` 支持自定义可读路径**。token 本身是随机串，客户系统对接时不该看到
   `hooks/Xk9-aB3...` 这种。允许指定 `order-sync` 之类的别名，便于对方配置与排障；
   别名全局唯一，冲突时报错而不是静默覆盖。

3. **`task_id` 改为可空**。绑定工作流的令牌没有 task_id —— 老数据全是定时任务，
   允许为空是向后兼容的最小改动。
"""
from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.base import IdMixin, TenantMixin, TimestampMixin


class WebhookToken(Base, IdMixin, TenantMixin, TimestampMixin):
    __tablename__ = "webhook_token"

    # 触发目标：scheduled_task（默认，兼容历史数据）| workflow
    kind: Mapped[str] = mapped_column(String(16), default="scheduled_task")
    # 定时任务 id（kind=scheduled_task 时必填）
    task_id: Mapped[int | None] = mapped_column(BigInteger, index=True, nullable=True)
    # 工作流型智能体 id（kind=workflow 时必填）
    agent_id: Mapped[int | None] = mapped_column(BigInteger, index=True, nullable=True)
    # 自定义可读路径：/api/v1/hooks/{path}，为空则用 token 明文
    path: Mapped[str | None] = mapped_column(String(64), unique=True, index=True, nullable=True)

    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), default="默认")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # 调用统计（外部对接排障用）
    call_count: Mapped[int] = mapped_column(BigInteger, default=0)
    last_called_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # 创建者：工作流以他的身份执行（继承权限，不放大）；为空则用租户级主体
    created_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # 限流：每分钟允许的调用次数（0=不限）。外部系统狂调会打爆 LLM 配额，
    # 所以按「接入点」而不是「工作流」限流 —— 同一工作流给不同对接方可配不同额度。
    rate_limit_per_min: Mapped[int] = mapped_column(BigInteger, default=60)
    # 异步回调地址（可选）：设置后调用方可用 mode=async，工作流跑完 POST 到这里。
    # 允许为空 —— 同步模式（默认）不需要回调。
    callback_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 回调签名密钥（加密存储）：回调时带 HMAC 头，供对方验证来源
    callback_secret: Mapped[str | None] = mapped_column(String(512), nullable=True)
