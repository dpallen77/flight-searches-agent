"""Local web server: serves the chat page and exposes the agent over JSON.
Run: python server.py  then open http://127.0.0.1:8000
"""
import json
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def load_env(path):
    """Minimal .env reader so no extra packages are needed."""
    try:
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except OSError:
        pass


ROOT = os.path.dirname(os.path.abspath(__file__))
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(os.path.join(ROOT, "server.log")), logging.StreamHandler()],
)
log = logging.getLogger("flight-agent")
load_env(os.path.join(ROOT, ".env"))  # must run before importing agent

import agent  # noqa: E402


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/":
            with open(os.path.join(ROOT, "index.html"), "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
        elif self.path == "/api/usage":
            self._send(200, agent.usage_snapshot())
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            sid = body.get("session", "default")
            if self.path == "/api/chat":
                log.info("chat: %r", body.get("message", ""))
                events = agent.handle_message(sid, body.get("message", ""))
            elif self.path == "/api/approval":
                log.info("approval: %s", body.get("decision"))
                events = agent.handle_approval(sid, body.get("decision", "cancel"))
            else:
                return self._send(404, {"error": "not found"})
            self._send(200, {"events": events, "usage": agent.usage_snapshot()})
        except Exception as e:
            log.exception("Unhandled error on %s", self.path)
            self._send(200, {"events": [{"type": "text",
                       "text": f"Server error: {type(e).__name__}: {e}. Details are in server.log."}],
                       "usage": agent.usage_snapshot()})

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    print(f"Flight agent running at http://127.0.0.1:8000 ({agent.usage_snapshot()['mode']} data)")
    ThreadingHTTPServer(("127.0.0.1", 8000), Handler).serve_forever()