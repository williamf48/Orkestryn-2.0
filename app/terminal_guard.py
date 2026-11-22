"""Recognize an interactive approval terminal, including Windows NUL handling."""
from __future__ import annotations

import sys
import unittest
from unittest import mock

WINDOWS = sys.platform == "win32"


def windows_console_input(stream):
    # Windows isatty() also accepts character devices such as NUL. Use the
    # handle of this Python input stream; never open CONIN$ to bypass redirection.
    import ctypes
    import msvcrt
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetConsoleMode.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel.GetConsoleMode.restype = wintypes.BOOL
    mode = wintypes.DWORD()
    return bool(kernel.GetConsoleMode(msvcrt.get_osfhandle(stream.fileno()), ctypes.byref(mode)))


def interactive_input(stream=None):
    stream = sys.stdin if stream is None else stream
    try:
        if stream is None or not stream.isatty():
            return False
        return windows_console_input(stream) if WINDOWS else True
    except (OSError, ValueError, AttributeError, ImportError):
        return False
