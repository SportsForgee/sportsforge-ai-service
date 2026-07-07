"""Pydantic models mirroring hardware-integration/schemas/video-analysis-result.json
and backend/Api/Models/Dtos/VideoDtos.cs — keep the three in sync."""
from typing import List, Optional
from pydantic import BaseModel


class AnalyzeRequest(BaseModel):
    videoId: str
    athleteId: str
    videoPath: str  # local path or mounted path; videoUrl in the future for blob storage
    callbackUrl: str
    # Optional calibration input: a known real-world distance (meters) covered between
    # two frames, and the pixel distance the athlete travelled over that same span.
    # Without both, speed is reported as uncalibrated (relative only).
    referenceDistanceMeters: Optional[float] = None
    referencePixels: Optional[float] = None


class JointAngleWindow(BaseModel):
    startSeconds: float
    endSeconds: float
    avgKneeAngleDeg: Optional[float] = None
    avgHipAngleDeg: Optional[float] = None
    avgAnkleAngleDeg: Optional[float] = None


class PoseMetrics(BaseModel):
    jointAngleWindows: List[JointAngleWindow] = []
    symmetryScore: Optional[float] = None
    postureStabilityScore: Optional[float] = None
    # Transparency for trust: how much of the clip the pose model actually had a
    # confident lock on a person, so a low-confidence analysis isn't presented as
    # equally reliable as a clean one. 0-100, weighted by per-landmark visibility.
    detectionConfidence: Optional[float] = None
    framesSampled: Optional[int] = None
    framesWithPersonDetected: Optional[int] = None


class Calibration(BaseModel):
    method: str = "uncalibrated"  # pitch-markings | manual-distance | uncalibrated
    confidence: float = 0.0


class SpeedMetrics(BaseModel):
    topSpeedKmh: Optional[float] = None
    avgSpeedKmh: Optional[float] = None
    accelerationPeaksMs2: List[float] = []
    strideFrequencyHz: Optional[float] = None
    calibration: Calibration = Calibration()


class GaitBalance(BaseModel):
    score: float


class Anomaly(BaseModel):
    type: str
    severity: str  # Info | Warning | Critical
    timestampSeconds: float
    description: str


class Keyframe(BaseModel):
    timestampSeconds: float
    label: str
    thumbnailUrl: str


class AnalysisResult(BaseModel):
    videoId: str
    athleteId: str
    status: str  # Complete | Failed
    durationSeconds: Optional[float] = None
    fps: Optional[float] = None
    failureReason: Optional[str] = None

    # Full clip with the tracked skeleton drawn on every sampled frame — playback proof
    # of what the model saw throughout, not just a couple of still keyframes. Plays at
    # the sampled rate (TARGET_FPS), so choppier than the source video by design.
    annotatedVideoUrl: Optional[str] = None

    poseMetrics: Optional[PoseMetrics] = None
    speedMetrics: Optional[SpeedMetrics] = None
    gaitBalance: Optional[GaitBalance] = None
    anomalies: List[Anomaly] = []
    keyframes: List[Keyframe] = []
