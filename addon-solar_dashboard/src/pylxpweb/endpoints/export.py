"""Data export endpoints for the Luxpower API.

This module provides data export functionality for downloading
historical runtime data in CSV or Excel formats.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import urljoin

import aiohttp

from pylxpweb.endpoints.base import BaseEndpoint
from pylxpweb.exceptions import LuxpowerConnectionError

if TYPE_CHECKING:
    from pylxpweb.client import LuxpowerClient


class ExportEndpoints(BaseEndpoint):
    """Data export endpoints for downloading historical data."""

    def __init__(self, client: LuxpowerClient) -> None:
        """Initialize export endpoints.

        Args:
            client: The parent LuxpowerClient instance
        """
        super().__init__(client)

    async def export_data(
        self,
        serial_num: str,
        start_date: str,
        end_date: str | None = None,
    ) -> bytes:
        """Export historical data to CSV/Excel.

        Downloads historical runtime data for the specified date range.
        Returns binary data (CSV or Excel format) for external analysis.

        Args:
            serial_num: Device serial number
            start_date: Start date in YYYY-MM-DD format
            end_date: Optional end date (if None, exports single day)

        Returns:
            bytes: CSV/Excel file content

        Raises:
            LuxpowerAPIError: If export fails

        Example:
            # Export single day
            csv_data = await client.export.export_data("1234567890", "2025-11-19")
            with open("data.csv", "wb") as f:
                f.write(csv_data)

            # Export date range
            csv_data = await client.export.export_data(
                "1234567890",
                "2025-11-01",
                "2025-11-19"
            )

        Note:
            This is a GET request that returns binary data, not JSON.
        """
        await self.client._ensure_authenticated()

        session = await self.client._get_session()
        url_path = f"/WManage/web/analyze/data/export/{serial_num}/{start_date}"

        if end_date:
            url_path += f"?endDateText={end_date}"

        url = urljoin(self.client.base_url, url_path)

        try:
            async with session.get(url) as response:
                response.raise_for_status()
                return await response.read()

        except aiohttp.ClientError as err:
            raise LuxpowerConnectionError(f"Export failed: {err}") from err
