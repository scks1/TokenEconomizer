#!/usr/bin/env python3
"""Replay a micro-skills state over recorded sessions and report how each learned fact's
`scope` decides what its lifecycle sees.

A fact the proposer writes carries a detector: `scope` picks the sessions where the fact is
relevant, `confirm` and `contradict` say whether it held there, and `fired` says whether the
waste it prevents occurred (`microskills.distiller.Detector`). A session outside the scope
records nothing, not even a contradiction, and `fired` falls back to `scope` when the
proposer gives none, which leaves the firing gate (`lifecycle.FIRE_GATE`) nothing to check:
every session in scope then counts as one where the fact fired. This script measures how
much that matters on real sessions, with no model calls:

    python scripts/scope_replay.py --cell $TE_DATA/results/<tree>/<arm>/<cell>
    python scripts/scope_replay.py --plugin ~/.tokeneconomizer/repos/<key>/<agent>
    python scripts/scope_replay.py --state manual_state/<repo>.json T1.jsonl T2.jsonl ...

For every fact in the saved state, over the sessions its lifecycle saw:

  * gate        -- whether the fact names its own `fired`, or the gate reduces to the scope
                   (a `fired` regex is read over commands only, as the detector reads it);
  * in scope    -- sessions the scope selected, and how many of them confirmed, contradicted,
                   or matched neither (the scope matched, but nothing showed the fact mattering);
  * out of scope -- sessions the scope missed where `confirm`, `contradict` or an own `fired`
                   matched anyway (the fact was at stake, but its lifecycle never heard);
  * counterfactual -- the fact's lifecycle replayed twice: as the plugin reads it, and with
                   contradictions counted wherever they match; the two differ only in scope.

The replay follows the distiller's clock. A fact enters at the tick its `admitted_at` records,
seeded by the window before it, with the contradiction condition in force at each session
(`revisions`); an extractor computes its feature from its own `admitted_at` on. A state written
before these were recorded has its facts' ticks reconstructed: the tick whose replay gives
back the saved lifecycle exactly (`ambiguous` when ticks that do observe different sessions,
`unmatched` when none does -- such a fact is replayed from the first session, its extractors
taken as always there). Either way the
report says whether the replay reproduces the saved lifecycle, which is the check that the
sessions given are the ones the distiller read, in its order. Only the proposer's facts
(`manual_state`) are read; the envfacts layer has no scope regex. Results print to stdout (and `--json`), never into the
repository. Stdlib only.
"""
import argparse
import glob
import json
import os
import sys

_EXPERIMENTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.dirname(_EXPERIMENTS)]
from tokeneconomizer.microskills import distiller as dist          # noqa: E402
from tokeneconomizer.microskills.lifecycle import Lifecycle        # noqa: E402


# ---- inputs ------------------------------------------------------------------
def cell_sessions(cell):
    """An experiments cell: run dirs `20*/` in order, the latest run per instance kept."""
    runs = {}
    for rowf in sorted(glob.glob(os.path.join(cell, "20*", "row.json"))):
        run = os.path.dirname(rowf)
        try:
            row = json.load(open(rowf, encoding="utf-8"))
        except ValueError:
            row = {}
        inst = row.get("instance") or row.get("instance_id") or run
        tp = os.path.join(run, "transcript.jsonl")
        if os.path.exists(tp):
            runs.pop(inst, None)                   # a re-run moves to its own place in order
            runs[inst] = (inst, tp, row.get("resolved"))
    return list(runs.values())


def plugin_sessions(agent_dir):
    """The plugin's per-agent directory: `sessions/*.json`, in the order they ended (the
    distiller folds a session in when it ends). A session whose transcript is gone -- an agent
    deletes its old logs -- keeps its place, unread, so the clock stays aligned."""
    recs = []
    for p in glob.glob(os.path.join(agent_dir, "sessions", "*.json")):
        try:
            s = json.load(open(p, encoding="utf-8"))
        except ValueError:
            continue
        tp = s.get("transcript")
        recs.append((s.get("ended") or s.get("started") or 0, s.get("session_id") or p,
                     tp if tp and os.path.exists(tp) else None))
    return [(sid, tp, None) for _, sid, tp in sorted(recs)]


def find_state(root):
    hits = sorted(glob.glob(os.path.join(root, dist.STATE_SUBDIR, "*.json")))
    if len(hits) != 1:
        raise SystemExit(f"expected one {dist.STATE_SUBDIR}/*.json under {root}, found "
                         f"{len(hits)}; pass --state")
    return hits[0]


# ---- the sessions, as the distiller saw them ---------------------------------
class Timeline:
    """Every session at its index in the distiller's clock (`Distiller.now`, which a session
    that cannot be read still advances), with what the distiller had of it at a given time.

    `plugin`: the plugin reloads the distiller for every session, so a window digest other than
    the newest is the saved one, its text capped (`Distiller.save`). An extractor admitted at
    tick T computes its feature for sessions T on as they end, and for the sessions then in the
    window from their saved text (`_admit_extractor`); one with no recorded tick is taken to
    have been there from the start."""

    def __init__(self, repo, sessions, generic, extractors, plugin):
        self.plugin = plugin
        self.exts = {n: (dist.Extractor(sp), sp.get("admitted_at"))
                     for n, sp in (extractors or {}).items()}
        self.base, self.cmds = [], []
        for iid, tp, res in sessions:
            dg = cmds = None
            if tp:
                try:
                    cmds = dist.bash_cmds(dist.load_calls(tp))
                    dg = dist.build_digest(repo, tp, iid, res, generic=generic)
                except Exception:
                    dg = None
            self.base.append(dg)
            self.cmds.append(cmds if dg is not None else None)
        self.read = [i for i, dg in enumerate(self.base) if dg is not None]

    def window_at(self, t):
        """The sessions in the distiller's window when tick `t` ran (after session t-1)."""
        return [i for i in self.read if i < t][-dist.WINDOW:]

    def _text(self, i, t):
        dg = self.base[i]
        if self.plugin and i < t - 1:
            dg = dict(dg, _cmd_blob=dg["_cmd_blob"][-dist.BLOB_CAP:],
                      _out_blob=dg["_out_blob"][-2 * dist.BLOB_CAP:])
        return dg

    def view(self, i, t):
        """Session i's digest as the distiller held it at time t (t == i: as it ended)."""
        dg = dict(self._text(i, t))
        feats = {}
        for name, (ex, at) in self.exts.items():
            try:
                if at is None or at <= i:
                    feats[name] = ex.compute(self.cmds[i])
                elif at <= t and i in self.window_at(at):
                    feats[name] = ex.compute(dist._reparse_cmds(self._text(i, at)))
            except Exception:
                feats[name] = False
        dg["features"] = feats
        return dg


# ---- one fact ----------------------------------------------------------------
def own_fired(spec):
    det = spec.get("detector", {})
    return bool(det.get("fired")) and det.get("fired") != det.get("scope")


def contradict_at(spec, t):
    """The contradiction condition in force from tick t on (`revisions`, oldest first)."""
    revs = sorted(spec.get("revisions") or [], key=lambda r: r["at"])
    cur = revs[0]["was"] if revs else spec.get("detector", {}).get("contradict")
    for r in revs:
        if r["at"] <= t:
            cur = r["contradict"]
    return cur


def read(spec, dg, contradict):
    """Every condition of a fact's detector on one session, the scope ignored."""
    det = dist.DetectorV3Scope(dict(spec.get("detector", {}), contradict=contradict),
                               dg.get("features", {}))
    blob = dg["_cmd_blob"] + "\n" + dg["_out_blob"]
    return {"scope": det._exercised(dg),
            "confirm": bool(det.confirm and det.confirm.search(blob)),
            "contradict": bool(det.contra and det.contra.search(blob)),
            "fired": det._fired(dg)}


def steps(tl, admitted):
    """(session, clock, time read) for each observation of a fact admitted at `admitted`:
    the window seeds it at the positions `Distiller._build_fact` gives them, then every
    later session as it ends. `None`: observed from the first session."""
    if admitted is None:
        return [(i, i, i) for i in tl.read]
    win = tl.window_at(admitted)
    return ([(i, admitted - len(win) + j, admitted) for j, i in enumerate(win)]
            + [(i, i, i) for i in tl.read if i >= admitted])


def replay(fid, spec, tl, admitted, scope_blind_contradict):
    """The fact's lifecycle as `Fact.observe` drives it: every observation is an update, one
    outside the scope with no verdict. With `scope_blind_contradict`, a contradiction outside
    the scope counts too; nothing else changes, and it does not enter the firing counts.
    Returns the lifecycle and each observation's reading."""
    life = Lifecycle(fid, bool(spec.get("high_stakes", False)))
    rows = []
    for i, now, t in steps(tl, admitted):
        dg = tl.view(i, t)
        r = read(spec, dg, contradict_at(spec, t))
        files = set(dg["edited_files"])
        if r["scope"]:                              # Detector.verdict, in scope
            verdict = ("contradict" if r["contradict"] else
                       "confirm" if r["confirm"] else None)
            life.update(now, verdict, True if r["contradict"] else r["fired"], True, files)
        else:
            verdict = "contradict" if scope_blind_contradict and r["contradict"] else None
            life.update(now, verdict, False, False, files)
        rows.append(r)
    return life, rows


_LIFE_KEYS = ("state", "confirms", "contradicts", "fire_num", "fire_den")


def same_life(life, saved):
    """The replayed lifecycle is the saved one, observation for observation."""
    if not saved or "state" not in saved:
        return False
    got = json.loads(json.dumps({k: getattr(life, k) for k in _LIFE_KEYS}))
    return all(got[k] == saved.get(k, got[k] if k == "state" else None) for k in _LIFE_KEYS)


def timing(fid, spec, saved, tl):
    """When the fact entered: recorded (`admitted_at`), or else every tick whose replay gives
    back the saved lifecycle exactly. A fact whose contradiction was revised before the
    revisions were recorded matches no tick, and is replayed from the first session."""
    at = spec.get("admitted_at")
    if at is not None:
        return at, "recorded", same_life(replay(fid, spec, tl, at, False)[0], saved)
    hits = [t for t in range(1, len(tl.base) + 1)
            if same_life(replay(fid, spec, tl, t, False)[0], saved)]
    if not hits:
        return None, "unmatched", False
    # ticks whose replays observe the same sessions are one answer: a window that reaches
    # back to the first session seeds them at the positions they would have ended at
    seen = {tuple(i for i, _, _ in steps(tl, t)) for t in hits}
    return hits[0], ("reconstructed" if len(seen) == 1 else f"ambiguous({len(seen)})"), True


def _out(life):
    return {"state": life.state, "inject": life.injectable(),
            "fire": f"{life.fire_num}/{life.fire_den}"}


def analyze(state, sessions, generic, plugin=False):
    repo = state.get("repo", "")
    tl = Timeline(repo, sessions, generic, state.get("extractors"), plugin)
    facts = []
    for fid, fd in sorted((state.get("facts") or {}).items()):
        spec, saved = fd["spec"], fd.get("life", {})
        admitted, how, matches = timing(fid, spec, saved, tl)
        life, rows = replay(fid, spec, tl, admitted, False)
        blind, _ = replay(fid, spec, tl, admitted, True)
        ins = [r for r in rows if r["scope"]]
        outs = [r for r in rows if not r["scope"]]
        own = own_fired(spec)
        f = {
            "id": fid,
            "saved_state": saved.get("state"),
            "saved_fire": f"{saved.get('fire_num', 0)}/{saved.get('fire_den', 0)}",
            "high_stakes": bool(spec.get("high_stakes", False)),
            "admitted_at": admitted, "timing": how, "matches_saved": matches,
            "observed": len(rows),
            "own_fired": own,
            "in_scope": len(ins),
            "in_confirm": sum(1 for r in ins if r["confirm"] and not r["contradict"]),
            "in_contradict": sum(1 for r in ins if r["contradict"]),
            "in_silent": sum(1 for r in ins if not (r["confirm"] or r["contradict"])),
            "in_fired": sum(1 for r in ins if r["fired"] or r["contradict"]),
            "out_confirm": sum(1 for r in outs if r["confirm"]),
            "out_contradict": sum(1 for r in outs if r["contradict"]),
            "out_fired": sum(1 for r in outs if own and r["fired"]),
            "replay_as_is": _out(life),
            "replay_scope_blind": _out(blind),
        }
        f["flips"] = f["replay_as_is"]["state"] != f["replay_scope_blind"]["state"] \
            or f["replay_as_is"]["inject"] != f["replay_scope_blind"]["inject"]
        facts.append(f)
    summary = {
        "sessions": len(sessions), "read": len(tl.read), "state_now": state.get("now"),
        "missing": sum(1 for _, tp, _ in sessions if not tp),
        "facts": len(facts),
        "timing": {k: sum(1 for f in facts if f["timing"].split("(")[0] == k)
                   for k in ("recorded", "reconstructed", "ambiguous", "unmatched")},
        "matches_saved": sum(1 for f in facts if f["matches_saved"]),
        "gate_is_scope": sum(1 for f in facts if not f["own_fired"]),
        "gate_is_scope_injected": sum(1 for f in facts if not f["own_fired"]
                                      and f["replay_as_is"]["inject"]),
        "with_out_of_scope_contradiction": sum(1 for f in facts if f["out_contradict"]),
        "with_out_of_scope_fired": sum(1 for f in facts if f["out_fired"]),
        "flipped_by_scope_blind_contradiction": sum(1 for f in facts if f["flips"]),
    }
    return {"repo": repo, "generic": generic, "plugin": plugin, "summary": summary,
            "facts": facts}


# ---- report ------------------------------------------------------------------
def report(res, out=sys.stdout):
    s = res["summary"]
    w = out.write
    w(f"repo {res['repo']}: {s['read']}/{s['sessions']} sessions read "
      f"({'generic' if res['generic'] else 'paper'} digest"
      f"{', reloaded every session' if res['plugin'] else ''}), {s['facts']} facts\n")
    if s["missing"]:
        w(f"  {s['missing']} sessions have no transcript left; they keep their place unread\n")
    if s["state_now"] is not None and s["state_now"] != s["sessions"]:
        w(f"  the state counted {s['state_now']} sessions, {s['sessions']} were given: the "
          f"replay's clock is not the distiller's\n")
    t = s["timing"]
    w(f"  admission: {t['recorded']} recorded, {t['reconstructed']} reconstructed, "
      f"{t['ambiguous']} ambiguous, {t['unmatched']} unmatched (replayed from session 0); "
      f"replay reproduces the saved lifecycle: {s['matches_saved']}/{s['facts']}\n")
    w(f"  firing gate reduces to the scope (no own `fired`): {s['gate_is_scope']} "
      f"({s['gate_is_scope_injected']} of them injectable in the replay)\n")
    w(f"  contradicted outside their scope: {s['with_out_of_scope_contradiction']}; "
      f"fired outside their scope: {s['with_out_of_scope_fired']}\n")
    w(f"  state or injection changed when contradictions count regardless of scope: "
      f"{s['flipped_by_scope_blind_contradiction']}\n\n")
    if not res["facts"]:
        return
    w(f"  {'fact':28s} {'gate':5s} {'scope':>5s} {'conf':>4s} {'ctra':>4s} {'none':>4s} "
      f"{'fire':>4s} | {'o.cnf':>5s} {'o.ctr':>5s} {'o.fir':>5s} | {'from':>4s} {'how':5s} "
      f"{'saved':18s} {'as-is':18s} {'blind':18s}\n")
    for f in res["facts"]:
        def st(x):
            return f"{x['state'][:7]}{'+inj' if x['inject'] else ''} {x['fire']}"
        saved = f"{(f['saved_state'] or '?')[:7]} {f['saved_fire']}"
        w(f"  {f['id'][:28]:28s} {'own' if f['own_fired'] else 'scope':5s} "
          f"{f['in_scope']:5d} {f['in_confirm']:4d} {f['in_contradict']:4d} "
          f"{f['in_silent']:4d} {f['in_fired']:4d} | {f['out_confirm']:5d} "
          f"{f['out_contradict']:5d} {f['out_fired']:5d} | "
          f"{'-' if f['admitted_at'] is None else f['admitted_at']:>4} "
          f"{f['timing'][:4] + ('=' if f['matches_saved'] else ' '):5s} {saved:18s} "
          f"{st(f['replay_as_is']):18s} {st(f['replay_scope_blind']):18s}"
          f"{'  <- flips' if f['flips'] else ''}\n")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--cell", help="an experiments cell: <tree>/<arm>/<cell>")
    src.add_argument("--plugin", help="the plugin's ~/.tokeneconomizer/repos/<key>/<agent>")
    ap.add_argument("--state", help="a manual_state/<repo>.json (default: found in the source)")
    ap.add_argument("transcripts", nargs="*", help="transcripts in session order (with --state)")
    dg = ap.add_mutually_exclusive_group()
    dg.add_argument("--generic", dest="generic", action="store_true", default=None,
                    help="the plugin's digest (default with --plugin)")
    dg.add_argument("--paper", dest="generic", action="store_false",
                    help="the experiments' digest (default with --cell or transcripts)")
    ap.add_argument("--reloaded", action="store_true",
                    help="the distiller was reloaded every session, as the plugin runs it "
                         "(default with --plugin)")
    ap.add_argument("--json", help="also write the full result here")
    a = ap.parse_args(argv)
    if a.cell:
        sessions, state_path = cell_sessions(a.cell), a.state or find_state(a.cell)
    elif a.plugin:
        root = os.path.join(a.plugin, "mechanisms")
        sessions, state_path = plugin_sessions(a.plugin), a.state or find_state(root)
    elif a.state and a.transcripts:
        sessions, state_path = [(os.path.basename(t), t, None) for t in a.transcripts], a.state
    else:
        ap.error("give --cell, --plugin, or --state with transcripts")
    generic = a.generic if a.generic is not None else bool(a.plugin)
    res = analyze(json.load(open(state_path, encoding="utf-8")), sessions, generic,
                  plugin=bool(a.plugin or a.reloaded))
    report(res)
    if a.json:
        json.dump(res, open(a.json, "w", encoding="utf-8"), indent=1)
    return res


if __name__ == "__main__":
    main()
