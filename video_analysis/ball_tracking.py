"""YOLOv8-based football (ball) detection + trajectory tracking (Phase 3), plus
discrete shot/strike speed-event detection built on top of that trajectory (Phase 4)
of the video analysis roadmap. Runs on the same sampled frames the pose pipeline
already extracted (no separate frame-extraction pass). Same honesty rules as
pipeline.py:
  - Speed is only ever reported in real km/h when the caller supplies the same
    calibration used for player speed. Without it, speed fields stay null — though
    *that* a shot happened, and when, is still reported uncalibrated since detecting
    an outlier speed spike doesn't require real-world units.
  - Assumes a single ball in frame (the common case for a training/skills clip) —
    picks the highest-confidence "sports ball" detection per frame, not multi-object
    tracking. Good enough for an MVP; a dedicated tracker (ByteTrack/DeepSORT) would be
    the natural upgrade if multi-ball or multi-camera footage becomes a need.
"""
import logging
import math
from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np

from .ball_model import BALL_CLASS_ID, get_model
from .models import BallMetrics, BallTrajectoryPoint, Calibration, ShotSpeedEvent

logger = logging.getLogger("video_analysis")

# Detections below this confidence are discarded — COCO's "sports ball" class is prone
# to false positives on other round objects; this trades a few missed frames for far
# fewer bogus trajectory points.
MIN_CONFIDENCE = 0.35

# Phase 4 shot detection: a frame-to-frame ball speed is flagged as a "shot" when it's
# a clear outlier vs. the clip's own typical ball speed — same relative-outlier idea as
# sudden_instability in pipeline.py, so a slow-dribbling clip and a hard-strike clip
# both self-calibrate instead of needing one fixed absolute px/s threshold.
SHOT_STD_MULTIPLIER = 2.5
SHOT_MIN_RATIO = 3.0
# Consecutive outlier frames within this window are the same kick sampled more than
# once, not separate shots — merge them and keep only the peak.
SHOT_MERGE_WINDOW_S = 0.5


@dataclass
class BallFrame:
    timestamp: float
    center_norm: Optional[Tuple[float, float]]  # (x, y) normalized 0-1, or None if not detected
    confidence: float = 0.0


def detect_ball(frames: List[Tuple[float, np.ndarray]], frame_width: int, frame_height: int) -> List[BallFrame]:
    """Runs YOLOv8 on each already-sampled frame, keeping the highest-confidence
    'sports ball' detection per frame (or none)."""
    try:
        model = get_model()
    except Exception:
        logger.exception("Could not load YOLO ball-detection model — continuing without ball tracking.")
        return [BallFrame(timestamp=ts, center_norm=None) for ts, _ in frames]

    ball_frames: List[BallFrame] = []
    for timestamp, frame_bgr in frames:
        try:
            results = model.predict(frame_bgr, classes=[BALL_CLASS_ID], conf=MIN_CONFIDENCE, verbose=False)
        except Exception:
            logger.exception("YOLO inference failed for frame at %.2fs — skipping.", timestamp)
            ball_frames.append(BallFrame(timestamp=timestamp, center_norm=None))
            continue

        best_box = None
        best_conf = 0.0
        for r in results:
            for box in r.boxes:
                conf = float(box.conf[0])
                if conf > best_conf:
                    best_conf = conf
                    best_box = box.xyxy[0]  # x1, y1, x2, y2 in pixel coords

        if best_box is None:
            ball_frames.append(BallFrame(timestamp=timestamp, center_norm=None))
            continue

        x1, y1, x2, y2 = [float(v) for v in best_box]
        cx = ((x1 + x2) / 2) / frame_width
        cy = ((y1 + y2) / 2) / frame_height
        ball_frames.append(BallFrame(timestamp=timestamp, center_norm=(cx, cy), confidence=best_conf))

    return ball_frames


def compute_ball_metrics(ball_frames: List[BallFrame], frame_width: int, frame_height: int,
                          reference_distance_m: Optional[float], reference_pixels: Optional[float]) -> BallMetrics:
    if not ball_frames:
        return BallMetrics(detected=False)

    detected = [bf for bf in ball_frames if bf.center_norm is not None]
    if not detected:
        return BallMetrics(detected=False, framesSampled=len(ball_frames), framesWithBallDetected=0)

    trajectory = [
        BallTrajectoryPoint(
            timestampSeconds=round(bf.timestamp, 2),
            xNorm=round(bf.center_norm[0], 4),
            yNorm=round(bf.center_norm[1], 4),
            confidence=round(bf.confidence, 2),
        )
        for bf in detected
    ]
    avg_confidence = round(sum(bf.confidence for bf in detected) / len(detected) * 100, 1)

    calibrated = bool(reference_distance_m and reference_pixels and reference_pixels > 0)
    meters_per_pixel = (reference_distance_m / reference_pixels) if calibrated else None

    speeds_ms: List[float] = []
    for prev, cur in zip(detected, detected[1:]):
        dt = cur.timestamp - prev.timestamp
        if dt <= 0:
            continue
        px_prev = (prev.center_norm[0] * frame_width, prev.center_norm[1] * frame_height)
        px_cur = (cur.center_norm[0] * frame_width, cur.center_norm[1] * frame_height)
        px_dist = math.hypot(px_cur[0] - px_prev[0], px_cur[1] - px_prev[1])
        px_speed = px_dist / dt
        if calibrated:
            speeds_ms.append(px_speed * meters_per_pixel)

    shots = detect_shots(detected, frame_width, frame_height, reference_distance_m, reference_pixels)
    top_shot_speed_kmh = None
    if shots:
        calibrated_shot_speeds = [s.speedKmh for s in shots if s.speedKmh is not None]
        if calibrated_shot_speeds:
            top_shot_speed_kmh = round(max(calibrated_shot_speeds), 2)

    if calibrated and speeds_ms:
        return BallMetrics(
            detected=True,
            framesSampled=len(ball_frames),
            framesWithBallDetected=len(detected),
            detectionConfidence=avg_confidence,
            trajectory=trajectory,
            topSpeedKmh=round(max(speeds_ms) * 3.6, 2),
            avgSpeedKmh=round((sum(speeds_ms) / len(speeds_ms)) * 3.6, 2),
            shots=shots,
            shotCount=len(shots),
            topShotSpeedKmh=top_shot_speed_kmh,
            calibration=Calibration(method="manual-distance", confidence=0.7),
        )

    return BallMetrics(
        detected=True,
        framesSampled=len(ball_frames),
        framesWithBallDetected=len(detected),
        detectionConfidence=avg_confidence,
        trajectory=trajectory,
        shots=shots,
        shotCount=len(shots),
        topShotSpeedKmh=top_shot_speed_kmh,
        calibration=Calibration(method="uncalibrated", confidence=0.0),
    )


def detect_shots(detected: List[BallFrame], frame_width: int, frame_height: int,
                  reference_distance_m: Optional[float], reference_pixels: Optional[float]) -> List[ShotSpeedEvent]:
    """Phase 4: finds discrete shot/strike events — frame-to-frame ball speed spikes
    well above the clip's own typical ball movement. Detecting *that* and *when* a shot
    happened doesn't need calibration; only converting the peak speed to km/h does."""
    if len(detected) < 3:
        return []

    calibrated = bool(reference_distance_m and reference_pixels and reference_pixels > 0)
    meters_per_pixel = (reference_distance_m / reference_pixels) if calibrated else None

    speed_points: List[Tuple[float, float]] = []  # (timestamp, px/s)
    for prev, cur in zip(detected, detected[1:]):
        dt = cur.timestamp - prev.timestamp
        if dt <= 0:
            continue
        px_prev = (prev.center_norm[0] * frame_width, prev.center_norm[1] * frame_height)
        px_cur = (cur.center_norm[0] * frame_width, cur.center_norm[1] * frame_height)
        px_dist = math.hypot(px_cur[0] - px_prev[0], px_cur[1] - px_prev[1])
        speed_points.append((cur.timestamp, px_dist / dt))

    if len(speed_points) < 2:
        return []

    speeds = [s for _, s in speed_points]
    mean_speed = sum(speeds) / len(speeds)
    variance = sum((s - mean_speed) ** 2 for s in speeds) / len(speeds)
    std_speed = math.sqrt(variance)

    threshold = max(mean_speed + SHOT_STD_MULTIPLIER * std_speed, mean_speed * SHOT_MIN_RATIO)
    if threshold <= 0:
        return []

    candidates = sorted([(ts, s) for ts, s in speed_points if s > threshold], key=lambda c: c[0])
    if not candidates:
        return []

    # Merge candidates that are close in time (same kick sampled across a couple of
    # consecutive frames) into one event, keeping only each cluster's peak.
    clusters: List[List[Tuple[float, float]]] = []
    for ts, s in candidates:
        if clusters and ts - clusters[-1][-1][0] <= SHOT_MERGE_WINDOW_S:
            clusters[-1].append((ts, s))
        else:
            clusters.append([(ts, s)])

    shots: List[ShotSpeedEvent] = []
    for cluster in clusters:
        peak_ts, peak_speed = max(cluster, key=lambda c: c[1])
        speed_kmh = round(peak_speed * meters_per_pixel * 3.6, 2) if calibrated else None
        shots.append(ShotSpeedEvent(timestampSeconds=round(peak_ts, 2), speedKmh=speed_kmh))

    return shots
