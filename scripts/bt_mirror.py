"""One-way mirror of runs/*/calls.jsonl to a private Braintrust project (A21). Standard library only.

  python3 scripts/bt_mirror.py --runs-dir runs --env-file PATH [--run-id ID ...] [--follow SECONDS] [--phase planning|study]

Reads only complete lines, tracks a byte offset per run in <state-dir>/bt_mirror_state.json (default: the
runs dir), inserts in batches with stable event ids (re-uploading replaces, never duplicates), and logs
error classes only to <state-dir>/bt_mirror.jsonl. It never writes inside a run directory, and nothing in
outbreak/ imports it. The API key is parsed from --env-file as text and goes only into the Authorization header."""
import argparse
import json
import re
import signal
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API = "https://api.braintrust.dev"
PROJECT = "outbreak"
BATCH = 50
PLANNING_PREFIXES = ("probe-", "explore-")
ANCHORS = ["word for word", "entire reminder", "reproduce this note"]  # copy of outbreak.world.ANCHORS (tested for parity)
MARKER = r"(?<![0-9A-Za-z])OB-[0-9a-f]{8}(?![0-9A-Za-z])"


class MirrorError(Exception):
    pass


def phase_of(run_id, smoke=False):
    return "planning" if smoke or run_id.startswith(PLANNING_PREFIXES) else "study"


def request_carried(text, wording, m):
    """Copy of outbreak.world.request_carried (tested for parity): the marker plus the wording's anchor phrase."""
    pat = r"(?<![0-9A-Za-z])" + re.escape(m) + r"(?![0-9A-Za-z])" if m else MARKER
    return bool(re.search(pat, text)) and ANCHORS[wording] in text.lower()


def load_key(path):
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        k, _, v = line.strip().partition("=")
        if k.removeprefix("export ").strip() == "BRAINTRUST_API_KEY":
            return v.strip().strip("'\"")
    raise MirrorError("NoKey")


def to_event(row, phase):
    """Row -> Braintrust event. Ids come from run id and slot id only."""
    u, cond = row.get("usage") or {}, row.get("condition") or {}
    scores = {"adopted": float(bool(row["adopted"]))}
    if "wording" in cond and "output_text" in row and row["status"] == "ok" and cond.get("form") != "carried" \
            and row["experiment"] != "E2N" and row["experiment"] != "E4":
        scores["request_carried"] = float(request_carried(row["output_text"], cond["wording"], row.get("marker")))
    metrics = {"prompt_tokens": u.get("input"), "completion_tokens": u.get("output"), "tokens": u.get("total"),
               "latency_ms": row.get("latency_ms")}
    ev = {"id": f"{row['run_id']}:{row['slot_id']}", "output": row["output_text"], "scores": scores,
          "metrics": {k: v for k, v in metrics.items() if v is not None},
          "metadata": {"condition": cond, "experiment": row["experiment"], "topology": row.get("topology"),
                       "round": row["round"], "agent": row["agent"], "trial_id": row["trial_id"],
                       "status": row["status"], "error_class": row.get("error_class"),
                       "model_requested": row.get("model_requested"), "model_returned": row.get("model_returned"),
                       "phase": phase, "marker": row.get("marker"), "slot_id": row["slot_id"], "run_id": row["run_id"]}}
    if "instructions" in row or "input" in row:
        ev["input"] = {k: row[k] for k in ("instructions", "input") if k in row}
    return ev


class Mirror:
    def __init__(self, a, sleep=time.sleep):
        self.runs, self.sleep = Path(a.runs_dir), sleep
        self.sd = Path(a.state_dir or a.runs_dir)
        self.api, self.key, self.retries, self.cap = a.api_url.rstrip("/"), load_key(a.env_file), a.retries, a.backoff_cap
        self.only, self.phase = a.run_id, a.phase
        self.sd.mkdir(parents=True, exist_ok=True)
        self.spath, self.lpath = self.sd / "bt_mirror_state.json", self.sd / "bt_mirror.jsonl"
        self.state = json.loads(self.spath.read_text()) if self.spath.exists() else {}
        self.project = self.state.get("project_id")

    def log(self, **kw):  # class names only, never a message or body
        with open(self.lpath, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), **kw}) + "\n")

    def save(self):
        tmp = self.spath.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=1))
        tmp.replace(self.spath)

    def request(self, path, body):
        req = urllib.request.Request(self.api + path, json.dumps(body).encode(), method="POST", headers={
            "Authorization": f"Bearer {self.key}", "Content-Type": "application/json", "User-Agent": "outbreak-bt-mirror"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read() or b"{}")

    def send(self, path, body):
        """Retry with capped backoff; raises MirrorError(class) after the last attempt."""
        for i in range(self.retries + 1):
            try:
                return self.request(path, body)
            except urllib.error.HTTPError as e:
                cls, retry = f"HTTP{e.code}", e.code in (408, 409, 425, 429) or e.code >= 500
            except Exception as e:
                cls, retry = type(e).__name__, True
            self.log(event="retry" if retry and i < self.retries else "error", path=path, error_class=cls, attempt=i + 1)
            if not retry or i == self.retries:
                raise MirrorError(cls)
            self.sleep(min(self.cap, 2 ** i))

    def project_id(self):
        if not self.project:  # create returns the existing project when the name is taken
            self.project = self.state["project_id"] = self.send("/v1/project", {"name": PROJECT})["id"]
            self.save()
        return self.project

    def experiment_id(self, run_id):
        exps = self.state.setdefault("experiments", {})
        if run_id not in exps:
            exps[run_id] = self.send("/v1/experiment", {"project_id": self.project_id(), "name": run_id})["id"]
            self.save()
        return exps[run_id]

    def runs_to_mirror(self):
        out = []
        for d in sorted(p for p in self.runs.iterdir() if (p / "calls.jsonl").is_file()) if self.runs.is_dir() else []:
            if self.only and d.name not in self.only:
                continue
            try:
                smoke = json.loads((d / "manifest.json").read_text()).get("smoke", False)
            except (OSError, ValueError):
                smoke = False
            if self.phase in (None, phase_of(d.name, smoke)):
                out.append((d, phase_of(d.name, smoke)))
        return out

    def pass_once(self):
        """One pass over every selected run. Returns the number of rows sent; failures are logged and skipped."""
        sent = 0
        for d, phase in self.runs_to_mirror():
            rid, off = d.name, self.state.setdefault("offsets", {}).get(d.name, 0)
            try:
                with open(d / "calls.jsonl", "rb") as f:
                    f.seek(0, 2)
                    if f.tell() < off:  # file shrank (a resume trimmed a torn line): re-read, ids make it safe
                        off = 0
                    f.seek(off)
                    data = f.read()
                data = data[:data.rfind(b"\n") + 1]  # complete lines only
                batch, end = [], off
                for line in data.splitlines(keepends=True):
                    end += len(line)
                    if line.strip():
                        try:
                            batch.append(to_event(json.loads(line), phase))
                        except (ValueError, KeyError, TypeError) as e:
                            self.log(event="skip_row", run_id=rid, error_class=type(e).__name__)
                    if len(batch) >= BATCH:
                        self.flush(rid, batch, end)
                        sent, batch = sent + len(batch), []
                if batch or end != off:
                    self.flush(rid, batch, end)
                    sent += len(batch)
            except MirrorError:
                continue  # already logged; the offset stays, so the next pass retries
            except Exception as e:
                self.log(event="run_error", run_id=rid, error_class=type(e).__name__)
        return sent

    def flush(self, rid, batch, end):
        if batch:
            self.send(f"/v1/experiment/{self.experiment_id(rid)}/insert", {"events": batch})
        self.state["offsets"][rid] = end
        self.save()


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--runs-dir", required=True)
    p.add_argument("--env-file", required=True)
    p.add_argument("--run-id", nargs="+")
    p.add_argument("--follow", type=float, metavar="SECONDS")
    p.add_argument("--phase", choices=["planning", "study"])
    p.add_argument("--state-dir", help="where the state file and error log go (default: the runs dir)")
    p.add_argument("--api-url", default=API)
    p.add_argument("--retries", type=int, default=5)
    p.add_argument("--backoff-cap", type=float, default=60)
    return p.parse_args(argv)


def main(argv=None, sleep=time.sleep):
    a = parse_args(argv)
    try:
        m = Mirror(a, sleep)
    except (MirrorError, OSError, ValueError) as e:
        print(f"refused: {type(e).__name__}", file=sys.stderr)
        return 2
    if not a.follow:
        n = m.pass_once()
        print(f"sent={n}")
        return 0
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt))
    try:
        while True:
            try:
                m.pass_once()
            except Exception as e:  # never exit on a network error
                m.log(event="pass_error", error_class=type(e).__name__)
            sleep(a.follow)
    except KeyboardInterrupt:
        pass
    try:
        print(f"final pass sent={m.pass_once()}")
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
