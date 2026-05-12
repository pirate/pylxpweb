"""Base endpoint class for all endpoint-specific modules.

This module provides the BaseEndpoint class that all endpoint modules inherit from.
It provides access to the parent client's session and request method.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pylxpweb.client import LuxpowerClient


class BaseEndpoint:
    """Base class for endpoint-specific functionality.

    All endpoint modules (analytics, plants, devices, etc.) inherit from this class
    to gain access to the parent client's session and request method.

    Attributes:
        client: Reference to the parent LuxpowerClient instance
    """

    def __init__(self, client: LuxpowerClient) -> None:
        """Initialize the endpoint with a reference to the parent client.

        Args:
            client: The parent LuxpowerClient instance
        """
        self.client = client

    def _get_cache_key(self, endpoint: str, **kwargs: Any) -> str:
        """Generate cache key for request.

        Args:
            endpoint: The endpoint name
            **kwargs: Parameters to include in cache key

        Returns:
            Cache key string
        """
        return self.client._get_cache_key(endpoint, **kwargs)
