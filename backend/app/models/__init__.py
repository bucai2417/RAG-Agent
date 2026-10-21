"""模型汇总导出，确保 Base.metadata 完整注册。"""
from app.models.agent import (
    Agent,
    AgentAction,
    AgentMode,
    AgentVersion,
    NodeRun,
    Skill,
    SkillPackage,
    Tool,
    Workflow,
    WorkflowRun,
    WorkflowTest,
    WorkflowCallbackDelivery,
    WorkflowTemplate,
    WorkflowTemplateRating,
    WorkflowVersion,
)
from app.models.audit import AuditLog
from app.models.api_key import ApiKey
from app.models.artifact import Artifact
from app.models.channel import Channel
from app.models.channel_user import ChannelUser
from app.models.email_source import EmailSource
from app.models.mail_account import MailAccount
from app.models.mail_message import MailMessage
from app.models.eval import EvalDataset, EvalQuestion, EvalResult, EvalRun
from app.models.event_subscription import EventSubscription
from app.models.mcp import McpServer
from app.models.scheduled_task import ScheduledTask
from app.models.service_ticket import ServiceTicket, ServiceTicketQuickReply
from app.models.sso import SsoConfig
from app.models.system_setting import SystemSetting
from app.models.base import BigIntPK, IdMixin, SoftDeleteMixin, TenantMixin, TimestampMixin, utcnow
from app.models.conversation import Conversation, Message
from app.models.knowledge_base import (
    VIS_KB_DEFAULT,
    VIS_KB_PUBLIC,
    VIS_RESTRICTED,
    Chunk,
    Document,
    DocumentACL,
    DocumentFolder,
    DocumentVersion,
    KBMember,
    KnowledgeBase,
)
from app.models.model_provider import ModelConfig, ModelProvider, UsageLog
from app.models.chat_room import ChatAnnouncement, ChatMessage, ChatRoom, ChatRoomMember, ChatUserRemark
from app.models.notification import Notification
from app.models.notify_channel import NotifyChannel
from app.models.persona_template import AgentPersonaTemplate
from app.models.record_template import RecordEntry, RecordTemplate
from app.models.reminder import Reminder
from app.models.retrieval_miss import RetrievalMiss
from app.models.scheduled_task_run import ScheduledTaskRun
from app.models.tenant import Department, Tenant
from app.models.webhook_token import WebhookToken
from app.models.user import (
    Permission,
    Role,
    RolePermission,
    User,
    UserGroup,
    UserGroupMember,
    UserRole,
)

__all__ = [
    "Tenant",
    "Department",
    "User",
    "UserGroup",
    "UserGroupMember",
    "Role",
    "Permission",
    "RolePermission",
    "UserRole",
    "KnowledgeBase",
    "KBMember",
    "Document",
    "DocumentACL",
    "DocumentFolder",
    "DocumentVersion",
    "Chunk",
    "VIS_KB_DEFAULT",
    "VIS_KB_PUBLIC",
    "VIS_RESTRICTED",
    "Conversation",
    "Message",
    "ChatRoom",
    "ChatAnnouncement",
    "ChatUserRemark",
    "ChatRoomMember",
    "ChatMessage",
    "ModelProvider",
    "ModelConfig",
    "UsageLog",
    # agent platform
    "Agent",
    "AgentMode",
    "AgentAction",
    "AgentVersion",
    "Skill",
    "SkillPackage",
    "Tool",
    "Workflow",
    "WorkflowRun",
    "WorkflowTest",
    "WorkflowCallbackDelivery",
    "WorkflowTemplate",
    "WorkflowTemplateRating",
    "WorkflowVersion",
    "NodeRun",
    "AuditLog",
    "ApiKey",
    "Artifact",
    "ScheduledTask",
    "ServiceTicket",
    "ServiceTicketQuickReply",
    "ScheduledTaskRun",
    "Notification",
    "NotifyChannel",
    "AgentPersonaTemplate",
    "RecordTemplate",
    "RecordEntry",
    "Reminder",
    "RetrievalMiss",
    "EvalDataset",
    "EvalQuestion",
    "EvalRun",
    "EvalResult",
    "EventSubscription",
    "WebhookToken",
    "Channel",
    "ChannelUser",
    "SsoConfig",
    "McpServer",
    "EmailSource",
    "MailAccount",
    "MailMessage",
    "SystemSetting",
    "IdMixin",
    "TimestampMixin",
    "SoftDeleteMixin",
    "TenantMixin",
    "utcnow",
]
