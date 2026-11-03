#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Блокировка screensaver/засыпания экрана во время просмотра.

Linux:
  * D-Bus: org.xfce.ScreenSaver (XFCE), org.freedesktop.ScreenSaver,
    org.mate.ScreenSaver, org.cinnamon.ScreenSaver,
    org.gnome.SessionManager, org.freedesktop.PowerManagement;
  * XFCE: временно включается presentation mode в xfce4-power-manager;
  * X11: XScreenSaverSuspend + DPMS + сброс idle-таймера (XTest Shift);
  * systemd-inhibit (если есть systemd).
Windows: SetThreadExecutionState.
macOS: caffeinate.

Периодический сброс idle-таймера нужен потому, что некоторые хранители
экрана (например, xfce4-screensaver/xfce4-power-manager в OpenRC-сборках)
не реагируют на XScreenSaverSuspend и D-Bus-Inhibit. XResetScreenSaver
не обновляет время последнего ввода, поэтому дополнительно отправляется
невидимое нажатие Shift через XTest.
"""

from __future__ import annotations

import atexit
import ctypes
import ctypes.util
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time


def _log(*args):
    if os.environ.get("HDREZKA_LOG"):
        print("[hdrezka]", *args, file=sys.stderr)


# D-Bus basic type codes.
_DBUS_TYPE_STRING = ord("s")
_DBUS_TYPE_UINT32 = ord("u")
_DBUS_TYPE_BOOLEAN = ord("b")
_DBUS_BUS_SESSION = 0


class _DBusConnection:
    """Минимальный клиент libdbus через ctypes.

    Нужен, чтобы держать одно постоянное подключение к сессионной шине:
    хранители экрана снимают Inhibit, когда процесс-вызыватель отключается
    от D-Bus, поэтому dbus-send/gdbus для этого не подходят.
    """

    def __init__(self):
        self._lib = None
        self._conn = None
        if not sys.platform.startswith("linux"):
            return
        name = ctypes.util.find_library("dbus-1") or "libdbus-1.so.3"
        if not name:
            _log("libdbus-1 not found")
            return
        try:
            lib = ctypes.CDLL(name)
            lib.dbus_error_init.argtypes = [ctypes.c_void_p]
            lib.dbus_error_init.restype = None
            lib.dbus_error_free.argtypes = [ctypes.c_void_p]
            lib.dbus_error_free.restype = None
            lib.dbus_bus_get_private.argtypes = [
                ctypes.c_int, ctypes.c_void_p
            ]
            lib.dbus_bus_get_private.restype = ctypes.c_void_p
            lib.dbus_connection_set_exit_on_disconnect.argtypes = [
                ctypes.c_void_p, ctypes.c_int
            ]
            lib.dbus_connection_close.argtypes = [ctypes.c_void_p]
            lib.dbus_connection_unref.argtypes = [ctypes.c_void_p]
            lib.dbus_message_new_method_call.argtypes = [
                ctypes.c_char_p, ctypes.c_char_p,
                ctypes.c_char_p, ctypes.c_char_p,
            ]
            lib.dbus_message_new_method_call.restype = ctypes.c_void_p
            lib.dbus_message_iter_init_append.argtypes = [
                ctypes.c_void_p, ctypes.c_void_p
            ]
            lib.dbus_message_iter_init.argtypes = [
                ctypes.c_void_p, ctypes.c_void_p
            ]
            lib.dbus_message_iter_init.restype = ctypes.c_int
            lib.dbus_message_iter_append_basic.argtypes = [
                ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p
            ]
            lib.dbus_message_iter_append_basic.restype = ctypes.c_int
            lib.dbus_message_iter_get_arg_type.argtypes = [ctypes.c_void_p]
            lib.dbus_message_iter_get_arg_type.restype = ctypes.c_int
            lib.dbus_message_iter_get_basic.argtypes = [
                ctypes.c_void_p, ctypes.c_void_p
            ]
            lib.dbus_connection_send_with_reply_and_block.argtypes = [
                ctypes.c_void_p, ctypes.c_void_p,
                ctypes.c_int, ctypes.c_void_p,
            ]
            lib.dbus_connection_send_with_reply_and_block.restype = (
                ctypes.c_void_p
            )
            lib.dbus_message_unref.argtypes = [ctypes.c_void_p]
            try:
                lib.dbus_threads_init_default()
            except Exception:
                pass
            self._lib = lib
        except Exception as e:
            _log("libdbus init:", e)

    @property
    def ready(self):
        return self._conn is not None

    def open(self):
        if self._conn is not None:
            return True
        if self._lib is None:
            return False
        err = ctypes.create_string_buffer(256)
        self._lib.dbus_error_init(err)
        conn = self._lib.dbus_bus_get_private(_DBUS_BUS_SESSION, err)
        if not conn:
            _log("dbus connect failed:", self._err_text(err))
            self._lib.dbus_error_free(err)
            return False
        self._lib.dbus_error_free(err)
        self._lib.dbus_connection_set_exit_on_disconnect(conn, 0)
        self._conn = conn
        return True

    def close(self):
        conn = self._conn
        self._conn = None
        if conn is None or self._lib is None:
            return
        try:
            self._lib.dbus_connection_close(conn)
        except Exception:
            pass
        try:
            self._lib.dbus_connection_unref(conn)
        except Exception:
            pass

    @staticmethod
    def _err_text(buf):
        try:
            parts = ctypes.cast(
                buf, ctypes.POINTER(ctypes.c_char_p)
            )
            msg = parts[1]
            if msg:
                return msg.decode("utf-8", "replace")
            name = parts[0]
            if name:
                return name.decode("utf-8", "replace")
        except Exception:
            pass
        return "unknown error"

    def _call(self, dest, path, method, args):
        lib = self._lib
        if lib is None or self._conn is None:
            return None
        msg = lib.dbus_message_new_method_call(
            dest.encode(), path.encode(),
            method.rsplit(".", 1)[0].encode(),
            method.rsplit(".", 1)[1].encode(),
        )
        if not msg:
            return None
        reply = None
        try:
            it = ctypes.create_string_buffer(128)
            lib.dbus_message_iter_init_append(msg, it)
            for kind, value in args:
                if kind == "s":
                    if not isinstance(value, bytes):
                        value = str(value).encode("utf-8")
                    holder = ctypes.c_char_p(value)
                    ok = lib.dbus_message_iter_append_basic(
                        it, _DBUS_TYPE_STRING, ctypes.byref(holder)
                    )
                elif kind == "u":
                    holder = ctypes.c_uint32(int(value))
                    ok = lib.dbus_message_iter_append_basic(
                        it, _DBUS_TYPE_UINT32, ctypes.byref(holder)
                    )
                else:
                    ok = 0
                if not ok:
                    _log("dbus append failed:", method)
                    return None
            err = ctypes.create_string_buffer(256)
            lib.dbus_error_init(err)
            reply = lib.dbus_connection_send_with_reply_and_block(
                self._conn, msg, 2000, err
            )
            if not reply:
                _log("dbus call failed:", method, self._err_text(err))
            lib.dbus_error_free(err)
        except Exception as e:
            _log("dbus call error:", method, e)
        finally:
            lib.dbus_message_unref(msg)
        return reply

    def _reply_value(self, reply, type_code, holder):
        lib = self._lib
        if lib is None or not reply:
            return None
        it = ctypes.create_string_buffer(128)
        if not lib.dbus_message_iter_init(reply, it):
            return None
        if lib.dbus_message_iter_get_arg_type(it) != type_code:
            return None
        lib.dbus_message_iter_get_basic(it, ctypes.byref(holder))
        return holder.value

    def call_uint(self, dest, path, method, args):
        reply = self._call(dest, path, method, args)
        if not reply:
            return None
        try:
            return self._reply_value(
                reply, _DBUS_TYPE_UINT32, ctypes.c_uint32()
            )
        finally:
            self._lib.dbus_message_unref(reply)

    def call_bool(self, dest, path, method, args):
        reply = self._call(dest, path, method, args)
        if not reply:
            return None
        try:
            value = self._reply_value(
                reply, _DBUS_TYPE_BOOLEAN, ctypes.c_int()
            )
            return None if value is None else bool(value)
        finally:
            self._lib.dbus_message_unref(reply)

    def call_void(self, dest, path, method, args):
        reply = self._call(dest, path, method, args)
        if reply:
            self._lib.dbus_message_unref(reply)
            return True
        return False



class ScreenInhibitor:
    """Блокирует screensaver/засыпание экрана, пока идёт просмотр."""

    #: сервисы хранителей экрана: (метка, имя в D-Bus, путь, интерфейс)
    SCREENSAVERS = (
        (
            "xfce",
            "org.xfce.ScreenSaver",
            "/org/xfce/ScreenSaver",
            "org.xfce.ScreenSaver",
        ),
        (
            "freedesktop",
            "org.freedesktop.ScreenSaver",
            "/org/freedesktop/ScreenSaver",
            "org.freedesktop.ScreenSaver",
        ),
        (
            "mate",
            "org.mate.ScreenSaver",
            "/org/mate/ScreenSaver",
            "org.mate.ScreenSaver",
        ),
        (
            "cinnamon",
            "org.cinnamon.ScreenSaver",
            "/org/cinnamon/ScreenSaver",
            "org.cinnamon.ScreenSaver",
        ),
    )

    POKE_INTERVAL = 20.0

    def __init__(self):
        self.active = False
        self.backend = ""
        self._dbus = _DBusConnection()
        self._dbus_locks = []
        self._sim_targets = []
        self._proc = None
        self._x11_dpy = None
        self._x11 = None
        self._xss = None
        self._x11_suspended = False
        self._dpms_disabled = False
        self._stop = None
        self._thread = None
        self._pokes = 0
        self._closed = False
        self._xfce_pres = False
        self._xfce_pres_old = ""
        self._lifecycle_q = queue.Queue()
        self._lifecycle_thread = None
        self._lifecycle_lock = threading.Lock()
        self._shutdown = False
        atexit.register(self._atexit_release)

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------
    def acquire(self):
        if self.active or self._shutdown:
            return
        self._closed = False
        parts = []
        try:
            if sys.platform.startswith("win"):
                self._acquire_windows()
                parts.append(self.backend)
            elif sys.platform == "darwin":
                self._acquire_macos()
                parts.append(self.backend)
            else:
                parts = self._acquire_linux()
        except Exception as e:
            _log("screen inhibitor error:", e)
            if os.environ.get("HDREZKA_LOG"):
                import traceback

                traceback.print_exc()
        self.backend = "+".join(p for p in parts if p)
        self.active = bool(self.backend)
        _log(
            "screen inhibitor:",
            "on" if self.active else "off",
            self.backend or "(нет доступного способа)",
        )

    def release(self):
        if self._closed:
            return
        self._closed = True
        if not self.active and not self._thread:
            return
        try:
            self._release_backend()
        except Exception as e:
            _log("screen inhibitor release error:", e)
        self.active = False
        _log("screen inhibitor: off")

    def _atexit_release(self):
        try:
            self._shutdown = True
            if (
                self._lifecycle_thread is not None
                and self._lifecycle_thread.is_alive()
            ):
                self.release_async(wait=2.0)
            else:
                self.release()
        except Exception:
            pass

    # ---------- асинхронный жизненный цикл ----------
    # D-Bus/xfconf-вызовы могут подвиснуть (особенно в момент гашения
    # экрана), поэтому в GUI их нельзя делать в потоке интерфейса.
    def acquire_async(self):
        self._ensure_lifecycle()
        self._lifecycle_q.put(("acquire", None))

    def release_async(self, wait=None):
        self._ensure_lifecycle()
        done = threading.Event()
        self._lifecycle_q.put(("release", done))
        if wait is not None:
            done.wait(wait)

    def shutdown(self):
        """Останавливает фоновые действия при выходе из приложения."""
        self._shutdown = True
        stop = self._stop
        if stop is not None:
            stop.set()

    def _ensure_lifecycle(self):
        with self._lifecycle_lock:
            thread = self._lifecycle_thread
            if thread is not None and thread.is_alive():
                return
            thread = threading.Thread(
                target=self._lifecycle_loop,
                name="screen-lifecycle",
                daemon=True,
            )
            self._lifecycle_thread = thread
            thread.start()

    def _lifecycle_loop(self):
        while True:
            job, done = self._lifecycle_q.get()
            if job is None:
                if done is not None:
                    done.set()
                return
            try:
                if job == "acquire":
                    self.acquire()
                elif job == "release":
                    self.release()
            except Exception as e:
                _log("screen lifecycle error:", e)
            finally:
                if done is not None:
                    done.set()

    # ------------------------------------------------------------------
    # Linux
    # ------------------------------------------------------------------
    def _acquire_linux(self):
        _log(
            "screen inhibitor env:",
            "session=", os.environ.get("XDG_SESSION_TYPE", "?"),
            "display=", bool(os.environ.get("DISPLAY")),
            "wayland=", bool(os.environ.get("WAYLAND_DISPLAY")),
        )
        parts = []

        if self._dbus.open():
            _log("libdbus: connected to session bus")
        else:
            _log("libdbus: session bus unavailable, using CLI fallback")

        dbus_parts = self._acquire_dbus()
        for label, ok in dbus_parts:
            if ok:
                parts.append(label)

        # XFCE: presentation mode надёжно отключает гашение экрана
        # в xfce4-power-manager (D-Bus-Inhibit делает это не всегда).
        if any(
            label == "dbus:power" and ok
            for label, ok in dbus_parts
        ):
            if self._xfce_presentation(True):
                parts.append("xfce-presentation")

        if self._try_x11():
            parts.append("x11-suspend")

        if self._start_keepalive():
            parts.append("keepalive")

        # systemd-inhibit — дополнительная страховка для сна системы.
        if not parts and self._try_systemd_inhibit():
            parts.append("systemd-inhibit")

        return parts

    # ---------- XFCE presentation mode ----------
    @staticmethod
    def _process_running(name):
        try:
            for pid in os.listdir("/proc"):
                if not pid.isdigit():
                    continue
                try:
                    with open(
                        os.path.join("/proc", pid, "comm"), "r"
                    ) as fh:
                        if fh.read().strip() == name:
                            return True
                except OSError:
                    continue
        except OSError:
            pass
        return False

    def _xfce_presentation(self, enable):
        """Включает/выключает presentation mode в xfce4-power-manager."""
        exe = shutil.which("xfconf-query")
        if not exe:
            return False
        channel = "xfce4-power-manager"
        prop = "/xfce4-power-manager/presentation-mode"
        marker = os.path.join(
            os.path.expanduser("~/.cache/hdrezka-gui"),
            "xfce_presentation_mode",
        )
        if enable:
            # Если прошлый запуск завершился аварийно и оставил
            # presentation mode включённым — сначала вернём значение.
            try:
                with open(marker, "r") as fh:
                    stale = fh.read().strip()
                if stale in ("true", "false"):
                    self._run(
                        [exe, "-c", channel, "-p", prop, "-s", stale]
                    )
                    _log(
                        "xfce presentation mode: recovered", stale
                    )
                os.remove(marker)
            except OSError:
                pass

            probe = self._run([exe, "-c", channel, "-p", prop])
            exists = probe is not None and probe.returncode == 0
            old = (probe.stdout or "").strip() if exists else ""
            self._xfce_pres_old = old
            if exists:
                result = self._run(
                    [exe, "-c", channel, "-p", prop, "-s", "true"]
                )
            else:
                result = self._run(
                    [
                        exe, "-c", channel, "-p", prop,
                        "-n", "-t", "bool", "-s", "true",
                    ]
                )
            if result is not None and result.returncode == 0:
                self._xfce_pres = True
                try:
                    os.makedirs(os.path.dirname(marker), exist_ok=True)
                    with open(marker, "w") as fh:
                        fh.write(
                            old if old in ("true", "false") else "false"
                        )
                except OSError:
                    pass
                _log(
                    "xfce presentation mode: on (was",
                    old or "unknown", ")",
                )
                return True
            err = (result.stderr or "").strip() if result else ""
            _log("xfce presentation mode: failed", err)
            return False

        if not self._xfce_pres:
            return False
        restore = (
            self._xfce_pres_old
            if self._xfce_pres_old in ("true", "false")
            else "false"
        )
        result = self._run(
            [exe, "-c", channel, "-p", prop, "-s", restore]
        )
        self._xfce_pres = False
        if result is not None and result.returncode == 0:
            try:
                os.remove(marker)
            except OSError:
                pass
        _log("xfce presentation mode: restored", restore)
        return result is not None and result.returncode == 0

    # ---------- D-Bus ----------
    @staticmethod
    def _run(cmd):
        try:
            return subprocess.run(
                cmd, capture_output=True, text=True, timeout=5
            )
        except (FileNotFoundError, subprocess.SubprocessError, OSError):
            return None

    @staticmethod
    def _cookie_from(text):
        m = re.search(r"uint32\s+(\d+)", text or "")
        if m:
            return int(m.group(1))
        m = re.search(r"\((\d+),?\)", text or "")
        return int(m.group(1)) if m else None

    def _gdbus(self, dest, path, method, args):
        cmd = [
            "gdbus", "call", "--session",
            "--dest", dest,
            "--object-path", path,
            "--method", method,
        ] + list(args)
        p = self._run(cmd)
        if p is None or p.returncode != 0:
            return None
        return (p.stdout or "").strip()

    def _dbus_send(self, dest, path, method, args):
        cmd = [
            "dbus-send", "--session", "--print-reply",
            f"--dest={dest}", path, method,
        ] + list(args)
        p = self._run(cmd)
        if p is None or p.returncode != 0:
            return None
        return (p.stdout or "").strip()

    def _has_owner(self, name):
        if self._dbus.ready:
            value = self._dbus.call_bool(
                "org.freedesktop.DBus",
                "/org/freedesktop/DBus",
                "org.freedesktop.DBus.NameHasOwner",
                [("s", name)],
            )
            if value is not None:
                _log("dbus name", name, "owned:", value)
                return value
        out = self._dbus_send(
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus.NameHasOwner",
            [f"string:{name}"],
        )
        if out is None:
            out = self._gdbus(
                "org.freedesktop.DBus",
                "/org/freedesktop/DBus",
                "org.freedesktop.DBus.NameHasOwner",
                [name],
            )
        result = "true" in (out or "").lower()
        _log("dbus name", name, "owned:", result)
        return result

    def _inhibit(self, label, dest, path, iface):
        """Вызывает Inhibit(app, reason) -> cookie."""
        method = iface + ".Inhibit"
        cookie = None
        via = "cli"
        if self._dbus.ready:
            cookie = self._dbus.call_uint(
                dest, path, method,
                [("s", "HDrezka"), ("s", "Video playback")],
            )
            if cookie is not None:
                via = "libdbus"
            else:
                _log("libdbus inhibit failed:", label)
        if cookie is None:
            out = self._dbus_send(
                dest, path, method,
                ["string:HDrezka", "string:Video playback"],
            )
            if out is None:
                out = self._gdbus(
                    dest, path, method, ["HDrezka", "Video playback"]
                )
            cookie = self._cookie_from(out or "")
        _log("inhibit", label, "cookie:", cookie, "via:", via)
        if cookie is None:
            return False
        self._dbus_locks.append(
            {
                "label": label,
                "dest": dest,
                "path": path,
                "un_method": iface + ".UnInhibit",
                "cookie": cookie,
                "via": via,
            }
        )
        return True

    def _inhibit_gnome(self):
        cookie = None
        via = "cli"
        args = [
            ("s", "HDrezka"), ("u", 0),
            ("s", "Video playback"), ("u", 8),
        ]
        if self._dbus.ready:
            cookie = self._dbus.call_uint(
                "org.gnome.SessionManager",
                "/org/gnome/SessionManager",
                "org.gnome.SessionManager.Inhibit",
                args,
            )
            if cookie is not None:
                via = "libdbus"
        if cookie is None:
            out = self._dbus_send(
                "org.gnome.SessionManager",
                "/org/gnome/SessionManager",
                "org.gnome.SessionManager.Inhibit",
                [
                    "string:HDrezka", "uint32:0",
                    "string:Video playback", "uint32:8",
                ],
            )
            if out is None:
                out = self._gdbus(
                    "org.gnome.SessionManager",
                    "/org/gnome/SessionManager",
                    "org.gnome.SessionManager.Inhibit",
                    ["HDrezka", "0", "Video playback", "8"],
                )
            cookie = self._cookie_from(out or "")
        _log("inhibit gnome cookie:", cookie, "via:", via)
        if cookie is None:
            return False
        self._dbus_locks.append(
            {
                "label": "gnome",
                "dest": "org.gnome.SessionManager",
                "path": "/org/gnome/SessionManager",
                "un_method": "org.gnome.SessionManager.Uninhibit",
                "cookie": cookie,
                "via": via,
            }
        )
        return True

    def _acquire_dbus(self):
        """Пробует все доступные D-Bus-ингибиторы.

        Возвращает список (метка, успех) для формирования backend.
        """
        result = []
        found_screensaver = False

        for label, dest, path, iface in self.SCREENSAVERS:
            if not self._has_owner(dest):
                continue
            found_screensaver = True
            if self._inhibit(label, dest, path, iface):
                result.append((f"dbus:{label}", True))
                self._sim_targets.append((dest, path, iface))
            else:
                result.append((f"dbus:{label}", False))

        if not found_screensaver and self._has_owner(
            "org.gnome.SessionManager"
        ):
            if self._inhibit_gnome():
                result.append(("dbus:gnome", True))
                self._sim_targets.append(
                    (
                        "org.gnome.ScreenSaver",
                        "/org/gnome/ScreenSaver",
                        "org.gnome.ScreenSaver",
                    )
                )
            else:
                result.append(("dbus:gnome", False))

        # Запрет энергосбережения/DPMS в менеджере питания.
        power_dest = ""
        if self._has_owner("org.freedesktop.PowerManagement"):
            power_dest = "org.freedesktop.PowerManagement"
        elif self._has_owner("org.xfce.PowerManager"):
            power_dest = "org.xfce.PowerManager"
        if power_dest:
            if self._inhibit(
                "power",
                power_dest,
                "/org/freedesktop/PowerManagement/Inhibit",
                "org.freedesktop.PowerManagement.Inhibit",
            ):
                result.append(("dbus:power", True))
            else:
                result.append(("dbus:power", False))

        return result

    def _simulate_activity(self):
        for dest, path, iface in self._sim_targets:
            out = self._gdbus(
                dest, path, iface + ".SimulateUserActivity", []
            )
            if out is None:
                self._dbus_send(
                    dest, path, iface + ".SimulateUserActivity", []
                )

    # ---------- X11 / DPMS ----------
    @staticmethod
    def _open_x11():
        """Открывает отдельное X11-подключение.

        Возвращает (x11, xss, dpy, xext, xtst) или
        (None, None, None, None, None).
        """
        if not os.environ.get("DISPLAY"):
            return None, None, None, None, None
        x11_name = ctypes.util.find_library("X11")
        if not x11_name:
            return None, None, None, None, None
        x11 = ctypes.CDLL(x11_name)
        x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x11.XOpenDisplay.restype = ctypes.c_void_p
        x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
        x11.XResetScreenSaver.argtypes = [ctypes.c_void_p]
        x11.XResetScreenSaver.restype = ctypes.c_int
        x11.XFlush.argtypes = [ctypes.c_void_p]
        x11.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
        x11.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        x11.XKeysymToKeycode.restype = ctypes.c_ubyte
        x11.XGetScreenSaver.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
        ]
        x11.XGetScreenSaver.restype = ctypes.c_int
        dpy = x11.XOpenDisplay(None)
        if not dpy:
            _log("x11: cannot open display")
            return None, None, None, None, None

        xss = None
        xss_name = ctypes.util.find_library("Xss")
        if xss_name:
            try:
                xss_lib = ctypes.CDLL(xss_name)
                xss_lib.XScreenSaverQueryExtension.argtypes = [
                    ctypes.c_void_p,
                    ctypes.POINTER(ctypes.c_int),
                    ctypes.POINTER(ctypes.c_int),
                ]
                xss_lib.XScreenSaverQueryExtension.restype = ctypes.c_int
                xss_lib.XScreenSaverSuspend.argtypes = [
                    ctypes.c_void_p, ctypes.c_bool
                ]
                xss_lib.XScreenSaverSuspend.restype = None
                ev = ctypes.c_int()
                er = ctypes.c_int()
                if xss_lib.XScreenSaverQueryExtension(
                    dpy, ctypes.byref(ev), ctypes.byref(er)
                ):
                    xss = xss_lib
            except Exception as e:
                _log("xss init:", e)

        xext = None
        xext_name = ctypes.util.find_library("Xext")
        if xext_name:
            try:
                xext_lib = ctypes.CDLL(xext_name)
                xext_lib.DPMSQueryExtension.argtypes = [
                    ctypes.c_void_p,
                    ctypes.POINTER(ctypes.c_int),
                    ctypes.POINTER(ctypes.c_int),
                ]
                xext_lib.DPMSQueryExtension.restype = ctypes.c_int
                xext_lib.DPMSInfo.argtypes = [
                    ctypes.c_void_p,
                    ctypes.POINTER(ctypes.c_ushort),
                    ctypes.POINTER(ctypes.c_int),
                ]
                xext_lib.DPMSInfo.restype = ctypes.c_int
                xext_lib.DPMSDisable.argtypes = [ctypes.c_void_p]
                xext_lib.DPMSDisable.restype = ctypes.c_int
                xext_lib.DPMSEnable.argtypes = [ctypes.c_void_p]
                xext_lib.DPMSEnable.restype = ctypes.c_int
                xext_lib.DPMSGetTimeouts.argtypes = [
                    ctypes.c_void_p,
                    ctypes.POINTER(ctypes.c_ushort),
                    ctypes.POINTER(ctypes.c_ushort),
                    ctypes.POINTER(ctypes.c_ushort),
                ]
                xext_lib.DPMSGetTimeouts.restype = ctypes.c_int
                xext = xext_lib
            except Exception as e:
                _log("xext init:", e)

        # XTest нужен для «настоящего» сброса idle-таймера: XResetScreenSaver
        # не обновляет время последнего ввода, которое читают xautolock,
        # xscreensaver и xfce4-power-manager.
        xtst = None
        xtst_name = ctypes.util.find_library("Xtst")
        if xtst_name:
            try:
                xtst_lib = ctypes.CDLL(xtst_name)
                xtst_lib.XTestQueryExtension.argtypes = [
                    ctypes.c_void_p,
                    ctypes.POINTER(ctypes.c_int),
                    ctypes.POINTER(ctypes.c_int),
                    ctypes.POINTER(ctypes.c_int),
                    ctypes.POINTER(ctypes.c_int),
                ]
                xtst_lib.XTestQueryExtension.restype = ctypes.c_int
                xtst_lib.XTestFakeKeyEvent.argtypes = [
                    ctypes.c_void_p, ctypes.c_uint,
                    ctypes.c_int, ctypes.c_ulong,
                ]
                xtst_lib.XTestFakeKeyEvent.restype = ctypes.c_int
                ev = ctypes.c_int()
                er = ctypes.c_int()
                maj = ctypes.c_int()
                mnr = ctypes.c_int()
                if xtst_lib.XTestQueryExtension(
                    dpy, ctypes.byref(ev), ctypes.byref(er),
                    ctypes.byref(maj), ctypes.byref(mnr),
                ):
                    xtst = xtst_lib
            except Exception as e:
                _log("xtst init:", e)

        return x11, xss, dpy, xext, xtst

    def _dpms_disable(self, x11, dpy, xext):
        if xext is None or not dpy:
            return False
        try:
            ev = ctypes.c_int()
            er = ctypes.c_int()
            if not xext.DPMSQueryExtension(
                dpy, ctypes.byref(ev), ctypes.byref(er)
            ):
                _log("dpms: extension not available")
                return False
            level = ctypes.c_ushort()
            state = ctypes.c_int()
            if not xext.DPMSInfo(
                dpy, ctypes.byref(level), ctypes.byref(state)
            ):
                return False
            # Если DPMS уже выключен — не трогаем, чтобы потом не включить.
            if not state.value:
                _log("dpms: already disabled")
                return False
            xext.DPMSDisable(dpy)
            x11.XSync(dpy, 0)
            self._dpms_disabled = True
            _log("dpms: disabled for playback")
            return True
        except Exception as e:
            _log("dpms disable:", e)
            return False

    def _try_x11(self):
        try:
            x11, xss, dpy, _xext, _xtst = self._open_x11()
            if not dpy:
                return False
            if xss is None:
                x11.XCloseDisplay(dpy)
                _log("x11: XScreenSaver extension not available")
                return False
            xss.XScreenSaverSuspend(dpy, True)
            x11.XFlush(dpy)
            self._x11 = x11
            self._xss = xss
            self._x11_dpy = dpy
            self._x11_suspended = True
            return True
        except Exception as e:
            _log("x11 inhibitor:", e)
            return False

    def _start_keepalive(self):
        if self._shutdown:
            return False
        if not (self._dbus_locks or os.environ.get("DISPLAY")):
            return False
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._keepalive_loop,
            args=(self._stop,),
            name="screen-keepalive",
            daemon=True,
        )
        self._thread.start()
        return True

    def _keepalive_loop(self, stop):
        """Периодически сбрасывает idle-таймеры X11 и D-Bus."""
        x11 = xss = dpy = xext = xtst = None
        dpms = False
        shift_code = 0
        xscreensaver_cmd = shutil.which("xscreensaver-command")
        xscreensaver_run = self._process_running("xscreensaver")
        xautolock_run = self._process_running("xautolock")
        _log(
            "keepalive tools:",
            "xscreensaver=", xscreensaver_run,
            "xautolock=", xautolock_run,
        )
        try:
            x11, xss, dpy, xext, xtst = self._open_x11()
            if dpy:
                timeout = ctypes.c_int()
                interval = ctypes.c_int()
                prefer = ctypes.c_int()
                allow = ctypes.c_int()
                if x11.XGetScreenSaver(
                    dpy, ctypes.byref(timeout), ctypes.byref(interval),
                    ctypes.byref(prefer), ctypes.byref(allow),
                ):
                    _log(
                        "x11 saver: timeout=", timeout.value,
                        "interval=", interval.value,
                    )
                if xext is not None:
                    st = ctypes.c_ushort()
                    su = ctypes.c_ushort()
                    off = ctypes.c_ushort()
                    if xext.DPMSGetTimeouts(
                        dpy, ctypes.byref(st),
                        ctypes.byref(su), ctypes.byref(off),
                    ):
                        _log(
                            "dpms timeouts:", st.value,
                            su.value, off.value,
                        )
                dpms = self._dpms_disable(x11, dpy, xext)
                if xss is not None:
                    xss.XScreenSaverSuspend(dpy, True)
                x11.XResetScreenSaver(dpy)
                if xtst is not None:
                    shift_code = int(
                        x11.XKeysymToKeycode(dpy, 0xFFE1)
                    )
                self._poke_x11(x11, dpy, xtst, shift_code)
            if xscreensaver_cmd:
                self._run([xscreensaver_cmd, "-deactivate"])
        except Exception as e:
            _log("keepalive init:", e)

        try:
            while not stop.wait(self.POKE_INTERVAL):
                try:
                    if dpy:
                        if xss is not None:
                            xss.XScreenSaverSuspend(dpy, True)
                        x11.XResetScreenSaver(dpy)
                    self._poke_x11(x11, dpy, xtst, shift_code)
                    if xscreensaver_cmd:
                        self._run([xscreensaver_cmd, "-deactivate"])
                    self._simulate_activity()
                    self._pokes += 1
                    _log(
                        "keepalive poke:", self._pokes,
                        "x11:", bool(dpy),
                        "dbus:", len(self._sim_targets),
                        "dpms:", dpms,
                        "xtest:", shift_code,
                    )
                except Exception as e:
                    _log("keepalive poke error:", e)
        finally:
            try:
                if dpms and dpy and xext is not None:
                    xext.DPMSEnable(dpy)
                    x11.XSync(dpy, 0)
                    dpms = False
                    self._dpms_disabled = False
                    _log("dpms: enabled again")
                if xss is not None and dpy:
                    xss.XScreenSaverSuspend(dpy, False)
                if dpy:
                    x11.XCloseDisplay(dpy)
            except Exception as e:
                _log("keepalive cleanup:", e)

    @staticmethod
    def _poke_x11(x11, dpy, xtst, shift_code):
        """Сбрасывает реальный idle-таймер X11 (XTest Shift)."""
        if not dpy or x11 is None:
            return
        if xtst is not None and shift_code:
            try:
                xtst.XTestFakeKeyEvent(dpy, shift_code, 1, 0)
                xtst.XTestFakeKeyEvent(dpy, shift_code, 0, 0)
            except Exception as e:
                _log("xtest poke:", e)
        try:
            x11.XFlush(dpy)
        except Exception:
            pass

    # ---------- systemd ----------
    def _try_systemd_inhibit(self):
        try:
            def _pdeathsig():
                try:
                    libc = ctypes.CDLL("libc.so.6", use_errno=True)
                    # PR_SET_PDEATHSIG = 1, SIGTERM = 15
                    libc.prctl(1, 15)
                except Exception:
                    pass

            self._proc = subprocess.Popen(
                [
                    "systemd-inhibit",
                    "--what=idle:sleep",
                    "--who=HDrezka",
                    "--why=Video playback",
                    "--mode=block",
                    "sleep", "infinity",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                preexec_fn=_pdeathsig,
            )
            time.sleep(0.15)
            if self._proc.poll() is not None:
                self._proc = None
                return False
            return True
        except Exception as e:
            _log("systemd-inhibit:", e)
            self._proc = None
            return False

    # ------------------------------------------------------------------
    # Windows / macOS
    # ------------------------------------------------------------------
    def _acquire_windows(self):
        try:
            kernel32 = ctypes.windll.kernel32
            ES_CONTINUOUS = 0x80000000
            ES_DISPLAY_REQUIRED = 0x00000002
            ES_SYSTEM_REQUIRED = 0x00000001
            kernel32.SetThreadExecutionState(
                ES_CONTINUOUS | ES_DISPLAY_REQUIRED | ES_SYSTEM_REQUIRED
            )
            self.backend = "windows"
        except Exception as e:
            _log("windows inhibitor:", e)
            self.backend = ""

    def _acquire_macos(self):
        try:
            self._proc = subprocess.Popen(
                [
                    "caffeinate", "-d", "-i",
                    "-w", str(os.getpid()),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.backend = "caffeinate"
        except Exception as e:
            _log("caffeinate:", e)
            self._proc = None
            self.backend = ""

    # ------------------------------------------------------------------
    # Освобождение ресурсов
    # ------------------------------------------------------------------
    def _release_backend(self):
        # Сначала останавливаем keepalive: он вернёт DPMS.
        stop = self._stop
        thread = self._thread
        self._stop = None
        self._thread = None
        if stop is not None:
            stop.set()
        if thread is not None and thread.is_alive():
            thread.join(timeout=3)

        # Если keepalive не успел вернуть DPMS (например, завис на D-Bus),
        # включаем его отдельным подключением.
        if self._dpms_disabled:
            try:
                x11, _xss, dpy, xext, _xtst = self._open_x11()
                if dpy:
                    if xext is not None:
                        xext.DPMSEnable(dpy)
                        x11.XSync(dpy, 0)
                        _log("dpms: enabled again (release)")
                    x11.XCloseDisplay(dpy)
                self._dpms_disabled = False
            except Exception as e:
                _log("dpms release:", e)

        # Возвращаем настройку XFCE.
        self._xfce_presentation(False)

        # Снимаем D-Bus-блокировки.
        for lock in self._dbus_locks:
            done = False
            if lock.get("via") == "libdbus" and self._dbus.ready:
                done = self._dbus.call_void(
                    lock["dest"],
                    lock["path"],
                    lock["un_method"],
                    [("u", lock["cookie"])],
                )
            if not done:
                out = self._dbus_send(
                    lock["dest"],
                    lock["path"],
                    lock["un_method"],
                    [f"uint32:{lock['cookie']}"],
                )
                if out is None:
                    self._gdbus(
                        lock["dest"],
                        lock["path"],
                        lock["un_method"],
                        [str(lock["cookie"])],
                    )
            _log("uninhibit", lock["label"], lock["cookie"])
        self._dbus_locks = []
        self._sim_targets = []
        # Закрываем постоянное подключение: вместе с ним снимаются
        # все оставшиеся D-Bus-блокировки.
        self._dbus.close()

        # XScreenSaverSuspend.
        if self._x11_suspended and self._x11_dpy and self._xss:
            try:
                self._xss.XScreenSaverSuspend(self._x11_dpy, False)
                self._x11.XFlush(self._x11_dpy)
            except Exception:
                pass
        if self._x11_dpy and self._x11 is not None:
            try:
                self._x11.XCloseDisplay(self._x11_dpy)
            except Exception:
                pass

        # systemd-inhibit.
        if self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=2)
            except Exception:
                pass

        # Windows / macOS.
        if self.backend == "windows":
            try:
                ctypes.windll.kernel32.SetThreadExecutionState(
                    0x80000000
                )
            except Exception:
                pass
        if self.backend == "caffeinate" and self._proc is not None:
            try:
                self._proc.terminate()
            except Exception:
                pass

        self._proc = None
        self._x11_dpy = None
        self._x11 = None
        self._xss = None
        self._x11_suspended = False
        self._pokes = 0


if __name__ == "__main__":
    # Диагностика: python3 screen_inhibitor.py [секунды]
    obj = ScreenInhibitor()
    obj.acquire()
    print("backend:", obj.backend or "(нет)")
    print("active:", obj.active)
    wait = 3.0
    if len(sys.argv) > 1:
        try:
            wait = float(sys.argv[1])
        except ValueError:
            pass
    try:
        time.sleep(wait)
    except KeyboardInterrupt:
        pass
    obj.release()
    print("released")
