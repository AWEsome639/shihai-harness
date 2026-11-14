#!/usr/bin/env python3
"""Local model relay for CI dev runs.

The harness sends the competition's `model_api` string (e.g.
`google-cloud/claude-opus-5-5`). A plain OpenAI-compatible provider such as
DeepSeek rejects that name, so this relay accepts any model name, rewrites it
to the real one, and forwards the request unchanged otherwise.

Usage: MODEL_RELAY_PORT=4100 MODEL_RELAY_TARGET=https://api.deepseek.com/v1 \
       MODEL_RELAY_MODEL=deepseek-chat MODEL_RELAY_KEY=sk-... python ci/model_relay.py
"""
import json
import os
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("MODEL_RELAY_PORT", "4100"))
TARGET = os.environ.get("MODEL_RELAY_TARGET", "https://api.deepseek.com/v1").rstrip("/")
MODEL = os.environ.get("MODEL_RELAY_MODEL", "deepseek-chat")
KEY = os.environ.get("MODEL_RELAY_KEY", "")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body=b"", ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            body = json.dumps({"object": "list", "data": [
                {"id": m, "object": "model", "owned_by": "relay"} for m in
                ("google-cloud/claude-opus-5-5", MODEL)]}).encode()
            self._send(200, body)
        else:
            self._send(404, b'{"error":"not found"}')

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        try:
            payload = json.loads(raw)
            payload["model"] = MODEL
            raw = json.dumps(payload).encode()
        except Exception:
            pass
        path = self.path
        print(f"REQ {self.command} {self.path} -> {TARGET}{path}", flush=True)
        # TARGET may already end with /v1; the client always sends /v1/...
        if TARGET.endswith("/v1") and path.startswith("/v1"):
            path = path[3:]
        req = urllib.request.Request(
            TARGET + path,
            data=raw,
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + KEY},
        )
        try:
            with urllib.request.urlopen(req, timeout=900) as r:
                body = r.read()
                self._send(r.status, body)
        except Exception as exc:
            self._send(502, json.dumps({"error": str(exc)[:300]}).encode())


if __name__ == "__main__":
    print(f"relay on :{PORT} -> {TARGET} (model {MODEL})", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
