"""``session_handoff``: moving who may run a session, and nothing else with it.

Driven against real slots and the real gate (:func:`authorize_target`), with the
managers file in a temporary data home. The crew log is off, so the tree edge is not
moved here; that move is ``session_adopt``'s own append, tested with that verb.
"""

from __future__ import annotations

import asyncio

import pytest
from chat_test_helpers import _make_state
from session_control_helpers import (  # noqa: F401 -- autouse fixtures apply by import
    _enabled_fixture,
    _fresh_create_budget,
    _fresh_stop_windows,
    _key,
    _slot,
)

from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard import session_managers

#: Spelled out rather than read from the production set, so a change to it fails here.
WIDENED = ("send", "read", "summary", "stop", "close", "adopt", "release", "handoff")
#: Verbs that stay with the creator after a handoff.
CREATOR_ONLY = ("fork", "rename", "set_model")


@pytest.fixture(autouse=True)
def _home(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "crewhome"))
    _floor_monkeypatch.setattr(sc, "manager_chain_enabled", lambda: False)
    session_managers.reset_cache()
    yield
    session_managers.reset_cache()


def _team(state):
    """A person's tab, a lead it started, and a conductor and worker the lead opened.

    The lead is agent-created, so it is fenced: the caller class the fence is about.
    """
    owner = _slot(state, "chat-owner")
    lead = _slot(state, "chat-lead")
    conductor = _slot(state, "chat-conductor")
    worker = _slot(state, "chat-worker")
    lead._created_by = owner.key
    conductor._created_by = lead.key
    worker._created_by = lead.key
    return lead, conductor, worker


def _reaches(state, caller, target, operation: str) -> bool:
    try:
        sc.authorize_target(
            state, caller_session_key=_key(caller), target=target.key, operation=operation
        )
    except sc.SessionControlError as exc:
        assert exc.code == "not_creator", exc.code
        return False
    return True


def _hand(state, caller, target, to):
    return asyncio.run(
        sc.handoff_target(state, caller_session_key=_key(caller), target=target.key, to=to.key)
    )


def _notices(slot) -> list[str]:
    return [m["content"] for m in slot.messages if m.get("role") == "notice"]


# ── nothing handed over ──────────────────────────────────────────────────────


def test_a_session_nobody_handed_over_is_run_by_its_creator(tmp_path):
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    assert session_managers.holder(worker) == lead.key
    for operation in WIDENED + CREATOR_ONLY:
        assert _reaches(state, lead, worker, operation), operation
    for operation in WIDENED:
        assert not _reaches(state, conductor, worker, operation), operation


# ── a handoff moves control ─────────────────────────────────────────────────


def test_a_handoff_moves_control_to_the_new_manager_and_away_from_the_giver(tmp_path):
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)

    result = _hand(state, lead, worker, conductor)

    assert result["manager"] == conductor.key
    assert result["previous_manager"] == lead.key
    for operation in WIDENED:
        assert _reaches(state, conductor, worker, operation), operation
        assert not _reaches(state, lead, worker, operation), operation


def test_created_by_does_not_move_so_fork_rename_and_model_stay_with_the_creator(tmp_path):
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)

    _hand(state, lead, worker, conductor)

    assert worker._created_by == lead.key
    for operation in CREATOR_ONLY:
        assert _reaches(state, lead, worker, operation), operation
        assert not _reaches(state, conductor, worker, operation), operation


def test_the_transcript_gets_one_note_per_handoff(tmp_path):
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)

    _hand(state, lead, worker, conductor)

    notes = _notices(worker)
    assert notes == [
        f"This session was handed over: {conductor.key} manages it now, in place of {lead.key}."
    ]


def test_a_refused_handoff_writes_no_note(tmp_path):
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    _hand(state, lead, worker, conductor)

    with pytest.raises(sc.SessionControlError):
        _hand(state, lead, worker, conductor)

    assert len(_notices(worker)) == 1


# ── who may hand over, and to whom ──────────────────────────────────────────


def test_only_the_current_manager_can_hand_a_session_on(tmp_path):
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    helper = _slot(state, "chat-helper")
    helper._created_by = conductor.key
    _hand(state, lead, worker, conductor)

    with pytest.raises(sc.SessionControlError) as refused:
        _hand(state, lead, worker, lead)
    assert refused.value.code == "not_creator"

    _hand(state, conductor, worker, helper)
    assert session_managers.holder(worker) == helper.key
    assert not _reaches(state, conductor, worker, "send")


def test_the_receiver_must_be_a_session_the_giver_manages(tmp_path):
    state = _make_state(tmp_path)
    lead, _conductor, worker = _team(state)
    stranger = _slot(state, "chat-stranger")
    stranger._created_by = "chat-someone-else"

    with pytest.raises(sc.SessionControlError) as refused:
        _hand(state, lead, worker, stranger)

    assert refused.value.code == "not_creator"
    assert session_managers.holder(worker) == lead.key
    assert session_managers.load() == {}


def test_an_agent_in_a_persons_tab_cannot_hand_over_another_persons_tab(tmp_path):
    """The person's own tabs have no manager. An unfenced agent in one of them must not
    be able to give another of them to a fenced session."""
    state = _make_state(tmp_path)
    lead, conductor, _worker = _team(state)
    owner = state.get_slot("chat-owner")
    other_tab = _slot(state, "chat-other-tab")
    mine = _slot(state, "chat-mine")
    mine._created_by = owner.key

    with pytest.raises(sc.SessionControlError) as refused:
        _hand(state, owner, other_tab, conductor)
    assert refused.value.code == "not_manager"
    assert not _reaches(state, conductor, other_tab, "send")

    # The person's tab still hands over what it created, to what it created.
    _hand(state, owner, mine, lead)
    assert _reaches(state, lead, mine, "send")


@pytest.mark.parametrize(
    ("to", "code"),
    [("worker", "self_manager"), ("lead", "already_manager"), ("child", "would_cycle")],
)
def test_a_handoff_that_changes_nothing_or_makes_a_loop_is_refused(tmp_path, monkeypatch, to, code):
    # The chain lets the lead name a session the worker manages, the one way to reach
    # a loop: handing the worker to it would put the worker above itself.
    monkeypatch.setattr(sc, "manager_chain_enabled", lambda: True)
    state = _make_state(tmp_path)
    lead, _conductor, worker = _team(state)
    child = _slot(state, "chat-child")
    child._created_by = lead.key
    _hand(state, lead, child, worker)
    receiver = {"worker": worker, "lead": lead, "child": child}[to]

    with pytest.raises(sc.SessionControlError) as refused:
        asyncio.run(
            sc.handoff_target(
                state, caller_session_key=_key(lead), target=worker.key, to=receiver.key
            )
        )

    assert refused.value.code == code
    assert session_managers.holder(worker) == lead.key


# ── the record survives a restart, and a bad one fails closed ───────────────


def test_a_handoff_survives_a_restart(tmp_path):
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    _hand(state, lead, worker, conductor)

    session_managers.reset_cache()

    assert session_managers.holder(worker) == conductor.key
    assert _reaches(state, conductor, worker, "send")
    assert not _reaches(state, lead, worker, "send")


def test_an_unreadable_record_file_falls_back_to_the_creator(tmp_path):
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    _hand(state, lead, worker, conductor)
    path = session_managers._path()
    path.write_text("{not json", "utf-8")
    session_managers.reset_cache()

    assert session_managers.holder(worker) == lead.key
    assert _reaches(state, lead, worker, "send")
    assert not _reaches(state, conductor, worker, "send")


@pytest.mark.parametrize(
    "record",
    [{"manager": "chat-conductor", "tab": "another-tab"}, {"manager": ""}, "chat-conductor"],
    ids=["other-tab", "no-manager", "not-an-object"],
)
def test_a_record_that_does_not_match_this_session_withholds_reach(tmp_path, record):
    """A record that names this key but cannot be trusted for this session decides for
    nobody. It must not fall back to the creator: that is how a giver who edits its
    tab id on the transcript line would win the session back."""
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    session_managers.write({worker.key: ("chat-conductor", worker._tab_id)})
    session_managers._cache[1][worker.key] = record

    assert session_managers.holder(worker) == ""
    assert not _reaches(state, lead, worker, "send")
    assert not _reaches(state, conductor, worker, "send")


def test_editing_the_tab_id_after_a_handoff_does_not_give_the_session_back(tmp_path):
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    _hand(state, lead, worker, conductor)

    worker._tab_id = "edited-by-the-giver"
    session_managers.reset_cache()

    assert not _reaches(state, lead, worker, "send")
    assert not _reaches(state, conductor, worker, "send")


# ── the manager chain switch ────────────────────────────────────────────────


def test_with_the_chain_off_the_lead_does_not_reach_a_worker_under_its_conductor(tmp_path):
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    _hand(state, lead, worker, conductor)

    assert not _reaches(state, lead, worker, "send")
    assert _reaches(state, lead, conductor, "send")


def test_with_the_chain_on_the_lead_reaches_the_line_under_it(tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "manager_chain_enabled", lambda: True)
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    _hand(state, lead, worker, conductor)

    for operation in WIDENED:
        assert _reaches(state, lead, worker, operation), operation
        assert _reaches(state, conductor, worker, operation), operation
    # The chain is about running sessions; it widens nothing else.
    assert not _reaches(state, conductor, worker, "fork")
    stranger = _slot(state, "chat-stranger")
    stranger._created_by = "chat-someone-else"
    assert not _reaches(state, lead, stranger, "send")


def test_with_the_chain_on_a_closed_conductor_hands_its_workers_up(tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "manager_chain_enabled", lambda: True)
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    _hand(state, lead, worker, conductor)
    state._slots.pop(conductor.key)

    moved = asyncio.run(sc.hand_back_on_close(state, conductor))

    assert moved == [worker.key]
    assert session_managers.holder(worker) == lead.key
    session_managers.reset_cache()
    assert _reaches(state, lead, worker, "send")


def test_with_the_chain_on_a_closed_conductor_hands_up_the_workers_it_created(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(sc, "manager_chain_enabled", lambda: True)
    state = _make_state(tmp_path)
    lead, conductor, _worker = _team(state)
    own = _slot(state, "chat-own")
    own._created_by = conductor.key
    assert _reaches(state, lead, own, "send")
    state._slots.pop(conductor.key)

    assert asyncio.run(sc.hand_back_on_close(state, conductor)) == [own.key]
    assert _reaches(state, lead, own, "send")


def test_with_the_chain_off_a_closed_conductor_hands_nothing_back(tmp_path):
    state = _make_state(tmp_path)
    lead, conductor, worker = _team(state)
    _hand(state, lead, worker, conductor)
    state._slots.pop(conductor.key)

    assert asyncio.run(sc.hand_back_on_close(state, conductor)) == []
    assert session_managers.holder(worker) == conductor.key
    assert not _reaches(state, lead, worker, "send")
