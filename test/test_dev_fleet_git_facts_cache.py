"""The fleet view's per-worktree git facts are cached on stat-only keys.

``git_facts._git_facts`` re-runs ``_git_info`` only when the worktree's own key
(HEAD file, branch ref, index stat) changes. When only the base ref moves, the
behind count comes from one shared ``for-each-ref`` in the main checkout and
only an expired dirty read runs in the worktree. ``_git_ahead_cached`` counts
``git cherry`` once per entry and keeps a zero across a forward base move. The discard path
(``_dirty_split`` / ``_real_dirty``) stays uncached because it decides what a
removal deletes.

Runs real git in ``tmp_path``. Every spawn goes through ``runtime._run_cmd``;
the test swaps it for a counting runner that spawns the same argv unsandboxed.
"""

from __future__ import annotations

import os
import subprocess
import time
from types import SimpleNamespace

import pytest

from kiro_crew.apps.builtins.dev_fleet import fleet_state, git_facts, repository, runtime


def _env():
    return {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@e",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@e",
    }


def _sh(cwd, *args):
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout.strip()


def _settle(checkout):
    """Make the index not "racily clean", so ``git status`` leaves it alone.

    A file written in the same second as the index makes ``git status``
    rewrite the index on every call, which (correctly) moves the cache key.
    Backdating the files and refreshing the index once ends that.
    """
    for name in _sh(checkout, "ls-files").splitlines():
        p = checkout / name
        st = p.stat()
        os.utime(p, (st.st_atime - 60, st.st_mtime - 60))
    _sh(checkout, "update-index", "-q", "--really-refresh")


def _commit(checkout, name):
    (checkout / name).write_text(name + "\n")
    _sh(checkout, "add", name)
    _sh(checkout, "commit", "-q", "-m", name)
    return _sh(checkout, "rev-parse", "HEAD")


class Repo:
    """A primary checkout with ``origin/main``, plus two linked worktrees.

    ``feat`` carries one commit of its own (ahead 1); ``even`` sits on the
    base commit (ahead 0).
    """

    def __init__(self, tmp_path):
        self.main = tmp_path / "main"
        self.main.mkdir()
        _sh(self.main, "init", "-q", "-b", "main")
        _commit(self.main, "one")
        _sh(self.main, "update-ref", "refs/remotes/origin/main", "HEAD")
        self.feat = tmp_path / "feat"
        _sh(self.main, "worktree", "add", "-q", "-b", "feat", str(self.feat))
        _commit(self.feat, "two")
        self.even = tmp_path / "even"
        _sh(self.main, "worktree", "add", "-q", "-b", "even", str(self.even))
        for checkout in (self.main, self.feat, self.even):
            _settle(checkout)

    def move_base(self, oid=None):
        """Point ``origin/main`` at ``oid``, or at a new commit on ``main``."""
        if oid is None:
            oid = _commit(self.main, f"base-{_sh(self.main, 'rev-list', '--count', 'HEAD')}")
            _settle(self.main)
        _sh(self.main, "update-ref", "refs/remotes/origin/main", oid)
        return oid


@pytest.fixture
def repo(tmp_path, monkeypatch):
    r = Repo(tmp_path)
    monkeypatch.setattr(repository, "_repo", lambda: str(r.main))
    return r


@pytest.fixture
def spawns(monkeypatch):
    """Count every command dev_fleet spawns; run it for real, unsandboxed."""
    calls: list[list[str]] = []

    async def run_cmd(cmd, *, cwd=None, env=None, timeout=30, mode="standard", pre_spawn=None):
        calls.append(list(cmd))
        p = subprocess.run(
            cmd, cwd=cwd, env=_env(), capture_output=True, text=True, encoding="utf-8"
        )
        return p.returncode, p.stdout, p.stderr

    monkeypatch.setattr(runtime, "_run_cmd", run_cmd)
    monkeypatch.setattr(repository, "_UPSTREAM_REMOTE", "origin")
    monkeypatch.setattr(repository, "BASE_BRANCH", "main")
    monkeypatch.setattr(git_facts, "_GIT_FACTS_CACHE", {})
    monkeypatch.setattr(git_facts, "_BASE_SNAPSHOT", {"key": None, "oid": None, "behind": {}})
    monkeypatch.setattr(git_facts, "_BASE_ANCESTORS", {})
    monkeypatch.setattr(fleet_state, "_CTX_LOG_CACHE", {})
    return calls


def _in(calls, checkout):
    """The spawned git commands that ran in ``checkout``."""
    return [c for c in calls if c[:3] == ["git", "-C", str(checkout)]]


@pytest.mark.asyncio
@pytest.mark.parametrize("which", ["main", "feat"])
async def test_repeat_call_with_unchanged_key_spawns_no_git(spawns, repo, which):
    path = str(getattr(repo, which))
    first = await git_facts._git_facts(path)
    await git_facts._git_ahead_cached(path)
    assert spawns, "the first call must read git"
    assert first[1] is not None
    spawns.clear()

    again = await git_facts._git_facts(path)
    ahead = await git_facts._git_ahead_cached(path)

    assert spawns == []
    assert again == first
    assert ahead == (1 if which == "feat" else 0)


@pytest.mark.asyncio
async def test_new_commit_on_head_invalidates(spawns, repo):
    info, _ = await git_facts._git_facts(str(repo.feat))
    assert await git_facts._git_ahead_cached(str(repo.feat)) == 1
    head = _commit(repo.feat, "three")
    spawns.clear()

    info2, _ = await git_facts._git_facts(str(repo.feat))
    ahead = await git_facts._git_ahead_cached(str(repo.feat))

    assert _in(spawns, repo.feat), "a moved HEAD must re-read git"
    assert ahead == 2
    assert info2["head_oid"] == head != info["head_oid"]


@pytest.mark.asyncio
async def test_branch_switch_invalidates(spawns, repo):
    await git_facts._git_facts(str(repo.feat))
    _sh(repo.feat, "checkout", "-q", "-b", "other")
    spawns.clear()

    info, _ = await git_facts._git_facts(str(repo.feat))

    assert _in(spawns, repo.feat)
    assert info["branch"] == "other"


@pytest.mark.asyncio
async def test_index_change_invalidates(spawns, repo):
    info, _ = await git_facts._git_facts(str(repo.feat))
    assert info["dirty"] is False
    (repo.feat / "one").write_text("edited\n")
    _sh(repo.feat, "add", "one")
    spawns.clear()

    info2, _ = await git_facts._git_facts(str(repo.feat))

    assert _in(spawns, repo.feat), "a rewritten index must re-read git"
    assert info2["dirty"] is True


@pytest.mark.asyncio
async def test_moved_base_rereads_behind_once_and_nothing_in_the_worktree(spawns, repo):
    for wt in (repo.feat, repo.even):
        info, _ = await git_facts._git_facts(str(wt))
        assert info["behind"] == 0
    repo.move_base()
    spawns.clear()

    infos = [(await git_facts._git_facts(str(wt)))[0] for wt in (repo.feat, repo.even)]

    assert [i["behind"] for i in infos] == [1, 1]
    assert _in(spawns, repo.feat) == _in(spawns, repo.even) == []
    assert [c[3] for c in spawns] == ["for-each-ref"], "one shared read for every worktree"


@pytest.mark.asyncio
async def test_moved_base_counts_a_detached_head_alone(spawns, repo):
    _sh(repo.even, "checkout", "-q", "--detach")
    info, _ = await git_facts._git_facts(str(repo.even))
    assert info["behind"] == 0
    repo.move_base()
    spawns.clear()

    info2, _ = await git_facts._git_facts(str(repo.even))

    assert info2["behind"] == 1
    assert [c[3] for c in _in(spawns, repo.even)] == ["rev-list"]


@pytest.mark.asyncio
async def test_forward_base_move_keeps_ahead_zero_without_cherry(spawns, repo):
    path = str(repo.even)
    await git_facts._git_facts(path)
    assert await git_facts._git_ahead_cached(path) == 0
    repo.move_base()
    spawns.clear()

    await git_facts._git_facts(path)
    ahead = await git_facts._git_ahead_cached(path)

    assert ahead == 0
    assert not [c for c in spawns if "cherry" in c]


@pytest.mark.asyncio
async def test_rewound_base_recounts_ahead(spawns, repo):
    path = str(repo.feat)
    await git_facts._git_facts(path)
    assert await git_facts._git_ahead_cached(path) == 1
    # Base fast-forwards onto feat's commit: nothing left to ship.
    repo.move_base(_sh(repo.feat, "rev-parse", "HEAD"))
    await git_facts._git_facts(path)
    assert await git_facts._git_ahead_cached(path) == 0
    # Base rewinds to the first commit: the old base is not an ancestor any more.
    repo.move_base(_sh(repo.main, "rev-parse", "HEAD"))
    spawns.clear()

    info, _ = await git_facts._git_facts(path)
    ahead = await git_facts._git_ahead_cached(path)

    assert [c for c in spawns if "cherry" in c], "a rewound base must recount"
    assert ahead == 1
    assert info["behind"] == 0


@pytest.mark.asyncio
async def test_packed_base_move_invalidates(spawns, repo):
    _sh(repo.main, "pack-refs", "--all")
    await git_facts._git_facts(str(repo.feat))
    oid = _commit(repo.main, "packed")
    _sh(repo.main, "update-ref", "refs/remotes/origin/main", oid)
    _sh(repo.main, "pack-refs", "--all")
    spawns.clear()

    info, _ = await git_facts._git_facts(str(repo.feat))

    assert spawns
    assert info["behind"] == 1


@pytest.mark.asyncio
async def test_base_snapshot_failure_falls_back_once_per_base_move(spawns, repo, monkeypatch):
    real = runtime._run_cmd

    async def no_ahead_behind(cmd, **kw):
        if "for-each-ref" in cmd:  # a git older than 2.41
            spawns.append(list(cmd))
            return 128, "", "fatal: unknown field name: ahead-behind"
        return await real(cmd, **kw)

    monkeypatch.setattr(runtime, "_run_cmd", no_ahead_behind)
    for wt in (repo.feat, repo.even):
        await git_facts._git_facts(str(wt))
    repo.move_base()
    spawns.clear()

    infos = [(await git_facts._git_facts(str(wt)))[0] for wt in (repo.feat, repo.even)]

    assert [i["behind"] for i in infos] == [1, 1]
    for wt in (repo.feat, repo.even):
        assert [c[3] for c in _in(spawns, wt)] == ["rev-list"], "per-worktree count"
    assert sum(1 for c in spawns if "for-each-ref" in c) == 1


@pytest.mark.asyncio
async def test_discard_path_stays_uncached(spawns, repo):
    path = str(repo.feat)
    await git_facts._git_facts(path)
    (repo.feat / "new.txt").write_text("untracked\n")
    spawns.clear()

    tracked, untracked = await repository._dirty_split(path)
    first = len(spawns)
    tracked2, untracked2 = await repository._dirty_split(path)
    dirty = await repository._real_dirty(path)

    # Each call ran git again: twice for both _dirty_split calls, once more for _real_dirty.
    assert first > 0
    assert len(spawns) == 2 * first + 1
    assert (tracked, untracked) == (tracked2, untracked2) == (False, ["new.txt"])
    assert dirty is True


@pytest.mark.asyncio
@pytest.mark.parametrize("dirt", ["unstaged edit", "untracked file"])
async def test_dirt_no_key_sees_is_reread_after_dirty_ttl(spawns, repo, monkeypatch, dirt):
    path = str(repo.feat)
    info, _ = await git_facts._git_facts(path)
    assert info["dirty"] is False
    if dirt == "unstaged edit":
        (repo.feat / "two").write_text("edited\n")
    else:
        (repo.feat / "new.txt").write_text("untracked\n")
    spawns.clear()

    within, _ = await git_facts._git_facts(path)
    assert (within["dirty"], spawns) == (False, []), "inside the TTL the cached value stands"

    monkeypatch.setattr(git_facts, "_DIRTY_TTL", 0.0)
    after, _ = await git_facts._git_facts(path)

    assert after["dirty"] is True
    assert [c[3:6] for c in spawns] == [["--no-optional-locks", "status", "--porcelain"]]
    assert str(repo.feat) in git_facts._GIT_FACTS_CACHE, "the refresh keeps the entry"


@pytest.mark.asyncio
async def test_moved_base_refreshes_expired_dirty(spawns, repo, monkeypatch):
    path = str(repo.feat)
    info, _ = await git_facts._git_facts(path)
    assert info["dirty"] is False
    (repo.feat / "two").write_text("edited\n")
    monkeypatch.setattr(git_facts, "_DIRTY_TTL", 0.0)
    repo.move_base()
    spawns.clear()

    after, _ = await git_facts._git_facts(path)

    assert after["dirty"] is True
    assert [c[3:] for c in _in(spawns, repo.feat)] == [
        ["--no-optional-locks", "status", "--porcelain"]
    ]
    assert after["behind"] == 1


@pytest.mark.asyncio
async def test_key_moving_during_the_read_is_not_cached(spawns, repo, monkeypatch):
    keys = iter([(("before",), ("b",)), (("after",), ("b",))])
    monkeypatch.setattr(git_facts, "_git_facts_key", lambda *_a: next(keys))

    _, key = await git_facts._git_facts(str(repo.feat))

    assert key is None
    assert str(repo.feat) not in git_facts._GIT_FACTS_CACHE


@pytest.mark.asyncio
async def test_unfingerprintable_path_is_not_cached(spawns, tmp_path):
    path = str(tmp_path / "not-a-checkout")

    _, key = await git_facts._git_facts(path)

    assert key is None
    assert path not in git_facts._GIT_FACTS_CACHE


@pytest.mark.asyncio
async def test_context_log_reused_until_key_changes(spawns, repo):
    path = str(repo.feat)
    _, key = await git_facts._git_facts(path)
    assert key is not None
    ctx = await fleet_state._build_context("feat", path, None, key)
    spawns.clear()

    again = await fleet_state._build_context("feat", path, None, key)
    assert spawns == []
    assert again == ctx
    assert ctx["summary"] == "two"

    await fleet_state._build_context("feat", path, None, (*key[:-1], ("changed",)))
    assert [c for c in spawns if "log" in c]


@pytest.mark.asyncio
async def test_context_log_rereads_after_a_base_move(spawns, repo):
    """The log covers `<base>..HEAD`, so a moved base must re-read it."""
    path = str(repo.feat)
    _, key = await git_facts._git_facts(path)
    await fleet_state._build_context("feat", path, None, key)
    repo.move_base(_sh(repo.feat, "rev-parse", "HEAD"))  # base absorbs feat's commit
    _, key2 = await git_facts._git_facts(path)
    spawns.clear()

    ctx = await fleet_state._build_context("feat", path, None, key2)

    assert key2 != key
    assert [c for c in spawns if "log" in c]
    assert ctx["summary"] is None


@pytest.mark.asyncio
async def test_retain_drops_vanished_worktrees(spawns, repo):
    await git_facts._git_facts(str(repo.main))
    await git_facts._git_facts(str(repo.feat))

    git_facts._git_facts_retain({str(repo.main)})

    assert set(git_facts._GIT_FACTS_CACHE) == {str(repo.main)}


@pytest.mark.asyncio
async def test_cached_passes_do_not_refire_gh_before_the_pr_ttl(monkeypatch):
    """A cached fleet pass takes about a second, so `_PR_TTL` alone paces `gh pr list`.

    300 s is the refresh a 330-370 s uncached pass gave before; 55 s would fire
    each open branch's query every minute.
    """
    assert fleet_state._PR_TTL >= 300
    now = [1_000.0]
    # A private clock for fleet_state only; the shared `time` module stays real.
    monkeypatch.setattr(
        fleet_state, "time", SimpleNamespace(time=lambda: now[0], monotonic=time.monotonic)
    )
    monkeypatch.setattr(fleet_state, "_PR_CACHE", {})
    fetches: list[str] = []

    async def fetch(branch):
        fetches.append(branch)
        return {"number": 1, "state": "OPEN"}

    monkeypatch.setattr(fleet_state, "_fetch_pr_status", fetch)

    for _ in range(int(fleet_state._PR_TTL) // 12):  # one poll every 12 s
        await fleet_state._pr_status_cached("feat", "a" * 40)
        now[0] += 12
    assert fetches == ["feat"]

    now[0] = 1_000.0 + fleet_state._PR_TTL + 1
    await fleet_state._pr_status_cached("feat", "a" * 40)
    assert fetches == ["feat", "feat"]
