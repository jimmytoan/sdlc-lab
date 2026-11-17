"""Paths, models, prices and limits. Prices are USD per million tokens (Anthropic price list, 2026-11)."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPECS = ROOT / "specs"            # visible to coding agents (copied into the sandbox)
HOLDOUT = ROOT / "holdout"        # NEVER mounted into a coder sandbox
REFERENCE = ROOT / "reference"    # NEVER mounted into a coder sandbox
RESULTS = ROOT / "results"
RAW = RESULTS / "raw"
RUNS = RESULTS / "runs.jsonl"
SPEND = RESULTS / "spend.jsonl"   # one line per model call; the spend cap reads this
RUNTIME = ROOT / ".runtime"
IMAGE = "dfl-sandbox"
CANARY = "DFL-HOLDOUT-CANARY-7c1e4b9a2f6d"

SPEC_NAMES = ["splitr", "bookings", "sessionize"]
ENTRY = {"splitr": "splitr.py", "bookings": "server.py", "sessionize": "job.py"}
DESIGNS = ["oneshot", "loop", "pce"]   # "ask" is run separately, on the brief level only
MAX_QUESTIONS = 10
ORACLE_MODEL = "claude-haiku-5-5"       # plays the spec owner in the ask design; fixed so coder tiers stay comparable

# input, output, cache read, cache write (5 min) per MTok
PRICES = {
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
    "claude-sonnet-5-5": (2.00, 10.00, 0.10, 2.50),
    "claude-opus-5-5": (4.00, 20.00, 0.20, 5.00),
}
HAIKU_LONG_PROMPT = 100_000       # above this Haiku 5.5 bills at 5x
MODEL_ALIASES = {"haiku": "claude-haiku-5-5", "sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5"}

EFFORT = "medium"                 # same for every model, so tiers are comparable
MAX_TOKENS = 32000
MAX_TURNS = {"coder": 40, "evaluator": 25, "fix": 20}
EVAL_ROUNDS = 2                   # planner/coder/evaluator: at most this many evaluate-then-fix rounds
RUN_COST_CAP = {"claude-haiku-5-5": 0.60, "claude-sonnet-5-5": 4.00, "claude-opus-5-5": 8.00}
CMD_TIMEOUT_S = 90
TOOL_OUTPUT_LIMIT = 8000


def cost_usd(model, usage):
    """usage: dict with input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens."""
    pin, pout, pread, pwrite = PRICES[model]
    i, o = usage.get("input_tokens", 0), usage.get("output_tokens", 0)
    r, w = usage.get("cache_read_input_tokens", 0) or 0, usage.get("cache_creation_input_tokens", 0) or 0
    mult = 5 if model == "claude-haiku-5-5" and (i + r + w) > HAIKU_LONG_PROMPT else 1
    return mult * (i * pin + o * pout + r * pread + w * pwrite) / 1e6
