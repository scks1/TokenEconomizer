# Reproducing the experiments

This directory is the code behind the paper's results: a harness that runs Claude Code, Codex
and the Copilot CLI on SWE-Bench and Web-Bench, the launchers, and the aggregation scripts. It
is not part of the plugin; the method is described in the [main README](../README.md).

| Path | Contents |
|---|---|
| `te_harness/` | runs an agent CLI in each SWE-Bench task's Docker image (on the host for Web-Bench) and grades it with the benchmark's own grader |
| `te_bench/` | the experiment arms, the benchmark adapters, and the evaluated task sets (`workloads.py`) |
| `scripts/` | `launch/` (the launchers), `run_experiment.py` (one arm on one task stream), `aggregate.py`, `compare_arms.py` and `scope_replay.py` |
| `workloads/` | the evaluated task ids: `swebench.json` (277 tasks), `webbench.json` (102), and the security sets |
| `tests/` | offline tests: no Docker, data or API keys |

## Requirements

- Linux x86_64 with Docker usable without `sudo`, and about 200 GB of disk for the SWE-Bench
  images.
- Python 3.10 or newer, git, and Node.js with npm (for Web-Bench).
- An Anthropic API key and an OpenAI API key. Claude Code and the Copilot CLI bill the first,
  Codex the second.
- The agent CLIs on the host, at the versions the paper used. The harness mounts them into
  each task container.

```bash
curl -fsSL https://claude.ai/install.sh | bash -s 2.1.170          # Claude Code
npm install -g @openai/codex@0.153.1                                 # Codex
curl -fsSL https://gh.io/copilot-install | VERSION=1.0.84-7 bash     # Copilot CLI
```

## Setup

**1. Install and check the code.** Every command after `cd experiments` runs from this directory.

```bash
git clone https://github.com/microsoft/TokenEconomizer.git && cd TokenEconomizer
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[test,baselines]'        # baselines: aider, for the RepoMap arms
cd experiments
python -m pytest tests -q
bash scripts/launch/smoke_test.sh         # every arm wired end to end, with a stub agent
```

**2. SWE-Bench.** Pick a data directory outside the repository, and use its canonical path:
Web-Bench fails every test when reached through a symlink.

```bash
export DATA="$(readlink -f ~/te_data)"
git clone https://github.com/SWE-bench/SWE-bench.git "$DATA/SWE-bench"
pip install -e "$DATA/SWE-bench"          # the official grader, into this venv
python scripts/export_swebench_tasks.py --out-dir "$DATA/swebench"
docker login                              # anonymous pulls are rate-limited
xargs -a "$DATA/swebench/image_tags.txt" -P 4 -n 1 docker pull -q
```

The export writes the task list, the grading file and the list of the 277 task images.

**3. Web-Bench.** The five evaluated projects, built, with the Playwright browser they pin:

```bash
git clone https://github.com/bytedance/web-bench.git "$DATA/web-bench"
( cd "$DATA/web-bench" && npm install -g @microsoft/rush && rush install \
  && rush build --to @web-bench/eval --to @web-bench/calculator --to @web-bench/expressjs \
                --to @web-bench/react --to @web-bench/zustand --to @web-bench/table \
  && cd projects/calculator && npx playwright install chromium chromium-headless-shell )
export TOKENECONOMIZER_WEBBENCH_PROJECTS="$DATA/web-bench/projects"
export TOKENECONOMIZER_WEBBENCH_TASKS="$DATA/webbench_tasks.json"
python scripts/webbench_tasks_from_yaml.py --all    # zustand ships only tasks.yml
python scripts/export_webbench_tasks.py
```

**4. Keys and paths.** `cp .env.example .env`, then fill it in with absolute paths (the file
does not expand variables):

| Variable | Value |
|---|---|
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` | your keys |
| `COPILOT_PROVIDER_API_KEY` | your Anthropic key again: the Copilot CLI bills Anthropic through its own provider settings, already set in the file |
| `DATA` | the data directory |
| `TOKENECONOMIZER_RESULTS` | where results go, e.g. `$DATA/results` |
| `TOKENECONOMIZER_SWEBENCH_TASKS` | `$DATA/swebench/swebench_tasks.json` |
| `TE_SWEBENCH_REPO` | `$DATA/SWE-bench` |
| `TOKENECONOMIZER_WEBBENCH_PROJECTS`, `TOKENECONOMIZER_WEBBENCH_TASKS` | as exported in step 3 |

**5. One paid task.** Before a full run, check that one task produces a graded row:

```bash
AGENT=claude MODEL=claude-sonnet-5 DOMAIN=swebench ARMS=baseline REPOS=sympy/sympy SEEDS=0 \
  LIMIT=1 DETACH=0 RUNNER=te_harness.swebench:make_runner bash scripts/launch/main_sweep.sh
```

Its `row.json`, under `$TOKENECONOMIZER_RESULTS/claude_swebench_sonnet5/baseline/sympy_s0/`,
should show `num_turns` above zero, token counts, and a `resolved` value.

## Running the paper's experiments

`main_sweep.sh` runs Claude Code and the Copilot CLI, `codex_ab.sh` runs Codex. Each command
below is one lane, one agent and model on one benchmark: every arm it names, on every
repository or project, with seeds 0, 1 and 2. A lane runs in the background and prints where
its log is and how to stop it, so the commands in a loop run in parallel. Run a command again
to resume a lane: cells already finished are skipped. `DRYRUN=1` prints a lane's plan without
running it, and `DETACH=0` keeps it in the foreground.

The arms, by the names the paper uses:

| Arm | In the paper |
|---|---|
| `baseline`, `baseline-medium`, `baseline-low` | default execution at high, medium and low effort |
| `rtk`, `repomap` | the fixed strategies RTK and RepoMap |
| `trimech` (`codex-trimech` on Codex) | TokenEconomizer |
| `trimech-rtk`, `trimech-repomap` (`codex-trimech-…`) | TokenEconomizer+RTK, TokenEconomizer+RepoMap |
| `inject-only`, `untool-only`, `spiral-only` | micro-skills, tool pruning and the failure-loop guard alone |
| `trimech-web` | TokenEconomizer on Web-Bench, whose streams are short |

**SWE-Bench: Table 1, Figures 1(b) and 5, and the ablation in Table 2(a).**

```bash
SWE="baseline baseline-medium baseline-low rtk repomap trimech trimech-rtk trimech-repomap"
for MODEL in claude-sonnet-5 claude-opus-4-8; do
  AGENT=claude MODEL=$MODEL DOMAIN=swebench ARMS="$SWE inject-only untool-only spiral-only" \
    RUNNER=te_harness.swebench:make_runner bash scripts/launch/main_sweep.sh
done
AGENT=copilot MODEL=claude-sonnet-5 DOMAIN=swebench ARMS="$SWE" \
  RUNNER=te_harness.swebench:make_runner bash scripts/launch/main_sweep.sh
for MODEL in gpt-5.6-terra gpt-5.6-sol; do
  MODEL=$MODEL DOMAIN=swebench \
    ARMS="baseline baseline-medium baseline-low rtk repomap codex-trimech codex-trimech-rtk codex-trimech-repomap" \
    RUNNER=te_harness.swebench:make_runner bash scripts/launch/codex_ab.sh
done
```

**Web-Bench: Table 2(b).** The same agent–model settings as on SWE-Bench.

```bash
for MODEL in claude-sonnet-5 claude-opus-4-8; do
  AGENT=claude MODEL=$MODEL DOMAIN=webbench ARMS="baseline trimech-web" \
    RUNNER=te_harness.webbench:make_runner bash scripts/launch/main_sweep.sh
done
AGENT=copilot MODEL=claude-sonnet-5 DOMAIN=webbench ARMS="baseline trimech-web" \
  RUNNER=te_harness.webbench:make_runner bash scripts/launch/main_sweep.sh
for MODEL in gpt-5.6-terra gpt-5.6-sol; do
  MODEL=$MODEL DOMAIN=webbench ARMS="baseline codex-trimech" \
    RUNNER=te_harness.webbench:make_runner bash scripts/launch/codex_ab.sh
done
```

**Security benchmarks.** The CredData and ReposVul task sets (`workloads/creddata.json`,
`workloads/reposvul.json`) and their adapters are included, but this harness does not run or
grade them.

Every arm, the baseline included, runs behind an answer-key guard that blocks the benchmarks'
public answers on GitHub. The rtk binary is downloaded into `$DATA/scaffolds`.

## Results

Each lane writes one results tree, `$TOKENECONOMIZER_RESULTS/<agent>_<benchmark>_<model>/`
(for example `claude_swebench_sonnet5`), with one directory per arm and cell
(`<arm>/<repository>_s<seed>/`) and a run directory per task holding `row.json`,
`transcript.jsonl` and `run_config.json`. At the end of a lane, `aggregate.py` writes
`index/all_rows.csv`, one row per task run, and `index/summary_by_cell.csv`, one row per arm,
repository and seed; run `python scripts/aggregate.py <tree>` to refresh it. A task run again
keeps both runs, and the index takes the newest.

```bash
python scripts/compare_arms.py --baseline <tree>/baseline/<cell> --arm <tree>/<arm>/<cell>
```

`compare_arms.py` pairs one cell's tasks by instance and reports cost and resolved tasks
against the baseline. Cost is in cache-aware effective tokens and includes the arm's own model
calls. Web-Bench rows carry `resolved_p2`, the Pass@2 the paper reports. Never pool two trees:
the agent and the model are both part of the cost basis.

```bash
python scripts/scope_replay.py --cell <tree>/<arm>/<cell>      # or --plugin ~/.tokeneconomizer/repos/<key>/<agent>
```

`scope_replay.py` replays a cell's saved micro-skills state over its sessions, with no model
calls. For each fact it reports whether the firing gate reduces to the `scope` regex (no
`fired` of its own), what the scope selected and missed (sessions outside it where `confirm`,
`contradict` or `fired` matched anyway), and whether the fact's state or injection changes
when contradictions count regardless of scope. It follows the distiller's clock: each fact from
the tick its `admitted_at` records (reconstructed for a state written before that), and says
whether the replay reproduces the saved lifecycle, which checks that the sessions given are the
ones the distiller read.

## Troubleshooting

| Symptom | Cause |
|---|---|
| a launch prints `RUNNER=stub -- SYNTHETIC run` | `RUNNER` was not set; the stub runs no agent |
| `ModuleNotFoundError` at grading | the SWE-Bench grader is not installed in the venv the launcher uses (step 2) |
| every Web-Bench test fails and the app never renders | a non-canonical data path; `readlink -f` it (step 2) |
| `Executable doesn't exist at …/chromium_headless_shell-…` | Playwright was installed outside a project, so its browser build differs from the one the projects pin (step 3) |
| Codex gets 401 with a valid key | a key issued for a regional endpoint; set `OPENAI_BASE_URL` in `.env` |
| a lane needs stopping | `kill -TERM -$(cat <log>.pid)`, as printed at launch; the leading `-` stops the whole process group |
