"""Hand classification of every agent test that fails on a holdout-clean dark factory build. The rules below were
written after reading each failure (results/c4/falsealarms-detail.jsonl) against the spec text; each rule names the
behaviour, the verdict and the spec wording it rests on. Writes results/c4-falsealarms-report.md.

Verdicts:
  false-alarm   the test pins something the spec explicitly leaves free (message text)
  open          the spec does not settle it; the test pins the reference's choice, the build made another
  ref-deviates  the reference departs from the spec text and the build follows the spec; the test pins the reference
  real-bug      the build departs from the spec text; the held-out suite does not check it
  flaky         failed in the real-fault run but not on a re-run (bookings is a live server)"""
import collections, json, re
from . import paths
from .c4 import load, OUT

R = [
    # spec, regex over "suite_id::test", verdict, behaviour, spec wording
    ("bookings", r"error_response_structure_and_content_type", "false-alarm", "error message must equal the code",
     'error body: "message": "<any human readable text>"'),
    ("splitr", r"gemini.*::test_usage_errors$", "false-alarm", "usage-error message must contain 'usage error'",
     "usage error: stderr 'any message'"),
    ("splitr", r"spelling|oracle|random_ledgers|range_inclusive_and_only_considered|filter_names_settlement|mixed_ledger",
     "ref-deviates", "name spelling when --since/--until filters rows out",
     "'shown with the spelling of their first appearance in the file'; the reference uses the first appearance among "
     "the considered rows"),
    ("splitr", r"usage_errors.*\[args1[013]\]", "real-bug", "--help accepted (exit 0) instead of a usage error",
     "'unknown option' is a usage error, exit 64; the spec defines no --help (argparse default)"),
    ("splitr", r"participants", "open", "'A:1:2' in participants (name 'A:1' with weight 2, or a bad weight)",
     "'name:weight, where weight is a positive integer'; the spec does not say a name cannot contain ':'"),
    ("splitr", r"header_case|missing_header_columns\[Date|first_required_column_only\[DATE|missing_headers\[Date|missing_header_priority\[columns6",
     "open", "header column names in another case ('Date', 'DATE') accepted",
     "'must contain the columns `date`, ...'; the spec does not say whether header names are case sensitive"),
    ("splitr", r"no_header|empty_file|completely_empty|missing_headers\[(-date|#)|missing_header_priority\[columns0|precedes_row_errors\[-date",
     "real-bug", "empty or comment-only file accepted as a valid empty ledger",
     "'The first CSV row is the header and must contain the columns ...'; only 'a ledger with a valid header and no expenses is valid'"),
    ("splitr", r"trim|whitespace|valid_amounts|good_amount|csv_bom", "open",
     "spaces around a date or amount field (' 7.00 ') accepted by the reference",
     "'Names are trimmed'; the spec says nothing about trimming date or amount, whose formats are given exactly"),
    ("sessionize", r"decimal_amounts|amount_validation", "open", "spaces around a purchase amount (' 1.20 ') accepted by the reference",
     "amount is 'a string holding a non-negative decimal with at most two fraction digits'; trimming is stated for event_id and user only"),
    ("sessionize", r"timestamp|bad_ts|invalid_timestamps|bad_timestamp", "open",
     "ISO 8601 variants: '+0700' offset, lower-case 't'/'z', surrounding spaces",
     "'ISO 8601 date and time with seconds and a UTC offset'; the examples use '+07:00', the variants are not ruled in or out"),
    ("sessionize", r"type|validation_and_error_precedence", "real-bug", "event type matched ignoring case or spaces ('VIEW', ' click ')",
     "'type: one of view, click, purchase'; trimming is stated for event_id and user only"),
    ("bookings", r"from_to|list_window_from_equals_to|list_filters_and_order", "real-bug",
     "list with from == to returns nothing instead of the bookings spanning that instant",
     "'a booking is included if its end is after from and its start is before to'"),
    ("bookings", r"idempotency|empty_idempotency_key", "real-bug", "empty Idempotency-Key header treated as a key",
     "'Idempotency-Key: <any non-empty string>'"),
    ("bookings", r"routing|not_found_paths", "open", "trailing slash ('/rooms/') served as '/rooms' instead of 404",
     "'A path that does not exist: 404'; the spec does not say whether '/rooms/' is the same path as '/rooms'"),
]
ORDER = ["false-alarm", "open", "ref-deviates", "real-bug", "flaky"]


def classify(spec, key):
    for s, rx, verdict, what, why in R:
        if s == spec and re.search(rx, key):
            return verdict, what, why
    return None


def main():
    rf = {r["suite_id"]: r for r in load(OUT / "realfaults.jsonl")}
    fa = {r["suite_id"]: r for r in load(OUT / "falsealarms.jsonl")}
    scores = {r["suite_id"]: r for r in load(OUT / "scores.jsonl") if r["kind"] == "agent" and r.get("tag") == "c4-main"}
    unmatched, behaviours = [], collections.defaultdict(lambda: {"suites": set(), "pairs": set(), "verdict": None, "why": ""})
    per_suite = {}
    for sid, rec in rf.items():
        if sid not in scores or not rec["false_alarms"]:
            continue
        spec = rec["spec"]
        failed_rf = {b for b, st in rec["status"].items() if st != "survived"}
        builds = {}
        for b, tests in fa.get(sid, {"failed": {}})["failed"].items():
            vs = set()
            for t in tests:
                c = classify(spec, f"{sid}::{t.split('::')[1]}")
                if not c:
                    unmatched.append((sid, t)); continue
                vs.add(c[0])
                bh = behaviours[(spec, c[1])]
                bh["verdict"], bh["why"] = c[0], c[2]; bh["suites"].add(sid); bh["pairs"].add((sid, b))
            if vs:
                builds[b] = vs
        clean = {b for b in rf[sid]["status"] if b in {x for x in fa.get(sid, {"failed": {}})["failed"]}} | set(builds)
        for b in failed_rf:
            if b in clean and b not in builds:
                builds[b] = {"flaky"}
        per_suite[sid] = builds
    # A (suite, build) false alarm counts under its worst verdict: real-bug > ref-deviates > open > false-alarm > flaky.
    rank = {"real-bug": 4, "ref-deviates": 3, "open": 2, "false-alarm": 1, "flaky": 0}
    tally = collections.Counter(); by_model = collections.defaultdict(collections.Counter)
    for sid, builds in per_suite.items():
        m = scores[sid]["model"] + (" (black-box)" if "-tests-bb-" in sid else "")
        for b, vs in builds.items():
            v = max(vs, key=rank.get); tally[v] += 1; by_model[m][v] += 1
    clean_pairs = sum(r["correct"] for s, r in rf.items() if s in scores)
    L = ["# C4: why agent suites fail on holdout-clean builds", "",
         "Every agent test that failed on a dark factory build that passes all held-out scenarios, read against the spec "
         "and sorted by hand. Rules and verdicts: `sdlc/fasort.py`; failing tests per build: `results/c4/falsealarms.jsonl`; "
         "assertion text: `results/c4/falsealarms-detail.jsonl`.", "",
         "Verdicts: **false-alarm** = the test pins something the spec explicitly leaves free; **open** = the spec does not "
         "settle it and the test pins the reference's choice; **ref-deviates** = the reference departs from the spec text and "
         "the build follows the spec; **real-bug** = the build departs from the spec text and the held-out suite misses it; "
         "**flaky** = failed in the real-fault run, passed on a re-run.", "",
         f"{len(scores)} agent suites x 11 holdout-clean builds per spec = {clean_pairs} suite-build pairs. "
         f"{sum(tally.values())} of them fail. A failing pair counts once, under its most serious verdict "
         "(real-bug, then ref-deviates, open, false-alarm, flaky).", "",
         "| verdict | failing suite-build pairs | share of failing pairs |", "|---|---|---|"]
    n = sum(tally.values())
    for v in ORDER:
        L.append(f"| {v} | {tally[v]} | {tally[v] / n:.0%} |")
    L += ["", "## By writer", "", "| writer | " + " | ".join(ORDER) + " | total |", "|---|" + "---|" * (len(ORDER) + 1)]
    for m in sorted(by_model):
        c = by_model[m]; L.append(f"| {m} | " + " | ".join(str(c[v]) for v in ORDER) + f" | {sum(c.values())} |")
    L += ["", "## Behaviours", "", "| spec | behaviour | verdict | suites | suite-build pairs | spec wording |", "|---|---|---|---|---|---|"]
    for (spec, what), b in sorted(behaviours.items(), key=lambda kv: (ORDER.index(kv[1]["verdict"]), -len(kv[1]["pairs"]))):
        L.append(f"| {spec} | {what} | {b['verdict']} | {len(b['suites'])} | {len(b['pairs'])} | {b['why']} |")
    flaky = [(s, sorted(b for b, v in bs.items() if v == {"flaky"})) for s, bs in per_suite.items()]
    flaky = [(s, bs) for s, bs in flaky if bs]
    L += ["", "## Flaky", ""] + ([f"- `{s}`: {len(bs)} build(s)" for s, bs in flaky] or ["- none"])
    L += ["", "## Unclassified", ""] + ([f"- `{s}` {t}" for s, t in unmatched] or ["- none"])
    (paths.RESULTS / "c4-falsealarms-report.md").write_text("\n".join(L) + "\n")
    print("\n".join(L))


if __name__ == "__main__":
    main()
