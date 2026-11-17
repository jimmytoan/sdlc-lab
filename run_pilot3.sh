#!/bin/bash
# Pilot round 3: Gemini test writers with a longer 503 back-off, then mutation and real-fault scoring, then C3 over C1 output.
set -u
uv run python -m sdlc.c4 write --tag c4-pilot --models gemini --trials 1 --cap 5 --cap-tags c1-pilot,c1-pilot2,c4-pilot --resume
uv run python -m sdlc.c4 score --tag c4-pilot
uv run python -m sdlc.c4 realfaults --tag c4-pilot
uv run python -m sdlc.c3
uv run python -m sdlc.report c1 --tag c1-pilot2
uv run python -m sdlc.report c1 --tag c1-pilot
uv run python -m sdlc.report c4 --tag c4-pilot
