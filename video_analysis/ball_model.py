"""Loads Ultralytics YOLOv8 (nano) for ball detection. Uses the standard COCO-pretrained
weights — class id 32 is "sports ball" in the COCO label set, so no custom training is
needed for the MVP. The model file is downloaded once and cached alongside the pose
landmarker model (see pose_model.py) rather than relying on ultralytics' own cache
directory, so both models live in one predictable, git-ignored place."""
import os
import threading

import requests

MODEL_URL = "https://github.com/ultralytics/assets/releases/download/v8.2.0/yolov8n.pt"
MODEL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models_cache")
MODEL_PATH = os.path.join(MODEL_DIR, "yolov8n.pt")

# COCO class id for "sports ball" — see https://docs.ultralytics.com/datasets/detect/coco/
BALL_CLASS_ID = 32

_lock = threading.Lock()
_model = None


def ensure_model_downloaded() -> str:
    """Downloads the YOLOv8n weights on first use. Returns the absolute path."""
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


def get_model():
    """Lazily creates and caches a single YOLO model instance for reuse across requests."""
    global _model
    if _model is None:
        with _lock:
            if _model is None:
                from ultralytics import YOLO
                _model = YOLO(ensure_model_downloaded())
    return _model
