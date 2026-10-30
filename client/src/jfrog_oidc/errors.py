from __future__ import annotations


class JFrogOidcError(Exception):
    """Base error for the jfrog_oidc package."""


class IdentityError(JFrogOidcError):
    """The platform identity token could not be obtained."""


class ExchangeError(JFrogOidcError):
    """JFrog rejected or failed the OIDC token exchange."""

    def __init__(self, message: str, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable
