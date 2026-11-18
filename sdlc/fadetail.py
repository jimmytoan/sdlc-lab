"""For every agent test that fails on a holdout-clean dark factory build (results/c4/falsealarms.jsonl), runs that one
test on the first build it fails on and keeps the assertion text, so the failure can be judged against the spec.
Writes results/c4/falsealarms-detail.jsonl."""
import collections, json, shutil, subprocess, uuid
from . import paths, mutate
from .c4 import load, RUNS, SCORES, OUT, df

OUTF = OUT / "falsealarms-detail.jsonl"
RUNNER = r"""
import json, os, shutil, subprocess, sys, tempfile
from concurrent.futures import ThreadPoolExecutor
entry = sys.argv[1]; jobs = json.load(open("/cfg/jobs.json"))
def one(job):
    test, mid = job
    d = tempfile.mkdtemp(dir="/tmp"); app = os.path.join(d, "app"); shutil.copytree("/app", app)
    shutil.copy(f"/mutants/{mid}.py", os.path.join(app, entry))
    p = subprocess.run([sys.executable, "-m", "pytest", "/suite/" + test, "-q", "-vv", "--tb=short", "-p", "no:cacheprovider",
                        "--timeout", "30"], cwd="/suite", env={**os.environ, "APP_DIR": app, "COLUMNS": "200"},
                       capture_output=True, text=True, timeout=300)
    out = p.stdout
    i = out.find("_____")
    return [test, mid, p.returncode, out[i:][-3000:] if i >= 0 else out[-3000:]]
with ThreadPoolExecutor(4) as ex:
    json.dump(list(ex.map(one, jobs)), open("/out/result.json", "w"))
"""

def main():
    done = {r["suite_id"] for r in load(OUTF)} if OUTF.exists() else set()
    runs = {r["run_id"]: r for r in load(RUNS)}
    for fa in load(OUT / "falsealarms.jsonl"):
        if fa["suite_id"] in done:
            continue
        per = collections.defaultdict(list)
        for b, tests in fa["failed"].items():
            for t in tests:
                per[t].append(b)
        if not per:
            continue
        spec = fa["spec"]; entry = df.ENTRY[spec]
        jobs = [[t, sorted(bs)[0]] for t, bs in sorted(per.items())]
        work = paths.RUNTIME / "fad" / uuid.uuid4().hex[:10]
        app, suite, out, cfg = work / "app", work / "suite", work / "out", work / "cfg"
        for d in (app, suite, out, cfg): d.mkdir(parents=True)
        shutil.copy(df.config.REFERENCE / spec / "v1" / entry, app / entry)
        for f in [df.config.SPECS / spec / "public" / "helpers.py", paths.ROOT / runs[fa["suite_id"]]["suite_path"]]:
            shutil.copy(f, suite / f.name)
        (cfg / "jobs.json").write_text(json.dumps(jobs)); (cfg / "runner.py").write_text(RUNNER)
        for p in [work, *work.rglob("*")]: p.chmod(0o777 if p.is_dir() else 0o666)
        subprocess.run(["docker", "run", "--rm", "--network", "none", "--memory", "4g", "--cpus", "4", "-v", f"{app}:/app:ro",
                        "-v", f"{suite}:/suite:ro", "-v", f"{paths.RUNTIME / 'realfaults' / spec}:/mutants:ro",
                        "-v", f"{cfg}:/cfg:ro", "-v", f"{out}:/out", mutate.IMAGE, "python", "/cfg/runner.py", entry],
                       capture_output=True, text=True, check=True, timeout=3600)
        res = json.loads((out / "result.json").read_text()); shutil.rmtree(work, ignore_errors=True)
        with open(OUTF, "a") as f:
            for test, mid, rc, text in res:
                f.write(json.dumps({"suite_id": fa["suite_id"], "spec": spec, "test": test, "builds_failed": len(per[test]),
                                    "build": mid, "rc": rc, "text": text}) + "\n")
        print(fa["suite_id"], len(jobs), flush=True)

if __name__ == "__main__":
    main()
