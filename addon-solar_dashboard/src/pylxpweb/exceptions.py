"""Exceptions for Luxpower/EG4 API client."""

from __future__ import annotations


class LuxpowerError(Exception):
    """Base exception for all Luxpower errors."""


class LuxpowerAuthError(LuxpowerError):
    """Raised when authentication fails."""


class LuxpowerConnectionError(LuxpowerError):
    """Raised when connection to the API fails."""


class LuxpowerAPIError(LuxpowerError):
    """Raised when the API returns an error response."""


class LuxpowerDeviceError(LuxpowerError):
    """Raised when there's an issue with device operations."""


class LuxpowerDeviceOfflineError(LuxpowerDeviceError):
    """Raised when a device is offline or unreachable."""
