"""Small statistics helpers. Standard library only."""
import math
import random
from collections import Counter, defaultdict


def mean(xs):
    return sum(xs) / len(xs) if xs else float("nan")


def wilson(k, n, z=1.96):
    if n <= 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def newcombe_diff(k1, n1, k2, n2, z=1.96):
    """Newcombe hybrid-score CI for p1 - p2."""
    if n1 <= 0 or n2 <= 0:
        return None
    p1, p2 = k1 / n1, k2 / n2
    l1, u1 = wilson(k1, n1, z)
    l2, u2 = wilson(k2, n2, z)
    d = p1 - p2
    return (d, d - math.sqrt((p1 - l1) ** 2 + (u2 - p2) ** 2),
            d + math.sqrt((u1 - p1) ** 2 + (p2 - l2) ** 2))


def quantile(sorted_xs, q):
    if not sorted_xs:
        return float("nan")
    i = q * (len(sorted_xs) - 1)
    lo, hi = math.floor(i), math.ceil(i)
    return sorted_xs[lo] + (sorted_xs[hi] - sorted_xs[lo]) * (i - lo)


def cluster_bootstrap(arms, stat, b=2000, seed=0, alpha=0.05):
    """Percentile CI. arms = list of lists of clusters (cascades, or trials for E1);
    each arm is resampled independently, stat(list_of_resampled_arms) -> number or None."""
    if not arms or any(not a for a in arms):
        return (float("nan"), float("nan"))
    rng = random.Random(seed)
    vals = []
    for _ in range(b):
        v = stat([[a[rng.randrange(len(a))] for _ in a] for a in arms])
        if v is not None and v == v:
            vals.append(v)
    vals.sort()
    return (quantile(vals, alpha / 2), quantile(vals, 1 - alpha / 2))


def two_stage_bootstrap(groups, b=2000, seed=0, alpha=0.05):
    """Percentile CI for the grand mean of values nested in clusters (A5): resample clusters
    (graphs), then values within each. One cluster reduces to a plain bootstrap."""
    rng = random.Random(seed)
    vals = sorted(two_stage_draw(groups, rng) for _ in range(b))
    return (quantile(vals, alpha / 2), quantile(vals, 1 - alpha / 2))


def two_stage_draw(groups, rng):
    pick = [groups[rng.randrange(len(groups))] for _ in groups]  # graphs first, then cascades within
    return mean([g[rng.randrange(len(g))] for g in pick for _ in g])


def attribute(events, seed, rule="fractional"):
    """events: [(agent, round, ordered exposed parents)] for non-seed infections.
    Returns offspring per infected agent (seed included). Fractional splits one infection
    across all exposed parents; last-read credits the last-listed parent only."""
    off = defaultdict(float)
    off[seed] += 0.0
    for a, r, ps in events:
        off[a] += 0.0
        if not ps:
            continue
        if rule == "fractional":
            for p in ps:
                off[p] += 1 / len(ps)
        else:
            off[ps[-1]] += 1
    return dict(off)


def hist(values):
    return dict(sorted(Counter(round(v, 3) for v in values).items()))


def final_size_summary(sizes, n_agents):
    """Major outbreak (A7): final size >= half the agents, seed included (4 of 8)."""
    thr = math.ceil(n_agents / 2)
    k = sum(s >= thr for s in sizes)
    return {"n": len(sizes), "mean": mean(sizes), "dist": hist(sizes), "major_threshold": thr,
            "p_major": k / len(sizes) if sizes else None, "p_major_ci": wilson(k, len(sizes))}
