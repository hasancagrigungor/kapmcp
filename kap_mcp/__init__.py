"""KAP MCP server — exposes the Turkish Public Disclosure Platform (KAP) API as MCP tools."""

from .client import KAPClient
from .exceptions import KAPAPIError, KAPAuthenticationError, KAPError, KAPValidationError

__version__ = "0.2.0"
__all__ = ["KAPClient", "KAPError", "KAPAPIError", "KAPAuthenticationError", "KAPValidationError"]
