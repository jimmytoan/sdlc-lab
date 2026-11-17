"""C3: passes the tests, hard to maintain? Static metrics over every shipped codebase, compared with the references.

  uv run python -m sdlc.c3            # analyse corpus/*/index.jsonl, write results/c3/metrics.jsonl and results/c3-report.md

No model calls. Deterministic: re-running gives the same numbers for the same tool versions.
"""
import json
import random
import statistics as st
from collections import Counter, defaultdict

from . import paths
from .metrics import analyze

OUT = paths.RESULTS / "c3"
METRICS = ["sloc", "ruff_per_kloc", "pylint_per_kloc", "cc_mean", "cc_max", "cc_over_10", "mi", "dup_ratio"]
LABEL = {"sloc": "SLOC", "ruff_per_kloc": "ruff/KLOC", "pylint_per_kloc": "pylint/KLOC", "cc_mean": "CC mean",
         "cc_max": "CC max", "cc_over_10": "fns CC>10", "mi": "MI", "dup_ratio": "dup %"}


SUPERSEDED = {"c1:c1-pilot", "c1:c1-smoke"}   # first C1 pilot: prompt bug let agents skip the spec phases (PLAN.md); kept on disk, not analysed


def load_index():
    rows = []
    for idx in sorted(paths.CORPUS.glob("*/index.jsonl")):
        rows += [json.loads(x) for x in open(idx) if x.strip()]
    return [r for r in rows if r["source"] not in SUPERSEDED]


def ranks(xs):
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    r = [0.0] * len(xs)
    i = 0
    while i < len(xs):
        j = i
        while j + 1 < len(xs) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            r[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return r


def pearson(a, b):
    ma, mb = st.fmean(a), st.fmean(b)
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    den = (sum((x - ma) ** 2 for x in a) * sum((y - mb) ** 2 for y in b)) ** 0.5
    return num / den if den else float("nan")


def spearman(a, b, perms=5000, seed=7):
    """rho and a two-sided permutation p-value. Returns (nan, nan) when either side is constant."""
    ra, rb = ranks(a), ranks(b)
    rho = pearson(ra, rb)
    if rho != rho:
        return rho, float("nan")
    rnd = random.Random(seed)
    rb2 = rb[:]
    hits = 0
    for _ in range(perms):
        rnd.shuffle(rb2)
        if abs(pearson(ra, rb2)) >= abs(rho) - 1e-12:
            hits += 1
    return rho, (hits + 1) / (perms + 1)


def within_spec_ranks(rows, key):
    """Rank within each spec, scaled to 0..1, so a pooled correlation is not driven by spec differences."""
    out = {}
    by = defaultdict(list)
    for r in rows:
        by[r["spec"]].append(r)
    for group in by.values():
        rk = ranks([g[key] for g in group])
        n = len(group)
        for g, x in zip(group, rk):
            out[g["id"]] = (x - 1) / (n - 1) if n > 1 else 0.5
    return out


def med(xs):
    return st.median(xs) if xs else float("nan")


def agg(m, xs):
    """Median, except duplication: almost every file has none, so the mean is the informative summary."""
    return (st.fmean(xs) if xs else float("nan")) if m == "dup_ratio" else med(xs)


def fmt(m, v):
    if v != v:
        return "-"
    if m == "dup_ratio":
        return f"{100 * v:.1f}%"
    if m in ("sloc", "cc_max", "cc_over_10"):
        return f"{v:.0f}" if float(v).is_integer() else f"{v:.1f}"
    return f"{v:.1f}"


def group_table(rows, keys, title):
    groups = defaultdict(list)
    for r in rows:
        groups[tuple(r[k] for k in keys)].append(r)
    lines = [f"### {title}", "", "Medians per codebase (duplication: mean, and how many files have any clone).", "",
             "| " + " | ".join(keys) + " | n | holdout | " + " | ".join(LABEL[m] for m in METRICS) + " | files with clones |",
             "|" + "---|" * (len(keys) + 3 + len(METRICS))]
    for k in sorted(groups, key=lambda t: tuple(str(x) for x in t)):
        g = groups[k]
        hold = med([x["holdout_rate"] for x in g])
        lines.append("| " + " | ".join(str(x) for x in k) + f" | {len(g)} | {100 * hold:.0f}% | "
                     + " | ".join(fmt(m, agg(m, [x["m"][m] for x in g])) for m in METRICS)
                     + f" | {sum(1 for x in g if x['m']['clones'])} |")
    return lines + [""]


def ratio_table(rows):
    """Per spec and round: agent median divided by the reference value."""
    lines = ["### Agent code against the reference, per spec", "",
             "Agent median (duplication: mean) divided by the reference value; 1.0 = same as the reference. MI: higher is "
             "better. The reference holds both the v1 and the v2 (change request) behaviour behind one flag, so it is compared "
             "with round-1 and round-2 agent code alike; for round 1 it is carrying code the agent did not have to write.", "",
             "| spec | round | agent n | " + " | ".join(LABEL[m] for m in METRICS) + " |", "|" + "---|" * (3 + len(METRICS))]
    refs = {r["spec"]: r for r in rows if r["source"] == "reference"}
    for spec, ref in sorted(refs.items()):
        for rnd in (1, 2):
            ag = [r for r in rows if r["source"] != "reference" and r["spec"] == spec and r["round"] == rnd]
            cells = []
            for m in METRICS:
                a, b = agg(m, [x["m"][m] for x in ag]), ref["m"][m]
                cells.append(f"{a / b:.2f}" if b else f"{fmt(m, a)} vs 0")
            lines.append(f"| {spec} | {rnd} | {len(ag)} | " + " | ".join(cells) + " |")
    lines += ["", "Reference values:", "", "| spec | " + " | ".join(LABEL[m] for m in METRICS) + " |",
              "|" + "---|" * (1 + len(METRICS))]
    for spec, ref in sorted(refs.items()):
        lines.append(f"| {spec} | " + " | ".join(fmt(m, ref["m"][m]) for m in METRICS) + " |")
    return lines + [""]


def corr_section(rows, target, label, note):
    lines = [f"### Do the metrics track {label}?", "", note, "",
             "| metric | pooled rho (within-spec ranks) | p | " + " | ".join(f"{s} rho" for s in sorted({r['spec'] for r in rows})) + " |",
             "|---|---|---|" + "---|" * len({r["spec"] for r in rows})]
    tr = within_spec_ranks(rows, target)
    for m in METRICS:
        mr = within_spec_ranks([{**r, m: r["m"][m]} for r in rows], m)
        ids = [r["id"] for r in rows]
        rho, p = spearman([mr[i] for i in ids], [tr[i] for i in ids])
        per = []
        for s in sorted({r["spec"] for r in rows}):
            g = [r for r in rows if r["spec"] == s]
            rr, _ = spearman([x["m"][m] for x in g], [x[target] for x in g], perms=0)
            per.append("-" if rr != rr else f"{rr:+.2f}")
        lines.append(f"| {LABEL[m]} | {rho:+.2f} | {p:.3f} | " + " | ".join(per) + " |")
    return lines + [f"", f"n = {len(rows)} codebases.", ""]


def top_findings(rows):
    ruff_c, pyl_c = Counter(), Counter()
    for r in rows:
        if r["source"] == "reference":
            continue
        ruff_c.update(r["m"]["ruff"])
        pyl_c.update(r["m"]["pylint"])
    lines = ["### Most frequent findings in agent code (all rounds)", "", "| ruff rule | count | pylint message | count |", "|---|---|---|---|"]
    a, b = ruff_c.most_common(12), pyl_c.most_common(12)
    for i in range(max(len(a), len(b))):
        x = a[i] if i < len(a) else ("", "")
        y = b[i] if i < len(b) else ("", "")
        lines.append(f"| {x[0]} | {x[1]} | {y[0]} | {y[1]} |")
    return lines + [""]


def change_section(rows):
    """Round 2 against its parent: growth and drift after one change request."""
    by_id = {r["id"]: r for r in rows}
    pairs = [(by_id[r["parent"]], r) for r in rows if r.get("parent") in by_id]
    lines = ["### After one change request (round 2 against its own round-1 parent)", "",
             f"{len(pairs)} pairs. Median of (round 2 minus round 1) per codebase. Survival and change scores are in the dark factory report.", "",
             "| spec level | model | n | " + " | ".join(f"Δ {LABEL[m]}" for m in METRICS) + " |", "|" + "---|" * (3 + len(METRICS))]
    groups = defaultdict(list)
    for p, c in pairs:
        groups[(c["spec_level"], c["model"])].append((p, c))
    for k in sorted(groups):
        g = groups[k]
        lines.append(f"| {k[0]} | {k[1]} | {len(g)} | " + " | ".join(fmt(m, med([c['m'][m] - p['m'][m] for p, c in g])) for m in METRICS) + " |")
    lines.append("")
    brief = [(p, c) for p, c in pairs if c["spec_level"] == "brief"]
    rows2 = [{**c, "m": p["m"], "change_rate": c["change_passed"] / c["change_total"], "id": c["id"]} for p, c in brief]
    if rows2:
        lines += corr_section(rows2, "change_rate", "how well a later change goes",
                              "Round-1 metrics of the parent codebase against the share of change scenarios passed in round 2 "
                              "(brief-spec builds only; on the full spec almost everything passes). Survival of old behaviour was "
                              "100% in every pair in the dark factory run, so it cannot be correlated.")
    return lines


def counts_section(rows):
    """Per codebase: how many static findings against how many failed held-out scenarios (round-1 builds)."""
    lines = ["### Static findings against functional failures, per codebase (round 1)", "",
             "Counts per codebase, medians. A finding is one ruff or pylint message from the maintainability rule sets; a "
             "failure is one held-out scenario that failed. They measure different things and are not interchangeable; the "
             "table only shows which one a reviewer would see more of.", "",
             "| spec level | n | ruff findings | pylint findings | failed held-out scenarios | codebases with 0 failures |",
             "|---|---|---|---|---|---|"]
    for lvl in ("full", "brief"):
        g = [r for r in rows if r["round"] == 1 and r.get("spec_level") == lvl]
        if not g:
            continue
        fails = [r["holdout_total"] - r["holdout_passed"] for r in g]
        lines.append(f"| {lvl} | {len(g)} | {fmt('x', med([r['m']['ruff_total'] for r in g]))} | "
                     f"{fmt('x', med([r['m']['pylint_total'] for r in g]))} | {fmt('x', med(fails))} | {sum(1 for f in fails if f == 0)} |")
    ref = [r for r in rows if r["source"] == "reference"]
    lines.append(f"| reference | {len(ref)} | {fmt('x', med([r['m']['ruff_total'] for r in ref]))} | "
                 f"{fmt('x', med([r['m']['pylint_total'] for r in ref]))} | 0 | {len(ref)} |")
    return lines + [""]


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    rows = load_index()
    cache = {}
    if (OUT / "metrics.jsonl").exists():
        cache = {r["id"]: r for r in map(json.loads, open(OUT / "metrics.jsonl"))}
    with open(OUT / "metrics.jsonl", "w") as f:
        for r in rows:
            c = cache.get(r["id"])
            r["m"] = c["m"] if c and c.get("file") == r["file"] else analyze(paths.ROOT / r["file"])
            f.write(json.dumps(r) + "\n")
    print(f"analysed {len(rows)} codebases")
    agent = [r for r in rows if r["source"] != "reference"]
    rep = ["# C3 report: maintainability of shipped agent code", "",
           f"{len(agent)} agent codebases and {len(rows) - len(agent)} reference implementations. Per-file numbers: "
           "`results/c3/metrics.jsonl`. Tools: ruff 0.17.3, pylint 4.1.3, radon 6.0.1, duplication from `sdlc/dup.py` "
           "(50 normalized tokens, 5 lines). Rule sets: `sdlc/metrics.py`.", "",
           "Caveat: the references were written by the dark factory lab session (a Claude model working against the full spec "
           "and the held-out suites), not by a human team. They are a careful baseline, not a human one.", ""]
    for src in sorted({r["source"] for r in agent}):
        sub = [r for r in rows if r["source"] in (src, "reference")]
        rep += [f"## Source: {src}", ""]
        r1 = [r for r in sub if r["round"] == 1]
        rep += group_table([{**r, "spec_level": r["spec_level"] or "-"} for r in r1], ["spec_level", "model", "design"],
                           "Round 1 builds by spec level, model and design (all three specs pooled)")
        rep += group_table([{**r, "spec_level": r["spec_level"] or "-"} for r in r1], ["spec", "spec_level", "design"],
                           "Round 1 builds by spec, level and design (models pooled)")
        rep += ratio_table(sub)
        brief = [r for r in sub if r["round"] == 1 and r.get("spec_level") == "brief"]
        if brief:
            rep += corr_section(brief, "holdout_rate", "holdout pass rate",
                                "Brief-spec round-1 builds only: on the full spec every build passed every held-out scenario, so "
                                "there is no variance to explain. Spearman rho; positive means more of the metric goes with a "
                                "higher pass rate. Pooled rho uses ranks within each spec so spec size does not drive it; "
                                "p is a two-sided permutation test (5,000 shuffles). Uncorrected for 8 metrics.")
        rep += counts_section(sub)
        rep += change_section(sub)
        rep += top_findings(sub)
    (paths.RESULTS / "c3-report.md").write_text("\n".join(rep) + "\n")
    print("wrote results/c3-report.md")


if __name__ == "__main__":
    main()
