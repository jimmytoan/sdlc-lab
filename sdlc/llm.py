"""Model access for sdlc-lab: Anthropic Messages API and OpenAI-compatible chat completions (OpenAI, Gemini).

Every backend speaks the dark factory agent loop's format (Anthropic-style content blocks), so factory.agent.agent_loop
works unchanged. Every call is appended to results/spend.jsonl. The spend cap is hard: before a call is made the ledger
is checked, and a call that would start above the cap raises CapReached.
"""
import json
import os
import threading
import time

from dotenv import load_dotenv

from . import paths

load_dotenv(paths.ROOT / ".env")
SPEND = paths.RESULTS / "spend.jsonl"

# USD per million tokens: input, output, cache read, cache write. Anthropic and OpenAI list prices from the series
# handoff (checked November 2026). OpenAI cached-input price is not in that list, so cached tokens are charged at the
# full input price (over-estimates, safe for the cap). Gemini runs on the free tier; it is charged at list price anyway
# so that cost comparisons across families are like for like, and the cap stays conservative.
PRICES = {
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
    "claude-sonnet-5-5": (2.00, 10.00, 0.10, 2.50),
    "claude-opus-5-5": (4.00, 20.00, 0.20, 5.00),
    "gpt-6.1-terra": (1.00, 5.00, 1.00, 0.0),
    "gpt-6-luna": (0.10, 0.50, 0.10, 0.0),
    "gemini-3.5-flash-lite": (0.30, 2.50, 0.30, 0.0),
}
FREE_TIER = {"gemini-3.5-flash-lite"}
ALIASES = {"haiku": "claude-haiku-5-5", "sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5",
           "terra": "gpt-6.1-terra", "luna": "gpt-6-luna", "gemini": "gemini-3.5-flash-lite"}
FAMILY = {"claude": "anthropic", "gpt": "openai", "gemini": "google"}
HAIKU_LONG_PROMPT = 100_000
EFFORT = "medium"
MAX_TOKENS = 32000


class CapReached(RuntimeError):
    pass


def family(model):
    return FAMILY.get(model.split("-")[0], model)


def cost_usd(model, usage):
    pin, pout, pread, pwrite = PRICES[model]
    i, o = usage.get("input_tokens", 0) or 0, usage.get("output_tokens", 0) or 0
    r, w = usage.get("cache_read_input_tokens", 0) or 0, usage.get("cache_creation_input_tokens", 0) or 0
    mult = 5 if model == "claude-haiku-5-5" and (i + r + w) > HAIKU_LONG_PROMPT else 1
    return mult * (i * pin + o * pout + r * pread + w * pwrite) / 1e6


_lock = threading.Lock()


def spent(tags=None):
    """Total list-price spend in the ledger, optionally only for the given tags (a str or a collection)."""
    if not SPEND.exists():
        return 0.0
    if isinstance(tags, str):
        tags = {tags}
    total = 0.0
    for line in open(SPEND):
        rec = json.loads(line)
        if tags is None or rec["tag"] in tags:
            total += rec["cost_usd"]
    return total


class Budget:
    """A hard cap over a group of tags. check() before every model call."""

    def __init__(self, cap, tags):
        self.cap, self.tags = cap, set(tags)

    def check(self):
        s = spent(self.tags)
        if s >= self.cap:
            raise CapReached(f"spend ${s:.4f} reached the hard cap ${self.cap:.2f} for tags {sorted(self.tags)}")
        return s


class Meter:
    """Usage for one run, per stage. Logs every call to the ledger. Compatible with factory.agent.agent_loop."""

    def __init__(self, tag, run_id, budget=None):
        self.tag, self.run_id, self.budget, self.stages = tag, run_id, budget, {}

    def add(self, stage, model, usage):
        cost = cost_usd(model, usage) if model in PRICES else 0.0
        s = self.stages.setdefault(stage, {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
                                           "cache_creation_input_tokens": 0, "cost_usd": 0.0})
        s["calls"] += 1
        for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
            s[k] += usage.get(k, 0) or 0
        s["cost_usd"] += cost
        if model in PRICES:
            SPEND.parent.mkdir(parents=True, exist_ok=True)
            with _lock, open(SPEND, "a") as f:
                f.write(json.dumps({"ts": time.time(), "tag": self.tag, "run_id": self.run_id, "stage": stage, "model": model,
                                    "usage": usage, "cost_usd": cost, "free_tier": model in FREE_TIER}) + "\n")

    @property
    def cost(self):
        return sum(s["cost_usd"] for s in self.stages.values())

    @property
    def calls(self):
        return sum(s["calls"] for s in self.stages.values())


class AnthropicBackend:
    def __init__(self, model, budget=None):
        import anthropic
        self.model, self.budget = model, budget
        self.client = anthropic.Anthropic(max_retries=6, timeout=600)

    def complete(self, system, messages, tools):
        if self.budget:
            self.budget.check()
        kwargs = dict(model=self.model, max_tokens=MAX_TOKENS, system=system, messages=messages,
                      output_config={"effort": EFFORT}, cache_control={"type": "ephemeral"})
        if tools:
            kwargs["tools"] = tools
        with self.client.messages.stream(**kwargs) as stream:
            msg = stream.get_final_message()
        usage = {k: getattr(msg.usage, k, 0) or 0 for k in
                 ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")}
        blocks = [b.model_dump(mode="json") for b in msg.content]
        return blocks, msg.stop_reason, usage, msg.content


def _block(b):
    return b if isinstance(b, dict) else b.model_dump(mode="json")


def to_openai_messages(system, messages):
    """Anthropic-style history (as factory.agent builds it) to chat-completions messages."""
    out = [{"role": "system", "content": system}]
    for m in messages:
        c = m["content"]
        if isinstance(c, str):
            out.append({"role": m["role"], "content": c})
            continue
        blocks = [_block(b) for b in c]
        if m["role"] == "assistant":
            text = "\n".join(b["text"] for b in blocks if b["type"] == "text")
            calls = [{"id": b["id"], "type": "function",
                      "function": {"name": b["name"], "arguments": json.dumps(b["input"])}} for b in blocks if b["type"] == "tool_use"]
            msg = {"role": "assistant", "content": text or None}
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
        else:
            for b in blocks:
                if b["type"] == "tool_result":
                    content = b["content"] if isinstance(b["content"], str) else json.dumps(b["content"])
                    out.append({"role": "tool", "tool_call_id": b["tool_use_id"], "content": content})
                elif b["type"] == "text":
                    out.append({"role": "user", "content": b["text"]})
    return out


class OpenAICompatBackend:
    """OpenAI chat completions; Gemini through its OpenAI-compatible endpoint. Gemini calls are spaced 4.5 s apart."""
    _last_call = {}

    def __init__(self, model, budget=None):
        from openai import OpenAI
        self.model, self.budget = model, budget
        if model.startswith("gemini"):
            self.client = OpenAI(api_key=os.environ["GEMINI_API_KEY"], max_retries=6, timeout=600,
                                 base_url="https://generativelanguage.googleapis.com/v1beta/openai/")
            self.spacing = 4.5
        else:
            self.client = OpenAI(max_retries=6, timeout=600)
            self.spacing = 0.0

    def complete(self, system, messages, tools):
        if self.budget:
            self.budget.check()
        wait = self._last_call.get(self.model, 0) + self.spacing - time.time()
        if wait > 0:
            time.sleep(wait)
        kwargs = dict(model=self.model, messages=to_openai_messages(system, messages), reasoning_effort=EFFORT,
                      max_completion_tokens=MAX_TOKENS)
        if tools:
            kwargs["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                                 "parameters": t["input_schema"]}} for t in tools]
        resp = self.client.chat.completions.create(**kwargs)
        OpenAICompatBackend._last_call[self.model] = time.time()
        choice = resp.choices[0]
        blocks = []
        if choice.message.content:
            blocks.append({"type": "text", "text": choice.message.content})
        for tc in choice.message.tool_calls or []:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {"_unparseable_arguments": tc.function.arguments}
            blocks.append({"type": "tool_use", "id": tc.id, "name": tc.function.name, "input": args})
        u = resp.usage
        cached = getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", 0) or 0
        usage = {"input_tokens": (u.prompt_tokens or 0) - cached, "output_tokens": u.completion_tokens or 0,
                 "cache_read_input_tokens": cached, "cache_creation_input_tokens": 0}
        stop = {"tool_calls": "tool_use", "stop": "end_turn", "length": "max_tokens",
                "content_filter": "refusal"}.get(choice.finish_reason, choice.finish_reason)
        if blocks and any(b["type"] == "tool_use" for b in blocks):
            stop = "tool_use"
        return blocks, stop, usage, blocks


def backend_for(model, budget=None):
    if os.environ.get("SDLC_OFFLINE"):
        raise RuntimeError("offline mode: no model calls allowed")
    model = ALIASES.get(model, model)
    if model.startswith("claude"):
        return AnthropicBackend(model, budget)
    return OpenAICompatBackend(model, budget)
