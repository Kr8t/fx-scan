"""Alert delivery: Windows toast and optional ntfy.sh phone push. Never raises."""

from __future__ import annotations

import importlib
import sys
import urllib.parse
import urllib.request
from collections.abc import Callable

Post = Callable[[str, bytes, dict[str, str]], None]
NTFY_URL = "https://ntfy.sh"


def _bell() -> None:
    sys.stdout.write("\a")
    sys.stdout.flush()


def beep() -> None:
    """Three rising tones, hard to miss from another room. Never raises.

    Windows: ``winsound.Beep`` tones; if the PC has no beep device, the system
    notification sound; anywhere else (or on failure) the terminal bell.
    """
    try:
        winsound = importlib.import_module("winsound")
    except ImportError:
        _bell()
        return
    try:
        for frequency in (900, 1200, 1500):
            winsound.Beep(frequency, 220)
    except Exception:  # no beep device: fall back to the system sound
        try:
            winsound.MessageBeep()
        except Exception:  # still nothing: terminal bell
            _bell()


def send_desktop(title: str, message: str) -> bool:
    """Show a Windows toast via winotify. False if unavailable (other OS, not installed)."""
    try:
        from winotify import Notification  # type: ignore[import-not-found,unused-ignore]

        Notification(app_id="FX SCAN", title=title, msg=message, duration="long").show()
    except Exception:  # missing package, non-Windows, or shell error: caller falls back
        return False
    return True


def _post(url: str, body: bytes, headers: dict[str, str]) -> None:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=10):
        pass


def send_ntfy(topic: str, title: str, message: str, post: Post | None = None) -> bool:
    """Push to https://ntfy.sh/<topic>. False on any network error."""
    url = f"{NTFY_URL}/{urllib.parse.quote(topic, safe='')}"
    try:
        (post or _post)(url, message.encode("utf-8"), {"Title": title, "Priority": "high"})
    except Exception:  # offline, DNS, HTTP error: the console alert still fired
        return False
    return True
