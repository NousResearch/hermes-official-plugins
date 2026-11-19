"""Error types shared by the store, services, tools, and HTTP API."""

from __future__ import annotations


class SecurityError(Exception):
    code = "error"

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message}


class ValidationError(SecurityError):
    code = "invalid_input"


class NotFound(SecurityError):
    code = "not_found"


class Conflict(SecurityError):
    code = "conflict"


class SealedError(SecurityError):
    code = "sealed"


class PolicyDenied(SecurityError):
    code = "policy_denied"


class TargetError(SecurityError):
    code = "target_error"
