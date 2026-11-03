"""scripts/scope_replay.py: how a fact's scope decides what its lifecycle sees."""
import importlib.util
import json
import os

from fixtures import write_claude_transcript

from tokeneconomizer.microskills import distiller as dist

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPT = os.path.join(_REPO, "scripts", "scope_replay.py")


def _mod():
    spec = importlib.util.spec_from_file_location("_scope_replay", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Four sessions confirm the fact on different files (it promotes and clears the firing
# gate's minimum), then one session the scope misses hits the contradiction: `tox` runs
# pytest without the command naming it.
def _confirming(name):
    return [("Bash", {"command": f"pytest -p no:cacheprovider tests/{name}"}, "1 passed"),
            ("Edit", {"file_path": f"/r/{name}.py", "old_string": "x", "new_string": "y"},
             "ok")]


_SESSIONS = [_confirming(n) for n in "abcd"] + [
    [("Bash", {"command": "tox -e py311"}, "PermissionError: .pytest_cache is read-only")],
    [("Bash", {"command": "ls"}, "a.py b.py")],
]

_SCOPED = {"id": "pytest-no-cache", "claim": "run pytest with -p no:cacheprovider",
           "detector": {"scope": r"\bpytest\b", "confirm": r"no:cacheprovider",
                        "contradict": r"\.pytest_cache is read-only"}}
_OWN_FIRED = {"id": "pytest-own-fired", "claim": "same, with its own firing condition",
              "detector": {"scope": r"\bpytest\b", "confirm": r"no:cacheprovider",
                           "contradict": r"\.pytest_cache is read-only",
                           "fired": r"\btox\b"}}


def _write(tmp_path):
    paths = []
    for i, calls in enumerate(_SESSIONS):
        paths.append(str(write_claude_transcript(str(tmp_path / f"s{i}.jsonl"), calls)))
    state = {"repo": "r", "extractors": {},
             "facts": {s["id"]: {"spec": s, "life": {}} for s in (_SCOPED, _OWN_FIRED)}}
    sp = tmp_path / "state.json"
    sp.write_text(json.dumps(state))
    return str(sp), paths


def test_counts_and_counterfactual(tmp_path):
    sp, paths = _write(tmp_path)
    res = _mod().main(["--state", sp, *paths, "--json", str(tmp_path / "out.json")])
    by = {f["id"]: f for f in res["facts"]}
    a, b = by["pytest-no-cache"], by["pytest-own-fired"]

    assert not a["own_fired"] and b["own_fired"]
    assert a["in_scope"] == 4 and a["in_confirm"] == 4 and a["in_silent"] == 0
    assert a["in_fired"] == 4                  # the gate reduced to the scope: always fired
    assert b["in_fired"] == 0                  # its own condition never matched in scope
    assert a["out_contradict"] == 1 and a["out_fired"] == 0
    assert b["out_fired"] == 1                 # fired where the scope did not look

    assert a["replay_as_is"] == {"state": "ACTIVE", "inject": True, "fire": "4/4"}
    assert a["replay_scope_blind"]["state"] == "RETIRED"
    assert not b["replay_as_is"]["inject"]     # its own gate holds it back: 0/4
    assert a["flips"] and b["flips"]

    s = res["summary"]
    assert s["read"] == 6 and s["facts"] == 2 and s["gate_is_scope"] == 1
    assert s["gate_is_scope_injected"] == 1
    assert s["with_out_of_scope_contradiction"] == 2
    assert s["with_out_of_scope_fired"] == 1
    assert s["flipped_by_scope_blind_contradiction"] == 2
    assert json.loads((tmp_path / "out.json").read_text())["summary"] == s


# A stream the plugin's way: the distiller reloaded for every session, an extractor and two
# facts admitted at the first tick (after s2), one fact's contradiction revised at the second
# (after s6), a session whose transcript is gone (s5), and a session (s0) too long for the
# saved window.
_CONFIRM_A = ("pytest -p no:cacheprovider tests/{}", "1 passed")
_TOX = ("tox -e py -- -p no:cacheprovider", "py: OK")
_LONG = ("python -c '" + "#" * (dist.BLOB_CAP + 100) + "'", "")
_STREAM = [
    ["a", _CONFIRM_A, _LONG],
    ["b", _CONFIRM_A, _TOX],
    ["c", _CONFIRM_A, _TOX],
    ["d", _TOX],
    ["e", _CONFIRM_A],
    None,
    ["g", ("ls", "a.py")],
    ["h", ("pytest tests/h", "PermissionError: .pytest_cache is read-only")],
    ["i", _CONFIRM_A],
    ["j", ("tox -e py", "PermissionError: .pytest_cache is read-only")],
    ["k", _CONFIRM_A],
]
_A = {"id": "pytest-no-cache", "claim": "run pytest with -p no:cacheprovider",
      "injection": "Run `pytest -p no:cacheprovider`: the cache directory is read-only.",
      "detector": {"scope": r"\bpytest\b", "confirm": r"no:cacheprovider",
                   "contradict": r"ZZZ-not-yet"}, "cite": ["s1", "s2"]}
_B = {"id": "tox-no-cache", "claim": "tox passes -p no:cacheprovider through",
      "injection": "Run `tox -e py -- -p no:cacheprovider`.",
      "detector": {"scope": "uses_tox", "confirm": r"no:cacheprovider"}, "cite": ["s1", "s2"]}


def _run_plugin_stream(tmp_path, monkeypatch):
    monkeypatch.setattr(dist, "PROPOSE_EVERY", 4)
    from tokeneconomizer.microskills import GENERIC
    ticks = []

    def runner(prompt, model):
        ticks.append(prompt)
        if len(ticks) == 1:
            return json.dumps({"extractors": [{"name": "uses_tox", "kind": "cmd_regex",
                                               "pattern": r"\btox\b"}],
                               "facts": [_A, _B]}), 1.0
        if len(ticks) == 2:
            revised = json.loads(json.dumps(_A))
            revised["detector"]["contradict"] = r"\.pytest_cache is read-only"
            return json.dumps({"extractors": [], "facts": [revised]}), 1.0
        return json.dumps({"extractors": [], "facts": []}), 1.0

    root = str(tmp_path / "state")
    sessions = []
    for i, spec in enumerate(_STREAM):
        if spec is None:
            tp = str(tmp_path / f"s{i}-gone.jsonl")
        else:
            name, *cmds = spec
            calls = [("Bash", {"command": c.format(name)}, o) for c, o in cmds]
            calls.append(("Edit", {"file_path": f"/r/{name}.py", "old_string": "x",
                                   "new_string": "y"}, "ok"))
            tp = str(write_claude_transcript(str(tmp_path / f"s{i}.jsonl"), calls))
        d = dist.Distiller.load("acme/tool", root, runner=runner, vocab=GENERIC)
        d.update(tp, iid=f"s{i}")
        d.save(root)
        sessions.append((f"s{i}", tp if os.path.exists(tp) else None, None))
    assert len(ticks) == 3                 # after s2, s6 and s10
    state = json.load(open(dist.Distiller._path(root, "acme/tool"), encoding="utf-8"))
    return state, sessions


def test_the_distiller_records_when_a_fact_entered_and_was_revised(tmp_path, monkeypatch):
    state, _ = _run_plugin_stream(tmp_path, monkeypatch)
    a, b = state["facts"]["pytest-no-cache"]["spec"], state["facts"]["tox-no-cache"]["spec"]
    assert a["admitted_at"] == b["admitted_at"] == 3
    assert state["extractors"]["uses_tox"]["admitted_at"] == 3
    assert a["rev"] == 2 and a["revisions"] == [
        {"at": 7, "contradict": r"\.pytest_cache is read-only", "was": "ZZZ-not-yet"}]
    assert "revisions" not in b


def test_the_replay_reproduces_the_plugin_exactly(tmp_path, monkeypatch):
    """With the recorded ticks, the replay's lifecycle is the saved one, observation for
    observation -- and only when it reads the window as the reloaded plugin held it."""
    state, sessions = _run_plugin_stream(tmp_path, monkeypatch)
    mod = _mod()
    res = mod.analyze(state, sessions, generic=True, plugin=True)
    assert res["summary"]["missing"] == 1 and res["summary"]["state_now"] == len(sessions)
    for f in res["facts"]:
        assert (f["timing"], f["admitted_at"], f["matches_saved"]) == ("recorded", 3, True)
    a = next(f for f in res["facts"] if f["id"] == "pytest-no-cache")
    assert a["in_contradict"] == 1 and a["out_contradict"] == 1   # s7 in scope, s9 not
    # s0's `pytest` fell out of the capped window text, so an in-memory reading differs
    mem = mod.analyze(state, sessions, generic=True, plugin=False)
    assert not next(f for f in mem["facts"] if f["id"] == "pytest-no-cache")["matches_saved"]


def test_an_older_state_has_its_ticks_reconstructed(tmp_path, monkeypatch):
    state, sessions = _run_plugin_stream(tmp_path, monkeypatch)
    for fd in state["facts"].values():
        fd["spec"].pop("admitted_at"), fd["spec"].pop("revisions", None)
    state["extractors"]["uses_tox"].pop("admitted_at")
    res = _mod().analyze(state, sessions, generic=True, plugin=True)
    for f in res["facts"]:
        # tick 2 and tick 3 seed s0..s1 at the positions they ended at: the same observations
        assert (f["timing"], f["matches_saved"]) == ("reconstructed", True), f
        assert f["admitted_at"] <= 3 and f["observed"] == 10
    # a saved lifecycle no tick reproduces: replayed from the first session, and said so
    state["facts"]["tox-no-cache"]["life"]["fire_den"] += 7
    f = next(f for f in _mod().analyze(state, sessions, True, True)["facts"]
             if f["id"] == "tox-no-cache")
    assert (f["timing"], f["admitted_at"], f["matches_saved"]) == ("unmatched", None, False)


def test_cell_keeps_the_latest_run_per_instance(tmp_path):
    cell = tmp_path / "cell"
    for stamp, inst in (("20260101", "i1"), ("20260102", "i2"), ("20260103", "i1")):
        d = cell / f"{stamp}__{inst}"
        d.mkdir(parents=True)
        (d / "row.json").write_text(json.dumps({"instance": inst, "resolved": True}))
        write_claude_transcript(str(d / "transcript.jsonl"), _SESSIONS[0])
    got = _mod().cell_sessions(str(cell))
    assert [(i, os.path.basename(os.path.dirname(t))) for i, t, _ in got] == \
        [("i2", "20260102__i2"), ("i1", "20260103__i1")]
