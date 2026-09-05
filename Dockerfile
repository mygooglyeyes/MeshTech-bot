# MeshTech-Bot - container image.
# Runs the same bot.py, talking to your openHop Repeater over the network.
#
# Build / run examples are in docs/INSTALL.md (Docker section); the usual
# path is docker compose, which mounts ./config.yaml and ./data from the
# host so your config and database survive container updates.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Build stamp: pass the source commit so the dashboard header can show
# which revision the container runs.  Overridable with --build-arg, or via
# compose: GIT_SHA=$(git rev-parse --short HEAD) docker compose up -d --build
ARG GIT_SHA=unknown

WORKDIR /app

# Dependencies first (better layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# The project itself
COPY bot.py ./
COPY core ./core
COPY handlers ./handlers
COPY web ./web
COPY config.example.yaml ./config.example.yaml

# Bake the build stamp (.git is not copied into the image, so a file is
# the only reliable source inside the container).
RUN echo -n "$GIT_SHA" > /app/.git-commit

# Run as an unprivileged user inside the container.
RUN mkdir -p /app/data \
    && useradd --system --uid 1001 --no-create-home meshtech \
    && chown -R meshtech:meshtech /app

USER meshtech

# Config is mounted in read-only at runtime (docker compose). Expose the
# dashboard port (only useful with bridge networking).
EXPOSE 8081

CMD ["python", "bot.py"]
