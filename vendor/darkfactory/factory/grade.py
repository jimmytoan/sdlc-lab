"""Grading: scripts only. Runs pristine suites against a copy of the shipped workspace in a fresh container."""
import json
import shutil
import subprocess
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

from . import config


def run_suite(app_dir, suite_files, deselect=()):
    """suite_files: list of paths copied flat into the suite dir. Returns {passed, total, failed: [names], error}."""
    work = config.RUNTIME / "grade" / uuid.uuid4().hex[:10]
    app, suite, out = work / "app", work / "suite", work / "out"
    shutil.copytree(app_dir, app, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    suite.mkdir(parents=True)
    out.mkdir()
    out.chmod(0o777)
    for f in suite_files:
        shutil.copy(f, suite / Path(f).name)
    cmd = ["docker", "run", "--rm", "--network", "none", "--memory", "1g", "--cpus", "2", "--pids-limit", "256",
           "-v", f"{app}:/app", "-v", f"{suite}:/suite:ro", "-v", f"{out}:/out", "-e", "APP_DIR=/app", "-w", "/suite",
           config.IMAGE, "python", "-m", "pytest", "/suite", "-q", "-p", "no:cacheprovider", "--timeout", "30",
           "--junitxml", "/out/junit.xml"]
    if deselect:
        cmd += ["-k", "not (" + " or ".join(deselect) + ")"]
    error = None
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    except subprocess.TimeoutExpired:
        error = "suite timed out"
    res = {"passed": 0, "total": 0, "failed": [], "names": [], "error": error}
    junit = out / "junit.xml"
    if junit.exists():
        for case in ET.parse(junit).getroot().iter("testcase"):
            if case.find("skipped") is not None:
                continue
            res["total"] += 1
            res["names"].append(case.get("name"))
            if case.find("failure") is None and case.find("error") is None:
                res["passed"] += 1
            else:
                res["failed"].append(case.get("name"))
    else:
        res["error"] = error or "no junit report"
    shutil.rmtree(work, ignore_errors=True)
    return res


_TOTALS = {}


def expected_total(suite_files, deselect=()):
    """Number of scenarios in a suite, counted once with pytest --collect-only (does not need the app)."""
    key = (tuple(str(f) for f in suite_files), tuple(deselect))
    if key not in _TOTALS:
        work = config.RUNTIME / "grade" / uuid.uuid4().hex[:10]
        work.mkdir(parents=True)
        for f in suite_files:
            shutil.copy(f, work / Path(f).name)
        cmd = ["docker", "run", "--rm", "--network", "none", "-v", f"{work}:/suite:ro", "-w", "/suite", config.IMAGE,
               "python", "-m", "pytest", "/suite", "--collect-only", "-q", "-p", "no:cacheprovider"]
        if deselect:
            cmd += ["-k", "not (" + " or ".join(deselect) + ")"]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=300).stdout
        _TOTALS[key] = sum(1 for line in out.splitlines() if "::test_" in line)
        shutil.rmtree(work, ignore_errors=True)
    return _TOTALS[key]


def scenario_class(name, classes):
    """Parametrized scenarios carry their class in the id: test_x[implied:foo]. Others are listed in classes.json."""
    if "[" in name:
        return name[name.index("[") + 1:].split(":")[0]
    return classes[name]


def _finish(res, files, deselect=()):
    total = expected_total(files, deselect)
    if res["total"] != total:      # crashed collection or timeout: count everything missing as failed
        res["error"] = res["error"] or f"collected {res['total']} of {total}"
        res["total"] = total
    res["rate"] = round(res["passed"] / res["total"], 4) if res["total"] else None
    return res


def grade(spec, app_dir, change=False):
    """Round 1: public + holdout. Round 2 adds public_v2, regression (old holdout still valid) and change holdout."""
    pub, hold = config.SPECS / spec / "public", config.HOLDOUT / spec / "v1"
    out = {}
    pub_files = [pub / "helpers.py", pub / "test_public.py"]
    if not change:
        out["public"] = _finish(run_suite(app_dir, pub_files), pub_files)
        hf = [hold / "helpers.py", hold / "test_holdout.py"]
        out["holdout"] = _finish(run_suite(app_dir, hf), hf)
        classes = json.loads((hold / "classes.json").read_text())
        by = {c: {"passed": 0, "total": 0} for c in ("stated", "implied", "arbitrary")}
        for name in out["holdout"].get("names", []):
            b = by[scenario_class(name, classes)]
            b["total"] += 1
            b["passed"] += name not in out["holdout"]["failed"]
        out["holdout"]["by_class"] = by
        return out
    v2 = config.HOLDOUT / spec / "v2"
    invalid = tuple(x for x in (v2 / "invalidated.txt").read_text().split() if x)
    pub2 = pub_files + [config.SPECS / spec / "public_v2" / "test_public_v2.py"]
    out["public"] = _finish(run_suite(app_dir, pub2), pub2)
    rf = [hold / "helpers.py", hold / "test_holdout.py"]
    out["regression"] = _finish(run_suite(app_dir, rf, invalid), rf, invalid)
    cf = [v2 / "helpers.py", v2 / "test_change.py"]
    out["change"] = _finish(run_suite(app_dir, cf), cf)
    p, t = out["regression"]["passed"] + out["change"]["passed"], out["regression"]["total"] + out["change"]["total"]
    out["holdout"] = {"passed": p, "total": t, "rate": round(p / t, 4)}
    return out
