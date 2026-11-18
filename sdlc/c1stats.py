"""Significance for C1: each condition against 'none', same model. Statistic = mean over specs of the difference in
mean held-out pass rate (runs within a spec). p = two-sided permutation test that shuffles condition labels within
each spec (10,000 shuffles). Also a bootstrap 95% CI of the same statistic (resampling runs within spec and condition)."""
import json, random, sys
from . import paths

def stat(data):
    return sum(sum(a) / len(a) - sum(b) / len(b) for a, b in data.values()) / len(data)

def main(tag="c1-main", key="rate", base="none"):
    rows = [json.loads(l) for l in open(paths.RESULTS / "c1" / "runs.jsonl")]
    rows = [r for r in rows if r.get("tag") == tag and not r.get("error")]
    def val(r):
        h = r["grade"]["holdout"]
        if key == "rate":
            return h["rate"]
        c = h["by_class"][key]
        return c["passed"] / c["total"]
    rng = random.Random(20261119)
    print(f"| model | condition vs {base} | diff (points) | 95% CI | p |\n|---|---|---|---|---|")
    for model in sorted({r["model"] for r in rows}):
        conds = sorted({r["condition"] for r in rows if r["model"] == model} - {base})
        for c in conds:
            data = {}
            for spec in sorted({r["spec"] for r in rows}):
                a = [val(r) for r in rows if r["model"] == model and r["spec"] == spec and r["condition"] == c]
                b = [val(r) for r in rows if r["model"] == model and r["spec"] == spec and r["condition"] == base]
                if a and b and len(data) >= 0:
                    data[spec] = (a, b)
            obs = stat(data)
            n = 10000; hits = 0
            for _ in range(n):
                perm = {}
                for s, (a, b) in data.items():
                    pool = a + b; rng.shuffle(pool); perm[s] = (pool[:len(a)], pool[len(a):])
                hits += abs(stat(perm)) >= abs(obs) - 1e-12
            boots = sorted(stat({s: ([rng.choice(a) for _ in a], [rng.choice(b) for _ in b]) for s, (a, b) in data.items()})
                           for _ in range(5000))
            print(f"| {model} | {c} | {obs*100:+.1f} | {boots[125]*100:+.1f} to {boots[4874]*100:+.1f} | {hits/n:.3f} |")

if __name__ == "__main__":
    main(*sys.argv[1:])
