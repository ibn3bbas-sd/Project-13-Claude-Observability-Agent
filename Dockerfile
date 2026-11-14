# ---- build stage ----
FROM python:3.12-slim AS builder
WORKDIR /build
COPY agent/requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ---- runtime stage ----
FROM python:3.12-slim
LABEL maintainer="platform-team@example.com"
LABEL description="claude-agent"
LABEL org.opencontainers.image.authors="Ibn Abbas"
LABEL org.opencontainers.image.licenses="MIT"
LABEL org.opencontainers.image.source="https://github.com/ibn3bbas-sd/Project-13-Claude-Observability-Agent"

# Non-root user
RUN useradd -u 10001 -r -s /sbin/nologin monitor
WORKDIR /app

# Copy installed packages
COPY --from=builder /install /usr/local

# Copy agent source
COPY agent/ .

USER monitor

ENTRYPOINT ["python", "-u", "monitor.py"]
