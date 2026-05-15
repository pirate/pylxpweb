#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = ["flask", "pylxpweb"]
# ///
"""
Solar Dashboard - 3D Visualization of Solar Array and Live Stats

A single-file Flask application with Three.js frontend showing:
- Animated sun position based on real astronomical calculations
- 3D roof with solar panels at correct azimuths (217° SW, 37° NE)
- Live inverter stats with real-time polling

Run with: uv run python solar_dashboard.py
Then open: http://localhost:5000
"""

import asyncio
import json
import math
import sys
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# Add the src directory to path for local development
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from flask import Flask, jsonify, render_template_string, request

from pylxpweb import LuxpowerClient
from pylxpweb.devices.station import Station

# =============================================================================
# CONFIGURATION
# =============================================================================

# Configuration source, in priority order:
#   1. /data/options.json   — Home Assistant add-on options (when running as addon)
#   2. environment vars     — convenient for docker compose / systemd
#   3. hardcoded defaults below — local dev fallback
def _load_addon_options():
    import json
    try:
        with open("/data/options.json") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

_OPTS = _load_addon_options()

# Load .env file (if present) into os.environ for local dev
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _, _v = _line.partition("=")
                os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

def _opt(key, env_key, default=""):
    return _OPTS.get(key) or os.getenv(env_key) or default

# Oakland, California coordinates
LATITUDE = float(_opt("latitude", "LATITUDE", 37.8044))
LONGITUDE = float(_opt("longitude", "LONGITUDE", -122.2712))
TIMEZONE = ZoneInfo("America/Los_Angeles")

# =============================================================================
# 3D SCENE CONFIGURATION (all dimensions in feet, scaled for visualization)
# =============================================================================
SCENE_SCALE = 3.0  # Feet per 3D unit

# House configuration (dimensions in feet)
HOUSE = {
    "length_ft": 80,           # Along ridgeline (NW-SE direction)
    "width_ft": 20,            # Perpendicular to ridgeline (SW-NE direction)
    "height_ft": 30,           # Wall height to eaves
    "position_ft": [-20, -20],  # [x, z] center position in feet (NW of origin to clear road)
}

# Solar array configuration
# - "roof" arrays: positioned automatically on house roof
# - "ground" arrays: need position_ft, size_ft, height_ft
SOLAR_ARRAYS = [
    {
        # Hardware: 14 × 550 W panels = 7.7 kW nameplate. Newer panels, minimal
        # degradation expected. MPPT1 input wired to the SW roof.
        "name": "SW Roof (MPPT1)",
        "type": "roof",
        "azimuth": 217.0,
        "tilt": 10.0,
        "capacity_kw": 7.7,
        "panel_count": 14,
        "panel_layout": [7, 2],   # 7 cols × 2 rows
        "color": 0x3498db,        # Blue
    },
    {
        # Hardware: 14 × 550 W panels = 7.7 kW nameplate. MPPT2 input wired to NE roof.
        "name": "NE Roof (MPPT2)",
        "type": "roof",
        "azimuth": 37.0,
        "tilt": 10.0,
        "capacity_kw": 7.7,
        "panel_count": 14,
        "panel_layout": [7, 2],
        "color": 0x9b59b6,        # Purple
    },
    {
        # MPPT3 is an older/less-efficient set of panels physically distributed
        # across BOTH the NE and SW roof faces, so its effective irradiance is
        # the average of what NE and SW receive at any given moment. Modeled
        # below as a "split" array with two azimuth halves at the same 10° tilt
        # as the rest of the roof.
        #
        # Hardware: 10 × 220 W panels = 2.2 kW nameplate.
        # Aspirational capacity 2.1 kW (~95% of nameplate): allows for ~5%
        # age-related cell degradation but assumes the panels are clean and
        # the strings are healthy. The gap between this and observed output
        # represents the recoverable headroom (soiling, MPPT tuning, etc.).
        "name": "Older Mixed (MPPT3)",
        "type": "split",
        "azimuth": [37.0, 217.0],   # 5 on NE face, 5 on SW face
        "fractions": [0.5, 0.5],    # equal split
        "tilt": 10.0,               # same roof pitch
        "capacity_kw": 2.1,         # aspirational: clean older 10 × 220 W string
        "panel_count": 10,          # 5 per side
        "panel_layout": [5, 1],     # per-side layout: 5 cols × 1 row
        "color": 0x27ae60,          # Green
    },
]

# =============================================================================
# DERIVED VALUES (computed from config above - do not edit)
# =============================================================================
TOTAL_ARRAY_KW = sum(arr["capacity_kw"] for arr in SOLAR_ARRAYS)

# Roof properties derived from roof arrays
_roof_arrays = [a for a in SOLAR_ARRAYS if a.get("type") == "roof"]
ROOF_TILT = _roof_arrays[0]["tilt"] if _roof_arrays else 10.0

# Ridgeline: perpendicular to SW roof face (SW azimuth - 90°)
_sw_array = next((a for a in SOLAR_ARRAYS if "SW" in a["name"]), None)
RIDGELINE_AZIMUTH = (_sw_array["azimuth"] - 90) if _sw_array else 127.0

# System efficiency — aspirational model.
# Represents the best realistic case if everything were optimal:
#   - Panels perfectly clean (no soiling losses)
#   - Modern hybrid inverter at peak efficiency (~97%)
#   - Minimal wiring losses (~1%)
#   - Optimal cell temperature (no thermal derating)
#   - No string mismatch
# 0.97 = 0.97 inverter * 0.99 wiring * 1.0 (clean) * 1.0 (cool cells) * ~1.01 small headroom
# Actual production should always be at-or-below this number; the gap shows you
# how much performance is being lost to soiling, age, heat, suboptimal MPPT,
# wiring losses, etc. — i.e. *optimization headroom*.
SYSTEM_EFFICIENCY = 0.97

# Inverter credentials — sourced from addon options / env / .env file
INVERTER_CONFIG = {
    "username": _opt("eg4_username", "EG4_USERNAME"),
    "password": _opt("eg4_password", "EG4_PASSWORD"),
    "base_url": _opt("eg4_base_url", "EG4_BASE_URL", "https://monitor.eg4electronics.com"),
}

if not INVERTER_CONFIG["username"] or not INVERTER_CONFIG["password"]:
    raise SystemExit(
        "Missing EG4 credentials. Set them in addon options (/data/options.json), "
        "environment variables (EG4_USERNAME / EG4_PASSWORD), or a .env file."
    )

# =============================================================================
# TOU / SCHEDULE CONSTANTS (mirror live inverter config — do not edit here to
# change inverter behavior; use a dedicated apply_*.py script for that)
# =============================================================================
PGE_PEAK_HOURS = (16, 21)        # 4 PM – 9 PM PG&E peak
PGE_PARTIAL_PEAK_HOURS_AFT = (15, 16)  # 3-4 PM partial peak
PGE_PARTIAL_PEAK_HOURS_EVE = (21, 24)  # 9 PM – midnight partial peak
AVA_BONUS_HOURS = (15, 20)       # 3 – 8 PM Ava peak-export bonus

FORCED_DISCHARGE_WINDOW = (16, 21)  # current schedule on inverter (16:00-20:59)
FORCED_DISCHARGE_SOC_FLOOR = 40     # %
SYSTEM_CHARGE_SOC_LIMIT = 95        # %
DISCHARGE_CUTOFF_SOC = 8            # %
FEED_IN_GRID_POWER_KW = 12          # kW max export

# PG&E E-TOU-C rate estimates for IMPORT $/kWh (seasonal — summer/winter)
# E-TOU-C summer (Jun-Sep): higher rates. Winter (Oct-May): lower.
# These ignore the baseline credit (~$0.10/kWh on first ~300 kWh/month) since
# our import volume is essentially zero.
_IMPORT_RATES = {
    # month: (peak_$/kWh, offpeak_$/kWh)
    1:  (0.47, 0.38), 2:  (0.47, 0.38), 3:  (0.47, 0.38),
    4:  (0.47, 0.38), 5:  (0.47, 0.38),
    6:  (0.55, 0.42), 7:  (0.55, 0.42), 8:  (0.55, 0.42),
    9:  (0.55, 0.42),
    10: (0.47, 0.38), 11: (0.47, 0.38), 12: (0.47, 0.38),
}

# NEM 3.0 / NBT EXPORT rates ($/kWh) via Avoided Cost Calculator.
# These vary widely by month. Spring (Mar-May) is the trough (lots of solar
# supply, low demand); late summer (Jul-Sep) evening peaks can spike to
# $0.30-1.00+/kWh during heat events. Numbers below are realistic monthly
# averages — actual hourly rates published at pge.com/energyexportcredit.
_EXPORT_RATES = {
    # month: (peak_window_$/kWh, offpeak_$/kWh)
    1:  (0.18, 0.05),  # Jan - winter
    2:  (0.18, 0.05),
    3:  (0.10, 0.03),  # Mar - spring trough begins
    4:  (0.08, 0.025),
    5:  (0.10, 0.025), # May - spring trough
    6:  (0.20, 0.04),  # Jun - summer ramping
    7:  (0.35, 0.06),
    8:  (0.45, 0.07),  # Aug - peak summer
    9:  (0.35, 0.06),
    10: (0.18, 0.04),
    11: (0.18, 0.05),
    12: (0.18, 0.05),
}

# Ava Community Energy peak-export bonus: +$0.025/kWh for non-CARE customers
# on exports between 15:00 and 20:00 (3-8 PM)
AVA_BONUS_USD_PER_KWH = 0.025


def get_rates_for_now(now: datetime):
    """Return (import_peak, import_offpeak, export_peak, export_offpeak) for the
    current month — used by the $/hr revenue estimator."""
    m = now.month
    ip, io = _IMPORT_RATES.get(m, (0.47, 0.38))
    ep, eo = _EXPORT_RATES.get(m, (0.10, 0.03))
    return ip, io, ep, eo

# =============================================================================
# SOLAR CALCULATIONS (same as run.py)
# =============================================================================

def calculate_solar_position(dt: datetime, latitude: float, longitude: float) -> dict:
    """Calculate sun position for a given time and location."""
    dt_utc = dt.astimezone(timezone.utc)

    year = dt_utc.year
    month = dt_utc.month
    day = dt_utc.day
    hour = dt_utc.hour + dt_utc.minute / 60.0 + dt_utc.second / 3600.0

    if month <= 2:
        year -= 1
        month += 12

    A = int(year / 100)
    B = 2 - A + int(A / 4)
    JD = int(365.25 * (year + 4716)) + int(30.6001 * (month + 1)) + day + hour / 24.0 + B - 1524.5
    T = (JD - 2451545.0) / 36525.0

    L0 = (280.46646 + 36000.76983 * T + 0.0003032 * T**2) % 360
    M = (357.52911 + 35999.05029 * T - 0.0001537 * T**2) % 360
    M_rad = math.radians(M)
    e = 0.016708634 - 0.000042037 * T - 0.0000001267 * T**2

    C = ((1.914602 - 0.004817 * T - 0.000014 * T**2) * math.sin(M_rad) +
         (0.019993 - 0.000101 * T) * math.sin(2 * M_rad) +
         0.000289 * math.sin(3 * M_rad))

    sun_lon = L0 + C
    omega = 125.04 - 1934.136 * T
    sun_lon_apparent = sun_lon - 0.00569 - 0.00478 * math.sin(math.radians(omega))

    obliquity = 23.439291 - 0.013004 * T
    obliquity_corrected = obliquity + 0.00256 * math.cos(math.radians(omega))
    obliquity_rad = math.radians(obliquity_corrected)

    sun_lon_rad = math.radians(sun_lon_apparent)
    declination = math.degrees(math.asin(math.sin(obliquity_rad) * math.sin(sun_lon_rad)))

    y = math.tan(obliquity_rad / 2) ** 2
    L0_rad = math.radians(L0)
    eq_time = 4 * math.degrees(
        y * math.sin(2 * L0_rad) -
        2 * e * math.sin(M_rad) +
        4 * e * y * math.sin(M_rad) * math.cos(2 * L0_rad) -
        0.5 * y**2 * math.sin(4 * L0_rad) -
        1.25 * e**2 * math.sin(2 * M_rad)
    )

    solar_noon_utc = 720 - 4 * longitude - eq_time
    current_time_utc = hour * 60
    hour_angle = (current_time_utc - solar_noon_utc) / 4
    hour_angle_rad = math.radians(hour_angle)

    lat_rad = math.radians(latitude)
    dec_rad = math.radians(declination)

    sin_altitude = (math.sin(lat_rad) * math.sin(dec_rad) +
                    math.cos(lat_rad) * math.cos(dec_rad) * math.cos(hour_angle_rad))
    altitude = math.degrees(math.asin(max(-1, min(1, sin_altitude))))

    cos_azimuth = ((math.sin(dec_rad) - math.sin(lat_rad) * sin_altitude) /
                   (math.cos(lat_rad) * math.cos(math.radians(altitude)))) if altitude != 0 else 0
    cos_azimuth = max(-1, min(1, cos_azimuth))
    azimuth = math.degrees(math.acos(cos_azimuth))
    if hour_angle > 0:
        azimuth = 360 - azimuth

    # Sunrise/sunset — solve at apparent altitude = -0.833° to account for
    # atmospheric refraction (~34') + solar disk radius (~16'). This matches
    # the standard "upper limb on horizon" definition used by sunrise-sunset.org,
    # NOAA, etc. Without this correction the result was ~6 min late at sunrise
    # and ~6 min early at sunset.
    SUNRISE_ALTITUDE_DEG = -0.833
    sin_alt_h = math.sin(math.radians(SUNRISE_ALTITUDE_DEG))
    cos_ha_sunrise = (sin_alt_h - math.sin(lat_rad) * math.sin(dec_rad)) / (math.cos(lat_rad) * math.cos(dec_rad))
    if cos_ha_sunrise >= 1:
        sunrise_hour, sunset_hour = None, None
    elif cos_ha_sunrise <= -1:
        sunrise_hour, sunset_hour = 0, 24
    else:
        ha_sunrise = math.degrees(math.acos(cos_ha_sunrise))
        sunrise_utc = solar_noon_utc - ha_sunrise * 4
        sunset_utc = solar_noon_utc + ha_sunrise * 4
        local_offset = dt.utcoffset().total_seconds() / 3600 if dt.utcoffset() else 0
        sunrise_hour = (sunrise_utc / 60 + local_offset) % 24
        sunset_hour = (sunset_utc / 60 + local_offset) % 24

    return {
        "altitude": altitude,
        "azimuth": azimuth,
        "sunrise_hour": sunrise_hour,
        "sunset_hour": sunset_hour,
        "is_daylight": altitude > 0,
    }


def calculate_sun_path(date: datetime, latitude: float, longitude: float) -> list[dict]:
    """Calculate sun positions throughout the day for the arc visualization."""
    sun_path = []
    # Calculate positions every 30 minutes from 5 AM to 8 PM
    for hour in range(5, 21):
        for minute in [0, 30]:
            time_at_hour = date.replace(hour=hour, minute=minute, second=0, microsecond=0)
            pos = calculate_solar_position(time_at_hour, latitude, longitude)
            if pos["altitude"] > -5:  # Include slightly below horizon for smooth arc
                sun_path.append({
                    "hour": hour + minute / 60,
                    "altitude": pos["altitude"],
                    "azimuth": pos["azimuth"],
                })
    return sun_path


def calculate_clear_sky_dni(altitude: float) -> float:
    """Calculate clear-sky Direct Normal Irradiance."""
    if altitude <= 0:
        return 0.0
    altitude_rad = math.radians(altitude)
    air_mass = 1.0 / (math.sin(altitude_rad) + 0.50572 * (altitude + 6.07995) ** -1.6364)
    transmittance = 0.7 ** (air_mass ** 0.678)
    return 1361.0 * transmittance


def calculate_panel_irradiance(sun_alt: float, sun_az: float, panel_tilt: float, panel_az: float, dni: float) -> float:
    """Calculate irradiance on a tilted panel."""
    if sun_alt <= 0 or dni <= 0:
        return 0.0

    sun_alt_rad = math.radians(sun_alt)
    sun_az_rad = math.radians(sun_az)
    panel_tilt_rad = math.radians(panel_tilt)
    panel_az_rad = math.radians(panel_az)

    cos_incidence = (
        math.sin(sun_alt_rad) * math.cos(panel_tilt_rad) +
        math.cos(sun_alt_rad) * math.sin(panel_tilt_rad) * math.cos(sun_az_rad - panel_az_rad)
    )

    if cos_incidence <= 0:
        return 0.0

    direct = dni * cos_incidence
    ghi = dni * math.sin(sun_alt_rad)
    # Diffuse fraction: 22% — aspirational clear-sky for California summer
    # (real-world ranges 12-20%, but the upper end represents pristine
    # atmospheric conditions). Combined with ground albedo, low-tilt panels
    # can pick up significant diffuse irradiance.
    DIFFUSE_FRACTION = 0.22
    diffuse = ghi * DIFFUSE_FRACTION * (1 + math.cos(panel_tilt_rad)) / 2
    return direct + diffuse


# =============================================================================
# INVERTER DATA FETCHING
# =============================================================================

# Global cache for inverter data and persistent client
_inverter_cache = {
    "data": None,
    "last_update": None,
    "client": None,
    "inverter": None,
    "loop": None
}


def compute_schedule_status(now: datetime, inverter_data: dict | None) -> dict:
    """Compute current TOU + operational mode based on local time + live data.

    Pure-local computation; uses the constants above. Used by the dashboard
    for at-a-glance "what is my system doing right now".
    """
    h = now.hour + now.minute / 60.0

    def in_window(window):
        start, end = window
        return start <= h < end

    in_peak = in_window(PGE_PEAK_HOURS)
    in_partial_peak = in_window(PGE_PARTIAL_PEAK_HOURS_AFT) or in_window(PGE_PARTIAL_PEAK_HOURS_EVE)
    in_ava_bonus = in_window(AVA_BONUS_HOURS)
    in_forced_discharge = in_window(FORCED_DISCHARGE_WINDOW)

    # Time until next peak window starts (or until current peak ends)
    if in_peak:
        next_event = "peak ends"
        next_event_h = PGE_PEAK_HOURS[1]
    else:
        next_event = "peak starts"
        next_event_h = PGE_PEAK_HOURS[0] if h < PGE_PEAK_HOURS[0] else PGE_PEAK_HOURS[0] + 24
    minutes_to_next = int((next_event_h - h) * 60) % (24 * 60)

    # Operational mode from current power flow (descriptive only)
    mode = "idle"
    if inverter_data:
        ptg = inverter_data.get("power_to_grid") or 0
        ptu = inverter_data.get("power_to_user") or 0
        bcp = inverter_data.get("battery_charge_power") or 0
        bdp = inverter_data.get("battery_discharge_power") or 0
        ppv = inverter_data.get("pv_total_power") or 0

        if in_forced_discharge and (bdp > 200 or ptg > 200):
            mode = "forced discharge to grid"
        elif ptg > 500 and bcp > 500:
            mode = "PV exporting + charging battery"
        elif ptg > 500:
            mode = "PV exporting to grid"
        elif bcp > 500:
            mode = "charging battery from PV"
        elif bdp > 500 and ptu > 100:
            mode = "battery serving load"
        elif ptu > 100 and ppv < 100:
            mode = "grid serving load (importing)"
        elif ppv > 100:
            mode = "PV serving load"

    # Seasonal $/hr estimate. Uses month-specific NBT export rates (NEM 3.0
    # ACC values) and E-TOU-C import rates. Adds Ava CARE/FERA peak-export
    # bonus when in the 15:00-20:00 window.
    ip_rate, io_rate, ep_rate, eo_rate = get_rates_for_now(now)
    if in_ava_bonus:
        ep_rate += AVA_BONUS_USD_PER_KWH
        eo_rate += AVA_BONUS_USD_PER_KWH

    dollars_per_hour = 0.0
    if inverter_data:
        ptg_kw = (inverter_data.get("power_to_grid") or 0) / 1000.0
        ptu_kw = (inverter_data.get("power_to_user") or 0) / 1000.0
        if in_peak:
            dollars_per_hour = ptg_kw * ep_rate - ptu_kw * ip_rate
        else:
            dollars_per_hour = ptg_kw * eo_rate - ptu_kw * io_rate

    return {
        "in_peak": in_peak,
        "in_partial_peak": in_partial_peak,
        "in_ava_bonus": in_ava_bonus,
        "in_forced_discharge_window": in_forced_discharge,
        "minutes_to_peak_change": minutes_to_next,
        "next_event_label": next_event,
        "operational_mode": mode,
        "dollars_per_hour_est": dollars_per_hour,
        "rates": {
            "import_peak":      ip_rate,
            "import_offpeak":   io_rate,
            "export_peak":      ep_rate,    # includes Ava bonus if in 15-20h window
            "export_offpeak":   eo_rate,
            "currently_active_export_rate": ep_rate if in_peak else eo_rate,
            "currently_active_import_rate": ip_rate if in_peak else io_rate,
        },
        "config": {
            "forced_discharge_window": list(FORCED_DISCHARGE_WINDOW),
            "forced_discharge_soc_floor": FORCED_DISCHARGE_SOC_FLOOR,
            "system_charge_soc_limit": SYSTEM_CHARGE_SOC_LIMIT,
            "discharge_cutoff_soc": DISCHARGE_CUTOFF_SOC,
            "feed_in_power_kw": FEED_IN_GRID_POWER_KW,
        },
    }


async def fetch_inverter_data():
    """Fetch live data from inverter using cached client."""
    global _inverter_cache
    try:
        # Reuse existing client if available
        if _inverter_cache["client"] is None:
            client = LuxpowerClient(
                username=INVERTER_CONFIG["username"],
                password=INVERTER_CONFIG["password"],
                base_url=INVERTER_CONFIG["base_url"]
            )
            await client.__aenter__()
            _inverter_cache["client"] = client

            # Load stations and get inverter once
            stations = await Station.load_all(client)
            if not stations:
                return None
            station = stations[0]
            for inv in station.all_inverters:
                _inverter_cache["inverter"] = inv
                break

        inverter = _inverter_cache["inverter"]
        if not inverter:
            return None

        await inverter.refresh()

        def _safe(getter, default=None):
            try:
                return getter()
            except Exception:
                return default

        # FlexBOSS21 BMS-reported max chg/dis currents come through pylxpweb's
        # max_charge_current / max_discharge_current with a ÷100 scale factor
        # baked in — but on this hardware the actual scaling is ÷10. Apply a
        # ×10 correction so the dashboard shows real amps (160/180 A range,
        # matching the published BMS limits) rather than off-by-10 values.
        max_chg = _safe(lambda: inverter.max_charge_current)
        max_dis = _safe(lambda: inverter.max_discharge_current)
        max_chg_corrected = max_chg * 10 if max_chg is not None else None
        max_dis_corrected = max_dis * 10 if max_dis is not None else None

        return {
            "pv1_voltage": inverter.pv1_voltage,
            "pv1_power": inverter.pv1_power,
            "pv2_voltage": inverter.pv2_voltage,
            "pv2_power": inverter.pv2_power,
            "pv3_voltage": inverter.pv3_voltage,
            "pv3_power": inverter.pv3_power,
            "pv_total_power": inverter.pv_total_power,
            "battery_voltage": inverter.battery_voltage,
            "battery_soc": inverter.battery_soc,
            "battery_charge_power": inverter.battery_charge_power,
            "battery_discharge_power": inverter.battery_discharge_power,
            "battery_temperature": inverter.battery_temperature,
            "max_charge_current": max_chg_corrected,
            "max_discharge_current": max_dis_corrected,
            "grid_voltage": inverter.grid_voltage_r,
            "grid_frequency": inverter.grid_frequency,
            "power_to_grid": inverter.power_to_grid,
            "power_to_user": inverter.power_to_user,
            "consumption_power": inverter.consumption_power,
            "inverter_power": inverter.inverter_power,
            "inverter_temperature": inverter.inverter_temperature,
            "status": inverter.status_text,
            # Today's energy totals (kWh) — populated when EnergyInfo is available
            "energy_today_yield":    _safe(lambda: inverter.total_energy_today, 0.0),
            "energy_today_charge":   _safe(lambda: inverter.energy_today_charging, 0.0),
            "energy_today_discharge":_safe(lambda: inverter.energy_today_discharging, 0.0),
            "energy_today_import":   _safe(lambda: inverter.energy_today_import, 0.0),
            "energy_today_export":   _safe(lambda: inverter.energy_today_export, 0.0),
            "energy_today_usage":    _safe(lambda: inverter.energy_today_usage, 0.0),
        }
    except Exception as e:
        import traceback
        print(f"Error fetching inverter data: {e}", flush=True)
        traceback.print_exc()
        # Reset client on error to force re-login next time
        _inverter_cache["client"] = None
        _inverter_cache["inverter"] = None
        return None


def get_inverter_data_sync():
    """Synchronous wrapper for async inverter fetch with persistent event loop."""
    global _inverter_cache
    # Reuse the same event loop to keep client connections alive
    if _inverter_cache["loop"] is None or _inverter_cache["loop"].is_closed():
        _inverter_cache["loop"] = asyncio.new_event_loop()
        asyncio.set_event_loop(_inverter_cache["loop"])
    return _inverter_cache["loop"].run_until_complete(fetch_inverter_data())


async def fetch_all_parameters():
    """Fetch all inverter parameters."""
    try:
        async with LuxpowerClient(
            username=INVERTER_CONFIG["username"],
            password=INVERTER_CONFIG["password"],
            base_url=INVERTER_CONFIG["base_url"]
        ) as client:
            stations = await Station.load_all(client)
            if not stations:
                return {}

            inverter = None
            for inv in stations[0].all_inverters:
                inverter = inv
                break

            if not inverter:
                return {}

            params = await client.api.control.read_device_parameters_ranges(inverter.serial_number)
            return params
    except Exception as e:
        print(f"Error fetching parameters: {e}", flush=True)
        return {}


def get_all_parameters_sync():
    """Synchronous wrapper for fetching all parameters."""
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor() as executor:
        future = executor.submit(asyncio.run, fetch_all_parameters())
        return future.result(timeout=60)


# NOTE: This dashboard is read-only by design — no write_parameter or
# set_parameter_sync helpers exist. The settings modal displays current values
# but cannot modify them. Use the dedicated apply_*.py scripts (one-shot, audited)
# if you need to change inverter configuration.


# =============================================================================
# FLASK APP
# =============================================================================

app = Flask(__name__)

HTML_TEMPLATE = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Solar Dashboard - Oakland, CA</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'Segoe UI', system-ui, sans-serif;
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
            color: #fff;
            overflow: hidden;
        }
        #container { display: flex; height: 100vh; }
        #canvas-container { flex: 1; position: relative; }
        #stats-panel {
            width: 380px;
            background: rgba(0,0,0,0.7);
            backdrop-filter: blur(10px);
            padding: 20px;
            overflow-y: auto;
            border-left: 1px solid rgba(255,255,255,0.1);
        }
        h1 {
            font-size: 1.4em;
            margin-bottom: 5px;
            background: linear-gradient(90deg, #f39c12, #e74c3c);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }
        .subtitle { color: #888; font-size: 0.85em; margin-bottom: 20px; }
        .section {
            background: rgba(255,255,255,0.05);
            border-radius: 12px;
            padding: 15px;
            margin-bottom: 15px;
        }
        .section-title {
            font-size: 0.75em;
            text-transform: uppercase;
            letter-spacing: 1px;
            color: #888;
            margin-bottom: 10px;
            display: flex;
            align-items: center;
            gap: 8px;
        }
        .section-title .icon { font-size: 1.2em; }
        .stat-row {
            display: flex;
            justify-content: space-between;
            padding: 8px 0;
            border-bottom: 1px solid rgba(255,255,255,0.05);
        }
        .stat-row:last-child { border-bottom: none; }
        .stat-label { color: #aaa; font-size: 0.9em; }
        .stat-value {
            font-weight: 600;
            font-size: 1.1em;
            font-variant-numeric: tabular-nums;
        }
        .stat-value.power { color: #f39c12; }
        .stat-value.voltage { color: #3498db; }
        .stat-value.percent { color: #2ecc71; }
        .stat-value.temp { color: #e74c3c; }
        .stat-value.good { color: #2ecc71; }
        .stat-value.warning { color: #f39c12; }
        .stat-value.bad { color: #e74c3c; }
        .mppt-detail { padding: 2px 0 8px 0; }
        .mppt-detail .stat-value { font-size: 0.85em; opacity: 0.8; }
        .power-summary {
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 10px;
            padding: 0;
            background: transparent;
        }
        .power-card {
            min-width: 0;
            background: rgba(255,255,255,0.05);
            border-radius: 12px;
            padding: 14px;
        }
        .power-card .label {
            color: #888;
            font-size: 0.72em;
            text-transform: uppercase;
            letter-spacing: 0.08em;
        }
        .power-card .value {
            margin-top: 4px;
            font-size: 1.9em;
            font-weight: 700;
            line-height: 1;
            font-variant-numeric: tabular-nums;
            overflow-wrap: anywhere;
        }
        .power-card .value.pv {
            background: linear-gradient(90deg, #f39c12, #e67e22);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }
        .power-card .value.battery { color: #3498db; }
        .power-card .value.battery.charging { color: #2ecc71; }
        .power-card .value.battery.discharging { color: #3498db; }
        .power-card .value.battery.idle { color: #888; }
        .power-card .value.grid { color: #95a5a6; }
        .power-card .value.grid.exporting { color: #2ecc71; }
        .power-card .value.grid.importing { color: #e74c3c; }
        .power-card .value.grid.idle { color: #888; }
        .power-card-meta {
            display: flex;
            justify-content: space-between;
            margin-top: 5px;
            color: #888;
            font-size: 0.75em;
        }
        .power-card-summary {
            margin-top: 4px;
            font-size: 0.78em;
            color: #aaa;
            font-variant-numeric: tabular-nums;
        }
        .power-card-summary .pos { color: #2ecc71; }
        .power-card-summary .neg { color: #e74c3c; }
        .sun-row {
            display: flex;
            justify-content: center;
            gap: 14px;
            margin: -4px 0 14px 0;
            font-size: 0.85em;
            color: #aaa;
            font-variant-numeric: tabular-nums;
        }
        .sun-row-label { font-size: 1.1em; }
        .sun-row-time { letter-spacing: 0.02em; }
        .event-row {
            display: flex;
            gap: 10px;
            padding: 6px 0;
            border-bottom: 1px solid rgba(255,255,255,0.05);
            font-size: 0.85em;
        }
        .event-row:last-child { border-bottom: none; }
        .event-time {
            color: #aaa;
            min-width: 70px;
            font-variant-numeric: tabular-nums;
        }
        .event-details { flex: 1; }
        .event-reason {
            color: #e67e22;
            font-weight: 600;
        }
        .event-meta {
            color: #888;
            font-size: 0.88em;
        }
        .event-empty {
            color: #2ecc71;
            text-align: center;
            padding: 12px 0;
            font-size: 0.9em;
        }
        .performance-bar {
            height: 8px;
            background: rgba(255,255,255,0.1);
            border-radius: 4px;
            overflow: hidden;
            margin-top: 10px;
        }
        .performance-bar .fill {
            height: 100%;
            background: linear-gradient(90deg, #e74c3c, #f39c12, #2ecc71);
            transition: width 0.5s ease;
        }
        .performance-bar .fill.battery {
            background: linear-gradient(90deg, #3498db, #9b59b6);
        }
        .performance-bar .fill.grid {
            background: linear-gradient(90deg, #95a5a6, #2ecc71);
        }
        .performance-bar .fill.grid.importing {
            background: linear-gradient(90deg, #95a5a6, #e74c3c);
        }
        .mppt-zero .mppt-row,
        .mppt-zero .legend {
            display: none;
        }
        .mppt-zero-message {
            display: none;
            color: #888;
            font-size: 0.85em;
            padding: 2px 0 4px;
        }
        .mppt-zero .mppt-zero-message { display: block; }
        .sun-info {
            display: flex;
            justify-content: space-around;
            text-align: center;
        }
        .sun-info .time-block .label { font-size: 0.75em; color: #888; }
        .sun-info .time-block .time { font-size: 1.2em; font-weight: 600; }
        .last-update {
            text-align: center;
            color: #666;
            font-size: 0.75em;
            margin-top: 10px;
        }
        .settings-btn {
            position: fixed;
            bottom: 20px;
            right: 400px;
            width: 50px;
            height: 50px;
            border-radius: 50%;
            background: rgba(243, 156, 18, 0.9);
            border: none;
            cursor: pointer;
            font-size: 24px;
            box-shadow: 0 4px 15px rgba(0,0,0,0.3);
            transition: transform 0.2s, background 0.2s;
            z-index: 100;
        }
        .settings-btn:hover {
            transform: scale(1.1);
            background: rgba(243, 156, 18, 1);
        }
        .modal-overlay {
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background: rgba(0,0,0,0.8);
            z-index: 1000;
            justify-content: center;
            align-items: center;
        }
        .modal-overlay.active { display: flex; }
        .modal {
            background: #1a1a2e;
            border-radius: 4px;
            width: 100%;
            max-width: 100%;
            height: 100vh;
            max-height: 100vh;
            display: flex;
            flex-direction: column;
            box-shadow: 0 20px 60px rgba(0,0,0,0.5);
            border: 1px solid rgba(255,255,255,0.1);
        }
        .modal-header {
            padding: 8px 12px;
            border-bottom: 1px solid rgba(255,255,255,0.1);
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .modal-header h2 {
            margin: 0;
            font-size: 1.1em;
            color: #f39c12;
        }
        .modal-close {
            background: none;
            border: none;
            color: #888;
            font-size: 20px;
            cursor: pointer;
            padding: 0 5px;
        }
        .modal-close:hover { color: #fff; }
        .modal-toolbar {
            padding: 6px 12px;
            border-bottom: 1px solid rgba(255,255,255,0.1);
            display: flex;
            gap: 6px;
            flex-wrap: wrap;
            align-items: center;
        }
        .modal-toolbar input[type="text"] {
            flex: 1;
            min-width: 150px;
            padding: 5px 10px;
            border-radius: 4px;
            border: 1px solid rgba(255,255,255,0.2);
            background: rgba(255,255,255,0.05);
            color: #fff;
            font-size: 12px;
        }
        .modal-toolbar input[type="text"]:focus {
            outline: none;
            border-color: #f39c12;
        }
        .filter-btn {
            padding: 4px 8px;
            border-radius: 4px;
            border: 1px solid rgba(255,255,255,0.2);
            background: rgba(255,255,255,0.05);
            color: #aaa;
            cursor: pointer;
            font-size: 11px;
            transition: all 0.2s;
        }
        .filter-btn:hover { background: rgba(255,255,255,0.1); color: #fff; }
        .filter-btn.active { background: #f39c12; color: #000; border-color: #f39c12; }
        .modal-body {
            flex: 1;
            overflow-y: auto;
            padding: 8px;
        }
        .param-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
            gap: 3px;
        }
        .param-item {
            display: flex;
            align-items: center;
            padding: 2px 6px;
            background: rgba(255,255,255,0.02);
            border-radius: 3px;
            border: 1px solid rgba(255,255,255,0.03);
            gap: 4px;
            min-height: 22px;
        }
        .param-item:hover { background: rgba(255,255,255,0.05); }
        .param-item.modified { border-color: #f39c12; background: rgba(243, 156, 18, 0.1); }
        .param-item.saving { opacity: 0.5; }
        .param-name {
            flex: 1;
            font-size: 9px;
            font-family: monospace;
            color: #888;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        .param-name.func { color: #9b59b6; }
        .param-name.hold { color: #3498db; }
        .param-name.bit { color: #e67e22; }
        .param-input {
            width: 50px;
            padding: 2px 4px;
            border-radius: 2px;
            border: 1px solid rgba(255,255,255,0.15);
            background: rgba(0,0,0,0.3);
            color: #fff;
            font-size: 10px;
            text-align: right;
        }
        .param-input:focus { outline: none; border-color: #f39c12; }
        .param-toggle {
            position: relative;
            width: 28px;
            height: 14px;
            background: rgba(255,255,255,0.1);
            border-radius: 7px;
            cursor: pointer;
            transition: background 0.2s;
            flex-shrink: 0;
        }
        .param-toggle.on { background: #27ae60; }
        .param-toggle::after {
            content: '';
            position: absolute;
            top: 2px;
            left: 2px;
            width: 10px;
            height: 10px;
            background: #fff;
            border-radius: 50%;
            transition: transform 0.2s;
        }
        .param-toggle.on::after { transform: translateX(14px); }
        .param-save {
            padding: 1px 4px;
            border-radius: 2px;
            border: none;
            background: #27ae60;
            color: #fff;
            cursor: pointer;
            font-size: 9px;
            opacity: 0;
            transition: opacity 0.2s;
        }
        .param-item.modified .param-save { opacity: 1; }
        .param-save:hover { background: #2ecc71; }
        .modal-footer {
            padding: 6px 12px;
            border-top: 1px solid rgba(255,255,255,0.1);
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .param-count { color: #888; font-size: 11px; }
        .modal-actions { display: flex; gap: 6px; }
        .btn {
            padding: 5px 12px;
            border-radius: 4px;
            border: none;
            cursor: pointer;
            font-size: 11px;
            transition: all 0.2s;
        }
        .btn-primary { background: #f39c12; color: #000; }
        .btn-primary:hover { background: #e67e22; }
        .btn-secondary { background: rgba(255,255,255,0.1); color: #fff; }
        .btn-secondary:hover { background: rgba(255,255,255,0.2); }
        #loading {
            position: absolute;
            top: 50%;
            left: 50%;
            transform: translate(-50%, -50%);
            color: #888;
        }
        .legend {
            display: flex;
            gap: 20px;
            justify-content: center;
            margin-top: 10px;
            font-size: 0.8em;
        }
        .legend-item {
            display: flex;
            align-items: center;
            gap: 5px;
        }
        .legend-color {
            width: 12px;
            height: 12px;
            border-radius: 2px;
        }
        .legend-color.sw { background: #3498db; }
        .legend-color.ne { background: #9b59b6; }
        .legend-color.yard { background: #27ae60; }

        /* Inline color dots next to MPPT labels */
        .mppt-dot {
            display: inline-block;
            width: 10px;
            height: 10px;
            border-radius: 50%;
            margin-right: 6px;
            vertical-align: middle;
        }
        .mppt-dot.sw { background: #3498db; }
        .mppt-dot.ne { background: #9b59b6; }
        .mppt-dot.yard { background: #27ae60; }
    </style>
</head>
<body>
    <div id="container">
        <div id="canvas-container">
            <div id="loading">Loading 3D scene...</div>
        </div>
        <div id="stats-panel">
            <h1>Solar Dashboard</h1>
            <div class="subtitle">Oakland, CA - Live System Monitor</div>

            <div class="section power-summary">
                <div class="power-card">
                    <div class="label">PV Power</div>
                    <div class="value pv" id="total-pv">--</div>
                    <div class="performance-bar">
                        <div class="fill" id="pv-load-fill" style="width: 0%"></div>
                    </div>
                    <div class="power-card-meta">
                        <span id="pv-load-text">--% of 12 kW</span>
                        <span>12 kW max</span>
                    </div>
                    <div class="power-card-summary" id="pv-summary">today: -- kWh</div>
                </div>
                <div class="power-card">
                    <div class="label">Battery Power</div>
                    <div class="value battery idle" id="battery-top-power">--</div>
                    <div class="performance-bar">
                        <div class="fill battery" id="battery-load-fill" style="width: 0%"></div>
                    </div>
                    <div class="power-card-meta">
                        <span id="battery-load-text">--% of 12 kW</span>
                        <span>12 kW max</span>
                    </div>
                    <div class="power-card-summary" id="battery-summary">today: ↓-- / ↑-- kWh</div>
                </div>
                <div class="power-card">
                    <div class="label">Grid Power</div>
                    <div class="value grid idle" id="grid-top-power">--</div>
                    <div class="performance-bar">
                        <div class="fill grid" id="grid-load-fill" style="width: 0%"></div>
                    </div>
                    <div class="power-card-meta">
                        <span id="grid-load-text">--% of 12 kW</span>
                        <span>12 kW max</span>
                    </div>
                    <div class="power-card-summary" id="grid-summary">today: +-- / --- kWh</div>
                </div>
            </div>

            <div class="section">
                <div class="section-title"><span class="icon">⚠️</span> Today's Import Events</div>
                <div id="import-events-list">
                    <div class="event-empty">No imports today ✓</div>
                </div>
            </div>

            <div class="sun-row">
                <span class="sun-row-label">☀</span>
                <span class="sun-row-time">↑ <span id="sunrise">--:--</span></span>
                <span class="sun-row-time">↓ <span id="sunset">--:--</span></span>
            </div>
            <!-- Hidden — these change but are only useful for the 3D animation,
                 not as readable stats. Kept in DOM so updateSunPosition / data
                 fetch code doesn't have to special-case them. -->
            <span id="sun-altitude" style="display:none">--°</span>
            <span id="sun-azimuth" style="display:none">--°</span>

            <div class="section" id="pv-arrays-section">
                <div class="section-title"><span class="icon">⚡</span> PV Arrays</div>
                <div class="mppt-zero-message">All MPPT inputs idle (0 W)</div>
                <div class="stat-row mppt-row">
                    <span class="stat-label"><span class="mppt-dot sw"></span> MPPT 1 (SW)</span>
                    <span class="stat-value power" id="pv1">-- W</span>
                </div>
                <div class="stat-row mppt-row mppt-detail">
                    <span class="stat-label"></span>
                    <span class="stat-value voltage" id="pv1-detail">-- V / -- A</span>
                </div>
                <div class="stat-row mppt-row">
                    <span class="stat-label"><span class="mppt-dot ne"></span> MPPT 2 (NE)</span>
                    <span class="stat-value power" id="pv2">-- W</span>
                </div>
                <div class="stat-row mppt-row mppt-detail">
                    <span class="stat-label"></span>
                    <span class="stat-value voltage" id="pv2-detail">-- V / -- A</span>
                </div>
                <div class="stat-row mppt-row">
                    <span class="stat-label"><span class="mppt-dot yard"></span> MPPT 3 (Mixed)</span>
                    <span class="stat-value power" id="pv3">-- W</span>
                </div>
                <div class="stat-row mppt-row mppt-detail">
                    <span class="stat-label"></span>
                    <span class="stat-value voltage" id="pv3-detail">-- V / -- A</span>
                </div>
                <div class="legend">
                    <div class="legend-item"><div class="legend-color ne"></div> <span id="legend-ne">NE Roof</span></div>
                    <div class="legend-item"><div class="legend-color sw"></div> <span id="legend-sw">SW Roof</span></div>
                    <div class="legend-item"><div class="legend-color yard"></div> <span id="legend-yard">Older Mixed</span></div>
                </div>
            </div>

            <div class="section">
                <div class="section-title"><span class="icon">🔋</span> Battery</div>
                <div class="stat-row">
                    <span class="stat-label">State of Charge</span>
                    <span class="stat-value percent" id="battery-soc">-- %</span>
                </div>
                <div class="stat-row">
                    <span class="stat-label">Voltage</span>
                    <span class="stat-value voltage" id="battery-voltage">-- V</span>
                </div>
                <div class="stat-row">
                    <span class="stat-label">Power</span>
                    <span class="stat-value power" id="battery-power">-- W</span>
                </div>
                <div class="stat-row">
                    <span class="stat-label">Temperature</span>
                    <span class="stat-value temp" id="battery-temp">-- °C</span>
                </div>
            </div>

            <div class="section">
                <div class="section-title"><span class="icon">🔌</span> Grid</div>
                <div class="stat-row">
                    <span class="stat-label">Export to Grid</span>
                    <span class="stat-value power" id="grid-export">-- W</span>
                </div>
                <div class="stat-row">
                    <span class="stat-label">To Home</span>
                    <span class="stat-value power" id="grid-import">-- W</span>
                </div>
                <div class="stat-row">
                    <span class="stat-label">Voltage</span>
                    <span class="stat-value voltage" id="grid-voltage">-- V</span>
                </div>
                <div class="stat-row">
                    <span class="stat-label">Frequency</span>
                    <span class="stat-value" id="grid-freq">-- Hz</span>
                </div>
            </div>

            <div class="section">
                <div class="section-title"><span class="icon">🌡️</span> Inverter</div>
                <div class="stat-row">
                    <span class="stat-label">Output Power</span>
                    <span class="stat-value power" id="inverter-power">-- W</span>
                </div>
                <div class="stat-row">
                    <span class="stat-label">Temperature</span>
                    <span class="stat-value temp" id="inverter-temp">-- °C</span>
                </div>
                <div class="stat-row">
                    <span class="stat-label">Status</span>
                    <span class="stat-value good" id="inverter-status">--</span>
                </div>
                <div class="stat-row">
                    <span class="stat-label">BMS Max Charge</span>
                    <span class="stat-value" id="bms-max-charge">-- A</span>
                </div>
                <div class="stat-row">
                    <span class="stat-label">BMS Max Discharge</span>
                    <span class="stat-value" id="bms-max-discharge">-- A</span>
                </div>
            </div>

            <div class="section">
                <div class="section-title"><span class="icon">📊</span> Today's Energy</div>
                <div class="stat-row">
                    <span class="stat-label">PV Yield</span>
                    <span class="stat-value good" id="energy-yield">-- kWh</span>
                </div>
                <div class="stat-row">
                    <span class="stat-label">Exported to Grid</span>
                    <span class="stat-value good" id="energy-export">-- kWh</span>
                </div>
                <div class="stat-row">
                    <span class="stat-label">Imported from Grid</span>
                    <span class="stat-value bad" id="energy-import">-- kWh</span>
                </div>
                <div class="stat-row">
                    <span class="stat-label">Home Usage</span>
                    <span class="stat-value" id="energy-usage">-- kWh</span>
                </div>
                <div class="stat-row">
                    <span class="stat-label">Battery Charged</span>
                    <span class="stat-value" id="energy-charge">-- kWh</span>
                </div>
                <div class="stat-row">
                    <span class="stat-label">Battery Discharged</span>
                    <span class="stat-value" id="energy-discharge">-- kWh</span>
                </div>
            </div>

            <div class="section">
                <div class="section-title"><span class="icon">🕒</span> TOU & Mode</div>
                <div class="stat-row">
                    <span class="stat-label">Mode</span>
                    <span class="stat-value" id="op-mode">--</span>
                </div>
                <div class="stat-row">
                    <span class="stat-label">PG&amp;E Period</span>
                    <span class="stat-value" id="tou-period">--</span>
                </div>
                <div class="stat-row">
                    <span class="stat-label">Ava Bonus Window</span>
                    <span class="stat-value" id="ava-bonus">--</span>
                </div>
                <div class="stat-row">
                    <span class="stat-label">Forced Discharge</span>
                    <span class="stat-value" id="forced-discharge-status">--</span>
                </div>
                <div class="stat-row">
                    <span class="stat-label">Schedule</span>
                    <span class="stat-value" id="schedule-window">16:00 – 21:00</span>
                </div>
                <div class="stat-row">
                    <span class="stat-label">Discharge Floor</span>
                    <span class="stat-value" id="discharge-floor">40 %</span>
                </div>
                <div class="stat-row">
                    <span class="stat-label">Export rate now</span>
                    <span class="stat-value" id="export-rate-now">--</span>
                </div>
                <div class="stat-row">
                    <span class="stat-label">Import rate now</span>
                    <span class="stat-value" id="import-rate-now">--</span>
                </div>
                <div class="stat-row">
                    <span class="stat-label">$/hr (estimate)</span>
                    <span class="stat-value" id="dollars-per-hour">--</span>
                </div>
            </div>

            <div class="last-update">Last update: <span id="last-update">--</span> · <span style="color:#e67e22;font-weight:600;">READ-ONLY</span></div>
        </div>
    </div>

    <button class="settings-btn" onclick="openSettings()" title="Inverter Settings">&#9881;</button>

    <div class="modal-overlay" id="settings-modal">
        <div class="modal">
            <div class="modal-header">
                <h2>Inverter Parameters <span style="font-size:0.55em;background:#e67e22;color:#fff;padding:3px 8px;border-radius:4px;margin-left:10px;vertical-align:middle;">READ-ONLY</span></h2>
                <button class="modal-close" onclick="closeSettings()">&times;</button>
            </div>
            <div class="modal-toolbar">
                <input type="text" id="param-search" placeholder="Search parameters..." oninput="filterParams()">
                <button class="filter-btn active" data-filter="all" onclick="setFilter('all')">All</button>
                <button class="filter-btn" data-filter="FUNC_" onclick="setFilter('FUNC_')">FUNC_</button>
                <button class="filter-btn" data-filter="HOLD_" onclick="setFilter('HOLD_')">HOLD_</button>
                <button class="filter-btn" data-filter="BIT_" onclick="setFilter('BIT_')">BIT_</button>
            </div>
            <div class="modal-body">
                <div class="param-grid" id="param-grid">
                    <div style="text-align:center;color:#888;padding:40px;">Loading parameters...</div>
                </div>
            </div>
            <div class="modal-footer">
                <span class="param-count" id="param-count">0 parameters</span>
                <div class="modal-actions">
                    <button class="btn btn-secondary" onclick="refreshParams()">Refresh</button>
                    <button class="btn btn-secondary" onclick="closeSettings()">Close</button>
                </div>
            </div>
        </div>
    </div>

    <script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/three@0.128.0/examples/js/controls/OrbitControls.js"></script>
    <script>
        // Configuration injected from Python
        const CONFIG = {{ config_json | safe }};

        // Three.js Scene Setup - Optimized for performance
        let scene, camera, renderer, sun, sunLight, controls;
        let roofSW, roofNE, backyardArray;
        let mixedSW, mixedNE;        // MPPT3: split-roof overlay panels (older array)
        let swPowerLabel, nePowerLabel, yardPowerLabel, gridExportLabel;
        let mixedPowerLabel;         // MPPT3 wattage label (floats above the older-panel zone)
        let batteryStatusLabel, batteryPowerLabel;
        let batteryWire;
        let homeWire;
        let homePowerLabel, inverterStatusLabel;
        let gridWires = [];
        let sunData = { altitude: 0, azimuth: 180 };
        let sunArc = null;  // Store sun arc for updates
        let sunArcInitialized = false;
        let animationId;
        let lastRenderTime = 0;
        const TARGET_FPS = 30; // Limit to 30fps
        const FRAME_TIME = 1000 / TARGET_FPS;

        const SUN_DISTANCE = 80;  // Far enough to look realistic with 30ft tall house

        // Sun ray particles
        let sunRayParticles;
        let sunRayPositions;
        let sunRayVelocities;
        const NUM_SUN_RAYS = 60;
        let sunRayDirection = new THREE.Vector3(0, -1, 0);

        const CAMERA_STORAGE_KEY = 'solarDashboardCameraPosition';
        let cameraSaveTimeout = null;

        function saveCameraPosition() {
            // Debounce localStorage writes to avoid excessive writes
            if (cameraSaveTimeout) clearTimeout(cameraSaveTimeout);
            cameraSaveTimeout = setTimeout(() => {
                if (!camera || !controls) return;
                const cameraState = {
                    position: {
                        x: camera.position.x,
                        y: camera.position.y,
                        z: camera.position.z
                    },
                    target: {
                        x: controls.target.x,
                        y: controls.target.y,
                        z: controls.target.z
                    }
                };
                try {
                    localStorage.setItem(CAMERA_STORAGE_KEY, JSON.stringify(cameraState));
                } catch (e) {
                    console.warn('Failed to save camera position:', e);
                }
            }, 500);  // Wait 500ms after last interaction before saving
        }

        function loadCameraPosition() {
            try {
                const saved = localStorage.getItem(CAMERA_STORAGE_KEY);
                if (saved) {
                    const state = JSON.parse(saved);
                    if (state.position && state.target) {
                        camera.position.set(state.position.x, state.position.y, state.position.z);
                        controls.target.set(state.target.x, state.target.y, state.target.z);
                        controls.update();
                    }
                }
            } catch (e) {
                console.warn('Failed to load camera position:', e);
            }
        }

        function init() {
            const container = document.getElementById('canvas-container');
            const loading = document.getElementById('loading');

            // Wrap all 3D setup in try/catch — some kiosk-mode browsers (e.g.
            // luakit/WebKitGTK without GL) can't create a WebGL context.
            // In that case we still want the data-polling loop and side panel
            // to work; only the 3D visualization is sacrificed.
            try {
            // Scene
            scene = new THREE.Scene();
            scene.background = new THREE.Color(0x1a1a2e);

            // Camera - fixed position, no rotation
            camera = new THREE.PerspectiveCamera(
                50,
                container.clientWidth / container.clientHeight,
                1,
                200
            );
            // Camera positioned to view house from NW, looking SE
            // Position relative to house center for good default view
            const defaultHouseX = CONFIG.house.position_ft[0] / CONFIG.sceneScale;
            const defaultHouseZ = CONFIG.house.position_ft[1] / CONFIG.sceneScale;
            camera.position.set(defaultHouseX - 15, 30, defaultHouseZ - 40);
            camera.lookAt(defaultHouseX, 5, defaultHouseZ);

            // Renderer - disable antialiasing for performance
            renderer = new THREE.WebGLRenderer({ antialias: false });
            renderer.setSize(container.clientWidth, container.clientHeight);
            renderer.setPixelRatio(Math.min(window.devicePixelRatio, 1.5)); // Limit pixel ratio
            container.appendChild(renderer.domElement);
            loading.style.display = 'none';

            // OrbitControls for click-and-drag rotation
            controls = new THREE.OrbitControls(camera, renderer.domElement);
            controls.enableDamping = true;
            controls.dampingFactor = 0.05;
            controls.target.set(defaultHouseX, 5, defaultHouseZ);
            controls.minDistance = 15;
            controls.maxDistance = 80;
            controls.maxPolarAngle = Math.PI / 2.1; // Don't go below ground

            // Load saved camera position from localStorage
            loadCameraPosition();

            // Save camera position when user stops interacting
            controls.addEventListener('end', saveCameraPosition);

            // Simple ambient light
            const ambient = new THREE.AmbientLight(0x6688aa, 0.6);
            scene.add(ambient);

            // Sun light - no shadows for performance
            sunLight = new THREE.DirectionalLight(0xffffee, 1.0);
            scene.add(sunLight);

            // Sun sphere - simple geometry
            const sunGeometry = new THREE.SphereGeometry(2, 16, 16);
            const sunMaterial = new THREE.MeshBasicMaterial({ color: 0xffdd44 });
            sun = new THREE.Mesh(sunGeometry, sunMaterial);
            scene.add(sun);

            // Sun ray lines - streaming from sun to roof (each ray = 2 points for a line segment)
            const rayGeometry = new THREE.BufferGeometry();
            sunRayPositions = new Float32Array(NUM_SUN_RAYS * 6); // 2 points per ray, 3 coords each
            sunRayVelocities = [];

            // Initialize all rays at origin - will be positioned properly on first sun update
            for (let i = 0; i < NUM_SUN_RAYS; i++) {
                // Start point
                sunRayPositions[i * 6] = 0;
                sunRayPositions[i * 6 + 1] = 50; // High up, will be set by sun position
                sunRayPositions[i * 6 + 2] = 0;
                // End point (slightly behind start to create line)
                sunRayPositions[i * 6 + 3] = 0;
                sunRayPositions[i * 6 + 4] = 52;
                sunRayPositions[i * 6 + 5] = 0;

                sunRayVelocities.push({
                    speed: 0.2 + Math.random() * 0.15,
                    offsetX: (Math.random() - 0.5) * 8, // Spread around sun
                    offsetZ: (Math.random() - 0.5) * 8,
                    t: Math.random() // Random start position along path
                });
            }

            rayGeometry.setAttribute('position', new THREE.BufferAttribute(sunRayPositions, 3));

            const rayMaterial = new THREE.LineBasicMaterial({
                color: 0xffee66,
                transparent: true,
                opacity: 0.4,
                blending: THREE.AdditiveBlending
            });

            sunRayParticles = new THREE.LineSegments(rayGeometry, rayMaterial);
            sunRayParticles.visible = false; // Hidden until sun is up
            scene.add(sunRayParticles);

            // Ground - simple plane
            const groundGeometry = new THREE.PlaneGeometry(80, 80);
            const groundMaterial = new THREE.MeshLambertMaterial({ color: 0x3d6b35 });
            const ground = new THREE.Mesh(groundGeometry, groundMaterial);
            ground.rotation.x = -Math.PI / 2;
            ground.position.y = 0;
            scene.add(ground);

            // Simple grid centered on house
            const gridHelper = new THREE.GridHelper(80, 20, 0x444444, 0x333333);
            gridHelper.position.set(ftToUnits(CONFIG.house.position_ft[0]), 0, ftToUnits(CONFIG.house.position_ft[1]));
            scene.add(gridHelper);

            // Create road first (scene reference)
            createRoad();

            // Create house with proper roof orientation
            createHouse();

            // Compass markers
            createCompass();

            // Sun arc indicator (shows sun path across southern sky)
            createSunArc();

            // Handle resize
            window.addEventListener('resize', onWindowResize);

            // Start animation loop (throttled)
            animate();
            } catch (e) {
                // 3D context unavailable (e.g. kiosk browser without WebGL).
                // Hide the canvas + loading text; the side stats panel still works.
                console.error('3D scene init failed (continuing in data-only mode):', e);
                const canvasContainer = document.getElementById('canvas-container');
                if (canvasContainer) {
                    canvasContainer.style.display = 'none';
                }
                // Expand the stats panel to the full window since the 3D pane is gone
                const stats = document.getElementById('stats-panel');
                if (stats) {
                    stats.style.width = '100%';
                    stats.style.maxWidth = '100%';
                }
            }

            // The following always runs, even if 3D init failed above
            try { updateLegendFromConfig(); } catch (e) { /* ignore */ }

            // Start data polling — this is what makes the side panel show numbers
            fetchData();
            setInterval(fetchData, 5000);
            // Events poll separately (slower — events change at minute-scale)
            fetchEvents();
            setInterval(fetchEvents, 60000);
        }

        async function fetchEvents() {
            try {
                const r = await fetch('/api/events');
                const data = await r.json();
                const listEl = document.getElementById('import-events-list');
                if (!listEl) return;
                if (!data.events || data.events.length === 0) {
                    listEl.innerHTML = '<div class="event-empty">No imports today ✓</div>';
                    return;
                }
                // Render newest first
                listEl.innerHTML = data.events.slice().reverse().map(ev => {
                    const timeStr = ev.duration_min > 5
                        ? `${ev.start}–${ev.end}` : ev.start;
                    const socStr = ev.soc != null ? ` · SOC ${ev.soc}%` : '';
                    const pvStr = ev.ppv != null && ev.ppv > 100 ? ` · PV ${(ev.ppv/1000).toFixed(1)}kW` : '';
                    return `<div class="event-row">
                        <div class="event-time">${timeStr}</div>
                        <div class="event-details">
                            <div class="event-reason">${ev.reason}</div>
                            <div class="event-meta">~${ev.kwh.toFixed(3)} kWh · peak ${ev.peak_w}W · ${ev.duration_min.toFixed(0)}min${socStr}${pvStr}</div>
                        </div>
                    </div>`;
                }).join('');
            } catch (err) {
                console.error('events fetch failed:', err);
            }
        }

        function updateLegendFromConfig() {
            const swArray = getArrayConfig('SW') || CONFIG.arrays[1];
            const neArray = getArrayConfig('NE') || CONFIG.arrays[0];
            const mixedArray = getArrayConfig('Older') || getArrayConfig('Mixed') || CONFIG.arrays[2];

            // Format azimuth(s): single value or list (split arrays)
            const fmtAz = (a) => Array.isArray(a) ? a.map(v => v + '°').join(' / ') : a + '°';

            document.getElementById('legend-sw').textContent = `SW Roof (${fmtAz(swArray.azimuth)})`;
            document.getElementById('legend-ne').textContent = `NE Roof (${fmtAz(neArray.azimuth)})`;
            document.getElementById('legend-yard').textContent = `Older Mixed (${fmtAz(mixedArray.azimuth)})`;
        }

        function createRoad() {
            // Road runs NE to SW (diagonal, parallel to NE roof face)
            // In Three.js: +X = East, +Z = South, -Z = North
            // Azimuth 0° = North (-Z), increases clockwise
            const neArray = getArrayConfig('NE') || CONFIG.arrays[0];
            const roadAzimuth = neArray.azimuth;  // Road parallel to NE roof
            const roadRad = roadAzimuth * Math.PI / 180;

            const roadGeometry = new THREE.PlaneGeometry(8, 70);
            const roadMaterial = new THREE.MeshLambertMaterial({ color: 0x333333 });
            const road = new THREE.Mesh(roadGeometry, roadMaterial);
            road.rotation.x = -Math.PI / 2;
            road.rotation.z = -roadRad;  // Rotate to run NE-SW diagonal
            road.position.set(18, 0.05, -8);
            scene.add(road);

            // Road center line (yellow dashed)
            const lineGeometry = new THREE.PlaneGeometry(0.3, 65);
            const lineMaterial = new THREE.MeshLambertMaterial({ color: 0xffcc00 });
            const centerLine = new THREE.Mesh(lineGeometry, lineMaterial);
            centerLine.rotation.x = -Math.PI / 2;
            centerLine.rotation.z = -roadRad;  // Same rotation as road
            centerLine.position.set(18, 0.06, -8);
            scene.add(centerLine);

            // Road label "Harrison St" at NE end of road
            const roadLabel = createPowerLabel('Harrison St');
            roadLabel.scale.set(6, 3, 1);
            // NE end: move along road direction
            const labelDist = 25;
            const labelX = 18 + labelDist * Math.sin(roadRad);
            const labelZ = -8 - labelDist * Math.cos(roadRad);
            roadLabel.position.set(labelX, 0.5, labelZ);
            scene.add(roadLabel);
        }

        // Helper to find array config by name pattern
        function getArrayConfig(pattern) {
            return CONFIG.arrays.find(a => a.name.toLowerCase().includes(pattern.toLowerCase()));
        }

        // Helper to convert feet to 3D units
        function ftToUnits(ft) {
            return ft / CONFIG.sceneScale;
        }

        function createHouse() {
            // Get configurations from CONFIG
            const house = CONFIG.house;
            const swArray = getArrayConfig('SW');
            const neArray = getArrayConfig('NE');

            // COORDINATE SYSTEM:
            // +X = East, -X = West, +Z = South, -Z = North
            // Azimuth 0° = North (-Z), increases clockwise

            // Convert house dimensions from feet to 3D units
            const houseLength = ftToUnits(house.length_ft);
            const houseWidth = ftToUnits(house.width_ft);
            const houseHeight = ftToUnits(house.height_ft);
            const houseCenterX = ftToUnits(house.position_ft[0]);
            const houseCenterZ = ftToUnits(house.position_ft[1]);

            // Ridgeline and roof tilt from pre-computed config
            // BoxGeometry long axis is along +X (East = 90° azimuth)
            // To rotate ridgeline to 127°, rotate by (127 - 90) = 37°
            const ridgeAzimuth = CONFIG.ridgelineAzimuth;  // 127°
            const ridgeRotation = (ridgeAzimuth - 90) * Math.PI / 180;  // Amount to rotate from +X
            const tiltAngle = CONFIG.roofTilt * Math.PI / 180;

            // House base
            const houseGeometry = new THREE.BoxGeometry(houseLength, houseHeight, houseWidth);
            const houseMaterial = new THREE.MeshLambertMaterial({ color: 0x8b7355 });
            const houseMesh = new THREE.Mesh(houseGeometry, houseMaterial);
            houseMesh.position.set(houseCenterX, houseHeight / 2, houseCenterZ);
            houseMesh.rotation.y = -ridgeRotation;  // Rotate clockwise from +X toward 127°
            scene.add(houseMesh);

            // Roof calculations
            const roofRun = houseWidth / 2;
            const roofRise = roofRun * Math.tan(tiltAngle);
            const roofSlopeLength = roofRun / Math.cos(tiltAngle);
            const ridgeHeight = houseHeight + roofRise;

            // Create roof group centered at house position
            const roofGroup = new THREE.Group();
            roofGroup.position.set(houseCenterX, 0, houseCenterZ);
            roofGroup.rotation.y = -ridgeRotation;

            // SW facing roof (at local +Z, which becomes SW after rotation)
            const roofSWGeometry = new THREE.PlaneGeometry(houseLength, roofSlopeLength);
            const roofSWMaterial = new THREE.MeshLambertMaterial({
                color: swArray ? swArray.color : 0x3498db,
                side: THREE.DoubleSide
            });
            roofSW = new THREE.Mesh(roofSWGeometry, roofSWMaterial);
            roofSW.rotation.x = -(Math.PI / 2 - tiltAngle);
            roofSW.position.set(0, houseHeight + roofRise / 2, roofRun / 2);
            roofGroup.add(roofSW);

            // NE facing roof (at local -Z, which becomes NE after rotation)
            const roofNEGeometry = new THREE.PlaneGeometry(houseLength, roofSlopeLength);
            const roofNEMaterial = new THREE.MeshLambertMaterial({
                color: neArray ? neArray.color : 0x9b59b6,
                side: THREE.DoubleSide
            });
            roofNE = new THREE.Mesh(roofNEGeometry, roofNEMaterial);
            roofNE.rotation.x = Math.PI / 2 - tiltAngle;
            roofNE.position.set(0, houseHeight + roofRise / 2, -roofRun / 2);
            roofGroup.add(roofNE);

            // Ridge cap
            const ridgeGeometry = new THREE.BoxGeometry(houseLength, 0.3, 0.4);
            const ridgeMaterial = new THREE.MeshLambertMaterial({ color: 0x555555 });
            const ridge = new THREE.Mesh(ridgeGeometry, ridgeMaterial);
            ridge.position.set(0, ridgeHeight, 0);
            roofGroup.add(ridge);

            scene.add(roofGroup);

            // Discrete panel rendering — MPPT1/MPPT2 panels live in the LEFT 70%
            // of their roof; the RIGHT 30% is reserved for the older MPPT3 string.
            const swLayout = swArray && swArray.panel_layout ? swArray.panel_layout : [7, 2];
            const neLayout = neArray && neArray.panel_layout ? neArray.panel_layout : [7, 2];
            const swAccent = swArray ? swArray.color : 0x3498db;
            const neAccent = neArray ? neArray.color : 0x9b59b6;
            const MPPT12_REGION = [0, 0.70];   // left 70% of each roof face
            addPanels(roofSW, swAccent, swLayout[0], swLayout[1], 0.1, 0.94, MPPT12_REGION);
            addPanels(roofNE, neAccent, neLayout[0], neLayout[1], -0.1, 0.94, MPPT12_REGION);

            // Calculate world positions for power labels
            const cosRidge = Math.cos(-ridgeRotation);
            const sinRidge = Math.sin(-ridgeRotation);

            // SW label position (local +Z becomes SW after rotation)
            const swLocalZ = roofRun / 2 + 1;
            const swWorldX = houseCenterX + swLocalZ * sinRidge;
            const swWorldZ = houseCenterZ + swLocalZ * cosRidge;
            swPowerLabel = createPowerLabel('0W');
            swPowerLabel.position.set(swWorldX, ridgeHeight + 2, swWorldZ);
            scene.add(swPowerLabel);

            // NE label position (local -Z becomes NE after rotation)
            const neLocalZ = -roofRun / 2 - 1;
            const neWorldX = houseCenterX + neLocalZ * sinRidge;
            const neWorldZ = houseCenterZ + neLocalZ * cosRidge;
            nePowerLabel = createPowerLabel('0W');
            nePowerLabel.position.set(neWorldX, ridgeHeight + 2, neWorldZ);
            scene.add(nePowerLabel);

            // ==== MPPT3 (older mixed array) - overlay on one end of both faces ====
            // The older panels are physically present at the +X end of the house,
            // distributed across BOTH NE and SW roof faces (5 panels per side).
            const mixedArr = getArrayConfig('Older') || getArrayConfig('Mixed') || CONFIG.arrays[2];
            const mixedColor = mixedArr ? mixedArr.color : 0x27ae60;
            const mixedLayout = mixedArr && mixedArr.panel_layout ? mixedArr.panel_layout : [5, 1];

            // Take ~25% of the ridgeline length at the far +X end of the roof
            const mixedLength = houseLength * 0.25;
            const mixedSlope = roofSlopeLength * 0.85;
            const mixedX = houseLength / 2 - mixedLength / 2 - 0.3;
            const Y_OFFSET = 0.05;  // tiny lift to avoid z-fighting with main roof

            // SW face overlay
            const mixedSWGeom = new THREE.PlaneGeometry(mixedLength, mixedSlope);
            const mixedSWMat = new THREE.MeshLambertMaterial({
                color: mixedColor, side: THREE.DoubleSide
            });
            mixedSW = new THREE.Mesh(mixedSWGeom, mixedSWMat);
            mixedSW.rotation.x = -(Math.PI / 2 - tiltAngle);
            mixedSW.position.set(mixedX, houseHeight + roofRise / 2 + Y_OFFSET, roofRun / 2);
            roofGroup.add(mixedSW);
            addPanels(mixedSW, mixedColor, mixedLayout[0], mixedLayout[1], 0.1);

            // NE face overlay
            const mixedNEGeom = new THREE.PlaneGeometry(mixedLength, mixedSlope);
            const mixedNEMat = new THREE.MeshLambertMaterial({
                color: mixedColor, side: THREE.DoubleSide
            });
            mixedNE = new THREE.Mesh(mixedNEGeom, mixedNEMat);
            mixedNE.rotation.x = Math.PI / 2 - tiltAngle;
            mixedNE.position.set(mixedX, houseHeight + roofRise / 2 + Y_OFFSET, -roofRun / 2);
            roofGroup.add(mixedNE);
            addPanels(mixedNE, mixedColor, mixedLayout[0], mixedLayout[1], -0.1);

            // Floating wattage label, hovering above the ridge over the MPPT3 zone
            // (single label, since MPPT3 reports one combined wattage)
            const mixedWorldX = houseCenterX + mixedX * cosRidge;
            const mixedWorldZ = houseCenterZ - mixedX * sinRidge;
            mixedPowerLabel = createPowerLabel('0W');
            mixedPowerLabel.position.set(mixedWorldX, ridgeHeight + 3.5, mixedWorldZ);
            scene.add(mixedPowerLabel);

            // Create ground-mounted arrays from config
            createGroundArrays();

            // Create grid service drop
            createGridService();
        }

        function createGroundArrays() {
            // Find all ground-mounted arrays in config
            const groundArrays = CONFIG.arrays.filter(a => a.type === 'ground');

            groundArrays.forEach(arr => {
                // Get dimensions from config (with defaults)
                const width = arr.size_ft ? ftToUnits(arr.size_ft[0]) : 6;
                const depth = arr.size_ft ? ftToUnits(arr.size_ft[1]) : 4;
                const posX = arr.position_ft ? ftToUnits(arr.position_ft[0]) : 0;
                const posZ = arr.position_ft ? ftToUnits(arr.position_ft[1]) : 0;
                const height = arr.height_ft ? ftToUnits(arr.height_ft) : 2.5;

                const geometry = new THREE.PlaneGeometry(width, depth);
                const material = new THREE.MeshLambertMaterial({
                    color: arr.color || 0x27ae60,
                    side: THREE.DoubleSide
                });
                const arrayMesh = new THREE.Mesh(geometry, material);

                // Position and orient
                arrayMesh.position.set(posX, height, posZ);
                arrayMesh.rotation.order = 'YXZ';
                arrayMesh.rotation.y = arr.azimuth * Math.PI / 180;
                arrayMesh.rotation.x = arr.tilt * Math.PI / 180;
                scene.add(arrayMesh);

                // Add panel grid (darken color for grid lines)
                const gridColor = (arr.color & 0xfefefe) >> 1;
                addPanelGrid(arrayMesh, gridColor, Math.round(width), Math.round(depth), 0.1);

                // Store reference for backyard array (for compatibility)
                if (arr.name.toLowerCase().includes('backyard')) {
                    backyardArray = arrayMesh;

                    // Add power label
                    yardPowerLabel = createPowerLabel('0W');
                    yardPowerLabel.position.set(0, 2, 1);
                    backyardArray.add(yardPowerLabel);
                }
            });
        }

        function createPowerLabel(text) {
            const canvas = document.createElement('canvas');
            canvas.width = 256;
            canvas.height = 128;
            const ctx = canvas.getContext('2d');

            const texture = new THREE.CanvasTexture(canvas);
            const material = new THREE.SpriteMaterial({ map: texture, transparent: true });
            const sprite = new THREE.Sprite(material);
            sprite.scale.set(4, 2, 1);
            sprite.userData = { canvas, ctx, texture };
            updatePowerLabel(sprite, text);
            return sprite;
        }

        function updatePowerLabel(sprite, text) {
            const { canvas, ctx, texture } = sprite.userData;
            ctx.clearRect(0, 0, canvas.width, canvas.height);

            // Background
            ctx.fillStyle = 'rgba(0, 0, 0, 0.7)';
            ctx.roundRect(10, 20, canvas.width - 20, canvas.height - 40, 10);
            ctx.fill();

            // Text
            ctx.fillStyle = '#f39c12';
            ctx.font = 'bold 48px Arial';
            ctx.textAlign = 'center';
            ctx.textBaseline = 'middle';
            ctx.fillText(text, canvas.width / 2, canvas.height / 2);

            texture.needsUpdate = true;
        }

        function formatPower(watts) {
            return watts.toLocaleString() + 'W';
        }

        const POWER_LOAD_MAX_W = 12000;

        function numericPower(watts) {
            const value = Number(watts);
            return Number.isFinite(value) ? value : 0;
        }

        function formatSignedPower(watts) {
            const value = numericPower(watts);
            return (value > 0 ? '+' : '') + value.toLocaleString() + 'W';
        }

        function updatePowerLoadCard(kind, watts) {
            const value = numericPower(watts);
            const loadPercent = Math.min(100, Math.abs(value) / POWER_LOAD_MAX_W * 100);
            const valueElId = (kind === 'battery') ? 'battery-top-power'
                            : (kind === 'grid')    ? 'grid-top-power'
                            : 'total-pv';
            const valueEl = document.getElementById(valueElId);
            const fillEl = document.getElementById(kind + '-load-fill');
            const textEl = document.getElementById(kind + '-load-text');

            // PV is always positive (no signed value); battery + grid show +/-
            const signed = (kind === 'battery' || kind === 'grid');
            valueEl.textContent = signed ? formatSignedPower(value) : formatPower(value);
            fillEl.style.width = loadPercent.toFixed(1) + '%';
            textEl.textContent = loadPercent.toFixed(0) + '% of 12 kW';

            if (kind === 'battery') {
                valueEl.classList.toggle('charging', value > 0);
                valueEl.classList.toggle('discharging', value < 0);
                valueEl.classList.toggle('idle', value === 0);
            } else if (kind === 'grid') {
                // Positive = exporting (good, money in), negative = importing (bad, money out)
                valueEl.classList.toggle('exporting', value > 0);
                valueEl.classList.toggle('importing', value < 0);
                valueEl.classList.toggle('idle', value === 0);
                fillEl.classList.toggle('importing', value < 0);
            }
        }

        function createGridService() {
            // SERVICE DROP ON SOUTHERN CORNER
            // Calculate positions relative to house from config
            const house = CONFIG.house;
            const houseCenterX = ftToUnits(house.position_ft[0]);
            const houseCenterZ = ftToUnits(house.position_ft[1]);
            const houseLength = ftToUnits(house.length_ft);
            const houseWidth = ftToUnits(house.width_ft);

            // House rotation (ridgeline at 127° azimuth)
            const ridgeAzimuth = CONFIG.ridgelineAzimuth;
            const ridgeRotation = (ridgeAzimuth - 90) * Math.PI / 180;
            const cosRot = Math.cos(-ridgeRotation);
            const sinRot = Math.sin(-ridgeRotation);

            // Find SW corner of the rotated house
            // House corners in local space: (±length/2, ±width/2)
            // After rotation: x' = x*cos - z*sin, z' = x*sin + z*cos
            // SW corner: local (-length/2, +width/2) - negative along ridgeline, positive toward SW face
            const localX = -houseLength / 2;
            const localZ = houseWidth / 2;
            const southCornerX = houseCenterX + localX * cosRot - localZ * sinRot;
            const southCornerZ = houseCenterZ + localX * sinRot + localZ * cosRot;

            // Meter near southern corner (offset slightly outward)
            const meterX = southCornerX + 1;
            const meterZ = southCornerZ + 2;

            // Utility pole further east toward road
            const poleX = meterX + 4;
            const poleZ = meterZ + 4;

            // Utility pole
            const poleGeometry = new THREE.CylinderGeometry(0.2, 0.3, 14, 8);
            const poleMaterial = new THREE.MeshLambertMaterial({ color: 0x4a3728 });
            const pole = new THREE.Mesh(poleGeometry, poleMaterial);
            pole.position.set(poleX, 7, poleZ);
            scene.add(pole);

            // Crossarm
            const crossarmGeometry = new THREE.BoxGeometry(6, 0.3, 0.3);
            const crossarm = new THREE.Mesh(crossarmGeometry, poleMaterial);
            crossarm.position.set(poleX, 13, poleZ);
            scene.add(crossarm);

            // Insulators
            const insulatorGeometry = new THREE.CylinderGeometry(0.15, 0.2, 0.5, 8);
            const insulatorMaterial = new THREE.MeshLambertMaterial({ color: 0x888888 });
            [-2, 0, 2].forEach(offset => {
                const insulator = new THREE.Mesh(insulatorGeometry, insulatorMaterial);
                insulator.position.set(poleX + offset, 13.4, poleZ);
                scene.add(insulator);
            });

            // Meter box on house SW wall
            const meterGeometry = new THREE.BoxGeometry(1, 1.5, 0.5);
            const meterMaterial = new THREE.MeshLambertMaterial({ color: 0x666666 });
            const meter = new THREE.Mesh(meterGeometry, meterMaterial);
            meter.position.set(meterX, 4, meterZ);
            scene.add(meter);

            // Service drop wires from pole to meter
            const wireMaterial = new THREE.LineBasicMaterial({ color: 0x222222, linewidth: 2 });
            const wirePoints = [
                new THREE.Vector3(poleX, 13, poleZ),
                new THREE.Vector3((poleX + meterX) / 2, 10, (poleZ + meterZ) / 2),
                new THREE.Vector3(meterX, 5, meterZ)
            ];
            const wireCurve = new THREE.CatmullRomCurve3(wirePoints);
            const wireGeometry = new THREE.BufferGeometry().setFromPoints(wireCurve.getPoints(20));
            const wire = new THREE.Line(wireGeometry, wireMaterial);
            scene.add(wire);
            gridWires.push({ curve: wireCurve, mesh: wire });

            // Grid export label (animated along wire)
            gridExportLabel = createPowerLabel('0W');
            gridExportLabel.scale.set(3, 1.5, 1);
            scene.add(gridExportLabel);

            // Battery bank - positioned near the meter/inverter (slightly west)
            const batteryX = meterX - 5;
            const batteryZ = meterZ - 1;
            const batteryGeometry = new THREE.BoxGeometry(2.5, 3, 1.5);
            const batteryMaterial = new THREE.MeshLambertMaterial({ color: 0x2ecc71 });
            const battery = new THREE.Mesh(batteryGeometry, batteryMaterial);
            battery.position.set(batteryX, 1.5, batteryZ);
            scene.add(battery);

            // Battery terminal strip on top
            const terminalGeometry = new THREE.BoxGeometry(1.5, 0.2, 0.8);
            const terminalMaterial = new THREE.MeshLambertMaterial({ color: 0x333333 });
            const terminal = new THREE.Mesh(terminalGeometry, terminalMaterial);
            terminal.position.set(batteryX, 3.1, batteryZ);
            scene.add(terminal);

            // Battery status label
            batteryStatusLabel = createPowerLabel('0V 0%');
            batteryStatusLabel.scale.set(3, 1.5, 1);
            batteryStatusLabel.position.set(batteryX, 5, batteryZ);
            scene.add(batteryStatusLabel);

            // Wire from battery to meter/inverter
            const batteryWireMaterial = new THREE.LineBasicMaterial({ color: 0xcc0000, linewidth: 2 });
            const batteryWirePoints = [
                new THREE.Vector3(batteryX, 3, batteryZ),
                new THREE.Vector3((batteryX + meterX) / 2, 4, (batteryZ + meterZ) / 2),
                new THREE.Vector3(meterX, 4, meterZ)
            ];
            const batteryWireCurve = new THREE.CatmullRomCurve3(batteryWirePoints);
            const batteryWireGeometry = new THREE.BufferGeometry().setFromPoints(batteryWireCurve.getPoints(15));
            const batteryWireMesh = new THREE.Line(batteryWireGeometry, batteryWireMaterial);
            scene.add(batteryWireMesh);
            batteryWire = { curve: batteryWireCurve, mesh: batteryWireMesh };

            // Battery power label (animated along wire)
            batteryPowerLabel = createPowerLabel('0W');
            batteryPowerLabel.scale.set(2.5, 1.25, 1);
            batteryPowerLabel.visible = false;
            scene.add(batteryPowerLabel);

            // Inverter status label - above meter
            inverterStatusLabel = createPowerLabel('0W');
            inverterStatusLabel.scale.set(3.5, 1.75, 1);
            inverterStatusLabel.position.set(meterX, 7, meterZ);
            scene.add(inverterStatusLabel);

            // Wire from meter/inverter into house
            const homeWireMaterial = new THREE.LineBasicMaterial({ color: 0xffaa00, linewidth: 2 });
            const homeWirePoints = [
                new THREE.Vector3(meterX, 4, meterZ),
                new THREE.Vector3((meterX + houseCenterX) / 2, 5, (meterZ + houseCenterZ) / 2),
                new THREE.Vector3(houseCenterX, 5, houseCenterZ)  // House center
            ];
            const homeWireCurve = new THREE.CatmullRomCurve3(homeWirePoints);
            const homeWireGeometry = new THREE.BufferGeometry().setFromPoints(homeWireCurve.getPoints(15));
            const homeWireMesh = new THREE.Line(homeWireGeometry, homeWireMaterial);
            scene.add(homeWireMesh);
            homeWire = { curve: homeWireCurve, mesh: homeWireMesh };

            // Home power label (animated along wire)
            homePowerLabel = createPowerLabel('0W');
            homePowerLabel.scale.set(3, 1.5, 1);
            homePowerLabel.visible = false;
            scene.add(homePowerLabel);
        }

        // Draw discrete solar panel rectangles on a roof/array surface.
        // Each panel is rendered as a slightly darker rectangle with a thin
        // contrasting border, with small gaps between panels showing the
        // underlying roof color through (mimics real panel arrays).
        //
        //   parent     : the host mesh (roof plane / overlay plane)
        //   accentRGB  : color of the panel border / cell-grid lines
        //   cols, rows : panel grid layout
        //   zOffset    : tiny offset along parent normal to avoid z-fighting
        //   fillRatio  : how much of the parent surface to cover (0.92 leaves
        //                a small margin so panels don't run to the very edge)
        function addPanels(parent, accentRGB, cols, rows, zOffset = 0.1, fillRatio = 0.92, regionX = [0, 1]) {
            // regionX = [startFrac, endFrac] within the parent width along local X
            // (default [0, 1] = full width). Used to leave room for adjacent arrays.
            const params = parent.geometry.parameters;
            const fullW = params.width;
            const surfaceH = params.height;

            const regionCenterX = fullW * (regionX[0] + regionX[1]) / 2 - fullW / 2;
            const surfaceW = fullW * (regionX[1] - regionX[0]);

            const usableW = surfaceW * fillRatio;
            const usableH = surfaceH * fillRatio;
            const cellW = usableW / cols;
            const cellH = usableH / rows;
            const gap = Math.min(cellW, cellH) * 0.10;
            const panelW = Math.max(0.1, cellW - gap);
            const panelH = Math.max(0.1, cellH - gap);

            // Panel face: deep navy/charcoal regardless of array color, so the
            // panels read as actual photovoltaic surfaces (real panels are very
            // dark). The roof underneath provides the color-coding.
            const panelMat = new THREE.MeshLambertMaterial({
                color: 0x1c2a3a,
                side: THREE.DoubleSide,
            });
            const borderMat = new THREE.LineBasicMaterial({ color: accentRGB });

            // Build all panels into one merged group for performance.
            const startX = regionCenterX - usableW / 2 + cellW / 2;
            const startY = -usableH / 2 + cellH / 2;

            // Reuse a single panel geometry across all panels (instances share it)
            const panelGeom = new THREE.PlaneGeometry(panelW, panelH);
            // Build a single edges geometry that we'll position per panel via line segments
            const edgesPositions = [];
            const halfW = panelW / 2;
            const halfH = panelH / 2;

            // Cell-grid lines on each panel — solar cells are usually visible
            // as a 6×10 or 6×12 mini-grid. Use 4×6 to look like cells without
            // spamming geometry.
            const cellGridCols = 6;
            const cellGridRows = 4;

            for (let i = 0; i < cols; i++) {
                for (let j = 0; j < rows; j++) {
                    const cx = startX + i * cellW;
                    const cy = startY + j * cellH;

                    const panel = new THREE.Mesh(panelGeom, panelMat);
                    panel.position.set(cx, cy, zOffset);
                    parent.add(panel);

                    // Panel outer border (4 segments)
                    edgesPositions.push(
                        cx - halfW, cy - halfH, zOffset + 0.001,  cx + halfW, cy - halfH, zOffset + 0.001,
                        cx + halfW, cy - halfH, zOffset + 0.001,  cx + halfW, cy + halfH, zOffset + 0.001,
                        cx + halfW, cy + halfH, zOffset + 0.001,  cx - halfW, cy + halfH, zOffset + 0.001,
                        cx - halfW, cy + halfH, zOffset + 0.001,  cx - halfW, cy - halfH, zOffset + 0.001,
                    );

                    // Internal cell-grid lines (vertical, then horizontal)
                    for (let k = 1; k < cellGridCols; k++) {
                        const xv = cx - halfW + (k / cellGridCols) * panelW;
                        edgesPositions.push(xv, cy - halfH, zOffset + 0.001, xv, cy + halfH, zOffset + 0.001);
                    }
                    for (let k = 1; k < cellGridRows; k++) {
                        const yv = cy - halfH + (k / cellGridRows) * panelH;
                        edgesPositions.push(cx - halfW, yv, zOffset + 0.001, cx + halfW, yv, zOffset + 0.001);
                    }
                }
            }

            // Merge all edge segments into one LineSegments draw call
            const edgesGeom = new THREE.BufferGeometry();
            edgesGeom.setAttribute('position', new THREE.Float32BufferAttribute(edgesPositions, 3));
            const edges = new THREE.LineSegments(edgesGeom, borderMat);
            parent.add(edges);
        }

        // Backwards-compatible alias (still called from a few places that
        // pass cols/rows from older code paths).
        function addPanelGrid(parent, color, cols, rows, zOffset) {
            addPanels(parent, color, cols, rows, zOffset);
        }

        function createCompass() {
            // Center compass over house position
            const houseCenterX = ftToUnits(CONFIG.house.position_ft[0]);
            const houseCenterZ = ftToUnits(CONFIG.house.position_ft[1]);

            const addLabel = (text, angle, color) => {
                const canvas = document.createElement('canvas');
                canvas.width = 64;
                canvas.height = 64;
                const ctx = canvas.getContext('2d');
                ctx.fillStyle = color;
                ctx.font = 'bold 48px Arial';
                ctx.textAlign = 'center';
                ctx.textBaseline = 'middle';
                ctx.fillText(text, 32, 32);

                const texture = new THREE.CanvasTexture(canvas);
                const sprite = new THREE.Sprite(new THREE.SpriteMaterial({ map: texture }));
                sprite.scale.set(4, 4, 1);
                const rad = angle * Math.PI / 180;
                sprite.position.set(houseCenterX + Math.sin(rad) * 35, 1, houseCenterZ - Math.cos(rad) * 35);
                scene.add(sprite);
            };

            addLabel('N', 0, '#e74c3c');
            addLabel('E', 90, '#888888');
            addLabel('S', 180, '#888888');
            addLabel('W', 270, '#888888');
        }

        function createSunArc() {
            // Create a placeholder arc - will be updated with real data from API
            const arcMaterial = new THREE.LineDashedMaterial({
                color: 0xffaa44,
                dashSize: 2,
                gapSize: 1,
                transparent: true,
                opacity: 0.4
            });

            // Create with dummy geometry initially
            const dummyPoints = [new THREE.Vector3(0, 0, 0), new THREE.Vector3(1, 0, 0)];
            const arcGeometry = new THREE.BufferGeometry().setFromPoints(dummyPoints);
            sunArc = new THREE.Line(arcGeometry, arcMaterial);
            sunArc.computeLineDistances();
            scene.add(sunArc);
        }

        function updateSunArc(sunPath) {
            if (!sunPath || sunPath.length < 2 || !sunArc) return;

            // Filter to only points above horizon
            const validPoints = sunPath.filter(p => p.altitude > 0);
            if (validPoints.length < 2) return;

            // Center the arc over the house
            const houseCenterX = ftToUnits(CONFIG.house.position_ft[0]);
            const houseCenterZ = ftToUnits(CONFIG.house.position_ft[1]);

            // Convert sun path positions to 3D coordinates centered on house
            const arcPoints = validPoints.map(pos => {
                const altRad = pos.altitude * Math.PI / 180;
                const azRad = pos.azimuth * Math.PI / 180;

                const x = houseCenterX + SUN_DISTANCE * Math.cos(altRad) * Math.sin(azRad);
                const y = SUN_DISTANCE * Math.sin(altRad);
                const z = houseCenterZ - SUN_DISTANCE * Math.cos(altRad) * Math.cos(azRad);

                return new THREE.Vector3(x, y, z);
            });

            // Create smooth curve through points
            const arcCurve = new THREE.CatmullRomCurve3(arcPoints);
            const curvePoints = arcCurve.getPoints(50);

            // Update the arc geometry
            sunArc.geometry.dispose();
            sunArc.geometry = new THREE.BufferGeometry().setFromPoints(curvePoints);
            sunArc.computeLineDistances();

            sunArcInitialized = true;
        }

        function updateSunPosition(altitude, azimuth) {
            // No-op when 3D scene wasn't initialized (e.g. WebGL not available)
            if (!sun || !sunLight) return;
            // Center the sun orbit over the house
            const houseCenterX = ftToUnits(CONFIG.house.position_ft[0]);
            const houseCenterZ = ftToUnits(CONFIG.house.position_ft[1]);

            const altRad = altitude * Math.PI / 180;
            const azRad = azimuth * Math.PI / 180;

            const x = houseCenterX + SUN_DISTANCE * Math.cos(altRad) * Math.sin(azRad);
            const y = SUN_DISTANCE * Math.sin(altRad);
            const z = houseCenterZ - SUN_DISTANCE * Math.cos(altRad) * Math.cos(azRad);

            sun.position.set(x, Math.max(y, -5), z);
            sunLight.position.copy(sun.position);

            // Update sun ray direction (from sun toward house center/roof)
            sunRayDirection.set(houseCenterX - x, -y, houseCenterZ - z).normalize();

            if (altitude > 0) {
                sun.visible = true;
                sunRayParticles.visible = true;
                sunLight.intensity = Math.min(1.2, 0.3 + altitude / 30);
                const hue = 0.12 - (Math.max(0, 15 - altitude) / 15) * 0.06;
                sun.material.color.setHSL(hue, 1, 0.6);
                // Adjust ray opacity based on sun altitude
                sunRayParticles.material.opacity = Math.min(0.6, altitude / 60);
            } else {
                sun.visible = false;
                sunRayParticles.visible = false;
                sunLight.intensity = 0.2;
            }

            updateRoofBrightness(altitude, azimuth);
        }

        function updateRoofBrightness(alt, az) {
            // PERF FIX: Use setRGB on existing emissive Color instead of creating new objects
            // Creating new THREE.Color every frame causes massive GC pressure
            if (alt <= 0) {
                roofSW.material.emissive.setRGB(0, 0, 0);
                roofNE.material.emissive.setRGB(0, 0, 0);
                return;
            }

            // Get azimuths from config
            const swArray = getArrayConfig('SW') || CONFIG.arrays[1];
            const neArray = getArrayConfig('NE') || CONFIG.arrays[0];

            // Calculate facing factor for each roof
            const swDiff = Math.abs(az - swArray.azimuth);
            const neDiff = Math.abs(az - neArray.azimuth);
            const swFace = Math.max(0, 1 - Math.min(swDiff, 360 - swDiff) / 90);
            const neFace = Math.max(0, 1 - Math.min(neDiff, 360 - neDiff) / 90);

            const swBright = swFace * (alt / 50) * 0.4;
            const neBright = neFace * (alt / 50) * 0.4;

            roofSW.material.emissive.setRGB(swBright * 0.2, swBright * 0.5, swBright);
            roofNE.material.emissive.setRGB(neBright * 0.5, neBright * 0.2, neBright);
        }

        function onWindowResize() {
            const container = document.getElementById('canvas-container');
            camera.aspect = container.clientWidth / container.clientHeight;
            camera.updateProjectionMatrix();
            renderer.setSize(container.clientWidth, container.clientHeight);
        }

        let gridExportT = 0;
        let currentGridExport = 0;
        let batteryPowerT = 0;
        let currentBatteryPower = 0; // Positive = charging, negative = discharging
        let homePowerT = 0;
        let currentHomePower = 0;

        function animate(currentTime) {
            animationId = requestAnimationFrame(animate);

            // Throttle to target FPS
            if (currentTime - lastRenderTime < FRAME_TIME) return;
            lastRenderTime = currentTime;

            // Animate grid export label along wire (from house to pole)
            if (gridWires.length > 0 && gridExportLabel && currentGridExport > 0) {
                gridExportT = (gridExportT + 0.012) % 1;
                // Reverse direction: 1-t goes from house (end) to pole (start)
                const pos = gridWires[0].curve.getPoint(1 - gridExportT);
                gridExportLabel.position.copy(pos);
                gridExportLabel.position.y += 2; // Offset above wire
                gridExportLabel.visible = true;
            } else if (gridExportLabel) {
                gridExportLabel.visible = false;
            }

            // Animate battery power label along wire
            if (batteryWire && batteryPowerLabel && currentBatteryPower !== 0) {
                batteryPowerT = (batteryPowerT + 0.015) % 1;
                // Direction based on charge/discharge:
                // Charging (positive): power flows from inverter TO battery (t goes 1->0)
                // Discharging (negative): power flows from battery TO inverter (t goes 0->1)
                const t = currentBatteryPower > 0 ? (1 - batteryPowerT) : batteryPowerT;
                const pos = batteryWire.curve.getPoint(t);
                batteryPowerLabel.position.copy(pos);
                batteryPowerLabel.position.y += 1.5; // Offset above wire
                batteryPowerLabel.visible = true;
            } else if (batteryPowerLabel) {
                batteryPowerLabel.visible = false;
            }

            // Animate home power label along wire (inverter to house)
            if (homeWire && homePowerLabel && currentHomePower > 0) {
                homePowerT = (homePowerT + 0.012) % 1;
                // Power flows from inverter TO house (t goes 0->1)
                const pos = homeWire.curve.getPoint(homePowerT);
                homePowerLabel.position.copy(pos);
                homePowerLabel.position.y += 1.5; // Offset above wire
                homePowerLabel.visible = true;
            } else if (homePowerLabel) {
                homePowerLabel.visible = false;
            }

            // Animate sun ray lines streaming from sun to roof
            if (sunRayParticles && sunRayParticles.visible && sunRayPositions && sun.visible) {
                const positions = sunRayParticles.geometry.attributes.position.array;

                // Target the house roof center
                const houseCenterX = ftToUnits(CONFIG.house.position_ft[0]);
                const houseCenterZ = ftToUnits(CONFIG.house.position_ft[1]);
                const roofHeight = ftToUnits(CONFIG.house.height_ft) + 2;  // Roof ridge height

                for (let i = 0; i < NUM_SUN_RAYS; i++) {
                    const vel = sunRayVelocities[i];

                    // Advance t (position along sun-to-roof path)
                    vel.t += vel.speed * 0.02;

                    // Reset if ray reached the roof
                    if (vel.t > 1) {
                        vel.t = 0;
                        vel.offsetX = (Math.random() - 0.5) * 12;
                        vel.offsetZ = (Math.random() - 0.5) * 8;
                    }

                    // Interpolate from sun to roof (house center at roof height)
                    const t = vel.t;
                    const targetX = houseCenterX + vel.offsetX;
                    const targetZ = houseCenterZ + vel.offsetZ;
                    const startX = sun.position.x * (1 - t) + targetX * t;
                    const startY = sun.position.y * (1 - t) + roofHeight * t;
                    const startZ = sun.position.z * (1 - t) + targetZ * t;

                    // End point slightly behind (toward sun) to create line
                    const t2 = Math.max(0, t - 0.03);
                    const endX = sun.position.x * (1 - t2) + targetX * t2;
                    const endY = sun.position.y * (1 - t2) + roofHeight * t2;
                    const endZ = sun.position.z * (1 - t2) + targetZ * t2;

                    // Update line segment positions
                    positions[i * 6] = startX;
                    positions[i * 6 + 1] = startY;
                    positions[i * 6 + 2] = startZ;
                    positions[i * 6 + 3] = endX;
                    positions[i * 6 + 4] = endY;
                    positions[i * 6 + 5] = endZ;
                }
                sunRayParticles.geometry.attributes.position.needsUpdate = true;
            }

            controls.update();
            renderer.render(scene, camera);
        }

        // Data fetching and UI updates
        async function fetchData() {
            try {
                const response = await fetch('/api/data');
                const data = await response.json();

                // Update sun position
                if (data.sun) {
                    updateSunPosition(data.sun.altitude, data.sun.azimuth);
                    document.getElementById('sun-altitude').textContent = data.sun.altitude.toFixed(1) + '°';
                    document.getElementById('sun-azimuth').textContent = data.sun.azimuth.toFixed(1) + '°';
                    document.getElementById('sunrise').textContent = formatTime(data.sun.sunrise_hour);
                    document.getElementById('sunset').textContent = formatTime(data.sun.sunset_hour);
                }

                // Update sun arc with real path data (only once per session)
                if (data.sun_path && !sunArcInitialized) {
                    updateSunArc(data.sun_path);
                }

                // Update inverter data
                if (data.inverter) {
                    const inv = data.inverter;
                    const pvPower = numericPower(inv.pv_total_power);
                    const battPower = numericPower(inv.battery_charge_power) - numericPower(inv.battery_discharge_power);
                    // Net grid: + = exporting to grid, - = importing from grid
                    const gridPower = numericPower(inv.power_to_grid) - numericPower(inv.power_to_user);
                    updatePowerLoadCard('pv', pvPower);
                    updatePowerLoadCard('battery', battPower);
                    updatePowerLoadCard('grid', gridPower);

                    // MPPT optimal voltage range for FlexBOSS21 — outside this range
                    // the MPPT efficiency drops noticeably. Color-code voltages so
                    // you can see at a glance which strings are wired suboptimally.
                    const MPPT_OPT_MIN = 300, MPPT_OPT_MAX = 580;
                    const fmtMpptDetail = (v, w) => {
                        const a = v > 0 ? (w / v).toFixed(1) : '0.0';
                        let cls = 'good';
                        let warn = '';
                        if (v > 0 && v < 140) { cls = 'bad'; warn = ' ⚠ below turn-on'; }
                        else if (v > 0 && v < MPPT_OPT_MIN) { cls = 'warning'; warn = ` ⚠ below optimal (300V)`; }
                        else if (v > MPPT_OPT_MAX) { cls = 'bad'; warn = ' ⚠ above max'; }
                        return { text: `${v.toFixed(0)}V / ${a}A${warn}`, cls };
                    };
                    const setMpptDetail = (id, v, w) => {
                        const el = document.getElementById(id);
                        const { text, cls } = fmtMpptDetail(v, w);
                        el.textContent = text;
                        el.classList.remove('good', 'warning', 'bad');
                        el.classList.add(cls);
                    };

                    // MPPT 1
                    document.getElementById('pv1').textContent = formatPower(inv.pv1_power);
                    setMpptDetail('pv1-detail', inv.pv1_voltage, inv.pv1_power);

                    // MPPT 2
                    document.getElementById('pv2').textContent = formatPower(inv.pv2_power);
                    setMpptDetail('pv2-detail', inv.pv2_voltage, inv.pv2_power);

                    // MPPT 3
                    document.getElementById('pv3').textContent = formatPower(inv.pv3_power);
                    setMpptDetail('pv3-detail', inv.pv3_voltage, inv.pv3_power);

                    const allMpptIdle = [inv.pv1_power, inv.pv2_power, inv.pv3_power]
                        .every((power) => numericPower(power) === 0);
                    document.getElementById('pv-arrays-section').classList.toggle('mppt-zero', allMpptIdle);

                    document.getElementById('battery-soc').textContent = inv.battery_soc + '%';
                    document.getElementById('battery-voltage').textContent = inv.battery_voltage.toFixed(1) + 'V';

                    document.getElementById('battery-power').textContent =
                        formatSignedPower(battPower);
                    document.getElementById('battery-temp').textContent = inv.battery_temperature + '°C';

                    document.getElementById('grid-export').textContent = formatPower(inv.power_to_grid);
                    document.getElementById('grid-import').textContent = formatPower(inv.consumption_power);
                    document.getElementById('grid-voltage').textContent = inv.grid_voltage.toFixed(1) + 'V';
                    document.getElementById('grid-freq').textContent = inv.grid_frequency.toFixed(2) + 'Hz';

                    document.getElementById('inverter-power').textContent = formatPower(inv.inverter_power);
                    document.getElementById('inverter-temp').textContent = inv.inverter_temperature + '°C';
                    document.getElementById('inverter-status').textContent = inv.status;

                    // BMS-reported limits
                    if (inv.max_charge_current != null) {
                        document.getElementById('bms-max-charge').textContent = inv.max_charge_current.toFixed(0) + ' A';
                    }
                    if (inv.max_discharge_current != null) {
                        document.getElementById('bms-max-discharge').textContent = inv.max_discharge_current.toFixed(0) + ' A';
                    }

                    // Today's energy totals
                    const fmtKWh = (v) => (v != null ? v.toFixed(1) : '--') + ' kWh';
                    document.getElementById('energy-yield').textContent     = fmtKWh(inv.energy_today_yield);
                    document.getElementById('energy-export').textContent    = fmtKWh(inv.energy_today_export);
                    document.getElementById('energy-import').textContent    = fmtKWh(inv.energy_today_import);
                    document.getElementById('energy-usage').textContent     = fmtKWh(inv.energy_today_usage);
                    document.getElementById('energy-charge').textContent    = fmtKWh(inv.energy_today_charge);
                    document.getElementById('energy-discharge').textContent = fmtKWh(inv.energy_today_discharge);

                    // Daily totals shown under each power card
                    const yld = (inv.energy_today_yield || 0).toFixed(1);
                    const chg = (inv.energy_today_charge || 0).toFixed(1);
                    const dis = (inv.energy_today_discharge || 0).toFixed(1);
                    const exp = (inv.energy_today_export || 0).toFixed(1);
                    const imp = (inv.energy_today_import || 0).toFixed(1);
                    document.getElementById('pv-summary').innerHTML =
                        `today: <span class="pos">${yld}</span> kWh generated`;
                    document.getElementById('battery-summary').innerHTML =
                        `today: <span class="pos">+${chg}</span> charged · ` +
                        `<span class="neg">−${dis}</span> discharged kWh`;
                    document.getElementById('grid-summary').innerHTML =
                        `today: <span class="pos">+${exp}</span> exported · ` +
                        `<span class="${(inv.energy_today_import || 0) > 0.05 ? 'neg' : 'pos'}">−${imp}</span> imported kWh`;

                    // Color import red if non-zero
                    const importEl = document.getElementById('energy-import');
                    importEl.classList.toggle('bad', (inv.energy_today_import || 0) > 0.05);
                    importEl.classList.toggle('good', (inv.energy_today_import || 0) <= 0.05);
                }

                // TOU + mode panel
                if (data.schedule) {
                    const sch = data.schedule;
                    document.getElementById('op-mode').textContent = sch.operational_mode;

                    let touText = 'off-peak';
                    if (sch.in_peak) touText = '🔴 PEAK';
                    else if (sch.in_partial_peak) touText = '🟡 partial peak';
                    document.getElementById('tou-period').textContent = touText;

                    document.getElementById('ava-bonus').textContent =
                        sch.in_ava_bonus ? '✓ active (+$0.025/kWh)' : 'inactive';

                    document.getElementById('forced-discharge-status').textContent =
                        sch.in_forced_discharge_window ? '🔋→⚡ ACTIVE' : 'idle';

                    if (sch.config) {
                        const w = sch.config.forced_discharge_window;
                        document.getElementById('schedule-window').textContent =
                            String(w[0]).padStart(2,'0') + ':00 – ' + String(w[1]).padStart(2,'0') + ':00';
                        document.getElementById('discharge-floor').textContent =
                            sch.config.forced_discharge_soc_floor + ' %';
                    }

                    if (sch.rates) {
                        document.getElementById('export-rate-now').textContent =
                            '$' + sch.rates.currently_active_export_rate.toFixed(3) + '/kWh';
                        document.getElementById('import-rate-now').textContent =
                            '$' + sch.rates.currently_active_import_rate.toFixed(2) + '/kWh';
                    }

                    const dph = sch.dollars_per_hour_est;
                    if (dph != null) {
                        const sign = dph >= 0 ? '+' : '';
                        document.getElementById('dollars-per-hour').textContent =
                            sign + '$' + dph.toFixed(2) + '/hr';
                    }
                }

                if (data.inverter) {
                    const inv = data.inverter;

                    // Update 3D power labels on arrays - match sidebar values exactly
                    // MPPT1 = NE face (purple)
                    // MPPT2 = SW face (blue)
                    // MPPT3 = Backyard (green, facing S)
                    // MPPT1 is wired to SW roof, MPPT2 is wired to NE roof
                    if (swPowerLabel) updatePowerLabel(swPowerLabel, formatPower(inv.pv1_power));
                    if (nePowerLabel) updatePowerLabel(nePowerLabel, formatPower(inv.pv2_power));
                    if (mixedPowerLabel) updatePowerLabel(mixedPowerLabel, formatPower(inv.pv3_power));
                    if (yardPowerLabel) updatePowerLabel(yardPowerLabel, formatPower(inv.pv3_power));

                    // Update grid export animation
                    currentGridExport = inv.power_to_grid;
                    if (gridExportLabel) {
                        updatePowerLabel(gridExportLabel, formatPower(inv.power_to_grid));
                    }

                    // Update battery 3D labels
                    const batteryNetPower = inv.battery_charge_power - inv.battery_discharge_power;
                    currentBatteryPower = batteryNetPower;

                    // Static label above battery: voltage & SOC
                    if (batteryStatusLabel) {
                        updatePowerLabel(batteryStatusLabel, inv.battery_voltage.toFixed(1) + 'V ' + inv.battery_soc + '%');
                    }

                    // Animated power label along wire
                    if (batteryPowerLabel && batteryNetPower !== 0) {
                        const prefix = batteryNetPower > 0 ? '+' : '';
                        updatePowerLabel(batteryPowerLabel, prefix + Math.abs(batteryNetPower).toLocaleString() + 'W');
                    }

                    // Update inverter status label (output wattage)
                    if (inverterStatusLabel) {
                        updatePowerLabel(inverterStatusLabel, formatPower(inv.inverter_power));
                    }

                    // Update home power animation
                    currentHomePower = inv.consumption_power;
                    if (homePowerLabel && currentHomePower > 0) {
                        updatePowerLabel(homePowerLabel, formatPower(inv.consumption_power));
                    }
                }

                document.getElementById('last-update').textContent = new Date().toLocaleTimeString();

            } catch (error) {
                console.error('Error fetching data:', error);
            }
        }

        function formatTime(hour) {
            if (hour === null || hour === undefined) return '--:--';
            const h = Math.floor(hour);
            const m = Math.floor((hour - h) * 60);
            return h.toString().padStart(2, '0') + ':' + m.toString().padStart(2, '0');
        }

        // =====================================================================
        // SETTINGS MODAL
        // =====================================================================
        let allParams = {};
        let originalParams = {};
        let currentFilter = 'all';

        function openSettings() {
            document.getElementById('settings-modal').classList.add('active');
            loadParams();
        }

        function closeSettings() {
            document.getElementById('settings-modal').classList.remove('active');
        }

        async function loadParams() {
            const grid = document.getElementById('param-grid');
            grid.innerHTML = '<div style="text-align:center;color:#888;padding:40px;">Loading parameters...</div>';

            try {
                const response = await fetch('/api/parameters');
                const data = await response.json();

                if (data.success) {
                    allParams = data.parameters;
                    originalParams = JSON.parse(JSON.stringify(data.parameters));
                    renderParams();
                } else {
                    grid.innerHTML = '<div style="text-align:center;color:#e74c3c;padding:40px;">Error: ' + data.error + '</div>';
                }
            } catch (error) {
                grid.innerHTML = '<div style="text-align:center;color:#e74c3c;padding:40px;">Error loading parameters</div>';
            }
        }

        function refreshParams() {
            loadParams();
        }

        function renderParams() {
            const grid = document.getElementById('param-grid');
            const search = document.getElementById('param-search').value.toLowerCase();

            // Sort parameters alphabetically
            const sortedKeys = Object.keys(allParams).sort();

            // Filter parameters
            const filteredKeys = sortedKeys.filter(key => {
                // Search filter
                if (search && !key.toLowerCase().includes(search)) return false;

                // Type filter (modified filter removed — read-only mode)
                if (currentFilter !== 'all' && !key.startsWith(currentFilter)) return false;

                return true;
            });

            // Update count
            document.getElementById('param-count').textContent = filteredKeys.length + ' of ' + sortedKeys.length + ' parameters';

            if (filteredKeys.length === 0) {
                grid.innerHTML = '<div style="text-align:center;color:#888;padding:40px;">No parameters match filter</div>';
                return;
            }

            // Render grid
            grid.innerHTML = filteredKeys.map(key => {
                const value = allParams[key];
                const isBool = typeof value === 'boolean' || value === 'true' || value === 'false' || value === true || value === false;
                const isFunc = key.startsWith('FUNC_');
                const isHold = key.startsWith('HOLD_');
                const isBit = key.startsWith('BIT_');

                let nameClass = 'param-name';
                if (isFunc) nameClass += ' func';
                else if (isHold) nameClass += ' hold';
                else if (isBit) nameClass += ' bit';

                // Trim prefixes for display, keep full name in tooltip
                let displayName = key;
                if (isFunc) displayName = key.substring(5);  // Remove FUNC_
                else if (isHold) displayName = key.substring(5);  // Remove HOLD_
                else if (isBit) displayName = key.substring(4);  // Remove BIT_

                let inputHtml;
                if (isBool || isFunc) {
                    const isOn = value === true || value === 'true' || value === 'True';
                    inputHtml = `<div class="param-toggle ${isOn ? 'on' : ''} read-only" title="read-only"></div>`;
                } else {
                    inputHtml = `<input type="text" class="param-input" value="${escapeHtml(String(value))}" readonly title="read-only">`;
                }

                return `<div class="param-item" data-key="${key}">
                    <span class="${nameClass}" title="${key}">${displayName}</span>
                    ${inputHtml}
                </div>`;
            }).join('');
        }

        function escapeHtml(text) {
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML.replace(/"/g, '&quot;');
        }

        // NOTE: This dashboard is read-only — toggleParam/updateParam/saveParam
        // intentionally removed. Use a dedicated apply_*.py script to modify settings.

        function filterParams() {
            renderParams();
        }

        function setFilter(filter) {
            currentFilter = filter;

            // Update button states
            document.querySelectorAll('.filter-btn').forEach(btn => {
                btn.classList.toggle('active', btn.dataset.filter === filter);
            });

            renderParams();
        }

        // Close modal on escape key
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') closeSettings();
        });

        // Close modal on overlay click
        document.getElementById('settings-modal').addEventListener('click', (e) => {
            if (e.target.id === 'settings-modal') closeSettings();
        });

        // Initialize
        init();
    </script>
</body>
</html>
'''


@app.route('/')
def index():
    # Build config object for JavaScript
    config = {
        "arrays": SOLAR_ARRAYS,
        "house": HOUSE,
        "sceneScale": SCENE_SCALE,
        "ridgelineAzimuth": RIDGELINE_AZIMUTH,
        "roofTilt": ROOF_TILT,
        "totalCapacityKw": TOTAL_ARRAY_KW,
        "latitude": LATITUDE,
        "longitude": LONGITUDE,
    }
    return render_template_string(HTML_TEMPLATE, config_json=json.dumps(config))


@app.route('/api/parameters')
def get_parameters():
    """API endpoint returning all inverter parameters."""
    try:
        params = get_all_parameters_sync()
        return jsonify({"success": True, "parameters": params})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/parameters', methods=['POST'])
def set_parameter():
    """Read-only dashboard — writes are explicitly blocked."""
    return jsonify({
        "success": False,
        "error": "This dashboard is read-only. Use a dedicated apply_*.py script to modify inverter settings.",
    }), 403


@app.route('/api/data')
def get_data():
    """API endpoint returning all live data."""
    now = datetime.now(TIMEZONE)

    # Calculate sun position
    sun_pos = calculate_solar_position(now, LATITUDE, LONGITUDE)

    # Calculate expected power for each array based on its orientation.
    # "split" arrays (e.g. MPPT3 = panels distributed across two roof faces)
    # take a list of azimuths + fractions and compute a weighted irradiance.
    dni = calculate_clear_sky_dni(sun_pos["altitude"])
    expected_power = 0.0
    for array in SOLAR_ARRAYS:
        if array.get("type") == "split":
            azimuths = array["azimuth"]
            fractions = array.get("fractions", [1.0 / len(azimuths)] * len(azimuths))
            irradiance = sum(
                f * calculate_panel_irradiance(
                    sun_pos["altitude"], sun_pos["azimuth"],
                    array["tilt"], az, dni,
                )
                for az, f in zip(azimuths, fractions)
            )
        else:
            irradiance = calculate_panel_irradiance(
                sun_pos["altitude"], sun_pos["azimuth"],
                array["tilt"], array["azimuth"], dni,
            )
        # Convert irradiance to power: capacity * (irradiance/1000) * efficiency
        array_power = array["capacity_kw"] * 1000 * (irradiance / 1000) * SYSTEM_EFFICIENCY
        expected_power += array_power

    # Get inverter data (cached or fresh)
    inverter_data = get_inverter_data_sync()

    # Calculate sun path for today (for arc visualization)
    sun_path = calculate_sun_path(now, LATITUDE, LONGITUDE)

    # Compute schedule + operational mode
    schedule = compute_schedule_status(now, inverter_data)

    return jsonify({
        "timestamp": now.isoformat(),
        "sun": {
            "altitude": sun_pos["altitude"],
            "azimuth": sun_pos["azimuth"],
            "sunrise_hour": sun_pos["sunrise_hour"],
            "sunset_hour": sun_pos["sunset_hour"],
            "is_daylight": sun_pos["is_daylight"],
        },
        "sun_path": sun_path,
        "expected_power": expected_power,
        "dni": dni,
        "inverter": inverter_data,
        "schedule": schedule,
        "read_only": True,
    })


# Cache today's events for 60s to avoid hammering the inverter API
_EVENTS_CACHE = {"date": None, "ts": None, "events": []}


async def _fetch_today_import_events():
    """Pull per-4-min pToUser/SOC/ppv chart samples for today and classify
    each contiguous import event."""
    from datetime import timedelta as _td
    now = datetime.now(TIMEZONE)
    today = now.date().isoformat()

    inverter = _inverter_cache.get("inverter")
    if not inverter:
        return []
    serial = inverter.serial_number
    client = _inverter_cache.get("client")
    if not client:
        return []

    try:
        ptouser, soc, ppv = await asyncio.gather(
            client.analytics.get_chart_data(serial, "pToUser", today),
            client.analytics.get_chart_data(serial, "soc", today),
            client.analytics.get_chart_data(serial, "ppv", today),
        )
    except Exception as e:
        print(f"events fetch error: {e}", flush=True)
        return []

    # Build lookups for SOC and PV at each sample timestamp
    soc_lookup = {s.get("time"): s.get("value") for s in soc.get("data", []) if "time" in s}
    ppv_lookup = {s.get("time"): s.get("value") for s in ppv.get("data", []) if "time" in s}

    # Group consecutive non-zero pToUser samples into events
    events_raw = []
    cur = None
    for s in ptouser.get("data", []):
        v = s.get("value") or 0
        t = s.get("time")
        if not t:
            continue
        if v > 10:  # >10 W = real import (filter noise)
            if cur is None:
                cur = {"start": t, "end": t, "vals": [v]}
            else:
                cur["end"] = t
                cur["vals"].append(v)
        else:
            if cur is not None:
                events_raw.append(cur)
                cur = None
    if cur is not None:
        events_raw.append(cur)

    # Classify each event
    out = []
    for e in events_raw:
        try:
            ts_start = datetime.strptime(e["start"], "%Y-%m-%d %H:%M:%S")
            ts_end = datetime.strptime(e["end"], "%Y-%m-%d %H:%M:%S")
        except (ValueError, KeyError):
            continue
        dur_min = (ts_end - ts_start).total_seconds() / 60 + 4  # +sample window
        avg_w = sum(e["vals"]) / len(e["vals"])
        peak_w = max(e["vals"])
        kwh = sum(e["vals"]) * 240 / 3600 / 1000  # 4-min samples → kWh
        soc_at = soc_lookup.get(e["start"])
        ppv_at = ppv_lookup.get(e["start"])
        hour = ts_start.hour

        reason = _classify_import_event(
            hour=hour,
            dur_min=dur_min,
            peak_w=peak_w,
            avg_w=avg_w,
            soc=soc_at,
            ppv=ppv_at,
        )
        out.append({
            "start": ts_start.strftime("%H:%M"),
            "end": ts_end.strftime("%H:%M"),
            "duration_min": round(dur_min, 1),
            "peak_w": int(peak_w),
            "kwh": round(kwh, 3),
            "soc": int(soc_at) if soc_at is not None else None,
            "ppv": int(ppv_at) if ppv_at is not None else None,
            "reason": reason,
        })
    return out


def _classify_import_event(*, hour, dur_min, peak_w, avg_w, soc, ppv):
    """Heuristic classifier for import events.

    Pattern catalog (from analyze_imports.py findings):
      - Pattern 1: Overnight battery floor exhaustion
      - Pattern 2: Mid-day CT dead-band / MPPT priority inversion
      - Pattern 3: Evening high-SOC discharge holdoff
      - Auto-AC-charge-from-grid (rescue cycle)
      - Brief load surge (catchall)
    """
    soc_v = soc if soc is not None else -1

    # Auto-AC-charge rescue: long high-power import during BAT_FIRST hours (1-6 AM)
    if 1 <= hour <= 6 and dur_min > 30 and peak_w > 4000 and 0 <= soc_v < 30:
        return "Auto-AC-charge rescue (battery hit BAT_FIRST trigger)"
    # Pattern 1: Overnight floor exhaustion
    if hour < 8 and 0 <= soc_v < 20:
        return "Battery hit overnight discharge floor"
    # Pattern 2: Mid-day load spike (SOC + time + short duration are sufficient)
    if 9 <= hour < 16 and soc_v > 70 and dur_min < 15:
        return "Brief load spike (CT dead-band, mid-day)"
    # Pattern 3: Evening high-SOC holdoff (battery refuses to discharge)
    if 16 <= hour < 21 and soc_v > 80 and dur_min < 60:
        return "Evening high-SOC discharge holdoff"
    # Short transient (catchall)
    if dur_min <= 5:
        return "Brief load surge (transient)"
    if 0 <= soc_v < 15:
        return "Low SOC — battery couldn't cover load"
    return "Unclassified import event"


def _events_sync():
    """Sync wrapper for the events fetch; reuses the dashboard's persistent loop."""
    today = datetime.now(TIMEZONE).date().isoformat()
    if (_EVENTS_CACHE["date"] == today and _EVENTS_CACHE["ts"] and
            (datetime.now() - _EVENTS_CACHE["ts"]).total_seconds() < 60):
        return _EVENTS_CACHE["events"]
    loop = _inverter_cache.get("loop")
    if loop is None or loop.is_closed():
        return _EVENTS_CACHE["events"]
    try:
        events = loop.run_until_complete(_fetch_today_import_events())
    except Exception as e:
        print(f"events sync error: {e}", flush=True)
        events = _EVENTS_CACHE.get("events", [])
    _EVENTS_CACHE["date"] = today
    _EVENTS_CACHE["ts"] = datetime.now()
    _EVENTS_CACHE["events"] = events
    return events


@app.route('/api/events')
def get_events():
    """Return today's classified import events."""
    return jsonify({
        "date": datetime.now(TIMEZONE).date().isoformat(),
        "events": _events_sync(),
    })


if __name__ == '__main__':
    print("\n" + "="*60)
    print("  Solar Dashboard - 3D Visualization")
    print("="*60)
    print(f"  Location: Oakland, CA ({LATITUDE}°N, {LONGITUDE}°W)")
    print(f"  House: {HOUSE['length_ft']}x{HOUSE['width_ft']}ft, {HOUSE['height_ft']}ft tall")
    print(f"  Ridgeline: {RIDGELINE_AZIMUTH}° azimuth, {ROOF_TILT}° pitch")
    print(f"  Total Array: {TOTAL_ARRAY_KW:.1f}kW across {len(SOLAR_ARRAYS)} arrays:")
    for arr in SOLAR_ARRAYS:
        arr_type = arr.get('type', 'roof')
        az = arr['azimuth']
        az_str = " / ".join(f"{a}°" for a in az) if isinstance(az, list) else f"{az}°"
        print(f"    - {arr['name']}: {arr['capacity_kw']}kW @ {az_str} az, {arr['tilt']}° tilt ({arr_type})")
    print("="*60)
    print("\n  Open in browser: http://localhost:5050\n")

    app.run(host='0.0.0.0', port=5050, debug=False)
