"""Maintainability metrics for one Python file: ruff smells, pylint messages, radon complexity and MI, duplication.

Tool versions are pinned in pyproject.toml (ruff 0.17.3, pylint 4.1.3, radon 6.0.1). Every tool runs with an explicit
rule set and no project config, so results do not depend on anything else in the tree.
"""
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

from radon.complexity import cc_visit
from radon.metrics import mi_visit
from radon.raw import analyze as raw_analyze

from .dup import duplication

# Maintainability-oriented ruff families. Pure layout/style families (E, W, I, D, N, Q, COM, UP) are left out on purpose:
# they measure formatting taste, not how hard the code is to change.
RUFF_SELECT = ["F", "B", "C90", "PLR", "PLW", "SIM", "RET", "ERA", "PIE", "BLE", "ARG", "PERF", "C4", "TRY"]
RUFF_IGNORE = ["TRY003"]  # long message passed to an exception: a style preference
# pylint: refactor (R) and warning (W) categories; conventions (C) are naming and docstrings, info (I) is noise.
# duplicate-code needs several files and every product here is one file.
PYLINT_ARGS = ["--disable=C,I,duplicate-code", "--score=n", "--output-format=json2", "--persistent=n", "--rcfile=/dev/null"]
BIN = Path(sys.executable).parent


def ruff(path):
    cmd = [str(BIN / "ruff"), "check", "--isolated", "--no-cache", "--output-format", "json",
           "--select", ",".join(RUFF_SELECT), "--ignore", ",".join(RUFF_IGNORE), str(path)]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode not in (0, 1):
        raise RuntimeError(p.stderr)
    return Counter(f["code"] for f in json.loads(p.stdout))


def pylint(path):
    p = subprocess.run([str(BIN / "pylint"), *PYLINT_ARGS, str(path)], capture_output=True, text=True)
    if p.returncode & 1 or p.returncode & 32:   # fatal or usage error
        raise RuntimeError(p.stderr or p.stdout)
    msgs = json.loads(p.stdout)["messages"]
    return Counter(m["symbol"] for m in msgs if m["type"] in ("refactor", "warning"))


def complexity(source):
    blocks, todo = [], list(cc_visit(source))
    while todo:   # flatten classes into their methods (a class's own score double counts them); closures stay in their parent
        b = todo.pop()
        if hasattr(b, "methods"):
            todo.extend(b.methods)
        else:
            blocks.append(b)
    cc = [b.complexity for b in blocks]
    return {"functions": len(cc), "cc_mean": round(sum(cc) / len(cc), 2) if cc else 0.0, "cc_max": max(cc, default=0),
            "cc_over_10": sum(1 for c in cc if c > 10), "mi": round(mi_visit(source, multi=True), 2)}


def family(code):
    """Longest selected prefix: C901 -> C90, C416 -> C4, PLR2004 -> PLR."""
    return max((f for f in RUFF_SELECT if code.startswith(f)), key=len, default=code)


def analyze(path):
    source = Path(path).read_text()
    raw = raw_analyze(source)
    r, pl = ruff(path), pylint(path)
    kloc = raw.sloc / 1000
    out = {"sloc": raw.sloc, "loc": raw.loc, "comments": raw.comments,
           "ruff_total": sum(r.values()), "ruff": dict(r), "pylint_total": sum(pl.values()), "pylint": dict(pl),
           **complexity(source), **duplication(source)}
    out["ruff_per_kloc"] = round(out["ruff_total"] / kloc, 2) if kloc else 0.0
    out["pylint_per_kloc"] = round(out["pylint_total"] / kloc, 2) if kloc else 0.0
    fam = Counter()
    for c, n in r.items():
        fam[family(c)] += n
    out["ruff_families"] = dict(fam)
    return out
