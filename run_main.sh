#!/bin/bash
# Stage 2, option M (approved 2026-11-18, hard cap $30 over c1-main and c4-main, enforced in code before every model call).
# Resumable: re-run this script; finished cells are skipped, errored cells are retried.
#   ./run_main.sh paid     Anthropic and OpenAI blocks, cheapest first, Sonnet C1 last
#   ./run_main.sh gemini   Gemini C4 writers (free tier, 15 req/min, 500/day; run alongside `paid`)
#   ./run_main.sh score    mutation and real-fault scoring, C3 over C1 output, reports (no model calls)
set -u
CAP="--cap 30 --cap-tags c1-main,c4-main --resume"
case "${1:-}" in
paid)
  uv run python -m sdlc.c1 --tag c1-main --models haiku --conditions none,plan,speckit,kiro --trials 5 $CAP
  uv run python -m sdlc.c1 --tag c1-main --models haiku --conditions speckit-clarify --trials 5 $CAP
  uv run python -m sdlc.c4 write --tag c4-main --models haiku,terra --trials 5 $CAP
  uv run python -m sdlc.c4 write --tag c4-main --models haiku,terra --trials 3 --variant blackbox $CAP
  uv run python -m sdlc.c4 write --tag c4-main --models sonnet --trials 3 $CAP
  uv run python -m sdlc.c1 --tag c1-main --models sonnet --conditions none,plan,speckit,kiro --trials 3 $CAP
  ;;
gemini)
  uv run python -m sdlc.c4 write --tag c4-main --models gemini --trials 5 $CAP
  ;;
score)
  uv run python -m sdlc.c4 score --tag c4-main
  uv run python -m sdlc.c4 realfaults --tag c4-main
  uv run python -m sdlc.c3
  uv run python -m sdlc.report c1 --tag c1-main
  uv run python -m sdlc.report c4 --tag c4-main
  ;;
*) echo "usage: $0 paid|gemini|score"; exit 2 ;;
esac
