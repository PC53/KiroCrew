"""The guest's first git command waits, bounded, for the proxy and the gateway's list.

Split from ``test_remote_git_proxy`` (file-size cap): the proxy starting beside
the tool server, and a partial lend list that keeps the command waiting.
"""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path

import pytest

from kiro_crew.remote_session import workspace_tools
from kiro_crew.remote_session.git_gateway import HOSTS

PROXY_SECRET = "a" * 64
#: The guest proxy runs inside the remote workspace, which is always POSIX.
_POSIX_GUEST = pytest.mark.skipif(sys.platform == "win32", reason="POSIX guest proxy")


def _rewritten_hosts(env):
    values = {v for k, v in env.items() if k.startswith("GIT_CONFIG_VALUE_")}
    return {host for host in HOSTS if f"https://{host}/" in values}


def test_a_command_while_the_proxy_starts_waits_for_its_port_file(tmp_path, monkeypatch):
    """The proxy starts beside the tool server: an early first clone still gets the login."""
    now = [100.0]
    sleeps: list[float] = []
    on_poll: dict[int, object] = {}
    listening: set[int] = set()

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds
        action = on_poll.get(len(sleeps))
        if callable(action):
            action()

    monkeypatch.setattr(workspace_tools, "_monotonic", lambda: now[0])
    monkeypatch.setattr(workspace_tools, "_sleep", sleep)
    monkeypatch.setattr(workspace_tools, "_listening", lambda port: port in listening)
    port_file = tmp_path / "git-proxy.port"
    lend = Path(str(port_file) + workspace_tools.GIT_PROXY_LEND_SUFFIX)
    port = 40000

    def proxy_up():
        listening.add(port)
        port_file.write_text(f"{port}\n")
        lend.write_text(f"{port} github.com\n")

    started = now[0]
    # The proxy publishes its port (and the gateway its list) at the fourth poll.
    on_poll[4] = proxy_up
    env = workspace_tools.git_proxy_env(str(port_file), PROXY_SECRET, startup_since=started)
    assert len(sleeps) == 4
    assert _rewritten_hosts(env) == {"github.com"}
    # A proxy that never comes: the wait ends with this tool server's startup window.
    port_file.unlink()
    listening.clear()
    sleeps.clear()
    on_poll.clear()
    assert workspace_tools.git_proxy_env(str(port_file), PROXY_SECRET, startup_since=started) == {}
    assert now[0] >= started + workspace_tools.GIT_PROXY_FIRST_LEND_WAIT_SECS
    # Past that window a missing proxy costs no wait at all, and neither does a
    # caller that names no startup.
    sleeps.clear()
    assert workspace_tools.git_proxy_env(str(port_file), PROXY_SECRET, startup_since=started) == {}
    assert workspace_tools.git_proxy_env(str(port_file), PROXY_SECRET) == {}
    assert sleeps == []


@_POSIX_GUEST
def test_the_tool_server_passes_its_own_start_to_the_proxy_wait(tmp_path, monkeypatch):
    seen = {}

    def env(port_file, secret, base=None, *, startup_since=None):
        seen["since"] = startup_since
        return {}

    monkeypatch.setattr(workspace_tools, "_monotonic", lambda: 7.0)
    workspace = workspace_tools.Workspace(
        str(tmp_path), "git-proxy.port", git_proxy_secret=PROXY_SECRET
    )
    monkeypatch.setattr(workspace_tools, "git_proxy_env", env)
    with contextlib.suppress(workspace_tools.ToolError):
        workspace.run_command("true")
    assert seen == {"since": 7.0}


def test_a_partial_first_list_keeps_the_command_waiting(tmp_path, monkeypatch):
    """A login published while another host is still being looked up does not
    end the wait: the slower host would be left out of this command's rewrite."""
    now = [0.0]
    sleeps: list[float] = []
    on_poll: dict[int, object] = {}

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds
        action = on_poll.get(len(sleeps))
        if callable(action):
            action()

    monkeypatch.setattr(workspace_tools, "_monotonic", lambda: now[0])
    monkeypatch.setattr(workspace_tools, "_sleep", sleep)
    monkeypatch.setattr(workspace_tools, "_listening", lambda _port: True)
    port_file = tmp_path / "git-proxy.port"
    lend = Path(str(port_file) + workspace_tools.GIT_PROXY_LEND_SUFFIX)
    port = 40000
    port_file.write_text(f"{port}\n")
    # GitHub is found first; Azure's lookup completes the list at the fifth poll.
    lend.write_text(f"{port} * github.com\n")
    on_poll[5] = lambda: lend.write_text(f"{port} dev.azure.com github.com\n")
    env = workspace_tools.git_proxy_env(str(port_file), PROXY_SECRET)
    assert len(sleeps) == 5
    assert _rewritten_hosts(env) == {"github.com", "dev.azure.com"}
    # Lookups still running at the bound: the command takes what was found.
    sleeps.clear()
    on_poll.clear()
    lend.write_text(f"{port} * github.com\n")
    start = now[0]
    env = workspace_tools.git_proxy_env(str(port_file), PROXY_SECRET)
    assert now[0] - start >= workspace_tools.GIT_PROXY_FIRST_LEND_WAIT_SECS
    assert _rewritten_hosts(env) == {"github.com"}
    # A partial list with nothing found yet routes nothing at the bound.
    lend.write_text(f"{port} *\n")
    assert workspace_tools.git_proxy_env(str(port_file), PROXY_SECRET) == {}


def test_the_guest_proxy_marks_a_partial_list(tmp_path):
    from kiro_crew.remote_session import git_proxy

    path = str(tmp_path / "git-proxy.port.lend")
    git_proxy._write_lend(40000, ["github.com"], path, True)
    assert Path(path).read_text() == "40000 * github.com\n"
    git_proxy._write_lend(40000, ["github.com", "*"], path, False)
    assert Path(path).read_text() == "40000 github.com\n"
