#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Проверка HTTPS-прокси hideip.me для HDrezka/mpv.

Один прокси проверяется целиком: сайт, Anubis, CDN и небольшой фрагмент
медиа. Проверки выполняются строго в 3 потока.
"""

from __future__ import annotations

import queue
import threading
import time
import urllib.parse
from typing import Dict, List, Optional

import requests

from PyQt6.QtCore import QThread, pyqtSignal

import hdrezka_core as core
from proxy_bridge import ProxyBridge

requests.packages.urllib3.disable_warnings()

STAGE_TIMEOUT = 12.0
TOTAL_TIMEOUT = 30.0
STOP_GRACE = 4.5
CONCURRENCY = 3
_CHECK_SLOTS = threading.BoundedSemaphore(CONCURRENCY)

STATUS_VIDEO = "video"
STATUS_SITE = "site"
STATUS_CDN = "cdn"
STATUS_ERROR = "error"
STATUS_STALE = "stale"

STATUS_LABELS = {
    STATUS_VIDEO: "видео",
    STATUS_SITE: "только сайт",
    STATUS_CDN: "CDN пусто",
    STATUS_ERROR: "ошибка",
    STATUS_STALE: "устарело",
}


class ScanCancelled(Exception):
    """Проверка прервана пользователем."""


def _check_cancel(stop: threading.Event):
    if stop.is_set():
        raise ScanCancelled()


def _media_headers(target: dict) -> dict:
    referer = str(target.get("url") or "")
    base = str(target.get("base") or core.DEFAULT_BASE)
    return {
        "User-Agent": core.USER_AGENT,
        "Referer": referer,
        "Origin": base,
        "Accept": "*/*",
    }


def _read_media_chunk(
    session: requests.Session,
    url: str,
    headers: dict,
    stop: threading.Event,
) -> bool:
    _check_cancel(stop)
    try:
        resp = session.get(
            url,
            headers={**headers, "Range": "bytes=0-1023"},
            timeout=STAGE_TIMEOUT,
            stream=True,
            allow_redirects=True,
        )
    except Exception:
        return False
    try:
        if resp.status_code not in (200, 206):
            return False
        try:
            data = resp.raw.read(1024, decode_content=True)
        except Exception:
            data = resp.content[:1024]
        return bool(data)
    finally:
        try:
            resp.close()
        except Exception:
            pass


def _test_hls(
    session: requests.Session,
    url: str,
    headers: dict,
    stop: threading.Event,
) -> bool:
    for _ in range(3):
        _check_cancel(stop)
        try:
            resp = session.get(
                url, headers=headers, timeout=STAGE_TIMEOUT,
                allow_redirects=True,
            )
        except Exception:
            return False
        try:
            if resp.status_code not in (200, 206):
                return False
            text = resp.text or ""
        finally:
            try:
                resp.close()
            except Exception:
                pass
        lines = [
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        if not lines:
            return False
        target = urllib.parse.urljoin(url, lines[0])
        if ".m3u8" in target.lower():
            url = target
            continue
        return _read_media_chunk(session, target, headers, stop)
    return False


def _test_media(
    bridge_url: str,
    target: dict,
    quality: core.Quality,
    stop: threading.Event,
) -> str:
    headers = _media_headers(target)
    session = requests.Session()
    session.verify = False
    session.proxies = {"http": bridge_url, "https": bridge_url}
    try:
        if quality.hls and _test_hls(session, quality.hls, headers, stop):
            return "HLS"
        if quality.mp4 and _read_media_chunk(
            session, quality.mp4, headers, stop
        ):
            return "MP4"
        return ""
    finally:
        session.close()


def check_proxy(
    proxy_url: str,
    target: dict,
    stop: threading.Event,
    register_bridge=None,
) -> dict:
    """Проверяет один прокси. Возвращает dict со статусом и таймингами."""
    started = time.monotonic()
    bridge: Optional[ProxyBridge] = None
    client: Optional[core.HDrezkaClient] = None
    result: Dict[str, object] = {
        "status": STATUS_ERROR,
        "error": "",
        "latency_ms": None,
        "site_ms": None,
        "video_ms": None,
        "media": "",
        "checked_at": int(time.time()),
    }
    try:
        _check_cancel(stop)
        bridge = ProxyBridge(proxy_url)
        if register_bridge is not None:
            register_bridge(bridge)
        _check_cancel(stop)

        base = str(target.get("base") or core.DEFAULT_BASE)
        title_url = str(target.get("url") or "")
        if not title_url:
            raise core.HDrezkaError("не задан тестовый тайтл")

        client = core.HDrezkaClient(
            base=base, proxy=bridge.url, timeout=STAGE_TIMEOUT
        )
        info = client.fetch_title(title_url)
        site_ms = int((time.monotonic() - started) * 1000)
        result["site_ms"] = site_ms
        _check_cancel(stop)

        tid = str(target.get("translator_id") or "")
        if not tid:
            translators = list(info.translators or [])
            normal = [t for t in translators if not t.premium]
            picked = (normal or translators or [None])[0]
            tid = picked.id if picked is not None else ""
        if not tid:
            result["status"] = STATUS_SITE
            result["error"] = "нет доступных переводов"
            return result

        sid = str(target.get("season") or "1")
        eid = str(target.get("episode") or "1")
        if not info.is_series:
            sid = eid = "1"

        try:
            stream = client.fetch_stream(
                info.post_id, tid, sid, eid, info.is_series,
                title_url,
            )
        except core.HDrezkaError as e:
            message = str(e)
            low = message.lower()
            if (
                "пуст" in low
                or "cdn" in low
                or "url" in low
                or "сери" in low
            ):
                result["status"] = STATUS_CDN
            else:
                result["status"] = STATUS_ERROR
            result["error"] = message
            return result

        chosen = core.choose_quality(stream.qualities, "360")
        if chosen is None:
            result["status"] = STATUS_CDN
            result["error"] = "нет доступных качеств"
            return result
        if time.monotonic() - started > TOTAL_TIMEOUT:
            result["status"] = STATUS_SITE
            result["error"] = "превышен лимит времени"
            return result

        _check_cancel(stop)
        try:
            media = _test_media(bridge.url, target, chosen, stop)
        except ScanCancelled:
            raise
        except Exception:
            media = ""
        if not media:
            result["status"] = STATUS_SITE
            result["error"] = "медиа недоступно"
            return result

        total_ms = int((time.monotonic() - started) * 1000)
        result.update(
            status=STATUS_VIDEO,
            media=media,
            latency_ms=total_ms,
            video_ms=total_ms,
            error="",
            checked_at=int(time.time()),
        )
        return result
    except ScanCancelled:
        raise
    except Exception as e:
        result["status"] = STATUS_ERROR
        result["error"] = str(e)
        result["checked_at"] = int(time.time())
        return result
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
        if bridge is not None:
            try:
                bridge.close()
            except Exception:
                pass


class ProxyScanWorker(QThread):
    """Фоновый сканер: 3 потока, пауза/продолжение."""

    checking = pyqtSignal(str)
    result = pyqtSignal(str, object)
    done = pyqtSignal(str)

    def __init__(
        self,
        proxies: List[str],
        target: dict,
        parent=None,
    ):
        super().__init__(parent)
        self._target = dict(target or {})
        self._queue: "queue.Queue[str]" = queue.Queue()
        for url in proxies:
            if url:
                self._queue.put(str(url))
        self._stop = threading.Event()
        self._reason = ""
        self._active: Dict[str, ProxyBridge] = {}
        self._lock = threading.Lock()
        self._run_found = 0
        self._threads: List[threading.Thread] = []

    def stop(self, reason: str = "stopped"):
        if not self._stop.is_set():
            self._reason = reason or "stopped"
            self._stop.set()
        self._close_active()

    def _close_active(self):
        with self._lock:
            bridges = list(self._active.values())
            self._active.clear()
        for bridge in bridges:
            try:
                bridge.close()
            except Exception:
                pass

    def _register(self, url: str, bridge: ProxyBridge):
        with self._lock:
            self._active[url] = bridge

    def _unregister(self, url: str):
        with self._lock:
            self._active.pop(url, None)

    def _worker(self):
        while not self._stop.is_set():
            try:
                url = self._queue.get_nowait()
            except queue.Empty:
                return
            if self._stop.is_set():
                return
            # Глобальный лимит: даже если старый сканер ещё завершает
            # проверки, одновременно работают не более трёх.
            with _CHECK_SLOTS:
                if self._stop.is_set():
                    return
                self.checking.emit(url)
                result = None
                try:
                    result = check_proxy(
                        url, self._target, self._stop,
                        lambda bridge, u=url: self._register(u, bridge),
                    )
                except ScanCancelled:
                    return
                except Exception as e:
                    result = {
                        "status": STATUS_ERROR,
                        "error": str(e),
                        "latency_ms": None,
                        "site_ms": None,
                        "video_ms": None,
                        "media": "",
                        "checked_at": int(time.time()),
                    }
                finally:
                    self._unregister(url)
            if self._stop.is_set():
                return
            self.result.emit(url, result)
            if result.get("status") == STATUS_VIDEO:
                with self._lock:
                    self._run_found += 1
                    found = self._run_found
                if found >= 2:
                    self.stop("found")
                    return

    def run(self):
        self._threads = [
            threading.Thread(
                target=self._worker,
                name=f"proxy-check-{i + 1}",
                daemon=True,
            )
            for i in range(CONCURRENCY)
        ]
        for thread in self._threads:
            thread.start()

        deadline = None
        while any(t.is_alive() for t in self._threads):
            if self._stop.is_set() and deadline is None:
                deadline = time.monotonic() + STOP_GRACE
            if deadline is not None and time.monotonic() >= deadline:
                break
            time.sleep(0.05)

        reason = self._reason
        if not reason:
            reason = "stopped" if self._stop.is_set() else "finished"
        self.done.emit(reason)
