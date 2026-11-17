"""Test helper: starts the server as a black box on a free port and talks HTTP to it."""
import http.client
import json
import os
import socket
import subprocess
import sys
import time

import pytest

APP_DIR = os.environ.get("APP_DIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Api:
    def __init__(self, port, proc):
        self.port, self.proc = port, proc

    def call(self, method, path, body=None, headers=None, raw=None):
        """Returns (status, parsed JSON body or None, response headers dict with lower-case keys)."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = dict(headers or {})
        data = raw
        if body is not None:
            data = json.dumps(body).encode()
        if data is not None:
            h.setdefault("Content-Type", "application/json")
        conn.request(method, path, body=data, headers=h)
        r = conn.getresponse()
        text = r.read().decode("utf-8")
        conn.close()
        parsed = json.loads(text) if text.strip() else None
        return r.status, parsed, {k.lower(): v for k, v in r.getheaders()}

    def room(self, name="Atlas", capacity=8, **extra):
        s, b, _ = self.call("POST", "/rooms", {"name": name, "capacity": capacity, **extra})
        assert s == 201, (s, b)
        return b["id"]

    def book(self, room_id, start, end, title="sync", attendees=2, headers=None):
        """start/end like '09:00' are expanded to 2026-03-02T09:00:00Z."""
        return self.call("POST", "/bookings", {"room_id": room_id, "start": ts(start), "end": ts(end),
                                               "title": title, "attendees": attendees}, headers=headers)


def ts(s):
    return s if "T" in s else f"2026-03-02T{s}:00Z"


def code(body):
    return body["error"]["code"]


@pytest.fixture
def api():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen([sys.executable, os.path.join(APP_DIR, "server.py"), "--port", str(port)],
                            cwd=APP_DIR, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.time() + 10
        while True:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                if proc.poll() is not None or time.time() > deadline:
                    raise RuntimeError("server did not start")
                time.sleep(0.05)
        yield Api(port, proc)
    finally:
        proc.kill()
        proc.wait()
