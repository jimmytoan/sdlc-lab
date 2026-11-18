"""Lists which agent tests fail on the holdout-clean dark factory builds (the 'fails on holdout-clean' column of the
C4 real-fault table), so each can be read by hand as a false alarm or a real bug. Writes results/c4/falsealarms.jsonl."""
import json, shutil, subprocess, sys, uuid
from . import paths, mutate
from .c4 import load, RUNS, SCORES, OUT, realfault_set, df

OUTF = OUT / "falsealarms.jsonl"
RUNNER = r"""
import json, os, re, shutil, subprocess, sys, tempfile
from concurrent.futures import ThreadPoolExecutor
entry, ids = sys.argv[1], sys.argv[2].split(",")
deselect = json.load(open("/cfg/deselect.json"))
def one(mid):
    d = tempfile.mkdtemp(dir="/tmp"); app = os.path.join(d, "app"); shutil.copytree("/app", app)
    shutil.copy(f"/mutants/{mid}.py", os.path.join(app, entry))
    cmd = [sys.executable, "-m", "pytest", "/suite", "-q", "-rf", "-p", "no:cacheprovider", "--timeout", "30"]
    for t in deselect: cmd += ["--deselect", t]
    p = subprocess.run(cmd, cwd="/suite", env={**os.environ, "APP_DIR": app}, capture_output=True, text=True, timeout=900)
    failed = sorted({m.group(1) for m in re.finditer(r"^FAILED (\S+)", p.stdout, re.M)})
    return mid, failed
with ThreadPoolExecutor(4) as ex:
    json.dump(dict(ex.map(one, ids)), open("/out/result.json", "w"))
"""

def run(spec, files, ids, deselect):
    entry = df.ENTRY[spec]
    work = paths.RUNTIME / "fa" / uuid.uuid4().hex[:10]
    app, suite, out, cfg = work / "app", work / "suite", work / "out", work / "cfg"
    for d in (app, suite, out, cfg): d.mkdir(parents=True)
    shutil.copy(df.config.REFERENCE / spec / "v1" / entry, app / entry)
    for f in files: shutil.copy(f, suite / f.name)
    (cfg / "deselect.json").write_text(json.dumps(list(deselect))); (cfg / "runner.py").write_text(RUNNER)
    for p in [work, *work.rglob("*")]: p.chmod(0o777 if p.is_dir() else 0o666)
    subprocess.run(["docker", "run", "--rm", "--network", "none", "--memory", "4g", "--cpus", "4", "-v", f"{app}:/app:ro",
                    "-v", f"{suite}:/suite:ro", "-v", f"{paths.RUNTIME / 'realfaults' / spec}:/mutants:ro", "-v", f"{cfg}:/cfg:ro",
                    "-v", f"{out}:/out", mutate.IMAGE, "python", "/cfg/runner.py", entry, ",".join(ids)],
                   capture_output=True, text=True, check=True, timeout=7200)
    res = json.loads((out / "result.json").read_text()); shutil.rmtree(work, ignore_errors=True)
    return res

def main(tag="c4-main"):
    done = {r["suite_id"] for r in load(OUTF)} if OUTF.exists() else set()
    rf = {r["suite_id"]: r for r in load(OUT / "realfaults.jsonl")}
    for s in load(SCORES):
        if s["kind"] != "agent" or s.get("tag") != tag or s["suite_id"] in done: continue
        if not rf.get(s["suite_id"], {}).get("false_alarms"): continue
        r = next(x for x in load(RUNS) if x["run_id"] == s["suite_id"])
        files = [df.config.SPECS / s["spec"] / "public" / "helpers.py", paths.ROOT / r["suite_path"]]
        ids = [c["id"] for c in realfault_set(s["spec"])[1]]
        res = run(s["spec"], files, ids, s["invalid_tests"])
        with open(OUTF, "a") as f:
            f.write(json.dumps({"suite_id": s["suite_id"], "spec": s["spec"], "failed": res}) + "\n")
        print(s["suite_id"], sum(1 for v in res.values() if v), flush=True)

if __name__ == "__main__":
    main(*sys.argv[1:])
