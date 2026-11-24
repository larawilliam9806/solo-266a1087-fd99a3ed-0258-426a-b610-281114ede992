"""Typed API errors."""
from __future__ import annotations

from typing import Any


class ApiProblem(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}


class ValidationProblem(ApiProblem):
    def __init__(self, code: str = "validation_error", message: str = "Invalid request.", details: dict[str, Any] | None = None):
        super().__init__(422, code, message, details)


class NotFoundProblem(ApiProblem):
    def __init__(self, code: str = "not_found", message: str = "Resource not found.", details: dict[str, Any] | None = None):
        super().__init__(404, code, message, details)


class ConflictProblem(ApiProblem):
    def __init__(self, code: str = "conflict", message: str = "Request conflicts with the current ledger.", details: dict[str, Any] | None = None):
        super().__init__(409, code, message, details)
