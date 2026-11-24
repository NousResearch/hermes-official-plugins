"""Feishu cases moved from core's shared approval suites when Feishu left core:

- ``tests/gateway/test_approval_requester_binding_adapters.py::test_feishu_card_click``: a card click honours
  ``approvals.approvers`` (Bob is refused when only the requester may answer).
- ``tests/gateway/test_approval_answer_slash_gate.py`` ``feishu_card`` path: a non-admin answers only where
  they could type the command.
- ``tests/gateway/test_exec_approval_deny_only_renderers.py::test_feishu_full_command_with_a_refused_chunk_...``:
  a refused chunk of the full command leaves the card Deny-only.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import test_feishu_approval_buttons as fx  # installs the lark stubs
from approval_helpers import DEFAULT, write_approvers

REFUSAL = "Only Alice can answer this approval."
ADMIN, MEMBER = "111", "222"


@pytest.fixture(autouse=True)
def _clean_state():
    from tools import approval, slash_confirm

    approval._gateway_queues.clear()
    slash_confirm._pending.clear()
    yield
    approval._gateway_queues.clear()
    slash_confirm._pending.clear()


def _enqueue(session_key, platform, user_id, user_name, command, **ctx):
    """Enqueue an approval the way the agent thread does, inside the requester's turn."""
    from gateway.session_context import clear_session_vars, set_session_vars
    from tools import approval
    from tools.approval_gateway_wait import _ApprovalEntry

    tokens = set_session_vars(platform=platform, user_id=user_id, user_name=user_name, session_key=session_key, **ctx)
    try:
        entry = _ApprovalEntry({"command": command})
    finally:
        clear_session_vars(tokens)
    approval._gateway_queues.setdefault(session_key, []).append(entry)
    return entry


def _bind_card_sdk(monkeypatch):
    monkeypatch.setattr(fx.feishu_module, "P2CardActionTriggerResponse", fx._FakeP2Response)
    monkeypatch.setattr(fx.feishu_module, "CallBackCard", fx._FakeCallBackCard)
    monkeypatch.setattr(fx.feishu_module, "CallBackToast", SimpleNamespace, raising=False)


def _click(adapter, uid, chat_id="oc_1"):
    data = fx._make_card_action_data({"hermes_action": "approve_once", "approval_id": 1}, chat_id=chat_id, open_id=uid)

    def submit(coro, _loop):
        asyncio.run(coro)
        return SimpleNamespace(add_done_callback=lambda *_a, **_k: None)

    with patch("asyncio.run_coroutine_threadsafe", side_effect=submit):
        return adapter._on_card_action_trigger(data)


@pytest.mark.parametrize(("approvers", "allowed"), [(DEFAULT, True), ([], False)])
def test_feishu_card_click_honours_approvers(approvers, allowed, monkeypatch):
    from tools import approval

    write_approvers(approvers)
    entry = _enqueue("sess-f", "feishu", "ou_alice", "Alice", "rm -rf build")
    approval.restrict_gateway_approval_card_to_deny("sess-f", entry.data["request_id"], restricted=False)
    adapter = fx._make_adapter()
    adapter._loop = MagicMock(is_closed=MagicMock(return_value=False))
    adapter._allowed_group_users = {"ou_alice", "ou_bob"}
    adapter._approval_state[1] = {"session_key": "sess-f", "message_id": "m", "chat_id": "oc_1",
                                  "request_id": entry.data["request_id"]}
    _bind_card_sdk(monkeypatch)

    response = _click(adapter, "ou_bob")

    assert entry.event.is_set() is allowed
    if not allowed:
        assert response.toast.content == REFUSAL and response.card is None
        assert 1 in adapter._approval_state  # Alice's card still answers


@pytest.mark.parametrize(("admins", "member_answers"), [([ADMIN], False), ([], True)], ids=["gated", "ungated"])
def test_a_non_admin_answers_a_feishu_card_only_where_they_could_type_the_command(admins, member_answers, monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "*")
    key = "agent:main:feishu:dm:oc_1"
    entry = _enqueue(key, "feishu", ADMIN, "Req", "rm -rf /srv/data", chat_type="dm")
    entry.card_deny_only = False  # its card showed the whole command
    adapter = fx._make_adapter()
    adapter.config.extra.update({"allow_admin_from": admins} if admins else {})
    adapter._loop = MagicMock(is_closed=MagicMock(return_value=False))
    adapter.set_authorization_check(lambda *_a, **_kw: True)
    adapter._approval_state[1] = {"session_key": key, "message_id": "m", "chat_id": "oc_1",
                                  "request_id": entry.data["request_id"]}
    _bind_card_sdk(monkeypatch)

    def answer(uid):
        response = _click(adapter, uid)
        if entry.result is None:  # refused: the card keeps its buttons
            assert response.card is None and 1 in adapter._approval_state
        return entry.result == "once"

    assert answer(MEMBER) is member_answers
    if not member_answers:
        assert answer(ADMIN) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_chunk", [0, 1])
async def test_feishu_full_command_with_a_refused_chunk_leaves_the_card_deny_only(failed_chunk, monkeypatch):
    """Feishu's send() used to report only its LAST chunk: a refused chunk followed by a delivered one
    returned success, the card kept Approve and the user could approve text they never received."""
    from tools import approval
    from tools.approval import resolve_gateway_approval_card
    from tools.approval_gateway_wait import _ApprovalEntry

    pending = _ApprovalEntry({"request_id": "r1", "command": "echo '```' " + "A" * 4000 + " && rm -rf ~ # TAIL"})
    monkeypatch.setitem(approval._gateway_queues, "sk", [pending])
    adapter, calls = fx._make_adapter(), []

    async def fake_send(**kwargs):
        calls.append(kwargs["msg_type"])
        ok = kwargs["msg_type"] == "interactive" or len(calls) - 1 != failed_chunk
        return SimpleNamespace(success=lambda: ok, code=0 if ok else 230001, msg="ok" if ok else "refused",
                               data=SimpleNamespace(message_id=f"m{len(calls)}"))

    adapter._feishu_send_with_retry = fake_send
    command = "echo " + "A" * 20000 + " && rm -f build.log # TAIL"
    await adapter.send_exec_approval(chat_id="oc_1", command=command, session_key="sk", request_id="r1")
    card = next(iter(adapter._approval_state.values()))
    assert calls[-1] == "interactive" and "only Deny is offered" in card["body"]
    assert resolve_gateway_approval_card("sk", "once", "r1") == 0 and not pending.event.is_set()
