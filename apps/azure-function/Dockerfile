# Functions on Container Apps image. Build context: repository root.
#   docker build -f apps/azure-function/Dockerfile --build-arg BASE_IMAGE=... -t <registry>/<name>:<tag> .
# BASE_IMAGE is the Azure Functions Python host (from MCR; pass a JFrog remote for it when there is one).
ARG BASE_IMAGE=mcr.microsoft.com/azure-functions/python:4-python3.12
FROM ${BASE_IMAGE}
ENV AzureWebJobsScriptRoot=/home/site/wwwroot AzureFunctionsJobHost__Logging__Console__IsEnabled=true \
    PYTHONDONTWRITEBYTECODE=1
COPY client/src/jfrog_oidc /home/site/wwwroot/jfrog_oidc
COPY apps/azure-function/function_app.py apps/azure-function/host.json /home/site/wwwroot/
