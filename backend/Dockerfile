FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
# Speech SDK runtime libraries, video encoding, and portable subtitle fonts.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates libasound2 libssl3 libstdc++6 ffmpeg fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.lock.txt ./
RUN pip install -r requirements.lock.txt && pip check
RUN groupadd --gid 10001 daybrief && useradd --uid 10001 --gid daybrief --create-home daybrief \
    && mkdir -p /data/videos /data/cache && chown -R daybrief:daybrief /data
COPY --chown=daybrief:daybrief daybrief ./daybrief
USER daybrief
# Offline prerequisite check: no credentials and no speech requests.
RUN python -m daybrief.video_check
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=5 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"
CMD ["uvicorn", "daybrief.api:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
