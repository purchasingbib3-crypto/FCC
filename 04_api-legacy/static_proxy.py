"""Combined server: static files + reverse proxy to fcc-api on :8001.

Fix over previous version: use HTTP/1.1 with explicit Content-Length
matching actual bytes written (avoids ERR_CONTENT_LENGTH_MISMATCH
in Chromium when proxy returns Content-Length that doesn't match body).
"""
import http.server, urllib.request, urllib.parse, os, json

BUNDLE_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
STATIC_DIR = os.environ.get("FCC_STATIC_DIR", os.path.join(BUNDLE_ROOT, "03_frontend"))
# /field serves the same canonical frontend unless explicitly overridden.
FIELD_STATIC_DIR = os.environ.get("FCC_FIELD_STATIC_DIR", STATIC_DIR)
API_HOST = os.environ.get("FCC_API_HOST", "127.0.0.1")
API_PORT = int(os.environ.get("FCC_API_PORT", os.environ.get("FCC_PORT", "8001")))
API_TIMEOUT = int(os.environ.get("FCC_PROXY_TIMEOUT", "300"))
PROXY_MAX_BODY_MB = int(os.environ.get("FCC_PROXY_MAX_BODY_MB", "60"))
DOWNLOAD_DIR = os.environ.get("FCC_DOWNLOAD_DIR", os.path.join(BUNDLE_ROOT, "downloads"))


class Handler(http.server.BaseHTTPRequestHandler):
    """Minimal HTTP/1.1 server. No keep-alive (simple + reliable)."""

    protocol_version = "HTTP/1.1"

    # ---- static files ----
    def do_GET(self):
        if self.path.startswith("/api/"):
            self._proxy("GET")
            return
        if self.path.startswith("/dashboard"):
            self._proxy("GET")
            return
        # Debug endpoint to help user diagnose browser-side issues
        if self.path == "/__diag__":
            self._diag()
            return
        # File download endpoint for sharing files (e.g. Supabase frontend bundle)
        if self.path.startswith("/downloads/"):
            self._serve_download()
            return
        self._serve_static()

    def _diag(self):
        """Return diagnostic info as JSON."""
        import platform, sys
        info = {
            "server": "fcc-proxy v2",
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "static_dir_exists": os.path.isdir(STATIC_DIR),
            "index_html_exists": os.path.isfile(os.path.join(STATIC_DIR, "index.html")),
            "index_html_size": os.path.getsize(os.path.join(STATIC_DIR, "index.html")) if os.path.isfile(os.path.join(STATIC_DIR, "index.html")) else None,
            "api_health": "unknown",
            "api_host": API_HOST,
            "api_port": API_PORT,
            "proxy_timeout_seconds": API_TIMEOUT,
            "proxy_max_body_mb": PROXY_MAX_BODY_MB,
        }
        # Try to ping API
        try:
            with urllib.request.urlopen(f"http://{API_HOST}:{API_PORT}/api/v1/health", timeout=5) as r:
                info["api_health"] = f"OK {r.status}"
        except Exception as e:
            info["api_health"] = f"FAIL: {e}"
        body = json.dumps(info, indent=2).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def do_HEAD(self):
        if self.path.startswith("/api/"):
            self._proxy("HEAD")
            return
        if self.path.startswith("/downloads/"):
            # HEAD support for downloads - same logic as GET
            self._serve_download()
            return
        # HEAD on static: same as GET but no body
        path = self._safe_path()
        if path is None:
            self.send_error(404)
            return
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            self.send_error(404)
            return
        ctype = self.guess_type(path)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()

    # ---- proxy all other verbs to API ----
    def do_POST(self):    self._maybe_proxy("POST")
    def do_PATCH(self):   self._maybe_proxy("PATCH")
    def do_PUT(self):     self._maybe_proxy("PUT")
    def do_DELETE(self):  self._maybe_proxy("DELETE")
    def do_OPTIONS(self): self._maybe_proxy("OPTIONS")

    def _maybe_proxy(self, method):
        if self.path.startswith("/api/"):
            self._proxy(method)
        else:
            self.send_error(404)

    # ---- static file handler ----
    def _serve_download(self):
        """Serve files from /home/ubuntu/fcc_export for download/sharing."""
        import urllib.parse
        rel = urllib.parse.unquote(self.path[len("/downloads/"):])
        # Strip path traversal
        if ".." in rel or rel.startswith("/"):
            self.send_error(403, "Forbidden")
            return
        path = os.path.join(DOWNLOAD_DIR, rel)
        print(f"  resolved path={path}, exists={os.path.isfile(path)}")
        if not os.path.isfile(path):
            self.send_error(404, "File not found")
            return
        size = os.path.getsize(path)
        ext = os.path.splitext(rel)[1].lower()
        ct = {
            ".zip": "application/zip",
            ".sql": "text/plain",
            ".js": "application/javascript",
            ".html": "text/html",
            ".txt": "text/plain",
            ".md": "text/markdown",
        }.get(ext, "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", f"attachment; filename={rel}")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        with open(path, "rb") as f:
            while True:
                chunk = f.read(64 * 1024)
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    break

    def _serve_static(self):
        # Mount /field/* → serve field-input web app (Supabase-first)
        if self.path.split("?", 1)[0].startswith("/field"):
            self._serve_field_static()
            return
        path = self._safe_path()
        if path is None:
            self.send_error(404)
            return
        # Root path or directory → serve index.html
        if self.path == "/" or self.path == "" or os.path.isdir(path):
            path = os.path.join(STATIC_DIR, "index.html")
        if not os.path.isfile(path):
            self.send_error(404)
            return
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            self.send_error(404)
            return
        ctype = self.guess_type(path)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        # Use chunked transfer encoding — no Content-Length needed.
        # This avoids ERR_CONTENT_LENGTH_MISMATCH if a middlebox modifies
        # the body (corporate proxy / antivirus) without updating headers.
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        # Send in chunks
        chunk_size = 8192
        for i in range(0, len(data), chunk_size):
            chunk = data[i:i+chunk_size]
            self.wfile.write(f"{len(chunk):x}\r\n".encode())
            self.wfile.write(chunk)
            self.wfile.write(b"\r\n")
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def _safe_path(self):
        # Strip query string
        p = self.path.split("?", 1)[0]
        # Disallow path traversal
        p = urllib.parse.unquote(p)
        rel = p.lstrip("/")
        full = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if not full.startswith(STATIC_DIR):
            return None
        return full

    def _safe_field_path(self):
        """Resolve a /field/* request to a file inside FIELD_STATIC_DIR.
        Strips the leading /field/ prefix and prevents path traversal."""
        p = self.path.split("?", 1)[0]
        p = urllib.parse.unquote(p)
        # Strip leading "/field" or "/field/" prefix
        if p == "/field" or p == "/field/":
            rel = "index.html"
        else:
            if p.startswith("/field/"):
                rel = p[len("/field/"):]
            elif p.startswith("/field"):
                rel = p[len("/field"):]
            else:
                rel = p.lstrip("/")
        full = os.path.normpath(os.path.join(FIELD_STATIC_DIR, rel))
        if not full.startswith(FIELD_STATIC_DIR):
            return None
        # If the resolved path is a directory (or empty), fall back to index.html
        if full == FIELD_STATIC_DIR or os.path.isdir(full):
            full = os.path.join(FIELD_STATIC_DIR, "index.html")
        return full

    def _serve_field_static(self):
        """Serve files from FIELD_STATIC_DIR (the field-input web app)."""
        path = self._safe_field_path()
        if path is None or not os.path.isfile(path):
            self.send_error(404)
            return
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            self.send_error(404)
            return
        ctype = self.guess_type(path)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        # Use chunked transfer encoding — consistent with main static handler
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        chunk_size = 8192
        for i in range(0, len(data), chunk_size):
            chunk = data[i:i+chunk_size]
            self.wfile.write(f"{len(chunk):x}\r\n".encode())
            self.wfile.write(chunk)
            self.wfile.write(b"\r\n")
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    # ---- proxy to API ----
    def _read_request_body(self):
        """Read Content-Length or chunked request bodies with a hard safety limit."""
        max_bytes = PROXY_MAX_BODY_MB * 1024 * 1024
        transfer = str(self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in transfer:
            chunks = []
            total = 0
            while True:
                line = self.rfile.readline(128)
                if not line:
                    raise ValueError("request body terputus")
                size_token = line.split(b";", 1)[0].strip()
                try:
                    size = int(size_token, 16)
                except ValueError as exc:
                    raise ValueError("chunked request tidak valid") from exc
                if size == 0:
                    # Consume optional trailer headers through the final empty line.
                    while True:
                        trailer = self.rfile.readline(8192)
                        if trailer in (b"\r\n", b"\n", b""):
                            break
                    break
                total += size
                if total > max_bytes:
                    raise OverflowError(f"request melebihi {PROXY_MAX_BODY_MB} MB")
                chunks.append(self.rfile.read(size))
                ending = self.rfile.read(2)
                if ending != b"\r\n":
                    raise ValueError("chunked request terminator tidak valid")
            return b"".join(chunks)

        n = int(self.headers.get("Content-Length", 0) or 0)
        if n > max_bytes:
            raise OverflowError(f"request melebihi {PROXY_MAX_BODY_MB} MB")
        return self.rfile.read(n) if n else None

    def _proxy(self, method):
        try:
            body = self._read_request_body()
        except OverflowError as exc:
            payload = json.dumps({"detail": str(exc)}).encode()
            self.send_response(413)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
            return
        except ValueError as exc:
            payload = json.dumps({"detail": f"Request body tidak valid: {exc}"}).encode()
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
            return
        url = f"http://{API_HOST}:{API_PORT}{self.path}"
        fwd = {}
        skip = {"host", "content-length", "transfer-encoding", "connection", "accept-encoding"}
        for k, v in self.headers.items():
            if k.lower() in skip:
                continue
            fwd[k] = v
        req = urllib.request.Request(url, data=body, method=method, headers=fwd)
        try:
            with urllib.request.urlopen(req, timeout=API_TIMEOUT) as r:
                resp_body = r.read()
                self._write_proxy_response(r.status, r.headers, resp_body)
        except urllib.error.HTTPError as e:
            resp_body = e.read()
            self._write_proxy_response(e.code, e.headers, resp_body)
        except Exception as e:
            err = f'{{"detail":"Proxy error: {e}"}}'.encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(err)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(err)
            self.wfile.flush()

    def _write_proxy_response(self, status, headers, body):
        self.send_response(status)
        for k, v in headers.items():
            if k.lower() in ("content-type", "set-cookie"):
                if k.lower() == "set-cookie":
                    # Make cookie Path=/ so it applies to all of our origin
                    if "Path=" in v:
                        v = v.replace("Path=/api/", "Path=/")
                    else:
                        v = v + "; Path=/"
                self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    # ---- helpers ----
    def guess_type(self, path):
        if path.endswith(".html"): return "text/html; charset=utf-8"
        if path.endswith(".js"):   return "application/javascript; charset=utf-8"
        if path.endswith(".css"):  return "text/css; charset=utf-8"
        if path.endswith(".json"): return "application/json; charset=utf-8"
        if path.endswith(".svg"):  return "image/svg+xml"
        if path.endswith(".png"):  return "image/png"
        if path.endswith(".jpg") or path.endswith(".jpeg"): return "image/jpeg"
        if path.endswith(".ico"):  return "image/x-icon"
        return "application/octet-stream"

    def log_message(self, fmt, *args):
        # Quieter log
        pass


class ThreadingHTTPServer(http.server.ThreadingHTTPServer):
    """HTTP/1.1 server with explicit socket close after each request."""
    pass


if __name__ == "__main__":
    port = int(os.environ.get("FCC_STATIC_PORT", 8765))
    s = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"FCC static+proxy on :{port}, API → :{API_PORT}", flush=True)
    s.serve_forever()
