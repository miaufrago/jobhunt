# Python 3.12: pandas/numpy (pulled in by JobSpy) ship wheels for it. 3.13+ do not yet.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    JOBHUNT_DATA=/data \
    JOBHUNT_CONFIG=/data/config.yaml \
    TZ=America/Los_Angeles

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata curl \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY jobhunt/ ./jobhunt/
COPY config.example.yaml ./config.default.yaml
COPY profile/resume.example.md ./profile.default/resume.md
COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD curl -fsS http://localhost:8080/healthz || exit 1

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["serve"]
