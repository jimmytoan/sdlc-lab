"""Test helper: runs the batch job as a black box in a temp directory."""
import csv
import json
import os
import subprocess
import sys
import tempfile
from types import SimpleNamespace

APP_DIR = os.environ.get("APP_DIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def ev(event_id, user, ts, type="view", **extra):
    """Build one event line. ts like '09:00:00' is expanded to 2026-03-01T09:00:00Z."""
    if "T" not in ts:
        ts = f"2026-03-01T{ts}Z"
    return json.dumps({"event_id": event_id, "user": user, "ts": ts, "type": type, **extra})


def run_job(lines, *extra_args, make_input=True):
    """lines: list of raw text lines for events.jsonl. Returns code, stderr, header, sessions, daily, rejects."""
    with tempfile.TemporaryDirectory() as d:
        inp, out = os.path.join(d, "in"), os.path.join(d, "out")
        os.makedirs(inp)
        if make_input:
            with open(os.path.join(inp, "events.jsonl"), "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + ("\n" if lines else ""))
        p = subprocess.run([sys.executable, os.path.join(APP_DIR, "job.py"), "--in", inp, "--out", out, *extra_args],
                           capture_output=True, text=True, timeout=30, cwd=APP_DIR)
        res = SimpleNamespace(code=p.returncode, stderr=p.stderr, header=None, sessions=None, daily=None, rejects=None,
                              files=sorted(os.listdir(out)) if os.path.isdir(out) else [])
        if "sessions.csv" in res.files:
            with open(os.path.join(out, "sessions.csv"), newline="", encoding="utf-8") as f:
                rows = list(csv.reader(f))
            res.header = rows[0] if rows else []
            res.sessions = [dict(zip(res.header, r)) for r in rows[1:] if r]
        if "daily.json" in res.files:
            with open(os.path.join(out, "daily.json"), encoding="utf-8") as f:
                res.daily = json.load(f)
        if "rejects.jsonl" in res.files:
            with open(os.path.join(out, "rejects.jsonl"), encoding="utf-8") as f:
                res.rejects = [json.loads(x) for x in f.read().split("\n") if x.strip()]
        return res


def pick(sessions, *cols):
    return [tuple(s[c] for c in cols) for s in sessions]
