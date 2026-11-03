"""The fleet CPU budget: one cap for every lane's containers, not one per lane.

`TE_SWEBENCH_MAX_CPUS` caps ONE task container, so N concurrent lanes are an N x cap
ceiling -- six lanes at the harness default would reserve 96 cores of a shared host. The
budget is therefore written onto the cgroup that every rootless-docker container scope
shares, where it binds lanes that are ALREADY RUNNING as well as future ones. These tests
drive the shell against a fake cgroup tree, because the thing that can silently go wrong is
writing the cap to the wrong directory -- which looks exactly like success.
"""
import os
import subprocess

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SH = os.path.join(_REPO, "scripts", "launch", "fleet_budget.sh")


def _run(root, *args, env=None):
    e = dict(os.environ, TE_CGROUP_ROOT=str(root))
    e.update(env or {})
    return subprocess.run(["bash", _SH, *args], capture_output=True, text=True, env=e)


def _tree(tmp_path, *, scope=True, writable=True):
    uid = os.getuid()
    base = tmp_path / "user.slice" / f"user-{uid}.slice" / f"user@{uid}.service" / "user.slice"
    base.mkdir(parents=True)
    (base / "cpu.max").write_text("max 100000\n")
    (base / "cpu.stat").write_text("nr_throttled 0\nthrottled_usec 0\n")
    if scope:
        (base / "docker-abc123.scope").mkdir()
    if not writable:
        os.chmod(base / "cpu.max", 0o444)
    return base


def test_the_cap_lands_on_the_cgroup_the_container_scopes_share(tmp_path):
    """The scope's PARENT is the target: writing the cap on the scope itself would bind one
    container and expire with it, which is indistinguishable from success in the log."""
    base = _tree(tmp_path)
    r = _run(tmp_path, "16")
    assert r.returncode == 0, r.stderr
    assert (base / "cpu.max").read_text().strip() == "1600000 100000"
    assert "capped at 16 cores" in r.stdout
    # the scope's own cpu.max is untouched -- there is none, and none is created
    assert not (base / "docker-abc123.scope" / "cpu.max").exists()


def test_the_canonical_path_is_the_fallback_when_no_container_is_running(tmp_path):
    """A launch must be able to set the budget BEFORE the first container exists."""
    base = _tree(tmp_path, scope=False)
    r = _run(tmp_path, "8")
    assert r.returncode == 0, r.stderr
    assert (base / "cpu.max").read_text().strip() == "800000 100000"


def test_a_report_reads_the_budget_back(tmp_path):
    _tree(tmp_path)
    assert "UNCAPPED" in _run(tmp_path).stdout
    _run(tmp_path, "16")
    assert "CPU budget: 16 cores" in _run(tmp_path).stdout


def test_zero_leaves_the_cgroup_alone(tmp_path):
    """An operator who wants no budget must get the unlimited cgroup exactly, not a huge
    number that still throttles."""
    base = _tree(tmp_path)
    r = _run(tmp_path, "0")
    assert r.returncode == 0 and "not enforced" in r.stdout
    assert (base / "cpu.max").read_text().strip() == "max 100000"


@pytest.mark.skipif(getattr(os, "geteuid", lambda: 1)() == 0, reason="root writes through a read-only cpu.max")
def test_an_unwritable_cgroup_warns_and_never_fails_the_launch(tmp_path):
    """cgroup v2 need not delegate `cpu` to the user manager. When it does not, the lane
    still has to run -- but it must SAY the budget is not in force rather than imply one."""
    _tree(tmp_path, writable=False)
    r = _run(tmp_path, "16")
    assert r.returncode == 0
    assert "NOT in force" in r.stderr


def test_a_missing_cgroup_tree_warns_and_never_fails_the_launch(tmp_path):
    r = _run(tmp_path / "nothing-here", "16")
    assert r.returncode == 0
    assert "NOT in force" in r.stderr


def test_the_codex_launcher_asserts_a_budget_and_a_per_container_cap():
    """Both halves are needed and neither substitutes for the other: the cgroup cap bounds
    the fleet, and the per-container cap is what stops joblib sizing a pool to the whole
    host and thrashing inside that bound."""
    src = open(os.path.join(_REPO, "scripts", "launch", "codex_ab.sh")).read()
    assert "fleet_budget.sh" in src
    assert 'te_fleet_cpu_cap "${TE_FLEET_CPUS:-16}"' in src
    assert 'export TE_SWEBENCH_MAX_CPUS="${TE_SWEBENCH_MAX_CPUS:-4}"' in src
