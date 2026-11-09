"""CLI: run E1, E1S, E1p, E2R, E2, E2N, E3 or E4 from parameters (or --probe MODEL for a 20-call smoke check).
--dry-run prints each experiment's planned-slot hash and counts, with no provider calls: the value a
prereg commits as `E2.planned_slots_sha256=...`."""
import argparse
import hashlib
import json
import os
import platform
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from . import guards, llm, world

ROOT = Path(__file__).resolve().parent.parent
EXPERIMENTS = ["E1", "E1S", "E1p", "E2R", "E2", "E2N", "E3", "E4"]  # A20: E2N = no-copy side arm of E2
OPERATIONAL = ("rpm", "max_total_tokens")  # A19: may change on resume (logged as a deviation), not part of the design
CREDIT_STREAK = 3  # consecutive HTTP 403 slots read as exhausted credit; one 402 is enough


def make_limiter(rpm, now=time.monotonic, sleep=time.sleep):
    gap, last = 60.0 / rpm, [None]

    def wait():
        if last[0] is not None and (d := last[0] + gap - now()) > 0:
            sleep(d)
        last[0] = now()
    return wait


def parse_args(argv=None):
    p = argparse.ArgumentParser(prog="outbreak.runner")
    p.add_argument("experiment", nargs="?", choices=EXPERIMENTS)
    p.add_argument("--dry-run", action="store_true", help="print planned-slot sha256 and counts (all experiments if none named)")
    p.add_argument("--probe", metavar="MODEL", help="explicit only: <=20 smoke calls of an E1 turn against MODEL")
    p.add_argument("--probe-form", default="footer", choices=["footer", "carried"], help="E1 form the probe exposes")
    p.add_argument("--probe-wording", type=int, default=0, choices=[0, 1, 2], help="E1 wording the probe exposes (A20)")
    p.add_argument("--provider", default="fake", choices=["fake", "adaption"])
    p.add_argument("--model", default="fake-model")
    p.add_argument("--reps", type=int, default=3)
    p.add_argument("--rounds", type=int, default=4)
    p.add_argument("--max-tokens", type=int, default=200)
    p.add_argument("--temperature", type=float, default=0.7)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--topologies", default="star,mesh,shared")
    p.add_argument("--contents", default="canary,control")
    p.add_argument("--warned-counts", default="0,2,4,6")
    p.add_argument("--rpm", type=float, default=30)
    p.add_argument("--max-total-tokens", type=int, default=200000)
    p.add_argument("--retries", type=int, default=2)
    p.add_argument("--breaker", type=int, default=5, help="stop after this many consecutive retryable failures")
    p.add_argument("--policies", default="off,single,corroborated")  # E4; "off" = sentinel runs, nothing enforced
    p.add_argument("--scenarios", default="benign,outbreak,framing,placebo")
    p.add_argument("--e4-notes", default=str(ROOT / "config" / "e4_notes.json"))
    p.add_argument("--fake-sentinel", default="0.5,0.02,0.3,0", help="tp,fp,named,junk for the fake sentinel")
    p.add_argument("--fake-p", type=float, default=0.5)
    p.add_argument("--fake-persist", type=float, default=1.0)
    p.add_argument("--smoke", action="store_true")
    p.add_argument("--prereg")
    p.add_argument("--prereg-file", default="PREREG.md")
    p.add_argument("--run-id")
    p.add_argument("--runs-dir", default=str(ROOT / "runs"))
    p.add_argument("--env-file", default=str(ROOT / ".env"))
    return p.parse_args(argv)


def tokens_of(u):
    """Budget cost of one call; absent usage fields are null in the log, so count what is present."""
    return u["total"] if u["total"] is not None else (u["input"] or 0) + (u["output"] or 0)


def build_params(a):
    """Everything that changes results; this is what a prereg-params block is checked against."""
    keys = ["experiment", "provider", "model", "reps", "rounds", "max_tokens", "temperature",
            "seed", "topologies", "contents", "warned_counts", "rpm", "max_total_tokens", "retries"]
    params = {k: getattr(a, k) for k in keys}
    if a.experiment == "E4":
        params.update(policies=a.policies, scenarios=a.scenarios,
                      e4_notes_sha256=world.load_e4_notes(a.e4_notes)[1])
    return params


def git_state():
    rc, sha = guards.run_git(ROOT, "rev-parse", "HEAD")
    if rc:
        return None, None
    return sha.strip(), bool(guards.run_git(ROOT, "status", "--porcelain")[1].strip())


def append(path, row):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")
        f.flush()
        os.fsync(f.fileno())


def recover(path, torn):
    """A19: a last line without its newline is unconfirmed (a torn write, or a crash before the newline).
    Move it to `torn` so its slot is re-issued and the next row never merges into it."""
    if not path.exists() or not (raw := path.read_bytes()) or raw.endswith(b"\n"):
        return
    cut = raw.rfind(b"\n") + 1
    with open(torn, "ab") as f:
        f.write(raw[cut:] + b"\n")
        f.flush()
        os.fsync(f.fileno())
    with open(path, "r+b") as f:
        f.truncate(cut)
        os.fsync(f.fileno())


def load_log(path, planned, attempts=None):
    rows, unplanned, seen, tokens = {}, [], set(), 0
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            if r["slot_id"] in seen:
                raise guards.GuardError(f"duplicate slot in log: {r['slot_id']}")
            seen.add(r["slot_id"])
            tokens += tokens_of(r["usage"]) + r.get("failed_attempt_tokens", 0)
            if r["slot_id"] in planned:
                rows[r["slot_id"]] = r
            else:
                unplanned.append(r["slot_id"])
    if attempts and attempts.exists():  # unissued slots: what the provider reported still counts against the budget
        tokens += sum(json.loads(x)["tokens"] for x in attempts.read_text(encoding="utf-8").splitlines() if x.strip())
    return rows, unplanned, len(seen), tokens


def plan(a):
    """The design a prereg binds: (trials, planned slot ids, planned_slots.txt text, its sha256)."""
    if a.experiment == "E2R" and a.rounds != 1:
        raise ValueError("E2R is the round-1 design: pass --rounds 1")
    p = {"reps": a.reps, "seed": a.seed, "topologies": a.topologies.split(","), "smoke": a.smoke,
         "contents": a.contents.split(","), "warned_counts": [int(x) for x in a.warned_counts.split(",")],
         "policies": a.policies.split(","), "scenarios": a.scenarios.split(",")}
    trials = world.make_trials(a.experiment, p)
    if a.probe:  # 4 cells (content x channel) x 5 reps = the 20-call smoke cap
        trials = [t for t in trials if (t["condition"]["dose"], t["condition"]["immunity"],
                                        t["condition"]["wording"], t["condition"]["form"]) == (1, "none", a.probe_wording, a.probe_form)]
    planned = sorted(s for t in trials for s in world.slot_ids(t, a.rounds))
    txt = "\n".join(planned) + "\n"
    return trials, planned, txt, hashlib.sha256(txt.encode()).hexdigest()


def execute(a, call=llm.call_model, now=time.monotonic, sleep=time.sleep, git=guards.run_git):
    params = build_params(a)
    e4_texts = None
    if a.experiment == "E4":
        e4_texts = world.load_e4_notes(a.e4_notes)[0]
    env = {} if a.provider == "fake" else llm.load_env(a.env_file)
    llm.preflight(a.provider, env)
    trials, planned, planned_txt, sha = plan(a)
    run_id = a.run_id or f"{a.experiment}-{sha[:8]}"
    run_dir = Path(a.runs_dir) / run_id
    mpath = run_dir / "manifest.json"
    old = json.loads(mpath.read_text()) if mpath.exists() else None
    # A19: a resume may change rpm and the budget; the prereg still has to state them, and the change is logged below
    prereg_sha = None if a.smoke else guards.check_prereg(a.prereg, params, sha, a.prereg_file, ROOT, git,
                                                          OPERATIONAL if old else ())
    run_dir.mkdir(parents=True, exist_ok=True)

    graphs = {}  # A5: edge lists in the manifest, so analysis never infers a graph from logs
    for t in trials:
        if t["graph_id"] and t["graph_id"] not in graphs:
            nb = world.graph_of(t["topology"], a.seed, t["graph_id"])
            graphs[t["graph_id"]] = world.edge_list(nb) if nb else {"board_read_latest": world.BOARD_READ, "n": world.N}
    gsha, dirty = git_state()
    fake = {"p": a.fake_p, "persist": a.fake_persist, "sentinel": a.fake_sentinel} if a.provider == "fake" else None
    manifest = {"run_id": run_id, "experiment": a.experiment, "git_sha": gsha, "git_dirty": dirty,
                "prereg_sha": prereg_sha, "params": params, "planned_slots_sha256": sha,
                "rng_seed": a.seed, "graphs": graphs, "python_version": platform.python_version(),
                "created_ts": datetime.now(timezone.utc).isoformat(), "smoke": a.smoke, "fake": fake}
    if a.experiment == "E4":
        manifest["e4"] = {t["id"]: {k: t[k] for k in ("policy", "scenario", "attacker", "target", "seed_agent")}
                          for t in trials}
    if old:
        # budget, rate and retries may change on resume; the design (and the fake's behaviour) may not
        design = lambda q: {k: v for k, v in q.items() if k not in OPERATIONAL + ("retries",)}
        if (old["planned_slots_sha256"], design(old["params"]), old["smoke"], old.get("fake")) != (
                sha, design(params), a.smoke, fake):
            raise guards.GuardError(f"run {run_id} exists with a different design; use another --run-id")
        dev = run_dir / "deviations.jsonl"
        was = {k: old["params"].get(k) for k in OPERATIONAL}  # the manifest keeps the start values; the log has the rest
        for x in dev.read_text().splitlines() if dev.exists() else []:
            was[json.loads(x)["key"]] = json.loads(x)["now"]
        for k in OPERATIONAL:
            if was[k] != params[k]:
                append(dev, {"ts": datetime.now(timezone.utc).isoformat(), "run_id": run_id,
                             "key": k, "was": was[k], "now": params[k]})
        if not (run_dir / "planned_slots.txt").exists():  # a crash between manifest and plan file
            (run_dir / "planned_slots.txt").write_text(planned_txt)
    else:
        mpath.write_text(json.dumps(manifest, indent=1))
        (run_dir / "planned_slots.txt").write_text(planned_txt)

    log = run_dir / "calls.jsonl"
    recover(log, run_dir / "torn_lines.jsonl")
    rows, unplanned, n_logged, tokens = load_log(log, set(planned), run_dir / "attempts.jsonl")
    wait = make_limiter(a.rpm, now, sleep)
    st = {"stop": None, "issued": 0, "logged": n_logged, "tokens": tokens, "fails": 0, "credit": 0}

    def issue(t, rnd, agent, prev, items, board=None, extra=None, post=None):
        sent = agent == "sentinel"
        sid = f"{t['id']}/r{rnd}/" + ("sentinel" if sent else f"a{agent}")
        if sid in rows:
            return rows[sid]  # resume: reuse the logged note, never re-issue
        cond = t["condition"]
        channel = cond["channel"]
        warned = agent in t["warned"]
        system = world.SENTINEL_SYSTEM if sent else world.system_prompt(agent, warned)
        prompt = world.sentinel_prompt(items) if sent else world.turn_prompt(prev, items, channel)
        est = len(system + prompt) // 4 + a.max_tokens
        if a.smoke and guards.smoke_remaining(st["logged"]) == 0:
            st["stop"] = f"smoke cap ({guards.SMOKE_CAP} calls)"
        elif st["tokens"] + est > a.max_total_tokens:
            st["stop"] = "token budget"
        if st["stop"]:
            return None
        attempt = failed = 0  # failed = usage the provider reported on attempts that were retried
        for tries in range(1, a.retries + 2):
            try:
                res = call(a.provider, a.model, system, prompt, max_tokens=a.max_tokens,
                           temperature=a.temperature, seed_key=f"{a.seed}:{sid}",
                           fake_p=a.fake_p, fake_persist=a.fake_persist, env=env,
                           **({"fake_s": tuple(map(float, a.fake_sentinel.split(",")))} if a.experiment == "E4" else {}),
                           retries=a.retries, sleep=sleep, pace=wait)
            except Exception as e:  # class name only, never the message
                res = {"text": "", "usage": dict.fromkeys(("input", "output", "total", "cached", "reasoning")),
                       "model_returned": None, "status": "error",
                       "error_class": type(e).__name__, "latency_ms": 0}
            attempt += res.get("attempts", 1)
            # a provider that reports `attempts` has already retried inside the call
            if res["status"] != "error" or tries > a.retries or "attempts" in res:
                break
            failed += tokens_of(res["usage"])
            sleep(2 ** (tries - 1))
        code = res["error_class"]
        if res["status"] == "error" and (res.get("retryable") or code in ("HTTP402", "HTTP403")):
            # A19: an outage or exhausted credit is not an outcome. No row, so resume retries the slot.
            append(run_dir / "attempts.jsonl", {"slot_id": sid, "attempts": attempt, "error_class": code,
                                                "tokens": failed + tokens_of(res["usage"]),
                                                "ts": datetime.now(timezone.utc).isoformat()})
            st["tokens"] += failed + tokens_of(res["usage"])
            credit = code in ("HTTP402", "HTTP403")
            st["credit"], st["fails"] = st["credit"] + 1 if credit else 0, 0 if credit else st["fails"] + 1
            if credit and (code == "HTTP402" or st["credit"] >= CREDIT_STREAK):
                st["stop"] = f"credit exhausted ({code})"  # the docs name no code for it: 402/403 is our reading
            elif st["fails"] >= a.breaker:
                st["stop"] = f"circuit breaker ({a.breaker} consecutive retryable failures)"
            return None
        st["fails"] = st["credit"] = 0
        row = {
            "slot_id": sid, "run_id": run_id, "experiment": a.experiment, "trial_id": t["id"],
            "round": rnd, "agent": agent,
            "graph_id": t["graph_id"], "seed_agent": t["seed_agent"], "board_order": board,
            "condition": {**cond, "immunity": "warned" if warned else "none"},
            "topology": t["topology"], "seed_position": t["seed_position"],
            "inbox_sources": [s for s, _ in items],
            "inbox_markers": {s: world.has_marker(x, t["marker"]) for s, x in items},
            "marker": t["marker"], "output_text": res["text"],
            "adopted": world.has_marker(res["text"], t["marker"]),
            "status": res["status"], "error_class": res["error_class"], "attempts": attempt,
            "failed_attempt_tokens": failed,
            "model_requested": a.model, "model_returned": res["model_returned"],
            "provider": a.provider, "temperature": a.temperature, "usage": res["usage"],
            "latency_ms": res["latency_ms"],
            "prompt_sha256": hashlib.sha256((system + "\n\n" + prompt).encode()).hexdigest(),
            "instructions": system, "input": prompt,  # A21: exactly as sent; the hash recomputes from them
            "ts": datetime.now(timezone.utc).isoformat()}
        if a.experiment == "E4":
            row.update({k: t[k] for k in ("policy", "scenario", "attacker", "target")}, **(extra or {}), **(post(res) if post else {}))
        append(log, row)
        rows[sid] = row
        st["issued"] += 1
        st["logged"] += 1
        st["tokens"] += tokens_of(res["usage"]) + failed
        return row

    def run_exposure(t):
        rec, peer = t["recipient"], t["peer"]
        if t["experiment"] != "E1p":  # A6: the contaminated note(s) sit beside the agent's own clean note and a clean peer's
            prev = world.clean_note(rec)
            items = [(f"a{peer}", world.clean_note(peer))] + [
                (f"ext{i + 1}", world.contaminate(t, world.clean_note(t["sender"]))) for i in range(t["condition"]["dose"])]
        else:  # E1p: the marker is only in the agent's own previous note
            prev, items = world.contaminate(t, world.clean_note(rec)), [(f"a{peer}", world.clean_note(peer))]
        return issue(t, 1, rec, prev, items) is not None

    def run_cascade(t):
        nbrs = world.graph_of(t["topology"], a.seed, t["graph_id"])
        notes = world.round0_notes(t)  # round 0 is written by the harness: no provider call
        board = [(ag, notes[ag]) for ag in world.board_order(t, 0)]
        infected, hist = {t["seed_agent"]}, []
        for rnd in range(1, a.rounds + 1):
            order = [e[0] for e in board] if nbrs is None else None
            new = {}
            for ag in range(world.N):
                r = issue(t, rnd, ag, notes[ag], world.inbox_items(nbrs, ag, notes, board), order)
                if r is None:
                    return False
                new[ag] = r
            notes = {ag: r["output_text"] for ag, r in new.items()}
            board += [(ag, notes[ag]) for ag in world.board_order(t, rnd)]
            fresh = {ag for ag, r in new.items() if r["adopted"]} - infected
            infected |= fresh
            hist.append(len(fresh))
            if hist[-2:] == [0, 0]:  # no new infections for 2 rounds
                break
        return True

    def run_e4(t):
        """Sentinel round r reads the notes delivered to inboxes in round r (written r-1, minus blocked),
        after agents write. A flag in round r blocks that agent's notes written from round r+1: the note
        it wrote in round r is already out, so quarantine leaks one delivery by design."""
        nbrs = world.graph_of("mesh", a.seed, t["graph_id"])
        notes, quar, flagged = world.e4_round0(t, e4_texts), {}, {}
        delivered = set(range(world.N))
        for rnd in range(1, a.rounds + 1):
            seen = [(f"a{j}", notes[j]) for j in sorted(delivered)]
            new = {}
            for ag in range(world.N):
                inbox = [(f"a{j}", notes[j]) for j in nbrs[ag] if j in delivered]
                r = issue(t, rnd, ag, notes[ag], inbox, extra={"blocked": ag in quar})
                if r is None:
                    return False
                new[ag] = r
            s = issue(t, rnd, "sentinel", None, seen, extra={"blocked": False},
                      post=lambda res: {"flags": world.parse_flags(res["text"]) if res["status"] == "ok" else None})
            if s is None:
                return False
            world.quarantine_step(t["policy"], rnd, s["flags"], flagged, quar)
            blocked = {ag for ag in range(world.N) if new[ag]["blocked"]}
            notes, delivered = {ag: r["output_text"] for ag, r in new.items()}, set(range(world.N)) - blocked
            if rnd == 1 and t["scenario"] in ("framing", "placebo"):  # A18: the attacker posts a second time
                notes[t["attacker"]] += " " + world.attacker_note(t, e4_texts)
        return True

    for t in trials:
        run = run_exposure if a.experiment in ("E1", "E1S", "E1p") else run_e4 if a.experiment == "E4" else run_cascade
        if not run(t) and st["stop"]:  # a slot that failed on its own leaves its trial unfinished; the run goes on
            break
    return {"run_dir": str(run_dir), "issued": st["issued"], "stop": st["stop"],
            "unplanned": unplanned, "tokens": st["tokens"]}


def probe_summary(run_dir, spy):
    rows = [json.loads(x) for x in (Path(run_dir) / "calls.jsonl").read_text().splitlines()]
    ok = [r for r in rows if r["status"] == "ok"]
    avg = lambda xs: round(sum(xs) / len(xs), 1) if xs else None
    tok = lambda k: avg([r["usage"][k] for r in rows if r["usage"][k] is not None])
    return {"calls": len(rows), "status": dict(sorted(Counter(r["status"] for r in rows).items())),
            "adoption_rate": {c: avg([r["adopted"] for r in ok if r["condition"]["content"] == c]) for c in ("canary", "control")},
            "request_carried_rate": {c: avg([world.request_carried(r["output_text"], r["condition"]["wording"], r["marker"])
                                             for r in ok if r["condition"]["content"] == c]) for c in ("canary", "control")},
            "mean_input_tokens": tok("input"), "mean_output_tokens": tok("output"), "mean_reasoning_tokens": tok("reasoning"),
            "mean_latency_ms": avg([r["latency_ms"] for r in rows]), "http_429": sum(x.get("http_429", 0) for x in spy),
            "models_returned": sorted({r["model_returned"] for r in rows if r["model_returned"]})}


def main(argv=None, **inject):
    a = parse_args(argv)
    spy = []
    if a.dry_run:
        try:
            for exp in [a.experiment] if a.experiment else EXPERIMENTS:
                b = argparse.Namespace(**{**vars(a), "experiment": exp,
                                          **({"rounds": 1} if exp == "E2R" and not a.experiment else {})})
                trials, planned, _, sha = plan(b)
                print(f"{exp}.planned_slots_sha256={sha}  # slots={len(planned)} trials={len(trials)}")
        except ValueError as e:
            print(f"refused: {e}", file=sys.stderr)
            return 2
        return 0
    if a.probe:
        a.experiment, a.model, a.reps, a.smoke = "E1", a.probe, 5, True
        call = inject.get("call", llm.call_model)
        inject["call"] = lambda *x, **k: spy.append(call(*x, **k)) or spy[-1]
    elif not a.experiment:
        print("refused: experiment or --probe MODEL required", file=sys.stderr)
        return 2
    try:
        s = execute(a, **inject)
    except (guards.GuardError, ValueError, NotImplementedError) as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    print(f"run_dir={s['run_dir']} issued={s['issued']} tokens={s['tokens']} stop={s['stop']}")
    if a.probe:
        print(json.dumps(probe_summary(s["run_dir"], spy), indent=1))
    if s["unplanned"]:
        print(f"unplanned rows (not merged): {len(s['unplanned'])}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
