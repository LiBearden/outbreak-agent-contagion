"""Independent-cascade simulator mirroring the live design. Standard library only.

A graph is {node: [in-neighbours (sources)]}. Round 0: the seed's note carries the marker.
Each round, an agent's note carries it with probability 1-(1-q*[own previous note carried])
*(1-p)^exposures: independent exposures plus persistence q (from E1p, A6). A17: exposure to
the seed's round-0 note (round 1) is at p, the footer-form probability; all later exposure
is at pc, the carried-form probability (default: p). A20: pc = pc_rate(f, p, p_tag) = f*p + (1-f)*p_tag, where
f is the share of carriers that also pass on the request. This treats fidelity as independent across a
carrier's edges; that is a limitation, not a measured fact.
Events are (agent, round, exposed parents) for each agent's first infection, and go through
stats.attribute with the same rules as the live analysis."""
import random

from .stats import final_size_summary, mean

READ = 4  # latest board entries by others that an agent reads


def pc_rate(f, p, p_tag):
    """A20: probability that a later exposure carries: the request survives (f) and acts at the footer rate p,
    or it was dropped (1-f) and only the bare tag remains, acting at the carried rate p_tag."""
    return f * p + (1 - f) * p_tag


def _carry(rng, p, q, was, exposed):
    return rng.random() < 1 - (1 - q * was) * (1 - p) ** exposed


def cascade(adj, seed, p, q, rounds, rng, immune=frozenset(), efficacy=0.0, q_warned=None, pc=None):
    """Immune agents get p*(1-efficacy) and persistence q_warned (A13); None means the same q."""
    carry, infected, events, quiet = {seed}, {seed: 0}, [], 0
    for r in range(1, rounds + 1):
        nxt, new = set(), []
        for j in adj:
            ps = [s for s in adj[j] if s in carry]
            pr = p if r == 1 or pc is None else pc
            pj, qj = (pr * (1 - efficacy), q if q_warned is None else q_warned) if j in immune else (pr, q)
            if _carry(rng, pj, qj, j in carry, len(ps)):
                nxt.add(j)
                if j not in infected:
                    infected[j] = r
                    new.append((j, r, ps))
        carry, events = nxt, events + new
        quiet = 0 if new else quiet + 1
        if quiet >= 2:  # spec: stop after 2 rounds with no new infections
            break
    return infected, events


def board_cascade(n, seed, p, q, rounds, rng, pc=None):
    """Shared board (A7, A8): 8 round-0 entries in seeded order; each agent reads the latest
    `READ` entries by others as of the round start; all then append, in a seeded shuffle."""
    carry, infected = {seed}, {seed: 0}
    entries = [(a, a == seed) for a in rng.sample(range(n), n)]
    events, quiet = [], 0
    for r in range(1, rounds + 1):
        nxt, new = set(), []
        for a in range(n):
            ps = list(dict.fromkeys(x for x, c in [e for e in entries if e[0] != a][-READ:] if c))
            if _carry(rng, p if r == 1 or pc is None else pc, q, a in carry, len(ps)):
                nxt.add(a)
                if a not in infected:
                    infected[a] = r
                    new.append((a, r, ps))
        carry, events = nxt, events + new
        entries += [(a, a in carry) for a in rng.sample(range(n), n)]
        quiet = 0 if new else quiet + 1
        if quiet >= 2:
            break
    return infected, events


def round1_offspring(events):
    return sum(r == 1 for _, r, _ in events)  # only the seed carries at round 0


def pick_seed(nodes, adj, position, rng):
    if adj and position in ("hub", "leaf"):
        hub = max(nodes, key=lambda j: len(adj[j]))
        return hub if position == "hub" else rng.choice([j for j in nodes if j != hub])
    return rng.choice(nodes)


def draw(rng, kn):
    # Beta(k+1, n-k+1): uniform prior, so the E1 / E1p uncertainty is propagated
    k, n = kn
    return rng.betavariate(k + 1, n - k + 1)


def predict(graphs, p, pc, q, rounds, n_cascades, reps, seed, position=None, pseed=None):
    """graphs: list of adjacency dicts (a cascade picks one), or an int N for the shared board.
    p, pc, q: (successes, trials) from E1 footer, E1 carried and E1p. Each rep draws all three,
    runs n_cascades cascades, and returns that rep's mean round-1 offspring and mean final size:
    the predictive distribution of a live batch. pseed fixes the Beta draws on their own stream, so
    two conditions that share an E1 estimate can share its draws (the pooled ratio needs that)."""
    rng = random.Random(seed)
    prng = rng if pseed is None else random.Random(pseed)
    board = isinstance(graphs, int)
    out = []
    for _ in range(reps):
        pd, pcd, qd = draw(prng, p), draw(prng, pc), draw(prng, q)
        z, s = [], []
        for _ in range(n_cascades):
            adj = None if board else rng.choice(graphs)
            sd = pick_seed(list(range(graphs)) if board else list(adj), adj, position, rng)
            inf, ev = (board_cascade(graphs, sd, pd, qd, rounds, rng, pcd) if board
                       else cascade(adj, sd, pd, qd, rounds, rng, pc=pcd))
            z.append(round1_offspring(ev))
            s.append(len(inf))
        out.append((mean(z), mean(s)))
    return out


def immunity_curve(adjs, p, q, efficacy, rounds, counts, n_cascades, seed, position=None, q_warned=None, pc=None):
    """Random vs hub-targeted immunization of exactly `count` agents (simulated). Immunized
    agents get p*(1-efficacy); the seed is never immunized (A9)."""
    rows = []
    for placement in ("random", "hub"):
        for k in counts:
            rng = random.Random(f"{seed}/{placement}/{k}")
            sizes, z1 = [], []
            for _ in range(n_cascades):
                adj = rng.choice(adjs)
                nodes = list(adj)
                sd = pick_seed(nodes, adj, position, rng)
                cand = [j for j in nodes if j != sd]
                if placement == "random":
                    imm = set(rng.sample(cand, min(k, len(cand))))
                else:  # ties in degree broken at random
                    imm = set(sorted(cand, key=lambda j: (-len(adj[j]), rng.random()))[:k])
                inf, ev = cascade(adj, sd, p, q, rounds, rng, imm, efficacy, q_warned, pc)
                sizes.append(len(inf))
                z1.append(round1_offspring(ev))
            fs = final_size_summary(sizes, len(nodes))
            rows.append({"placement": placement, "n_immunized": k, "n": n_cascades,
                         "mean_z1": mean(z1), "mean_final_size": fs["mean"], "p_major": fs["p_major"]})
    return rows
