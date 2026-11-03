#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Блокировка screensaver/засыпания экрана во время просмотра."""

from __future__ import annotations

import ctypes
import os
import re
import subprocess
import sys
import time


def _log(*args):
    if os.environ.get("HDREZKA_LOG"):
        print("[hdrezka]", *args, file=sys.stderr)


class ScreenInhibitor:
    """Блокирует screensaver/засыпание экрана, пока идёт просмотр."""

    def __init__(self):
        self.active = False
        self.backend = ""
        self._cookie = None
        self._dbus_kind = ""
        self._proc = None
        self._x11_dpy = None
        self._x11 = None
        self._xss = None

    def acquire(self):
        if self.active:
            return
        try:
            if sys.platform.startswith("win"):
                self._acquire_windows()
            elif sys.platform == "darwin":
                self._acquire_macos()
            else:
                self._acquire_linux()
        except Exception as e:
            _log("screen inhibitor error:", e)
            if os.environ.get("HDREZKA_LOG"):
                import traceback

                traceback.print_exc()
            self.backend = ""
        self.active = bool(self.backend)
        _log(
            "screen inhibitor:",
            "on" if self.active else "off",
            self.backend,
        )

    def release(self):
        if not self.active:
            return
        try:
            self._release_backend()
        except Exception as e:
            _log("screen inhibitor release error:", e)
        self.active = False
        _log("screen inhibitor: off")

    # ---------- Linux ----------
    def _acquire_linux(self):
        if self._try_freedesktop():
            return
        if self._try_gnome():
            return
        if self._try_x11():
            return
        self._try_systemd_inhibit()

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

    def _try_freedesktop(self):
        out = self._gdbus(
            "org.freedesktop.ScreenSaver",
            "/org/freedesktop/ScreenSaver",
            "org.freedesktop.ScreenSaver.Inhibit",
            ["HDrezka", "Video playback"],
        )
        if out is None:
            out = self._dbus_send(
                "org.freedesktop.ScreenSaver",
                "/org/freedesktop/ScreenSaver",
                "org.freedesktop.ScreenSaver.Inhibit",
                ["string:HDrezka", "string:Video playback"],
            )
        cookie = self._cookie_from(out or "")
        if cookie is None:
            return False
        self._cookie = cookie
        self._dbus_kind = "freedesktop"
        self.backend = "dbus:screensaver"
        return True

    def _try_gnome(self):
        out = self._gdbus(
            "org.gnome.SessionManager",
            "/org/gnome/SessionManager",
            "org.gnome.SessionManager.Inhibit",
            ["HDrezka", "0", "Video playback", "8"],
        )
        if out is None:
            out = self._dbus_send(
                "org.gnome.SessionManager",
                "/org/gnome/SessionManager",
                "org.gnome.SessionManager.Inhibit",
                [
                    "string:HDrezka", "uint32:0",
                    "string:Video playback", "uint32:8",
                ],
            )
        cookie = self._cookie_from(out or "")
        if cookie is None:
            return False
        self._cookie = cookie
        self._dbus_kind = "gnome"
        self.backend = "dbus:gnome"
        return True

    def _try_x11(self):
        try:
            import ctypes.util

            x11_name = ctypes.util.find_library("X11")
            xss_name = ctypes.util.find_library("Xss")
            if not x11_name or not xss_name:
                return False
            x11 = ctypes.CDLL(x11_name)
            xss = ctypes.CDLL(xss_name)
            x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
            x11.XOpenDisplay.restype = ctypes.c_void_p
            x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
            xss.XScreenSaverSuspend.argtypes = [
                ctypes.c_void_p, ctypes.c_bool
            ]
            dpy = x11.XOpenDisplay(None)
            if not dpy:
                return False
            xss.XScreenSaverSuspend(dpy, True)
            self._x11 = x11
            self._xss = xss
            self._x11_dpy = dpy
            self.backend = "x11"
            return True
        except Exception as e:
            _log("x11 inhibitor:", e)
            return False

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
            self.backend = "systemd-inhibit"
        except Exception as e:
            _log("systemd-inhibit:", e)
            self._proc = None

    # ---------- Windows / macOS ----------
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

    # ---------- release ----------
    def _release_backend(self):
        kind = self._dbus_kind
        cookie = self._cookie
        if kind == "freedesktop":
            out = self._gdbus(
                "org.freedesktop.ScreenSaver",
                "/org/freedesktop/ScreenSaver",
                "org.freedesktop.ScreenSaver.UnInhibit",
                [str(cookie)],
            )
            if out is None:
                self._dbus_send(
                    "org.freedesktop.ScreenSaver",
                    "/org/freedesktop/ScreenSaver",
                    "org.freedesktop.ScreenSaver.UnInhibit",
                    [f"uint32:{cookie}"],
                )
        elif kind == "gnome":
            out = self._gdbus(
                "org.gnome.SessionManager",
                "/org/gnome/SessionManager",
                "org.gnome.SessionManager.Uninhibit",
                [str(cookie)],
            )
            if out is None:
                self._dbus_send(
                    "org.gnome.SessionManager",
                    "/org/gnome/SessionManager",
                    "org.gnome.SessionManager.Uninhibit",
                    [f"uint32:{cookie}"],
                )
        elif self.backend == "x11":
            if self._x11_dpy and self._x11 is not None:
                try:
                    if self._xss is not None:
                        self._xss.XScreenSaverSuspend(
                            self._x11_dpy, False
                        )
                except Exception:
                    pass
                try:
                    self._x11.XCloseDisplay(self._x11_dpy)
                except Exception:
                    pass
        elif self.backend == "systemd-inhibit":
            if self._proc is not None:
                try:
                    self._proc.terminate()
                    self._proc.wait(timeout=2)
                except Exception:
                    pass
        elif self.backend == "windows":
            try:
                ctypes.windll.kernel32.SetThreadExecutionState(
                    0x80000000
                )
            except Exception:
                pass
        elif self.backend == "caffeinate":
            if self._proc is not None:
                try:
                    self._proc.terminate()
                except Exception:
                    pass
        self._cookie = None
        self._dbus_kind = ""
        self._proc = None
        self._x11_dpy = None
        self._x11 = None
        self._xss = None
