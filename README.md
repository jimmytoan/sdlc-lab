# sdlc-lab

Three experiments on claims about AI in the software lifecycle, built on the dark factory lab (`vendor/darkfactory`, a copy of its specs, held-out suites, references and harness at commit a468a6c).

- **C1. Does a spec make the code better?** A brief ticket goes to code under four conditions: no spec, a plain `plan.md`, GitHub Spec Kit v1.2.6 (`vendor/speckit`), and a Kiro-style requirements/design/tasks workflow. Graded on the dark factory's held-out suites. `sdlc/c1.py`.
- **C3. Passes tests, hard to maintain?** ruff, pylint, radon and a token-clone detector over every shipped codebase, compared with the references. `sdlc/c3.py`, `sdlc/metrics.py`, `sdlc/dup.py`.
- **C4. Are AI-written tests any good?** Models from three families write tests for the references; mutmut mutants and the dark factory's real faulty builds score them against the held-out suites. `sdlc/c4.py`, `sdlc/mutate.py`.

Status, results and the proposed full run: `PLAN.md`. Sources: `SOURCES.md`. Reports: `results/*.md`.

## Setup

```bash
uv sync
```

```bash
docker build -t dfl-sandbox vendor/darkfactory/sandbox
```

```bash
docker build -t sdlc-mut -f sandbox/Dockerfile.mut sandbox
```

```bash
uv run python -m sdlc.mutate generate
```

API keys go in a git-ignored `.env` (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`).

## Commands

```bash
uv run python -m sdlc.selftest
```

```bash
uv run python -m sdlc.import_df && uv run python -m sdlc.c3
```

```bash
./run_pilot.sh
```

Every model run checks a hard spend cap (`--cap`, over `--cap-tags`) against `results/spend.jsonl` before each call; `--resume` skips finished cells. Transcripts and workspaces go to `results/raw/` (git-ignored).
