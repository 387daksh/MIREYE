from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def start_health_server(port: int, dependencies: dict[str, tuple[str, int]]) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path not in {"/health/live", "/health/ready"}:
                self.send_error(404)
                return
            if self.path == "/health/live":
                self._write(200, {"status": "live"})
                return
            status = {name: _reachable(host, dependency_port) for name, (host, dependency_port) in dependencies.items()}
            ready = all(status.values())
            self._write(200 if ready else 503, {"status": "ready" if ready else "unavailable", "dependencies": status})

        def _write(self, status_code: int, body: dict) -> None:
            payload = json.dumps(body).encode()
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, _format: str, *_args: object) -> None:
            pass

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _reachable(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False
