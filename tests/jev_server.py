"""A local stand-in for api.typesafe.ai, shared by the Jev tests.

Each request takes the next scripted reply (the last one repeats), so a test
states the exact conversation: `[(429, {}), (200, body)]`. Nothing here, or in
any Jev test, reaches the real API.
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

KEY = "jev-test-key-0123456789abcdef"


def reply(status=200, body=None, headers=None, delay=0.0):
    return {"status": status, "body": body, "headers": headers or {}, "delay": delay}


def answers_for(questions: dict, **overrides) -> dict:
    """A well-formed 200 body: every asked id answered, shaped by its type."""
    out = {}
    for qid, q in questions.items():
        if qid in overrides:
            out[qid] = overrides[qid]
        elif q["type"] == "noul":
            out[qid] = {"type": "noul", "noul": 0.5}
        elif q["type"] == "choice":
            name = next(iter(q["criteria"]))
            out[qid] = {"type": "choice", "choice": name, "confidence": 0.9,
                        "probabilities": {name: 0.9}}
        else:
            out[qid] = {"type": "score", "score": 1.0, "confidence": 0.9,
                        "legend": {}, "probabilities": {}}
    return {"model": "jev-1.13.0", "answers": out,
            "usage": {"input_tokens": 1000, "output_tokens": 10}}


class FakeJev:
    def __init__(self, script):
        self.script = list(script)
        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - http.server's name
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length)
                try:
                    parsed = json.loads(raw)
                except ValueError:
                    parsed = None
                outer.requests.append({"auth": self.headers.get("Authorization"),
                                       "body": parsed, "raw": raw.decode("utf-8", "replace")})
                step = outer.script[min(len(outer.requests), len(outer.script)) - 1]
                if step["delay"]:
                    time.sleep(step["delay"])
                body = step["body"]
                if callable(body):
                    body = body(parsed)
                data = body if isinstance(body, str) else json.dumps(body)
                try:
                    self.send_response(step["status"])
                    for k, v in step["headers"].items():
                        self.send_header(k, v)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(data.encode("utf-8"))
                except OSError:
                    pass

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1/systemone"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def sent_text(self) -> str:
        """Everything the server received, for 'this never left the machine' checks."""
        return "\n".join(r["raw"] for r in self.requests)


class RawServer:
    """A server that does not speak HTTP properly: it reads the request, then
    writes `payload` byte by byte (`drip` seconds apart, 0 = at once) and closes."""

    def __init__(self, payload: bytes, drip: float = 0.0):
        import socket

        self.payload, self.drip = payload, drip
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.url = f"http://127.0.0.1:{self.sock.getsockname()[1]}/v1/systemone"
        self.running = True
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while self.running:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        try:
            conn.settimeout(5)
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                data += chunk
            if self.drip:
                for i in range(len(self.payload)):
                    conn.sendall(self.payload[i:i + 1])
                    time.sleep(self.drip)
            else:
                conn.sendall(self.payload)
        except OSError:
            pass
        finally:
            conn.close()

    def close(self):
        self.running = False
        self.sock.close()
