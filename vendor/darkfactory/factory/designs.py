"""The three factory designs. Each takes a prepared workspace (SPEC.md, tests/, maybe existing code) to shipped code."""
import re

from . import config
from .agent import agent_loop

FACTORY = ("You are a stage in an automated software factory. No human will read, review or test the code: "
           "whatever is in the workspace when the factory finishes is shipped to customers as is. "
           "The workspace root is the current directory. Python 3.13, standard library only, no network.")
RULES = ("Rules: implement exactly what the spec says, including every edge case and error rule it states. "
         "The files under tests/ are acceptance examples supplied by the customer; treat them as read-only. "
         "They show the expected interface but cover only part of the spec. "
         "The shipped product is judged against everything the customer expects from the spec, not only against the examples. "
         "Where the spec is silent, choose the behaviour the customer most likely wants.")

CODER_BUILD = f"""{FACTORY}

Your job: build the product described in SPEC.md from scratch. {RULES}"""
CODER_CHANGE = f"""{FACTORY}

Your job: the workspace holds an existing, shipped product that was built to SPEC.md. Apply the change request in CHANGE.md. \
Behaviour that CHANGE.md does not mention must keep working exactly as SPEC.md describes. {RULES} \
tests/test_public_v2.py holds the acceptance examples for the change."""
PLANNER = f"""{FACTORY}

You are the planner. You do not write product code. Read the material and write the plan the coder will follow: \
(1) a short design (files, main functions, data structures), (2) a numbered checklist of every rule, edge case and error \
case stated in the spec, each as one concrete, checkable sentence, (3) risks: places where a quick implementation would \
get the spec wrong. Reply with the plan as markdown and nothing else."""
EVALUATOR = f"""{FACTORY}

You are the evaluator. You did not write this code and you should not trust it. Your job is to find where the product \
in the workspace deviates from the spec. Read {{docs}} closely, then write your own black-box checks under eval/ and run them \
against the product. Go beyond tests/: probe the edge cases and error rules the spec states. You may only write files under eval/. \
Do not fix the product.

Finish with a report as your final message. If you found deviations, list each one as: what you ran, what the spec requires \
(quote the rule), what happened instead. If you found none, the last line of the report must be exactly: VERDICT: PASS"""


def _sh(sandbox, cmd):
    return sandbox.exec(cmd)[1]


def _material(sandbox, change):
    """Spec, tests and (for a change) the current source, inlined for stages that have no tools."""
    parts = [("SPEC.md", sandbox.read_file("SPEC.md")[1])]
    if change:
        parts.append(("CHANGE.md", sandbox.read_file("CHANGE.md")[1]))
    files = _sh(sandbox, "find . -type f -name '*.py' -not -path './eval/*' -not -path '*/__pycache__/*' | sort").split()
    for f in files:
        f = f[2:] if f.startswith("./") else f
        if f.startswith("tests/") or change:
            parts.append((f, sandbox.read_file(f)[1]))
    return "\n\n".join(f"===== {name} =====\n{body}" for name, body in parts)


def oneshot(ctx):
    """One model response. It can write files, but it never sees a test run."""
    task = ("Apply CHANGE.md to the existing product." if ctx.change else "Build the product.")
    msg = (f"{_material(ctx.sandbox, ctx.change)}\n\n{task} You get exactly one response and cannot run anything. "
           "Write every file you need with write_file calls in this single response.")
    info = agent_loop(ctx.backend, ctx.meter, "coder", ctx.sandbox, CODER_CHANGE if ctx.change else CODER_BUILD,
                      [{"role": "user", "content": msg}], ["write_file"], 1, ctx.transcript, ctx.cost_cap, single_pass=True)
    return {"stages": {"coder": info}, "iterations": 1}


def _coder_kickoff(ctx, plan=False):
    what = "Read SPEC.md and CHANGE.md, then the existing code." if ctx.change else "Read SPEC.md and the examples in tests/."
    plan_txt = " PLAN.md holds the plan from the planning stage; follow it and work through its checklist." if plan else ""
    return (f"{what}{plan_txt} Build it, run the tests with `python -m pytest tests -q`, and keep going until you are "
            "confident the product matches the whole spec. When you are done, reply with a short summary and no tool call.")


def loop(ctx):
    """One agent with a shell, iterating on the visible tests until it decides it is done."""
    messages = [{"role": "user", "content": _coder_kickoff(ctx)}]
    info = agent_loop(ctx.backend, ctx.meter, "coder", ctx.sandbox, CODER_CHANGE if ctx.change else CODER_BUILD, messages,
                      ["write_file", "read_file", "list_files", "run"], config.MAX_TURNS["coder"], ctx.transcript, ctx.cost_cap)
    return {"stages": {"coder": info}, "iterations": info["turns"]}


def _src_hash(sandbox):
    return _sh(sandbox, "find . -type f -not -path './eval/*' -not -path '*/__pycache__/*' -not -path '*/.pytest_cache/*' "
                        "| sort | xargs sha256sum | sha256sum")


def pce(ctx):
    """Planner writes a plan, coder builds, a separate evaluator probes the result, coder fixes. Fresh context per role."""
    stages = {}
    task = "the change request" if ctx.change else "the product"
    p_msgs = [{"role": "user", "content": f"{_material(ctx.sandbox, ctx.change)}\n\nWrite the plan for {task}."}]
    stages["planner"] = agent_loop(ctx.backend, ctx.meter, "planner", ctx.sandbox, PLANNER, p_msgs, [], 1, ctx.transcript, ctx.cost_cap)
    ctx.sandbox.write_file("PLAN.md", stages["planner"]["final_text"])

    c_msgs = [{"role": "user", "content": _coder_kickoff(ctx, plan=True)}]
    coder_sys = CODER_CHANGE if ctx.change else CODER_BUILD
    tools = ["write_file", "read_file", "list_files", "run"]
    stages["coder"] = agent_loop(ctx.backend, ctx.meter, "coder", ctx.sandbox, coder_sys, c_msgs, tools,
                                 config.MAX_TURNS["coder"], ctx.transcript, ctx.cost_cap)
    rounds, verdicts, evaluator_touched_src = 0, [], False
    docs = "SPEC.md and CHANGE.md" if ctx.change else "SPEC.md"
    for r in range(1, config.EVAL_ROUNDS + 1):
        if ctx.meter.cost >= ctx.cost_cap:
            break
        rounds = r
        before = _src_hash(ctx.sandbox)
        e_msgs = [{"role": "user", "content": f"Evaluate the product in the workspace against {docs}. This is evaluation round {r}."}]
        ev = agent_loop(ctx.backend, ctx.meter, f"evaluator{r}", ctx.sandbox, EVALUATOR.format(docs=docs), e_msgs, tools,
                        config.MAX_TURNS["evaluator"], ctx.transcript, ctx.cost_cap, write_prefix="eval")
        stages[f"evaluator{r}"] = ev
        evaluator_touched_src = evaluator_touched_src or (_src_hash(ctx.sandbox) != before)
        passed = ev["final_text"].strip().endswith("VERDICT: PASS")
        verdicts.append("pass" if passed else "defects")
        if passed or ev["stopped_by"] != "done":
            break
        c_msgs.append({"role": "user", "content": "An independent evaluator checked your work against the spec and reported the "
                       "following. Verify each point against the spec, fix what is really wrong, re-run the tests, then reply "
                       f"with a short summary and no tool call.\n\n{ev['final_text']}"})
        stages[f"fix{r}"] = agent_loop(ctx.backend, ctx.meter, f"fix{r}", ctx.sandbox, coder_sys, c_msgs, tools,
                                       config.MAX_TURNS["fix"], ctx.transcript, ctx.cost_cap)
    return {"stages": stages, "iterations": sum(s["turns"] for s in stages.values()), "eval_rounds": rounds,
            "eval_verdicts": verdicts, "evaluator_touched_src": evaluator_touched_src}


ASKER = f"""{FACTORY}

You are the analyst. You do not write code. Before the coder starts, you may put questions to the customer who wrote the spec. \
Read the spec and the examples and find what a correct implementation needs to know but the spec does not settle: ambiguous rules, \
missing edge cases, unstated ordering or tie-breaks, input variants. Ask at most {config.MAX_QUESTIONS} questions. Each must be \
specific and answerable in one or two sentences; do not ask for the spec to be rewritten. Reply with a numbered list of questions and nothing else."""
ORACLE = """You are the customer who owns this product. A developer is asking you questions about what you want. \
Below is everything you know about what the product must do. Answer each numbered question in one or two sentences, using only \
this knowledge. Answer only what is asked and do not volunteer other rules. If your knowledge does not settle a question, \
answer "No preference." Reply with the numbered answers and nothing else.

===== what you know =====
"""


def ask(ctx):
    """Like loop, but first an analyst may ask the spec owner up to N questions. The owner is a model holding the full spec."""
    stages = {}
    q_msgs = [{"role": "user", "content": f"{_material(ctx.sandbox, ctx.change)}\n\nWrite your questions for the customer."}]
    stages["asker"] = agent_loop(ctx.backend, ctx.meter, "asker", ctx.sandbox, ASKER, q_msgs, [], 1, ctx.transcript, ctx.cost_cap)
    questions = stages["asker"]["final_text"]
    n_questions = len(re.findall(r"(?m)^\s*\d+[.)]", questions))
    o_msgs = [{"role": "user", "content": f"Questions from the developer:\n\n{questions}"}]
    stages["oracle"] = agent_loop(ctx.oracle, ctx.meter, "oracle", ctx.sandbox, ORACLE + ctx.full_spec, o_msgs, [], 1,
                                  ctx.transcript, ctx.cost_cap)
    ctx.sandbox.write_file("ANSWERS.md", "# Questions to the customer, and the customer's answers\n\n## Questions\n\n"
                           f"{questions}\n\n## Answers\n\n{stages['oracle']['final_text']}\n")
    kickoff = _coder_kickoff(ctx) + " ANSWERS.md holds the customer's answers to questions an analyst asked; they are part of the spec."
    stages["coder"] = agent_loop(ctx.backend, ctx.meter, "coder", ctx.sandbox, CODER_CHANGE if ctx.change else CODER_BUILD,
                                 [{"role": "user", "content": kickoff}], ["write_file", "read_file", "list_files", "run"],
                                 config.MAX_TURNS["coder"], ctx.transcript, ctx.cost_cap)
    return {"stages": stages, "iterations": sum(s["turns"] for s in stages.values()), "questions": n_questions}


DESIGNS = {"oneshot": oneshot, "loop": loop, "pce": pce, "ask": ask}
