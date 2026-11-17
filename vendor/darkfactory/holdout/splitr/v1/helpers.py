"""Test helper: runs the CLI as a black box."""
import os
import subprocess
import sys
import tempfile

APP_DIR = os.environ.get("APP_DIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run(ledger_text, *args, raw=None):
    """Write the ledger to a temp file, run `python splitr.py <args with {f} replaced>`.

    Returns (exit_code, stdout, stderr)."""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "ledger.csv")
        with open(path, "wb") as f:
            f.write(raw if raw is not None else ledger_text.encode("utf-8"))
        argv = [a.replace("{f}", path) for a in args]
        p = subprocess.run([sys.executable, os.path.join(APP_DIR, "splitr.py"), *argv],
                           capture_output=True, text=True, timeout=20, cwd=APP_DIR)
        return p.returncode, p.stdout, p.stderr.replace(path, "{f}")


def lines(s):
    return [x for x in s.strip().split("\n") if x] if s.strip() else []
