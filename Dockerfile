# Public demo of the portal (fly.io): both consoles on recorded fixtures, Setup read-only.
# No cloud CLIs, no logins, no JFrog token in this image.
FROM python:3.12-slim
RUN useradd --create-home app
RUN mkdir /app && chown app /app
WORKDIR /app
COPY --chown=app . .
USER app
ENV PYTHONPATH=/app/client/src:/app PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["python", "-m", "portal", "--demo", "--host", "0.0.0.0", "--port", "8080", "--public-host", "app.oidc.sudhakarchundu.org", "--public-host", "jfrog-oidc-portal.fly.dev"]
