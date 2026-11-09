"""Power and budget calculations behind PREREG.md. Standard library only, seeded, offline, no LLM calls.
Run from anywhere: python3 scripts/power.py   (about two minutes). Every number quoted in PREREG.md is printed here.
Nothing is fitted to data; there is none yet.

The primary prediction check (A15, A19) compares live round-1 offspring with the simulator's: ratio = live mean Z1 /
predicted mean Z1. Agreement iff the 95% CI of the ratio lies inside BAND, miss iff it lies outside, else inconclusive;
with fewer than MIN_EVENTS live or E1 adoption events there is no verdict. Round-1 offspring does not depend on
persistence q (only the seed carries at round 0), so for each condition Z1 per cascade is Binomial(exposed agents, p)
and the prediction is deg * p_hat from E1. The CI of the log ratio is approximated by the delta method (live sampling
variance plus E1's Beta variance) and enumerated exactly over the live total and the E1 count; the pooled ratio over
the three primaries is simulated the same way. Section CHECK runs the project's own analyze.ratio_check as a Monte
Carlo test of the single-condition approximation. Counts and hashes come from the runner's own planner."""
import bisect
import math
import random
import sys
from pathlib import Path
from statistics import NormalDist

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from outbreak import analyze, runner, simulate as sim, world  # noqa: E402
from outbreak.stats import mean, newcombe_diff, wilson  # noqa: E402

SEED = 20261106
PS, QS = (0.05, 0.1, 0.2, 0.35, 0.5), (0.3, 0.8)
BAND = analyze.BAND                 # A15, imported so the two cannot drift
MIN_EV = analyze.MIN_EVENTS         # A19
LB, UB = math.log(BAND[0]), math.log(BAND[1])
HALF = 0.07           # E1 target 95% CI half-width, secondary cells
GAP = 0.3             # E4 target gap
POWER = 0.8
ALPHA_GAP = 0.025     # two-sided; Bonferroni over the 2 E4 policies
AGREE, FALSE_AGREE = 0.8, 0.05      # correct model agrees w.p. >= 0.8; a factor-2 error agrees w.p. <= 0.05
WRONG = (0.5, 2.0)
TOK, ROUNDS, N = 750, 4, 8  # TOK: max_total_tokens per planned call
RPMS = (50, 60, 300, 600)
MESH_F = 1.9          # A5 two-stage bootstrap over 10 graphs inflates the live variance by about 2 - 1/10 (see CHECK)
Z = NormalDist().inv_cdf
Phi = NormalDist().cdf
rng = random.Random(SEED)

ALL_POL, ALL_SCEN = ("off", "single", "corroborated"), ("benign", "outbreak", "framing", "placebo")
FRAMING = (("single", "corroborated"), ("framing", "placebo"))
# A19: each experiment has its own seed (the prereg block carries them as E1.seed=... lines)
SEEDS = {"E1": 1, "E1S": 2, "E1p": 3, "E2R": 4, "E2": 5, "E3": 6, "E4": 7, "E2N": 8}
# Reps per experiment and tier. E1: n per pooled cell = 3 x reps. E2R/E2 reps are multiples of 30 (10 graphs x 3 wordings),
# E3 of 3. E1 and E2R are the smallest designs the PRIMARY table gives for p >= 0.2 / 0.1 / 0.05 (star-hub, mesh, shared).
# A20: E2N (no-copy side arm, star-hub and mesh, canary only) has the same reps in every tier and at most E2N_CAP calls.
# 12 reps = 4 per wording; 12 x (star-hub + mesh) x 4 rounds x 8 agents = 768. Mesh cycles the 10 graphs, so two graphs get 2.
E2N_REPS, E2N_CAP = 12, 800
PTAG = 0.05           # planted later-round rate of a bare tag in the E2N arm (A20 planning: agents shown a bare tag almost never copied)
TIERS = {
    "lean": dict(E1=280, E1S=20, E1p=20, E2R=180, E2=30, E2N=E2N_REPS, E3=0, E4=(30,) + FRAMING),
    # A22: the standard tier adds the E4 `outbreak` scenario (monitor check); framing and placebo keep their reps and ids
    "standard": dict(E1=570, E1S=40, E1p=40, E2R=420, E2=60, E2N=E2N_REPS, E3=0,
                     E4=(60, FRAMING[0], FRAMING[1] + ("outbreak",))),
    "high": dict(E1=1170, E1S=64, E1p=150, E2R=900, E2=150, E2N=E2N_REPS, E3=51, E4=(90, ALL_POL, ALL_SCEN)),
}


def argv(exp, reps, e4=None):
    """The command line a run uses: the single place a design's flags are written down."""
    base = {"E1": ["--contents", "canary"], "E1S": [], "E1p": ["--contents", "canary"],
            "E2R": ["--rounds", "1", "--contents", "canary"], "E2": ["--contents", "canary"],
            "E2N": ["--topologies", "star,mesh", "--contents", "canary"],
            "E3": ["--topologies", "star", "--contents", "canary", "--warned-counts", "2,4,6"],
            "E4": ["--policies", ",".join(e4[0]), "--scenarios", ",".join(e4[1])] if e4 else []}[exp]
    return [exp, "--reps", str(reps), "--seed", str(SEEDS[exp]), *base]


def plan(exp, reps, e4=None):
    """(slots, trials, sha256) of the design, from the planner behind `python3 -m outbreak.runner --dry-run`."""
    trials, planned, _, sha = runner.plan(runner.parse_args(argv(exp, reps, e4)))
    return len(planned), len(trials), sha


# ---------------- distributions ----------------
def logpmf(n, p, k):
    return (math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)
            + k * math.log(p) + (n - k) * math.log1p(-p))


def window(n, p, sd=6.0):
    """(k, prob) over the +-sd window of Binomial(n, p)."""
    s = math.sqrt(n * p * (1 - p))
    lo, hi = max(0, int(n * p - sd * s - 1)), min(n, int(n * p + sd * s + 2))
    return [(k, math.exp(logpmf(n, p, k))) for k in range(lo, hi + 1)]


def binom_pmf(n, p):
    return [math.exp(logpmf(n, p, k)) for k in range(n + 1)]


def sampler(pmf):
    """Draw from a (value, prob) list by inverse CDF."""
    cum, c = [], 0.0
    for _, w in pmf:
        c += w
        cum.append(c)
    return lambda r: pmf[min(bisect.bisect(cum, r * c), len(pmf) - 1)][0]


# ---------------- E1 / E1p: Wilson half-width ----------------
def e1_n(p):
    for n in range(10, 1000):
        h = sum(w * (wilson(k, n)[1] - wilson(k, n)[0]) / 2 for k, w in window(n, p))
        if h < HALF:
            return n, h


# ---------------- exposure and per-cascade variance ----------------
def exposure_dist():
    """Round-1 exposed agents per shared-board cascade (sampled from simulate.board_cascade at p=1)."""
    vs = []
    for _ in range(4000):
        _, ev = sim.board_cascade(N, rng.randrange(N), 1.0, 0.0, 1, rng)
        vs.append(sim.round1_offspring(ev))
    m = mean(vs)
    return m, sum((x - m) ** 2 for x in vs) / len(vs), sum(x > 0 for x in vs) / len(vs)


def cascade_var(deg, vdeg, pl):  # variance of one cascade's Z1 when `deg` agents are exposed on average
    return deg * pl * (1 - pl) + vdeg * pl * pl


# ---------------- the ratio check ----------------
def se_log_ratio(deg, vdeg, n_c, n1, x, k, f=1.0):
    """Delta-method SE of log(live mean / (deg p_hat)) from the live mean x per cascade and E1 count k of n1."""
    pl, ph = x / deg, k / n1
    return math.sqrt(f * cascade_var(deg, vdeg, pl) / (n_c * x * x) + (1 - ph) / (n1 * ph))


def agree_closed(deg, vdeg, n_c, n1, p, m, f=1.0):
    """Normal-approximation P(agreement) when live p is m * p; used only to search for designs."""
    pl = m * p
    if pl >= 1:
        return None
    s = math.sqrt(f * cascade_var(deg, vdeg, pl) / (n_c * (deg * pl) ** 2) + (1 - p) / (n1 * p))
    lo, hi = LB + 1.96 * s, UB - 1.96 * s
    return 0.0 if hi <= lo else Phi((hi - math.log(m)) / s) - Phi((lo - math.log(m)) / s)


def live_pmf(deg, vdeg, n_c, pl):
    """Live total over n_c cascades: a binomial matched to the cascade mean and variance (exact when vdeg = 0)."""
    mu, v = n_c * deg * pl, n_c * cascade_var(deg, vdeg, pl)
    pe = max(1e-9, 1 - v / mu)
    ne = max(1, round(mu / pe))
    return window(ne, mu / ne)


def verdict_probs(deg, vdeg, n_c, n1, p, m, f=1.0):
    """(P(agreement), P(miss)) when live p is m * p: enumerate the E1 count k ~ Bin(n1, p) and the live total T, apply
    the delta-method CI. Fewer than MIN_EV events on either side is inconclusive, so it adds to neither."""
    pl = m * p
    if pl >= 1:
        return None
    live = live_pmf(deg, vdeg, n_c, pl)
    agree = miss = 0.0
    for k, wk in window(n1, p):
        if k < MIN_EV or wk < 1e-9:
            continue
        for t, wt in live:
            if t < MIN_EV:
                continue
            x = t / n_c
            s = se_log_ratio(deg, vdeg, n_c, n1, x, k, f)
            lr = math.log(x / (deg * k / n1))
            if lr - 1.96 * s >= LB and lr + 1.96 * s <= UB:
                agree += wk * wt
            elif lr + 1.96 * s < LB or lr - 1.96 * s > UB:
                miss += wk * wt
    return min(agree, 1.0), min(miss, 1.0)


def pooled_probs(conds, n_c, n1, p, m, draws=3000):
    """(P(agreement), P(miss)) of the pooled ratio over star-hub, mesh and shared (equal cascade counts n_c):
    total live events over total predicted events, E1 counts drawn separately for the direct and shared channels."""
    pl = m * p
    if pl >= 1:
        return None
    live = {c: sampler(live_pmf(d, vd, n_c, pl)) for c, (d, vd, f) in conds.items()}
    e1 = sampler(window(n1, p))
    a = n_c * (conds["star-hub"][0] + conds["mesh"][0])      # direct-channel predicted events per unit p
    b = n_c * conds["shared"][0]
    agree = miss = 0
    for _ in range(draws):
        kd, ks = e1(rng.random()), e1(rng.random())
        ts = {c: live[c](rng.random()) for c in conds}
        tot = sum(ts.values())
        if min(kd, ks) < MIN_EV or tot < MIN_EV:
            continue
        pred = (a * kd + b * ks) / n1
        var = sum(f * n_c * cascade_var(d, vd, ts[c] / (n_c * d)) for c, (d, vd, f) in conds.items())
        rel = var / tot ** 2 + (a * a * (kd / n1) * (1 - kd / n1) + b * b * (ks / n1) * (1 - ks / n1)) / (n1 * pred ** 2)
        lr, s = math.log(tot / pred), 1.96 * math.sqrt(rel)
        agree += lr - s >= LB and lr + s <= UB
        miss += lr + s < LB or lr - s > UB
    return agree / draws, miss / draws


def min_cascades(deg, vdeg, n1, p, f=1.0, cap=40000):
    """Smallest n_c (multiple of 10) with P(agree | m=1) >= 0.8 and P(agree | m=0.5, 2) <= 0.05, else None
    (normal approximation, used only to search; the printed tables use verdict_probs)."""
    def ok(n):
        if agree_closed(deg, vdeg, n, n1, p, 1.0, f) < AGREE:
            return False
        return all(a <= FALSE_AGREE for a in (agree_closed(deg, vdeg, n, n1, p, m, f) for m in WRONG) if a is not None)
    if not ok(cap):
        return None
    lo, hi = 1, cap // 10
    while lo < hi:
        mid = (lo + hi) // 2
        lo, hi = (lo, mid) if ok(mid * 10) else (mid + 1, hi)
    return lo * 10


def ceil_to(x, m):
    return int(math.ceil(x / m) * m)


# ---------------- E3 / E4: two-proportion gap with the project's Newcombe interval ----------------
def gap_power(n, b, gap, z):
    p0, p1 = binom_pmf(n, b), binom_pmf(n, b + gap)
    pw = 0.0
    for k1, w1 in enumerate(p1):
        if w1 < 1e-9:
            continue
        for k0, w0 in enumerate(p0):
            if w0 >= 1e-9 and newcombe_diff(k1, n, k0, n, z)[1] > 0:
                pw += w1 * w0
    return pw


def gap_n(b):
    for n in range(5, 400, 5):
        pw = gap_power(n, b, GAP, Z(1 - ALPHA_GAP / 2))
        if pw >= POWER:
            return n, pw


def tier_plans(t):
    """{design: (reps, slots, trials, sha256)} for one tier, from the planner."""
    out = {}
    for exp in ("E1", "E1S", "E1p", "E2R", "E2", "E2N", "E3", "E4"):
        reps = t[exp][0] if exp == "E4" else t[exp]
        if reps:
            out[exp] = (reps, *plan(exp, reps, t["E4"][1:] if exp == "E4" else None))
    return out


def main():
    print(f"seed {SEED}; planted p in {PS}, q in {QS}; {TOK} tokens/call; band {BAND}; minimum events {MIN_EV}; "
          f"agreement target {AGREE}, factor-2 false agreement limit {FALSE_AGREE}\n")
    mv, vv, vis = exposure_dist()
    print(f"shared board: round-1 exposed agents per cascade mean {mv:.3f} variance {vv:.3f}; "
          f"seed visible in {vis:.3f} of cascades")
    conds = {"star-hub": (7.0, 0.0, 1.0), "star-leaf": (1.0, 0.0, 1.0), "mesh": (4.0, 0.0, MESH_F), "shared": (mv, vv, 1.0)}
    prim = {c: v for c, v in conds.items() if c != "star-leaf"}  # A19: star-leaf is secondary
    per = {e: plan(e, 1)[0] for e in ("E1", "E1S", "E1p")}
    r8 = plan("E2R", 30)[0] // 30
    print(f"slots per rep: E1 {per['E1']} (4 cells: channel x form; n per pooled cell = 3 x reps, canary only), "
          f"E1S {per['E1S']} (canary and control), E1p {per['E1p']} (canary only); E2R {r8} per rep over its four "
          f"conditions (8 calls per cascade)")

    print(f"\nSecondary cells: E1/E1S/E1p trials per pooled cell for expected Wilson 95% half-width < {HALF}")
    for lab, v in [("p", x) for x in PS] + [("q", x) for x in QS]:
        n, h = e1_n(v)
        print(f"  {lab}={v}: n={n} (expected half-width {h:.4f})")

    # ---- primary: smallest designs for the ratio check (normal approximation, primaries only) ----
    print(f"\nPRIMARY ratio check, smallest design per p. E1 reps r1 (n1 = 3 r1 per pooled cell) and E2R reps u (one --reps "
          f"for all four conditions, multiple of 30) minimise calls = {per['E1']} r1 + 32 u such that, for star-hub, mesh and "
          "shared, P(agreement | correct) >= 0.8 and P(agreement | p x0.5 or x2) <= 0.05 (star-leaf is secondary and "
          "does not size the design).")
    for p in PS:
        best = None
        for r1 in range(10, 3000, 10):
            ncs = {c: min_cascades(d, vd, 3 * r1, p, f) for c, (d, vd, f) in prim.items()}
            if any(v is None for v in ncs.values()):
                continue
            cost = per["E1"] * r1 + 32 * ceil_to(max(ncs.values()), 30)
            if best is None or cost < best[0]:
                best = (cost, r1, ncs)
        cost, r1, ncs = best
        print(f"  p={p}: E1 reps {r1} (n1 {3 * r1}); cascades " + ", ".join(f"{c} {n}" for c, n in ncs.items())
              + f"; E2R reps {ceil_to(max(ncs.values()), 30)}; calls {cost}")

    # ---- F2: a correct model at p near zero ----
    pk, pt = binom_pmf(192, 0.005), binom_pmf(240, 0.005)
    few = 1 - sum(pk[MIN_EV:]) * sum(pt[MIN_EV:])
    ag, ms = verdict_probs(4, 0, 60, 192, 0.005, 1.0)
    print(f"\nF2 (audit): correct model, p=0.005, E1 n=192, 60 live cascades of degree 4: P(no live event) = {pt[0]:.3f} "
          f"(the old rule called every such dataset a miss); P(fewer than {MIN_EV} events on either side, so no verdict) = "
          f"{few:.5f}; P(agreement) = {ag:.4f}; P(miss) = {ms:.4f}")

    # ---- final size: precision with the full cascades ----
    print("\nFINAL SIZE (secondary; the only estimand q touches). SD of final size and 95% half-width of its mean at "
          "30 and 90 full cascades, simulated (2000 cascades per cell)")
    adj = {j: [(j + s) % N for s in (-2, -1, 1, 2)] for j in range(N)}
    star = {0: list(range(1, N)), **{i: [0] for i in range(1, N)}}
    for q in QS:
        for p in PS:
            row = []
            for c, (g, pos) in {"star-hub": (star, 0), "star-leaf": (star, 1), "mesh": (adj, None)}.items():
                sz = [len(sim.cascade(g, pos if pos is not None else rng.randrange(N), p, q, ROUNDS, rng)[0])
                      for _ in range(2000)]
                sd = math.sqrt(sum((x - mean(sz)) ** 2 for x in sz) / len(sz))
                row.append(f"{c} sd {sd:.2f} hw {1.96 * sd / math.sqrt(30):.2f}/{1.96 * sd / math.sqrt(90):.2f}")
            print(f"  q={q} p={p}: " + "; ".join(row))

    # ---- E2N / H6: expected width of the interval on E2 canary minus E2N canary final size ----
    print(f"\nH6 (A20): E2 canary minus E2N canary mean final size, star-hub and mesh. E2N has {E2N_REPS} cascades per condition "
          f"in every tier ({E2N_REPS // 3} per wording; mesh graphs 0 and 1 used twice; {plan('E2N', E2N_REPS)[0]} calls, cap {E2N_CAP}); E2 has its tier's reps. Expected 95% interval width "
          f"= 2 x 1.96 x sqrt(F (sd_E2^2/n_E2 + sd_E2N^2/n_E2N)), F = {MESH_F} for mesh (graph-clustered bootstrap) and 1 for star-hub; "
          f"sd from 2000 simulated T={ROUNDS} cascades per arm. E2 arm: self-copying, pc = p. E2N arm: footer at p, then the bare "
          f"tag at pc = {PTAG}. Widths in agents of 8; q = {QS[0]} / {QS[1]}; the analysis calls a wider interval "
          f"inconclusive (H6_MAX_WIDTH = {analyze.H6_MAX_WIDTH}).")
    hrng = random.Random(SEED + 1)  # own stream: adding this section leaves every other printed number unchanged
    sd_of = lambda g, pos, p, q, pc: (lambda sz: math.sqrt(sum((x - mean(sz)) ** 2 for x in sz) / len(sz)))(
        [len(sim.cascade(g, pos if pos is not None else hrng.randrange(N), p, q, ROUNDS, hrng, pc=pc)[0]) for _ in range(2000)])
    wmax = 0.0
    for name, t in TIERS.items():
        for cn, (g, pos, F) in {"star-hub": (star, 0, 1.0), "mesh": (adj, None, MESH_F)}.items():
            cells = []
            for p in PS:
                ws = []
                for q in QS:
                    w = 2 * 1.96 * math.sqrt(F * (sd_of(g, pos, p, q, None) ** 2 / t["E2"]
                                                  + sd_of(g, pos, p, q, PTAG) ** 2 / E2N_REPS))
                    ws.append(w)
                    wmax = max(wmax, w) if name == "standard" else wmax
                cells.append(f"p={p}: {ws[0]:.2f}/{ws[1]:.2f}")
            print(f"  {name} (E2 n = {t['E2']}) {cn:8s} " + "; ".join(cells))
    print(f"  largest standard-tier width {wmax:.2f}; H6_MAX_WIDTH {analyze.H6_MAX_WIDTH}")

    # ---- E3 / E4 ----
    print(f"\nE4 (and E3): cascades per cell, gap {GAP} over a placebo/matched cell, 80% power, "
          f"Newcombe {100 * (1 - ALPHA_GAP):.1f}% interval excluding 0 (alpha {ALPHA_GAP} two-sided)")
    gn = {}
    for b in (0.05, 0.2, 0.5):
        gn[b] = gap_n(b)
        print(f"  baseline rate {b} vs {b + GAP:.2f}: n per cell = {gn[b][0]} (power {gn[b][1]:.3f})")
    print(f"  worst baseline needs {max(v[0] for v in gn.values())}; E4 reps are multiples of 30, so 60 is the smallest that fits")
    z = Z(1 - ALPHA_GAP / 2)
    for n in (30, 60, 90):
        print(f"  n={n}: power at baseline 0.05/0.2/0.5 = " + " / ".join(f"{gap_power(n, b, GAP, z):.3f}" for b in (0.05, 0.2, 0.5)))

    # ---- tiers ----
    print("\nTIERS. Counts, trials and hashes come from the planner behind `python3 -m outbreak.runner --dry-run` "
          f"(upper bounds: early stopping lowers E2, E3); tokens at {TOK}/call; wall clock = calls / rpm.")
    fmt = lambda x: "--" if x is None else f"{math.floor(x * 100) / 100:.2f}"
    worst = 0.0  # largest P(miss | model correct) over every tier, p and condition
    for name, t in TIERS.items():
        plans = tier_plans(t)
        tot = sum(v[1] for v in plans.values())
        assert plans["E2N"][1] <= E2N_CAP, plans["E2N"]
        print(f"  {name}:")
        for exp, (reps, slots, trials, sha) in plans.items():
            print(f"    {exp}.max_total_tokens={slots * TOK}")
            extra = f" (n {3 * reps}/pooled cell)" if exp in ("E1", "E1S", "E1p") else ""
            print(f"    {exp} --reps {reps}{extra} --seed {SEEDS[exp]}: slots {slots} trials {trials} "
                  f"{exp}.planned_slots_sha256={sha}")
        print(f"    TOTAL calls {tot}; tokens {tot * TOK:,}; wall clock " + "; ".join(f"{r} rpm {tot / r / 60:.1f} h" for r in RPMS))
        print(f"    E4 gap power at n={t['E4'][0]}: baseline 0.05/0.2/0.5 = "
              + " / ".join(f"{gap_power(t['E4'][0], b, GAP, z):.3f}" for b in (0.05, 0.2, 0.5)))
        n1, nc = 3 * t["E1"], t["E2R"]
        print(f"    ratio check: E1 n1 = {n1} per channel and form, {nc} round-1 cascades per condition (E2R). "
              "Each cell: P(agreement)/P(miss) when the model is correct | p x0.5 | p x2; '--' = p x2 above 1; "
              "'*' = correct-model agreement below 0.8 (not reached)")
        for cn, (d, vd, f) in conds.items():
            cells = []
            for p in PS:
                a = [verdict_probs(d, vd, nc, n1, p, m, f) for m in (1.0, 0.5, 2.0)]
                worst = max(worst, a[0][1])
                body = " | ".join("--" if x is None else f"{fmt(x[0])}/{fmt(x[1])}" for x in a)
                cells.append(f"p={p}: {body}" + ("" if a[0][0] >= AGREE else " *"))
            print(f"      {cn:9s} " + " ; ".join(cells))
        cells = []
        for p in PS:
            a = [pooled_probs(prim, nc, n1, p, m) for m in (1.0, 0.5, 2.0)]
            worst = max(worst, a[0][1])
            body = " | ".join("--" if x is None else f"{fmt(x[0])}/{fmt(x[1])}" for x in a)
            cells.append(f"p={p}: {body}" + ("" if a[0][0] >= AGREE else " *"))
        print("      pooled    " + " ; ".join(cells))

    print(f"\nLargest P(miss | model correct) over every tier, p and condition above: {worst:.4f}")

    # ---- check: the approximation against the project's ratio_check ----
    print("\nCHECK: share of Monte Carlo trials that analyze.ratio_check calls agreement / miss vs the exact delta-method "
          "probability (40 trials, 100 predictive reps; live data and E1 counts drawn fresh each trial; mesh rows use the 10-graph two-stage bootstrap)")
    graphs = [world.graph("mesh", 0, i) for i in range(10)]
    crng = random.Random(SEED)
    for name, deg, G, ncg, gr, pos, f in [("mesh", 4, 10, 17, graphs, None, MESH_F), ("star-hub", 7, 1, 60, [star], "hub", 1.0)]:
        p, n1, trials = 0.2, 420, 40
        for m in (1.0, 1.3, 2.0):
            ag = ms = 0
            for t in range(trials):
                k = sum(crng.random() < p for _ in range(n1))
                live = {i: [sum(crng.random() < m * p for _ in range(deg)) for _ in range(ncg)] for i in range(G)}
                r = analyze.ratio_check(live, gr, (k, n1), (k, n1), (1, 2), pos, 100, f"chk{t}")
                ag += r[3] == "agreement"
                ms += r[3] == "miss"
            ex = verdict_probs(deg, 0, G * ncg, n1, p, m, f)
            print(f"  {name} p={p} n_c={G * ncg} n1={n1} m={m}: ratio_check agree {ag}/{trials} = {ag / trials:.2f}, "
                  f"miss {ms}/{trials} = {ms / trials:.2f}; delta-method {ex[0]:.2f} / {ex[1]:.2f}")


if __name__ == "__main__":
    main()
