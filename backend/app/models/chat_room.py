"""企业聊天室：房间、成员、消息。

与 AI 问答的 Conversation/Message 分离——后者带 UsageLog/上下文压缩/单属主语义，
不适用于「人与人」的多成员聊天。仅复用附件存储与签名 URL 机制。
"""
from __future__ import annotations

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.base import IdMixin, TenantMixin, TimestampMixin


class ChatRoom(Base, IdMixin, TimestampMixin, TenantMixin):
    """聊天室。kind: group=群聊（含默认大群）/ direct=一对私聊。"""

    __tablename__ = "chat_room"

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), default="group", index=True)  # group/direct
    owner_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)  # 默认大群（全员可见）
    announcement: Mapped[str | None] = mapped_column(Text, nullable=True)  # 群公告（置顶展示）
    # 私聊时存对方 user_id（direct 房间按 (user_a,user_b) 唯一）
    peer_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # 扩展：绑定的机器人 {agent_id: {name, ...}} 等
    settings: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    last_message_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # 毫秒
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="active")  # active/archived

    __table_args__ = (
        Index("ix_chat_room_direct", "tenant_id", "peer_user_id"),
        # 「先查后建」在没有唯一约束时挡不住并发：两个用户同时点开私聊会各建一个房间，
        # 消息从此分裂在两个房间里。业务键（租户 + 归一化后的用户对）上必须有唯一约束，
        # 否则查询与插入之间的窗口永远存在。
        # 带上 kind/status 是为了让「群聊」和「已解散的旧房间」不占键：
        #   部分唯一索引的谓词不参与唯一性比较，只能靠列本身区分。
        Index(
            "uq_chat_room_direct_pair",
            "tenant_id", "owner_id", "peer_user_id", "kind", "status",
            unique=True,
            sqlite_where=text("kind = 'direct' AND status = 'active'"),
            postgresql_where=text("kind = 'direct' AND status = 'active'"),
        ),
        # 每个租户只能有一个「活跃的」默认大群：否则 ensure_default_room 的
        # 「查不到就建」在多人首次登录时会建出多个 is_default 房间。
        # 谓词必须带上 status：ensure_default_room 只认 active，若约束只看 is_default，
        # 已归档的旧默认大群仍占着键，重复行既归档不掉、也建不出新房间。
        Index(
            "uq_chat_room_tenant_default",
            "tenant_id",
            unique=True,
            sqlite_where=text("is_default = 1 AND status = 'active'"),
            postgresql_where=text("is_default AND status = 'active'"),
        ),
    )


class ChatRoomMember(Base, IdMixin, TenantMixin, TimestampMixin):
    """房间成员。role: owner=群主 / admin=群管理员 / member=普通成员。

    principal 支持把「机器人」也加进群：principal_type=agent 时 principal_id=Agent.id。
    """

    __tablename__ = "chat_room_member"

    room_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    user_id: Mapped[int | None] = mapped_column(BigInteger, index=True, nullable=True)  # 人类成员
    # 机器人成员：principal_type=agent + principal_id=Agent.id
    agent_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    role: Mapped[str] = mapped_column(String(16), default="member")  # owner/admin/member
    muted_until: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # 禁言到期（毫秒）
    last_read_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # 已读水位（毫秒）
    added_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    __table_args__ = (
        UniqueConstraint("room_id", "user_id", name="uq_chat_member_room_user"),
    )


class ChatUserRemark(Base, IdMixin, TimestampMixin, TenantMixin):
    """用户对某人的私聊备注（仅本人可见，不改对方账号）。

    remark 为空/删除记录即恢复显示对方真实姓名。
    """

    __tablename__ = "chat_user_remark"

    owner_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)  # 设置者
    peer_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)  # 被备注的人
    remark: Mapped[str] = mapped_column(String(64), nullable=False, default="")

    __table_args__ = (
        UniqueConstraint("owner_id", "peer_id", name="uq_chat_remark_owner_peer"),
    )


class ChatAnnouncement(Base, IdMixin, TimestampMixin, TenantMixin):
    """群公告记录（QQ 式：一个群可有多条公告，支持编辑/删除）。

    ChatRoom.announcement 保留为「最后一条公告」的兼容快照，展示以本表为准。
    """

    __tablename__ = "chat_announcement"

    room_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # 发布者 user_id
    updated_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # 最后编辑者
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)  # 置顶（长期展示）

    __table_args__ = (
        Index("ix_chat_ann_room", "room_id", "id"),
    )


class ChatMessage(Base, IdMixin, TenantMixin):
    """聊天消息。撤回用软删（revoked + 保留占位），置顶用 pinned。"""

    __tablename__ = "chat_message"

    room_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    sender_id: Mapped[int | None] = mapped_column(BigInteger, index=True, nullable=True)  # 人类发送者
    # 机器人发送的消息：sender_type=agent + sender_id=Agent.id
    sender_type: Mapped[str] = mapped_column(String(16), default="user")  # user/agent/system
    content: Mapped[str] = mapped_column(Text, default="")
    content_type: Mapped[str] = mapped_column(String(16), default="text")  # text/system
    # 附件（复用聊天附件结构：{type,file_key,name,mime,size,url}）
    attachments: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # @提及的用户 id 列表
    mentions: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # 引用的消息 id（回复某条）
    reply_to_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    revoked_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    revoked_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[int] = mapped_column(BigInteger, default=0, index=True)  # 毫秒

    __table_args__ = (
        Index("ix_chat_msg_room_time", "room_id", "created_at"),
    )
