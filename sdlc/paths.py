"""Paths for sdlc-lab. The dark factory assets live in vendor/darkfactory (a copy; never edit)."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENDOR = ROOT / "vendor" / "darkfactory"
CORPUS = ROOT / "corpus"
RESULTS = ROOT / "results"
RUNTIME = ROOT / ".runtime"
