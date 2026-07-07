"""CLI equivalent of the hardware simulators (see hardware-integration/simulators) —
runs the full OpenCV + MediaPipe pipeline on a local video file and prints
schema-conformant result JSON. Runnable without the .NET API or a live callback.

Usage:
    python scripts/analyze_sample.py path/to/clip.mp4
    python scripts/analyze_sample.py path/to/clip.mp4 --ref-distance 10 --ref-pixels 240
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from video_analysis.pipeline import analyze_video  # noqa: E402
from video_analysis.routes import thumbnail_path    # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("video_path")
    parser.add_argument("--video-id", default="sample-video-1")
    parser.add_argument("--athlete-id", default="sample-athlete")
    parser.add_argument("--out", default=None, help="Also write the result JSON to this file")
    parser.add_argument("--ref-distance", type=float, default=None, help="Known real-world distance in meters, for speed calibration")
    parser.add_argument("--ref-pixels", type=float, default=None, help="Pixel distance corresponding to --ref-distance")
    args = parser.parse_args()

    result = analyze_video(
        video_id=args.video_id, athlete_id=args.athlete_id, video_path=args.video_path,
        storage_thumbnail_fn=thumbnail_path,
        reference_distance_m=args.ref_distance, reference_pixels=args.ref_pixels,
    )

    output = json.dumps(result.model_dump(), indent=2)
    print(output)
    if args.out:
        with open(args.out, "w") as f:
            f.write(output)
        print(f"\nWrote result to {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
