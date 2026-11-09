"""Prereg freeze check (pattern: research-stack run_parrot_glimmer.sh lines 74-110) and smoke cap."""
import re
import subprocess

SMOKE_CAP = 20
PLANNED_KEY = "planned_slots_sha256"  # the design binding: reps, cells, topologies, seeds, policies, wordings
# A19 freeze scope: every outbreak/*.py (these must exist at the frozen commit) and everything under config/.
SCOPE = re.compile(r"outbreak/[^/]+\.py|config/.+")
REQUIRED = ["outbreak/__init__.py", "outbreak/analyze.py", "outbreak/audit.py", "outbreak/guards.py",
            "outbreak/llm.py", "outbreak/runner.py", "outbreak/simulate.py", "outbreak/stats.py", "outbreak/world.py"]


class GuardError(Exception):
    pass


def run_git(repo, *args):
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    return p.returncode, p.stdout


def smoke_remaining(logged):
    return max(0, SMOKE_CAP - logged)


def check_prereg(ref, params, planned_sha, prereg_file, repo, git=run_git, free=()):
    """Refuse unless ref is a commit SHA (not a branch, tag or HEAD) that is an ancestor of HEAD, its
    prereg is FROZEN, its prereg-params block states every run parameter plus this experiment's
    planned_slots_sha256 and all match (A16/A17), and the frozen scope (every outbreak/*.py and
    config/ file, A19) is unchanged in the working tree, with nothing added. `free` keys must be stated but may differ (A19: rpm and budget on resume). Block lines are `key=value`, or `E2.key=value` for one experiment. Returns the full SHA."""
    if not ref:
        raise GuardError("non-smoke run needs --prereg <commit sha> (or use --smoke)")
    rc, out = git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    commit = out.strip()
    if rc or not re.fullmatch(r"[0-9a-f]{7,40}", ref) or not commit.startswith(ref):
        raise GuardError(f"--prereg {ref!r} is not a commit SHA (branch names, tags and HEAD are refused)")
    if git(repo, "merge-base", "--is-ancestor", commit, "HEAD")[0]:
        raise GuardError(f"prereg {commit} is not an ancestor of HEAD")
    rc, txt = git(repo, "show", f"{commit}:{prereg_file}")
    if rc:
        raise GuardError(f"cannot read {prereg_file} at {commit}")
    if not re.search(r"^\*\*Status: FROZEN", txt, re.M):
        raise GuardError(f"prereg {commit}: {prereg_file} is not FROZEN")
    m = re.search(r"^[ \t]*```prereg-params[ \t]*\n(.*?)^[ \t]*```", txt, re.M | re.S)
    if not m:  # unlike the shell pattern this fails closed: the spec requires the block
        raise GuardError(f"prereg {commit}: no prereg-params block")
    shared, scoped = {}, {}
    for line in m.group(1).splitlines():
        if not line.strip():
            continue
        k, _, v = line.partition("=")
        scope, dot, name = k.strip().partition(".")
        v = v.partition(" #")[0].strip()  # the dry-run line carries a trailing comment
        if not dot:
            shared[scope] = v
        elif scope == params["experiment"]:
            scoped[name] = v
    have = {"experiment": params["experiment"], **shared, **scoped}  # a scope names its experiment
    actual = {**params, PLANNED_KEY: planned_sha}
    for k in have:
        if k not in actual:
            raise GuardError(f"prereg {commit}: unknown parameter {k}")
    missing = [k for k in actual if k not in have]
    if missing:  # a key the prereg does not state is a key it does not bind
        # A20: with no lines for this experiment at all (E3 under a standard-tier freeze) the message says so
        none = "" if scoped else f" (the block has no {params['experiment']} lines, so it does not bind it)"
        raise GuardError(f"prereg {commit}: prereg-params lacks {', '.join(missing)} for {params['experiment']}{none}")
    for k, v in have.items():
        if k not in free and str(actual[k]) != v:
            raise GuardError(f"prereg {commit}: {k}={v}, run has {k}={actual[k]}")
    rc, out = git(repo, "ls-tree", "-r", "--name-only", commit, "--", "outbreak", "config")
    frozen = sorted(f for f in out.split("\n") if SCOPE.fullmatch(f))
    if rc or [f for f in REQUIRED if f not in frozen]:
        raise GuardError(f"prereg {commit} does not freeze {', '.join(f for f in REQUIRED if f not in frozen) or 'the code'}")
    _, now = git(repo, "ls-files", "--cached", "--others", "--exclude-standard", "--", "outbreak", "config")
    added = sorted(f for f in set(now.split("\n")) if SCOPE.fullmatch(f) and f not in frozen)
    if added:  # a new module can change behaviour without touching a frozen file
        raise GuardError(f"files in the frozen scope were added after prereg {commit}: {', '.join(added)}")
    if git(repo, "diff", "--quiet", commit, "--", *frozen, prereg_file)[0]:
        raise GuardError(f"frozen code differs from prereg {commit}")
    return commit
