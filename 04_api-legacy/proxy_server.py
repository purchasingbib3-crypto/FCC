#!/usr/bin/env python3
"""Simple HTTP server that serves frontend + proxies /api/* to FastAPI on port 8000."""
import http.server
import socketserver
import urllib.request
import os
import threading

FRONTEND_DIR = '/home/ubuntu/fuel-control-center/preview-static'
API_HOST = 'http://127.0.0.1:8000'
PORT = 80

class ProxyHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=FRONTEND_DIR, **kwargs)

    def send_head(self):
        """Override to inject cache-control headers BEFORE Content-Length."""
        path = self.translate_path(self.path)
        if os.path.isdir(path):
            parts = self.path.split('/')
            parts[-1] = 'index.html'
            path = self.translate_path('/'.join(parts))
        ctype = self.guess_type(path)
        if ctype.startswith('text/'):
            ctype += '; charset=utf-8'
        try:
            f = open(path, 'rb')
        except OSError:
            self.send_error(404, 'File not found')
            return None
        try:
            self.send_response(200)
            self.send_header('Content-Type', ctype)
            self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0')
            self.send_header('Pragma', 'no-cache')
            self.send_header('Expires', '0')
            fs = os.fstat(f.fileno())
            self.send_header('Content-Length', str(fs.st_size))
            self.send_header('Last-Modified', self.date_time_string(fs.st_mtime))
            self.end_headers()
            return f
        except:
            f.close()
            raise

    def do_GET(self):
        if self.path.startswith('/api/'):
            self.proxy_request()
        else:
            super().do_GET()

    def do_POST(self):
        if self.path.startswith('/api/'):
            self.proxy_request()
        else:
            self.send_error(405)

    def do_OPTIONS(self):
        if self.path.startswith('/api/'):
            self.send_response(200)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'X-API-Key, Authorization, Content-Type')
            self.end_headers()
        else:
            super().do_OPTIONS()

    def proxy_request(self):
        url = f"{API_HOST}{self.path}"
        try:
            headers = {}
            for key in ['X-API-Key', 'Authorization', 'Content-Type']:
                if key in self.headers:
                    headers[key] = self.headers[key]
            # Read body for POST
            body = None
            if self.command == 'POST' or self.command == 'PUT' or self.command == 'PATCH':
                clen = int(self.headers.get('Content-Length', 0) or 0)
                if clen > 0:
                    body = self.rfile.read(clen)
            req = urllib.request.Request(url, data=body, headers=headers, method=self.command)
            with urllib.request.urlopen(req) as resp:
                content = resp.read()
                self.send_response(resp.status)
                self.send_header('Content-Type', resp.headers.get('Content-Type', 'application/json'))
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(content)
        except urllib.error.HTTPError as e:
            content = e.read()
            self.send_response(e.code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(content)
        except Exception as e:
            self.send_response(502)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(f'{{"error":"{str(e)}"}}'.encode())

if __name__ == '__main__':
    socketserver.TCPServer.allow_reuse_address = True
with socketserver.TCPServer(('0.0.0.0', PORT), ProxyHandler) as httpd:
        print(f"Serving frontend + API proxy on port {PORT}")
        httpd.serve_forever()
