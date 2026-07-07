FROM python:3.11-slim

WORKDIR /app

# mediapipe + opencv need these native libs even in "headless" builds (libGL is the
# classic missing-shared-object failure on slim Debian images).
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 libglib2.0-0 libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Pre-download the pose model at build time so the first real request isn't slow
# and so the container doesn't need outbound internet at runtime.
RUN python -c "from video_analysis.pose_model import ensure_model_downloaded; ensure_model_downloaded()"

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001"]
