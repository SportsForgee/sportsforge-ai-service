import logging
import os
import traceback

import requests
from fastapi import APIRouter, BackgroundTasks

from .models import AnalyzeRequest, AnalysisResult
from .pipeline import analyze_video
from .pose_model import is_model_ready
from .ball_model import is_model_ready as is_ball_model_ready

logger = logging.getLogger("video_analysis")
router = APIRouter()

# Must match backend/Api's Video:StorageRoot / Video:PublicBaseUrl — same shared
# volume in Docker, same convention for local dev. Env vars so nothing is hardcoded.
STORAGE_ROOT = os.environ.get("VIDEO_STORAGE_ROOT", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "storage", "videos"))
PUBLIC_BASE_URL = os.environ.get("VIDEO_PUBLIC_BASE_URL", "/storage/videos")


def thumbnail_path(athlete_id: str, video_id: str, filename: str):
    directory = os.path.join(STORAGE_ROOT, athlete_id, video_id, "keyframes")
    os.makedirs(directory, exist_ok=True)
    abs_path = os.path.join(directory, filename)
    public_url = f"{PUBLIC_BASE_URL}/{athlete_id}/{video_id}/keyframes/{filename}"
    return abs_path, public_url


def annotated_video_path(athlete_id: str, video_id: str):
    directory = os.path.join(STORAGE_ROOT, athlete_id, video_id)
    os.makedirs(directory, exist_ok=True)
    filename = "annotated.mp4"
    abs_path = os.path.join(directory, filename)
    public_url = f"{PUBLIC_BASE_URL}/{athlete_id}/{video_id}/{filename}"
    return abs_path, public_url


@router.get("/video/health")
def video_health():
    status = {"opencvAvailable": False, "mediapipeAvailable": False, "modelReady": False,
              "ultralyticsAvailable": False, "ballModelReady": False}
    try:
        import cv2  # noqa: F401
        status["opencvAvailable"] = True
    except Exception:
        pass
    try:
        import mediapipe  # noqa: F401
        status["mediapipeAvailable"] = True
    except Exception:
        pass
    try:
        import ultralytics  # noqa: F401
        status["ultralyticsAvailable"] = True
    except Exception:
        pass
    status["modelReady"] = is_model_ready()
    status["ballModelReady"] = is_ball_model_ready()
    return status


@router.post("/video/analyze", status_code=202)
def request_analysis(req: AnalyzeRequest, background_tasks: BackgroundTasks):
    background_tasks.add_task(_run_and_callback, req)
    return {"status": "Accepted", "videoId": req.videoId}


def _run_and_callback(req: AnalyzeRequest):
    try:
        result = analyze_video(
            video_id=req.videoId, athlete_id=req.athleteId, video_path=req.videoPath,
            storage_thumbnail_fn=thumbnail_path,
            reference_distance_m=req.referenceDistanceMeters, reference_pixels=req.referencePixels,
            storage_annotated_video_fn=annotated_video_path,
        )
    except Exception as exc:
        logger.exception("Video analysis pipeline crashed for videoId=%s", req.videoId)
        result = AnalysisResult(
            videoId=req.videoId, athleteId=req.athleteId, status="Failed",
            failureReason=f"{type(exc).__name__}: {exc}",
        )

    _post_callback(req.callbackUrl, result)


def _post_callback(callback_url: str, result: AnalysisResult):
    try:
        resp = requests.post(callback_url, json=result.model_dump(), timeout=15)
        if not resp.ok:
            logger.error("Callback to %s failed: %s %s", callback_url, resp.status_code, resp.text)
    except Exception:
        logger.error("Could not reach callback URL %s\n%s", callback_url, traceback.format_exc())
