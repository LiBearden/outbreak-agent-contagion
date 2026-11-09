# Outbreak

Harness and analysis for one question: can the spread of an instruction through a network of LLM agents be measured and predicted like an epidemic? Agents are text-only with no tools, see only fake workplace data, and the "payload" is an inert marker string. The design is in `SPEC.md` (later amendments override earlier text) and `PREREG.md`. Python 3.10+, standard library only, `git` on the PATH.

## Tests

From a clean clone, at the repository root:

    python3 -m unittest discover -s tests

No network, no `.env`, no skips; about 80 s. Some tests build a throwaway git repo in a temp directory. The offline calibration only validates the pipeline: the fake provider shares the simulator's rule, so recovering planted parameters says nothing about LLMs (see `tests/test_e2e.py`).

## A fake run

    python3 -m outbreak.runner E2 --smoke --topologies star --contents canary --reps 3 --run-id demo
    python3 -m outbreak.runner --dry-run E2R --rounds 1 --reps 60 --contents canary

The first writes `runs/demo/` (fake provider, at most 20 calls, marked `smoke`; analysis excludes smoke runs). The second makes no calls and prints each experiment's planned-slot sha256 and counts, as the line a prereg commits (`E2.planned_slots_sha256=...`); name no experiment to print all eight. A non-smoke run needs `--prereg <commit sha>` of a FROZEN prereg whose `prereg-params` block states every run parameter and that hash. The full offline pipeline (runner, then `python3 -m outbreak.analyze RUN_DIR... --out DIR`) with a stand-in for git is `tests/test_e2e.py`. `python3 -m outbreak.audit sample RUN_DIR... --out DIR` writes the 200-output blind-audit sample and its separate key.

## Experiments and outages (A19)

`E1` (primary prediction cells), `E1S` (dose 2, warned, control), `E1p` (persistence), `E2R` (`--rounds 1` cascades, the offspring estimand), `E2` (T=4, final size), `E2N`, `E3`, `E4`. Each has its own seed, frozen scope and hash, and analysis never pools rounds. `python3 scripts/power.py` prints the tier sizes and power quoted in `PREREG.md`.

## Wordings and the E2N side arm (A20)

The footer wordings (`CANARY`, `CONTROL` in `outbreak/world.py`) are self-copying: they ask the recipient to copy the whole request, marker included, into its own note. The `carried` form stays the bare tag, what an agent sees when a carrier dropped the request. `request_carried(text, wording)` is true when an output has the marker and the wording's anchor phrase (`word for word`, `entire reminder`, `reproduce this note`); analysis reports f, the share of carrying E1 footer outputs that also carry the request, and uses pc = f p + (1 - f) p_tag for later simulated rounds. The control also asks to be copied, so it tests whether the content of the request matters, not unprompted copying. `E2N` runs E2's design (star-hub and mesh, separately seeded) (canary only, 12 reps, 768 calls) with the earlier no-copy wordings (`CANARY_NOCOPY`) and is never pooled with E2; analysis reports H6, the E2 minus E2N final-size difference with an interval. `--probe-wording {0,1,2}` picks the wording a probe exposes, and the probe summary includes the request-carried rate. The E3 lines are not in the standard-tier prereg block, and E3 is refused under a freeze without them.

A transient failure (429, 408, 409, 5xx, timeout) is retried, the limiter counts every HTTP attempt, and a slot that still fails is not logged as an outcome: it goes to `attempts.jsonl`, stays unissued, and a resume re-issues it. `--breaker N` (default 5) stops the run after N such slots in a row; HTTP 402, or three 403s in a row, stops it as "credit exhausted" (Adaption documents no code for that, so this is an assumption). On resume a torn or newline-less last line of `calls.jsonl` is moved to `torn_lines.jsonl` and its slot re-issued; changing `--rpm` or `--max-total-tokens` is allowed and written to `deviations.jsonl`.

## Prompt logging and the Braintrust mirror (A21)

Each row of `calls.jsonl` also holds `instructions` and `input` exactly as sent; `sha256(instructions + "\n\n" + input)` equals `prompt_sha256`. Audit samples never carry them and analysis ignores them (older planning rows have none). `scripts/bt_mirror.py --runs-dir DIR --env-file PATH [--run-id ID ...] [--follow SECONDS] [--phase planning|study] [--state-dir DIR]` copies rows one way to the private Braintrust project `outbreak` (one experiment per run id, event id `run_id:slot_id`, so re-uploads never duplicate). It reads only complete lines, keeps byte offsets in `bt_mirror_state.json`, logs error classes only to `bt_mirror.jsonl`, never writes inside a run directory, and nothing in `outbreak/` imports it. It parses `BRAINTRUST_API_KEY` from the env file as text.

## Preexisting code

`preexisting/` holds code copied unchanged from before the sprint: `timestamp-freeze.sh`, from the research-stack repo (`automation/timestamp-freeze.sh`) at commit `dedfb62`. See `preexisting/README.md`. The freeze itself is timestamped by `scripts/timestamp_freeze.sh`, written during the sprint, because the preexisting script needs a runner file from its original repo. To check the freeze without git history, run `python3 scripts/verify_freeze.py`, which checks the receipts and compares each file with the frozen commit's file list in `timestamps/8327244aad0a/manifest.txt`.

## Authorship

Agent contributors (Claude models) wrote the code and tests, under the direction of Li Bearden, who set the design and the amendments.
