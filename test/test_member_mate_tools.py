"""Every new crewmate gets its own-record tools, and the text it hears matches what it has.

``member_mate_tools.grant_mate_tools_to_new_member`` enrolls a just-created
crewmate whose template lacks ``kirocrew-mate`` through the capability editor;
the identity sentence, the first welcome and the goal nudges name those tools
only when its spec mounts them.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from agent_capabilities_helpers import _accepting_kiro_cli  # noqa: F401
from agent_capabilities_helpers import editor_fixture  # noqa: F401
from agent_capabilities_helpers import save, spec_for

from kiro_crew import agent, member_mate_tools
from kiro_crew.config import loader
from kiro_crew.context_assembly import member as member_ctx
from kiro_crew.dashboard import goal_setup, mate_welcome

MATE_TOOLS = ("set_goal", "set_key_results", "set_check_ins", "update_self")

TRANSPORT = {"command": "/opt/kirocrew", "args": ["mcp", "mate"]}


@pytest.fixture(name="granting")
def granting_fixture(editor, monkeypatch):
    """The capability editor, with the mate transport and the policy pinned."""
    monkeypatch.setattr(
        "kiro_crew.agent_capabilities._member_platform_transport", lambda name: dict(TRANSPORT)
    )
    monkeypatch.setattr(member_mate_tools, "_policy_permits", lambda member: True)
    return editor


def _grant(member="A"):
    loader._invalidate_config_cache()
    return member_mate_tools.grant_mate_tools_to_new_member(member, catalog=lambda project: {})


def _binding(home, member="A"):
    return json.loads((home / "config.json").read_text())["agents"][member]


def test_a_template_without_the_server_is_enrolled_with_it(granting) -> None:
    service, home, specs, parent = granting
    assert _grant() == member_mate_tools.GRANTED
    assert _binding(home)["kiro_agent"] != "parent"
    spec = spec_for(home, specs)
    # Exactly the default template's entry: no ``disabled`` key, which a
    # per-session element cannot express and would cost the server its identity.
    assert spec["mcpServers"]["kirocrew-mate"] == TRANSPORT
    from kiro_crew.acp.session_mcp import native_mount_withholding

    assert (
        native_mount_withholding("kirocrew-mate", spec["mcpServers"]["kirocrew-mate"], ()) is None
    )
    assert "@kirocrew-mate" in spec["tools"]
    assert set(agent._MATE_AUTO_GRANTS) <= set(spec["allowedTools"])
    # Every other row keeps the template's value and still inherits.
    assert spec["mcpServers"]["search"] == parent["mcpServers"]["search"]
    assert "read" in spec["tools"] and spec["prompt"] == "original"
    assert spec["resources"] == parent["resources"]
    assert member_mate_tools.spec_has_mate_tools(spec)
    # B shares the same template and was not touched.
    assert _binding(home, "B")["kiro_agent"] == "parent"
    # A second call finds the member enrolled and writes nothing.
    before = (specs / f"{_binding(home)['kiro_agent']}.json").read_bytes()
    assert _grant() == member_mate_tools.ALREADY_ENROLLED
    assert (specs / f"{_binding(home)['kiro_agent']}.json").read_bytes() == before


def test_a_template_that_already_mounts_it_is_left_alone(granting) -> None:
    _, home, specs, parent = granting
    parent = {
        **parent,
        "tools": ["read", "@kirocrew-mate"],
        "mcpServers": {**parent["mcpServers"], "kirocrew-mate": dict(TRANSPORT)},
    }
    (specs / "parent.json").write_text(json.dumps(parent), encoding="utf-8")
    assert _grant() == member_mate_tools.TEMPLATE_HAS_IT
    assert _binding(home)["kiro_agent"] == "parent"
    assert json.loads((specs / "parent.json").read_text()) == parent


def test_a_template_that_mutes_it_is_left_alone(granting) -> None:
    _, home, specs, parent = granting
    parent = {
        **parent,
        "mcpServers": {**parent["mcpServers"], "kirocrew-mate": {**TRANSPORT, "disabled": True}},
    }
    (specs / "parent.json").write_text(json.dumps(parent), encoding="utf-8")
    assert _grant() == member_mate_tools.TEMPLATE_MUTES_IT
    assert _binding(home)["kiro_agent"] == "parent"


def test_a_backend_that_cannot_load_the_copy_gets_no_enrollment(granting, monkeypatch) -> None:
    _, home, _, _ = granting
    monkeypatch.setattr(
        "kiro_crew.agent_capabilities.member_backend_warning", lambda spec: {"backend": "x"}
    )
    assert _grant() == member_mate_tools.BACKEND_UNSUPPORTED
    assert _binding(home)["kiro_agent"] == "parent"


def test_a_policy_that_withholds_the_server_gets_no_enrollment(granting, monkeypatch) -> None:
    _, home, _, _ = granting
    monkeypatch.setattr(member_mate_tools, "_policy_permits", lambda member: False)
    assert _grant() == member_mate_tools.POLICY_WITHHOLDS
    assert _binding(home)["kiro_agent"] == "parent"


def test_an_unavailable_server_gets_no_enrollment(granting, monkeypatch) -> None:
    _, home, _, _ = granting
    monkeypatch.setattr(
        "kiro_crew.agent_capabilities._member_platform_transport", lambda name: None
    )
    assert _grant() == member_mate_tools.SERVER_UNAVAILABLE
    assert _binding(home)["kiro_agent"] == "parent"


def test_the_default_template_and_the_default_member_are_skipped(granting) -> None:
    _, home, _, _ = granting
    config = json.loads((home / "config.json").read_text())
    config["agents"]["A"]["kiro_agent"] = "kirocrew"
    (home / "config.json").write_text(json.dumps(config), encoding="utf-8")
    assert _grant() == member_mate_tools.DEFAULT_TEMPLATE
    assert _grant("default") == member_mate_tools.NOT_A_MEMBER
    assert _grant("nobody") == member_mate_tools.NOT_A_MEMBER


def test_a_later_save_keeps_the_platform_server_current(granting, monkeypatch) -> None:
    service, home, specs, _ = granting
    assert _grant() == member_mate_tools.GRANTED
    moved = {"command": "/new/kirocrew", "args": ["mcp", "mate"]}
    monkeypatch.setattr(
        "kiro_crew.agent_capabilities._member_platform_transport", lambda name: dict(moved)
    )
    save(service, [])
    assert spec_for(home, specs)["mcpServers"]["kirocrew-mate"] == moved
    # A build that stops emitting the server retracts it from the copy.
    monkeypatch.setattr(
        "kiro_crew.agent_capabilities._member_platform_transport", lambda name: None
    )
    save(service, [])
    assert "kirocrew-mate" not in spec_for(home, specs).get("mcpServers", {})


def test_a_refresh_keeps_a_per_tool_restriction_the_copy_holds(monkeypatch) -> None:
    from kiro_crew import agent_capabilities

    moved = {"command": "/new/kirocrew", "args": ["mcp", "mate"]}
    monkeypatch.setattr(agent_capabilities, "_member_platform_transport", lambda name: dict(moved))
    monkeypatch.setattr(agent_capabilities, "_parent_is_owned", lambda snap: False)
    held = {**TRANSPORT, "disabledTools": ["update_self"]}
    spec = {"mcpServers": {"kirocrew-mate": held}}
    agent_capabilities._maintain_owned({"parent_spec": {"mcpServers": {}}}, spec)
    assert spec["mcpServers"]["kirocrew-mate"] == {**moved, "disabledTools": ["update_self"]}


def test_has_mate_tools_reads_the_spec_the_member_runs(granting) -> None:
    _, home, _, _ = granting
    loader._invalidate_config_cache()
    assert member_mate_tools.member_has_mate_tools("A") is False
    assert _grant() == member_mate_tools.GRANTED
    loader._invalidate_config_cache()
    assert member_mate_tools.member_has_mate_tools("A") is True
    # Nothing to read keeps the wording every crewmate had before.
    assert member_mate_tools.member_has_mate_tools("nobody") is True
    assert member_mate_tools.binding_has_mate_tools(SimpleNamespace()) is True


def test_a_project_template_shadows_the_global_one(granting, tmp_path, monkeypatch) -> None:
    _, _, specs, parent = granting
    project = tmp_path / "proj"
    (project / ".kiro" / "agents").mkdir(parents=True)
    monkeypatch.setattr("kiro_crew.config.loader.default_project_dir", lambda _: str(project))
    with_mate = {
        **parent,
        "tools": ["@kirocrew-mate"],
        "mcpServers": {"kirocrew-mate": dict(TRANSPORT)},
    }
    (specs / "parent.json").write_text(json.dumps(with_mate), encoding="utf-8")
    shadow = project / ".kiro" / "agents" / "parent.json"
    shadow.write_text(json.dumps(parent), encoding="utf-8")
    binding = SimpleNamespace(kiro_agent="parent", workspace="default")
    assert member_mate_tools.binding_has_mate_tools(binding) is False
    # An unreadable project copy keeps the default wording and never reads the
    # global template in its place.
    shadow.write_bytes(b"\xff")
    monkeypatch.setattr(
        agent, "agent_spec_path", lambda *a, **k: pytest.fail("fell back to a scan")
    )
    assert member_mate_tools.binding_has_mate_tools(binding) is True
    traversal = SimpleNamespace(kiro_agent="../parent", workspace="default")
    assert member_mate_tools.binding_has_mate_tools(traversal) is True


def test_a_set_goal_switched_off_in_the_global_settings_keeps_the_editor(
    granting, tmp_path, monkeypatch
) -> None:
    """The dashboard's tool-off action writes ``disabledTools`` to the global
    ``settings/mcp.json`` only; kiro-cli withholds the tool from every session
    on it, so the Goals tab must read it beside the spec or it sends the user to
    a chat that cannot write the goal down."""
    _, _, specs, parent = granting
    with_mate = {
        **parent,
        "tools": ["@kirocrew-mate"],
        "mcpServers": {"kirocrew-mate": dict(TRANSPORT)},
    }
    (specs / "parent.json").write_text(json.dumps(with_mate), encoding="utf-8")
    settings = tmp_path / "settings-mcp.json"
    monkeypatch.setattr(agent, "_KIRO_MCP_JSON", settings)
    binding = SimpleNamespace(kiro_agent="parent", workspace="default")
    assert member_mate_tools.binding_can_set_goal(binding) is True
    settings.write_text(
        json.dumps({"mcpServers": {"kirocrew-mate": {"disabledTools": ["set_goal"]}}}),
        encoding="utf-8",
    )
    assert member_mate_tools.binding_can_set_goal(binding) is False
    # Another of its tools switched off there leaves the chat route alone.
    settings.write_text(
        json.dumps({"mcpServers": {"kirocrew-mate": {"disabledTools": ["goal_read"]}}}),
        encoding="utf-8",
    )
    assert member_mate_tools.binding_can_set_goal(binding) is True
    # The global switch holds when the template cannot be resolved at all.
    settings.write_text(
        json.dumps({"mcpServers": {"kirocrew-mate": {"disabledTools": ["set_goal"]}}}),
        encoding="utf-8",
    )
    missing = SimpleNamespace(kiro_agent="nobody", workspace="default")
    assert member_mate_tools.binding_can_set_goal(missing) is False
    # A row bound to no template runs the default one, under the same switch.
    unbound = SimpleNamespace(kiro_agent="", workspace="default")
    assert member_mate_tools.binding_can_set_goal(unbound) is False
    # The server muted whole there withholds every one of its tools.
    settings.write_text(
        json.dumps({"mcpServers": {"kirocrew-mate": {"disabled": True}}}), encoding="utf-8"
    )
    assert member_mate_tools.binding_can_set_goal(binding) is False
    assert member_mate_tools.binding_can_set_goal(unbound) is False
    settings.write_bytes(b"\xff")
    assert member_mate_tools.binding_can_set_goal(binding) is True


def test_a_member_backend_that_withholds_the_mate_server_keeps_the_editor(
    granting, tmp_path, monkeypatch
) -> None:
    """A mirrored member backend composes the session's array itself, and codex
    withholds every managed Crew server outside the control plane, so a spec
    that mounts ``kirocrew-mate`` still runs the crewmate without ``set_goal``
    there. The Goals tab reads the backend's own projection, not the spec alone."""
    from chat_test_helpers import pin_harness_choice

    from kiro_crew.agent_sdk.backends import ACP_BACKEND_CODEX, ACP_BACKEND_KIRO

    _, _, specs, parent = granting
    with_mate = {
        **parent,
        "tools": ["@kirocrew-mate"],
        "mcpServers": {"kirocrew-mate": dict(TRANSPORT)},
    }
    (specs / "parent.json").write_text(json.dumps(with_mate), encoding="utf-8")
    monkeypatch.setattr(agent, "_KIRO_MCP_JSON", tmp_path / "absent-settings.json")
    binding = SimpleNamespace(kiro_agent="parent", workspace="default")
    pin_harness_choice(member_acp_backend=ACP_BACKEND_KIRO)
    assert member_mate_tools.binding_can_set_goal(binding) is True
    pin_harness_choice(member_acp_backend=ACP_BACKEND_CODEX)
    assert member_mate_tools.binding_can_set_goal(binding) is False


def test_spec_has_mate_tools_needs_the_server_and_a_tools_ref() -> None:
    entry = {"kirocrew-mate": dict(TRANSPORT)}
    assert member_mate_tools.spec_has_mate_tools({"mcpServers": entry, "tools": ["*"]})
    assert member_mate_tools.spec_has_mate_tools(
        {"mcpServers": entry, "tools": ["@kirocrew-mate/set_goal"]}
    )
    assert not member_mate_tools.spec_has_mate_tools({"mcpServers": entry, "tools": ["read"]})
    assert not member_mate_tools.spec_has_mate_tools({"tools": ["@kirocrew-mate"]})
    muted = {"kirocrew-mate": {**TRANSPORT, "disabled": True}}
    assert not member_mate_tools.spec_has_mate_tools({"mcpServers": muted, "tools": ["*"]})


def test_the_identity_sentence_names_the_tools_only_when_they_are_there() -> None:
    assert all(t in member_ctx._MATE_TOOLS_IDENTITY for t in MATE_TOOLS)
    assert not any(t in member_ctx._NO_MATE_TOOLS_IDENTITY for t in MATE_TOOLS)
    assert "Goals tab" in member_ctx._NO_MATE_TOOLS_IDENTITY


def test_the_first_welcomes_follow_the_tools() -> None:
    entry = SimpleNamespace(display_name="Scout", description="")
    with_tools = mate_welcome.goal_kickoff(entry, "scout")
    without = mate_welcome.goal_kickoff(entry, "scout", has_mate_tools=False)
    assert "set_goal" in with_tools
    assert "set_goal" not in without and "Goals tab" in without
    described = SimpleNamespace(display_name="Scout", description="ship notes")
    known = mate_welcome.goal_kickoff(described, "scout", has_mate_tools=False)
    assert "ship notes" in known and "set_goal" not in known
    for others in (False, True):
        mate = mate_welcome.welcome_kickoff(entry, has_other_crewmates=others)
        plain = mate_welcome.welcome_kickoff(
            entry, has_other_crewmates=others, has_mate_tools=False
        )
        assert "update_self" in mate
        assert "update_self" not in plain and "mate page" in plain


GOAL = {"id": "g1", "text": "Keep the release notes current"}


def test_the_goal_nudges_follow_the_tools() -> None:
    rec = {"goals": [GOAL], "key_results": [], "schedules": {}}
    assert "set_key_results" in goal_setup.kickoff_prompt(rec, "g1")
    bare = goal_setup.kickoff_prompt(rec, "g1", has_mate_tools=False)
    assert bare is not None and "set_key_results" not in bare and "Goals tab" in bare
    krs = {**rec, "key_results": [{"id": "k1", "text": "notes out", "goal_ids": ["g1"]}]}
    assert "set_check_ins" in goal_setup.milestones_prompt(krs)
    off = goal_setup.milestones_prompt(krs, has_mate_tools=False)
    assert off is not None and "set_check_ins" not in off and "Goals tab" in off
    removed = {
        "goals": [],
        "key_results": [{"id": "k1", "text": "notes out", "goal_ids": []}],
        "removed_goal": {"goal": GOAL, "index": 0, "at": 1, "key_result_ids": ["k1"]},
    }
    assert "set_key_results" in goal_setup.removed_prompt(removed, "g1")
    gone = goal_setup.removed_prompt(removed, "g1", has_mate_tools=False)
    assert gone is not None and "set_key_results" not in gone and "Goals tab" in gone


def test_the_shipped_defaults_auto_approve_every_mate_grant() -> None:
    from importlib import resources

    defaults = json.loads(
        resources.files("kiro_crew.config").joinpath("defaults.json").read_text(encoding="utf-8")
    )
    shipped = {t for t in defaults["allowedTools"] if t.startswith("@kirocrew-mate/")}
    assert shipped == set(agent._MATE_AUTO_GRANTS)


def test_set_goal_is_exposed_only_when_the_spec_mounts_that_tool() -> None:
    """The Goals tab starts a goal in the chat only when the mate can write it
    down: the server being mounted is not enough, the tool itself must be."""
    base = {"mcpServers": {"kirocrew-mate": dict(TRANSPORT)}}
    assert member_mate_tools.spec_exposes_mate_tool(
        {**base, "tools": ["*"]}, member_mate_tools.SET_GOAL_TOOL
    )
    assert member_mate_tools.spec_exposes_mate_tool(
        {**base, "tools": ["read", "@kirocrew-mate"]}, member_mate_tools.SET_GOAL_TOOL
    )
    assert member_mate_tools.spec_exposes_mate_tool(
        {**base, "tools": ["@kirocrew-mate/set_goal"]}, member_mate_tools.SET_GOAL_TOOL
    )
    # Mounted, but only another of its tools: the server is there, set_goal is not.
    assert not member_mate_tools.spec_exposes_mate_tool(
        {**base, "tools": ["@kirocrew-mate/goal_read"]}, member_mate_tools.SET_GOAL_TOOL
    )
    # Mounted whole, then the tool, its server or everything excluded.
    for excluded in (["@kirocrew-mate/set_goal"], ["@kirocrew-mate"], ["*"]):
        assert not member_mate_tools.spec_exposes_mate_tool(
            {**base, "tools": ["@kirocrew-mate"], "excludedTools": excluded},
            member_mate_tools.SET_GOAL_TOOL,
        )
    # The server entry's own per-tool switch withholds it too.
    for disabled in (["set_goal"], ["@kirocrew-mate/set_goal"], ["*"]):
        assert not member_mate_tools.spec_exposes_mate_tool(
            {
                "mcpServers": {"kirocrew-mate": {**TRANSPORT, "disabledTools": disabled}},
                "tools": ["*"],
            },
            member_mate_tools.SET_GOAL_TOOL,
        )
    assert member_mate_tools.spec_exposes_mate_tool(
        {
            "mcpServers": {"kirocrew-mate": {**TRANSPORT, "disabledTools": ["goal_read"]}},
            "tools": ["*"],
        },
        member_mate_tools.SET_GOAL_TOOL,
    )
    # No server, or a muted one, exposes nothing.
    assert not member_mate_tools.spec_exposes_mate_tool(
        {"tools": ["*"]}, member_mate_tools.SET_GOAL_TOOL
    )
    assert not member_mate_tools.spec_exposes_mate_tool(
        {"mcpServers": {"kirocrew-mate": {**TRANSPORT, "disabled": True}}, "tools": ["*"]},
        member_mate_tools.SET_GOAL_TOOL,
    )
