from __future__ import annotations


class AppError(Exception):
    def __init__(self, message: str, *, exit_code: int = 2, data: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.exit_code = exit_code
        self.data = data or {}


class ValidationError(AppError):
    def __init__(self, message: str, *, data: dict | None = None) -> None:
        super().__init__(message, exit_code=2, data=data)


class ReconciliationError(AppError):
    def __init__(self, message: str, *, data: dict | None = None) -> None:
        super().__init__(message, exit_code=3, data=data)


class LockedPeriodError(AppError):
    def __init__(self, message: str, *, data: dict | None = None) -> None:
        super().__init__(message, exit_code=4, data=data)


class ImportConflictError(AppError):
    def __init__(self, message: str, *, data: dict | None = None) -> None:
        super().__init__(message, exit_code=5, data=data)


class ComplianceError(AppError):
    def __init__(self, message: str, *, data: dict | None = None) -> None:
        super().__init__(message, exit_code=6, data=data)
