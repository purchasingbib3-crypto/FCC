"""V12.3.1 wrapper for the existing FCC static proxy.

Only static HTML delivery is overridden: canonical index.html is injected with
v12_3_1_fast_import_patch.js. API proxy/upload behavior remains inherited from
the already-tested V12.3 static_proxy.Handler.
"""
from __future__ import annotations

import os

from static_proxy import (
    API_HOST,
    API_PORT,
    FIELD_STATIC_DIR,
    STATIC_DIR,
    Handler as BaseHandler,
    ThreadingHTTPServer,
)

PATCH_TAG = b'<script src="/v12_3_1_fast_import_patch.js?v=20260814"></script>'


def inject_patch(path: str, data: bytes) -> bytes:
    if os.path.basename(path).lower() != "index.html" or PATCH_TAG in data:
        return data
    marker = b"</body>"
    if marker in data:
        return data.replace(marker, PATCH_TAG + b"\n" + marker, 1)
    return data + b"\n" + PATCH_TAG


class Handler(BaseHandler):
    def _serve_static(self):
        if self.path.split("?", 1)[0].startswith("/field"):
            self._serve_field_static()
            return
        path = self._safe_path()
        if path is None:
            self.send_error(404)
            return
        if self.path == "/" or self.path == "" or os.path.isdir(path):
            path = os.path.join(STATIC_DIR, "index.html")
        if not os.path.isfile(path):
            self.send_error(404)
            return
        with open(path, "rb") as handle:
            data = inject_patch(path, handle.read())
        self._send_static_bytes(path, data)

    def _serve_field_static(self):
        path = self._safe_field_path()
        if path is None or not os.path.isfile(path):
            self.send_error(404)
            return
        with open(path, "rb") as handle:
            data = inject_patch(path, handle.read())
        self._send_static_bytes(path, data)

    def _send_static_bytes(self, path: str, data: bytes):
        self.send_response(200)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)
            self.wfile.flush()


if __name__ == "__main__":
    port = int(os.environ.get("FCC_STATIC_PORT", "8765"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(
        f"FCC static+proxy V12.3.1 on :{port}, API -> {API_HOST}:{API_PORT}, static={STATIC_DIR}, field={FIELD_STATIC_DIR}",
        flush=True,
    )
    server.serve_forever()
