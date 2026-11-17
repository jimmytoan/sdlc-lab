"""Model access. One real backend (Anthropic Messages API) and a scripted one for the offline self-test."""
import json
import os
import time

from . import config


def _log_spend(tag, run_id, stage, model, usage, cost):
    config.RESULTS.mkdir(exist_ok=True)
    with open(config.SPEND, "a") as f:
        f.write(json.dumps({"ts": time.time(), "tag": tag, "run_id": run_id, "stage": stage, "model": model,
                            "usage": usage, "cost_usd": cost}) + "\n")


def spent(tag=None):
    if not config.SPEND.exists():
        return 0.0
    total = 0.0
    for line in open(config.SPEND):
        rec = json.loads(line)
        if tag is None or rec["tag"] == tag:
            total += rec["cost_usd"]
    return total


class Meter:
    """Accumulates usage for one run, per stage."""

    def __init__(self, tag, run_id):
        self.tag, self.run_id, self.stages = tag, run_id, {}

    def add(self, stage, model, usage):
        cost = config.cost_usd(model, usage) if model in config.PRICES else 0.0
        s = self.stages.setdefault(stage, {"calls": 0, "input_tokens": 0, "output_tokens": 0,
                                           "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "cost_usd": 0.0})
        s["calls"] += 1
        for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
            s[k] += usage.get(k, 0) or 0
        s["cost_usd"] += cost
        if model in config.PRICES:
            _log_spend(self.tag, self.run_id, stage, model, usage, cost)

    @property
    def cost(self):
        return sum(s["cost_usd"] for s in self.stages.values())

    @property
    def calls(self):
        return sum(s["calls"] for s in self.stages.values())


class AnthropicBackend:
    def __init__(self, model):
        import anthropic
        from dotenv import load_dotenv
        load_dotenv(config.ROOT / ".env")
        self.model = model
        self.client = anthropic.Anthropic(max_retries=6, timeout=600)

    def complete(self, system, messages, tools):
        """Returns (content blocks as dicts, stop_reason, usage dict, raw content to append to history)."""
        kwargs = dict(model=self.model, max_tokens=config.MAX_TOKENS, system=system, messages=messages,
                      output_config={"effort": config.EFFORT}, cache_control={"type": "ephemeral"})
        if tools:
            kwargs["tools"] = tools
        with self.client.messages.stream(**kwargs) as stream:
            msg = stream.get_final_message()
        usage = {k: getattr(msg.usage, k, 0) or 0 for k in
                 ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")}
        blocks = [b.model_dump(mode="json") for b in msg.content]
        return blocks, msg.stop_reason, usage, msg.content


class ScriptedBackend:
    """Replays a fixed list of turns. Each turn is a list of ("text", str) or ("tool", name, input)."""
    model = "scripted"

    def __init__(self, turns):
        self.turns = list(turns)

    def complete(self, system, messages, tools):
        turn = self.turns.pop(0) if self.turns else [("text", "done")]
        blocks = []
        for i, item in enumerate(turn):
            if item[0] == "text":
                blocks.append({"type": "text", "text": item[1]})
            else:
                blocks.append({"type": "tool_use", "id": f"toolu_{len(messages)}_{i}", "name": item[1], "input": item[2]})
        stop = "tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn"
        return blocks, stop, {"input_tokens": 0, "output_tokens": 0}, blocks


def backend_for(model):
    if os.environ.get("DFL_OFFLINE"):
        raise RuntimeError("offline mode: no model calls allowed")
    return AnthropicBackend(config.MODEL_ALIASES.get(model, model))
