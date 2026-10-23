"""Read how a Crew agent's sessions are shaped from application code, without the ACP layer.

Application code may not import ``kiro_crew.acp`` or ``kiro_crew.providers``
(``scripts/check_agent_sdk_boundary.py``). A crewmate's heartbeat
(``dashboard.goal_reminder.member_has_session_tools``) still needs the answer a
session would get to "is this MCP server switched off for this agent", and the Goals
tab (``member_mate_tools.binding_can_set_goal``) to "does a crewmate session hold
this tool", so this module is the seam they reach those reads through. The ACP and
provider imports are made at call time, both because those layers import back into
``agent_sdk`` at load time and so that a test patching the function on its own
module is seen here.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def session_mcp_server_is_disabled(name: str, agent: str | None) -> bool:
    """Whether MCP server *name* is switched off whole for a session as *agent*.

    ``acp.session_mcp.session_mcp_server_is_disabled``: blocking, and an unreadable
    source switches nothing off.
    """
    from kiro_crew.acp import session_mcp

    return session_mcp.session_mcp_server_is_disabled(name, agent)


def session_mcp_tool_is_disabled(server: str, tool: str, agent: str | None) -> bool:
    """Whether *tool* on MCP server *server* is switched off for a session as *agent*,
    by the agent spec's ``disabledTools`` or the user's global MCP settings file.

    ``acp.session_mcp.session_mcp_disabled_tools``: blocking, and an unreadable
    source switches nothing off. The Goals tab asks this of ``kirocrew-mate`` /
    ``set_goal`` (``member_mate_tools.binding_can_set_goal``) because the
    dashboard's tool-off action writes only the global file.
    """
    from kiro_crew.acp import session_mcp

    return (server, tool) in session_mcp.session_mcp_disabled_tools(agent)


def member_backend_mounts_spec_server(
    name: str, agent: str | None, work_dir: str | None = None
) -> bool:
    """Whether a crewmate session on the configured member backend MOUNTS the MCP
    server *name* that *agent*'s spec declares.

    The spec saying so is not the whole answer: a mirrored harness composes the
    session's array from the spec and can withhold a server the spec mounts, as
    codex, opencode, goose and deepseek withhold every managed Crew server outside
    the control plane (``providers.mirrors.identity.withheld_servers``: their MCP
    child would come up bound to no session). So the answer is read off the
    backend's own projection, the one its ``session/new`` is composed from:

    * a backend whose spec servers reach the session by a channel of its own
      (``ACP_BACKENDS_SPEC_SERVERS_OFF_WIRE``: kiro-cli reads the file, KAS is
      handed the agent definition) mounts what the spec mounts;
    * a mirrored backend mounts what its mirror's ``session_projection`` places,
      asked for the transport (``permission_surface_owned``), since whether one
      session owns its permission surface is not knowable here;
    * a backend with no mirror and no channel of its own (broker-only, no-channel)
      mounts none of the spec's servers.

    The member backend is ``agent.member_acp_backend``, read the way
    ``context._member_backend_can_dispatch`` reads it. Blocking (the projection
    parses the agent spec). A projection that cannot be read answers True, the
    reading every caller of this module keeps: the question only chooses a route,
    and an unreadable spec must not take one away.
    """
    from collections.abc import Mapping

    from kiro_crew.agent_sdk.backends import (
        ACP_BACKENDS_SPEC_SERVERS_OFF_WIRE,
        resolve_selected_backend,
    )
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.providers.mirrors import mirror_for

    try:
        backend = resolve_selected_backend(KiroCrewConfig.load().agent.member_acp_backend)
        if backend in ACP_BACKENDS_SPEC_SERVERS_OFF_WIRE:
            return True
        mirror = mirror_for(backend)
        if mirror is None:
            return False
        projection = mirror.session_projection(
            agent, work_dir=work_dir, permission_surface_owned=True
        )
    except Exception:
        logger.debug("member backend projection unreadable for %r", agent, exc_info=True)
        return True
    servers = projection.params.get("mcpServers") or ()
    return any(isinstance(e, Mapping) and e.get("name") == name for e in servers)
