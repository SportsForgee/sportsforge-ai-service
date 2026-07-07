"""Loads MediaPipe's PoseLandmarker (Tasks API). Note: the legacy `mp.solutions.pose`
API is NOT available in current mediapipe wheels — this must use the Tasks API, which
requires a separately-downloaded `.task` model file (not bundled in the pip package)."""
import os
import threading

import requests

MODEL_URL = "https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_lite/float16/latest/pose_landmarker_lite.task"
MODEL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models_cache")
MODEL_PATH = os.path.join(MODEL_DIR, "pose_landmarker_lite.task")

_lock = threading.Lock()


def ensure_model_downloaded() -> str:
    """Downloads the pose landmarker model on first use. Returns the absolute path."""
    if os.path.exists(MODEL_PATH) and os.path.getsize(MODEL_PATH) > 0:
        return MODEL_PATH

    with _lock:
        if os.path.exists(MODEL_PATH) and os.path.getsize(MODEL_PATH) > 0:
            return MODEL_PATH
        os.makedirs(MODEL_DIR, exist_ok=True)
        resp = requests.get(MODEL_URL, timeout=60)
        resp.raise_for_status()
        tmp_path = MODEL_PATH + ".tmp"
        with open(tmp_path, "wb") as f:
            f.write(resp.content)
        os.replace(tmp_path, MODEL_PATH)
    return MODEL_PATH


def is_model_ready() -> bool:
    return os.path.exists(MODEL_PATH) and os.path.getsize(MODEL_PATH) > 0


def create_landmarker(running_mode: str = "VIDEO"):
    """running_mode: 'VIDEO' for pre-recorded file processing (monotonic timestamps),
    'IMAGE' for single-frame calls."""
    import mediapipe as mp
    from mediapipe.tasks.python import vision, BaseOptions

    model_path = ensure_model_downloaded()
    mode = vision.RunningMode.VIDEO if running_mode == "VIDEO" else vision.RunningMode.IMAGE

    options = vision.PoseLandmarkerOptions(
        base_options=BaseOptions(model_asset_path=model_path),
        running_mode=mode,
    )
    return vision.PoseLandmarker.create_from_options(options)
