"""Make Windows consoles print UTF-8 instead of crashing on rich glyphs.

Rich renders panels with Unicode box characters. On a Windows console using a
legacy code page such as cp1252, Python raises UnicodeEncodeError before the
first banner is printed. Switching the console code page and the stream
encoding to UTF-8 keeps the output intact and prevents the crash.
"""

import os
import sys


def enable_utf8():
    """Best effort: never raise, even on exotic terminals."""
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:
            pass

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
