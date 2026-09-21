"""Typed errors raised by the KAP client and surfaced to MCP callers."""

from typing import Optional


class KAPError(Exception):
    """Base error."""


class KAPValidationError(KAPError):
    """Bad input from the caller — fix the arguments, don't retry."""


class KAPAPIError(KAPError):
    """The gateway rejected or failed the request."""

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.error_message = error_message
        self.correlation_id = correlation_id

    @property
    def not_found(self) -> bool:
        """True for HTTP 404 and for the gateway's 400 + 'Bildirim bulunamadı / not found' answers."""
        m = (self.error_message or "").lower()
        return self.status_code == 404 or "bulunamad" in m or "not found" in m

    def __str__(self) -> str:
        parts = [super().__str__()]
        if self.status_code:
            parts.append(f"HTTP {self.status_code}")
        if self.error_code:
            parts.append(f"code={self.error_code}")
        if self.error_message:
            parts.append(self.error_message)
        if self.correlation_id:
            parts.append(f"correlationId={self.correlation_id}")
        return " | ".join(parts)


class KAPAuthenticationError(KAPAPIError):
    """Credentials or token were rejected."""
