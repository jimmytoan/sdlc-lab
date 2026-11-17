"""Import the dark factory harness from vendor/darkfactory (an unmodified copy) and point its output paths at sdlc-lab.

Its modules read `config.X` at call time, so overriding the attributes here redirects grading scratch space and keeps
the vendored copy clean. Image `dfl-sandbox` is built from vendor/darkfactory/sandbox/Dockerfile.
"""
import sys

from . import paths

sys.path.insert(0, str(paths.VENDOR))

from factory import config  # noqa: E402

config.RESULTS = paths.RESULTS
config.RAW = paths.RESULTS / "raw"
config.RUNS = paths.RESULTS / "df-unused-runs.jsonl"     # never written by sdlc code; set so nothing lands in vendor/
config.SPEND = paths.RESULTS / "df-unused-spend.jsonl"
config.RUNTIME = paths.RUNTIME

from factory import agent, designs, gaming, grade  # noqa: E402,F401
from factory.sandbox import Sandbox  # noqa: E402,F401

SPEC_NAMES = config.SPEC_NAMES
ENTRY = config.ENTRY
