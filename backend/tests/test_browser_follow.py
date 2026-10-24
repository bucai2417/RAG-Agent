"""浏览器：新标签跟随、会话内标签记忆、JS 对话框回显。

这三件事共同的失败形态是「AI 觉得点了没反应」：

- 点击 target=_blank 后新开了标签，AI 仍在旧标签上找元素 → 找不到
- 记住的标签是**进程级**的，多用户下 A 的会话把 B 带到别人的页面上 → 越权
- confirm 被浏览器端自动点掉，AI 不知道那个「确定」是自己被代点的 → 推理前提错误

不连浏览器、不起进程、不联网。
"""
from __future__ import annotations

import asyncio

from app.agents.tools.base import ToolContext
from app.core.errors import ValidationError
from app.services import cdp_client as C


def _tab(i: str, title: str, url: str, type_: str = "page") -> C.Tab:
    return C.Tab(id=i, title=title, url=url, ws_url=f"ws://{i}", type=type_)


def _ctx(conv: int | None = 1, user: int = 1) -> ToolContext:
    return ToolContext(db=None, ps=None, tenant_id=1, user_id=user, conversation_id=conv)  # type: ignore[arg-type]


# ---------------------------------------------------------------- 标签记忆


class TestActiveTab:
    def test_remembered_tab_wins_over_first(self):
        """未指定 target 时回到上次操作的标签，而不是列表第一个。"""
        from app.agents.tools.browser_tools import _pick_tab

        tabs = [_tab("1", "首页", "https://a.test/"), _tab("2", "详情", "https://a.test/p/1")]
        assert _pick_tab(tabs, None, preferred="2").id == "2"
        assert _pick_tab(tabs, None, preferred=None).id == "1"

    def test_missing_preferred_falls_back_to_first(self):
        """记住的标签已被用户关掉：退回第一个页面，不能报错。"""
        from app.agents.tools.browser_tools import _pick_tab

        tabs = [_tab("1", "首页", "https://a.test/")]
        assert _pick_tab(tabs, None, preferred="gone").id == "1"

    def test_explicit_target_beats_preferred(self):
        """显式 target 是 AI 的明确意图，优先级高于隐式记忆。"""
        from app.agents.tools.browser_tools import _pick_tab

        tabs = [_tab("1", "首页", "https://a.test/"), _tab("2", "订单", "https://a.test/orders")]
        assert _pick_tab(tabs, "orders", preferred="1").id == "2"

    def test_scope_isolated_between_conversations(self):
        """A 会话记下的标签不得影响 B 会话 —— 那会变成跨用户读别人的页面。"""
        from app.agents.tools import browser_tools as BT

        BT.set_active_tab(_ctx(conv=1), "tab-a")
        BT.set_active_tab(_ctx(conv=2), "tab-b")
        assert BT._get_active_tab(_ctx(conv=1)) == "tab-a"
        assert BT._get_active_tab(_ctx(conv=2)) == "tab-b"
        assert BT._get_active_tab(_ctx(conv=3)) is None

    def test_no_conversation_falls_back_to_user_scope(self):
        """定时任务等无会话场景：按用户隔离，仍不能跨用户。"""
        from app.agents.tools import browser_tools as BT

        BT.set_active_tab(_ctx(conv=None, user=7), "t7")
        assert BT._get_active_tab(_ctx(conv=None, user=7)) == "t7"
        assert BT._get_active_tab(_ctx(conv=None, user=8)) is None

    def test_none_ctx_is_noop(self):
        """没上下文时不记忆，也不能抛错（部分调用方传 None）。"""
        from app.agents.tools import browser_tools as BT

        BT.set_active_tab(None, "x")
        assert BT._get_active_tab(None) is None

    def test_clear_removes_entry(self):
        from app.agents.tools import browser_tools as BT

        BT.set_active_tab(_ctx(conv=9), "x")
        BT.set_active_tab(_ctx(conv=9), None)
        assert BT._get_active_tab(_ctx(conv=9)) is None


# ---------------------------------------------------------------- 新标签识别


class TestNewTabs:
    def test_diff_by_id(self):
        from app.agents.tools.browser_tools import _new_tabs

        before = [_tab("1", "列表", "https://a.test/")]
        after = before + [_tab("2", "详情", "https://a.test/p/1")]
        assert [t.id for t in _new_tabs(before, after)] == ["2"]

    def test_no_change_returns_empty(self):
        from app.agents.tools.browser_tools import _new_tabs

        tabs = [_tab("1", "列表", "https://a.test/")]
        assert _new_tabs(tabs, list(tabs)) == []

    def test_popup_windows_are_ignored(self):
        """window.open 出来的弹窗不承载可操作内容，跟过去只会看到空白。"""
        from app.agents.tools.browser_tools import _new_tabs

        before = [_tab("1", "列表", "https://a.test/")]
        after = before + [_tab("9", "", "https://a.test/popup", type_="background_page")]
        assert _new_tabs(before, after) == []


# ---------------------------------------------------------------- 对话框回显


class TestDialogNote:
    def _sess(self, dialogs: list[dict]) -> C.CDPSession:
        s = C.CDPSession("ws://x", timeout=0.05)
        s.dialogs = dialogs
        return s

    def test_no_dialogs_returns_empty(self):
        from app.agents.tools.browser_tools import _dialog_note

        note, data = _dialog_note(self._sess([]))
        assert note == "" and data == []

    def test_accepted_dialog_is_reported_with_message(self):
        """代 AI 点了「确定」必须说出来：否则它以为页面什么都没问。"""
        from app.agents.tools.browser_tools import _dialog_note

        note, data = _dialog_note(self._sess([{
            "type": "confirm", "message": "确定要清空购物车吗？", "policy": "accept",
        }]))
        assert "确定要清空购物车吗？" in note
        assert "已自动点「确定」" in note
        assert data[0]["type"] == "confirm"

    def test_dismissed_dialog_says_cancel(self):
        from app.agents.tools.browser_tools import _dialog_note

        note, _ = _dialog_note(self._sess([{
            "type": "confirm", "message": "删除？", "policy": "dismiss",
        }]))
        assert "已自动点「取消」" in note

    def test_ignored_dialog_warns_about_stall(self):
        """未应答 = 该标签的 CDP 已停摆，必须让 AI 知道后续会失败。"""
        from app.agents.tools.browser_tools import _dialog_note

        note, _ = _dialog_note(self._sess([{
            "type": "alert", "message": "hi", "policy": "ignore",
        }]))
        assert "未应答" in note and "卡住" in note

    def test_recovered_dialog_has_no_fake_message(self):
        """上一会话遗留的对话框：只知道"已点掉"，不该编造它的文案。"""
        from app.agents.tools.browser_tools import _dialog_note

        note, _ = _dialog_note(self._sess([{
            "type": "recovered", "message": "（在本次操作开始前弹出，已自动应答）",
            "policy": "accept",
        }]))
        assert "已自动应答" in note
        assert "「（在本次操作开始前弹出" not in note

    def test_empty_message_does_not_render_empty_quotes(self):
        from app.agents.tools.browser_tools import _dialog_note

        note, _ = _dialog_note(self._sess([{"type": "alert", "message": "", "policy": "accept"}]))
        assert "「」" not in note


# ---------------------------------------------------------------- 遗留对话框恢复


class TestRecoverStalledDialog:
    """上一会话结束后才弹出的 confirm 会让 `Page.enable` 直接超时。

    这是真机上「AI 突然卡死、后续全失败」最常见的原因：处理器已随旧会话关闭，
    没人点掉对话框，整个标签的 CDP 通道停摆。
    """

    def _session(self) -> tuple[C.CDPSession, list[dict]]:
        sess = C.CDPSession("ws://x", timeout=0.05)
        sent: list[dict] = []

        class _WS:
            async def send(self, raw):
                import json

                sent.append(json.loads(raw))

        sess._ws = _WS()  # type: ignore[assignment]
        return sess, sent

    def test_sends_handle_dialog_without_leaving_pending(self):
        """应答命令的响应可能永远不来，不能留在 _pending 里占 id。"""
        sess, sent = self._session()
        ok = asyncio.run(sess._recover_stalled_dialog())
        assert ok is True
        assert sent[0]["method"] == "Page.handleJavaScriptDialog"
        assert sent[0]["params"] == {"accept": True}
        assert sess._pending == {}

    def test_records_recovered_dialog(self):
        sess, _ = self._session()
        asyncio.run(sess._recover_stalled_dialog())
        assert sess.dialogs and sess.dialogs[0]["type"] == "recovered"

    def test_respects_dismiss_policy(self):
        sess, sent = self._session()
        sess.dialog_policy = "dismiss"
        asyncio.run(sess._recover_stalled_dialog())
        assert sent[0]["params"] == {"accept": False}

    def test_ignore_policy_does_not_try(self):
        """策略是 ignore 时不该替用户做决定，宁可让上层报错。"""
        sess, sent = self._session()
        sess.dialog_policy = "ignore"
        assert asyncio.run(sess._recover_stalled_dialog()) is False
        assert sent == []

    def test_no_ws_returns_false(self):
        sess = C.CDPSession("ws://x", timeout=0.05)
        assert asyncio.run(sess._recover_stalled_dialog()) is False


class TestArmDialogsRecovery:
    """`_arm_dialogs` 是这条恢复链的入口：它决定是否记账、是否重试。"""

    def _session(self, failures: int) -> tuple[C.CDPSession, list[dict]]:
        """前 failures 次 Page.enable 超时，之后成功。"""
        sess = C.CDPSession("ws://x", timeout=0.05)
        sent: list[dict] = []
        state = {"n": 0}

        class _WS:
            async def send(self, raw):
                import json

                sent.append(json.loads(raw))

        sess._ws = _WS()  # type: ignore[assignment]

        async def fake_call(method, params=None):
            if method == "Page.enable":
                state["n"] += 1
                if state["n"] <= failures:
                    raise ValidationError("浏览器操作超时（Page.enable）")
            return {}

        sess.call = fake_call  # type: ignore[assignment]
        return sess, sent

    def test_recovers_and_rearms_after_timeout(self):
        sess, sent = self._session(failures=1)
        asyncio.run(sess._arm_dialogs())
        assert sess._dialog_armed is True
        assert [m["method"] for m in sent] == ["Page.handleJavaScriptDialog"]
        assert sess.dialogs[-1]["type"] == "recovered"

    def test_non_timeout_error_is_not_treated_as_dialog(self):
        """CDP 错误（如 target 已关闭）不是对话框：盲目应答会记下假记录。"""
        sess, sent = self._session(failures=0)

        async def fake_call(method, params=None):
            raise ValidationError("CDP 错误：No target with given id found")

        sess.call = fake_call  # type: ignore[assignment]
        asyncio.run(sess._arm_dialogs())
        assert sess._dialog_armed is False
        assert sent == []
        assert sess.dialogs == []

    def test_failed_recovery_rolls_back_record(self):
        """应答没起作用就不该留下「已恢复」的记录 —— 那是给 AI 的假事实。"""
        sess, _sent = self._session(failures=0)

        async def fake_call(method, params=None):
            raise ValidationError("浏览器操作超时（Page.enable）")

        sess.call = fake_call  # type: ignore[assignment]
        asyncio.run(sess._arm_dialogs())
        assert sess._dialog_armed is False
        assert sess.dialogs == []
