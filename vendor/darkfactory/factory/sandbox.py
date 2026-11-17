"""The coder's sandbox: a Docker container with no network and exactly one bind mount (the workspace)."""
import json
import posixpath
import subprocess
import uuid

from . import config


class Sandbox:
    def __init__(self, workspace):
        self.workspace = str(workspace)
        self.name = f"dfl-{uuid.uuid4().hex[:10]}"

    def start(self):
        subprocess.run(
            ["docker", "run", "-d", "--rm", "--name", self.name, "--network", "none", "--memory", "1g", "--cpus", "2",
             "--pids-limit", "256", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
             "-v", f"{self.workspace}:/work", "-w", "/work", config.IMAGE, "sleep", "86400"],
            check=True, capture_output=True, text=True)
        return self

    def stop(self):
        subprocess.run(["docker", "rm", "-f", self.name], capture_output=True)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    def inspect(self):
        out = subprocess.run(["docker", "inspect", self.name], check=True, capture_output=True, text=True).stdout
        return json.loads(out)[0]

    def exec(self, command, timeout=config.CMD_TIMEOUT_S, stdin=None):
        """Run a shell command in /work. Returns (exit_code, combined output)."""
        try:
            p = subprocess.run(
                ["docker", "exec", "-i", "-w", "/work", self.name, "timeout", "-k", "2", str(timeout), "bash", "-c", command],
                input=stdin, capture_output=True, timeout=timeout + 20)
        except subprocess.TimeoutExpired:
            return 124, f"[command killed after {timeout}s]"
        out = (p.stdout + p.stderr).decode("utf-8", "replace")
        if p.returncode == 124:
            out += f"\n[command timed out after {timeout}s]"
        return p.returncode, out

    @staticmethod
    def resolve(path):
        """Map a tool path to an absolute path under /work, or None if it escapes."""
        full = posixpath.normpath(posixpath.join("/work", path))
        return full if full == "/work" or full.startswith("/work/") else None

    def write_file(self, path, content):
        full = self.resolve(path)
        if full is None or full == "/work":
            return False, "path must stay inside the workspace"
        code = ("import os,sys; p=sys.argv[1]; os.makedirs(os.path.dirname(p), exist_ok=True); "
                "open(p,'wb').write(sys.stdin.buffer.read())")
        p = subprocess.run(["docker", "exec", "-i", self.name, "python", "-c", code, full],
                           input=content.encode("utf-8"), capture_output=True, timeout=60)
        if p.returncode != 0:
            return False, p.stderr.decode("utf-8", "replace")[-500:]
        return True, f"wrote {len(content)} chars to {posixpath.relpath(full, '/work')}"

    def read_file(self, path):
        full = self.resolve(path)
        if full is None:
            return False, "path must stay inside the workspace"
        p = subprocess.run(["docker", "exec", self.name, "cat", full], capture_output=True, timeout=60)
        if p.returncode != 0:
            return False, p.stderr.decode("utf-8", "replace")[-500:]
        return True, p.stdout.decode("utf-8", "replace")

    def list_files(self):
        _, out = self.exec("find . -type f -not -path '*/__pycache__/*' -not -path './.pytest_cache/*' | sort | head -200")
        return out
