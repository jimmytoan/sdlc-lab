#!/bin/bash
# Stage 1 pilot: C4 writers, C1 on Haiku, C1 on Sonnet (splitr). One $5 hard cap shared by both tags. Resumable.
set -u
CAP="--cap 5 --cap-tags c1-pilot,c4-pilot --resume"
uv run python -m sdlc.c4 write --tag c4-pilot --models haiku,terra,gemini --trials 1 $CAP
uv run python -m sdlc.c1 --tag c1-pilot --models haiku --conditions none,plan,speckit,kiro --trials 1 $CAP
uv run python -m sdlc.c1 --tag c1-pilot --models sonnet --specs splitr --conditions none,plan,speckit,kiro --trials 1 --run-cap 1.2 $CAP
uv run python -m sdlc.c4 score --tag c4-pilot
