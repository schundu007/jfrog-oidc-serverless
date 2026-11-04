"""gunicorn for Cloud Run and Azure Container Apps: one process, threads for concurrency
(the JFrog token is cached per process), no worker timeout (the platform enforces the
request timeout), and a graceful stop inside the platform's 10 s SIGTERM window."""

import os

wsgi_app = "app:create_app()"   # App Service starts gunicorn with no app argument (its shell eval breaks on quotes)
bind = f"0.0.0.0:{os.environ.get('PORT', '8000')}"
workers = int(os.environ.get("WEB_CONCURRENCY", "1"))
worker_class = "gthread"
threads = int(os.environ.get("GUNICORN_THREADS", "8"))
timeout = 0
graceful_timeout = 8
keepalive = 5
forwarded_allow_ips = "*"   # behind the platform's front end, which sets X-Forwarded-*
accesslog = None            # the app logs each request as structured JSON
control_socket_disable = True  # no control socket in a read-only, home-less container
errorlog = "-"
loglevel = os.environ.get("LOG_LEVEL", "info").lower()
logconfig_dict = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"json": {"()": "app.logs.JsonFormatter"}},
    "handlers": {"stdout": {"class": "logging.StreamHandler", "formatter": "json", "stream": "ext://sys.stdout"}},
    "root": {"handlers": ["stdout"], "level": loglevel.upper()},
    "loggers": {
        "gunicorn.error": {"handlers": ["stdout"], "level": loglevel.upper(), "propagate": False, "qualname": "gunicorn.error"},
        "gunicorn.access": {"handlers": [], "level": "CRITICAL", "propagate": False, "qualname": "gunicorn.access"},
    },
}
