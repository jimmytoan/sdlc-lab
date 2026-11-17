"""Render GitHub Spec Kit v1.2.6 command templates for a shell-script agent, without the specify CLI.

Follows specify_cli.integrations.base.IntegrationBase.process_template (v1.2.6):
1. take scripts.sh from the frontmatter, 2. replace {SCRIPT}, 3. drop the scripts: block, 4. {ARGS} and $ARGUMENTS
become the arguments, 6. scripts/ and templates/ paths move under .specify/, 7. __SPECKIT_COMMAND_X__ becomes /speckit.x.
Step 5 (__AGENT__) does not occur in these commands. The workspace gets .specify/scripts/bash and .specify/templates
exactly as `specify init --script sh` lays them out; no constitution is written (the commands treat it as optional).
"""
import re
import shutil
from pathlib import Path

from . import paths

SPECKIT = paths.ROOT / "vendor" / "speckit"


def render(command, arguments):
    text = (SPECKIT / "commands" / f"{command}.md").read_text()
    fm_end = text.index("\n---", 3)
    front, body = text[:fm_end], text[fm_end:]
    m = re.search(r"(?m)^\s*sh:\s*(.+)$", front)
    script = m.group(1).strip() if m else ""
    script = script.replace("{ARGS}", "$ARGUMENTS")
    front = re.sub(r"(?ms)^scripts:\n(?:[ \t]+.*\n?)*", "", front)
    text = front + body
    if script:
        text = text.replace("{SCRIPT}", script)
    text = text.replace("{ARGS}", "$ARGUMENTS").replace("$ARGUMENTS", arguments)
    text = re.sub(r"(?<![\w./])scripts/", ".specify/scripts/", text)
    text = re.sub(r"(?<![\w./])templates/", ".specify/templates/", text)
    text = re.sub(r"__SPECKIT_COMMAND_([A-Z][A-Z0-9_-]*)__", lambda x: "/speckit." + x.group(1).lower().replace("_", "."), text)
    return text


def install(workspace):
    """Lay out .specify/ in a workspace as `specify init --script sh` would (scripts and templates only)."""
    dst = Path(workspace) / ".specify"
    shutil.copytree(SPECKIT / "scripts", dst / "scripts")
    shutil.copytree(SPECKIT / "templates", dst / "templates")
