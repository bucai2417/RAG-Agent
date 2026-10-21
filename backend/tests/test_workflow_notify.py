"""工作流结果回推渠道：目标解析、发送、失败留痕、补发。"""
from __future__ import annotations

import asyncio

import pytest

from app.services import workflow_notify as N


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _Run:
    def __init__(self, **kw):
        self.id = kw.get("id", 1)
        self.tenant_id = kw.get("tenant_id", 1)
        self.conversation_id = kw.get("conversation_id")
        self.output = kw.get("output")


class _CU:
    def __init__(self, channel="wework", external_id="u1"):
        self.channel = channel
        self.external_id = external_id
        self.conversation_id = 10


class _Ch:
    def __init__(self, id_=5, kind="wework"):
        self.id = id_
        self.kind = kind
        self.status = "active"


class _DB:
    """按查询参数返回预设对象的最小替身。"""

    def __init__(self, cu=None, ch=None):
        self._cu = cu
        self._ch = ch
        self.committed = 0
        self.flushed = 0

    async def execute(self, stmt):
        # 第一次查 ChannelUser，第二次查 Channel
        obj = self._cu if not getattr(self, "_used", False) else self._ch
        self._used = True

        class _R:
            def __init__(self, o):
                self._o = o

            def scalars(self):
                o = self._o

                class _S:
                    def first(self_inner):
                        return o
                return _S()
        return _R(obj)

    async def flush(self):
        self.flushed += 1

    async def commit(self):
        self.committed += 1

    async def rollback(self):
        pass


# ============================================================ 目标解析


def test_resolve_target_none_without_conversation():
    r = _run(N._resolve_target(_DB(), _Run(conversation_id=None)))
    assert r is None


def test_resolve_target_returns_channel_and_user():
    db = _DB(cu=_CU(), ch=_Ch(id_=7))
    r = _run(N._resolve_target(db, _Run(conversation_id=10)))
    assert r["channel_id"] == 7
    assert r["channel_kind"] == "wework"
    assert r["external_user"] == "u1"


def test_resolve_target_without_channel_record():
    """渠道被删除时仍返回身份，但 channel_id 为 None（由 kind 兜底发送）。"""
    r = _run(N._resolve_target(_DB(cu=_CU(), ch=None), _Run(conversation_id=10)))
    assert r["channel_id"] is None
    assert r["channel_kind"] == "wework"


# ============================================================ 发送


def test_push_sends_and_records(monkeypatch):
    sent: list = []

    async def _fake_send(channel_id, out):
        sent.append((channel_id, out))
        return True

    from app.channels import manager as mgr

    monkeypatch.setattr(mgr.channel_manager, "send", _fake_send)

    db = _DB(cu=_CU(), ch=_Ch(id_=7))
    r = _Run(id=3, conversation_id=10, output={"output": "x"})
    ok = _run(N.push_workflow_result(db, run=r, text="已处理", prefix="提示："))

    assert ok is True
    assert sent and sent[0][0] == 7
    assert sent[0][1].content == "提示：已处理"
    assert sent[0][1].user_id == "u1"
    assert r.output["channel_reply"]["sent"] is True


def test_push_empty_text_returns_false():
    ok = _run(N.push_workflow_result(_DB(), run=_Run(conversation_id=10), text="   "))
    assert ok is False


def test_push_without_target_returns_false():
    r = _Run(conversation_id=None)
    ok = _run(N.push_workflow_result(_DB(), run=r, text="hi"))
    assert ok is False


def test_push_falls_back_by_kind(monkeypatch):
    """渠道 id 失效（渠道重建）时按 kind 兜底发送。"""
    calls: list = []

    async def _send(channel_id, out):
        calls.append(("send", channel_id))
        return False

    async def _fallback(channel_id, kind, out):
        calls.append(("fallback", kind))
        return True

    from app.channels import manager as mgr

    monkeypatch.setattr(mgr.channel_manager, "send", _send)
    monkeypatch.setattr(mgr.channel_manager, "send_with_fallback", _fallback)

    db = _DB(cu=_CU(), ch=None)  # 无渠道记录 → channel_id=None
    ok = _run(N.push_workflow_result(db, run=_Run(conversation_id=10), text="hi"))
    assert ok is True
    assert ("fallback", "wework") in calls


def test_push_records_failure(monkeypatch):
    """发送失败也要留痕，便于后台补发。"""
    async def _send(channel_id, out):
        return False

    async def _fallback(channel_id, kind, out):
        return False

    from app.channels import manager as mgr

    monkeypatch.setattr(mgr.channel_manager, "send", _send)
    monkeypatch.setattr(mgr.channel_manager, "send_with_fallback", _fallback)

    db = _DB(cu=_CU(), ch=_Ch())
    r = _Run(id=9, conversation_id=10, output={})
    ok = _run(N.push_workflow_result(db, run=r, text="结果"))
    assert ok is False
    assert r.output["channel_reply"]["sent"] is False
    assert r.output["channel_reply"]["text"] == "结果"


# ============================================================ 审批决定


def test_notify_reject_sends_fixed_text(monkeypatch):
    seen: list = []

    async def _push(db, *, run, text, prefix=""):
        seen.append((text, prefix))
        return True

    monkeypatch.setattr(N, "push_workflow_result", _push)
    _run(N.notify_approval_decision(_DB(), run=_Run(), decision="reject"))
    assert seen and "未通过审批" in seen[0][0]


def test_notify_approve_uses_result_text(monkeypatch):
    seen: list = []

    async def _push(db, *, run, text, prefix=""):
        seen.append((text, prefix))
        return True

    monkeypatch.setattr(N, "push_workflow_result", _push)
    _run(N.notify_approval_decision(_DB(), run=_Run(), decision="approve", result_text="订单已发货"))
    assert seen[0][0] == "订单已发货"
    assert "已处理完成" in seen[0][1]


def test_notify_approve_without_result_text(monkeypatch):
    seen: list = []

    async def _push(db, *, run, text, prefix=""):
        seen.append(text)
        return True

    monkeypatch.setattr(N, "push_workflow_result", _push)
    _run(N.notify_approval_decision(_DB(), run=_Run(), decision="approve", result_text=None))
    assert seen == ["已处理完成。"]


# ============================================================ 补发


def test_resend_uses_cached_text(monkeypatch):
    seen: list = []

    async def _push(db, *, run, text, prefix=""):
        seen.append(text)
        return True

    monkeypatch.setattr(N, "push_workflow_result", _push)
    r = _Run(output={"channel_reply": {"text": "缓存的结果"}})
    ok = _run(N.resend_workflow_result(_DB(), run=r))
    assert ok is True and seen == ["缓存的结果"]


def test_resend_without_cache_returns_false():
    ok = _run(N.resend_workflow_result(_DB(), run=_Run(output={})))
    assert ok is False


# ============================================================ 接口注册


def test_resend_endpoint_registered():
    from app.main import app

    paths = app.openapi()["paths"]
    assert "/api/v1/workflow-runs/{run_id}/resend" in paths
