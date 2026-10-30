# Build context: repository root
#   docker build -f samples/cloud-run-job/Dockerfile -t <region>-docker.pkg.dev/<p>/platform/jfrog-oidc-demo:0.1.0 .
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/client/src
WORKDIR /app
COPY client/src /app/client/src
COPY samples/cloud-run-job/main.py /app/main.py
RUN useradd --uid 10001 --no-create-home app
USER 10001
ENTRYPOINT ["python", "/app/main.py"]
