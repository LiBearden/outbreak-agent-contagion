# Audit of outbreak @ ad02ded (copy in src/, scripts in scripts/, synthetic runs in runs/)
Baseline: 43 tests pass (PYTHONPATH=.:tests python3 -m unittest tests.test_harness tests.test_analysis tests.test_e2e; plain discover fails on import of test_harness).

BLOCKER
B1 analyze.py:177 `complete = all(status ok)` over logged rows only; never compared with planned slots/rounds. Budget-stopped cascade (runs/e2budget: 6 of 32 calls, stop=token budget) analysed as complete: n_complete=1, final_size 6, n_agents=6 so major_threshold=3 not 4, errors=[]. planned_not_logged (analyze.py:86) computed, never reported. scripts/s3.py.
B2 guards.py:37-45 prereg-params only checks keys PRESENT in block. Block "experiment=E2" alone accepted for temperature=1.5 rounds=9 reps=1 (scripts/s4.py). Spec: block must match run params. A12-A13 style drift of design (rounds, temperature, model, reps, seed) passes the freeze.
SHOULD-FIX
S1 Circular calibration: llm._fake (llm.py:47-50) implements exactly simulate._carry (1-(1-q*was)(1-p)^k); test_e2e.py:100 asserts all 8 checks within_interval. Only the "crowd" departure test has power, and it asserts only 2 misses (mesh z1, star leaf z1).
S2 Intervals are wide: my run (15 cascades, E1 13/45, E1p 27/45): mesh z1 pred [0.53,1.93] live 0.87; star hub size [3.2,6.6]; mesh size [3.2,7.7]. 8 checks, no multiplicity handling; z1 and size share the same cascades. Live-mean-vs-predictive of same n is a valid posterior-predictive check (live noise is inside), but power is low at small n. Mutant M19 (drop p,q uncertainty) survives.
S3 Truncated rows are non-ok: e1_adoption (analyze.py:107) drops truncated rows that contain the marker; any truncation makes a cascade incomplete (selection against long, marker-appending outputs). A truncated row with marker present is a known adopt.
S4 Control not length-matched: canary 83/96/104 chars vs control 117/123/119 (wording 2: +41%). Spec says similar length. Position/channel are symmetric (both appended to the same clean note). Control seed agent and mesh graph differ from canary cascades (tid includes content), so not paired.
S5 Marker match counts any mention: a warned agent quoting "I will not add OB-x" is adopted (world.py:36). No refusal detector exists; status "refused" is never produced by llm.py, only injected in tests. Over-states adoption/under-states warned efficacy.
S6 Transfer: E1/E1p contaminated notes carry the full footer instruction (runner.py:187-189); in cascades gen>=2 carriers hold only the marker the agent wrote. Simulator uses p,q from footer-bearing notes for every generation. Declared in A6 as empirical; keep it labelled in the report.
S7 analyze accepts any run dirs and never checks manifests agree (model, temperature, seed, git_sha, provider fake vs real, prereg_sha).
S8 --prereg stored and used as an unresolved ref (manifest prereg_sha = argument string); a moving ref (HEAD, branch) passes whenever the tip is frozen.
S9 Mutations that survive (see below): rounds off-by-one; two-stage bootstrap graph level; p,q uncertainty.
NOTE
N1 Mesh CI uses 5 graph clusters (percentile bootstrap, narrow); p_major CI is Wilson ignoring graph clustering.
N2 Budget: failed attempts' usage is dropped (runner.py:151,180) and retries are not pre-charged, so overshoot of up to retries x est per call.
N3 Wording balance rep%3 and graph rep%5 only balanced for reps multiple of 15.
N4 Immunity threshold = first fraction where mean first-gen Z1<=1 (analyze.py:301); with random placement it is R0-scaled by construction. Labelled simulated.
N5 E1 always uses agent 0's role and agent 2's note; E1 shows one role/item only.
N6 marker is deterministic (sha256 seed:trial), not random; harmless but not "fresh random".
N7 Adaption provider raises NotImplementedError (llm.py:39): no real run is possible at this commit.
HOLDS
- Estimands: independent recompute over 120 fake E2 cascades (all topologies; scripts/s2.py, own regex, own parent derivation): final size, round-1 offspring, fractional attribution identical to build_cascade; log inbox_markers matched mine in all rows (0 mismatches). Hand case in tests (z1=3, frac 3.5, last 3.0) matches my arithmetic. Seed re-adoption not double counted (analyze.py:168).
- Exposure rule: all 3256 E2 rows' inbox_sources match star/mesh edge list in manifest and board last-4-others (0 violations, scripts/s6.py). Shared: seed invisible in 14/30 cascades round 1 (A12 expects 3/8, within noise).
- Leakage: system prompt, turn_prompt contain marker only via inbox items or own previous note; clean/control agents see no condition field. Own-prev path is the intended persistence.
- Simulator rules (carry, own-note persistence, early stop 2 quiet rounds, board order) equal runner; E3 seed never warned.
- Wilson and Newcombe formulas correct; two-stage bootstrap structure graphs then cascades correct.
- Guards: frozen set covers all logic modules (not __init__.py, tests; harmless); resume manifest compares sha, params minus budget/rpm/retries, smoke; duplicates refused; budget check precedes each call; smoke cap holds.
## Mutation results (22 mutants run in src copy, full tests)
Caught: z1 all rounds, attribution unsplit, last-read first, board oldest-4, prereg diff off, params off, FROZEN off, sim no persistence, sim all-neighbours, budget off, case-insensitive marker, control=canary text, early stop 1 round, seed warned, wilson z, complete=True, smoke included, resume re-issue, inbox_markers false, prev note withheld, dose-2 single, attribution swap, sim round1=all events.
SURVIVED: M1 runner rounds off-by-one (range(1,rounds) -> T=3 instead of 4); M13 bootstrap without graph resampling; M19 predictive without p,q uncertainty.
