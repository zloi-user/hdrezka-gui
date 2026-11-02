#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HDrezka — настольное GUI для просмотра фильмов и сериалов HDrezka.

Сделано по образцу limehd_gui.py:
  * слева каталог/поиск с группами;
  * в центре встроенный mpv (python-mpv/libmpv);
  * справа постер, описание, выбор перевода, сезона, серии и качества;
  * настройки: зеркало, прокси, качество, hwdec;
  * кэш главной страницы для быстрого старта.

Запуск:
    python3 hdrezka_gui.py

Зависимости:
    pip install PyQt6 requests python-mpv Pillow
    sudo apt install mpv libmpv2        # Debian/Ubuntu

API-логика и решение Anubis — в hdrezka_core.py (без браузера).
"""

from __future__ import annotations

import ctypes
import io
import json
import locale
import os
import queue
import sys
import threading
import time
import traceback
import urllib.parse
from datetime import datetime
from typing import Dict, List, Optional

try:
    locale.setlocale(locale.LC_NUMERIC, "C")
except locale.Error:
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from PyQt6.QtCore import (
        QObject, QSettings, QStandardPaths, Qt, QThread, QTimer, QUrl,
        pyqtSignal,
    )
    from PyQt6.QtGui import (
        QBrush, QColor, QKeySequence, QOpenGLContext, QPixmap, QShortcut,
    )
    from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkRequest
    from PyQt6.QtOpenGLWidgets import QOpenGLWidget
    from PyQt6.QtWidgets import (
        QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog,
        QDialogButtonBox, QFrame, QGridLayout, QHBoxLayout, QLabel,
        QLineEdit, QMainWindow, QMessageBox, QPushButton, QSizePolicy,
        QSplitter, QSpinBox, QStyle, QTextEdit, QTreeWidget, QTreeWidgetItem,
        QVBoxLayout, QWidget,
    )
except ImportError:  # pragma: no cover
    sys.exit("Не найден PyQt6. Установите: python3 -m pip install PyQt6")

import hdrezka_core as core

# --------------------------------------------------------------------------
# Константы и настройки
# --------------------------------------------------------------------------

APP_TITLE = "HDrezka"
ORG_NAME = "hdrezka"
APP_NAME = "hdrezka-gui"

QUALITY_OPTIONS = [
    ("Авто (макс. доступное)", "auto"),
    ("1080p", "1080"),
    ("720p", "720"),
    ("480p", "480"),
    ("360p", "360"),
]

SOURCE_OPTIONS = [
    ("Авто (HLS → MP4)", "auto"),
    ("HLS (m3u8)", "hls"),
    ("MP4 (файл)", "mp4"),
]

# Сколько ждать первый сегмент HLS, прежде чем автоматически
# переключиться на прямой MP4 (режим «Авто»).
HLS_START_TIMEOUT_MS = 10000
# Сколько ждать MP4 после фолбэка, прежде чем показать «долго не начинается».
MP4_START_TIMEOUT_MS = 15000

HW_DEC_OPTIONS = [
    ("auto-safe (рекомендуется)", "auto-safe"),
    ("auto", "auto"),
    ("no (только CPU)", "no"),
    ("vaapi", "vaapi"),
    ("vdpau", "vdpau"),
    ("nvdec", "nvdec"),
    ("videotoolbox", "videotoolbox"),
]


def _dbg(*args):
    if os.environ.get("HDREZKA_MPV_LOG"):
        print("[mpv]", *args, file=sys.stderr)


def _log(*args):
    if os.environ.get("HDREZKA_LOG"):
        print("[hdrezka]", *args, file=sys.stderr)


# --------------------------------------------------------------------------
# Вспомогательные функции
# --------------------------------------------------------------------------

def normalize_base(text: str) -> str:
    text = (text or core.DEFAULT_BASE).strip()
    if not text:
        text = core.DEFAULT_BASE
    if "://" not in text:
        text = "https://" + text
    return text.rstrip("/")


def normalize_proxy(text: str) -> Optional[str]:
    text = (text or "").strip()
    if not text:
        return None
    if "://" not in text:
        text = "http://" + text
    return text


def fmt_ts(ts) -> str:
    if not ts:
        return "неизвестно"
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%d.%m %H:%M")
    except (TypeError, ValueError, OSError):
        return "неизвестно"


def fmt_pos(seconds) -> str:
    try:
        total = max(0, int(float(seconds)))
    except (TypeError, ValueError):
        total = 0
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def cache_dir() -> str:
    base = QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.CacheLocation
    )
    if not base:
        base = os.path.expanduser("~/.cache/hdrezka-gui")
    os.makedirs(base, exist_ok=True)
    return base


def cache_file() -> str:
    return os.path.join(cache_dir(), "home.json")


def positions_file() -> str:
    return os.path.join(cache_dir(), "positions.json")


def initials(name: str) -> str:
    text = (name or "").replace("«", "").replace("»", "")
    text = text.replace('"', "").strip()
    words = [w for w in text.split() if w]
    if len(words) >= 2:
        return (words[0][0] + words[1][0]).upper()
    if text:
        return text[:2].upper()
    return "?"


# --------------------------------------------------------------------------
# Фоновые потоки
# --------------------------------------------------------------------------

class CatalogWorker(QThread):
    done = pyqtSignal(int, object)

    def __init__(
        self, client, section: str, page: int, genre: str,
        token: int, parent=None,
    ):
        super().__init__(parent)
        self.client = client
        self.section = section
        self.page = page
        self.genre = genre
        self.token = token

    def run(self):
        try:
            if self.section == "home":
                items = self.client.fetch_home()
                has_more = False
                genres = []
            else:
                items, has_more, genres = self.client.fetch_catalog(
                    self.section, self.page, self.genre
                )
            self.done.emit(
                self.token,
                {
                    "items": items,
                    "has_more": has_more,
                    "page": self.page,
                    "section": self.section,
                    "genre": self.genre,
                    "genres": genres,
                    "error": "",
                },
            )
        except Exception as e:
            traceback.print_exc()
            self.done.emit(
                self.token,
                {
                    "items": [],
                    "has_more": False,
                    "page": self.page,
                    "section": self.section,
                    "genre": self.genre,
                    "genres": [],
                    "error": str(e),
                },
            )


class SearchWorker(QThread):
    done = pyqtSignal(int, object)

    def __init__(self, client, query: str, token: int, parent=None):
        super().__init__(parent)
        self.client = client
        self.query = query
        self.token = token

    def run(self):
        try:
            items = self.client.search(self.query)
            self.done.emit(
                self.token,
                {"items": items, "query": self.query, "error": ""},
            )
        except Exception as e:
            traceback.print_exc()
            self.done.emit(
                self.token,
                {"items": [], "query": self.query, "error": str(e)},
            )


class TitleWorker(QThread):
    done = pyqtSignal(str, object)

    def __init__(self, client, url: str, parent=None):
        super().__init__(parent)
        self.client = client
        self.url = url

    def run(self):
        try:
            info = self.client.fetch_title(self.url)
            self.done.emit(self.url, {"info": info, "error": ""})
        except Exception as e:
            traceback.print_exc()
            self.done.emit(self.url, {"info": None, "error": str(e)})


class EpisodesWorker(QThread):
    done = pyqtSignal(str, str, int, object)

    def __init__(
        self, client, post_id: str, translator_id: str, referer: str,
        token: int, parent=None,
    ):
        super().__init__(parent)
        self.client = client
        self.post_id = post_id
        self.translator_id = translator_id
        self.referer = referer
        self.token = token

    def run(self):
        try:
            seasons, episodes, premium = self.client.fetch_episodes(
                self.post_id, self.translator_id, self.referer
            )
            self.done.emit(
                self.referer,
                self.translator_id,
                self.token,
                {
                    "seasons": seasons,
                    "episodes": episodes,
                    "premium": premium,
                    "error": "",
                },
            )
        except Exception as e:
            traceback.print_exc()
            self.done.emit(
                self.referer,
                self.translator_id,
                self.token,
                {"error": str(e)},
            )


class StreamWorker(QThread):
    done = pyqtSignal(int, object)

    def __init__(
        self, client, post_id: str, translator_id: str, season: str,
        episode: str, is_series: bool, quality: str, referer: str,
        token: int, parent=None,
    ):
        super().__init__(parent)
        self.client = client
        self.post_id = post_id
        self.translator_id = translator_id
        self.season = season
        self.episode = episode
        self.is_series = is_series
        self.quality = quality
        self.referer = referer
        self.token = token

    def run(self):
        try:
            info = self.client.fetch_stream(
                self.post_id,
                self.translator_id,
                self.season,
                self.episode,
                self.is_series,
                self.referer,
            )
            chosen = core.choose_quality(info.qualities, self.quality)
            if chosen is None:
                raise core.HDrezkaError("нет доступных качеств")
            self.done.emit(
                self.token,
                {"stream": info, "chosen": chosen, "error": ""},
            )
        except Exception as e:
            traceback.print_exc()
            self.done.emit(self.token, {"error": str(e)})


class CheckWorker(QThread):
    done = pyqtSignal(bool, str)

    def __init__(self, base: str, proxy: Optional[str], parent=None):
        super().__init__(parent)
        self.base = base
        self.proxy = proxy

    def run(self):
        client = None
        try:
            client = core.HDrezkaClient(self.base, self.proxy, 20.0)
            items = client.fetch_home()
            self.done.emit(
                True, f"Сайт доступен. Тайтлов на главной: {len(items)}"
            )
        except Exception as e:
            self.done.emit(False, str(e))
        finally:
            if client is not None:
                client.close()


_PROXY_WORKERS = set()


def _forget_worker(worker):
    _PROXY_WORKERS.discard(worker)
    worker.deleteLater()


# --------------------------------------------------------------------------
# mpv: render API в QOpenGLWidget + очередь команд
# --------------------------------------------------------------------------

class VideoOverlay(QLabel):
    """Полупрозрачная плашка поверх видео: подготовка/буферизация."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setWordWrap(True)
        self.setMaximumWidth(480)
        self.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, True
        )
        self.setStyleSheet(
            "QLabel { background: #1c1c1c; color: #ffffff; "
            "font-size: 15px; font-weight: 600; padding: 16px 24px; "
            "border: 1px solid #444444; border-radius: 10px; }"
        )
        self.hide()

    def center(self):
        parent = self.parentWidget()
        if parent is None:
            return
        self.adjustSize()
        x = max(0, (parent.width() - self.width()) // 2)
        y = max(0, (parent.height() - self.height()) // 2)
        self.move(x, y)

    def show_text(self, text: str):
        self.setText(text or "")
        self.center()
        self.show()
        self.raise_()


class NextEpisodeOverlay(QLabel):
    """Плашка «следующая серия» с обратным отсчётом."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setWordWrap(True)
        self.setMinimumWidth(320)
        self.setMaximumWidth(460)
        self.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, True
        )
        self.setStyleSheet(
            "QLabel { background: #1c1c1c; color: #ffffff; "
            "font-size: 15px; font-weight: 600; padding: 16px 24px; "
            "border: 1px solid #444444; border-radius: 10px; }"
        )
        self._episode_text = ""
        self.hide()

    def center(self):
        parent = self.parentWidget()
        if parent is None:
            return
        self.adjustSize()
        x = max(0, (parent.width() - self.width()) // 2)
        y = max(0, (parent.height() - self.height()) // 2)
        self.move(x, y)

    def set_next(self, episode_text: str, seconds: int):
        self._episode_text = episode_text
        self.set_remaining(seconds)
        self.center()
        self.show()
        self.raise_()

    def set_remaining(self, seconds: int):
        self.setText(
            "Следующая серия\n"
            f"{self._episode_text}\n\n"
            f"Автозапуск через {max(0, int(seconds))} с\n"
            "«Смотреть» — сейчас · Esc — отмена"
        )
        self.center()


class MpvVideo(QOpenGLWidget):
    """Видео через libmpv render API (без wid)."""

    update_wanted = pyqtSignal()

    _KEYS = {
        Qt.Key.Key_Space: "SPACE", Qt.Key.Key_Escape: "ESC",
        Qt.Key.Key_Up: "UP", Qt.Key.Key_Down: "DOWN",
        Qt.Key.Key_Left: "LEFT", Qt.Key.Key_Right: "RIGHT",
        Qt.Key.Key_Enter: "ENTER", Qt.Key.Key_Return: "ENTER",
        Qt.Key.Key_Home: "HOME", Qt.Key.Key_End: "END",
        Qt.Key.Key_PageUp: "PGUP", Qt.Key.Key_PageDown: "PGDN",
        Qt.Key.Key_Delete: "DEL", Qt.Key.Key_Backspace: "BS",
        Qt.Key.Key_Tab: "TAB", Qt.Key.Key_Insert: "INS",
        Qt.Key.Key_F1: "F1", Qt.Key.Key_F2: "F2", Qt.Key.Key_F3: "F3",
        Qt.Key.Key_F4: "F4", Qt.Key.Key_F5: "F5", Qt.Key.Key_F6: "F6",
        Qt.Key.Key_F7: "F7", Qt.Key.Key_F8: "F8", Qt.Key.Key_F9: "F9",
        Qt.Key.Key_F10: "F10", Qt.Key.Key_F11: "F11", Qt.Key.Key_F12: "F12",
    }
    _MBTN = {
        Qt.MouseButton.LeftButton: "MBTN_LEFT",
        Qt.MouseButton.MiddleButton: "MBTN_MID",
        Qt.MouseButton.RightButton: "MBTN_RIGHT",
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self._mpv = None
        self._mpv_mod = None
        self._enq = None
        self._rctx = None
        self._gl = False
        self._gp = None
        self._mouse_t = 0.0
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.setMinimumSize(320, 180)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.overlay = None
        self.next_overlay = None
        self.update_wanted.connect(
            self.update, Qt.ConnectionType.QueuedConnection
        )

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.overlay is not None:
            self.overlay.center()
        if self.next_overlay is not None:
            self.next_overlay.center()

    def bind_mpv(self, mpv_inst, mpv_mod, enqueue=None):
        self._mpv = mpv_inst
        self._mpv_mod = mpv_mod
        self._enq = enqueue
        if self._gl:
            try:
                self.makeCurrent()
                self._make_ctx()
                self.doneCurrent()
            except Exception as e:
                _dbg("bind_mpv:", e)

    def unbind_mpv(self):
        r, self._rctx = self._rctx, None
        if r is None:
            return
        try:
            self.makeCurrent()
            r.free()
            self.doneCurrent()
        except Exception as e:
            _dbg("render free:", e)

    def initializeGL(self):
        self._gl = True
        self._make_ctx()

    def _make_ctx(self):
        if self._rctx or not self._gl or self._mpv is None:
            return
        mod = self._mpv_mod
        try:
            gp = mod.MpvGlGetProcAddressFn(self._get_proc)
            ctx = mod.MpvRenderContext(
                self._mpv, "opengl",
                opengl_init_params={"get_proc_address": gp},
                advanced_control=True,
            )
            ctx.update_cb = self._on_update
            self._gp = gp
            self._rctx = ctx
            _dbg("render context created")
        except Exception as e:
            _dbg("render context:", e)

    def _get_proc(self, _ud, name):
        try:
            ctx = QOpenGLContext.currentContext()
            if ctx is None:
                return None
            addr = ctx.getProcAddress(name)
            if not addr:
                return None
            v = int(addr)
            return v or None
        except Exception:
            return None

    def _on_update(self):
        try:
            self.update_wanted.emit()
        except Exception:
            pass

    def _clear_black(self):
        try:
            ctx = QOpenGLContext.currentContext()
            if ctx is None:
                return

            def _fn(sig, name):
                addr = ctx.getProcAddress(name)
                v = int(addr) if addr else 0
                return sig(v) if v else None

            pc = _fn(
                ctypes.CFUNCTYPE(
                    None, ctypes.c_float, ctypes.c_float,
                    ctypes.c_float, ctypes.c_float,
                ),
                "glClearColor",
            )
            cl = _fn(ctypes.CFUNCTYPE(None, ctypes.c_uint), "glClear")
            if pc and cl:
                pc(0.0, 0.0, 0.0, 1.0)
                cl(0x4000 | 0x0100 | 0x0400)
        except Exception:
            pass

    def paintGL(self):
        r = self._rctx
        if r is None:
            self._clear_black()
            return
        dpr = self.devicePixelRatioF()
        w = int(self.width() * dpr)
        h = int(self.height() * dpr)
        if w <= 0 or h <= 0:
            return
        try:
            r.update()
            r.render(
                opengl_fbo={
                    "w": w,
                    "h": h,
                    "fbo": int(self.defaultFramebufferObject()),
                    "internal_format": 0x8058,
                },
                flip_y=True,
            )
            r.report_swap()
        except Exception as e:
            _dbg("mpv render:", e)

    def _cmd(self, *args):
        m = self._mpv
        if m is None:
            return
        job = lambda: m.command(*args)
        if self._enq is not None:
            self._enq(job)
            return
        try:
            job()
        except Exception as e:
            _dbg("mpv input:", e)

    def _xy(self, e):
        dpr = self.devicePixelRatioF()
        return (
            str(int(e.position().x() * dpr)),
            str(int(e.position().y() * dpr)),
        )

    def keyPressEvent(self, event):
        if event.modifiers() & (
            Qt.KeyboardModifier.ControlModifier
            | Qt.KeyboardModifier.AltModifier
            | Qt.KeyboardModifier.MetaModifier
        ):
            super().keyPressEvent(event)
            return
        name = self._KEYS.get(event.key())
        if name is None:
            text = event.text()
            if text and text.isprintable():
                name = text
        if name:
            self._cmd("keypress", name)
            event.accept()
        else:
            super().keyPressEvent(event)

    def mouseMoveEvent(self, event):
        now = time.monotonic()
        if now - self._mouse_t < 0.04:
            return
        self._mouse_t = now
        x, y = self._xy(event)
        self._cmd("mouse", x, y)
        event.accept()

    def mousePressEvent(self, event):
        name = self._MBTN.get(event.button())
        if name is None:
            super().mousePressEvent(event)
            return
        x, y = self._xy(event)
        self._cmd("mouse", x, y)
        self._cmd("keydown", name)
        event.accept()

    def mouseReleaseEvent(self, event):
        name = self._MBTN.get(event.button())
        if name is None:
            super().mouseReleaseEvent(event)
            return
        x, y = self._xy(event)
        self._cmd("mouse", x, y)
        self._cmd("keyup", name)
        event.accept()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            x, y = self._xy(event)
            self._cmd("mouse", x, y, "0", "double")
            event.accept()
        else:
            super().mouseDoubleClickEvent(event)

    def wheelEvent(self, event):
        delta = event.angleDelta().y()
        if delta:
            self._cmd(
                "keypress", "WHEEL_UP" if delta > 0 else "WHEEL_DOWN"
            )
            event.accept()
        else:
            super().wheelEvent(event)


class Player(QObject):
    """Обёртка над mpv: очередь команд, отложенный play, fallback опций."""

    failed = pyqtSignal(str)
    key = pyqtSignal(str)
    fs_changed = pyqtSignal(bool)
    buffering = pyqtSignal(str)
    resumed = pyqtSignal(float)
    finished = pyqtSignal()
    playback_idle = pyqtSignal()

    MPV_KEYS = {
        "SPACE": "pause",
        "f": "fullscreen",
        "F11": "fullscreen",
        "ESC": "escape",
        "LEFT": "vol_down",
        "RIGHT": "vol_up",
        "WHEEL_UP": "vol_up",
        "WHEEL_DOWN": "vol_down",
        "m": "mute",
        "M": "mute",
        "+": "vol_up",
        "-": "vol_down",
        "MBTN_LEFT_DBL": "fullscreen",
    }

    def __init__(self, mpv_module, widget: MpvVideo, opts: dict, parent=None):
        super().__init__(parent)
        self._mpv_module = mpv_module
        self._widget = widget
        self._opts = dict(opts)
        self.mpv = None
        self.error = ""
        self._play_seq = 0
        self._loading = False
        self._has_video = False
        self._paused_for_cache = False
        self._cache_pct = None
        self._pending_start = (0, 0.0)
        self._failed = False
        self._stopped = False
        self._seeking = False
        self._seek_until = 0.0
        self._mpvq = queue.Queue()
        self._mpvq_thread = threading.Thread(
            target=self._mpvq_loop, name="mpv-cmd", daemon=True
        )
        self._mpvq_thread.start()
        self._create_mpv()

    def _mpvq_loop(self):
        while True:
            job = self._mpvq.get()
            if job is None:
                return
            try:
                job()
            except Exception as e:
                _dbg("mpv job:", e)

    def enqueue(self, job):
        self._mpvq.put(job)

    def _create_mpv(self):
        opts = dict(self._opts)
        try:
            self.mpv = self._mpv_module.MPV(**opts)
            self.error = ""
        except Exception as e:
            _dbg("mpv create:", e)
            self.mpv = self._retry_mpv_opts(opts, e)
        if self.mpv is None:
            return

        @self.mpv.event_callback("end-file")
        def _on_end_file(event):
            data = getattr(event, "data", None)
            # python-mpv отдаёт MpvEventEndFile с числовым reason:
            # 0 = EOF, 4 = ERROR. Старые версии могли отдавать dict.
            reason = getattr(data, "reason", None)
            if reason is None and isinstance(data, dict):
                reason = data.get("reason")
            _log("end-file reason:", reason)
            self._loading = False
            self._has_video = False
            self.buffering.emit("")
            if reason in (0, "eof"):
                self.finished.emit()
            elif reason in (4, "error"):
                self._failed = True
                self.failed.emit("ошибка воспроизведения потока")
            elif reason in (2, 3, "aborted", "quit"):
                self._stopped = True

        @self.mpv.event_callback("file-loaded")
        def _on_file_loaded(_event):
            seq, pos = self._pending_start
            if seq != self._play_seq or pos <= 0:
                return
            self._pending_start = (0, 0.0)
            self.enqueue(
                lambda: self.mpv.command("seek", str(pos), "absolute")
            )
            self.resumed.emit(pos)

        @self.mpv.event_callback("seek")
        def _on_seek(_event):
            self._seek_until = time.monotonic() + 2.0

        self._bind_keys()
        self._observe_fullscreen()
        self._observe_buffering()
        try:
            self._widget.bind_mpv(self.mpv, self._mpv_module, self.enqueue)
        except Exception as e:
            _dbg("bind_mpv:", e)

    def _retry_mpv_opts(self, opts: dict, first_error: Exception):
        last = first_error
        for key in ("gpu_context", "vo", "hwdec"):
            if key not in opts:
                continue
            trial = dict(opts)
            trial.pop(key)
            try:
                mpv_obj = self._mpv_module.MPV(**trial)
                _dbg("mpv: option dropped:", key)
                self._opts = dict(trial)
                self.error = f"{key} не поддерживается, опция отброшена"
                return mpv_obj
            except Exception as e:
                last = e
        self.error = str(last)
        return None

    def _bind_keys(self):
        def make(action):
            def handler(*args):
                state = args[0] if args else None
                s = str(state) if state is not None else ""
                if not s or s[0] in ("d", "p", "D", "P"):
                    self.key.emit(action)
            return handler

        for key_name, action in self.MPV_KEYS.items():
            try:
                self.mpv.register_key_binding(key_name, make(action))
            except Exception as e:
                _dbg("register_key_binding", key_name, e)

    def _observe_fullscreen(self):
        def on_fullscreen(_name, value):
            if value is None:
                return
            self.fs_changed.emit(bool(value))

        try:
            self.mpv.observe_property("fullscreen", on_fullscreen)
        except Exception as e:
            _dbg("observe fullscreen:", e)

    def _observe_buffering(self):
        """Индикатор открытия/буферизации первого сегмента."""

        def on_core_idle(_name, value):
            if value is None:
                return
            if value:
                if (
                    self._has_video
                    and not self._loading
                    and not self._failed
                    and not self._stopped
                    and not self.in_seek_grace()
                ):
                    self.playback_idle.emit()
                if self._loading:
                    self.buffering.emit("Открытие потока…")
            elif self._loading and not self._has_video:
                self.buffering.emit("Подготовка потока…")

        def on_paused(_name, value):
            if value is None:
                return
            self._paused_for_cache = bool(value)
            if self._paused_for_cache:
                pct = self._cache_pct
                text = "Буферизация…"
                if pct is not None:
                    text = f"Буферизация… {int(pct)}%"
                self.buffering.emit(text)
            elif self._has_video:
                self.buffering.emit("")
            elif self._loading:
                self.buffering.emit("Подготовка потока…")

        def on_cache(_name, value):
            if value is None:
                return
            try:
                self._cache_pct = float(value)
            except (TypeError, ValueError):
                return
            if self._paused_for_cache:
                self.buffering.emit(
                    f"Буферизация… {int(self._cache_pct)}%"
                )

        def on_video(_name, value):
            if value:
                self._loading = False
                self._has_video = True
                self.buffering.emit("")

        try:
            self.mpv.observe_property("core-idle", on_core_idle)
            self.mpv.observe_property("paused-for-cache", on_paused)
            self.mpv.observe_property(
                "cache-buffering-state", on_cache
            )
            self.mpv.observe_property("video-params", on_video)
        except Exception as e:
            _dbg("observe buffering:", e)

        def on_seeking(_name, value):
            if value is None:
                return
            self._seeking = bool(value)
            # Небольшая пауза после перемотки: mpv может кратко
            # уходить в idle/eof и нельзя принимать это за конец серии.
            self._seek_until = time.monotonic() + 2.0

        try:
            self.mpv.observe_property("seeking", on_seeking)
        except Exception as e:
            _dbg("observe seeking:", e)

        def on_eof_reached(_name, value):
            if (
                value
                and not self._failed
                and not self._stopped
                and not self.in_seek_grace()
            ):
                self.finished.emit()

        try:
            self.mpv.observe_property("eof-reached", on_eof_reached)
        except Exception as e:
            _dbg("observe eof-reached:", e)

        @self.mpv.event_callback("playback-restart")
        def _on_playback_restart(_event):
            self._loading = False
            self._has_video = True
            self.buffering.emit("")

    def in_seek_grace(self) -> bool:
        return self._seeking or time.monotonic() < self._seek_until

    def play(
        self, url: str, headers=None, proxy=None, title=None,
        start_pos: float = 0.0,
    ):
        if self.mpv is None:
            self.failed.emit(self.error or "mpv недоступен")
            return
        self._play_seq += 1
        self._loading = True
        self._has_video = False
        self._paused_for_cache = False
        self._cache_pct = None
        self._failed = False
        self._stopped = False
        self._pending_start = (self._play_seq, float(start_pos or 0.0))
        self.buffering.emit("Подготовка потока…")
        self._defer_play(url, self._play_seq, 0, headers, proxy, title)

    def stop(self):
        self._play_seq += 1
        self._loading = False
        self._has_video = False
        self._stopped = True
        self._pending_start = (0, 0.0)
        self.buffering.emit("")
        if self.mpv is not None:
            self.enqueue(lambda: self.mpv.command("stop"))

    def _defer_play(self, url, seq, tries, headers, proxy, title):
        if seq != self._play_seq:
            return
        if getattr(self._widget, "_rctx", None) is None:
            if tries >= 200:
                self.failed.emit("render context mpv не создан")
                return
            QTimer.singleShot(
                50,
                lambda: self._defer_play(
                    url, seq, tries + 1, headers, proxy, title
                ),
            )
            return

        def job():
            if seq != self._play_seq:
                return
            try:
                if headers is not None:
                    self.mpv["http-header-fields"] = list(headers)
                try:
                    self.mpv["http-proxy"] = proxy or ""
                except Exception as e:
                    _dbg("http-proxy:", e)
                if title:
                    try:
                        self.mpv["title"] = title
                    except Exception:
                        pass
                self.mpv.play(url)
            except Exception as e:
                self.failed.emit(str(e))

        self.enqueue(job)

    def command(self, *args):
        if self.mpv is None:
            return
        self.enqueue(lambda: self.mpv.command(*args))

    def toggle_pause(self):
        self.command("cycle", "pause")

    def volume(self, delta):
        def _vol():
            try:
                cur = int(self.mpv.volume or 50)
                self.mpv.volume = max(0, min(150, cur + delta))
            except Exception as e:
                _dbg("volume:", e)

        self.enqueue(_vol)

    def set_hwdec(self, value: str):
        def _hw():
            try:
                self.mpv.hwdec = value
            except Exception as e:
                _dbg("hwdec:", e)

        self.enqueue(_hw)

    def set_fullscreen(self, value: bool):
        self.enqueue(lambda: self._sync_fullscreen(bool(value)))

    def _sync_fullscreen(self, value: bool):
        try:
            if bool(self.mpv.fullscreen) != value:
                self.mpv.fullscreen = value
        except Exception as e:
            _dbg("fullscreen sync:", e)

    def terminate(self):
        self._play_seq += 1
        try:
            self._widget.unbind_mpv()
        except Exception:
            pass
        try:
            if self.mpv is not None:
                self.enqueue(lambda: self.mpv.terminate())
            self.enqueue(None)
            self._mpvq_thread.join(timeout=1.0)
        except Exception:
            pass
        self.mpv = None


# --------------------------------------------------------------------------
# Диалог настроек
# --------------------------------------------------------------------------

class SettingsDialog(QDialog):
    def __init__(self, settings: QSettings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.win = parent
        self._check_worker = None
        self.setWindowTitle("Настройки")
        self.setModal(True)
        self.resize(700, 510)

        root = QVBoxLayout(self)
        grid = QGridLayout()

        self.base_label = QLabel("Зеркало:")
        self.base_edit = QLineEdit()
        self.base_edit.setPlaceholderText(core.DEFAULT_BASE)
        self.base_edit.setToolTip(
            "Базовый адрес сайта. Если домен недоступен, попробуйте другое "
            "зеркало (например, https://hdrezka.co)."
        )

        self.proxy_check = QCheckBox("Прокси для API и mpv:")
        self.proxy_edit = QLineEdit()
        self.proxy_edit.setPlaceholderText("http://127.0.0.1:8080")
        self.proxy_edit.setToolTip(
            "Необязательно. Используется для запросов к сайту и для mpv. "
            "Поддерживаются http:// и socks://."
        )

        self.quality_label = QLabel("Макс. качество:")
        self.quality_combo = QComboBox()
        for text, value in QUALITY_OPTIONS:
            self.quality_combo.addItem(text, value)
        self.quality_combo.setToolTip(
            "При запуске выбирается вариант не выше этого. "
            "«Авто» — лучшее доступное (PRO-качества пропускаются)."
        )

        self.source_label = QLabel("Источник потока:")
        self.source_combo = QComboBox()
        for text, value in SOURCE_OPTIONS:
            self.source_combo.addItem(text, value)
        self.source_combo.setToolTip(
            "HLS — m3u8-плейлист; MP4 — прямой файл. "
            "«Авто» сначала пробует HLS, а если первый сегмент "
            "не открылся за 10 секунд — переключается на MP4."
        )

        self.hwdec_label = QLabel("hwdec:")
        self.hwdec_combo = QComboBox()
        for text, value in HW_DEC_OPTIONS:
            self.hwdec_combo.addItem(text, value)
        self.hwdec_combo.setToolTip(
            "Аппаратное декодирование mpv. Применяется сразу; "
            "если видео чёрное/тормозит — попробуйте no."
        )

        self.autoplay_check = QCheckBox(
            "Автозапуск следующей серии"
        )
        self.autoplay_check.setToolTip(
            "После окончания серии показывать плашку с обратным "
            "отсчётом и запускать следующую серию (в том числе "
            "первую серию следующего сезона)."
        )
        self.next_timer_label = QLabel("Таймер между сериями, сек:")
        self.next_timer_spin = QSpinBox()
        self.next_timer_spin.setRange(3, 300)
        self.next_timer_spin.setValue(10)
        self.next_timer_spin.setSuffix(" с")
        self.next_timer_spin.setToolTip(
            "Сколько секунд показывать плашку до автозапуска "
            "следующей серии."
        )
        self.autoplay_last_check = QCheckBox(
            "При запуске продолжать последнее"
        )
        self.autoplay_last_check.setToolTip(
            "При старте приложения загрузить последний просмотренный "
            "тайтл, восстановить перевод/серию и сразу продолжить "
            "с сохранённого места."
        )

        grid.addWidget(self.base_label, 0, 0)
        grid.addWidget(self.base_edit, 0, 1, 1, 2)
        grid.addWidget(self.proxy_check, 1, 0)
        grid.addWidget(self.proxy_edit, 1, 1, 1, 2)
        grid.addWidget(self.quality_label, 2, 0)
        grid.addWidget(self.quality_combo, 2, 1)
        grid.addWidget(self.source_label, 3, 0)
        grid.addWidget(self.source_combo, 3, 1)
        grid.addWidget(self.hwdec_label, 4, 0)
        grid.addWidget(self.hwdec_combo, 4, 1)
        grid.addWidget(self.autoplay_check, 5, 0, 1, 2)
        grid.addWidget(self.next_timer_label, 6, 0)
        grid.addWidget(self.next_timer_spin, 6, 1)
        grid.addWidget(self.autoplay_last_check, 7, 0, 1, 2)
        grid.setColumnStretch(1, 1)
        root.addLayout(grid)

        actions = QHBoxLayout()
        self.check_btn = QPushButton("Проверить сайт")
        self.check_btn.setToolTip("Загрузить главную страницу с этими настройками")
        self.check_btn.clicked.connect(self._check_site)
        self.reset_btn = QPushButton("Сбросить сессию")
        self.reset_btn.setToolTip("Забыть cookie Anubis и пройти проверку заново")
        self.reset_btn.clicked.connect(self._reset_session)
        self.cache_btn = QPushButton("Очистить кэш")
        self.cache_btn.setToolTip("Удалить сохранённый список главной")
        self.cache_btn.clicked.connect(self._clear_cache)
        actions.addWidget(self.check_btn)
        actions.addWidget(self.reset_btn)
        actions.addWidget(self.cache_btn)
        actions.addStretch(1)
        root.addLayout(actions)

        hint = QLabel(
            "Сайт закрыт проверкой Anubis (proof-of-work). Клиент решает её "
            "сам, без браузера. Если домен не открывается, смените зеркало "
            "или включите прокси. PRO-качества без подписки могут не играть. "
            "«Авто» сначала пробует HLS, а если первый сегмент не открылся "
            "за 10 секунд — переключается на прямой MP4. Автозапуск "
            "показывает плашку с таймером и запускает следующую серию."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#777;")
        root.addWidget(hint)
        root.addStretch(1)

        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        root.addWidget(bb)

        self.proxy_check.toggled.connect(self._sync_enabled)
        self.autoplay_check.toggled.connect(self._sync_enabled)
        self._load()

    def _load(self):
        s = self.settings
        self.base_edit.setText(
            s.value("base_url", core.DEFAULT_BASE, type=str)
        )
        self.proxy_check.setChecked(
            s.value("proxy_enabled", False, type=bool)
        )
        self.proxy_edit.setText(s.value("proxy", "", type=str))
        idx = self.quality_combo.findData(
            s.value("quality", "auto", type=str)
        )
        self.quality_combo.setCurrentIndex(idx if idx >= 0 else 0)
        s_idx = self.source_combo.findData(
            s.value("source", "auto", type=str)
        )
        self.source_combo.setCurrentIndex(s_idx if s_idx >= 0 else 0)
        hw_idx = self.hwdec_combo.findData(
            s.value("hwdec", "auto-safe", type=str)
        )
        self.hwdec_combo.setCurrentIndex(hw_idx if hw_idx >= 0 else 0)
        self.autoplay_check.setChecked(
            s.value("autoplay_next", True, type=bool)
        )
        try:
            timer = int(s.value("next_timer", 10, type=int))
        except (TypeError, ValueError):
            timer = 10
        self.next_timer_spin.setValue(max(3, min(300, timer)))
        self.autoplay_last_check.setChecked(
            s.value("autoplay_last", True, type=bool)
        )
        self._sync_enabled()

    def _save(self):
        s = self.settings
        s.setValue("base_url", normalize_base(self.base_edit.text()))
        s.setValue("proxy_enabled", self.proxy_check.isChecked())
        s.setValue("proxy", self.proxy_edit.text())
        s.setValue("quality", self.quality_combo.currentData())
        s.setValue("source", self.source_combo.currentData())
        s.setValue("hwdec", self.hwdec_combo.currentData())
        s.setValue(
            "autoplay_next", self.autoplay_check.isChecked()
        )
        s.setValue("next_timer", self.next_timer_spin.value())
        s.setValue(
            "autoplay_last", self.autoplay_last_check.isChecked()
        )
        s.sync()

    def _sync_enabled(self):
        self.proxy_edit.setEnabled(self.proxy_check.isChecked())
        self.next_timer_spin.setEnabled(
            self.autoplay_check.isChecked()
        )

    def exec(self):
        self._load()
        return super().exec()

    def accept(self):
        self._detach_check()
        self._save()
        super().accept()

    def reject(self):
        self._detach_check()
        self._load()
        super().reject()

    def _detach_check(self):
        if self._check_worker is not None:
            try:
                self._check_worker.done.disconnect(self._check_done)
            except Exception:
                pass
            self._check_worker = None

    # ---------- значения ----------
    def base_url(self) -> str:
        return normalize_base(self.base_edit.text())

    def proxy(self) -> Optional[str]:
        if not self.proxy_check.isChecked():
            return None
        return normalize_proxy(self.proxy_edit.text())

    def quality(self) -> str:
        return str(self.quality_combo.currentData() or "auto")

    def source(self) -> str:
        return str(self.source_combo.currentData() or "auto")

    def hwdec(self) -> str:
        return str(self.hwdec_combo.currentData() or "auto-safe")

    def autoplay_next(self) -> bool:
        return self.autoplay_check.isChecked()

    def next_timer(self) -> int:
        return int(self.next_timer_spin.value())

    def autoplay_last(self) -> bool:
        return self.autoplay_last_check.isChecked()

    # ---------- действия ----------
    def _check_site(self):
        if self._check_worker is not None and self._check_worker.isRunning():
            return
        self.check_btn.setEnabled(False)
        self.check_btn.setText("Проверка…")
        # Без parent: диалог может закрыться раньше, чем поток завершится.
        worker = CheckWorker(self.base_url(), self.proxy(), None)
        _PROXY_WORKERS.add(worker)
        worker.done.connect(self._check_done)
        worker.finished.connect(lambda w=worker: _forget_worker(w))
        self._check_worker = worker
        worker.start()

    def _check_done(self, ok: bool, text: str):
        self.check_btn.setEnabled(True)
        self.check_btn.setText("Проверить сайт")
        self._check_worker = None
        if ok:
            QMessageBox.information(self, "Проверка сайта", text)
        else:
            QMessageBox.warning(
                self, "Проверка сайта",
                "Не удалось подключиться:\n" + text +
                "\n\nПопробуйте другое зеркало или прокси."
            )

    def _reset_session(self):
        if self.win is not None:
            self.win.reset_session()
        QMessageBox.information(
            self, "Сессия",
            "Cookie Anubis сброшены. Следующая загрузка пройдёт проверку "
            "заново."
        )

    def _clear_cache(self):
        if self.win is not None:
            self.win.clear_cache()
        QMessageBox.information(
            self, "Кэш",
            "Кэш главной страницы удалён. Список будет загружен заново."
        )


# --------------------------------------------------------------------------
# Главное окно
# --------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self, mpv_module):
        super().__init__()
        self.mpv_module = mpv_module
        self.setWindowTitle(APP_TITLE)
        self.resize(1320, 800)
        self.setWindowIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
        )

        self.settings = QSettings(ORG_NAME, APP_NAME)
        self.client = self._new_client()

        self.catalog_items: List[core.SearchItem] = []
        self._catalog_section = "home"
        self._catalog_genre = ""
        self._catalog_page = 1
        self._has_more = False
        self._search_active = False
        self._search_token = 0
        self._catalog_token = 0
        self._title_token = 0
        self._episodes_token = 0
        self._episodes_preferred_episode = ""
        self._stream_token = 0

        self._title_info: Optional[core.TitleInfo] = None
        self._episodes: Dict[str, List[core.Episode]] = {}
        self._seasons: Dict[str, str] = {}
        self._current_url = ""
        self._pending_play = False
        self._last_stream: Optional[core.StreamInfo] = None
        self._last_chosen: Optional[core.Quality] = None
        self._last_play_url = ""
        self._play_alt_url = ""
        self._play_title = ""
        self._play_referer = ""
        self._play_status_text = ""
        self._play_start_pos = 0.0
        self._fallback_tried = False
        self._positions: Dict[str, dict] = {}
        self._playing_key = ""
        self._playing_post_id = ""
        self._playing_title = ""
        self._playing_label = ""
        self._playing_tid = ""
        self._playing_sid = ""
        self._playing_eid = ""
        self._finish_latched = False
        self._near_end_count = 0
        self._pending_selection = ("", "1", "1")
        self._startup_resume_url = ""
        self._autoplay_last_pending = False
        self._suppress_selection = False
        self._suppress_ui = False
        self._closing = False

        self._workers = set()
        self._catalog_worker = None
        self._title_worker = None
        self._search_worker = None

        self._poster_cache: Dict[str, QPixmap] = {}
        self._poster_replies = {}
        self._poster_url = ""

        self._fullscreen = False
        self._fs_t = 0.0
        self._was_maximized = False
        self._list_visible = True
        self._info_visible = True

        self._startup_timer = QTimer(self)
        self._startup_timer.setSingleShot(True)
        self._startup_timer.timeout.connect(self._on_startup_timeout)

        self._resume_timer = QTimer(self)
        self._resume_timer.setSingleShot(True)
        self._resume_timer.timeout.connect(self._on_resume_timeout)

        self._pos_timer = QTimer(self)
        self._pos_timer.setInterval(10000)
        self._pos_timer.timeout.connect(self.save_position)

        self._next_timer = QTimer(self)
        self._next_timer.setInterval(1000)
        self._next_timer.timeout.connect(self._on_next_tick)
        self._next_remaining = 0
        self._next_episode = None

        self._info_timer = QTimer(self)
        self._info_timer.setInterval(1000)
        self._info_timer.timeout.connect(self._update_playback_info)

        self._load_positions()
        self._build_ui()
        self._poster_mgr = QNetworkAccessManager(self)
        self.settings_dlg = SettingsDialog(self.settings, self)
        self.player = Player(
            mpv_module, self.video, self._mpv_options(), self
        )
        self.player.failed.connect(self._on_player_failed)
        self.player.key.connect(self._on_mpv_key)
        self.player.fs_changed.connect(self._on_mpv_fullscreen)
        self.player.buffering.connect(self._on_buffering)
        self.player.resumed.connect(self._on_resumed)
        self.player.finished.connect(self._on_finished)
        self.player.playback_idle.connect(self._on_playback_idle)
        if self.player.mpv is None:
            QTimer.singleShot(
                0,
                lambda: QMessageBox.critical(
                    self, "mpv",
                    "Плеер mpv недоступен:\n\n"
                    + (self.player.error or "неизвестная ошибка")
                    + "\n\nПроверьте mpv/libmpv и python-mpv."
                ),
            )
        elif self.player.error:
            QTimer.singleShot(
                0,
                lambda: self.statusBar().showMessage(
                    "mpv: " + self.player.error, 12000
                ),
            )

        geo = self.settings.value("geometry")
        if geo is not None:
            try:
                self.restoreGeometry(geo)
            except Exception:
                pass
        state = self.settings.value("splitter_state")
        if state is not None:
            try:
                self.splitter.restoreState(state)
            except Exception:
                pass

        self._pos_timer.start()
        self._info_timer.start()

        resume_url = str(
            self.settings.value("last_play_url", "", type=str)
        )
        if resume_url and self.settings.value(
            "autoplay_last", True, type=bool
        ):
            self._startup_resume_url = resume_url
            pid = str(
                self.settings.value("last_play_post_id", "", type=str)
            )
            tid = str(
                self.settings.value("last_play_tid", "", type=str)
            )
            sid = str(
                self.settings.value("last_play_sid", "1", type=str)
            )
            eid = str(
                self.settings.value("last_play_eid", "1", type=str)
            )
            entry = self._positions.get(
                f"{pid}|{tid}|{sid}|{eid}"
            ) if pid and tid else None
            has_pos = False
            if entry:
                try:
                    p = float(entry.get("pos") or 0)
                    d = float(entry.get("duration") or 0)
                except (TypeError, ValueError):
                    p, d = 0.0, 0.0
                has_pos = p >= 3 and (not d or p < d - 15)
            self._autoplay_last_pending = has_pos

        self._load_cache()
        if self._startup_resume_url and not (
            self._current_url == self._startup_resume_url
            and (
                self._title_worker is not None
                or self._title_info is not None
            )
        ):
            self._current_url = self._startup_resume_url
            self._load_title(self._startup_resume_url)
        QTimer.singleShot(0, lambda: self.refresh_catalog(reset=True))

    # ---------- клиент/настройки ----------
    def _proxy_url(self) -> Optional[str]:
        if not self.settings.value("proxy_enabled", False, type=bool):
            return None
        return normalize_proxy(self.settings.value("proxy", "", type=str))

    def _new_client(self) -> core.HDrezkaClient:
        return core.HDrezkaClient(
            base=normalize_base(
                self.settings.value("base_url", core.DEFAULT_BASE, type=str)
            ),
            proxy=self._proxy_url(),
            timeout=30.0,
        )

    def _mpv_options(self) -> dict:
        hwdec = str(
            self.settings.value("hwdec", "auto-safe", type=str)
            or "auto-safe"
        )
        vo = os.environ.get("HDREZKA_MPV_VO") or "libmpv"
        opts = {
            "vo": vo,
            "hwdec": hwdec,
            "ytdl": False,
            "osc": True,
            "input_default_bindings": False,
            "input_vo_keyboard": True,
            "user_agent": core.USER_AGENT,
            "referrer": normalize_base(
                self.settings.value("base_url", core.DEFAULT_BASE, type=str)
            ) + "/",
            "cache": "yes",
            "demuxer_max_bytes": "64MiB",
            "log_handler": self._mpv_log,
        }
        ao = os.environ.get("HDREZKA_MPV_AO")
        if ao:
            opts["ao"] = ao
        return opts

    def _mpv_proxy(self) -> Optional[str]:
        proxy = self._proxy_url()
        if not proxy:
            return None
        scheme = urllib.parse.urlparse(proxy).scheme.lower()
        if scheme in ("http", "socks", "socks5", "socks5h"):
            return proxy
        return None

    def reset_session(self):
        self.client.reset()
        self.statusBar().showMessage(
            "Сессия Anubis сброшена; следующая загрузка пройдёт проверку.",
            8000,
        )

    def clear_cache(self):
        path = cache_file()
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            traceback.print_exc()
        self.catalog_items = []
        self._current_url = ""
        self._title_info = None
        self.refresh_catalog(reset=True)

    # ---------- UI ----------
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(9, 9, 9, 9)
        root.setSpacing(6)

        self.controls = QWidget()
        row = QHBoxLayout(self.controls)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)

        self.catalog_combo = QComboBox()
        for key, label in core.CATALOG_SECTIONS:
            self.catalog_combo.addItem(label, key)
        self.catalog_combo.setMinimumWidth(130)
        self.catalog_combo.setToolTip("Раздел каталога")

        self.genre_combo = QComboBox()
        self.genre_combo.addItem("Все жанры", "")
        self.genre_combo.setMinimumWidth(145)
        self.genre_combo.setToolTip(
            "Фильтр по жанру для текущего раздела. "
            "Для «Новинок» и «ТВ-шоу» недоступен."
        )
        self.genre_combo.setEnabled(False)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Поиск фильма или сериала…")
        self.search.setClearButtonEnabled(True)
        self.search.setToolTip("Поиск по названию (Ctrl+F)")

        self.refresh_btn = QPushButton("Обновить")
        self.refresh_btn.setToolTip("Обновить текущий список (Ctrl+R)")
        self.refresh_btn.clicked.connect(self.refresh_clicked)

        self.more_btn = QPushButton("Ещё…")
        self.more_btn.setToolTip("Загрузить следующую страницу каталога")
        self.more_btn.setEnabled(False)
        self.more_btn.clicked.connect(self.load_more)

        self.list_btn = QPushButton("Скрыть список")
        self.list_btn.setToolTip("Показать/скрыть список (Ctrl+L)")
        self.list_btn.clicked.connect(self.toggle_list)

        self.info_btn = QPushButton("Скрыть инфо")
        self.info_btn.setToolTip("Показать/скрыть панель информации (Ctrl+I)")
        self.info_btn.clicked.connect(self.toggle_info)

        self.fs_btn = QPushButton("На весь экран")
        self.fs_btn.setToolTip(
            "Полный экран (F11; в окне видео — F, Esc — выйти)"
        )
        self.fs_btn.clicked.connect(self.toggle_fullscreen)

        self.settings_btn = QPushButton("⚙")
        self.settings_btn.setFixedWidth(36)
        self.settings_btn.setToolTip("Настройки: зеркало, прокси, качество (Ctrl+,)")
        self.settings_btn.clicked.connect(self.open_settings)

        row.addWidget(self.catalog_combo)
        row.addWidget(self.genre_combo)
        row.addWidget(self.search, 1)
        row.addWidget(self.refresh_btn)
        row.addWidget(self.more_btn)
        row.addWidget(self.list_btn)
        row.addWidget(self.info_btn)
        row.addWidget(self.fs_btn)
        row.addWidget(self.settings_btn)
        root.addWidget(self.controls)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)

        self.list = QTreeWidget()
        self.list.setHeaderHidden(True)
        self.list.setUniformRowHeights(True)
        self.list.setAlternatingRowColors(True)
        self.list.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.list.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.list.setMinimumWidth(280)
        self.list.setStyleSheet("QTreeWidget::item { padding: 4px; }")
        self.list.itemSelectionChanged.connect(self._on_selection_changed)
        self.list.itemDoubleClicked.connect(self._on_item_double_click)
        self.splitter.addWidget(self.list)

        self.video = MpvVideo()
        self.video_overlay = VideoOverlay(self.video)
        self.video.overlay = self.video_overlay
        self.next_overlay = NextEpisodeOverlay(self.video)
        self.video.next_overlay = self.next_overlay
        self.splitter.addWidget(self.video)

        self.info_panel = QWidget()
        self.info_panel.setMinimumWidth(340)
        info = QVBoxLayout(self.info_panel)
        info.setContentsMargins(0, 0, 0, 0)
        info.setSpacing(6)

        header = QHBoxLayout()
        header.setSpacing(10)
        self.poster_label = QLabel()
        self.poster_label.setFixedSize(112, 160)
        self.poster_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._set_poster_placeholder("HDrezka")
        header.addWidget(
            self.poster_label, 0, Qt.AlignmentFlag.AlignTop
        )

        text_col = QVBoxLayout()
        text_col.setSpacing(3)
        self.title_label = QLabel("Выберите тайтл")
        self.title_label.setWordWrap(True)
        self.title_label.setStyleSheet(
            "font-size: 17px; font-weight: 600;"
        )
        self.meta_label = QLabel("")
        self.meta_label.setWordWrap(True)
        self.meta_label.setStyleSheet("color:#777;")
        self.rating_label = QLabel("")
        self.rating_label.setStyleSheet(
            "color:#b16286; font-weight:600;"
        )
        text_col.addWidget(self.title_label)
        text_col.addWidget(self.meta_label)
        text_col.addWidget(self.rating_label)
        text_col.addStretch(1)
        header.addLayout(text_col, 1)
        info.addLayout(header)

        self.desc = QTextEdit()
        self.desc.setReadOnly(True)
        self.desc.setFrameShape(QFrame.Shape.NoFrame)
        self.desc.setMinimumHeight(100)
        self.desc.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.desc.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self.desc.document().setDocumentMargin(0)
        self.desc.setStyleSheet(
            "QTextEdit { background:transparent; color:#555; "
            "border:none; padding:0; }"
        )
        info.addWidget(self.desc, 1)

        form = QGridLayout()
        form.setHorizontalSpacing(8)
        form.setVerticalSpacing(4)
        self.translation_label = QLabel("Перевод:")
        self.translation_combo = QComboBox()
        self.season_label = QLabel("Сезон:")
        self.season_combo = QComboBox()
        self.episode_label = QLabel("Серия:")
        self.episode_combo = QComboBox()
        self.quality_label = QLabel("Качество:")
        self.quality_combo = QComboBox()
        for text, value in QUALITY_OPTIONS:
            self.quality_combo.addItem(text, value)
        idx = self.quality_combo.findData(
            self.settings.value("quality", "auto", type=str)
        )
        self.quality_combo.setCurrentIndex(idx if idx >= 0 else 0)

        self.source_label = QLabel("Источник:")
        self.source_combo = QComboBox()
        for text, value in SOURCE_OPTIONS:
            self.source_combo.addItem(text, value)
        self.source_combo.setToolTip(
            "HLS (m3u8) или прямой MP4. «Авто» сначала пробует HLS, "
            "а если первый сегмент не открылся за 10 секунд — "
            "автоматически переключается на MP4."
        )
        s_idx = self.source_combo.findData(
            self.settings.value("source", "auto", type=str)
        )
        self.source_combo.setCurrentIndex(s_idx if s_idx >= 0 else 0)

        form.addWidget(self.translation_label, 0, 0)
        form.addWidget(self.translation_combo, 0, 1)
        form.addWidget(self.season_label, 1, 0)
        form.addWidget(self.season_combo, 1, 1)
        form.addWidget(self.episode_label, 2, 0)
        form.addWidget(self.episode_combo, 2, 1)
        form.addWidget(self.quality_label, 3, 0)
        form.addWidget(self.quality_combo, 3, 1)
        form.addWidget(self.source_label, 4, 0)
        form.addWidget(self.source_combo, 4, 1)
        form.setColumnStretch(1, 1)
        info.addLayout(form)

        btns = QHBoxLayout()
        self.play_btn = QPushButton("▶ Смотреть")
        self.play_btn.setEnabled(False)
        self.play_btn.setStyleSheet(
            "QPushButton { background:#2a9d8f; color:white; "
            "font-weight:600; padding:7px 12px; border-radius:5px; }"
            "QPushButton:disabled { background:#b9c6c3; }"
        )
        self.play_btn.clicked.connect(self._play_current)
        self.copy_btn = QPushButton("Копировать ссылку")
        self.copy_btn.setEnabled(False)
        self.copy_btn.clicked.connect(self._copy_link)
        btns.addWidget(self.play_btn, 1)
        btns.addWidget(self.copy_btn)
        info.addLayout(btns)

        self.playback_label = QLabel("Остановлено")
        self.playback_label.setWordWrap(True)
        self.playback_label.setStyleSheet(
            "color:#2a9d8f; font-size:12px; font-weight:600;"
        )
        info.addWidget(self.playback_label)

        self.stream_status = QLabel("Поток не выбран")
        self.stream_status.setWordWrap(True)
        self.stream_status.setStyleSheet("color:#777; font-size:11px;")
        info.addWidget(self.stream_status)

        self.splitter.addWidget(self.info_panel)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setStretchFactor(2, 0)
        self.splitter.setSizes([400, 580, 360])
        root.addWidget(self.splitter, 1)

        self.statusBar().showMessage("Готово")

        # сигналы
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(450)
        self._search_timer.timeout.connect(self._run_search)
        self.search.textChanged.connect(self._on_search_changed)
        self.search.returnPressed.connect(self._run_search)
        self.catalog_combo.currentIndexChanged.connect(
            self._on_catalog_changed
        )
        self.genre_combo.currentIndexChanged.connect(
            self._on_genre_changed
        )
        self.translation_combo.currentIndexChanged.connect(
            self._on_translation_changed
        )
        self.season_combo.currentIndexChanged.connect(
            self._on_season_changed
        )
        self.episode_combo.currentIndexChanged.connect(
            self._on_episode_changed
        )
        self.quality_combo.currentIndexChanged.connect(
            self._on_quality_changed
        )
        self.source_combo.currentIndexChanged.connect(
            self._on_source_changed
        )

        # горячие клавиши
        sc = QShortcut(QKeySequence("F11"), self)
        sc.setContext(Qt.ShortcutContext.ApplicationShortcut)
        sc.activated.connect(self.toggle_fullscreen)
        esc = QShortcut(QKeySequence("Esc"), self)
        esc.setContext(Qt.ShortcutContext.ApplicationShortcut)
        esc.activated.connect(self._on_escape)
        find = QShortcut(QKeySequence("Ctrl+F"), self)
        find.setContext(Qt.ShortcutContext.ApplicationShortcut)
        find.activated.connect(self._focus_search)
        refresh = QShortcut(QKeySequence("Ctrl+R"), self)
        refresh.setContext(Qt.ShortcutContext.ApplicationShortcut)
        refresh.activated.connect(self.refresh_clicked)
        lst = QShortcut(QKeySequence("Ctrl+L"), self)
        lst.setContext(Qt.ShortcutContext.ApplicationShortcut)
        lst.activated.connect(self.toggle_list)
        inf = QShortcut(QKeySequence("Ctrl+I"), self)
        inf.setContext(Qt.ShortcutContext.ApplicationShortcut)
        inf.activated.connect(self.toggle_info)
        st = QShortcut(QKeySequence("Ctrl+,"), self)
        st.setContext(Qt.ShortcutContext.ApplicationShortcut)
        st.activated.connect(self.open_settings)

    # ---------- кэш ----------
    def _load_cache(self):
        path = cache_file()
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            items = [
                core.SearchItem.from_dict(d)
                for d in (data.get("items") or [])
                if isinstance(d, dict)
            ]
            if not items:
                return
            self.catalog_items = items
            last_url = (
                self.settings.value("last_play_url", "", type=str)
                or self.settings.value("last_url", "", type=str)
            )
            self._populate_list(items, select_url=last_url)
            ts = int(data.get("ts") or 0)
            when = datetime.fromtimestamp(ts).strftime("%d.%m %H:%M") if ts else "?"
            self.statusBar().showMessage(
                f"Кэш: {len(items)} тайтлов от {when}. Идёт обновление…"
            )
        except Exception:
            traceback.print_exc()

    def _save_cache(self):
        path = cache_file()
        data = {
            "version": 1,
            "ts": int(time.time()),
            "items": [item.to_dict() for item in self.catalog_items],
        }
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, path)
        except Exception:
            traceback.print_exc()

    # ---------- позиции просмотра ----------
    def _load_positions(self):
        path = positions_file()
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return
            now = time.time()
            entries = []
            for key, value in data.items():
                if not isinstance(value, dict):
                    continue
                updated = int(value.get("updated") or 0)
                if updated and now - updated > 180 * 86400:
                    continue
                entries.append((key, value))
            entries.sort(
                key=lambda kv: int(kv[1].get("updated") or 0),
                reverse=True,
            )
            self._positions = dict(entries[:1000])
            _log("positions loaded:", len(self._positions), "from", path)
        except Exception:
            traceback.print_exc()

    def _save_positions(self):
        path = positions_file()
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._positions, f, ensure_ascii=False)
            os.replace(tmp, path)
        except Exception:
            traceback.print_exc()

    def _selection(self, info: core.TitleInfo):
        tid = str(self.translation_combo.currentData() or "")
        if not tid:
            tid = str(info.default_translator or "")
        if info.is_series:
            sid = str(self.season_combo.currentData() or "")
            eid = str(self.episode_combo.currentData() or "")
        else:
            sid, eid = "1", "1"
        return tid, sid, eid

    def _position_key(self) -> str:
        info = self._title_info
        if info is None:
            return ""
        tid, sid, eid = self._selection(info)
        if not tid:
            return ""
        return f"{info.post_id}|{tid}|{sid}|{eid}"

    def _current_saved_position(self) -> float:
        key = self._position_key()
        if not key:
            return 0.0
        entry = self._positions.get(key)
        if not entry:
            return 0.0
        try:
            pos = float(entry.get("pos") or 0)
            dur = float(entry.get("duration") or 0)
        except (TypeError, ValueError):
            return 0.0
        if pos >= 3 and (not dur or pos < dur - 15):
            return pos
        return 0.0

    def _mpv_time_pos(self):
        if self.player.mpv is None:
            return None
        for name in ("time_pos", "playback_time"):
            try:
                value = getattr(self.player.mpv, name)
            except Exception:
                continue
            if value is None:
                continue
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
        return None

    def _read_mpv_position(self):
        if self.player.mpv is None:
            _log("position: mpv is None")
            return None
        pos = self._mpv_time_pos()
        if pos is None:
            try:
                dur = self.player.mpv.duration
            except Exception:
                dur = None
            _log("position: time_pos is None, duration", repr(dur))
            return None
        try:
            dur = float(self.player.mpv.duration or 0)
        except Exception:
            dur = 0.0
        return pos, dur

    def save_position(self):
        key = self._playing_key
        if not key:
            return
        data = self._read_mpv_position()
        if data is None:
            _log("save: no time_pos for", key)
            return
        pos, dur = data
        if pos < 3 or (dur and pos >= max(0.0, dur - 15.0)):
            if key in self._positions:
                self._positions.pop(key, None)
                self._save_positions()
            _log("save: skip/remove", key, round(pos, 1), round(dur, 1))
            return
        old = self._positions.get(key) or {}
        try:
            old_pos = float(old.get("pos") or 0)
            old_dur = float(old.get("duration") or 0)
        except (TypeError, ValueError):
            old_pos, old_dur = 0.0, 0.0
        if old and abs(old_pos - pos) < 1.0 and abs(old_dur - dur) < 1.0:
            return
        old.update(
            {
                "pos": round(pos, 1),
                "duration": round(dur, 1),
                "updated": int(time.time()),
                "title": self._playing_title or old.get("title", ""),
                "label": self._playing_label or old.get("label", ""),
                "tid": self._playing_tid,
                "sid": self._playing_sid,
                "eid": self._playing_eid,
            }
        )
        self._positions[key] = old
        self._save_positions()
        _log("save:", key, round(pos, 1), "of", round(dur, 1))

    def _on_finished(self):
        if not self._playing_key:
            return
        self._finish_latched = True
        key = self._playing_key
        _log(
            "finished:", key,
            "sid/eid:", self._playing_sid, self._playing_eid,
        )
        self._hide_overlay()
        if key and key in self._positions:
            self._positions.pop(key, None)
            self._save_positions()
        self._playing_key = ""
        self.playback_label.setText("Остановлено")
        self._maybe_autoplay_next()

    def _on_playback_idle(self):
        if self._closing or not self._playing_key or self._finish_latched:
            return
        QTimer.singleShot(200, self._confirm_idle_finish)

    def _confirm_idle_finish(self):
        if self._closing or not self._playing_key or self._finish_latched:
            return
        try:
            failed = self.player._failed
            stopped = self.player._stopped
        except Exception:
            failed = stopped = False
        if failed or stopped:
            return
        _log("idle -> finish")
        self._on_finished()

    def _update_playback_info(self):
        if self.player.mpv is None:
            self.playback_label.setText("Остановлено")
            return
        if not self._playing_key:
            if self._next_timer.isActive():
                self.playback_label.setText(
                    f"Следующая серия через {self._next_remaining} с"
                )
            else:
                saved = self._current_saved_position()
                if saved:
                    self.playback_label.setText(
                        f"Сохранено: {fmt_pos(saved)} — "
                        "«Смотреть» продолжит"
                    )
                else:
                    self.playback_label.setText("Остановлено")
            return
        pos = self._mpv_time_pos()
        if pos is None:
            _log("info: time_pos is None")
            return
        try:
            dur = float(self.player.mpv.duration or 0)
        except (TypeError, ValueError):
            dur = 0.0
        try:
            paused = bool(self.player.mpv.pause)
        except Exception:
            paused = False
        try:
            pfc = bool(self.player.mpv.paused_for_cache)
        except Exception:
            pfc = False

        # Страховка, если mpv не прислал end-file: конец определяем
        # по времени. Требуем два подряд замера у конца, чтобы случайная
        # перемотка/скачок позиции не запускали следующую серию.
        near_end = (
            self._playing_key
            and dur >= 30.0
            and pos >= dur - 1.0
            and not self._finish_latched
            and not self.player.in_seek_grace()
        )
        if near_end:
            self._near_end_count += 1
        else:
            self._near_end_count = 0
        if self._near_end_count >= 2:
            _log("polling finish confirmed:", pos, "/", dur)
            self._on_finished()
            return

        # Видео реально пошло — таймер «HLS не стартовал» больше не нужен.
        if pos > 0.5:
            self._startup_timer.stop()

        # Страховка: если видео уже реально играет, плашки «Подготовка…»
        # и «Буферизация…» не должны висеть.
        if pos > 0.5 and self.video_overlay.isVisible():
            ov_text = self.video_overlay.text()
            if ov_text.startswith("Подготовка") or (
                ov_text.startswith("Буферизация") and not pfc
            ):
                self._hide_overlay()
        state = "⏸" if paused else "▶"
        text = f"{state} {fmt_pos(pos)}"
        if dur:
            text += f" / {fmt_pos(dur)}"
        if self._playing_label:
            text += f" · {self._playing_label}"
        if self._next_timer.isActive():
            text += f" · следующая через {self._next_remaining} с"
        self.playback_label.setText(text)

    def _on_resumed(self, pos: float):
        _log("resumed at", pos)
        self._startup_timer.stop()
        text = f"Продолжаю с {fmt_pos(pos)}"
        self.stream_status.setText(text)
        self._set_overlay(text)
        self._resume_timer.start(4000)

    def _on_resume_timeout(self):
        if self.video_overlay.isVisible():
            text = self.video_overlay.text()
            if text.startswith("Продолжаю") or text.startswith(
                "Подготовка"
            ):
                self._hide_overlay()
        if (
            self.stream_status.text().startswith("Продолжаю")
            and self._play_status_text
        ):
            self.stream_status.setText(self._play_status_text)

    # ---------- следующая серия ----------
    @staticmethod
    def _season_sort_key(sid):
        s = str(sid)
        return (0, int(s)) if s.isdigit() else (1, s)

    @staticmethod
    def _episode_sort_key(ep: core.Episode):
        s = str(ep.episode_id)
        return (0, int(s)) if s.isdigit() else (1, s)

    def _next_episode_after(self, sid: str, eid: str):
        info = self._title_info
        if info is None or not info.is_series:
            _log("next_after: no series info")
            return None
        seasons = dict(self._seasons or {})
        if not seasons:
            seasons = {
                str(k): f"Сезон {k}"
                for k in (self._episodes or {})
            }
        if not seasons:
            _log("next_after: no seasons")
            return None

        season_ids = sorted(seasons.keys(), key=self._season_sort_key)
        sid = str(sid or "")
        eid = str(eid or "")
        if sid not in season_ids:
            sid = str(
                self.season_combo.currentData()
                or (season_ids[0] if season_ids else "")
            )
        if sid not in season_ids:
            _log("next_after: season not found", sid, season_ids)
            return None

        idx = season_ids.index(sid)
        cur_eps = sorted(
            self._episodes.get(sid) or [],
            key=self._episode_sort_key,
        )
        cur_pos = -1
        for i, ep in enumerate(cur_eps):
            if str(ep.episode_id) == eid:
                cur_pos = i
                break
        if cur_pos < 0:
            _log(
                "next_after: episode not found",
                eid, [e.episode_id for e in cur_eps],
            )
            return None

        if cur_pos + 1 < len(cur_eps):
            nxt = cur_eps[cur_pos + 1]
            return {
                "sid": sid,
                "eid": str(nxt.episode_id),
                "season_name": seasons.get(sid, f"Сезон {sid}"),
                "episode_name": nxt.name,
            }

        # Последняя серия сезона — переходим к первому эпизоду
        # следующего сезона, если он есть.
        for nsid in season_ids[idx + 1:]:
            neps = sorted(
                self._episodes.get(nsid) or [],
                key=self._episode_sort_key,
            )
            if not neps:
                continue
            nxt = neps[0]
            return {
                "sid": nsid,
                "eid": str(nxt.episode_id),
                "season_name": seasons.get(nsid, f"Сезон {nsid}"),
                "episode_name": nxt.name,
            }
        _log("next_after: no next season", sid)
        return None

    def _maybe_autoplay_next(self):
        info = self._title_info
        _log(
            "autoplay check:",
            "series=", bool(info and info.is_series),
            "post=", info.post_id if info is not None else None,
            "playing_post=", self._playing_post_id,
            "playing_sid/eid=", self._playing_sid, self._playing_eid,
            "enabled=", self.settings.value(
                "autoplay_next", True, type=bool
            ),
        )
        if info is None or not info.is_series:
            return
        if (
            self._playing_post_id
            and info.post_id != self._playing_post_id
        ):
            _log("autoplay: post_id mismatch")
            return
        if not self.settings.value(
            "autoplay_next", True, type=bool
        ):
            _log("autoplay: disabled in settings")
            return
        nxt = self._next_episode_after(
            self._playing_sid, self._playing_eid
        )
        _log("autoplay next:", nxt)
        if nxt is None:
            self.stream_status.setText("Это была последняя серия")
            return
        try:
            seconds = int(
                self.settings.value("next_timer", 10, type=int)
            )
        except (TypeError, ValueError):
            seconds = 10
        seconds = max(1, min(300, seconds))
        self._start_next_countdown(nxt, seconds)

    def _start_next_countdown(self, nxt: dict, seconds: int):
        _log("countdown start:", nxt, seconds)
        self._startup_timer.stop()
        self._hide_overlay()
        self._next_episode = nxt
        self._next_remaining = max(1, int(seconds))

        self._suppress_ui = True
        sidx = self.season_combo.findData(str(nxt["sid"]))
        if sidx >= 0:
            self.season_combo.setCurrentIndex(sidx)
        self._fill_episodes(
            str(nxt["sid"]), str(nxt["eid"])
        )
        self._suppress_ui = False

        label = f"{nxt['season_name']} · {nxt['episode_name']}"
        self.next_overlay.set_next(label, self._next_remaining)
        try:
            self.video.update()
        except Exception:
            pass
        self.stream_status.setText(
            f"Следующая серия: {label} · через "
            f"{self._next_remaining} с"
        )
        self._next_timer.start()

    def _on_next_tick(self):
        if self._next_episode is None:
            self._next_timer.stop()
            return
        self._next_remaining -= 1
        if self._next_remaining <= 0:
            _log("countdown done, play next")
            self._next_timer.stop()
            self.next_overlay.hide()
            try:
                self.video.update()
            except Exception:
                pass
            self._next_episode = None
            self._play_current()
            return
        self.next_overlay.set_remaining(self._next_remaining)
        nxt = self._next_episode
        label = f"{nxt['season_name']} · {nxt['episode_name']}"
        self.stream_status.setText(
            f"Следующая серия: {label} · через "
            f"{self._next_remaining} с"
        )

    def _cancel_next_timer(self, quiet: bool = False):
        active = (
            self._next_timer.isActive()
            or self.next_overlay.isVisible()
        )
        _log("cancel next timer: active=", active, "quiet=", quiet)
        self._next_timer.stop()
        self.next_overlay.hide()
        try:
            self.video.update()
        except Exception:
            pass
        self._next_episode = None
        if active and not quiet:
            self.stream_status.setText(
                "Автозапуск следующей серии отменён"
            )

    def _on_escape(self):
        now = time.monotonic()
        if now - getattr(self, "_esc_t", 0.0) < 0.35:
            return
        self._esc_t = now
        if (
            self._next_timer.isActive()
            or self.next_overlay.isVisible()
        ):
            self._cancel_next_timer()
            return
        self.leave_fullscreen()

    # ---------- каталог/поиск ----------
    def refresh_clicked(self):
        if self.search.text().strip():
            self._run_search()
        else:
            self.refresh_catalog(reset=True)

    def refresh_catalog(self, reset: bool = True):
        if (
            not reset
            and self._catalog_worker is not None
            and self._catalog_worker.isRunning()
        ):
            return
        if self.search.text().strip():
            self._run_search()
            return
        section = str(self.catalog_combo.currentData() or "home")
        genre = self._catalog_genre if section != "home" else ""
        page = 1 if reset else self._catalog_page + 1
        self._catalog_token += 1
        token = self._catalog_token
        self.refresh_btn.setEnabled(False)
        self.more_btn.setEnabled(False)
        self.statusBar().showMessage(
            "Загрузка каталога…" if reset else f"Загрузка страницы {page}…"
        )
        worker = CatalogWorker(
            self.client, section, page, genre, token, self
        )
        self._catalog_worker = worker
        self._track_worker(worker)
        worker.done.connect(self._on_catalog_done)
        worker.start()

    def _on_catalog_done(self, token: int, result: dict):
        if token != self._catalog_token:
            return
        self.refresh_btn.setEnabled(True)
        self._catalog_worker = None
        err = (result or {}).get("error") or ""
        if err:
            self.statusBar().showMessage("Не удалось загрузить каталог: " + err)
            if not self.catalog_items:
                QMessageBox.critical(
                    self, "Каталог",
                    "Не удалось загрузить каталог:\n\n" + err +
                    "\n\nПроверьте зеркало/прокси в настройках (⚙)."
                )
            return

        items = (result or {}).get("items") or []
        page = int((result or {}).get("page") or 1)
        section = str((result or {}).get("section") or "home")
        genre = str((result or {}).get("genre") or "")
        genres = (result or {}).get("genres") or []
        if page <= 1:
            self._catalog_genre = genre
            self._update_genres(genres, section)

        if page <= 1:
            new_items = list(items)
        else:
            known = {i.url for i in self.catalog_items}
            new_items = self.catalog_items + [
                i for i in items if i.url not in known
            ]
        self._catalog_page = page
        self._has_more = bool((result or {}).get("has_more"))

        # Пока идёт поиск, каталог не должен перебивать его результаты.
        if self.search.text().strip():
            self.catalog_items = new_items
            self.genre_combo.setEnabled(False)
            if section == "home" and page == 1:
                self._save_cache()
            return

        self.catalog_items = new_items
        self.more_btn.setEnabled(self._has_more)
        self._populate_list(self.catalog_items)
        if section == "home" and page == 1:
            self._save_cache()
        msg = f"Загружено: {len(self.catalog_items)}"
        if self._has_more:
            msg += " · есть ещё"
        self.statusBar().showMessage(msg, 6000)

    def _update_genres(self, genres, section: str):
        current = self._catalog_genre
        current_text = self.genre_combo.currentText() if current else ""
        self._suppress_ui = True
        self.genre_combo.clear()
        self.genre_combo.addItem("Все жанры", "")
        for slug, name in genres or []:
            self.genre_combo.addItem(name, slug)
        idx = self.genre_combo.findData(current)
        if idx < 0 and current:
            # На странице выбранного жанра сайт часто убирает его из меню.
            # Возвращаем пункт, чтобы фильтр не сбрасывался на «Все жанры».
            self.genre_combo.insertItem(
                1, current_text or current, current
            )
            idx = 1
        if idx < 0:
            idx = 0
            self._catalog_genre = ""
        self.genre_combo.setCurrentIndex(idx)
        enabled = (
            section != "home"
            and self.genre_combo.count() > 1
            and not self.search.text().strip()
        )
        self.genre_combo.setEnabled(enabled)
        self._suppress_ui = False

    def _on_genre_changed(self):
        if self._suppress_ui:
            return
        self._catalog_genre = str(self.genre_combo.currentData() or "")
        self._catalog_page = 1
        self._has_more = False
        self.catalog_items = []
        self.refresh_catalog(reset=True)

    def load_more(self):
        if not self._has_more:
            return
        if self.search.text().strip():
            return
        self.refresh_catalog(reset=False)

    def _on_catalog_changed(self):
        if self._suppress_ui:
            return
        if self.search.text():
            self.search.blockSignals(True)
            self.search.clear()
            self.search.blockSignals(False)
        self._search_active = False
        self._search_token += 1          # отменяем возможный старый поиск
        self._catalog_genre = ""
        self._catalog_page = 1
        self._has_more = False
        self.catalog_items = []
        self._suppress_ui = True
        self.genre_combo.clear()
        self.genre_combo.addItem("Все жанры", "")
        self.genre_combo.setEnabled(False)
        self._suppress_ui = False
        self.refresh_catalog(reset=True)

    def _on_search_changed(self, text: str):
        # Инвалидируем уже запущенный поиск: его результат уже не нужен.
        self._search_token += 1
        if not text.strip():
            self._search_timer.stop()
            self._search_active = False
            self._populate_list(self.catalog_items)
            self.more_btn.setEnabled(self._has_more)
            section = str(self.catalog_combo.currentData() or "home")
            self.genre_combo.setEnabled(
                section != "home" and self.genre_combo.count() > 1
            )
            return
        self.genre_combo.setEnabled(False)
        self._search_timer.start()

    def _run_search(self):
        query = self.search.text().strip()
        if not query:
            return
        self._search_token += 1
        token = self._search_token
        self._search_active = True
        self.more_btn.setEnabled(False)
        self.refresh_btn.setEnabled(False)
        self.statusBar().showMessage(f"Поиск: {query}…")
        worker = SearchWorker(self.client, query, token, self)
        self._search_worker = worker
        self._track_worker(worker)
        worker.done.connect(self._on_search_done)
        worker.start()

    def _on_search_done(self, token: int, result: dict):
        if token != self._search_token:
            return
        self.refresh_btn.setEnabled(True)
        self._search_worker = None
        err = (result or {}).get("error") or ""
        if err:
            self.statusBar().showMessage("Поиск не удался: " + err)
            return
        items = (result or {}).get("items") or []
        query = (result or {}).get("query") or ""
        self._populate_list(items, select_url="")
        self.genre_combo.setEnabled(False)
        if items:
            self.statusBar().showMessage(
                f"Найдено: {len(items)} · {query}", 6000
            )
        else:
            self.statusBar().showMessage(
                f"Ничего не найдено: {query}", 6000
            )

    # ---------- список ----------
    def _populate_list(
        self, items: List[core.SearchItem], select_url: Optional[str] = None
    ):
        current = self._current_url if select_url is None else select_url
        self._suppress_selection = True
        self.list.clear()

        groups: Dict[str, List[core.SearchItem]] = {}
        for item in items:
            groups.setdefault(item.kind or "Другое", []).append(item)

        for kind in sorted(groups, key=core._kinds_key):
            group = QTreeWidgetItem([kind])
            font = group.font(0)
            font.setBold(True)
            group.setFont(0, font)
            group.setFlags(group.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            group.setToolTip(0, f"{kind}: {len(groups[kind])}")
            for item in groups[kind]:
                label = item.title
                if item.year:
                    label = f"{label} ({item.year})"
                if item.genres:
                    label = f"{label}  ·  {item.genres}"
                node = QTreeWidgetItem([label])
                node.setData(
                    0, Qt.ItemDataRole.UserRole, item.to_dict()
                )
                tip = []
                if item.meta:
                    tip.append(item.meta)
                if item.rating:
                    tip.append("Рейтинг: " + item.rating)
                if item.info:
                    tip.append(item.info)
                node.setToolTip(0, "\n".join(tip) or item.title)
                group.addChild(node)
            self.list.addTopLevelItem(group)
            group.setExpanded(True)

        self._suppress_selection = False

        target = self._find_item(current) if current else None
        if target is None:
            if (
                current
                and self._startup_resume_url
                and current == self._startup_resume_url
            ):
                # Последний тайтл из поиска может отсутствовать в каталоге;
                # не подменяем его первым попавшимся.
                target = None
            elif select_url is None and self._playing_key:
                # Фоновое обновление каталога не должно переключать
                # правую панель во время воспроизведения.
                target = None
            else:
                target = self._first_item()
        if target is not None:
            self.list.setCurrentItem(target)
            self.list.scrollToItem(target)

    def _find_item(self, url: str):
        if not url:
            return None
        for i in range(self.list.topLevelItemCount()):
            group = self.list.topLevelItem(i)
            for j in range(group.childCount()):
                node = group.child(j)
                data = node.data(0, Qt.ItemDataRole.UserRole)
                if isinstance(data, dict) and data.get("url") == url:
                    return node
        return None

    def _first_item(self):
        for i in range(self.list.topLevelItemCount()):
            group = self.list.topLevelItem(i)
            if group.childCount():
                return group.child(0)
        return None

    def _on_selection_changed(self):
        if self._suppress_selection:
            return
        selected = self.list.selectedItems()
        if not selected:
            return
        data = selected[0].data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(data, dict):
            return
        url = str(data.get("url") or "")
        if not url:
            return
        if url == self._current_url and (
            self._title_info is not None
            or self._title_worker is not None
        ):
            return
        self._current_url = url
        self._load_title(url)

    def _on_item_double_click(self, _item, _column):
        if self._title_info is not None:
            self._play_current()
        else:
            self._pending_play = True

    # ---------- тайтл ----------
    def _load_title(self, url: str):
        self.save_position()
        self._cancel_next_timer(quiet=True)
        self._resume_timer.stop()
        self._title_token += 1
        token = self._title_token
        # старые ответы по сериям/потоку больше не актуальны
        self._episodes_token += 1
        self._stream_token += 1
        self._title_info = None
        self._episodes = {}
        self._seasons = {}
        self._last_stream = None
        self._last_chosen = None
        self._last_play_url = ""
        self._play_status_text = ""
        self._startup_timer.stop()
        self._hide_overlay()
        self.copy_btn.setEnabled(False)
        self.play_btn.setEnabled(False)
        self.title_label.setText("Загрузка…")
        self.meta_label.setText("")
        self.rating_label.setText("")
        self.desc.setPlainText("")
        self._suppress_ui = True
        self.translation_combo.clear()
        self.season_combo.clear()
        self.episode_combo.clear()
        self._suppress_ui = False
        self._set_poster_placeholder("…")
        if not self._playing_key:
            self.stream_status.setText("Загрузка информации…")
        self.statusBar().showMessage("Загрузка тайтла…")

        worker = TitleWorker(self.client, url, self)
        self._title_worker = worker
        self._track_worker(worker)
        worker.done.connect(
            lambda u, res, t=token: self._on_title_done(u, res, t)
        )
        worker.start()

    def _on_title_done(self, url: str, result: dict, token: int):
        if token != self._title_token:
            return
        self._title_worker = None
        err = (result or {}).get("error") or ""
        if err:
            self.title_label.setText("Ошибка загрузки")
            self.meta_label.setText("")
            self.desc.setPlainText(err)
            self.stream_status.setText("Ошибка: " + err)
            self.statusBar().showMessage("Ошибка: " + err)
            self._set_poster_placeholder("!")
            return
        info: core.TitleInfo = (result or {}).get("info")
        if info is None:
            return
        self._apply_title(info)

    def _is_startup_resume(self, info: Optional[core.TitleInfo]) -> bool:
        if not self._startup_resume_url or info is None:
            return False
        pid = str(
            self.settings.value("last_play_post_id", "", type=str)
        )
        if pid and pid == info.post_id:
            return True
        return bool(info.url) and info.url == self._startup_resume_url

    def _autoplay_startup_now(self):
        if self._closing or not self._autoplay_last_pending:
            return
        if (
            self._title_info is None
            or not self._is_startup_resume(self._title_info)
            or self._playing_key
        ):
            self._autoplay_last_pending = False
            return
        self._autoplay_last_pending = False
        self._play_current()

    def _apply_title(self, info: core.TitleInfo):
        self._title_info = info
        if self._startup_resume_url and not self._is_startup_resume(info):
            self._autoplay_last_pending = False
        restore = False
        saved_tid = saved_sid = saved_eid = ""
        last_pid = str(
            self.settings.value("last_play_post_id", "", type=str)
        )
        last_url = str(
            self.settings.value("last_play_url", "", type=str)
        )
        matched = bool(last_pid) and last_pid == info.post_id
        if not last_pid:
            matched = bool(last_url) and last_url == info.url
        if matched:
            restore = True
            saved_tid = str(
                self.settings.value("last_play_tid", "", type=str)
            )
            saved_sid = str(
                self.settings.value("last_play_sid", "", type=str)
            )
            saved_eid = str(
                self.settings.value("last_play_eid", "", type=str)
            )

        self._current_url = info.url or self._current_url
        self._episodes = dict(info.episodes)
        self._seasons = dict(info.seasons)
        if info.is_series and not self._seasons:
            self._seasons = {"1": "Сезон 1"}

        self.title_label.setText(info.title)
        meta = [info.year, info.country, info.genres, info.duration]
        self.meta_label.setText(
            " · ".join(p for p in meta if p)
        )
        self.rating_label.setText(info.rating)
        self.desc.setPlainText(
            info.description or "Описание отсутствует."
        )
        self._load_poster(info.poster)

        self._suppress_ui = True
        self.translation_combo.clear()
        translators = list(info.translators)
        if not translators and info.default_translator:
            translators = [
                core.Translator(
                    id=info.default_translator, name="По умолчанию"
                )
            ]

        # PRO-переводы недоступны без подписки — не показываем их.
        # Если это PRO-only тайтл, оставляем список, иначе нечего выбрать.
        usable = [tr for tr in translators if not tr.premium]
        pro_only = bool(translators) and not usable
        if not usable:
            usable = translators

        for tr in usable:
            label = tr.name + (" (PRO)" if tr.premium else "")
            self.translation_combo.addItem(label, tr.id)

        idx = self.translation_combo.findData(info.default_translator)
        if restore and saved_tid:
            saved_idx = self.translation_combo.findData(saved_tid)
            if saved_idx >= 0:
                idx = saved_idx
        if idx < 0 and self.translation_combo.count():
            idx = 0
        if idx >= 0:
            self.translation_combo.setCurrentIndex(idx)

        for widget in (
            self.season_label, self.season_combo,
            self.episode_label, self.episode_combo,
        ):
            widget.setVisible(info.is_series)

        if info.is_series:
            preferred_season = info.default_season
            preferred_episode = ""
            if restore:
                if saved_sid:
                    preferred_season = saved_sid
                preferred_episode = saved_eid
            # Начальное состояние: сезон со страницы (или сохранённый),
            # серия 1 (или сохранённая).
            self._fill_seasons(
                info.seasons or self._seasons,
                preferred_season,
                preferred_episode=preferred_episode,
            )
        else:
            self.season_combo.clear()
            self.episode_combo.clear()
        self._suppress_ui = False

        self.play_btn.setEnabled(True)
        note = ""
        if pro_only:
            note = " · только PRO-переводы (нужен аккаунт с подпиской)"
        if not self._playing_key:
            self.stream_status.setText(
                f"{info.kind} · {info.year}"
                + (" · PRO-контент" if info.premium else "")
                + note
            )
        self.statusBar().showMessage(
            f"{info.title} · {info.kind} · {info.year}", 6000
        )

        # Для сериала подтягиваем полную карту сезонов/серий именно для
        # выбранного (не PRO) перевода.
        if info.is_series:
            tid = str(self.translation_combo.currentData() or "")
            if tid:
                self._start_episodes_worker(
                    tid,
                    preserve_episode=False,
                    preferred_episode=(saved_eid if restore else None),
                )

        if (
            self._autoplay_last_pending
            and not info.is_series
            and self._is_startup_resume(info)
        ):
            QTimer.singleShot(150, self._autoplay_startup_now)

        if self._pending_play:
            self._pending_play = False
            self._play_current()

    def _fill_seasons(
        self, seasons: Dict[str, str], preferred_season: str,
        preferred_episode: str = "",
    ):
        prev = self._suppress_ui
        self._suppress_ui = True
        self.season_combo.clear()
        items = sorted(
            (seasons or {}).items(),
            key=lambda kv: (
                int(kv[0]) if str(kv[0]).isdigit() else 10 ** 9
            ),
        )
        for sid, name in items:
            self.season_combo.addItem(name, sid)
        idx = self.season_combo.findData(str(preferred_season or ""))
        if idx < 0 and self.season_combo.count():
            idx = 0
        if idx >= 0:
            self.season_combo.setCurrentIndex(idx)
        self._fill_episodes(
            str(self.season_combo.currentData() or ""),
            preferred_episode,
        )
        self._suppress_ui = prev

    def _fill_episodes(
        self, season_id: str, preferred_episode: str = ""
    ):
        prev = self._suppress_ui
        self._suppress_ui = True
        self.episode_combo.clear()
        episodes = list(self._episodes.get(str(season_id)) or [])
        episodes.sort(
            key=lambda e: (
                int(e.episode_id) if e.episode_id.isdigit() else 10 ** 9
            )
        )
        for ep in episodes:
            label = ep.name + (" (PRO)" if ep.premium else "")
            self.episode_combo.addItem(label, ep.episode_id)
        if not episodes:
            self.episode_combo.addItem("—", "")
        idx = self.episode_combo.findData(str(preferred_episode or ""))
        if idx < 0 and self.episode_combo.count():
            # По умолчанию — первая серия по порядку.
            idx = 0
        if idx >= 0:
            self.episode_combo.setCurrentIndex(idx)
        self._suppress_ui = prev

    def _start_episodes_worker(
        self, tid: str, preserve_episode: bool,
        preferred_episode: Optional[str] = None,
    ):
        info = self._title_info
        if info is None or not info.is_series or not tid:
            return
        self._episodes_token += 1
        token = self._episodes_token
        if preferred_episode is not None:
            self._episodes_preferred_episode = str(
                preferred_episode or ""
            )
        elif preserve_episode:
            self._episodes_preferred_episode = str(
                self.episode_combo.currentData() or ""
            )
        else:
            self._episodes_preferred_episode = ""
        self.statusBar().showMessage("Загрузка серий…", 2000)
        if not self._playing_key:
            self.stream_status.setText("Загрузка серий…")
        worker = EpisodesWorker(
            self.client, info.post_id, tid, info.url, token, self
        )
        self._track_worker(worker)
        worker.done.connect(self._on_episodes_done)
        worker.start()

    def _on_translation_changed(self):
        if self._suppress_ui or self._title_info is None:
            return
        self._autoplay_last_pending = False
        self._cancel_next_timer(quiet=True)
        info = self._title_info
        tid = str(self.translation_combo.currentData() or "")
        if not tid:
            return
        if not info.is_series:
            self.stream_status.setText(f"Перевод выбран: {tid}")
            return
        self._start_episodes_worker(tid, preserve_episode=True)

    def _on_episodes_done(
        self, referer: str, translator_id: str, token: int, result: dict
    ):
        if token != self._episodes_token:
            return
        if self._title_info is None or referer != self._title_info.url:
            return
        if str(self.translation_combo.currentData() or "") != translator_id:
            return
        err = (result or {}).get("error") or ""
        if err:
            self.stream_status.setText("Ошибка серий: " + err)
            if (
                self._autoplay_last_pending
                and self._title_info is not None
                and self._is_startup_resume(self._title_info)
            ):
                QTimer.singleShot(150, self._autoplay_startup_now)
            return
        self._seasons = (result or {}).get("seasons") or {}
        self._episodes = (result or {}).get("episodes") or {}
        preferred_season = str(self.season_combo.currentData() or "1")
        preferred_episode = getattr(
            self, "_episodes_preferred_episode", ""
        )
        self._fill_seasons(
            self._seasons, preferred_season, preferred_episode
        )
        total = sum(len(v) for v in self._episodes.values())
        self.statusBar().showMessage(
            f"Сезонов: {len(self._seasons)} · серий: {total}", 3000
        )
        if not self._playing_key:
            self.stream_status.setText(
                f"Сезонов: {len(self._seasons)} · серий: {total}"
            )

        if (
            self._autoplay_last_pending
            and self._title_info is not None
            and self._is_startup_resume(self._title_info)
        ):
            QTimer.singleShot(150, self._autoplay_startup_now)

    def _on_season_changed(self):
        if self._suppress_ui or self._title_info is None:
            return
        self._autoplay_last_pending = False
        self._cancel_next_timer(quiet=True)
        # При смене сезона начинаем с первой серии.
        self._fill_episodes(
            str(self.season_combo.currentData() or ""),
            preferred_episode="",
        )

    def _on_episode_changed(self):
        if self._suppress_ui:
            return
        self._autoplay_last_pending = False
        self._cancel_next_timer(quiet=True)

    def _on_quality_changed(self):
        if self._suppress_ui:
            return
        self.settings.setValue("quality", self.quality_combo.currentData())
        self.settings.sync()

    # ---------- поток ----------
    def _play_current(self):
        info = self._title_info
        if info is None:
            return
        _log("play_current:", self._selection(info))
        self._autoplay_last_pending = False
        if self.player.mpv is None:
            QMessageBox.warning(
                self, "mpv",
                "Плеер mpv недоступен:\n\n"
                + (self.player.error or "неизвестная ошибка")
            )
            return

        tid = str(self.translation_combo.currentData() or "")
        if not tid:
            tid = info.default_translator
        if not tid:
            self.stream_status.setText("Не выбран перевод")
            return

        if info.is_series:
            sid = str(self.season_combo.currentData() or "")
            eid = str(self.episode_combo.currentData() or "")
            if not sid or not eid:
                self.stream_status.setText("Выберите сезон и серию")
                return
        else:
            sid, eid = "1", "1"

        self._cancel_next_timer(quiet=True)
        self._startup_timer.stop()
        self._resume_timer.stop()
        self._hide_overlay()
        self._play_status_text = ""
        self._pending_selection = (tid, sid, eid)
        self._stream_token += 1
        token = self._stream_token
        self.play_btn.setEnabled(False)
        self.stream_status.setText("Получение ссылки на поток…")
        self.statusBar().showMessage("Получение ссылки…")
        quality = str(self.quality_combo.currentData() or "auto")
        worker = StreamWorker(
            self.client, info.post_id, tid, sid, eid, info.is_series,
            quality, info.url, token, self,
        )
        self._track_worker(worker)
        worker.done.connect(self._on_stream_done)
        worker.start()

    def _on_stream_done(self, token: int, result: dict):
        if token != self._stream_token:
            return
        self.play_btn.setEnabled(True)
        err = (result or {}).get("error") or ""
        if err:
            self._startup_timer.stop()
            self._hide_overlay()
            self.stream_status.setText("Ошибка: " + err)
            self.statusBar().showMessage("Не удалось получить поток: " + err)
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Warning)
            box.setWindowTitle("Поток")
            box.setText("Не удалось получить ссылку")
            box.setInformativeText(
                err
                + "\n\nПопробуйте ещё раз, выберите другой перевод, "
                  "источник (HLS/MP4) или сбросьте сессию в настройках (⚙)."
            )
            retry = box.addButton(
                "Повторить", QMessageBox.ButtonRole.AcceptRole
            )
            box.addButton("Закрыть", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            if box.clickedButton() is retry:
                QTimer.singleShot(50, self._play_current)
            return

        stream: core.StreamInfo = (result or {}).get("stream")
        chosen: core.Quality = (result or {}).get("chosen")
        if stream is None or chosen is None:
            return
        self._last_stream = stream
        self._start_playback(stream, chosen)

    def _start_playback(
        self, stream: core.StreamInfo, chosen: core.Quality
    ):
        # Позицию предыдущего потока сохраняем до смены ключа.
        self.save_position()
        self._last_chosen = chosen
        info = self._title_info
        title = info.title if info is not None else APP_TITLE
        referer = info.url if info is not None else (self.client.base + "/")

        source = str(self.source_combo.currentData() or "auto")
        hls, mp4 = chosen.hls, chosen.mp4

        if source == "mp4":
            url = mp4 or hls
            is_hls = not mp4 and bool(hls)
            alt = ""
        elif source == "hls":
            url = hls or mp4
            is_hls = bool(hls)
            alt = ""
        else:  # auto: HLS, при долгом старте — MP4
            url = hls or mp4
            is_hls = bool(hls)
            alt = mp4 if (hls and mp4) else ""

        if not url:
            self.stream_status.setText(
                "Нет ссылки для выбранного источника"
            )
            return

        kind = "HLS" if is_hls else "MP4"
        tid, sid, eid = getattr(
            self, "_pending_selection", ("", "1", "1")
        )
        if not tid and info is not None:
            tid, sid, eid = self._selection(info)
        key = (
            f"{info.post_id}|{tid}|{sid}|{eid}"
            if info is not None and tid else ""
        )
        self._playing_key = key
        self._finish_latched = False
        self._near_end_count = 0
        self._playing_post_id = info.post_id if info is not None else ""
        self._playing_title = title
        self._playing_tid = tid
        self._playing_sid = sid
        self._playing_eid = eid
        self._playing_label = f"{chosen.label} · {kind}"
        if info is not None:
            s = self.settings
            s.setValue("last_play_url", info.url)
            s.setValue("last_play_post_id", info.post_id)
            s.setValue("last_play_title", title)
            s.setValue("last_play_tid", tid)
            s.setValue("last_play_sid", sid)
            s.setValue("last_play_eid", eid)
            s.sync()

        start_pos = 0.0
        saved = self._positions.get(key) if key else None
        if saved is None and info is not None and tid and eid:
            # Если перевод в прошлый раз был другим (например, PRO-перевод
            # скрыт), ищем позицию той же серии в другом переводе.
            prefix = f"{info.post_id}|"
            suffix = f"|{sid}|{eid}"
            candidates = [
                (k, v)
                for k, v in self._positions.items()
                if k.startswith(prefix) and k.endswith(suffix)
            ]
            if candidates:
                candidates.sort(
                    key=lambda kv: int(
                        kv[1].get("updated") or 0
                    ),
                    reverse=True,
                )
                saved = candidates[0][1]
                _log("fallback position:", candidates[0][0])
        if saved:
            try:
                spos = float(saved.get("pos") or 0)
                sdur = float(saved.get("duration") or 0)
            except (TypeError, ValueError):
                spos, sdur = 0.0, 0.0
            if spos >= 3 and (not sdur or spos < sdur - 15):
                start_pos = spos
        self._play_start_pos = start_pos
        _log(
            "start:", key,
            "saved:", bool(saved),
            "start_pos:", start_pos,
        )

        text = (
            f"Поток: {chosen.label} · {kind} · до {fmt_ts(stream.expires)}"
            f" · качеств: {len(stream.qualities)}"
        )
        if chosen.premium:
            text += " · PRO"
        self._play_status_text = text
        self._last_play_url = url
        self.copy_btn.setEnabled(True)
        self.stream_status.setText(text)
        self._play_alt_url = alt
        self._play_title = title
        self._play_referer = referer
        self._fallback_tried = False
        self._play_url(
            url, title, referer, start_pos=start_pos
        )
        self._set_overlay(f"Подготовка {kind}…")
        self.statusBar().showMessage(
            f"Запуск: {title} · {chosen.label} · {kind}", 6000
        )
        # Если первый сегмент не открылся за 10 секунд — пробуем MP4.
        self._startup_timer.start(HLS_START_TIMEOUT_MS)

    def _play_url(
        self, url: str, title: str, referer: str,
        start_pos: float = 0.0,
    ):
        if not url:
            return
        fields = [
            f"User-Agent: {core.USER_AGENT}",
            f"Referer: {referer or (self.client.base + '/')}",
            f"Origin: {self.client.base}",
            "Accept: */*",
        ]
        proxy = self._mpv_proxy()
        if self._proxy_url() and not proxy:
            self.statusBar().showMessage(
                "https-прокси не передан в mpv (нужен http:// или socks://)",
                10000,
            )
        self.player.play(
            url,
            headers=fields,
            proxy=proxy,
            title=title or APP_TITLE,
            start_pos=start_pos,
        )
        try:
            self.video.setFocus()
        except Exception:
            pass

    def _on_buffering(self, text: str):
        _log("buffering:", repr(text))
        if not text:
            self._startup_timer.stop()
            # Оставляем только сообщение о продолжении просмотра.
            # «Подготовка…» и «Буферизация…» должны скрываться всегда.
            keep_resume = (
                self._resume_timer.isActive()
                and self.video_overlay.isVisible()
                and self.video_overlay.text().startswith("Продолжаю")
            )
            if not keep_resume:
                self._hide_overlay()
                if self._play_status_text:
                    self.stream_status.setText(self._play_status_text)
            return
        self._set_overlay(text)

    def _set_overlay(self, text: str):
        if not text:
            self._hide_overlay()
            return
        self.video_overlay.show_text(text)
        try:
            self.video.update()
        except Exception:
            pass

    def _hide_overlay(self):
        self.video_overlay.hide()
        try:
            self.video.update()
        except Exception:
            pass

    def _on_startup_timeout(self):
        if self._closing:
            return
        source = str(self.source_combo.currentData() or "auto")

        # Авто: HLS не стартовал за 10 секунд — пробуем прямой MP4.
        if (
            source == "auto"
            and not self._fallback_tried
            and self._play_alt_url
        ):
            self._fallback_tried = True
            alt = self._play_alt_url
            title = self._play_title
            referer = self._play_referer
            self._play_alt_url = ""
            self._set_overlay(
                "HLS долго не начинается.\nПереключаюсь на MP4…"
            )
            self.stream_status.setText(
                "HLS медленный — переключаюсь на MP4…"
            )
            self.player.stop()

            def _fallback():
                if self._closing:
                    return
                self._play_url(
                    alt, title, referer,
                    start_pos=self._play_start_pos,
                )
                self._set_overlay("Подготовка MP4…")
                self._play_status_text = self._play_status_text.replace(
                    " · HLS ·", " · MP4 ·"
                )
                self.stream_status.setText(self._play_status_text)
                self._startup_timer.start(MP4_START_TIMEOUT_MS)

            QTimer.singleShot(300, _fallback)
            return

        self._set_overlay(
            "Поток долго не начинается.\n"
            "Попробуйте сменить источник (HLS/MP4)\n"
            "или выбрать другой перевод."
        )
        self.stream_status.setText("Долгая загрузка потока…")

    def _on_source_changed(self):
        if self._suppress_ui:
            return
        source = str(self.source_combo.currentData() or "auto")
        self.settings.setValue("source", source)
        self.settings.sync()

        # Если уже что-то играет — перезапускаем с новым источником.
        if self._last_stream is None or self._title_info is None:
            return
        try:
            path = str(self.player.mpv.path or "") if self.player.mpv else ""
        except Exception:
            path = ""
        if path:
            QTimer.singleShot(50, self._play_current)

    def _copy_link(self):
        if not self._last_play_url:
            return
        QApplication.clipboard().setText(self._last_play_url)
        self.statusBar().showMessage("Ссылка скопирована в буфер обмена", 5000)

    def _on_player_failed(self, text: str):
        self._startup_timer.stop()
        self._hide_overlay()
        self.stream_status.setText("Ошибка mpv: " + text)
        self.statusBar().showMessage("Ошибка mpv: " + text)

    def _mpv_log(self, level, prefix, text):
        if os.environ.get("HDREZKA_MPV_LOG"):
            print(f"[mpv] {level} {prefix}: {text}", file=sys.stderr)

    def _on_mpv_key(self, action: str):
        if action == "pause":
            self.player.toggle_pause()
        elif action == "fullscreen":
            self.toggle_fullscreen()
        elif action == "escape":
            self._on_escape()
        elif action == "vol_up":
            self.player.volume(5)
        elif action == "vol_down":
            self.player.volume(-5)
        elif action == "mute":
            self.player.command("cycle", "mute")

    # ---------- постер ----------
    def _set_poster_placeholder(self, name: str):
        self.poster_label.setPixmap(QPixmap())
        self.poster_label.setText(initials(name))
        self.poster_label.setStyleSheet(
            "background:#e7edf0; border-radius:8px; color:#5b6b73; "
            "font-weight:600; font-size:22px;"
        )

    def _apply_poster(self, pix: QPixmap):
        if pix is None or pix.isNull():
            return
        self.poster_label.setText("")
        self.poster_label.setStyleSheet(
            "background:transparent; border-radius:8px;"
        )
        self.poster_label.setPixmap(
            pix.scaled(
                112, 160,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def _load_poster(self, url: str):
        if not url:
            name = self._title_info.title if self._title_info else "HDrezka"
            self._set_poster_placeholder(name)
            return
        if url == self._poster_url:
            return
        self._poster_url = url
        cached = self._poster_cache.get(url)
        if cached is not None and not cached.isNull():
            self._apply_poster(cached)
            return
        name = self._title_info.title if self._title_info else "…"
        self._set_poster_placeholder(name)
        req = QNetworkRequest(QUrl(url))
        req.setRawHeader(b"User-Agent", core.USER_AGENT.encode())
        req.setRawHeader(
            b"Referer", (self.client.base + "/").encode()
        )
        try:
            req.setTransferTimeout(15000)
        except AttributeError:
            pass
        reply = self._poster_mgr.get(req)
        self._poster_replies[reply] = url
        reply.finished.connect(lambda r=reply: self._on_poster_reply(r))

    def _on_poster_reply(self, reply):
        url = self._poster_replies.pop(reply, "")
        try:
            data = bytes(reply.readAll())
        except Exception:
            data = b""
        reply.deleteLater()
        if not data:
            return
        pix = QPixmap()
        ok = pix.loadFromData(data) and not pix.isNull()
        if not ok:
            try:
                from PIL import Image

                im = Image.open(io.BytesIO(data)).convert("RGBA")
                buf = io.BytesIO()
                im.save(buf, "PNG")
                pix = QPixmap()
                ok = pix.loadFromData(buf.getvalue()) and not pix.isNull()
            except Exception:
                ok = False
        if not ok:
            return
        if url:
            self._poster_cache[url] = pix
        if url == self._poster_url:
            self._apply_poster(pix)

    # ---------- панели/полный экран ----------
    def _focus_search(self):
        self.search.setFocus()
        self.search.selectAll()

    def toggle_list(self):
        self._list_visible = not self._list_visible
        if not self.isFullScreen():
            self.list.setVisible(self._list_visible)
        self.list_btn.setText(
            "Скрыть список" if self._list_visible else "Показать список"
        )

    def toggle_info(self):
        self._info_visible = not self._info_visible
        if not self.isFullScreen():
            self.info_panel.setVisible(self._info_visible)
        self.info_btn.setText(
            "Скрыть инфо" if self._info_visible else "Показать инфо"
        )

    def toggle_fullscreen(self):
        now = time.monotonic()
        if now - getattr(self, "_fs_t", 0.0) < 0.4:
            return
        self._fs_t = now
        if self.isFullScreen():
            self.leave_fullscreen()
        else:
            self.enter_fullscreen()

    def enter_fullscreen(self):
        if self.isFullScreen():
            return
        self._was_maximized = self.isMaximized()
        self.controls.hide()
        self.list.hide()
        self.info_panel.hide()
        self.statusBar().hide()
        self.centralWidget().layout().setContentsMargins(0, 0, 0, 0)
        self.showFullScreen()
        self.player.set_fullscreen(True)

    def leave_fullscreen(self):
        if not self.isFullScreen():
            return
        self.controls.show()
        self.list.setVisible(self._list_visible)
        self.info_panel.setVisible(self._info_visible)
        self.statusBar().show()
        self.centralWidget().layout().setContentsMargins(9, 9, 9, 9)
        if self._was_maximized:
            self.showMaximized()
        else:
            self.showNormal()
        self.player.set_fullscreen(False)

    def _on_mpv_fullscreen(self, value: bool):
        if value and not self.isFullScreen():
            self.enter_fullscreen()
        elif not value and self.isFullScreen():
            self.leave_fullscreen()

    # ---------- настройки ----------
    def open_settings(self):
        old_base = normalize_base(
            self.settings.value("base_url", core.DEFAULT_BASE, type=str)
        )
        old_proxy = (
            self.settings.value("proxy_enabled", False, type=bool),
            self.settings.value("proxy", "", type=str),
        )
        old_hwdec = self.settings.value("hwdec", "auto-safe", type=str)
        old_source = self.settings.value("source", "auto", type=str)

        if self.settings_dlg.exec() != QDialog.DialogCode.Accepted:
            return

        new_base = normalize_base(
            self.settings.value("base_url", core.DEFAULT_BASE, type=str)
        )
        new_proxy = (
            self.settings.value("proxy_enabled", False, type=bool),
            self.settings.value("proxy", "", type=str),
        )
        new_hwdec = self.settings.value("hwdec", "auto-safe", type=str)
        new_quality = self.settings.value("quality", "auto", type=str)
        new_source = self.settings.value("source", "auto", type=str)

        if old_hwdec != new_hwdec:
            self.player.set_hwdec(str(new_hwdec))

        q_idx = self.quality_combo.findData(new_quality)
        self._suppress_ui = True
        self.quality_combo.setCurrentIndex(q_idx if q_idx >= 0 else 0)
        s_idx = self.source_combo.findData(new_source)
        self.source_combo.setCurrentIndex(s_idx if s_idx >= 0 else 0)
        self._suppress_ui = False

        if old_base != new_base or old_proxy != new_proxy:
            self._catalog_token += 1
            self._search_token += 1
            self._title_token += 1
            self._episodes_token += 1
            self._stream_token += 1
            self._startup_timer.stop()
            self._hide_overlay()
            self.client = self._new_client()
            self._poster_cache.clear()
            self._poster_url = ""
            self._title_info = None
            self._current_url = ""
            self._last_stream = None
            self._last_play_url = ""
            self.catalog_items = []
            self.copy_btn.setEnabled(False)
            self.play_btn.setEnabled(False)
            self.refresh_catalog(reset=True)
        elif old_source != new_source:
            # Источник поменялся — перезапускаем текущий поток.
            if self._last_stream is not None and self._title_info is not None:
                try:
                    path = (
                        str(self.player.mpv.path or "")
                        if self.player.mpv else ""
                    )
                except Exception:
                    path = ""
                if path:
                    QTimer.singleShot(50, self._play_current)

    # ---------- потоки ----------
    def _track_worker(self, worker: QThread):
        self._workers.add(worker)
        worker.finished.connect(
            lambda w=worker: self._workers.discard(w)
        )
        worker.finished.connect(worker.deleteLater)

    def closeEvent(self, event):
        if self._closing:
            event.accept()
            return
        self._closing = True
        self._startup_timer.stop()
        self._resume_timer.stop()
        self._pos_timer.stop()
        self._next_timer.stop()
        self._info_timer.stop()
        self.next_overlay.hide()
        self.save_position()
        self._hide_overlay()
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("splitter_state", self.splitter.saveState())
        self.settings.setValue("last_url", self._current_url)
        self.settings.sync()

        try:
            self.player.terminate()
        except Exception:
            pass

        deadline = time.monotonic() + 2.0
        for w in list(self._workers):
            if w.isRunning():
                left = int((deadline - time.monotonic()) * 1000)
                if left <= 0:
                    break
                try:
                    w.wait(left)
                except Exception:
                    pass
        for w in list(_PROXY_WORKERS):
            if w.isRunning():
                try:
                    w.wait(500)
                except Exception:
                    pass
        try:
            self.client.close()
        except Exception:
            pass
        event.accept()


# --------------------------------------------------------------------------
# Точка входа
# --------------------------------------------------------------------------

def main() -> int:
    QApplication.setApplicationName(APP_NAME)
    QApplication.setOrganizationName(ORG_NAME)
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setApplicationDisplayName(APP_TITLE)
    try:
        locale.setlocale(locale.LC_NUMERIC, "C")
    except locale.Error:
        pass

    try:
        import mpv as mpv_module
    except (OSError, ImportError) as e:
        what = (
            "не установлен python-mpv"
            if isinstance(e, ImportError)
            else "не найдена libmpv.so.2"
        )
        QMessageBox.critical(
            None, APP_TITLE,
            f"Плеер недоступен: {what}\n\n"
            "sudo apt install mpv libmpv2\n"
            "python3 -m pip install python-mpv\n\n" + str(e),
        )
        return 1

    win = MainWindow(mpv_module)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
