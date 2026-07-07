"""Builds tiny synthetic test clips for verifying the video-analysis pipeline without
needing a real athlete recording. Uses a real MediaPipe sample image (an actual photo
of a person) so pose detection has something genuine to find — not a stick figure or
vector drawing, which the pose model won't reliably detect.
"""
import argparse
import os
import urllib.request

import cv2

SAMPLE_IMAGE_URL = "https://storage.googleapis.com/mediapipe-assets/pose.jpg"
HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_IMAGE = os.path.join(HERE, "_pose_sample.jpg")


def _get_sample_image():
    if not os.path.exists(CACHE_IMAGE):
        urllib.request.urlretrieve(SAMPLE_IMAGE_URL, CACHE_IMAGE)
    img = cv2.imread(CACHE_IMAGE)
    if img is None:
        raise RuntimeError("Failed to load sample pose image.")
    return img


def build_clip(out_path, num_frames=36, fps=12, mirror_second_half=False, zoom_drift=True):
    """mirror_second_half=True deliberately induces a left/right asymmetry partway
    through the clip, for testing the anomaly-detection + alert path end-to-end."""
    img = _get_sample_image()
    h, w = img.shape[:2]
    writer = cv2.VideoWriter(out_path, cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))

    for i in range(num_frames):
        frame = img.copy()
        if mirror_second_half and i >= num_frames // 2:
            frame = cv2.flip(frame, 1)
        if zoom_drift:
            # Simulate the athlete drifting/approaching the camera: a slight
            # progressive crop+resize gives the speed calculation something to see.
            scale = 1.0 + 0.15 * (i / num_frames)
            nh, nw = int(h / scale), int(w / scale)
            y0, x0 = (h - nh) // 2, (w - nw) // 2
            cropped = frame[y0:y0 + nh, x0:x0 + nw]
            frame = cv2.resize(cropped, (w, h))
        writer.write(frame)
    writer.release()
    return out_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=os.path.join(HERE, "sample_clip.mp4"))
    parser.add_argument("--asymmetric", action="store_true",
                         help="Mirror the second half of frames to deliberately induce a symmetry anomaly")
    parser.add_argument("--frames", type=int, default=36)
    args = parser.parse_args()

    path = build_clip(args.out, num_frames=args.frames, mirror_second_half=args.asymmetric)
    print(f"Wrote sample clip: {path}")
