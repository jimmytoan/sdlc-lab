"""Mutation testing for black-box suites.

Mutants come from mutmut 3.8.1 (its operators): `mutmut run` generates them, `mutmut apply <name>` writes one mutant into
the source file, and we keep that file. mutmut's own runner cannot be used: it maps tests to mutants in-process, and these
suites run the product as a subprocess, so mutmut finds "no test covers any mutant". Our runner instead copies each mutant
over the product and runs the suite against it (stop at first failure). A mutant is killed if the suite fails or times out.

  uv run python -m sdlc.mutate generate           # once; writes .runtime/mutants/<spec>/ and results/c4/mutants.json
"""
import hashlib
import json
import random
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

from . import df, paths

IMAGE = "sdlc-mut"
MUT_DIR = paths.RUNTIME / "mutants"
MANIFEST = paths.RESULTS / "c4" / "mutants.json"
PER_MUTANT_TIMEOUT = 120
WORKERS = 4

GEN_SCRIPT = r"""
set -e
cd /work
cp pristine.py "$ENTRY"
mutmut run > /tmp/run.log 2>&1 || true
mutmut results --all true 2>/dev/null | sed -n 's/^ *\([^ ]*\): .*/\1/p' > names.txt
mkdir -p out
i=0
while read -r name; do
  cp pristine.py "$ENTRY"
  if mutmut apply "$name" >/dev/null 2>&1; then
    i=$((i+1)); cp "$ENTRY" "out/$(printf %05d $i).py"; printf '%05d\t%s\n' "$i" "$name" >> out/index.tsv
  fi
done < names.txt
cp pristine.py "$ENTRY"
"""


def generate(spec):
    """Generate every mutmut mutant of the v1 reference for one spec. Returns the manifest entry."""
    entry = df.ENTRY[spec]
    work = paths.RUNTIME / "mutgen" / spec
    if work.exists():
        shutil.rmtree(work)
    (work / "tests").mkdir(parents=True)
    src = df.config.REFERENCE / spec / "v1" / entry
    shutil.copy(src, work / "pristine.py")
    (work / "tests" / "test_dummy.py").write_text("def test_dummy():\n    assert True\n")
    (work / "pyproject.toml").write_text(f'[tool.mutmut]\nsource_paths = ["{entry}"]\n'
                                         'pytest_add_cli_args_test_selection = ["tests/"]\n')
    for p in [work, *work.rglob("*")]:
        p.chmod(0o777 if p.is_dir() else 0o666)
    subprocess.run(["docker", "run", "--rm", "--network", "none", "-e", f"ENTRY={entry}", "-v", f"{work}:/work",
                    IMAGE, "bash", "-c", GEN_SCRIPT], check=True, capture_output=True, text=True, timeout=3600)
    pristine = (work / "pristine.py").read_text()
    out = MUT_DIR / spec
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    seen, items = {}, []
    for line in (work / "out" / "index.tsv").read_text().splitlines():
        num, name = line.split("\t")
        text = (work / "out" / f"{num}.py").read_text()
        if text == pristine:
            continue                     # no-op mutant
        h = hashlib.sha256(text.encode()).hexdigest()[:16]
        if h in seen:
            continue                     # duplicate of an earlier mutant
        seen[h] = name
        mid = f"m{len(items) + 1:04d}"
        (out / f"{mid}.py").write_text(text)
        items.append({"id": mid, "mutmut_name": name, "sha": h})
    return {"spec": spec, "source_sha": hashlib.sha256(pristine.encode()).hexdigest()[:16], "mutmut": "3.8.1",
            "generated": len((work / "out" / "index.tsv").read_text().splitlines()), "kept": len(items), "mutants": items}


def load_manifest():
    return json.loads(MANIFEST.read_text())


def sample(spec, n, seed=20261117):
    """A fixed random sample of mutant ids for one spec (the same sample for every suite)."""
    ids = [m["id"] for m in load_manifest()[spec]["mutants"]]
    if n is None or n >= len(ids):
        return ids
    return sorted(random.Random(f"{seed}-{spec}").sample(ids, n))


RUNNER = r"""
import json, os, shutil, subprocess, sys, tempfile
from concurrent.futures import ThreadPoolExecutor
entry, ids, workers, timeout = sys.argv[1], sys.argv[2].split(","), int(sys.argv[3]), int(sys.argv[4])
deselect = json.load(open("/cfg/deselect.json"))
def one(mid):
    d = tempfile.mkdtemp(dir="/tmp")
    app = os.path.join(d, "app")
    shutil.copytree("/app", app)
    if mid != "original":
        shutil.copy(f"/mutants/{mid}.py", os.path.join(app, entry))
    cmd = [sys.executable, "-m", "pytest", "/suite", "-x", "-q", "-p", "no:cacheprovider", "--timeout", "30"]
    for t in deselect:
        cmd += ["--deselect", t]
    try:
        p = subprocess.run(cmd, cwd="/suite", env={**os.environ, "APP_DIR": app}, capture_output=True, text=True, timeout=timeout)
        status = "survived" if p.returncode == 0 else ("killed" if p.returncode == 1 else f"error{p.returncode}")
        tail = p.stdout.strip().splitlines()[-1:] if p.returncode else []
    except subprocess.TimeoutExpired:
        status, tail = "timeout", []
    shutil.rmtree(d, ignore_errors=True)
    return mid, status, tail
with ThreadPoolExecutor(workers) as ex:
    res = {m: {"status": s, "tail": t} for m, s, t in ex.map(one, ids)}
json.dump(res, open("/out/result.json", "w"))
"""


def run_suite_on_mutants(spec, suite_files, mutant_ids, deselect=(), workers=WORKERS, mutant_dir=None):
    """Runs the suite against each mutant (and the original first). Returns {mutant_id: status}.
    killed = suite failed; timeout counts as killed; error<N> = pytest could not run (counted as killed only if the
    original ran clean, which run_mutation checks)."""
    entry = df.ENTRY[spec]
    work = paths.RUNTIME / "mutrun" / uuid.uuid4().hex[:10]
    app, suite, out, cfg = work / "app", work / "suite", work / "out", work / "cfg"
    for d in (app, suite, out, cfg):
        d.mkdir(parents=True)
    shutil.copy(df.config.REFERENCE / spec / "v1" / entry, app / entry)
    for f in suite_files:
        shutil.copy(f, suite / Path(f).name)
    (cfg / "deselect.json").write_text(json.dumps(list(deselect)))
    (cfg / "runner.py").write_text(RUNNER)
    for p in [work, *work.rglob("*")]:
        p.chmod(0o777 if p.is_dir() else 0o666)
    cmd = ["docker", "run", "--rm", "--network", "none", "--memory", "4g", "--cpus", str(workers), "--pids-limit", "1024",
           "-v", f"{app}:/app:ro", "-v", f"{suite}:/suite:ro", "-v", f"{mutant_dir or MUT_DIR / spec}:/mutants:ro", "-v", f"{cfg}:/cfg:ro",
           "-v", f"{out}:/out", IMAGE, "python", "/cfg/runner.py", entry, ",".join(["original", *mutant_ids]),
           str(workers), str(PER_MUTANT_TIMEOUT)]
    subprocess.run(cmd, capture_output=True, text=True, timeout=36000, check=True)
    res = json.loads((out / "result.json").read_text())
    shutil.rmtree(work, ignore_errors=True)
    return res


def score(res, mutant_ids):
    if res["original"]["status"] != "survived":
        return {"valid": False, "original": res["original"]}
    st = {m: res[m]["status"] for m in mutant_ids}
    killed = [m for m, s in st.items() if s != "survived"]
    return {"valid": True, "killed": len(killed), "total": len(mutant_ids), "score": round(len(killed) / len(mutant_ids), 4),
            "timeouts": sum(1 for s in st.values() if s == "timeout"),
            "errors": sum(1 for s in st.values() if s.startswith("error")), "status": st}


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "generate":
        MANIFEST.parent.mkdir(parents=True, exist_ok=True)
        man = {}
        for spec in df.SPEC_NAMES:
            man[spec] = generate(spec)
            print(f"{spec}: {man[spec]['generated']} generated, {man[spec]['kept']} kept after removing no-ops and duplicates")
        MANIFEST.write_text(json.dumps(man, indent=1))


if __name__ == "__main__":
    main()
