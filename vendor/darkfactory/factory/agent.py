"""A plain tool loop. The model only ever touches the sandbox through these four tools."""
import json
import re

from . import config

TOOLS = {
    "write_file": {"name": "write_file", "description": "Create or overwrite a text file in the workspace.",
                   "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                                    "required": ["path", "content"]}},
    "read_file": {"name": "read_file", "description": "Read a text file from the workspace.",
                  "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    "list_files": {"name": "list_files", "description": "List all files in the workspace.",
                   "input_schema": {"type": "object", "properties": {}}},
    "run": {"name": "run", "description": "Run a shell command in the workspace root (bash, Python 3.13, pytest; no network). "
                                          f"Killed after {config.CMD_TIMEOUT_S} seconds. Returns exit code and output.",
            "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
}
PYTEST_SUMMARY = re.compile(r"(\d+ (?:passed|failed|error)[^\n]*in [\d.]+s)")


def clip(text, limit=config.TOOL_OUTPUT_LIMIT):
    if len(text) <= limit:
        return text
    half = limit // 2
    return text[:half] + f"\n[... {len(text) - limit} characters cut ...]\n" + text[-half:]


class Transcript:
    def __init__(self, path):
        self.path = path
        self.f = open(path, "a") if path else None

    def log(self, **rec):
        if self.f:
            self.f.write(json.dumps(rec, default=str) + "\n")
            self.f.flush()


def run_tool(sandbox, name, args, write_prefix=None):
    """Returns (ok, text)."""
    if not isinstance(args, dict):
        return False, "invalid tool input"
    if name == "write_file":
        if not isinstance(args.get("path"), str) or not isinstance(args.get("content"), str):
            return False, "write_file needs string 'path' and 'content'"
        if write_prefix:
            full = sandbox.resolve(args["path"])
            if full is None or not full.startswith(f"/work/{write_prefix}/"):
                return False, f"you may only write files under {write_prefix}/"
        return sandbox.write_file(args["path"], args["content"])
    if name == "read_file":
        if not isinstance(args.get("path"), str):
            return False, "read_file needs a string 'path'"
        ok, text = sandbox.read_file(args["path"])
        return ok, clip(text, 30000)
    if name == "list_files":
        return True, sandbox.list_files()
    if name == "run":
        if not isinstance(args.get("command"), str):
            return False, "run needs a string 'command'"
        code, out = sandbox.exec(args["command"])
        return True, f"exit code: {code}\n{clip(out)}"
    return False, f"unknown tool {name}"


def agent_loop(backend, meter, stage, sandbox, system, messages, tool_names, max_turns, transcript, cost_cap,
               write_prefix=None, single_pass=False):
    """Runs until the model stops calling tools. Mutates `messages` (append only). Returns a small result dict."""
    tools = [TOOLS[n] for n in tool_names]
    info = {"turns": 0, "tool_calls": 0, "stopped_by": None, "last_pytest": None, "final_text": "", "commands": []}
    while True:
        if info["turns"] >= max_turns:
            info["stopped_by"] = "max_turns"
            break
        if meter.cost >= cost_cap:
            info["stopped_by"] = "run_cost_cap"
            break
        blocks, stop, usage, raw = backend.complete(system, messages, tools)
        meter.add(stage, backend.model, usage)
        info["turns"] += 1
        transcript.log(stage=stage, role="assistant", turn=info["turns"], stop_reason=stop, usage=usage, content=blocks)
        messages.append({"role": "assistant", "content": raw})
        text = "\n".join(b["text"] for b in blocks if b["type"] == "text")
        if text.strip():
            info["final_text"] = text
        calls = [b for b in blocks if b["type"] == "tool_use"]
        if stop in ("refusal", "max_tokens") and not calls:
            info["stopped_by"] = stop
            break
        if not calls:
            info["stopped_by"] = "done"
            break
        results = []
        for c in calls:
            ok, out = run_tool(sandbox, c["name"], c["input"], write_prefix)
            info["tool_calls"] += 1
            if c["name"] == "run" and isinstance(c["input"], dict):
                info["commands"].append(c["input"].get("command", ""))
                found = PYTEST_SUMMARY.findall(out)
                if found:
                    info["last_pytest"] = found[-1]
            results.append({"type": "tool_result", "tool_use_id": c["id"], "content": out, **({} if ok else {"is_error": True})})
            transcript.log(stage=stage, role="tool", turn=info["turns"], tool=c["name"], ok=ok, output=out)
        if single_pass:
            info["stopped_by"] = "single_pass"
            break
        messages.append({"role": "user", "content": results})
    return info
