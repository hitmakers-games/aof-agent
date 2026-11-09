"""Bounded HTTPS CONNECT proxy. Routing and fail-closed rules belong to vpn.py."""
import ipaddress
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import select
import socket
import socketserver
import threading
import time

STATUS = Path("/status/health.json")


def allowed_hosts():
    return {h.strip().lower() for h in os.environ.get(
        "PROXY_HOSTS", "api.openai.com,api.telegram.org,api.ipify.org"
    ).split(",") if h.strip()}


def destination(authority):
    host, separator, port = authority.rpartition(":")
    if not separator or port != "443" or host.lower() not in allowed_hosts():
        raise ValueError("destination denied")
    addresses = socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError("non-public destination denied")
    # Connect to the validated address without a second DNS lookup.
    return addresses[0][4]


class Proxy(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(10)
        try:
            request = self.rfile.readline(4097)
            parts = request.decode("ascii").strip().split()
            if len(request) > 4096 or len(parts) != 3 or parts[0] != "CONNECT" or parts[2] not in ("HTTP/1.0", "HTTP/1.1"):
                raise ValueError("invalid request")
            total = len(request)
            for _ in range(64):
                line = self.rfile.readline(4097)
                total += len(line)
                if not line or len(line) > 4096 or total > 16384:
                    raise ValueError("invalid headers")
                if line == b"\r\n":
                    break
            else:
                raise ValueError("too many headers")
            target = destination(parts[1])
        except (ValueError, UnicodeError, OSError):
            self.connection.sendall(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
            return
        try:
            with socket.create_connection(target, timeout=10) as upstream:
                self.connection.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                # Unbuffered reads prevent consuming TLS bytes into rfile's buffer.
                self.relay(upstream)
        except OSError:
            return

    rbufsize = 0

    def relay(self, upstream):
        streams = (self.connection, upstream)
        for stream in streams:
            stream.settimeout(10)
        deadline = time.monotonic() + 3600
        while time.monotonic() < deadline:
            readable, _, _ = select.select(streams, (), (), 60)
            if not readable:
                return
            for source in readable:
                data = source.recv(65536)
                if not data:
                    return
                target = upstream if source is self.connection else self.connection
                target.sendall(data)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address):
        self.slots = threading.BoundedSemaphore(64)
        super().__init__(address, Proxy)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        # Do not log client payloads, destinations, or credentials.
        pass


class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/health":
            self.send_error(404)
            return
        try:
            data = json.loads(STATUS.read_text())
            # This endpoint contains operational state only, never configuration.
            allowed = ("heartbeat", "ready", "phase", "profile", "country", "config_id",
                       "generation", "failed_attempts", "probes", "egress_ip")
            body = json.dumps({k: data[k] for k in allowed if k in data}).encode()
        except (OSError, ValueError):
            self.send_error(503)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    # A fresh pod must install its egress firewall before accepting any clients.
    while not STATUS.exists():
        time.sleep(0.2)
    health = ThreadingHTTPServer(("0.0.0.0", 8889), Health)
    threading.Thread(target=health.serve_forever, daemon=True).start()
    with Server(("0.0.0.0", 8888)) as server:
        server.serve_forever()
