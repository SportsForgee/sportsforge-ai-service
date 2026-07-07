"""Ensures H.264 (avc1) encoding is available for the annotated-video feature.

pip's opencv-python-headless wheels don't bundle an H.264 encoder (patent licensing),
so cv2.VideoWriter falls back to MPEG-4 Part 2 ("mp4v") unless Cisco's OpenH264 shared
library is present — and mp4v-encoded .mp4 files often fail to play on iOS/Android
native video players, which only reliably support H.264/HEVC. Windows-only: on Linux
(Docker) opencv-python-headless's bundled FFmpeg typically already supports H.264
without this extra step, so this is a no-op there.
"""
import os
import platform
import threading

import requests

OPENH264_VERSION = "2.5.0"
OPENH264_FILENAME = f"openh264-{OPENH264_VERSION}-win64.dll"
OPENH264_URL = f"http://ciscobinary.openh264.org/{OPENH264_FILENAME}.bz2"
OPENH264_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models_cache")
OPENH264_PATH = os.path.join(OPENH264_DIR, OPENH264_FILENAME)

_lock = threading.Lock()


def ensure_h264_available() -> None:
    """Downloads OpenH264 on first use (Windows only) and adds it to the DLL search
    path so cv2's FFmpeg backend can find it regardless of the process's cwd."""
    if platform.system() != "Windows":
        return
    if os.path.exists(OPENH264_PATH) and os.path.getsize(OPENH264_PATH) > 0:
        _add_to_dll_path()
        return

    with _lock:
        if os.path.exists(OPENH264_PATH) and os.path.getsize(OPENH264_PATH) > 0:
            _add_to_dll_path()
            return
        try:
            import bz2
            os.makedirs(OPENH264_DIR, exist_ok=True)
            resp = requests.get(OPENH264_URL, timeout=30)
            resp.raise_for_status()
            tmp_path = OPENH264_PATH + ".tmp"
            with open(tmp_path, "wb") as f:
                f.write(bz2.decompress(resp.content))
            os.replace(tmp_path, OPENH264_PATH)
        except Exception:
            # Best-effort — generate_annotated_video() falls back to mp4v if H.264
            # still isn't available after this.
            return
    _add_to_dll_path()


def _add_to_dll_path() -> None:
    # Belt and suspenders: add_dll_directory is the modern (Python 3.8+) mechanism,
    # but ffmpeg's own LoadLibrary call inside cv2 doesn't reliably honor it — the
    # classic PATH-based search is what's actually guaranteed to work here.
    if hasattr(os, "add_dll_directory"):
        try:
            os.add_dll_directory(OPENH264_DIR)
        except OSError:
            pass
    if OPENH264_DIR not in os.environ.get("PATH", ""):
        os.environ["PATH"] = OPENH264_DIR + os.pathsep + os.environ.get("PATH", "")
