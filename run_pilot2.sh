#!/bin/bash
# Pilot round 2 after the prompt fix: Gemini test writers (round 1 hit a thought-signature bug), C1 again on Haiku and
# Sonnet. Same $5 hard cap over every pilot tag. Resumable.
set -u
CAP="--cap 5 --cap-tags c1-pilot,c1-pilot2,c4-pilot --resume"
uv run python -m sdlc.c4 write --tag c4-pilot --models gemini --trials 1 $CAP
uv run python -m sdlc.c1 --tag c1-pilot2 --models haiku --conditions none,plan,speckit,kiro --trials 1 $CAP
uv run python -m sdlc.c1 --tag c1-pilot2 --models sonnet --specs splitr --conditions none,plan,speckit,kiro --trials 1 --run-cap 1.2 $CAP
uv run python -m sdlc.c4 score --tag c4-pilot
