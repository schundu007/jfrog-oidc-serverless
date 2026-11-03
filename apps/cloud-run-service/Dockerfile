# JFrog browser (Flask + gunicorn). Build context: repository root.
#
# Packages come only from apps/cloud-run-service/wheels: the build pipeline fills it from
# JFrog (pypi-remote) with its OIDC token, and BASE_IMAGE pulls through JFrog (docker-remote).
# Locally: pip download --only-binary=:all: --platform manylinux2014_x86_64 --python-version 3.12 \
#            -d apps/cloud-run-service/wheels -r apps/cloud-run-service/requirements.txt
ARG BASE_IMAGE=python:3.12-slim

FROM ${BASE_IMAGE} AS build
ENV PIP_NO_INDEX=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONDONTWRITEBYTECODE=1
COPY apps/cloud-run-service/requirements.txt /src/requirements.txt
COPY apps/cloud-run-service/wheels /src/wheels
RUN python -m venv /venv \
 && /venv/bin/pip install --no-index --find-links /src/wheels --require-virtualenv -r /src/requirements.txt
COPY client/src/jfrog_oidc /app/jfrog_oidc
COPY apps/cloud-run-service/app /app/app
COPY apps/cloud-run-service/gunicorn.conf.py /app/gunicorn.conf.py
COPY apps/cloud-run-service/tests /src/tests
# The image is only built when its tests pass.
RUN cd /src && PYTHONPATH=/app APP_JSON_LOGS=0 /venv/bin/python -m unittest discover -s tests -t . \
 && rm -rf /app/*/__pycache__

FROM ${BASE_IMAGE}
ARG APP_VERSION=dev
ENV PATH=/venv/bin:$PATH PYTHONPATH=/app PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    APP_VERSION=${APP_VERSION} PORT=8000
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app
COPY --from=build /venv /venv
COPY --from=build /app /app
WORKDIR /app
USER 10001
EXPOSE 8000
ENTRYPOINT ["gunicorn", "--config", "/app/gunicorn.conf.py", "app:create_app()"]
