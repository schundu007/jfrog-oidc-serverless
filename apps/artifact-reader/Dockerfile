# Build context: repository root. BASE_IMAGE lets CI pull the base through JFrog
# (e.g. <jfrog-host>/docker-remote/library/python:3.12-slim) with an OIDC token.
#   docker build -f apps/artifact-reader/Dockerfile --build-arg BASE_IMAGE=... -t <registry>/artifact-reader:<tag> .
ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/client/src
WORKDIR /app
COPY client/src /app/client/src
COPY apps/artifact-reader/main.py /app/main.py
RUN useradd --uid 10001 --no-create-home app
USER 10001
ENTRYPOINT ["python", "/app/main.py"]
