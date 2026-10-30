# Build context: repository root
#   docker build -f rotator/Dockerfile -t <region>-docker.pkg.dev/<p>/<repo>/jfrog-oidc-rotator:0.1.0 .
#
# Host this image in Artifact Registry (standard repo) / ACR, NOT behind the
# JFrog remote it refreshes: if the token ever expires, the rotator must still be
# pullable to fix it.
#
# BASE_IMAGE: point it at your approved mirror (e.g. a JFrog remote of Docker
# Hub, or AR/ACR) when policy forbids pulling from Docker Hub directly. The AKS
# pilot's security review flagged exactly that for its probe images and chart.
#   docker build --build-arg BASE_IMAGE=acme-docker-remote.jfrog.io/library/python:3.12-slim ...
ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/client/src
WORKDIR /app
COPY client/src /app/client/src
COPY rotator/rotate.py /app/rotate.py

RUN useradd --uid 10001 --no-create-home rotator
USER 10001

ENTRYPOINT ["python", "/app/rotate.py"]
