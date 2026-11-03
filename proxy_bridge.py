#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Локальный HTTP-прокси для mpv.

mpv/ffmpeg понимает только `--http-proxy=http://...`. Схемы socks5://,
socks4:// и https:// он молча игнорирует. Этот модуль поднимает локальный
HTTP-прокси на 127.0.0.1, а сам ходит к внешнему прокси по нужному протоколу.

Сертификаты нигде не проверяются (как и во всех запросах приложения).
"""

from __future__ import annotations

import base64
import os
import select
import socket
import ssl
import threading
from urllib.parse import unquote, urlsplit


def _log(*args):
    if os.environ.get("HDREZKA_LOG"):
        print("[proxy-bridge]", *args, flush=True)


def _recv_until(sock, marker, limit=256 * 1024):
    data = b""
    while marker not in data:
        chunk = sock.recv(8192)
        if not chunk:
            break
        data += chunk
        if len(data) > limit:
            raise OSError("too large request")
    return data


def _recv_exact(sock, size):
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise OSError("unexpected eof")
        data += chunk
    return data


def _is_ip(host):
    try:
        socket.inet_aton(host)
        return True
    except OSError:
        return False


def _parse_host_port(text, default_port=80):
    if text.startswith("["):
        host, _, rest = text[1:].partition("]")
        port = int(rest[1:]) if rest.startswith(":") else default_port
        return host, port
    if ":" in text:
        host, port = text.rsplit(":", 1)
        return host, int(port)
    return text, default_port


def _relay(a, b, stop):
    sockets = [a, b]
    try:
        while not stop.is_set():
            try:
                ready, _, _ = select.select(sockets, [], [], 1.0)
            except (OSError, ValueError):
                return
            for src in ready:
                try:
                    chunk = src.recv(65536)
                except OSError:
                    return
                if not chunk:
                    return
                dst = b if src is a else a
                try:
                    dst.sendall(chunk)
                except OSError:
                    return
    finally:
        for s in sockets:
            try:
                s.close()
            except OSError:
                pass


class ProxyBridge:
    """HTTP-прокси на localhost, пересылающий трафик во внешний прокси."""

    def __init__(self, proxy_url: str, host: str = "127.0.0.1"):
        raw = (proxy_url or "").strip()
        if "://" not in raw:
            raw = "http://" + raw
        parts = urlsplit(raw)
        self.scheme = (parts.scheme or "http").lower()
        if self.scheme not in (
            "http", "https", "socks", "socks4", "socks4a",
            "socks5", "socks5h",
        ):
            raise ValueError(f"unsupported proxy scheme: {self.scheme}")
        self.host = parts.hostname
        if not self.host:
            raise ValueError("proxy host is empty")
        default_port = 443 if self.scheme == "https" else (
            1080 if self.scheme.startswith("socks") else 80
        )
        self.proxy_port = int(parts.port or default_port)
        self.username = unquote(parts.username or "")
        self.password = unquote(parts.password or "")
        self._stop = threading.Event()
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind((host, 0))
        self._srv.listen(64)
        self.port = self._srv.getsockname()[1]
        self.url = f"http://{host}:{self.port}"
        self._clients = set()
        self._clients_lock = threading.Lock()
        self._thread = threading.Thread(
            target=self._accept_loop, name="proxy-bridge", daemon=True
        )
        self._thread.start()
        _log("started", self.url, "->", self.scheme, self.host, self.proxy_port)

    @property
    def alive(self):
        return not self._stop.is_set() and self._thread.is_alive()

    def close(self):
        self._stop.set()
        try:
            self._srv.close()
        except OSError:
            pass
        with self._clients_lock:
            clients = list(self._clients)
            self._clients.clear()
        for sock in clients:
            try:
                sock.close()
            except OSError:
                pass
        if self._thread.is_alive():
            self._thread.join(timeout=2)
        _log("stopped")

    # ------------------------------------------------------------------
    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                conn, _addr = self._srv.accept()
            except OSError:
                return
            with self._clients_lock:
                self._clients.add(conn)
            threading.Thread(
                target=self._handle, args=(conn,), daemon=True
            ).start()

    def _register(self, sock):
        with self._clients_lock:
            self._clients.add(sock)

    def _unregister(self, sock):
        with self._clients_lock:
            self._clients.discard(sock)
        try:
            sock.close()
        except OSError:
            pass

    def _handle(self, client):
        upstream = None
        try:
            head = _recv_until(client, b"\r\n\r\n")
            if not head:
                return
            raw_headers, _, rest = head.partition(b"\r\n\r\n")
            lines = raw_headers.decode("iso-8859-1").split("\r\n")
            if not lines or not lines[0]:
                return
            parts = lines[0].split(" ", 2)
            if len(parts) < 3:
                return
            method, target, version = parts
            forwarded = self._forward_headers(lines[1:])

            if method.upper() == "CONNECT":
                host, port = _parse_host_port(target, 443)
                upstream = self._open_to(host, port)
                try:
                    client.sendall(
                        b"HTTP/1.1 200 Connection established\r\n"
                        b"Proxy-Agent: HDrezka\r\n\r\n"
                    )
                except OSError:
                    return
                if rest:
                    upstream.sendall(rest)
                _relay(client, upstream, self._stop)
                return

            uri = urlsplit(target)
            if not uri.scheme:
                # Уже origin-form: клиент считает, что говорит с origin.
                host = uri.hostname or ""
                port = uri.port or 80
                path = target
                upstream = self._open_to(host, port)
                request = (
                    f"{method} {path} {version}\r\n".encode("iso-8859-1")
                    + forwarded
                    + b"\r\n"
                )
                upstream.sendall(request)
                if rest:
                    upstream.sendall(rest)
                _relay(client, upstream, self._stop)
                return

            host = uri.hostname or ""
            port = uri.port or (443 if uri.scheme == "https" else 80)
            path = uri.path or "/"
            if uri.query:
                path += "?" + uri.query

            if self.scheme in ("http", "https") and uri.scheme != "https":
                upstream = self._open_proxy_socket()
                request = (
                    f"{method} {target} {version}\r\n".encode("iso-8859-1")
                    + forwarded
                    + self._proxy_auth()
                    + b"\r\n"
                )
                upstream.sendall(request)
                if rest:
                    upstream.sendall(rest)
                _relay(client, upstream, self._stop)
                return

            if self.scheme in ("http", "https"):
                upstream = self._open_proxy_socket()
                connect = (
                    f"CONNECT {host}:{port} HTTP/1.1\r\n"
                    f"Host: {host}:{port}\r\n"
                ).encode("iso-8859-1")
                upstream.sendall(
                    connect + forwarded + self._proxy_auth() + b"\r\n"
                )
                reply = _recv_until(upstream, b"\r\n\r\n")
                if not reply.startswith(b"HTTP/"):
                    raise OSError("proxy CONNECT failed")
                status = reply.split(b" ", 2)[1:2]
                if status and status[0] not in (b"200", b"201"):
                    raise OSError(
                        "proxy CONNECT status " + status[0].decode()
                    )
                request = (
                    f"{method} {path} {version}\r\n".encode("iso-8859-1")
                    + forwarded
                    + b"\r\n"
                )
                upstream.sendall(request)
                if rest:
                    upstream.sendall(rest)
                _relay(client, upstream, self._stop)
                return

            # SOCKS: подключаемся к origin напрямую и шлём origin-form.
            upstream = self._open_to(host, port)
            request = (
                f"{method} {path} {version}\r\n".encode("iso-8859-1")
                + forwarded
                + b"\r\n"
            )
            upstream.sendall(request)
            if rest:
                upstream.sendall(rest)
            _relay(client, upstream, self._stop)
        except Exception as e:
            _log("client error:", e)
            try:
                client.sendall(
                    b"HTTP/1.1 502 Bad Gateway\r\n"
                    b"Content-Length: 0\r\nConnection: close\r\n\r\n"
                )
            except OSError:
                pass
        finally:
            self._unregister(client)
            if upstream is not None and upstream is not client:
                self._unregister(upstream)

    # ------------------------------------------------------------------
    @staticmethod
    def _forward_headers(lines):
        out = []
        for line in lines:
            if not line:
                continue
            low = line.lower()
            if low.startswith("proxy-connection:"):
                continue
            if low.startswith("proxy-authorization:"):
                continue
            out.append(line)
        return ("\r\n".join(out) + "\r\n").encode("iso-8859-1")

    def _proxy_auth(self):
        if not self.username and not self.password:
            return b""
        token = base64.b64encode(
            f"{self.username}:{self.password}".encode("utf-8")
        ).decode("ascii")
        return f"Proxy-Authorization: Basic {token}\r\n".encode("ascii")

    def _open_proxy_socket(self):
        sock = socket.create_connection((self.host, self.proxy_port), 10)
        if self.scheme == "https":
            ctx = ssl._create_unverified_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            sock = ctx.wrap_socket(sock, server_hostname=self.host)
        self._register(sock)
        return sock

    def _open_to(self, host, port):
        """Открывает соединение с host:port через внешний прокси."""
        if self.scheme in ("http", "https"):
            sock = self._open_proxy_socket()
            try:
                auth = self._proxy_auth()
                request = (
                    f"CONNECT {host}:{port} HTTP/1.1\r\n"
                    f"Host: {host}:{port}\r\n"
                ).encode("iso-8859-1") + auth + b"\r\n"
                sock.sendall(request)
                reply = _recv_until(sock, b"\r\n\r\n")
                if not reply.startswith(b"HTTP/"):
                    raise OSError("proxy CONNECT failed")
                status = reply.split(b" ", 2)[1:2]
                if status and status[0] not in (b"200", b"201"):
                    raise OSError(
                        "proxy CONNECT status " + status[0].decode()
                    )
                return sock
            except Exception:
                self._unregister(sock)
                raise
        if self.scheme in ("socks", "socks5", "socks5h"):
            return self._socks5(host, port)
        return self._socks4(host, port)

    # ------------------------------------------------------------------
    def _socks5(self, host, port):
        sock = socket.create_connection((self.host, self.proxy_port), 10)
        self._register(sock)
        try:
            use_auth = bool(self.username or self.password)
            methods = b"\x00\x02" if use_auth else b"\x00"
            sock.sendall(b"\x05" + bytes([len(methods)]) + methods)
            ver, method = _recv_exact(sock, 2)
            if ver != 5:
                raise OSError("bad socks5 version")
            if method == 2:
                if not use_auth:
                    raise OSError("socks5 auth required")
                user = self.username.encode("utf-8")[:255]
                pwd = self.password.encode("utf-8")[:255]
                sock.sendall(
                    b"\x01" + bytes([len(user)]) + user
                    + bytes([len(pwd)]) + pwd
                )
                _ver, status = _recv_exact(sock, 2)
                if status != 0:
                    raise OSError("socks5 auth failed")
            elif method != 0:
                raise OSError("no acceptable socks5 auth method")

            remote_dns = self.scheme == "socks5h"
            if not remote_dns and not _is_ip(host):
                try:
                    host = socket.gethostbyname(host)
                except OSError:
                    remote_dns = True
            if _is_ip(host):
                addr = b"\x01" + socket.inet_aton(host)
            else:
                raw_host = host.encode("idna")
                addr = b"\x03" + bytes([len(raw_host)]) + raw_host
            sock.sendall(
                b"\x05\x01\x00" + addr + int(port).to_bytes(2, "big")
            )
            ver, rep, _rsv, atyp = _recv_exact(sock, 4)
            if ver != 5 or rep != 0:
                raise OSError(f"socks5 connect failed: {rep}")
            if atyp == 1:
                _recv_exact(sock, 4)
            elif atyp == 3:
                ln = _recv_exact(sock, 1)[0]
                _recv_exact(sock, ln)
            elif atyp == 4:
                _recv_exact(sock, 16)
            _recv_exact(sock, 2)
            return sock
        except Exception:
            self._unregister(sock)
            raise

    def _socks4(self, host, port):
        sock = socket.create_connection((self.host, self.proxy_port), 10)
        self._register(sock)
        try:
            user = self.username.encode("utf-8")
            if self.scheme == "socks4a" or not _is_ip(host):
                ip = b"\x00\x00\x00\x01"
                tail = host.encode("idna") + b"\x00"
            else:
                ip = socket.inet_aton(host)
                tail = b""
            request = (
                b"\x04\x01" + int(port).to_bytes(2, "big")
                + ip + user + b"\x00" + tail
            )
            sock.sendall(request)
            reply = _recv_exact(sock, 8)
            if reply[1] != 0x5A:
                raise OSError(f"socks4 connect failed: {reply[1]}")
            return sock
        except Exception:
            self._unregister(sock)
            raise
