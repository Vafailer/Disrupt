"""Explicit business refusals, kept separate from transport and service errors."""

from fastapi import HTTPException


class IntegrationRejection(HTTPException):
    def __init__(self, status_code, code, message, *, operation_id=None):
        super().__init__(status_code, message)
        self.code = code
        self.operation_id = operation_id
