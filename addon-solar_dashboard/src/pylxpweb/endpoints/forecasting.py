"""Forecasting endpoints for the Luxpower API.

This module provides forecasting functionality including:
- Solar production forecasting
- Weather forecasts for plant locations
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pylxpweb.endpoints.base import BaseEndpoint

if TYPE_CHECKING:
    from pylxpweb.client import LuxpowerClient


class ForecastingEndpoints(BaseEndpoint):
    """Forecasting endpoints for solar and weather predictions."""

    def __init__(self, client: LuxpowerClient) -> None:
        """Initialize forecasting endpoints.

        Args:
            client: The parent LuxpowerClient instance
        """
        super().__init__(client)

    async def get_solar_forecast(self, serial_num: str) -> dict[str, Any]:
        """Get solar production forecast for parallel group.

        Retrieves predicted solar production for today/tomorrow based on
        weather data and historical patterns. Useful for optimizing battery
        charging schedules and energy management.

        Args:
            serial_num: Device serial number (any inverter in parallel group)

        Returns:
            Dict containing:
                - success: Boolean
                - serialNum: Device serial number
                - forecastDate: Date of forecast
                - predictions: List of prediction objects with:
                    - time: Time period (hour or date)
                    - predictedPower: Predicted PV power (W)
                    - confidence: Prediction confidence (0-100%)

        Example:
            forecast = await client.forecasting.get_solar_forecast("1234567890")
            for pred in forecast["predictions"]:
                print(f"{pred['time']}: {pred['predictedPower']}W "
                      f"(confidence: {pred['confidence']}%)")
        """
        await self.client._ensure_authenticated()

        data = {"serialNum": serial_num}

        response = await self.client._request(
            "POST",
            "/WManage/api/predict/solar/dayPredictColumnParallel",
            data=data,
        )

        return dict(response)

    async def get_weather_forecast(self, serial_num: str) -> dict[str, Any]:
        """Get weather forecast for plant location.

        Retrieves weather forecast data for the plant's geographic location.
        Useful for understanding conditions affecting solar production.

        Args:
            serial_num: Device serial number

        Returns:
            Dict containing:
                - success: Boolean
                - location: Location info (latitude, longitude, city)
                - current: Current weather conditions
                    - temperature: Current temp (Celsius)
                    - conditions: Weather description
                    - cloudCover: Cloud cover percentage
                - forecast: Multi-day forecast array
                    - date: Forecast date
                    - tempHigh: High temperature
                    - tempLow: Low temperature
                    - conditions: Weather description
                    - cloudCover: Cloud cover percentage

        Example:
            weather = await client.forecasting.get_weather_forecast("1234567890")
            print(f"Current: {weather['current']['temperature']}°C, "
                  f"{weather['current']['conditions']}")
            for day in weather["forecast"]:
                print(f"{day['date']}: {day['tempHigh']}°C/{day['tempLow']}°C, "
                      f"{day['conditions']}")
        """
        await self.client._ensure_authenticated()

        data = {"serialNum": serial_num}

        response = await self.client._request(
            "POST",
            "/WManage/api/weather/forecast",
            data=data,
        )

        return dict(response)
