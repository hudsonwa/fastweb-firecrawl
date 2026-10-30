"""Test servers for the real-Chrome egress test stack. origin: serves www/ + /redir?to=URL (302). target: logs every TCP request (80, 8081, 443)
and every UDP datagram (3478) to /log/target.log, answers permissive CORS + keep-alive."""

import http.server
import os
import socket
import sys
import threading
import time
import urllib.parse

LOG = "/log/target.log"


def log(s):
    with open(LOG, "a") as f:
        f.write(f"{time.time():.3f} {s}\n")


class Origin(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory="/www", **k)

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        if u.path == "/redir":
            to = urllib.parse.parse_qs(u.query)["to"][0]
            self.send_response(302)
            self.send_header("Location", to)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        return super().do_GET()

    do_POST = do_GET


class Target(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _any(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            self.rfile.read(n)
        log(f"tcp:{self.server.server_address[1]} {self.command} {self.path} host={self.headers.get('Host')}")
        b = b"<html><body>TARGET-SECRET " + b"x" * 3000 + b"</body></html>"
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    do_GET = do_POST = do_HEAD = do_OPTIONS = _any

    def log_message(self, *a):
        pass


def raw443():
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", 443))
    s.listen(64)
    while True:
        c, a = s.accept()
        log(f"tcp:443 connect from {a[0]}")
        c.close()


def udp():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("0.0.0.0", 3478))
    while True:
        d, a = s.recvfrom(4096)
        log(f"udp:3478 {len(d)}B from {a[0]}")


if sys.argv[1] == "origin":
    http.server.ThreadingHTTPServer(("0.0.0.0", 8080), Origin).serve_forever()
else:
    open(LOG, "a").close()
    for p in (80, 8081):
        threading.Thread(
            target=http.server.ThreadingHTTPServer(("0.0.0.0", p), Target).serve_forever, daemon=True
        ).start()
    threading.Thread(target=raw443, daemon=True).start()
    udp()
