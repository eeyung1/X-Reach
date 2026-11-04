"""In-memory webhook receiver used by the Compose E2E profile."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


DELIVERIES: list[dict[str, object]] = []


class WebhookHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path != "/events":
            self.send_error(404)
            return
        body = json.dumps(DELIVERIES).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path != "/events":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 1_000_000:
            self.send_error(413)
            return
        body = self.rfile.read(length).decode("utf-8")
        DELIVERIES.append(
            {
                "body": body,
                "headers": {key.lower(): value for key, value in self.headers.items()},
            }
        )
        self.send_response(202)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, _format: str, *_args: object) -> None:
        return


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 9000), WebhookHandler).serve_forever()
