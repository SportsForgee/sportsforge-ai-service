"""OpenCV + MediaPipe pose/speed analysis pipeline.

MVP scope and honest limitations (documented rather than hidden):
  - All joint-angle and displacement math works in the 2D image plane. There is no
    camera calibration / 3D reconstruction — this is a reasonable MVP approximation,
    not biomechanically precise 3D analysis.
  - Speed is only ever reported in real km/h when the caller supplies calibration
    (a known real-world distance + the pixel distance it corresponds to). Without it,
    speed fields are left null rather than guessed — never fabricate absolute speed.
  - Anomaly rules are simple, explicit thresholds (documented per-rule below), meant
    to be swapped for an ML model in Phase 4's second half.
"""
import logging
import math
import os
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import cv2
import numpy as np

from .models import (
    AnalysisResult, Anomaly, Calibration, DrillRecommendation, GaitBalance,
    JointAngleWindow, Keyframe, PoseMetrics, SpeedMetrics,
)
from .codec_setup import ensure_h264_available
from .pose_model import create_landmarker
from .ball_tracking import detect_ball, compute_ball_metrics

logger = logging.getLogger("video_analysis")

TARGET_FPS = 12
WINDOW_SECONDS = 2.0
SMOOTHING_FRAMES = 3

# Anomaly-rule thresholds (see detect_anomalies() for the rules that use them).
TRUNK_LEAN_THRESHOLD_DEG = 30.0
FATIGUE_MIN_VALID_FRAMES = 6
FATIGUE_STARTING_STABILITY_MIN = 50.0
FATIGUE_STABILITY_DROP_THRESHOLD = 20.0

# BlazePose (33-point) landmark indices used here.
L_SHOULDER, R_SHOULDER = 11, 12
L_HIP, R_HIP = 23, 24
L_KNEE, R_KNEE = 25, 26
L_ANKLE, R_ANKLE = 27, 28
L_FOOT_INDEX, R_FOOT_INDEX = 31, 32


@dataclass
class PoseFrame:
    timestamp: float
    landmarks: Optional[List[Tuple[float, float, float]]]  # (x, y, visibility), normalized 0-1


@dataclass
class VideoInfo:
    fps: float
    frame_width: int
    frame_height: int
    duration_seconds: float


class NoPersonDetectedError(Exception):
    pass


def extract_frames(video_path: str, target_fps: int = TARGET_FPS):
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise FileNotFoundError(f"Could not open video: {video_path}")

    native_fps = cap.get(cv2.CAP_PROP_FPS) or target_fps
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    duration = frame_count / native_fps if native_fps > 0 else 0.0

    sample_every = max(1, round(native_fps / target_fps))

    frames = []
    idx = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if idx % sample_every == 0:
            timestamp = idx / native_fps if native_fps > 0 else idx / target_fps
            frames.append((timestamp, frame))
        idx += 1
    cap.release()

    info = VideoInfo(fps=native_fps / sample_every, frame_width=width, frame_height=height, duration_seconds=duration)
    return frames, info


def run_pose_estimation(frames: List[Tuple[float, np.ndarray]]) -> List[PoseFrame]:
    import mediapipe as mp

    pose_frames: List[PoseFrame] = []
    with create_landmarker(running_mode="VIDEO") as landmarker:
        for timestamp, frame_bgr in frames:
            mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB))
            result = landmarker.detect_for_video(mp_image, int(timestamp * 1000))
            if result.pose_landmarks and len(result.pose_landmarks) > 0:
                lm = result.pose_landmarks[0]  # largest/primary detected person
                coords = [(p.x, p.y, p.visibility) for p in lm]
                pose_frames.append(PoseFrame(timestamp=timestamp, landmarks=coords))
            else:
                pose_frames.append(PoseFrame(timestamp=timestamp, landmarks=None))

    return _smooth_landmarks(pose_frames)


def _smooth_landmarks(pose_frames: List[PoseFrame]) -> List[PoseFrame]:
    """Simple trailing moving average over SMOOTHING_FRAMES valid frames, to reduce
    per-frame landmark jitter before computing angles/displacement."""
    smoothed: List[PoseFrame] = []
    history: List[List[Tuple[float, float, float]]] = []

    for pf in pose_frames:
        if pf.landmarks is None:
            smoothed.append(pf)
            continue
        history.append(pf.landmarks)
        if len(history) > SMOOTHING_FRAMES:
            history.pop(0)

        n = len(history)
        avg = [
            (
                sum(h[i][0] for h in history) / n,
                sum(h[i][1] for h in history) / n,
                sum(h[i][2] for h in history) / n,
            )
            for i in range(len(pf.landmarks))
        ]
        smoothed.append(PoseFrame(timestamp=pf.timestamp, landmarks=avg))
    return smoothed


def _angle_deg(a: Tuple[float, float], b: Tuple[float, float], c: Tuple[float, float]) -> float:
    """Angle at vertex b formed by rays b->a and b->c, in degrees."""
    ba = (a[0] - b[0], a[1] - b[1])
    bc = (c[0] - b[0], c[1] - b[1])
    dot = ba[0] * bc[0] + ba[1] * bc[1]
    mag = math.hypot(*ba) * math.hypot(*bc)
    if mag == 0:
        return 0.0
    cos_angle = max(-1.0, min(1.0, dot / mag))
    return math.degrees(math.acos(cos_angle))


def _joint_angles(lm: List[Tuple[float, float, float]]):
    """Returns (avg_knee, avg_hip, avg_ankle, left_knee, right_knee) in degrees for one frame."""
    def pt(i):
        return (lm[i][0], lm[i][1])

    left_knee  = _angle_deg(pt(L_HIP), pt(L_KNEE), pt(L_ANKLE))
    right_knee = _angle_deg(pt(R_HIP), pt(R_KNEE), pt(R_ANKLE))
    left_hip   = _angle_deg(pt(L_SHOULDER), pt(L_HIP), pt(L_KNEE))
    right_hip  = _angle_deg(pt(R_SHOULDER), pt(R_HIP), pt(R_KNEE))
    left_ankle = _angle_deg(pt(L_KNEE), pt(L_ANKLE), pt(L_FOOT_INDEX))
    right_ankle= _angle_deg(pt(R_KNEE), pt(R_ANKLE), pt(R_FOOT_INDEX))

    avg_knee  = (left_knee + right_knee) / 2
    avg_hip   = (left_hip + right_hip) / 2
    avg_ankle = (left_ankle + right_ankle) / 2
    return avg_knee, avg_hip, avg_ankle, left_knee, right_knee


def _trunk_lean_deg(lm: List[Tuple[float, float, float]]) -> float:
    """Angle of the shoulder-hip line from vertical, in degrees — a "virtual point
    directly above the hip" trick that reuses _angle_deg instead of new geometry:
    the angle at the hip midpoint between the ray to the shoulder midpoint and a
    ray straight up is exactly the trunk's lean from vertical."""
    shoulder_mid = ((lm[L_SHOULDER][0] + lm[R_SHOULDER][0]) / 2, (lm[L_SHOULDER][1] + lm[R_SHOULDER][1]) / 2)
    hip_mid = ((lm[L_HIP][0] + lm[R_HIP][0]) / 2, (lm[L_HIP][1] + lm[R_HIP][1]) / 2)
    above_hip = (hip_mid[0], hip_mid[1] - 1.0)
    return _angle_deg(shoulder_mid, hip_mid, above_hip)


def _posture_stability_score(frames: List[PoseFrame]) -> float:
    """Frame-to-frame hip-center jitter, converted to a 0-100 stability score
    (jitter is in normalized-image-plane units; scaled empirically). Factored
    out of compute_pose_metrics() so detect_anomalies() can compare stability
    across the first vs second half of a clip (fatigue_drift) without
    duplicating the math."""
    prev_hip_center = None
    jitter_samples: List[float] = []
    for pf in frames:
        if pf.landmarks is None:
            continue
        hip_center = ((pf.landmarks[L_HIP][0] + pf.landmarks[R_HIP][0]) / 2,
                      (pf.landmarks[L_HIP][1] + pf.landmarks[R_HIP][1]) / 2)
        if prev_hip_center is not None:
            jitter_samples.append(math.hypot(hip_center[0] - prev_hip_center[0], hip_center[1] - prev_hip_center[1]))
        prev_hip_center = hip_center

    if not jitter_samples:
        return 100.0
    avg_jitter = sum(jitter_samples) / len(jitter_samples)
    return max(0.0, min(100.0, 100.0 - avg_jitter * 800.0))


def compute_pose_metrics(pose_frames: List[PoseFrame]) -> PoseMetrics:
    valid = [pf for pf in pose_frames if pf.landmarks is not None]
    if not valid:
        raise NoPersonDetectedError("No person detected in any sampled frame.")

    # Confidence is the fraction of sampled frames with a detected person, scaled by
    # how confident the model was in the landmarks it did find (avg visibility of the
    # joints this pipeline actually uses) — a clip where the person is only visible
    # half the time, or poorly, should read as less trustworthy than a clean one.
    detection_rate = len(valid) / len(pose_frames)
    used_landmarks = (L_SHOULDER, R_SHOULDER, L_HIP, R_HIP, L_KNEE, R_KNEE, L_ANKLE, R_ANKLE)
    avg_visibility = sum(pf.landmarks[i][2] for pf in valid for i in used_landmarks) / (len(valid) * len(used_landmarks))
    detection_confidence = round(max(0.0, min(100.0, detection_rate * avg_visibility * 100.0)), 1)

    windows: List[JointAngleWindow] = []
    knee_deltas: List[float] = []

    start = valid[0].timestamp
    end_of_clip = valid[-1].timestamp
    window_start = start

    while window_start <= end_of_clip:
        window_end = window_start + WINDOW_SECONDS
        in_window = [pf for pf in valid if window_start <= pf.timestamp < window_end]

        if in_window:
            knees, hips, ankles = [], [], []
            for pf in in_window:
                avg_knee, avg_hip, avg_ankle, lk, rk = _joint_angles(pf.landmarks)
                knees.append(avg_knee)
                hips.append(avg_hip)
                ankles.append(avg_ankle)
                knee_deltas.append(abs(lk - rk))

            windows.append(JointAngleWindow(
                startSeconds=round(window_start, 2), endSeconds=round(window_end, 2),
                avgKneeAngleDeg=round(sum(knees) / len(knees), 1),
                avgHipAngleDeg=round(sum(hips) / len(hips), 1),
                avgAnkleAngleDeg=round(sum(ankles) / len(ankles), 1),
            ))
        window_start = window_end

    avg_knee_delta = sum(knee_deltas) / len(knee_deltas) if knee_deltas else 0.0
    # 1 degree of L/R delta costs 2 symmetry points; fully symmetric (0 delta) = 100.
    symmetry_score = max(0.0, min(100.0, 100.0 - avg_knee_delta * 2.0))

    posture_stability = _posture_stability_score(valid)

    return PoseMetrics(
        jointAngleWindows=windows,
        symmetryScore=round(symmetry_score, 1),
        postureStabilityScore=round(posture_stability, 1),
        detectionConfidence=detection_confidence,
        framesSampled=len(pose_frames),
        framesWithPersonDetected=len(valid),
    )


def compute_speed_metrics(pose_frames: List[PoseFrame], frame_width: int, frame_height: int,
                           reference_distance_m: Optional[float], reference_pixels: Optional[float]) -> SpeedMetrics:
    valid = [pf for pf in pose_frames if pf.landmarks is not None]
    if len(valid) < 2:
        return SpeedMetrics(calibration=Calibration(method="uncalibrated", confidence=0.0))

    calibrated = bool(reference_distance_m and reference_pixels and reference_pixels > 0)
    meters_per_pixel = (reference_distance_m / reference_pixels) if calibrated else None

    speeds_ms: List[float] = []
    for prev, cur in zip(valid, valid[1:]):
        dt = cur.timestamp - prev.timestamp
        if dt <= 0:
            continue
        px_prev = ((prev.landmarks[L_HIP][0] + prev.landmarks[R_HIP][0]) / 2 * frame_width,
                   (prev.landmarks[L_HIP][1] + prev.landmarks[R_HIP][1]) / 2 * frame_height)
        px_cur = ((cur.landmarks[L_HIP][0] + cur.landmarks[R_HIP][0]) / 2 * frame_width,
                  (cur.landmarks[L_HIP][1] + cur.landmarks[R_HIP][1]) / 2 * frame_height)
        px_dist = math.hypot(px_cur[0] - px_prev[0], px_cur[1] - px_prev[1])
        px_speed = px_dist / dt  # px/s

        if calibrated:
            speeds_ms.append(px_speed * meters_per_pixel)
        else:
            speeds_ms.append(px_speed)  # relative units only — never surfaced as km/h below

    if not speeds_ms:
        return SpeedMetrics(calibration=Calibration(method="uncalibrated", confidence=0.0))

    accel_peaks = []
    for a, b in zip(speeds_ms, speeds_ms[1:]):
        accel_peaks.append(b - a)

    # Stride frequency: count sign changes in ankle-vertical oscillation (a full gait
    # cycle is one down-up-down of either ankle relative to the other).
    ankle_diff = [pf.landmarks[L_ANKLE][1] - pf.landmarks[R_ANKLE][1] for pf in valid]
    sign_changes = sum(1 for a, b in zip(ankle_diff, ankle_diff[1:]) if (a > 0) != (b > 0) and a != 0 and b != 0)
    duration = valid[-1].timestamp - valid[0].timestamp
    stride_frequency = (sign_changes / 2) / duration if duration > 0 else None

    if calibrated:
        return SpeedMetrics(
            topSpeedKmh=round(max(speeds_ms) * 3.6, 2),
            avgSpeedKmh=round((sum(speeds_ms) / len(speeds_ms)) * 3.6, 2),
            accelerationPeaksMs2=[round(a, 2) for a in sorted(accel_peaks, key=abs, reverse=True)[:5]],
            strideFrequencyHz=round(stride_frequency, 2) if stride_frequency else None,
            calibration=Calibration(method="manual-distance", confidence=0.7),
        )

    return SpeedMetrics(
        topSpeedKmh=None,
        avgSpeedKmh=None,
        accelerationPeaksMs2=[],
        strideFrequencyHz=round(stride_frequency, 2) if stride_frequency else None,
        calibration=Calibration(method="uncalibrated", confidence=0.0),
    )


def compute_gait_balance(pose_metrics: PoseMetrics) -> GaitBalance:
    symmetry = pose_metrics.symmetryScore or 0.0
    stability = pose_metrics.postureStabilityScore or 0.0
    score = symmetry * 0.6 + stability * 0.4
    return GaitBalance(score=round(max(0.0, min(100.0, score)), 1))


def detect_anomalies(pose_metrics: PoseMetrics, speed_metrics: SpeedMetrics, pose_frames: List[PoseFrame]) -> List[Anomaly]:
    anomalies: List[Anomaly] = []

    if pose_metrics.symmetryScore is not None and pose_metrics.symmetryScore < 70:
        worst_window = max(
            pose_metrics.jointAngleWindows,
            key=lambda w: abs((w.avgKneeAngleDeg or 0)),
            default=None,
        )
        ts = worst_window.startSeconds if worst_window else 0.0
        anomalies.append(Anomaly(
            type="asymmetric_stride", severity="Warning", timestampSeconds=ts,
            description=f"Left/right symmetry score is {pose_metrics.symmetryScore:.0f}/100 — below the 70 threshold.",
        ))

    # Simplified knee-valgus proxy: a markedly flexed knee angle in the 2D image plane.
    # This is an MVP heuristic, not true 3D valgus (frontal-plane knee collapse) detection.
    for w in pose_metrics.jointAngleWindows:
        if w.avgKneeAngleDeg is not None and w.avgKneeAngleDeg < 150:
            anomalies.append(Anomaly(
                type="knee_valgus", severity="Warning", timestampSeconds=w.startSeconds,
                description=f"Knee angle averaged {w.avgKneeAngleDeg:.0f}° in this window — deep flexion pattern worth a coach review.",
            ))
            break  # one flag is enough for the MVP; don't spam per-window

    if pose_metrics.postureStabilityScore is not None and pose_metrics.postureStabilityScore < 60:
        anomalies.append(Anomaly(
            type="deceleration_form_breakdown", severity="Info", timestampSeconds=0.0,
            description=f"Posture stability score is {pose_metrics.postureStabilityScore:.0f}/100 — noticeable movement jitter across the clip.",
        ))

    # Posture stability above is a whole-clip AVERAGE, which a brief severe event (a
    # fall, a stumble) barely moves if the rest of the clip was calm — so it must not
    # be the only signal. Look for a single frame-to-frame jump far bigger than the
    # clip's own typical movement, independent of the averaged score.
    valid = [pf for pf in pose_frames if pf.landmarks is not None]
    jitter_events: List[Tuple[float, float]] = []
    prev_hip_center = None
    for pf in valid:
        hip_center = ((pf.landmarks[L_HIP][0] + pf.landmarks[R_HIP][0]) / 2,
                      (pf.landmarks[L_HIP][1] + pf.landmarks[R_HIP][1]) / 2)
        if prev_hip_center is not None:
            jitter_events.append((pf.timestamp, math.hypot(hip_center[0] - prev_hip_center[0], hip_center[1] - prev_hip_center[1])))
        prev_hip_center = hip_center

    if jitter_events:
        peak_ts, peak_val = max(jitter_events, key=lambda e: e[1])
        avg_val = sum(v for _, v in jitter_events) / len(jitter_events)
        if peak_val > 0.12 and (avg_val == 0 or peak_val > avg_val * 4):
            anomalies.append(Anomaly(
                type="sudden_instability", severity="Critical", timestampSeconds=round(peak_ts, 2),
                description=f"Sudden large movement at {peak_ts:.1f}s, much larger than the rest of the clip — could be a fall or loss of balance. Review the footage.",
            ))

    # Losing the person mid-clip (not just at the very start/end, which is normal —
    # the athlete walking into/out of frame) often means they went to the ground or
    # were occluded — another signal the averaged scores above can't see at all.
    run_start = None
    for i, pf in enumerate(pose_frames):
        if pf.landmarks is None and run_start is None:
            run_start = i
        elif pf.landmarks is not None and run_start is not None:
            run_len = i - run_start
            if run_len >= 3 and run_start > 0:
                anomalies.append(Anomaly(
                    type="tracking_lost", severity="Warning", timestampSeconds=round(pose_frames[run_start].timestamp, 2),
                    description=f"Lost track of the athlete for {run_len} sampled frames from {pose_frames[run_start].timestamp:.1f}s — possibly off-frame, occluded, or on the ground.",
                ))
            run_start = None

    # Trunk lean: sustained forward/backward lean is a common technique breakdown
    # (and, like knee_valgus, an MVP 2D-plane heuristic — not true 3D posture analysis).
    # Reuses the window boundaries pose_metrics already computed rather than
    # re-deriving them.
    for w in pose_metrics.jointAngleWindows:
        in_window = [pf for pf in valid if w.startSeconds <= pf.timestamp < w.endSeconds]
        if not in_window:
            continue
        avg_lean = sum(_trunk_lean_deg(pf.landmarks) for pf in in_window) / len(in_window)
        if avg_lean > TRUNK_LEAN_THRESHOLD_DEG:
            anomalies.append(Anomaly(
                type="trunk_lean", severity="Warning", timestampSeconds=w.startSeconds,
                description=f"Trunk leaned ~{avg_lean:.0f}° from vertical in this window — sustained lean can signal fatigue or poor running posture; review form.",
            ))
            break  # one flag is enough for the MVP; don't spam per-window

    # Fatigue drift: posture_stability above is a whole-clip average, which can hide a
    # clip that started clean and fell apart later — a real, common in-clip pattern as
    # fatigue sets in, distinct from sudden_instability's single sharp event above.
    if len(valid) >= FATIGUE_MIN_VALID_FRAMES:
        mid = len(valid) // 2
        first_stability = _posture_stability_score(valid[:mid])
        second_stability = _posture_stability_score(valid[mid:])
        if first_stability >= FATIGUE_STARTING_STABILITY_MIN and \
                (first_stability - second_stability) > FATIGUE_STABILITY_DROP_THRESHOLD:
            anomalies.append(Anomaly(
                type="fatigue_drift", severity="Warning", timestampSeconds=round(valid[mid].timestamp, 2),
                description=(
                    f"Movement stability dropped from {first_stability:.0f}/100 in the first half of the "
                    f"clip to {second_stability:.0f}/100 in the second half — a common sign of fatigue "
                    "affecting form late in a set. Consider shorter reps or more rest."
                ),
            ))

    return anomalies


# Static anomaly-type -> suggested focus area lookup. Deliberately not the coach's
# per-org Drill library (a different domain: coach-assigned training sessions) — this
# is a lightweight, curated suggestion computed straight off what the clip showed.
# tracking_lost is intentionally absent: it's a filming/data-quality flag (camera lost
# the athlete), not a movement issue — no drill fixes that. category values reuse the
# same vocabulary as Drill.Category so this can plug into the real library later.
_DRILL_SUGGESTIONS = {
    "asymmetric_stride": DrillRecommendation(
        anomalyType="asymmetric_stride", category="skill",
        title="Single-Leg Stability Drills",
        description="Single-leg RDLs and lateral step-downs to build left/right symmetry and correct stride imbalance.",
    ),
    "knee_valgus": DrillRecommendation(
        anomalyType="knee_valgus", category="strength",
        title="Knee-Tracking Strength Work",
        description="Banded lateral walks and controlled box step-downs to reinforce proper knee alignment under load.",
    ),
    "deceleration_form_breakdown": DrillRecommendation(
        anomalyType="deceleration_form_breakdown", category="strength",
        title="Posture & Core Stability",
        description="Dead bug and plank progressions to build the core control needed to hold form as movement gets less controlled.",
    ),
    "sudden_instability": DrillRecommendation(
        anomalyType="sudden_instability", category="skill",
        title="Balance & Fall-Recovery Drills",
        description="Single-leg balance holds and reactive-balance drills to reduce the chance of a stumble like this recurring.",
    ),
    "trunk_lean": DrillRecommendation(
        anomalyType="trunk_lean", category="strength",
        title="Posture & Trunk Control",
        description="Core anti-flexion work (planks, pallof presses) to keep your trunk upright and stable during movement.",
    ),
    "fatigue_drift": DrillRecommendation(
        anomalyType="fatigue_drift", category="endurance",
        title="Conditioning & Work Capacity",
        description="Interval conditioning (e.g. repeated shuttle sets with short rest) to extend how long you can hold clean form.",
    ),
}


def recommend_drills(anomalies: List[Anomaly]) -> List[DrillRecommendation]:
    """One suggestion per distinct flagged anomaly type, ordered by severity —
    same dedupe/ordering convention as generate_keyframes()."""
    severity_rank = {"Critical": 0, "Warning": 1, "Info": 2}
    ranked = sorted(anomalies, key=lambda a: severity_rank.get(a.severity, 3))

    seen: set = set()
    recommendations: List[DrillRecommendation] = []
    for a in ranked:
        if a.type in seen or a.type not in _DRILL_SUGGESTIONS:
            continue
        seen.add(a.type)
        recommendations.append(_DRILL_SUGGESTIONS[a.type])
    return recommendations


# Lower-body skeleton edges — mirrors what this pipeline actually measures (knee/hip/
# ankle angles, stride symmetry), rather than drawing all 33 BlazePose points.
_SKELETON_EDGES = [
    (L_SHOULDER, R_SHOULDER), (L_SHOULDER, L_HIP), (R_SHOULDER, R_HIP), (L_HIP, R_HIP),
    (L_HIP, L_KNEE), (L_KNEE, L_ANKLE), (L_ANKLE, L_FOOT_INDEX),
    (R_HIP, R_KNEE), (R_KNEE, R_ANKLE), (R_ANKLE, R_FOOT_INDEX),
]


def _draw_skeleton(frame: np.ndarray, landmarks: List[Tuple[float, float, float]]) -> np.ndarray:
    """Overlays the joints this pipeline actually used, so a flagged anomaly can be
    visually checked against what the model saw — not just trusted as a number."""
    annotated = frame.copy()
    h, w = annotated.shape[:2]

    def px(i):
        return int(landmarks[i][0] * w), int(landmarks[i][1] * h)

    for a, b in _SKELETON_EDGES:
        cv2.line(annotated, px(a), px(b), (46, 174, 125), 3, lineType=cv2.LINE_AA)  # BGR, brand green

    for i in {L_SHOULDER, R_SHOULDER, L_HIP, R_HIP, L_KNEE, R_KNEE, L_ANKLE, R_ANKLE, L_FOOT_INDEX, R_FOOT_INDEX}:
        visibility = landmarks[i][2]
        color = (46, 125, 46) if visibility > 0.6 else (0, 165, 255)  # amber where the model was unsure
        cv2.circle(annotated, px(i), 6, color, -1, lineType=cv2.LINE_AA)

    return annotated


def generate_keyframes(frames: List[Tuple[float, np.ndarray]], pose_frames: List[PoseFrame],
                        anomalies: List[Anomaly], athlete_id: str, video_id: str, storage) -> List[Keyframe]:
    keyframes: List[Keyframe] = []
    if not frames:
        return keyframes

    severity_rank = {"Critical": 0, "Warning": 1, "Info": 2}
    ranked = sorted(anomalies, key=lambda a: severity_rank.get(a.severity, 3))
    targets: List[Tuple[float, str]] = [(a.timestampSeconds, a.type) for a in ranked[:3]]
    if not targets:
        mid = frames[len(frames) // 2][0]
        targets = [(frames[0][0], "clip_start"), (mid, "clip_midpoint"), (frames[-1][0], "clip_end")]

    for i, (target_ts, label) in enumerate(targets):
        nearest = min(frames, key=lambda f: abs(f[0] - target_ts))
        nearest_pose = min(pose_frames, key=lambda pf: abs(pf.timestamp - target_ts), default=None)

        image = nearest[1]
        if nearest_pose is not None and nearest_pose.landmarks is not None:
            image = _draw_skeleton(image, nearest_pose.landmarks)

        filename = f"keyframe_{i}_{label}.jpg"
        abs_path, public_url = storage.GetThumbnailPath(athlete_id, video_id, filename) if hasattr(storage, "GetThumbnailPath") \
            else storage(athlete_id, video_id, filename)
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        cv2.imwrite(abs_path, image)
        keyframes.append(Keyframe(timestampSeconds=round(nearest[0], 2), label=label, thumbnailUrl=public_url))

    return keyframes


def generate_annotated_video(frames: List[Tuple[float, np.ndarray]], pose_frames: List[PoseFrame],
                              athlete_id: str, video_id: str, storage, fps: float,
                              frame_width: int, frame_height: int) -> Optional[str]:
    """Writes every sampled frame back out with the tracked skeleton drawn on it — full
    playback proof of what the model saw throughout the clip, not just 2-3 stills.
    Runs at the pipeline's sampled rate (TARGET_FPS), not the source video's native
    frame rate, so it plays choppier than the original — that's expected, not a bug."""
    if not frames:
        return None

    ensure_h264_available()
    abs_path, public_url = storage.GetAnnotatedVideoPath(athlete_id, video_id) if hasattr(storage, "GetAnnotatedVideoPath") \
        else storage(athlete_id, video_id)
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)

    # avc1 (H.264) is what iOS/Android native players actually support in an .mp4
    # container — mp4v (MPEG-4 Part 2) often silently fails to play on iOS. Fall back
    # to mp4v only if H.264 isn't available in this environment (missing openh264 lib).
    writer = None
    for fourcc_str in ("avc1", "mp4v"):
        candidate = cv2.VideoWriter(abs_path, cv2.VideoWriter_fourcc(*fourcc_str), max(fps, 1.0), (frame_width, frame_height))
        if candidate.isOpened():
            writer = candidate
            break
        candidate.release()

    if writer is None:
        logger.error("Could not open VideoWriter (tried avc1, mp4v) for annotated video at %s", abs_path)
        return None

    pose_by_ts = {pf.timestamp: pf for pf in pose_frames}
    try:
        for timestamp, frame_bgr in frames:
            pf = pose_by_ts.get(timestamp)
            image = _draw_skeleton(frame_bgr, pf.landmarks) if pf is not None and pf.landmarks is not None else frame_bgr
            writer.write(image)
    finally:
        writer.release()

    return public_url


def analyze_video(video_id: str, athlete_id: str, video_path: str, storage_thumbnail_fn,
                   reference_distance_m: Optional[float] = None, reference_pixels: Optional[float] = None,
                   storage_annotated_video_fn=None) -> AnalysisResult:
    """Runs the full pipeline synchronously. storage_thumbnail_fn(athleteId, videoId, filename)
    -> (absolutePath, publicUrl), matching IVideoStorageService.GetThumbnailPath's contract.
    storage_annotated_video_fn(athleteId, videoId) -> (absolutePath, publicUrl); optional —
    when omitted, no full annotated video is generated (only keyframe stills)."""
    frames, info = extract_frames(video_path)
    if not frames:
        return AnalysisResult(videoId=video_id, athleteId=athlete_id, status="Failed",
                               failureReason="Could not read any frames from the video.")

    pose_frames = run_pose_estimation(frames)

    try:
        pose_metrics = compute_pose_metrics(pose_frames)
    except NoPersonDetectedError as e:
        return AnalysisResult(videoId=video_id, athleteId=athlete_id, status="Failed", failureReason=str(e))

    speed_metrics = compute_speed_metrics(pose_frames, info.frame_width, info.frame_height,
                                          reference_distance_m, reference_pixels)
    gait_balance = compute_gait_balance(pose_metrics)
    anomalies = detect_anomalies(pose_metrics, speed_metrics, pose_frames)
    keyframes = generate_keyframes(frames, pose_frames, anomalies, athlete_id, video_id, storage_thumbnail_fn)
    drill_recommendations = recommend_drills(anomalies)

    # Ball detection/tracking (Phase 3) is additive — never let it take down pose/speed
    # analysis, which is the core deliverable this pipeline already provides.
    try:
        ball_frames = detect_ball(frames, info.frame_width, info.frame_height)
        ball_metrics = compute_ball_metrics(ball_frames, info.frame_width, info.frame_height,
                                             reference_distance_m, reference_pixels)
    except Exception:
        logger.exception("Ball detection failed for videoId=%s — continuing without it.", video_id)
        ball_metrics = None

    annotated_video_url = None
    if storage_annotated_video_fn is not None:
        try:
            annotated_video_url = generate_annotated_video(
                frames, pose_frames, athlete_id, video_id, storage_annotated_video_fn,
                info.fps, info.frame_width, info.frame_height,
            )
        except Exception:
            logger.exception("Annotated video generation failed for videoId=%s — continuing without it.", video_id)

    return AnalysisResult(
        videoId=video_id, athleteId=athlete_id, status="Complete",
        durationSeconds=round(info.duration_seconds, 2), fps=round(info.fps, 2),
        poseMetrics=pose_metrics, speedMetrics=speed_metrics, gaitBalance=gait_balance,
        anomalies=anomalies, keyframes=keyframes, annotatedVideoUrl=annotated_video_url,
        drillRecommendations=drill_recommendations, ballMetrics=ball_metrics,
    )
