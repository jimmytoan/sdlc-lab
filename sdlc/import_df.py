"""Import the shipped product code of the dark factory main run (tag `main`) into corpus/df/.

Reads dark-factory-lab read-only. Copies only the product source (no tests, no transcripts) plus the run metadata
that C3 needs. Writes corpus/df/index.jsonl, one line per codebase, and copies the reference implementations.

  uv run python -m sdlc.import_df
"""
import json
import shutil
from pathlib import Path

from . import paths

DF = Path("/Users/jimmynguyen/git/me/dark-factory-lab")
ENTRY = {"splitr": "splitr.py", "bookings": "server.py", "sessionize": "job.py"}


def main():
    out = paths.CORPUS / "df"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    runs = [json.loads(x) for x in open(DF / "results" / "runs.jsonl") if x.strip()]
    rows = []
    for r in runs:
        if r["tag"] != "main" or r["error"]:
            continue
        src = DF / "results" / "raw" / r["run_id"] / "workspace" / ENTRY[r["spec"]]
        if not src.exists():
            print("missing", src)
            continue
        dst = out / r["run_id"] / ENTRY[r["spec"]]
        dst.parent.mkdir(parents=True)
        shutil.copy(src, dst)
        g = r["grade"]
        row = {"id": r["run_id"], "source": "darkfactory", "spec": r["spec"], "spec_level": r.get("spec_level", "full"),
               "design": r["design"], "model": r["model"], "trial": r["trial"], "round": r["round"],
               "parent": r.get("parent_run_id"), "file": str(dst.relative_to(paths.ROOT)),
               "holdout_passed": g["holdout"]["passed"], "holdout_total": g["holdout"]["total"],
               "holdout_rate": g["holdout"]["rate"], "cost_usd": r["usage"]["cost_usd"]}
        if r["round"] == 1:
            row["by_class"] = g["holdout"]["by_class"]
        else:
            row["change_passed"], row["change_total"] = g["change"]["passed"], g["change"]["total"]
            row["survival"] = {k: r["survival"][k] for k in ("kept", "base")}
        rows.append(row)
    for spec, entry in ENTRY.items():
        # The reference is one file holding v1 and v2 behind a `V2 = False/True` flag; v1 and v2 differ only in that line.
        # So there is one reference per spec, and it carries the change-request code too (it is larger than a v1-only build).
        dst = out / f"reference-{spec}" / entry
        dst.parent.mkdir(parents=True)
        shutil.copy(DF / "reference" / spec / "v1" / entry, dst)
        rows.append({"id": f"reference-{spec}", "source": "reference", "spec": spec, "spec_level": None,
                     "design": "reference", "model": "reference", "trial": None, "round": 1,
                     "file": str(dst.relative_to(paths.ROOT)), "holdout_rate": 1.0})
    with open(out / "index.jsonl", "w") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")
    print(f"imported {len(rows)} codebases into {out}")


if __name__ == "__main__":
    main()
