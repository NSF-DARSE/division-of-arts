# Container image for the SceneScout demo site.
# Used by App Runner / ECS / Lightsail; the EC2 path in deploy/ needs no Docker.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    SCENESCOUT_PRELOAD=1 \
    PORT=8080

WORKDIR /app

COPY deploy/requirements-site.txt deploy/requirements-site.txt
RUN pip install --upgrade pip && pip install -r deploy/requirements-site.txt

COPY scenescout/ scenescout/
COPY mock_site/ mock_site/
COPY assets/ assets/
COPY wsgi.py wsgi.py

RUN useradd --system --create-home scenescout \
    && mkdir -p /app/data /app/out \
    && chown -R scenescout:scenescout /app
USER scenescout

EXPOSE 8080

# --preload seeds the calendar once in the master, before workers fork.
CMD ["sh", "-c", "gunicorn --preload -b 0.0.0.0:${PORT} -w 2 --timeout 120 --access-logfile - wsgi:app"]
