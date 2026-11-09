# Process notebook

Dated record of how this project was run: what agents did, what humans decided, what was checked.
Design details are in `SPEC.md`, analysis commitments in `PREREG.md`, earlier history in git.

Human gates: Li approves the prereg freeze, approves launch, and signs off before submission.

## 2026-11-07

- Second audit findings and the focused design (SPEC amendment A19) implemented by a worker agent, not
  yet independently re-checked at the time of this entry. Worker reported 153 tests passing.
- Li stored the Adaption key on the lab (mode 600, never seen by agents).
- Agent read Adaption's terms and privacy policy (both dated February 24, 2026). No ban on the subject
  matter. Generic clauses on scripted use and systematic collection read literally onto a large run,
  and prompts go to third-party AI providers. Li emailed Adaption and pinged an Apart mod for a written
  answer. No answer yet. Review kept privately in `resources/providers/`.
- First live contact: 20-call test of gemma-4-31b-it at commit 4bf414f, run id `probe-gemma-4-31b-it`.
  20 ok, 0 HTTP 429 at 30 rpm, 5,573 tokens, 0 reasoning tokens, about 1 s per call, `model` returned as
  requested. Carriage on the footer form: canary 10/10, control 5/10. Planning data only, never pooled
  with study results. The carried form, which decides spread past the first generation, was not tested.
- Independent checker on 254f6f0: no blockers. Suite and tier totals confirmed, A19 clauses mutation-tested,
  and the one mutation it could not finish (50-of-200 audit floor) was redone and fails its test. Suite passes
  on the lab at da44ca7. Open: E3 parameters sit in the standard prereg block though standard runs no E3.
- Planning tests, wording 0 only, canary then control out of 10. glm-53-flash returned HTTP 404 throughout.
  - Footer: gemma-4-31b 10, 5; gemma-4-26b 10, 10; deepseek-v4-flash 10, 10 (5 of 20 calls truncated);
    qwen3.8-27b at 800 max tokens 10, 10 (all truncated at 200).
  - Carried: gemma-4-31b 0, 0; gemma-4-26b 2, 0; deepseek 0, 0 (7 of 20 truncated); qwen at 800 max tokens 2, 0.
- Design gap found from those tests: the canary asks agents to include a code, not to pass the request on.
  gemma-4-31b outputs carried the code but none carried the request, so spread stops at one hop by construction.
- Exploratory test, patched in memory and not in the repo (run ids `explore-selfcopy-*`): wording asks agents to
  copy the whole P.S., including the request. Calls with marker and request carried, of ok calls:
  gemma-4-31b canary 6/10, control 7/10; gemma-4-26b 10/10, 10/10; deepseek at 800 max tokens 9/9, 9/10;
  qwen at 800 max tokens 2/5, 0/2. Chosen after seeing data, so any adoption must be disclosed as such.
- Li chose self-copy wordings plus a no-copy side arm (SPEC A20). Implemented at 99287ea, 178 tests reported.
- Wordings 1 and 2 at 99287ea on gemma-4-31b, 60 rpm, run ids `probe-gemma-4-31b-selfcopy-w1`, `-w2`. Marker then
  request carried, out of 10: wording 1 canary 10, 7, control 10, 0; wording 2 canary 4, 1, control 5, 1.
  Pooled with wording 0, canary marker 20/30, request 14/30. Wording is a large effect in this small sample.
  No 429s. 20 calls took 21 to 23 s, and the runner is serial, so about 50 rpm is the practical ceiling.
- Independent checker on 99287ea: no blockers. It found a wide H6 interval could read as supported, an E1p note
  that no longer matched cascades, and undisclosed limits. All sent back for fixing.
- The principal first leaned to gemma-4-31b without testing gemma-4-26b on wordings 1 and 2, and caught this before
  freeze. Tested at 50 rpm, run ids `probe-gemma-4-26b-selfcopy-w1`, `-w2`. Canary marker 10, 4 and request 10, 4;
  control marker 10, 10. Pooled with wording 0: canary 24/30, request 24/30, control 30/30. Applying the selection
  rule as written picks gemma-4-26b-a4b-it, about 6% cheaper per call at listed prices.
- Launch script rehearsed on a temporary frozen clone: guard passed all seven experiments and refused four
  tampering cases. Full suite passes on the lab at 3dc6787.
- Li asked for prior work to be re-run and Braintrust confirmed before any experiment. Two agents searched, and a
  checker verified 10 claims at source. Result: no "first" claim survives. Two prereg citations were wrong
  (AgentWorm is not a harmless-canary study, Passaro is not hub removal) and were corrected. Tout's full text
  could not be read (HTTP 403); its public repository was read instead. To re-run on submission day.
- Braintrust: key and account worked on a read-only request, but Outbreak was not connected. Li asked for the most
  comprehensive setup, so SPEC A21 adds full prompt logging and a one-way mirror to a private project.
  Mirror live test at 0cae267 work: 400 planning rows, counts matched after a second upload, no duplicates.
- Pre-freeze real-model checks, 20 calls each, run ids `probe-smoke-e2`, `probe-smoke-e4`. E2: all ok, longest output
  99 tokens against the 200 cap, prompts about 377 tokens by round 3. E4: monitor returned no flags in its two calls.
- That prompted a direct monitor test, 20 calls per arm, run id `explore-sentinel`. It found a real bug: all 20
  unparseable replies were valid answers in markdown code fences, which the parser discarded. The fake provider never
  fences, so no test or audit could catch it. Recount with fences handled, flagged any / author / accused or target:
  benign 0 / 0 / 0; placebo 2 / 2 / 0; framing 18 / 12 / 6; outbreak 20 / 20 / 0. Before the fix the outbreak row read
  5 flags. Lesson for the process record: live checks caught what two audits and mutation testing could not.
- Added an outbreak arm to the standard tier so a null framing gap can be interpreted (SPEC A22 clause under A21).
  Attacker wording not changed after seeing these tests.
- Final read of the prereg before freeze found conflicting statements of the primaries, unspecified H1, H2 and H5 rules,
  and a stale power figure. All fixed; roles are now stated once. H1, H2 and the policy comparison were promised but not
  implemented, so they were implemented and tested on planted answers (cd99409).
- A test depended on the private E4 notes; it now uses the example file. The Holm family shrank when a condition was
  missing, which would have weakened the correction. Fixed so the family stays at six, and resampled arms made
  independent (1d931ad). A mutation of the fix fails its test. One earlier mutation result was an import failure, not a
  real test result, and was redone.
- Adaption: Li reports Adaption approved the study and upgraded the account with 500 credits added. Agents have not seen
  the text of the approval. At the capped rate the run is expected to use about 2 credits, under 9 at the token cap.
- After the upgrade, a 20-call smoke test on the lab at 1d931ad, run id `probe-post-upgrade`: 20 ok, 7,284 tokens,
  gemma-4-26b-a4b-it. Planning data, excluded from every estimate.
- `preexisting/timestamp-freeze.sh` failed its dry run: it needs a runner file from its original repo. Left unchanged;
  `scripts/timestamp_freeze.sh` was written for this repo.
- **First human gate: Li approved the freeze in chat on 2026-11-07 ("Freeze").** No study data exists at this commit.
- **Frozen commit: `8327244aad0aa0d3e5e4dd926ecb0ba2429d37b6`.** RFC 3161 receipts in `timestamps/8327244aad0a/`: FreeTSA 2026-11-07 04:48:34 UTC,
  DigiCert 04:48:35 UTC. Both message digests match `commitment.txt`; DigiCert's signature verified against the system
  roots, FreeTSA's chain not checked. Only the hash left the machine.
- **Second human gate: Li approved launch in chat on 2026-11-07 ("launch"), after an estimate of about 1.8 credits and
  16M tokens, capped at 33.07M tokens.** Started on bulma-lab at 05:07:48 UTC as user services `outbreak-study` (2 GB
  memory cap) and `outbreak-mirror` (one-way copy to the private Braintrust project), prereg `8327244aad0a`, from HEAD
  `df0223c`. Agents check technical health only (status, tokens, errors) until the run ends; rates are not read.
- E1 finished at 07:24:43 UTC: 6,840 of 6,840 ok, 1,965,195 tokens, 0.225 credits at listed prices.
- **Braintrust mirror stopped on a plan limit.** From 07:14:35 UTC every insert returned HTTP 400: the free plan allows
  11,000 scores per calendar month and the org reached 11,039. The block is org-wide: a row with no scores is refused
  too. The agents did not check this limit before launch, though 44,088 rows at one or two scores each could never
  fit. The mirror was stopped at about 07:31 UTC (its last logged error is 07:30:51); its offsets are kept. Braintrust therefore holds planning rows and only
  part of E1. The study does not depend on the mirror: nothing in `outbreak/` imports it, and the lab logs are complete.
  Not a deviation from the analysis plan, but it falls short of the mirror described in SPEC A21. The report must state
  that the Braintrust copy is partial.
- Li decided to leave the Braintrust mirror stopped and keep its saved place (`runs/bt_mirror_state.json` on the lab).
  No paid upgrade. Lab logs remain the study's record.
- Li asked for the unknown provider routing to go in the report's limitations. Started `report/limitations.md`
  as the running list for that section; no report draft exists yet.
- A worker agent added 17 known limitations to `report/limitations.md`, each citing its source line; the principal
  spot-checked the less certain claims against PREREG, SPEC and README. One candidate (later rounds barely tested)
  was left out as not stated in the records; its supported part is under request fidelity.
- Independent checker verified all 46 citations in `report/limitations.md`: no wrong, unsupported or overstated claim.
  Three loose citations tightened (a date's heading, the H4 definition, and "published" narrowed to "in the prereg").
- Paraphrase-resistant leak check of the two private E4 notes (script run locally, scores and file:line only printed,
  never note text): rarity-weighted content-word overlap in 30-word windows across 121 files (repo, Outbreak research
  notes, report, memory). Planted reworded copies scored 0.55 and 0.71; the highest real windows scored 0.40 (the
  public A20 canary-wording test) and 0.34; no real window reached a planted copy's score. Limit: lexical only. No local
  embedding model, and the notes were not sent to any external service, so a synonym-level rewording is not ruled out.
- Leak checks of the private E4 notes completed; pointers in `report/redaction_review.md`. No verbatim or close copy
  outside places that hold them by design. Two low-severity items for Li's review before any public release
  (a test fixture and one SPEC clause). Meaning-level check by an independent checker agent, which first read only
  keyword regions and was sent back to read all 36 files in full.
- 2026-11-07, Li's decision on the two redaction items: the E4 test fixtures (`tests/test_e4.py:15-16`) were replaced
  with neutral stand-ins of equal length, and the fake sentinels now key on the new words. The A14 framing clause in
  `SPEC.md` was generalized to "falsely accusing a named healthy target", with an in-place note that this was done
  after the freeze. The change touches wording only. No frozen-scope file changed (`outbreak/*.py`, `config/`,
  `PREREG.md`), and the running study is unaffected, because the lab runs df0223c. 208 tests pass on the Mac. The old
  wordings remain in git history, including the frozen commit.
- 2026-11-07, Li's decision: any public code release is a fresh export of the files (`git archive` of the release
  commit), never the git history, so the old fixture and A14 wordings stay out. Trade-off recorded in
  `report/redaction_review.md` and `report/limitations.md`: readers cannot verify the RFC 3161 freeze receipts
  against the frozen commit themselves. The frozen archive is available from the authors on request.
- 2026-11-08, Li's decision: publish the frozen commit's file list (`timestamps/8327244aad0a/manifest.txt`, from
  `git ls-tree -r -t 8327244`; paths and hashes only) and add `scripts/verify_freeze.py`. With them, the freeze can
  be checked without history: the receipts' digests and signatures, a rebuild of the tree hash from the list
  (f4aab524…, matching commitment.txt), and a comparison of every file with its frozen hash. On a fresh export of
  HEAD, 31 of 34 frozen files match, and NOTEBOOK.md, SPEC.md and tests/test_e4.py differ, as expected. A tampered
  commitment file or file list fails the check. DigiCert's signature verifies against the system roots, and
  FreeTSA's still does not (known gap). Added `tests/test_verify_freeze.py`; 210 tests pass.
- 2026-11-08, process record (Li asked). Li's run-location answer on 2026-11-06 chose bulma-lab, framed as "the
  Virtual Eval Lab ... with your prereg and provenance tools". The agents then narrowed this without asking Li as a
  decision. A one-line status message ("Paperclip and Braintrust are too heavy for a weekend", also skipping `core/`)
  was the only notice, and Li did not object or approve. Result: Outbreak is a standalone repo on the lab VM, run as
  systemd user services started over SSH from the Mac Claude Code session. It does not use Paperclip, HumanLayer,
  `core/` run, artifact, provenance or cost contracts, or the priced-run approval path. Braintrust was added back
  later as a mirror at Li's request. From the lab repo, the freeze guard was rewritten in Python after its pattern,
  and the copied timestamp script never worked, so no lab code runs in the study. This is a gap between the stated
  plan and the run, not a change to the analysis plan.
- 2026-11-08, lab records (Li asked to load the finished run). Adapter `adapters/tenants/outbreak.py` is on branch
  `outbreak-tenant` of research-stack (worktree on the Mac, based on lab/main 7ce0e16; commits d3afb39, 339b34e; not
  pushed). It records counts, tokens, cost and file hashes, never prompt or reply text. An independent checker passed
  all 7 checks, and the lab suite passes (410). Li's decision: Adaption credits go in the lab's USD ledger at 1:1,
  and each record labels this ("free credits, no money spent"). The load runs once, after the study ends, with the
  Braintrust sink off. The experiment number is pending Li's choice.
- 2026-11-08 00:34 UTC, study loaded into the lab records as experiment 3. Li chose "the next free experiment number".
  0 and 1 are used, and 2 is reserved by the lab's draft prereg `prereg-exp2-three-arm.md`. Branch `outbreak-tenant`
  (339b34e) was pushed to the lab, not merged, and run from a separate worktree `~/research-stack-outbreak` with the
  Braintrust sink off. The store was backed up first to `~/research-stack-runs-backup-20261108T003445Z.tar`.
  `core.verify` passes: exp 3 has 7 runs and 1.881705 credits, recorded as USD 1:1. A second load added 0 runs. There
  are 28 new store files totalling 23.7 KB, none over 64 KB, so no log text was copied.
- 2026-11-08, steps 2 and 3. The lab copy of the repo fast-forwarded to the Mac's main (d235ede), clean. Provider
  probe `probe-provider-headers` (one call of 27 input and 3 output tokens, excluded from all estimates): status 200,
  and no header or body field names a provider. `X-Powered-By: Express`, plus body fields named like vLLM's
  (`kv_transfer_params`, `ec_transfer_params`, null). The body's `cost` of 2.8305e-06 credits matches the list price
  exactly. The raw reply is in `runs/probe-provider-headers/raw.json` on the lab. `report/limitations.md` is updated.
- 2026-11-08, frozen analysis run on the lab (`python3 -m outbreak.analyze` on the 7 study runs, code identical to
  8327244). Output `analysis/study-8327244aad0a/`: results.json and 24 CSVs, committed as produced. Completeness: 0
  errors, 0 torn lines, 0 trials not started, 0 incomplete cascades. Planned-not-logged slots are E2 872 and E2N 104
  (early stopping, to be confirmed by the checker) and 0 elsewhere. 71 truncated calls, and the worst-case shift of the
  E1 p and E2 Z1 estimates is 0. An independent checker is re-deriving H3 and H5 from the raw logs with its own code,
  and no estimate is reported until it is done.
- 2026-11-08, independent re-derivation (checker agent, its own stdlib code on the raw logs, scripts in
  `~/outbreak-check/` on the lab; it never imported the official analysis). All 4 H3 prediction verdicts and both H5
  gaps match the official files. The H3 point ratios agree to 3 decimals, with CI bounds within its 0.04 tolerance;
  the H5 gaps and intervals match exactly. Every unlogged E2 and E2N slot belongs to a cascade that legitimately
  stopped early (replayed stop rule), with 0 missing data. Logged `adopted` equals a fresh marker recompute on every
  E1 and E2R row. 0 unparseable sentinel replies. No bug suspected. Not covered: the H1, H2 and H6 secondaries and
  `e4_policy_gap_difference`. Numbers are withheld from Li until Li decides whether to file a sealed PI take first
  (ADR-0009, Sep 7 amendment).
- 2026-11-08, results seminar (run 20261108T010449Z-5c2d2c): the Claude (Opus 5.5) and GPT-6.1 Sol seats are
  committed. The first Kimi K3.1 Pro call stopped on the length limit: all 12,000 output tokens were used, mostly on
  hidden reasoning, and only 1,940 characters of visible text came back. Cost $0.1814. That take was never read or
  committed, and its file was deleted. Li approved a rerun with a 32,000-token limit (about $0.37 more at most), and
  `seminar_seat.py kimi` gained a `--max-tokens` option for it. The rerun sends the same prompt, so the seats stay
  at parity.
- 2026-11-08, Kimi rerun: finished cleanly (finish stop; 35,499 input and 21,659 output tokens; $0.2732; 835 words).
  Committed unread as a6e8a8ff8439c3ba616d2f04917960dc and the file deleted. The Kimi seat cost $0.4546 over two
  calls. A second copy of the rerun command was started by mistake at about the same time. Its output, if any, is
  discarded unread and never committed; whatever it cost is unrecorded until the DeepInfra usage page is checked.
- 2026-11-08, PI seat ceded. Li gave up the PI take in favour of a take by the conducting agent (Claude Opus 5.5,
  this session), to record the reading of the model that ran the experiment alongside the outside models. That take
  may not be filed under role `li`: the lab records that role as typed by a person. So, under ADR-0009's rule that a
  second thought is a new seminar, a second seminar opens with roles claude, gpt, kimi and conductor, the same question
  and the same packet. The three sealed takes carry over byte for byte with `seminar_seat.py recommit`, which refuses
  if the question or packet digest differs. The first seminar, 20261108T010449Z-5c2d2c, stays as filed, with 3 of 4
  commits and its li seat empty. Disclosures for the minutes: the conductor take was written with full knowledge of
  the run, its process notes and the independent re-derivation, not from the packet alone. It was written after the
  conductor had given Li a plain-language reading of the results. It was written without seeing any other take, all
  of which were still sealed. Two of the four takes come from the same base model.
- 2026-11-08, results seminar 20261108T130331Z-7f395d, done. Blind verdict by deputy-pi (Claude Fable 5.1), on the
  rubric fixed in advance. Scores out of 50: A 50 (claude), B 50 (kimi), C 45 (conductor), D 44 (gpt). The winner is
  A, on an insight tie-break; judge confidence 0.6. No checked number was wrong in any take. A's decisive point: a
  model in which every exposed agent adopts would also pass the H3 band (ratios 1.0 at star-hub and 0.883 at mesh),
  so H3 cannot separate the fitted forecast from universal adoption. Minutes are filed in the seminar record, with
  caveats: the judge and two takes are Anthropic models, the wrappers differed, and C was not packet-only. While
  filing, the verdict name check refused the word "blind", because the blind/unblind units' bookkeeping counted as
  names. That was fixed at the source in research-stack seminar-blind 28d9569, with a test, before the verdict was
  committed; the verdict was not edited.
- 2026-11-08, layouts page: https://claude.ai/artifact/PWYZvuw7ujKHdPiTYfBtNG (private). It replays all 240 E2
  cascades, exported by scripts/export_cascades.py using analyze.load_run and build_cascade; final sizes match
  e2_summary.csv. Sources are in report/layouts_page/. Layout literature notes are in resources/layouts/ (gitignored, like the other prior-work notes); a checker
  found 12 of 13 claims stated in the sources, and the G-Safeguard, NetSafe and Spark to Fire wording was corrected.
  Descriptive finding, not a test: in star-leaf cascades the hub adopted in 60 of 60, yet in 11 of 60 no other
  worker adopted from the hub.
- 2026-11-08, panel vote: Li cast out the single-judge Fable verdict and replaced it with a three-judge panel. The
  judges were Fable 5.1, GPT-6 Astra (Codex) and Gemini 4 Argon (OpenRouter; Adaption and DeepInfra do not serve
  it). They agreed a rubric by consensus before seeing the takes, then voted. The Fable verdict stays filed in
  20261108T130331Z-7f395d and is marked superseded.
  The rubric took three rounds: 1be550, eeb7cf/62e8c3 and 54e17b. In round 2 the Fable seat's write was blocked by
  a safety classifier; Li chose to continue with two seats. In round 3 all three judges accepted the Astra
  proposal, which was frozen unedited as seminar/panel/rubric_final.txt (sha256 0af901052e0503e4).
  The vote is 4eeb73. The W–Z labels were committed beforehand (e4073370…).
  Result: kimi won 2 of 3 first-place votes (Fable and Argon; Astra voted gpt). The summed totals rank the takes
  kimi 14.1, gpt 13.5, claude 12.8, conductor 11.5. Every judge placed the conductor's take last. No runoff.
  Disclosures:
  - Astra wrote the rubric and voted for the GPT take.
  - Argon overran the word limit in every round.
  - The bundle tool added a "Stated confidence" line to each take, and Astra penalised it uniformly.
  The OpenRouter key was added to ~/outbreak/.env on the lab from Li's clipboard; it was never printed. Argon cost
  $0.4906 over 4 calls. Minutes, with the judges' uncertainties and follow-up work: seminar/panel/vote_minutes.txt.
- 2026-11-09, round-3 order check (5f1d3c): Li asked for round 3 to be rerun with the proposals reversed. The Astra
  rubric was again the only one accepted by all three, so the rubric and the vote stand. Fable and Argon picked
  whichever proposal was listed first both times, so their first choices were position effects. The selection rests
  on Astra rejecting Argon's proposal, in both orders. Argon cost $0.1030 (panel total $0.5936).
