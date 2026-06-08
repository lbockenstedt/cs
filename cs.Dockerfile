FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY src ./src
# Default to hub mode for compose, but can be overridden via CMD
CMD ["python", "src/control_plane.py", "--id", "cs-spoke-1", "--secret", "lm-secret", "--hub", "ws://hub:8765"]
