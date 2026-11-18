"""C3 follow-up: does a metric track held-out pass rate *within* one C1 method (same spec, design and model)? The pooled
C3 table ranks within spec only, so the spec-writing methods (longer code, higher pass rate) can drive a correlation.
Here both the metric and the pass rate are ranked within each (spec, design, model) group, then a Spearman rho with a
5,000-shuffle permutation p. Writes results/c3-within-method.md."""
import json
from . import c3, paths

def main():
    rows = [json.loads(l) for l in open(paths.RESULTS / "c3" / "metrics.jsonl")]
    rows = [r for r in rows if r["source"] == "c1:c1-main" and r["round"] == 1]
    def wr(get):
        out, groups = {}, {}
        for r in rows:
            groups.setdefault((r["spec"], r["design"], r["model"]), []).append(r)
        for g in groups.values():
            rk = c3.ranks([get(x) for x in g]); n = len(g)
            for x, v in zip(g, rk):
                out[x["id"]] = (v - (n + 1) / 2) / n
        return out
    tgt = wr(lambda x: x["holdout_rate"])
    L = ["# C3: metrics against held-out pass rate within one C1 method", "",
         f"{len(rows)} C1 codebases (tag c1-main, round 1). Ranks within each (spec, design, model) group. Code: `sdlc/c3within.py`.", "",
         "| metric | rho | p |", "|---|---|---|"]
    for m in ["sloc", "ruff_per_kloc", "pylint_per_kloc", "cc_mean", "cc_max", "cc_over_10", "mi", "dup_ratio"]:
        a = wr(lambda x: x["m"][m]); ids = list(a)
        rho, p = c3.spearman([a[i] for i in ids], [tgt[i] for i in ids])
        L.append(f"| {m} | {rho:+.2f} | {p:.3f} |")
    (paths.RESULTS / "c3-within-method.md").write_text("\n".join(L) + "\n"); print("\n".join(L))

if __name__ == "__main__":
    main()
