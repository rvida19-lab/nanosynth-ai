# Self-hosting image. Recommended over the public demo for real research use:
# the Anthropic API key stays inside your own infrastructure.
FROM python:3.12-slim

WORKDIR /app

# Install deps first so code edits don't invalidate the dependency layer.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY static/ ./static/

# Run as a non-root user.
RUN useradd --create-home --uid 1000 nanosynth && chown -R nanosynth:nanosynth /app
USER nanosynth

EXPOSE 5000

# See render.yaml for why this is 1 worker / 8 threads / 300s timeout.
CMD ["gunicorn", "--workers", "1", "--threads", "8", "--timeout", "300", \
     "--bind", "0.0.0.0:5000", "app:app"]
