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
        # 14 × 550 W = 7.7 kW nameplate. Measured peak from inverter chart
        # data on best-5 May days: 4.7-4.8 kW (those are 4-min sample
        # maxes; instantaneous peaks can run a bit higher, and June will
        # be higher still as we approach solstice). effective_peak_kw is
        # the array's output normalized to POA=1000 W/m². Set high enough
        # that actual never exceeds the model, accounting for:
        #   - sub-4-min spikes the chart-data summary misses
        #   - June/July gains (~5% over May)
        #   - cool-panel days with marginally higher cell efficiency
        "name": "SW Roof (MPPT1)",
        "type": "roof",
        "azimuth": 217.0,
        "tilt": 10.0,
        "effective_peak_kw": 5.6,
        "capacity_kw": 7.7,
        "panel_count": 14,
        "panel_layout": [7, 2],
        "color": 0x3498db,
    },
    {
        # 7.7 kW nameplate, measured peak 4.5-4.6 kW (NE peaks in morning).
        "name": "NE Roof (MPPT2)",
        "type": "roof",
        "azimuth": 37.0,
        "tilt": 10.0,
        "effective_peak_kw": 5.6,
        "capacity_kw": 7.7,
        "panel_count": 14,
        "panel_layout": [7, 2],
        "color": 0x9b59b6,
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
        # 10 × 220 W nameplate. MEASURED peak ≈ 2.0 kW (very close to
        # nameplate — this string is actually performing well). Bumped to
        # 2.2 so sub-4-min spikes + solstice gains stay under model.
        "effective_peak_kw": 2.2,
        "capacity_kw": 2.1,
        "panel_count": 10,          # 5 per side
        "panel_layout": [5, 1],     # per-side layout: 5 cols × 1 row
        "color": 0x27ae60,          # Green
    },
]

# =============================================================================
# DERIVED VALUES (computed from config above - do not edit)
# =============================================================================
TOTAL_ARRAY_KW = sum(arr["capacity_kw"] for arr in SOLAR_ARRAYS)

# Combined battery pack capacity (32 kWh Docan + 14 kWh EG4 paralleled)
TOTAL_BATTERY_KWH = 46.0

# Roof properties derived from roof arrays
_roof_arrays = [a for a in SOLAR_ARRAYS if a.get("type") == "roof"]
ROOF_TILT = _roof_arrays[0]["tilt"] if _roof_arrays else 10.0

# Ridgeline: perpendicular to SW roof face (SW azimuth - 90°)
_sw_array = next((a for a in SOLAR_ARRAYS if "SW" in a["name"]), None)
RIDGELINE_AZIMUTH = (_sw_array["azimuth"] - 90) if _sw_array else 127.0

# System efficiency — aspirational model.
# Represents the best realistic case if everything were optimal:
#   - Panels perfectly clean (no soiling losses)
# SYSTEM_EFFICIENCY here is just inverter + DC-side wiring + minor module
# mismatch. The DOMINANT loss in this install is sub-optimal MPPT voltage
# (we run ~250 V strings into an inverter whose efficiency peaks at
# ~400-500 V Vmp). That's modeled separately per-array via
# mppt_voltage_efficiency() below so the cause is identifiable, not
# rolled into a fudge constant.
SYSTEM_EFFICIENCY = 0.96

# Standard Bird-Hulstrom clear-sky transmittance. The user has confirmed
# Oakland-area sky is consistently clear; no atmospheric-availability
# fudge factor on top of this.
ATMOS_TRANSMITTANCE = 0.70

# Diffuse fraction of GHI. Very clear-sky values (high-altitude desert
# or coastal CA with clear marine air) can be as low as 0.05. User
# confirmed Oakland skies are consistently very clear.
DIFFUSE_FRACTION_OF_GHI = 0.05


def inverter_load_efficiency(load_fraction: float) -> float:
    """Inverter conversion efficiency as a function of output load.

    Documented in every inverter datasheet — at low loads, fixed
    switching + control losses dominate. CEC weighted-average
    methodology accounts for this. Typical curve:

        load <  2%   :  shutdown (deep idle, no output)
        load <  5%   :  ~75% efficient (mostly burning fixed loss)
        load < 10%   :  ~88%
        load < 20%   :  ~93%
        load < 30%   :  ~96%
        load >= 30%  :  ~97% (peak)

    This makes morning/evening hours significantly less productive
    than the cos_incidence math alone would suggest — the real
    reason daily integrals are lower than astronomical-day-length
    × peak-power implies.
    """
    if load_fraction < 0.02:
        return 0.0
    if load_fraction < 0.05:
        return 0.50 + (load_fraction - 0.02) / 0.03 * (0.75 - 0.50)
    if load_fraction < 0.10:
        return 0.75 + (load_fraction - 0.05) / 0.05 * (0.88 - 0.75)
    if load_fraction < 0.20:
        return 0.88 + (load_fraction - 0.10) / 0.10 * (0.93 - 0.88)
    if load_fraction < 0.30:
        return 0.93 + (load_fraction - 0.20) / 0.10 * (0.96 - 0.93)
    return 0.97


def mppt_voltage_efficiency(vmp: float) -> float:
    """Efficiency of the inverter's DC-DC stage at a given string Vmp.

    FlexBOSS21 (and most string hybrids) hit peak DC-DC efficiency in
    the 400-500 V Vmp range. Below that the converter has to step down
    a wider ratio (Vmp → ~50 V battery bus) and the per-watt switching
    losses go up. Our system runs ~245-256 V on MPPT1/2 and ~135 V on
    MPPT3, well below optimal — that's the dominant non-physics loss.

    Approximate curve (piecewise-linear S-shape):
        ≤80 V       : 0           (well below turn-on)
        80 → 150 V  : 0.55 → 0.72 (just past turn-on; lossy)
        150 → 250 V : 0.72 → 0.85
        250 → 400 V : 0.85 → 0.97 (climbing to sweet spot)
        400 → 580 V : 0.97 → 0.99 (sweet spot)
        >580 V      : 0           (over-voltage cutoff)
    """
    if vmp <= 80 or vmp > 580:
        return 0.0
    if vmp <= 150:
        return 0.55 + (vmp - 80) / (150 - 80) * (0.72 - 0.55)
    if vmp <= 250:
        return 0.72 + (vmp - 150) / (250 - 150) * (0.85 - 0.72)
    if vmp <= 400:
        return 0.85 + (vmp - 250) / (400 - 250) * (0.97 - 0.85)
    return 0.97 + (vmp - 400) / (580 - 400) * (0.99 - 0.97)

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
FORCED_DISCHARGE_SOC_FLOOR = 70     # % (HOLD_FORCED_DISCHG_SOC_LIMIT)
FORCED_DISCHARGE_POWER_KW  = 8      # kW (HOLD_FORCED_DISCHG_POWER_CMD)
SYSTEM_CHARGE_SOC_LIMIT = 95        # %
DISCHARGE_CUTOFF_SOC = 5            # % (HOLD_DISCHG_CUT_OFF_SOC_EOD)
FEED_IN_GRID_POWER_KW = 8           # kW max export (HOLD_FEED_IN_GRID_POWER_PERCENT)

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


def rates_at(ts: datetime) -> tuple[float, float]:
    """Return (import_rate, export_rate) effective at exactly this timestamp,
    accounting for PG&E TOU windows + Ava bonus window."""
    h = ts.hour + ts.minute / 60.0
    ip, io, ep, eo = get_rates_for_now(ts)
    in_peak = PGE_PEAK_HOURS[0] <= h < PGE_PEAK_HOURS[1]
    in_ava = AVA_BONUS_HOURS[0] <= h < AVA_BONUS_HOURS[1]
    import_rate = ip if in_peak else io
    export_rate = ep if in_peak else eo
    if in_ava:
        export_rate += AVA_BONUS_USD_PER_KWH
    return import_rate, export_rate

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
    transmittance = ATMOS_TRANSMITTANCE ** (air_mass ** 0.678)
    return 1361.0 * transmittance


def aoi_modifier(cos_incidence: float) -> float:
    """ASHRAE incidence-angle modifier for panel-glass surface reflection.

    cos_incidence projects the beam onto the panel (the geometric piece);
    this captures the additional optical loss when the beam hits the glass
    at an oblique angle and more of it gets reflected away. b0 = 0.05 is
    the standard value for glass-encapsulated crystalline Si.

    At normal incidence (cos=1) returns ~1.0. By cos=0.5 (60° AOI) it's
    ~0.95. By cos=0.2 (78° AOI) it's ~0.80. Very oblique = sharp drop.
    """
    if cos_incidence <= 0.05:
        return 0.0
    return max(0.0, 1.0 - 0.05 * (1.0 / cos_incidence - 1.0))


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

    # Direct beam: project onto panel + apply AOI reflection modifier.
    # This is critical for low-sun hours — without IAM, a 10° tilt array
    # gets unrealistically much output during morning/evening because the
    # geometric cos_incidence stays nonzero but the actual glass-reflection
    # loss isn't modeled.
    direct = dni * cos_incidence * aoi_modifier(cos_incidence)
    ghi = dni * math.sin(sun_alt_rad)
    diffuse = ghi * DIFFUSE_FRACTION_OF_GHI * (1 + math.cos(panel_tilt_rad)) / 2
    return direct + diffuse


# --- Module-side corrections applied to the clear-sky panel-output model ----

# Si-PV temperature coefficient. Datasheet typical = -0.4%/°C above STC (25°C).
# Cell temp estimated from NOCT model: T_cell = T_ambient + (NOCT-20) * G/800.
# We don't have an outdoor temp sensor, so we use a fixed ambient assumption
# that's roughly right for our coastal CA climate. This costs us accuracy on
# very hot or very cold days but the structure is here to plug in real
# ambient data later (Open-Meteo or HA weather entity).
PV_TEMP_COEFF_PCT_PER_C = -0.40
ASSUMED_AMBIENT_TEMP_C = 22.0      # mild coastal CA default
PANEL_NOCT_C = 45.0                # typical for residential Si modules
STC_TEMP_C = 25.0


def pv_temperature_derate(irradiance_w_per_m2: float, ambient_c: float = ASSUMED_AMBIENT_TEMP_C) -> float:
    """Multiplier (0..1) representing power loss due to panel heating.

    NOCT model: T_cell = T_ambient + (NOCT-20) × G/800 W/m².
    At 1000 W/m² on a 22 °C day, panels reach ~53 °C → 0.4%/°C × 28 °C ≈ 11%
    loss versus the clear-sky DNI value.
    """
    if irradiance_w_per_m2 <= 0:
        return 1.0
    t_cell = ambient_c + (PANEL_NOCT_C - 20) * irradiance_w_per_m2 / 800.0
    delta = t_cell - STC_TEMP_C
    return max(0.5, 1.0 + delta * PV_TEMP_COEFF_PCT_PER_C / 100.0)


# Cloud-cover factor. We don't currently hit a weather API. Set to 1.0
# (perfectly clear-sky) by default. Override in compute_expected_power if a
# weather source is plugged in later — e.g. fetch open-meteo "cloud_cover_low"
# and map 0%→1.0, 100%→~0.15.
DEFAULT_CLOUD_FACTOR = 1.0


# =============================================================================
# INVERTER DATA FETCHING
# =============================================================================

import threading

# Global cache for inverter data and persistent client
_inverter_cache = {
    "data": None,
    "last_update": None,
    "client": None,
    "inverter": None,
    "loop": None
}

# Flask is multi-threaded — serialize access to the shared asyncio loop so two
# concurrent requests can't both call run_until_complete() (which would throw
# "This event loop is already running" on the second one).
_LOOP_LOCK = threading.Lock()


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
            "forced_discharge_power_kw": FORCED_DISCHARGE_POWER_KW,
            "system_charge_soc_limit": SYSTEM_CHARGE_SOC_LIMIT,
            "discharge_cutoff_soc": DISCHARGE_CUTOFF_SOC,
            "feed_in_power_kw": FEED_IN_GRID_POWER_KW,
        },
        "timeline": _build_schedule_timeline(now),
    }


def _build_schedule_timeline(now: datetime) -> list[dict]:
    """Build today's schedule from "now" through end-of-day (no tomorrow).

    The day breaks into 4 logical phases that map to the PG&E TOU
    structure + the forced-discharge window:

      00:00 – 15:00  PV Charge Priority    (off-peak;   PV fills battery to 95%)
      15:00 – 16:00  self-consumption      (partial peak; hold battery for peak)
      16:00 – 21:00  Forced Discharge      (peak;       8 kW target, ≥70% floor)
      21:00 – 24:00  self-consumption      (partial peak;  battery balances load)

    Past phases are skipped. The first remaining phase is clipped to start
    at "now" so the active row shows the time-until-next-transition. The
    list stops at midnight — tomorrow's slots are NOT included.

    Returns a list of dicts: {start, end, label, detail, kind, active}.
    """
    h_now = now.hour + now.minute / 60.0
    fd_start, fd_end = FORCED_DISCHARGE_WINDOW
    pp_aft_start, pp_aft_end = PGE_PARTIAL_PEAK_HOURS_AFT
    pp_eve_start, pp_eve_end = PGE_PARTIAL_PEAK_HOURS_EVE

    fd_detail = (f"{FORCED_DISCHARGE_POWER_KW} kW target · ≥{FORCED_DISCHARGE_SOC_FLOOR}% "
                 f"SOC floor")

    phases = [
        (0,            pp_aft_start, "PV Charge Priority", "pv_charge",
         f"PV → battery ({SYSTEM_CHARGE_SOC_LIMIT}% cap) → grid"),
        (pp_aft_start, pp_aft_end,   "self-consumption",   "self_consumption",
         "hold battery (partial peak)"),
        (fd_start,     fd_end,       "Forced Discharge",   "forced_discharge",
         fd_detail),
        (pp_eve_start, pp_eve_end,   "self-consumption",   "self_consumption",
         "battery + PV balance load (partial peak)"),
    ]

    def _fmt(h):
        h_mod = h % 24
        hh = int(h_mod)
        mm = int(round((h_mod - hh) * 60))
        if mm == 60:
            hh = (hh + 1) % 24
            mm = 0
        if hh == 24:
            return "12a"   # midnight, end of day
        suffix = "p" if hh >= 12 else "a"
        h12 = hh % 12 or 12
        return f"{h12}:{mm:02d}{suffix}" if mm else f"{h12}{suffix}"

    out = []
    for start, end, label, kind, detail in phases:
        if end <= h_now:
            continue  # already past
        is_active = start <= h_now < end
        slot_start = max(start, h_now)
        out.append({
            "start":  _fmt(slot_start),
            "end":    _fmt(end),
            "label":  label,
            "detail": detail,
            "kind":   kind,
            "active": is_active,
        })
    return out


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
            # battery_temperature deliberately omitted — sensor reports a stuck
            # ~2°C value that's nowhere near real cell temp. Don't surface it.
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
    # Reuse the same event loop to keep client connections alive. Serialize via
    # _LOOP_LOCK so two Flask threads can't drive the same loop concurrently.
    with _LOOP_LOCK:
        if _inverter_cache["loop"] is None or _inverter_cache["loop"].is_closed():
            _inverter_cache["loop"] = asyncio.new_event_loop()
            asyncio.set_event_loop(_inverter_cache["loop"])
        data = _inverter_cache["loop"].run_until_complete(fetch_inverter_data())
        # Stash for cross-endpoint reads (e.g. /api/events wants the
        # daily import counter without re-fetching)
        if data is not None:
            _inverter_cache["data"] = data
            _inverter_cache["last_update"] = datetime.now()
        return data


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


# Watchdog: every 3 hours, drop the asyncio loop + inverter cache so the next
# request rebuilds them fresh. Cheaper than exiting + container restart, and
# clears any accumulated state (stale session cookies, leaked tasks, etc.).
_WATCHDOG_INTERVAL_SEC = 3 * 60 * 60


def _watchdog_reset():
    """Reset the inverter cache + asyncio loop in a thread-safe way."""
    global _inverter_cache
    with _LOOP_LOCK:
        old_loop = _inverter_cache.get("loop")
        _inverter_cache["client"] = None
        _inverter_cache["inverter"] = None
        _inverter_cache["loop"] = None
        if old_loop and not old_loop.is_closed():
            try:
                old_loop.close()
            except Exception as e:
                print(f"watchdog: error closing loop: {e}", flush=True)
    print(f"watchdog: reset inverter cache at {datetime.now().isoformat()}",
          flush=True)


def _watchdog_loop():
    """Background thread that triggers the watchdog reset every N seconds."""
    import time as _time
    while True:
        _time.sleep(_WATCHDOG_INTERVAL_SEC)
        try:
            _watchdog_reset()
        except Exception as e:
            print(f"watchdog: error during reset: {e}", flush=True)


_watchdog_thread = threading.Thread(target=_watchdog_loop, daemon=True)
_watchdog_thread.start()

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
        /* Single-line MPPT row: name on left, V/A in middle, W on right.
           Grid layout (not flex) so the voltage + power columns align
           across all three rows — the longer "(NE · 37° / SW · 217°)"
           label on row 3 used to push them out of position. */
        .mppt-line {
            display: grid;
            grid-template-columns: minmax(0, 1fr) 95px 60px;
            align-items: baseline;
            gap: 8px;
            padding: 4px 0 2px;
            font-size: 0.95em;
        }
        .mppt-line .mppt-name {
            color: #aaa;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        .mppt-line .mppt-voltage-text {
            text-align: right;
            color: #3498db;
            font-size: 0.85em;
            font-variant-numeric: tabular-nums;
            opacity: 0.85;
            white-space: nowrap;
        }
        .mppt-line .mppt-voltage-text.good { color: #2ecc71; }
        .mppt-line .mppt-voltage-text.warning { color: #f39c12; }
        .mppt-line .mppt-voltage-text.bad { color: #e74c3c; }
        .mppt-line .mppt-power-text {
            text-align: right;
            color: #f39c12;
            font-weight: 600;
            font-variant-numeric: tabular-nums;
            white-space: nowrap;
        }
        .power-summary {
            display: grid;
            /* 4 cards side-by-side on full-width kiosk, collapses to 2x2 on
               narrow viewports (each card has a 180px floor).
               gap: 0 — the per-card 3px border (matching page bg) provides
               the visual separation AND doubles as a color-coded state
               indicator (see .power-card.border-* below). */
            grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
            gap: 0;
            padding: 0;
            background: transparent;
        }
        /* Responsive card grid: auto-fits as many ~260px columns as the
           container width allows. On narrow sidebars (e.g. <540px) this
           collapses to a single column; on the full-width HASS kiosk view
           it expands to 2 / 3 / 4 columns side-by-side. */
        .card-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
            gap: 12px;
            margin: 0;
        }
        .card-grid > .section {
            margin: 0;
        }
        /* Two-column row used for Import Events (left) + PV Arrays (right).
           Collapses to a single column on narrow viewports. */
        .two-col-row {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
            gap: 12px;
            margin-bottom: 15px;
        }
        .two-col-row > .section {
            margin: 0;
        }
        .power-card {
            min-width: 0;
            background: rgba(255,255,255,0.05);
            border-radius: 10px;
            padding: 10px 8px;
            /* 3px border doubles as state indicator. Default matches the
               panel background (#0a0a14-ish via rgba(0,0,0,0.7) over the
               body gradient) so the cards appear "padded" by black. JS
               adds .border-green / -orange / -red based on the card's
               state (see updateCardBorders in fetchData). */
            border: 3px solid #000;
            transition: border-color 0.4s ease;
        }
        .power-card.border-green  { border-color: #2ecc71; }
        .power-card.border-orange { border-color: #e67e22; }
        .power-card.border-red    { border-color: #e74c3c; }
        .power-card .label {
            color: #888;
            font-size: 0.62em;
            text-transform: uppercase;
            letter-spacing: 0.06em;
        }
        .power-card-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 6px;
        }
        .sun-times,
        .card-meta-tag {
            color: #aaa;
            font-size: 0.62em;
            font-variant-numeric: tabular-nums;
            letter-spacing: 0.02em;
            white-space: nowrap;
        }
        /* Sun + moon icons next to sunrise/sunset times. The unicode glyphs
           ☀ ☾ render as text so we can colorize them via CSS. */
        .sun-times .sun-icon  { color: #f1c40f; font-size: 1.15em; }
        .sun-times .moon-icon { color: #5d6d9c; font-size: 1.15em; }
        /* Battery upper-right SOC · V · A — all three bold for legibility */
        .card-meta-tag.battery-tag {
            font-weight: 700;
            color: #ddd;
        }
        /* Sign-colored battery current: green when charging, blue when discharging.
           Wiring-temperature warning rules override the direction color:
              |amps| > 170A → orange (heads up)
              |amps| > 180A → red    (check wiring) */
        .card-meta-tag.battery-tag #battery-current.charging    { color: #2ecc71; }
        .card-meta-tag.battery-tag #battery-current.discharging { color: #3498db; }
        .card-meta-tag.battery-tag #battery-current.idle        { color: #888; }
        .card-meta-tag.battery-tag #battery-current.warn-warm { color: #e67e22; }
        .card-meta-tag.battery-tag #battery-current.warn-hot  { color: #e74c3c; }
        .power-card .value {
            margin-top: 6px;
            font-size: 2.1em;
            font-weight: 700;
            line-height: 1.05;
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
        .power-card .value.home { color: #1abc9c; }
        .power-card .value.home.idle { color: #888; }
        .power-card-meta {
            display: flex;
            justify-content: space-between;
            margin-top: 4px;
            color: #888;
            font-size: 0.62em;
        }
        /* Daily-total meta — bigger and more prominent than the "of 12 kW now"
           line. Tabular nums so the digits don't shift between samples. */
        .power-card-meta.daily-meta {
            font-size: 0.95em;
            font-weight: 600;
            color: #ddd;
            font-variant-numeric: tabular-nums;
            margin-top: 6px;
        }
        .power-card-meta.daily-meta .meta-left  { color: #e74c3c; }   /* import / discharge side */
        .power-card-meta.daily-meta .meta-right { color: #2ecc71; }   /* export / charge side */
        .power-card-meta.daily-meta .meta-suffix {
            color: #777;
            font-size: 0.7em;
            font-weight: 400;
            margin-left: 4px;
        }
        /* Bidirectional bar: split at center, fills extend outward.
           Used for Battery (charge/discharge) and Grid (export/import). */
        .performance-bar.bidi {
            position: relative;
            overflow: visible;       /* allow center marker to extend above/below */
            background: rgba(255,255,255,0.08);
        }
        .performance-bar.bidi::before {
            /* Vertical line at the 50% center mark */
            content: '';
            position: absolute;
            left: 50%;
            top: -2px;
            bottom: -2px;
            width: 1px;
            background: rgba(255,255,255,0.4);
            z-index: 2;
        }
        .performance-bar.bidi .fill-left,
        .performance-bar.bidi .fill-right {
            position: absolute;
            top: 0;
            height: 100%;
            transition: width 0.4s ease;
            background: transparent;     /* set per-kind below */
        }
        .performance-bar.bidi .fill-left  { right: 50%; border-radius: 4px 0 0 4px; }
        .performance-bar.bidi .fill-right { left:  50%; border-radius: 0 4px 4px 0; }
        /* Grid bidi: left = import = red, right = export = green */
        .performance-bar.bidi.grid .fill-left  { background: #e74c3c; }
        .performance-bar.bidi.grid .fill-right { background: #2ecc71; }
        /* Battery bidi: left = discharge = blue, right = charge = green */
        .performance-bar.bidi.battery .fill-left  { background: #3498db; }
        .performance-bar.bidi.battery .fill-right { background: #2ecc71; }
        /* The daily-bidi bar is shorter, like the existing daily-bar */
        .performance-bar.bidi.daily-bar {
            height: 6px;
            margin-top: 7px;
        }
        .power-card-summary {
            margin-top: 4px;
            font-size: 0.66em;
            color: #aaa;
            font-variant-numeric: tabular-nums;
            line-height: 1.3;
        }
        .power-card-summary .pos { color: #2ecc71; }
        .power-card-summary .neg { color: #e74c3c; }
        /* Events card header: card title on left, current operation
           mode + inverter status on the right. Mirrors .power-card-header
           styling on the top cards. */
        .events-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 4px;
        }
        .events-header-label {
            color: #888;
            font-size: 0.62em;
            text-transform: uppercase;
            letter-spacing: 0.06em;
        }
        .events-header-mode {
            color: #ddd;
            font-size: 0.78em;
            font-variant-numeric: tabular-nums;
        }

        /* Events list: oldest events scroll up out of view, newest at the
           bottom (always in view because we scrollTop=scrollHeight after
           every render). Container is the same height as the MPPT panel
           so they balance side-by-side. */
        #import-events-list {
            max-height: 220px;
            overflow-y: auto;
            scroll-behavior: smooth;
        }

        /* Single-line daily $ summary under the grid card's daily-bar.
           Layout: imp formula on left, net $ in center, exp formula on right.
           The kWh number is full daily-meta size + bold; the "× X.X¢" rate
           annotation is smaller and lighter weight (like a meta-suffix). */
        .grid-money-row { font-variant-numeric: tabular-nums; color: #888; }
        .grid-money-row .money-imp { color: #e74c3c; }
        .grid-money-row .money-exp { color: #2ecc71; }
        /* Only the kWh NUMBER is big+bold; the unit and rate annotation
           stay small/light so the number stands out as the primary data. */
        .grid-money-row .money-imp .kwh-num,
        .grid-money-row .money-exp .kwh-num {
            font-weight: 700;
            font-size: 1em;             /* full daily-meta size */
        }
        .grid-money-row .money-imp .kwh-unit,
        .grid-money-row .money-exp .kwh-unit {
            font-weight: 400;
            font-size: 0.55em;
            opacity: 0.65;
            margin-left: 2px;
        }
        .grid-money-row .money-imp .rate-mult,
        .grid-money-row .money-exp .rate-mult {
            font-weight: 400;
            font-size: 0.55em;
            opacity: 0.65;
            margin-left: 4px;
        }
        /* Net $ in center — slightly smaller than the side numbers so it
           reads as a derived total rather than a primary data point. */
        .grid-money-row .money-net {
            color: #ddd;
            font-weight: 600;
            font-size: 0.86em;
        }
        .grid-money-row .money-net.profit { color: #2ecc71; }
        .grid-money-row .money-net.loss   { color: #e74c3c; }
        .event-row {
            display: flex;
            gap: 10px;
            padding: 3px 0;             /* compact — was 6px */
            border-bottom: 1px solid rgba(255,255,255,0.05);
            font-size: 0.85em;
            align-items: baseline;
            transition: opacity 0.2s ease;
        }
        .event-row:last-child { border-bottom: none; }
        /* Older-than-today events: heavily muted so today stands out. */
        .event-row.event-old {
            opacity: 0.4;
            font-size: 0.78em;
        }
        .event-row.event-old .event-reason { font-weight: 400; }
        /* Unaccounted-imports placeholder row: synthetic, no real time, so
           the time column shows --:--? and everything is greyed. */
        .event-row.event-unaccounted {
            opacity: 0.55;
        }
        .event-row.event-unaccounted .event-time { color: #666; }
        .event-row.event-unaccounted .event-reason {
            color: #aaa;
            font-weight: 400;
        }
        /* BMS limit-change events: routine, no special emphasis. Same
           neutral grey-ish color as the meta text; normal weight. */
        .event-row.event-bms .event-reason {
            font-weight: 400;
            color: #aaa;
        }
        /* Schedule timeline events interleaved into the events list. */
        .event-row.event-sched .event-reason { font-weight: 600; }
        .event-row.event-sched.sched-pv_charge        .event-reason { color: #2ecc71; }
        .event-row.event-sched.sched-forced_discharge .event-reason { color: #f39c12; }
        .event-row.event-sched.sched-self_consumption .event-reason { color: #95a5a6; }
        .event-row.event-sched-active {
            background: rgba(255,255,255,0.08);
            border-left: 3px solid #f1c40f;
            padding-left: 8px;
        }
        .event-time {
            color: #aaa;
            min-width: 70px;
            font-variant-numeric: tabular-nums;
        }
        /* Reason on the left, meta annotation on the right — single line per
           event for compactness. Meta truncates with ellipsis if too long. */
        .event-details {
            flex: 1;
            min-width: 0;
            display: flex;
            justify-content: space-between;
            align-items: baseline;
            gap: 6px;
        }
        .event-reason {
            color: #e67e22;
            font-weight: 600;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        .event-meta {
            color: #888;
            font-size: 0.85em;
            text-align: right;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            flex-shrink: 1;
        }
        .event-empty {
            color: #2ecc71;
            text-align: center;
            padding: 12px 0;
            font-size: 0.9em;
        }
        .performance-bar {
            position: relative;        /* positioning context for .progress-indicator */
            height: 8px;
            background: rgba(255,255,255,0.1);
            border-radius: 4px;
            overflow: hidden;
            margin-top: 10px;
        }
        /* White vertical tick on a daily-bar marking "where you should be".
           Used by the PV bar (cumulative model integral) and the Home bar
           (linear time-of-day reference). Drawn on top of the .fill. */
        .performance-bar .progress-indicator {
            position: absolute;
            top: 0; bottom: 0;            /* full bar height (bar has overflow:hidden) */
            width: 2px;
            background: rgba(255,255,255,0.95);
            z-index: 2;
            pointer-events: none;
            transform: translateX(-1px);  /* center the 2px line on the % point */
            transition: left 0.5s ease;
        }
        .performance-bar.daily-bar {
            height: 6px;
            margin-top: 7px;
            opacity: 0.7;
        }
        /* Solid-color bar fills. Color reflects the card's state, not a
           rainbow gradient. State classes are applied by JS to match the
           same logic as the card border (PV: % of expected; Home: load
           magnitude). */
        .performance-bar .fill {
            height: 100%;
            background: #1abc9c;          /* default solid (used by Home) */
            transition: width 0.5s ease, background-color 0.3s ease;
        }
        /* PV fill — same thresholds as the PV card border */
        .performance-bar .fill.pv-good { background: #2ecc71; }
        .performance-bar .fill.pv-warn { background: #e67e22; }
        .performance-bar .fill.pv-bad  { background: #e74c3c; }
        /* Home fill — same thresholds as the Home card border */
        .performance-bar .fill.home-warm { background: #e67e22; }
        .performance-bar .fill.home-hot  { background: #e74c3c; }
        .mppt-zero .mppt-row {
            display: none;
        }
        /* MPPT power bar — fraction of that array's effective peak (the
           measured max it ever produces). Same thresholds as the PV card
           border so the colors match:
              < 50%       red    — under-performing
              50-75%      orange — partial
              >= 75%      green  — close to peak */
        .mppt-voltage-bar-row {
            padding: 0 0 8px 0;
        }
        .mppt-voltage-bar {
            position: relative;
            height: 4px;
            background: rgba(255,255,255,0.08);
            border-radius: 2px;
            overflow: hidden;
            margin: 2px 0 0 0;
        }
        .mppt-voltage-bar .fill {
            position: absolute;
            top: 0;
            left: 0;
            height: 100%;
            border-radius: 2px;
            transition: width 0.4s ease, background-color 0.4s ease;
        }
        .mppt-voltage-bar .fill.pct-low  { background: #e74c3c; }
        .mppt-voltage-bar .fill.pct-mid  { background: #e67e22; }
        .mppt-voltage-bar .fill.pct-high { background: #2ecc71; }
        .mppt-zero-message {
            display: none;
            color: #888;
            font-size: 0.85em;
            padding: 2px 0 4px;
        }
        .mppt-zero .mppt-zero-message { display: block; }
        .history-chart-wrap {
            position: relative;
            height: 280px;
            margin-top: 4px;
        }
        /* Schedule timeline is now interleaved into the events list
           via event-row.event-sched.* — no standalone container. */
        /* Combined import/export rate display: "$0.380 ⬅  ➡ $0.025" */
        .nem-rates {
            display: inline-flex;
            align-items: center;
            gap: 4px;
            font-variant-numeric: tabular-nums;
        }
        /* The "active" side (whichever direction grid power is flowing) is
           bold; the inactive side stays the same color but lighter weight.
           JS toggles .active on whichever side is currently relevant. */
        .nem-rates .rate-import { color: #e74c3c; font-weight: 400; opacity: 0.7; }
        .nem-rates .rate-export { color: #2ecc71; font-weight: 400; opacity: 0.7; }
        .nem-rates .rate-import.active,
        .nem-rates .rate-export.active { font-weight: 700; opacity: 1; }
        .nem-rates .rate-arrow-in  { color: #e74c3c; font-size: 0.85em; opacity: 0.85; }
        .nem-rates .rate-arrow-out { color: #2ecc71; font-size: 0.85em; opacity: 0.85; }
        .nem-rates .rate-ava-bonus {
            color: #2ecc71;
            font-size: 0.72em;
            font-weight: 600;
            margin-left: 6px;
        }
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
            <div class="section power-summary">
                <div class="power-card" id="pv-card">
                    <div class="power-card-header">
                        <div class="label">PV Power</div>
                        <div class="sun-times">
                            <span class="sun-icon">☀</span><span id="sunrise">--:--</span>
                            &nbsp;<span class="moon-icon">☾</span><span id="sunset">--:--</span>
                        </div>
                    </div>
                    <div class="value pv" id="total-pv">--</div>
                    <div class="performance-bar">
                        <div class="fill" id="pv-load-fill" style="width: 0%"></div>
                    </div>
                    <div class="power-card-meta">
                        <span id="pv-load-text">--%</span>
                        <span id="pv-load-target">of -- kW expected</span>
                    </div>
                    <div class="performance-bar daily-bar">
                        <div class="fill" id="pv-daily-fill" style="width: 0%"></div>
                        <div class="progress-indicator" id="pv-daily-indicator" style="left: 0%" title="Where the model says you should be by now"></div>
                    </div>
                    <div class="power-card-meta daily-meta">
                        <span id="pv-daily-text" style="color:#f39c12;">--</span>
                        <span class="meta-suffix" id="pv-daily-target">of -- kWh expected</span>
                    </div>
                </div>
                <div class="power-card" id="battery-card">
                    <div class="power-card-header">
                        <div class="label">Battery</div>
                        <div class="card-meta-tag battery-tag">
                            <span id="battery-soc">--%</span>
                            &nbsp;<span id="battery-voltage">-- V</span>
                            &nbsp;<span id="battery-current">-- A</span>
                        </div>
                    </div>
                    <div class="value battery idle" id="battery-top-power">--</div>
                    <div class="performance-bar bidi battery">
                        <div class="fill-left"  id="battery-load-left"  style="width: 0%"></div>
                        <div class="fill-right" id="battery-load-right" style="width: 0%"></div>
                    </div>
                    <div class="power-card-meta">
                        <span id="battery-load-text">--%</span>
                        <span>of ±12 kW now</span>
                    </div>
                    <div class="performance-bar bidi battery daily-bar">
                        <div class="fill-left"  id="battery-daily-left"  style="width: 0%"></div>
                        <div class="fill-right" id="battery-daily-right" style="width: 0%"></div>
                    </div>
                    <div class="power-card-meta daily-meta">
                        <span class="meta-left"  id="battery-daily-discharged">-- kWh<span class="meta-suffix">out</span></span>
                        <span class="meta-right" id="battery-daily-charged">-- kWh<span class="meta-suffix">in</span></span>
                    </div>
                </div>
                <div class="power-card" id="grid-card">
                    <div class="power-card-header">
                        <div class="label">Grid</div>
                        <div class="card-meta-tag">
                            🌡 <span id="inverter-temp">-- °C</span>
                            &nbsp;<span id="tou-period-icon" title="TOU period">⚪</span>
                            &nbsp;<span id="dollars-per-hour">--</span>
                        </div>
                    </div>
                    <div class="value grid idle" id="grid-top-power">--</div>
                    <div class="performance-bar bidi grid">
                        <div class="fill-left"  id="grid-load-left"  style="width: 0%"></div>
                        <div class="fill-right" id="grid-load-right" style="width: 0%"></div>
                    </div>
                    <div class="power-card-meta">
                        <span id="grid-load-text">--%</span>
                        <span>of ±12 kW now</span>
                    </div>
                    <div class="performance-bar bidi grid daily-bar">
                        <div class="fill-left"  id="grid-daily-left"  style="width: 0%"></div>
                        <div class="fill-right" id="grid-daily-right" style="width: 0%"></div>
                    </div>
                    <!-- Daily summary: imports × import-rate · net $ · exports × export-rate (one line) -->
                    <div class="power-card-meta daily-meta grid-money-row">
                        <span class="meta-left money-imp"  id="grid-daily-imported">--</span>
                        <span class="money-net"            id="grid-money-net">--</span>
                        <span class="meta-right money-exp" id="grid-daily-exported">--</span>
                    </div>
                </div>
                <div class="power-card" id="home-card">
                    <div class="label">Home</div>
                    <div class="value home idle" id="home-top-power">--</div>
                    <div class="performance-bar">
                        <div class="fill home" id="home-load-fill" style="width: 0%"></div>
                    </div>
                    <div class="power-card-meta">
                        <span id="home-load-text">--%</span>
                        <span>of 20 kW now</span>
                    </div>
                    <div class="performance-bar daily-bar">
                        <div class="fill home" id="home-daily-fill" style="width: 0%"></div>
                        <div class="progress-indicator" id="home-daily-indicator" style="left: 0%" title="Linear time-of-day reference (45 kWh/24h)"></div>
                    </div>
                    <div class="power-card-meta daily-meta">
                        <span id="home-daily-text" style="color:#1abc9c;">--</span>
                        <span class="meta-suffix" id="home-daily-target">of 45 kWh</span>
                    </div>
                </div>
            </div>

            <!-- Two-column row: import events on the left, PV arrays on the right -->
            <div class="two-col-row">

            <div class="section">
                <div class="events-header">
                    <span class="events-header-label">Events</span>
                    <span class="events-header-mode">
                        <span id="op-mode">--</span>
                        &nbsp;·&nbsp;
                        <span class="good" id="inverter-status">--</span>
                    </span>
                </div>
                <div id="import-events-list">
                    <div class="event-empty">No imports in the last 24h ✓</div>
                </div>
            </div>

            <div class="section" id="pv-arrays-section">
                <div class="mppt-zero-message">All MPPT inputs idle (0 W)</div>
                <div class="mppt-line mppt-row">
                    <span class="mppt-name"><span class="mppt-dot sw"></span> MPPT 1 (SW · 217°)</span>
                    <span class="mppt-voltage-text" id="pv1-detail">-- V / -- A</span>
                    <span class="mppt-power-text" id="pv1">-- W</span>
                </div>
                <div class="mppt-row mppt-voltage-bar-row">
                    <div class="mppt-voltage-bar"><div class="fill" id="pv1-voltage-fill"></div></div>
                </div>
                <div class="mppt-line mppt-row">
                    <span class="mppt-name"><span class="mppt-dot ne"></span> MPPT 2 (NE · 37°)</span>
                    <span class="mppt-voltage-text" id="pv2-detail">-- V / -- A</span>
                    <span class="mppt-power-text" id="pv2">-- W</span>
                </div>
                <div class="mppt-row mppt-voltage-bar-row">
                    <div class="mppt-voltage-bar"><div class="fill" id="pv2-voltage-fill"></div></div>
                </div>
                <div class="mppt-line mppt-row">
                    <span class="mppt-name"><span class="mppt-dot yard"></span> MPPT 3 (NE · 37° / SW · 217°)</span>
                    <span class="mppt-voltage-text" id="pv3-detail">-- V / -- A</span>
                    <span class="mppt-power-text" id="pv3">-- W</span>
                </div>
                <div class="mppt-row mppt-voltage-bar-row">
                    <div class="mppt-voltage-bar"><div class="fill" id="pv3-voltage-fill"></div></div>
                </div>
            </div>

            </div><!-- /.two-col-row -->

            <!-- Mode + Schedule + Rates row removed: mode/status moved to
                 events-header, TOU period + $/hr now in the grid card,
                 schedule transitions interleaved into the events list. -->

            <!-- 24h history chart: PV / Battery SOC / Grid in+out / Home -->
            <div class="section">
                <div class="history-chart-wrap">
                    <canvas id="history-chart"></canvas>
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
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3.0.0/dist/chartjs-adapter-date-fns.bundle.min.js"></script>
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

            // Start data polling — this is what makes the side panel show numbers
            fetchData();
            setInterval(fetchData, 5000);
            // Events poll separately (slower — events change at minute-scale)
            fetchEvents();
            setInterval(fetchEvents, 60000);
            // History chart — refresh every 5 min (4-min source resolution)
            fetchHistory();
            setInterval(fetchHistory, 5 * 60 * 1000);
        }

        let historyChart;
        // One-time Chart.js global defaults so axis ticks, legend, tooltip
        // all share the same font + color (avoids the fuzzy serif fallback).
        if (typeof Chart !== 'undefined') {
            Chart.defaults.font.family = "-apple-system, system-ui, 'Segoe UI', Roboto, sans-serif";
            Chart.defaults.font.size = 12;
            Chart.defaults.color = '#ddd';
        }
        async function fetchHistory() {
            try {
                const r = await fetch('/api/history');
                const data = await r.json();
                renderHistoryChart(data.samples || []);
            } catch (e) {
                console.warn('history fetch failed', e);
            }
        }

        function renderHistoryChart(samples) {
            const canvas = document.getElementById('history-chart');
            if (!canvas || typeof Chart === 'undefined') return;

            // Convert "YYYY-MM-DD HH:MM:SS" → JS Date for the X axis.
            const labels = samples.map(s => new Date(s.t.replace(' ', 'T')));

            // Card-matching colors so the chart legend is self-explanatory.
            const datasets = [
                { label: 'Solar PV',  data: samples.map(s => s.pv_kw),     borderColor: '#f39c12', backgroundColor: 'rgba(243,156,18,0.0)',  yAxisID: 'kw' },
                { label: 'Home',      data: samples.map(s => s.home_kw),   borderColor: '#1abc9c', backgroundColor: 'rgba(26,188,156,0.0)',  yAxisID: 'kw' },
                { label: 'Grid Export', data: samples.map(s => s.export_kw), borderColor: '#2ecc71', backgroundColor: 'rgba(46,204,113,0.0)', yAxisID: 'kw' },
                { label: 'Grid Import', data: samples.map(s => s.import_kw), borderColor: '#e74c3c', backgroundColor: 'rgba(231,76,60,0.0)',  yAxisID: 'kw' },
                { label: 'Battery SOC', data: samples.map(s => s.soc),     borderColor: '#9b59b6', backgroundColor: 'rgba(155,89,182,0.0)',  yAxisID: 'pct', borderDash: [4, 3] },
            ];
            datasets.forEach(d => {
                d.borderWidth = 2;
                d.pointRadius = 0;
                d.pointHoverRadius = 3;
                d.tension = 0.25;
                d.spanGaps = true;
            });

            // Custom plugin: shade the forced-discharge window (16:00-21:00
            // each day) with a subtle red-orange band so you can see when
            // the peak-export schedule is supposed to be active.
            const forcedDischargeShade = {
                id: 'forcedDischargeShade',
                beforeDatasetsDraw(chart) {
                    if (!samples.length) return;
                    const xScale = chart.scales.x;
                    const yScale = chart.scales.kw;
                    if (!xScale || !yScale) return;
                    const ctx = chart.ctx;
                    ctx.save();
                    ctx.fillStyle = 'rgba(231,76,60,0.10)';
                    // Cover yesterday + today's 16:00-21:00 windows
                    const ts0 = new Date(samples[0].t.replace(' ', 'T'));
                    const tsN = new Date(samples[samples.length-1].t.replace(' ', 'T'));
                    const days = [];
                    const cur = new Date(ts0);
                    cur.setHours(0,0,0,0);
                    while (cur <= tsN) {
                        days.push(new Date(cur));
                        cur.setDate(cur.getDate() + 1);
                    }
                    for (const day of days) {
                        const start = new Date(day); start.setHours(16, 0, 0, 0);
                        const end   = new Date(day); end.setHours(21, 0, 0, 0);
                        if (end < ts0 || start > tsN) continue;
                        const xs = xScale.getPixelForValue(start);
                        const xe = xScale.getPixelForValue(end);
                        ctx.fillRect(xs, yScale.top, xe - xs, yScale.bottom - yScale.top);
                    }
                    ctx.restore();
                },
            };

            // High-DPI rendering — fixes the fuzzy/aliased text on retina
            // and HASS-kiosk displays. Chart.js defaults to 1.0 in some
            // environments (luakit, especially) so set it explicitly.
            const dpr = Math.max(window.devicePixelRatio || 1, 2);

            const cfg = {
                type: 'line',
                data: { labels, datasets },
                plugins: [forcedDischargeShade],
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    devicePixelRatio: dpr,
                    animation: false,
                    interaction: { mode: 'index', intersect: false },
                    font: {
                        family: "-apple-system, system-ui, 'Segoe UI', sans-serif",
                    },
                    plugins: {
                        legend: {
                            position: 'top',
                            labels: { color: '#ddd', font: { size: 12 }, boxWidth: 12, padding: 8 },
                        },
                        tooltip: {
                            backgroundColor: 'rgba(20,20,28,0.95)',
                            titleColor: '#fff', bodyColor: '#ddd',
                            borderColor: '#444', borderWidth: 1,
                        },
                    },
                    scales: {
                        x: {
                            type: 'time',
                            time: {
                                unit: 'hour',
                                // 12-hour compact tick format: "1p", "9a"
                                displayFormats: { hour: 'ha' },
                                tooltipFormat: 'MMM d, h:mma',
                            },
                            ticks: { color: '#888', maxRotation: 0 },
                            grid:  { color: 'rgba(255,255,255,0.05)' },
                        },
                        kw: {
                            type: 'linear', position: 'left',
                            title: { display: true, text: 'kW', color: '#888' },
                            ticks: { color: '#888' },
                            grid:  { color: 'rgba(255,255,255,0.06)' },
                        },
                        pct: {
                            type: 'linear', position: 'right',
                            min: 0, max: 100,
                            title: { display: true, text: '%', color: '#9b59b6' },
                            ticks: { color: '#9b59b6' },
                            grid:  { drawOnChartArea: false },
                        },
                    },
                },
            };

            if (historyChart) {
                historyChart.data = cfg.data;
                historyChart.update('none');
            } else {
                historyChart = new Chart(canvas.getContext('2d'), cfg);
            }
        }

        async function fetchEvents() {
            try {
                const r = await fetch('/api/events');
                const data = await r.json();
                const listEl = document.getElementById('import-events-list');
                if (!listEl) return;

                // Daily counter vs sum of import events. The chart-data is
                // 4-min sampled — brief imports between sample points are
                // missed by the events list but caught by the continuous
                // daily counter. Show the delta as a phantom event row.
                // Uses the daily_import_kwh value the server includes in
                // the /api/events response (no race with /api/data).
                let unaccountedNote = '';
                const dailyKwh = (typeof data.daily_import_kwh === 'number')
                    ? data.daily_import_kwh : null;
                if (dailyKwh != null) {
                    const captured = (data.events || [])
                        .filter(e => (e.type || 'import') === 'import')
                        .reduce((s, e) => s + (e.kwh || 0), 0);
                    const gap = dailyKwh - captured;
                    if (gap > 0.005) {
                        unaccountedNote = `<div class="event-row event-unaccounted">
                            <div class="event-time">--:--?</div>
                            <div class="event-details">
                                <div class="event-reason">-${gap.toFixed(3)} kWh imported (unaccounted)</div>
                            </div>
                        </div>`;
                    }
                }

                if (!data.events || data.events.length === 0) {
                    listEl.innerHTML = '<div class="event-empty">No events in the last 24h ✓</div>'
                                       + unaccountedNote;
                    return;
                }
                // Render oldest at top, newest at bottom (chronological). The
                // server returns newest-first so we reverse for display.
                // The container is overflow-scrollable; we scroll to the
                // bottom so the most-recent event is always in view.
                listEl.innerHTML = data.events.slice().reverse().map(renderEvent).join('')
                                   + unaccountedNote;
                listEl.scrollTop = listEl.scrollHeight;
            } catch (err) {
                console.error('events fetch failed:', err);
            }
        }

        function renderEvent(ev) {
            // Events from previous days get the "event-old" class which
            // significantly mutes them so today's events stand out.
            const ageCls = ev.is_today === false ? ' event-old' : '';
            // Schedule transitions (PV charge / forced discharge / etc).
            // Phase label color matches the kind (.sched-* classes).
            if (ev.type === 'schedule') {
                const activeCls = ev.active ? ' event-sched-active' : '';
                return `<div class="event-row event-sched sched-${ev.kind}${activeCls}${ageCls}">
                    <div class="event-time">${ev.start}</div>
                    <div class="event-details">
                        <div class="event-reason">${ev.reason}</div>
                        <div class="event-meta">${ev.detail || ''}</div>
                    </div>
                </div>`;
            }
            // BMS limit-change event (typically correlates with charging stop)
            if (ev.type === 'bms_charge' || ev.type === 'bms_discharge') {
                const dir = ev.new_a < ev.prev_a ? 'down' : 'up';
                // Right-side meta: SOC and battery voltage at the moment
                // the limit changed — helps diagnose whether the BMS was
                // top-balancing (high SOC, high V) vs over-current trip.
                const socStr  = ev.soc  != null ? `SOC ${ev.soc}%` : '';
                const vbatStr = ev.vbat != null ? `${ev.vbat}V`    : '';
                const meta = [socStr, vbatStr].filter(Boolean).join(' · ');
                return `<div class="event-row event-bms event-bms-${dir}${ageCls}">
                    <div class="event-time">${ev.start}</div>
                    <div class="event-details">
                        <div class="event-reason">⚡ ${ev.reason}</div>
                        ${meta ? `<div class="event-meta">${meta}</div>` : ''}
                    </div>
                </div>`;
            }
            // Default: import event
            const timeStr = ev.duration_min > 5 ? `${ev.start}–${ev.end}` : ev.start;
            const socStr = ev.soc != null ? ` · SOC ${ev.soc}%` : '';
            const pvStr = ev.ppv != null && ev.ppv > 100 ? ` · PV ${(ev.ppv/1000).toFixed(1)}kW` : '';
            return `<div class="event-row${ageCls}">
                <div class="event-time">${timeStr}</div>
                <div class="event-details">
                    <div class="event-reason">${ev.reason}</div>
                    <div class="event-meta">~${ev.kwh.toFixed(3)} kWh · peak ${ev.peak_w}W · ${ev.duration_min.toFixed(0)}min${socStr}${pvStr}</div>
                </div>
            </div>`;
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

        const POWER_LOAD_MAX_W = 12000;   // PV / Battery / Grid bar denominator
        const HOME_LOAD_MAX_W  = 20000;   // Home consumption bar denominator

        function numericPower(watts) {
            const value = Number(watts);
            return Number.isFinite(value) ? value : 0;
        }

        function formatSignedPower(watts) {
            const value = numericPower(watts);
            return (value > 0 ? '+' : '') + value.toLocaleString() + 'W';
        }

        function updatePowerLoadCard(kind, watts, maxWOverride) {
            const value = numericPower(watts);
            // PV uses the model-derived expected_power as its denominator
            // (passed in via maxWOverride); other cards use static maxes.
            const maxW = (maxWOverride != null && maxWOverride > 0)
                ? maxWOverride
                : ((kind === 'home') ? HOME_LOAD_MAX_W : POWER_LOAD_MAX_W);
            const loadPercent = Math.min(100, Math.abs(value) / maxW * 100);
            const valueElId = (kind === 'battery') ? 'battery-top-power'
                            : (kind === 'grid')    ? 'grid-top-power'
                            : (kind === 'home')    ? 'home-top-power'
                            : 'total-pv';
            const valueEl = document.getElementById(valueElId);
            const textEl  = document.getElementById(kind + '-load-text');

            const signed = (kind === 'battery' || kind === 'grid');
            valueEl.textContent = signed ? formatSignedPower(value) : formatPower(value);

            if (signed) {
                // Bidirectional bar: each half is 50% wide max, value scales to that.
                const halfPct = Math.min(50, Math.abs(value) / maxW * 50);
                const leftEl  = document.getElementById(kind + '-load-left');
                const rightEl = document.getElementById(kind + '-load-right');
                leftEl.style.width  = (value < 0 ? halfPct : 0).toFixed(1) + '%';
                rightEl.style.width = (value > 0 ? halfPct : 0).toFixed(1) + '%';
                // Centered % text — show signed direction (e.g. "-12%" or "+45%")
                const signedPct = (value === 0) ? '0%'
                                : (value > 0 ? '+' : '-') + loadPercent.toFixed(0) + '%';
                textEl.textContent = signedPct;
            } else {
                // Single-sided bar (PV, Home)
                const fillEl = document.getElementById(kind + '-load-fill');
                fillEl.style.width = loadPercent.toFixed(1) + '%';
                textEl.textContent = loadPercent.toFixed(0) + '%';
            }

            if (kind === 'battery') {
                valueEl.classList.toggle('charging', value > 0);
                valueEl.classList.toggle('discharging', value < 0);
                valueEl.classList.toggle('idle', value === 0);
            } else if (kind === 'grid') {
                valueEl.classList.toggle('exporting', value > 0);
                valueEl.classList.toggle('importing', value < 0);
                valueEl.classList.toggle('idle', value === 0);
            } else if (kind === 'home') {
                valueEl.classList.toggle('idle', value === 0);
            }
        }

        // Color-code each top-card's border + bar fill based on state.
        // Border + fill use the SAME state buckets — fill state is set on
        // both the now-bar and the daily-bar so they color together.
        //
        //   PV:       green if generating ≥75% of expected, orange 50-75%,
        //             red <50% (only when expected > 1 kW so it's not
        //             gating on noise around dawn/dusk).
        //   Battery:  by SOC. ≥70% green, <30% orange, <18% red.
        //   Grid:     by current flow. import>50W red, export>50W green.
        //   Home:     by current consumption. >5 kW red, >2 kW orange.
        function updateCardBorders({pv_w, expected_pv_w, soc, amps, battery_w, grid_w, home_w}) {
            const setBorder = (id, color) => {
                const el = document.getElementById(id);
                if (!el) return;
                el.classList.remove('border-green', 'border-orange', 'border-red');
                if (color) el.classList.add('border-' + color);
            };
            const setFills = (kind, ...stateClasses) => {
                // Apply state to both the now-fill and daily-fill of `kind`
                for (const suffix of ['load-fill', 'daily-fill']) {
                    const el = document.getElementById(kind + '-' + suffix);
                    if (!el) continue;
                    el.classList.remove('pv-good','pv-warn','pv-bad',
                                        'home-warm','home-hot');
                    for (const cls of stateClasses) el.classList.add(cls);
                }
            };

            // PV: ratio of actual to expected. Only color when there's a
            // meaningful expected number (above noise floor).
            let pvColor = null;
            let pvFillState = 'pv-good';   // default green when no signal
            if (expected_pv_w > 1000) {
                const ratio = pv_w / expected_pv_w;
                if      (ratio >= 0.75) { pvColor = 'green';  pvFillState = 'pv-good'; }
                else if (ratio >= 0.50) { pvColor = 'orange'; pvFillState = 'pv-warn'; }
                else                    { pvColor = 'red';    pvFillState = 'pv-bad';  }
            }
            setBorder('pv-card', pvColor);
            setFills('pv', pvFillState);

            // Battery: SOC bands (border only — daily-bar is bidi, fixed
            // colors). Only emphasize state when the battery is actively
            // in use (|power| ≥ 50W). When idle the border stays black
            // unless SOC is critically low — at <18% the red border
            // remains as a persistent warning regardless of activity.
            let batColor = null;
            const batteryActive = Math.abs(numericPower(battery_w) || 0) >= 50;
            if      (soc < 18)             batColor = 'red';     // always-on critical warning
            else if (batteryActive) {
                if      (soc > 70)         batColor = 'green';
                else if (soc < 30)         batColor = 'orange';
            }
            setBorder('battery-card', batColor);

            // Grid: signed power (border only — daily-bar is bidi)
            let gridColor = null;
            if      (grid_w >  50) gridColor = 'green';   // exporting
            else if (grid_w < -50) gridColor = 'red';     // importing
            setBorder('grid-card', gridColor);

            // Home: consumption magnitude bands
            let homeColor = null;
            let homeFillState = null;        // default solid teal
            if      (home_w > 5000) { homeColor = 'red';    homeFillState = 'home-hot';  }
            else if (home_w > 2000) { homeColor = 'orange'; homeFillState = 'home-warm'; }
            setBorder('home-card', homeColor);
            setFills('home', homeFillState);
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
                    const expectedPower = numericPower(data.expected_power);
                    const battPower = numericPower(inv.battery_charge_power) - numericPower(inv.battery_discharge_power);
                    // Net grid: + = exporting to grid, - = importing from grid
                    const gridPower = numericPower(inv.power_to_grid) - numericPower(inv.power_to_user);
                    // PV bar is "% of expected at this instant" — uses the
                    // model-derived expected_power as its denominator.
                    updatePowerLoadCard('pv', pvPower, expectedPower);
                    updatePowerLoadCard('battery', battPower);
                    updatePowerLoadCard('grid', gridPower);

                    // PV now-bar label: "of {N} kW expected @ HH:MM"
                    const nowHm = formatHM12(new Date().getHours(), new Date().getMinutes());
                    const expectedKw = (expectedPower / 1000).toFixed(1);
                    document.getElementById('pv-load-target').textContent =
                        `of ${expectedKw} kW expected @ ${nowHm}`;

                    // MPPT optimal voltage range for FlexBOSS21 — outside this range
                    // the MPPT efficiency drops noticeably. Color-code voltages so
                    // you can see at a glance which strings are wired suboptimally.
                    const MPPT_OPT_MIN = 300, MPPT_OPT_MAX = 580;
                    const fmtMpptDetail = (v, w) => {
                        const a = v > 0 ? (w / v).toFixed(1) : '0.0';
                        let cls = 'good';
                        let warn = '';
                        if (v > 0 && v < 140) { cls = 'bad'; }
                        else if (v > 0 && v < MPPT_OPT_MIN) { cls = 'warning'; }
                        else if (v > MPPT_OPT_MAX) { cls = 'bad'; }
                        return { text: `${v.toFixed(0)}V / ${a}A${warn}`, cls };
                    };
                    const setMpptDetail = (id, v, w) => {
                        const el = document.getElementById(id);
                        const { text, cls } = fmtMpptDetail(v, w);
                        el.textContent = text;
                        el.classList.remove('good', 'warning', 'bad');
                        el.classList.add(cls);
                    };

                    // Power bar — fraction of this array's effective peak
                    // (the calibrated MAX it ever produces, time-of-day-
                    // independent). 100% = absolutely best moment.
                    // Color thresholds match the PV card border.
                    const setMpptPowerBar = (id, power_w, effective_peak_kw) => {
                        const fill = document.getElementById(id);
                        if (!fill || !effective_peak_kw) return;
                        const pct = Math.min(100, Math.max(0,
                            power_w / (effective_peak_kw * 1000) * 100));
                        fill.style.width = pct.toFixed(1) + '%';
                        fill.classList.remove('pct-low', 'pct-mid', 'pct-high');
                        if      (pct >= 75) fill.classList.add('pct-high');
                        else if (pct >= 50) fill.classList.add('pct-mid');
                        else                fill.classList.add('pct-low');
                    };
                    // Look up each array's effective_peak_kw by name
                    const swArr = getArrayConfig('SW');
                    const neArr = getArrayConfig('NE');
                    const mxArr = getArrayConfig('Mixed') || getArrayConfig('Older');
                    const swPeak = swArr ? swArr.effective_peak_kw : null;
                    const nePeak = neArr ? neArr.effective_peak_kw : null;
                    const mxPeak = mxArr ? mxArr.effective_peak_kw : null;

                    // MPPT 1 = SW (verified afternoon 2026-05-22)
                    document.getElementById('pv1').textContent = formatPower(inv.pv1_power);
                    setMpptDetail('pv1-detail', inv.pv1_voltage, inv.pv1_power);
                    setMpptPowerBar('pv1-voltage-fill', inv.pv1_power, swPeak);

                    // MPPT 2 = NE
                    document.getElementById('pv2').textContent = formatPower(inv.pv2_power);
                    setMpptDetail('pv2-detail', inv.pv2_voltage, inv.pv2_power);
                    setMpptPowerBar('pv2-voltage-fill', inv.pv2_power, nePeak);

                    // MPPT 3 = Mixed/Older
                    document.getElementById('pv3').textContent = formatPower(inv.pv3_power);
                    setMpptDetail('pv3-detail', inv.pv3_voltage, inv.pv3_power);
                    setMpptPowerBar('pv3-voltage-fill', inv.pv3_power, mxPeak);

                    const allMpptIdle = [inv.pv1_power, inv.pv2_power, inv.pv3_power]
                        .every((power) => numericPower(power) === 0);
                    document.getElementById('pv-arrays-section').classList.toggle('mppt-zero', allMpptIdle);

                    // Upper-right info on the Battery card: SOC · V · A
                    // (current computed from power/voltage; sign = direction)
                    document.getElementById('battery-soc').textContent = inv.battery_soc + '%';
                    document.getElementById('battery-voltage').textContent = inv.battery_voltage.toFixed(1) + 'V';
                    const chgW = numericPower(inv.battery_charge_power);
                    const disW = numericPower(inv.battery_discharge_power);
                    const netW = chgW - disW;                      // +chg / -dis
                    const v    = numericPower(inv.battery_voltage);
                    const amps = v > 0 ? netW / v : 0;
                    const ampsEl = document.getElementById('battery-current');
                    const ampsStr = (amps === 0)
                        ? '0A'
                        : (amps > 0 ? '+' : '−') + Math.abs(amps).toFixed(0) + 'A';
                    ampsEl.textContent = ampsStr;
                    // Reset all current-state classes before applying new ones
                    ampsEl.classList.remove('charging','discharging','idle','warn-warm','warn-hot');
                    const absAmps = Math.abs(amps);
                    if      (absAmps > 180) ampsEl.classList.add('warn-hot');     // wiring check!
                    else if (absAmps > 170) ampsEl.classList.add('warn-warm');    // heads up
                    else if (amps >  0.5)   ampsEl.classList.add('charging');
                    else if (amps < -0.5)   ampsEl.classList.add('discharging');
                    else                    ampsEl.classList.add('idle');

                    // Upper-right on Grid card (temperature only — frequency and
                    // BMS amperage and inverter output power are intentionally not
                    // surfaced anywhere; user found them noise).
                    document.getElementById('inverter-temp').textContent = inv.inverter_temperature + '°C';

                    // Inverter status goes in the TOU & Mode section next to Mode
                    document.getElementById('inverter-status').textContent = inv.status;

                    // NEW Home card — consumption_power is whole-house load
                    const homePower = numericPower(inv.consumption_power);
                    updatePowerLoadCard('home', homePower);

                    // Color-code the top-card borders based on state.
                    updateCardBorders({
                        pv_w: pvPower,
                        expected_pv_w: numericPower(data.expected_power),
                        soc: numericPower(inv.battery_soc),
                        amps: amps,
                        battery_w: battPower,        // signed: + charge, − discharge
                        grid_w: gridPower,           // signed: + export, − import
                        home_w: homePower,
                    });

                    // Daily-totals bars under each power card.
                    //   PV:      generated kWh        / expected_daily_kwh
                    //   Battery: charged + discharged / battery capacity (46 kWh)
                    //   Grid:    exported kWh         / (50% of expected daily)
                    //   Home:    usage kWh            / (consumption budget = expected/2)
                    const yld = inv.energy_today_yield || 0;
                    const chg = inv.energy_today_charge || 0;
                    const dis = inv.energy_today_discharge || 0;
                    const exp = inv.energy_today_export || 0;
                    const imp = inv.energy_today_import || 0;
                    const usage = inv.energy_today_usage || 0;
                    // Stash so the events renderer can show "unaccounted" delta
                    window._lastInvDailyImport = imp;

                    const expectedDaily = data.expected_daily_kwh || 0;
                    const batCap = data.total_battery_kwh || 46;
                    const exportTarget = expectedDaily * 0.5;
                    // Practical home-usage ceiling — heat-pump days hit ~30 kWh,
                    // EV charging would push it; 45 kWh is a generous cap so the
                    // bar shows meaningful fill on a normal day (~20 kWh = 44%).
                    const homeBudget = 45;

                    // Single-sided daily bar: PV, Home. Label is HTML so
                    // we can use <span class="meta-suffix"> to make trailing
                    // words ("generated", "used") small/grey like "expected"
                    // on the right side — only the kWh number stays big+bold.
                    const setDailySingle = (kind, current, target, label) => {
                        const pct = target > 0 ? Math.min(100, current / target * 100) : 0;
                        document.getElementById(kind + '-daily-fill').style.width = pct.toFixed(1) + '%';
                        document.getElementById(kind + '-daily-text').innerHTML = label;
                    };
                    setDailySingle('pv',   yld,   expectedDaily,
                        `${yld.toFixed(1)} kWh<span class="meta-suffix">generated</span>`);
                    setDailySingle('home', usage, homeBudget,
                        `${usage.toFixed(1)} kWh<span class="meta-suffix">used</span>`);
                    document.getElementById('pv-daily-target').textContent =
                        `of ${expectedDaily.toFixed(0)} kWh expected`;
                    document.getElementById('home-daily-target').textContent =
                        `of ${homeBudget.toFixed(0)} kWh`;

                    // "Where you should be" indicators on the daily bars.
                    //
                    // PV: model-integrated expected kWh from start-of-day to
                    //     now, expressed as % of full-day expected. Non-linear
                    //     curve (peaks at solar noon).
                    // Home: linear time-of-day reference — 45 kWh / 24h means
                    //       50% at noon, 100% at midnight.
                    const expSoFar = numericPower(data.expected_kwh_so_far);
                    const pvIndPct = expectedDaily > 0
                        ? Math.min(100, expSoFar / expectedDaily * 100) : 0;
                    document.getElementById('pv-daily-indicator').style.left =
                        pvIndPct.toFixed(1) + '%';

                    const _now = new Date();
                    const minSinceMidnight = _now.getHours() * 60 + _now.getMinutes();
                    const homeIndPct = minSinceMidnight / 1440 * 100;
                    document.getElementById('home-daily-indicator').style.left =
                        homeIndPct.toFixed(1) + '%';

                    // Bidirectional daily bars: Battery (chg/dis), Grid (exp/imp).
                    // Each half is 50% wide max → value scales to that half.
                    const setDailyBidi = (kind, leftVal, rightVal, target,
                                          leftLabel, rightLabel) => {
                        const leftPct  = target > 0 ? Math.min(50, leftVal  / target * 50) : 0;
                        const rightPct = target > 0 ? Math.min(50, rightVal / target * 50) : 0;
                        document.getElementById(kind + '-daily-left').style.width  = leftPct.toFixed(1) + '%';
                        document.getElementById(kind + '-daily-right').style.width = rightPct.toFixed(1) + '%';
                        document.getElementById(leftLabel.id).innerHTML  = leftLabel.html;
                        document.getElementById(rightLabel.id).innerHTML = rightLabel.html;
                    };
                    // Battery: total cycled energy ~= batCap is a "full charge OR full
                    // discharge"; each side's bar maxes out at batCap.
                    setDailyBidi('battery', dis, chg, batCap,
                        { id: 'battery-daily-discharged',
                          html: `${dis.toFixed(1)} kWh<span class="meta-suffix">out</span>` },
                        { id: 'battery-daily-charged',
                          html: `${chg.toFixed(1)} kWh<span class="meta-suffix">in</span>` });
                    // Grid: export target = expectedDaily/2 (matches the original
                    // single-sided bar). The left/right text spans get written
                    // below in the schedule.rates block with rate annotations.
                    const gridLeftPct  = exportTarget > 0 ? Math.min(50, imp / exportTarget * 50) : 0;
                    const gridRightPct = exportTarget > 0 ? Math.min(50, exp / exportTarget * 50) : 0;
                    document.getElementById('grid-daily-left').style.width  = gridLeftPct.toFixed(1) + '%';
                    document.getElementById('grid-daily-right').style.width = gridRightPct.toFixed(1) + '%';
                }

                // TOU + mode panel — most of this now lives in compact
                // places: operation mode in the events-header upper-right,
                // TOU-period icon + $/hr in the Grid card meta tag,
                // import/export $-formula in the Grid card daily-meta row.
                if (data.schedule) {
                    const sch = data.schedule;
                    document.getElementById('op-mode').textContent = sch.operational_mode;

                    // TOU period → emoji icon (red/yellow/green dot)
                    let touIcon = '🟢', touTitle = 'off-peak';
                    if (sch.in_peak)              { touIcon = '🔴'; touTitle = 'peak'; }
                    else if (sch.in_partial_peak) { touIcon = '🟡'; touTitle = 'partial peak'; }
                    const touEl = document.getElementById('tou-period-icon');
                    if (touEl) { touEl.textContent = touIcon; touEl.title = touTitle; }

                    // Single-line daily summary under the grid card's daily-bar:
                    //   [import kWh × import rate]   [net $]   [export kWh × export rate]
                    // Net $ comes from the server's TOU-aware integration
                    // (data.today_money.net), NOT (current_rate × daily_kWh) —
                    // the rate side-numbers show the CURRENT rate just for
                    // context, but the cumulative $ accounts for which rate
                    // was active at each minute of the day.
                    if (sch.rates && data.inverter) {
                        const impKwh = numericPower(data.inverter.energy_today_import) || 0;
                        const expKwh = numericPower(data.inverter.energy_today_export) || 0;
                        const impRate = sch.rates.currently_active_import_rate;
                        const expRate = sch.rates.currently_active_export_rate;
                        document.getElementById('grid-daily-imported').innerHTML =
                            `<span class="kwh-num">${impKwh.toFixed(2)}</span>`
                            + `<span class="kwh-unit">kWh</span>`
                            + `<span class="rate-mult">× ${(impRate*100).toFixed(1)}¢</span>`;
                        document.getElementById('grid-daily-exported').innerHTML =
                            `<span class="kwh-num">${expKwh.toFixed(1)}</span>`
                            + `<span class="kwh-unit">kWh</span>`
                            + `<span class="rate-mult">× ${(expRate*100).toFixed(1)}¢</span>`;
                        // TOU-aware net (server-computed). Fall back to the
                        // current-rate approximation if not yet loaded.
                        const tm = data.today_money;
                        const net = (tm && typeof tm.net === 'number')
                            ? tm.net
                            : (expKwh * expRate) - (impKwh * impRate);
                        const netEl = document.getElementById('grid-money-net');
                        const sign = net >= 0 ? '+' : '−';
                        netEl.textContent = `${sign}$${Math.abs(net).toFixed(2)}`;
                        netEl.classList.toggle('profit', net >= 0);
                        netEl.classList.toggle('loss',   net <  0);
                    }

                    // $/hr (estimate) in the grid card meta tag, sign-colored
                    const dph = sch.dollars_per_hour_est;
                    const dphEl = document.getElementById('dollars-per-hour');
                    if (dph != null && dphEl) {
                        const sign = dph >= 0 ? '+' : '−';
                        dphEl.textContent = sign + '$' + Math.abs(dph).toFixed(2) + '/hr';
                        dphEl.style.color = dph >= 0 ? '#2ecc71' : '#e74c3c';
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
            return formatHM12(h, m);
        }
        // 12-hour compact format: "1:15p", "9:31a", "12:00p", "12:00a"
        function formatHM12(h, m) {
            const suffix = h >= 12 ? 'p' : 'a';
            let h12 = h % 12;
            if (h12 === 0) h12 = 12;
            return h12 + ':' + String(m).padStart(2, '0') + suffix;
        }
        // Format "HH:00 – HH:00" pair as "Ha – Hp" (no minutes since schedule is hourly).
        function formatHourRange12(h0, h1) {
            const fmt = (h) => {
                const suffix = h >= 12 ? 'p' : 'a';
                let h12 = h % 12;
                if (h12 === 0) h12 = 12;
                return h12 + suffix;
            };
            return fmt(h0) + ' – ' + fmt(h1);
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
        # Calibrated model: each array's effective_peak_kw is the MEASURED
        # peak from inverter chart data on best days. We modulate it by the
        # POA irradiance ratio (so current POA / peak POA = fraction of
        # peak). All real-world losses (MPPT-voltage, wiring, mismatch)
        # are already baked into effective_peak_kw because it's measured,
        # not derived from nameplate.
        # Reference POA ≈ 1000 W/m² (typical clear-noon irradiance).
        array_power = (array.get("effective_peak_kw", array["capacity_kw"])
                       * 1000 * (irradiance / 1000.0) * DEFAULT_CLOUD_FACTOR)
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
        "expected_daily_kwh": _expected_daily_kwh_for(now.date()),
        # How much we *should* have produced by this exact moment
        # (cumulative integral of the model up to "now"). Used to draw
        # the white "where you should be" tick on the PV daily-bar.
        "expected_kwh_so_far": _expected_kwh_so_far(now),
        "total_battery_kwh": TOTAL_BATTERY_KWH,
        "dni": dni,
        "inverter": inverter_data,
        "schedule": schedule,
        # TOU-aware day-to-date earnings (integrates pToGrid × export-rate
        # − pToUser × import-rate at each 4-min sample using the rate
        # window in effect at that sample's timestamp).
        "today_money": _today_money_sync(),
        "read_only": True,
    })


# 60-second cache for the TOU-aware daily-earnings integration.
_TODAY_MONEY_CACHE: dict = {"date": None, "ts": None, "data": None}


async def _compute_today_money_so_far():
    """Integrate today's pToGrid × export-rate − pToUser × import-rate
    using the TOU window in effect at each 4-min chart-data sample.

    Returns {"income", "cost", "net"} in dollars. This is what the user
    sees as "+$X.XX today" under the grid card. Replaces the previous
    naive `(daily_export_kWh × current_rate)` calculation which over-
    estimated when the current rate was high (e.g. Ava bonus window)
    because most of the day's exports happened at the lower off-peak
    rate.
    """
    today = datetime.now(TIMEZONE).date()
    inverter = _inverter_cache.get("inverter")
    client   = _inverter_cache.get("client")
    if not inverter or not client:
        return {"income": 0.0, "cost": 0.0, "net": 0.0}
    serial = inverter.serial_number
    try:
        pgrid_r, puser_r = await asyncio.gather(
            client.analytics.get_chart_data(serial, "pToGrid", today.isoformat()),
            client.analytics.get_chart_data(serial, "pToUser", today.isoformat()),
        )
    except Exception as e:
        print(f"today_money fetch error: {e}", flush=True)
        return {"income": 0.0, "cost": 0.0, "net": 0.0}

    income = 0.0
    cost   = 0.0
    SAMPLE_HOURS = 4.0 / 60.0   # 4-min chart-data interval
    for s in (pgrid_r.get("data") or []):
        t = s.get("time"); v = s.get("value") or 0
        if not t or v <= 0: continue
        try:
            ts = datetime.strptime(t, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TIMEZONE)
        except ValueError:
            continue
        _, exp_rate = rates_at(ts)
        income += (v / 1000.0) * SAMPLE_HOURS * exp_rate
    for s in (puser_r.get("data") or []):
        t = s.get("time"); v = s.get("value") or 0
        if not t or v <= 0: continue
        try:
            ts = datetime.strptime(t, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TIMEZONE)
        except ValueError:
            continue
        imp_rate, _ = rates_at(ts)
        cost += (v / 1000.0) * SAMPLE_HOURS * imp_rate
    return {"income": round(income, 3),
            "cost":   round(cost, 3),
            "net":    round(income - cost, 3)}


def _today_money_sync():
    cache = _TODAY_MONEY_CACHE
    today_iso = datetime.now(TIMEZONE).date().isoformat()
    if (cache["date"] == today_iso and cache["ts"]
            and (datetime.now() - cache["ts"]).total_seconds() < 60):
        return cache["data"]
    with _LOOP_LOCK:
        loop = _inverter_cache.get("loop")
        if loop is None or loop.is_closed():
            return cache.get("data") or {"income": 0.0, "cost": 0.0, "net": 0.0}
        try:
            data = loop.run_until_complete(_compute_today_money_so_far())
        except Exception as e:
            print(f"today_money sync error: {e}", flush=True)
            data = cache.get("data") or {"income": 0.0, "cost": 0.0, "net": 0.0}
    cache["date"] = today_iso
    cache["ts"] = datetime.now()
    cache["data"] = data
    return data


# Cache today's cumulative expected-kWh curve (one entry per 30-min slot,
# 48 entries total). Used by:
#   - _expected_daily_kwh_for(date)   → last entry / 1000
#   - _expected_kwh_so_far(now)       → interpolated lookup at h_now
_EXPECTED_CUMULATIVE_CACHE = {"date": None, "cumulative_wh": []}


def _ensure_today_cumulative(date):
    """Build (and cache) a 48-element cumulative expected-Wh array for `date`.

    cumulative_wh[i] = total expected energy produced from 00:00 through the
    *end* of the 30-min slot starting at i*30 minutes. So
    cumulative_wh[-1] is the full-day expected energy in Wh.

    Includes the temperature derate + cloud factor so the cumulative curve
    matches the per-instant `expected_power` model exactly.
    """
    cache = _EXPECTED_CUMULATIVE_CACHE
    iso = date.isoformat()
    if cache["date"] == iso:
        return cache["cumulative_wh"]

    cumulative: list[float] = []
    running_wh = 0.0
    for hour in range(24):
        for minute in (0, 30):
            t = datetime(date.year, date.month, date.day, hour, minute,
                         tzinfo=TIMEZONE)
            pos = calculate_solar_position(t, LATITUDE, LONGITUDE)
            slot_power_w = 0.0
            if pos["altitude"] > 0:
                dni = calculate_clear_sky_dni(pos["altitude"])
                for array in SOLAR_ARRAYS:
                    if array.get("type") == "split":
                        azimuths = array["azimuth"]
                        fractions = array.get(
                            "fractions",
                            [1.0 / len(azimuths)] * len(azimuths))
                        irr = sum(
                            f * calculate_panel_irradiance(
                                pos["altitude"], pos["azimuth"],
                                array["tilt"], az, dni)
                            for az, f in zip(azimuths, fractions)
                        )
                    else:
                        irr = calculate_panel_irradiance(
                            pos["altitude"], pos["azimuth"],
                            array["tilt"], array["azimuth"], dni)
                    slot_power_w += (array.get("effective_peak_kw", array["capacity_kw"])
                                     * 1000 * (irr / 1000.0) * DEFAULT_CLOUD_FACTOR)
            running_wh += slot_power_w * 0.5   # 30-min slot, Wh
            cumulative.append(running_wh)

    cache["date"] = iso
    cache["cumulative_wh"] = cumulative
    return cumulative


def _expected_daily_kwh_for(date):
    """Full-day expected kWh — last entry of the cumulative curve.

    Single unified model: the cumulative curve already incorporates the
    altitude-dependent atmospheric_clearness factor, so the daily total
    and the instantaneous expected_power use the exact same math.
    """
    cum = _ensure_today_cumulative(date)
    return (cum[-1] / 1000.0) if cum else 0.0


def _expected_kwh_so_far(now: datetime) -> float:
    """Expected kWh produced from start-of-day up to `now`, interpolated."""
    cum = _ensure_today_cumulative(now.date())
    if not cum:
        return 0.0
    slot_idx_float = (now.hour + now.minute / 60.0) * 2
    if slot_idx_float >= len(cum):
        return cum[-1] / 1000.0
    if slot_idx_float <= 0:
        return 0.0
    low = int(slot_idx_float)
    high = min(low + 1, len(cum) - 1)
    frac = slot_idx_float - low
    interp_wh = cum[low] + frac * (cum[high] - cum[low])
    return interp_wh / 1000.0


# Cache today's events for 60s to avoid hammering the inverter API
_EVENTS_CACHE = {"date": None, "ts": None, "events": []}


def _fmt_12h(ts):
    """Format a datetime as compact 12-hour time, e.g. '1:15p', '9:31a'."""
    h = ts.hour
    m = ts.minute
    suffix = "p" if h >= 12 else "a"
    h12 = h % 12 or 12
    return f"{h12}:{m:02d}{suffix}"


async def _fetch_recent_import_events():
    """Pull per-4-min pToUser/SOC/ppv chart samples for the last 24h
    (today + yesterday's tail) and classify each contiguous import event."""
    from datetime import timedelta as _td
    now = datetime.now(TIMEZONE)
    today = now.date()
    yesterday = today - _td(days=1)
    cutoff = now - _td(hours=24)

    inverter = _inverter_cache.get("inverter")
    if not inverter:
        return []
    serial = inverter.serial_number
    client = _inverter_cache.get("client")
    if not client:
        return []

    # Fetch both days in parallel — yesterday gives us last night, today
    # gives us anything in the morning to now.
    try:
        results = await asyncio.gather(
            client.analytics.get_chart_data(serial, "pToUser", yesterday.isoformat()),
            client.analytics.get_chart_data(serial, "soc",     yesterday.isoformat()),
            client.analytics.get_chart_data(serial, "ppv",     yesterday.isoformat()),
            client.analytics.get_chart_data(serial, "pToUser", today.isoformat()),
            client.analytics.get_chart_data(serial, "soc",     today.isoformat()),
            client.analytics.get_chart_data(serial, "ppv",     today.isoformat()),
        )
    except Exception as e:
        print(f"events fetch error: {e}", flush=True)
        return []

    ptouser_y, soc_y, ppv_y, ptouser_t, soc_t, ppv_t = results

    # Merge both days' samples
    ptouser_all = (ptouser_y.get("data", []) or []) + (ptouser_t.get("data", []) or [])
    soc_lookup = {}
    for src in (soc_y.get("data", []) or []) + (soc_t.get("data", []) or []):
        if "time" in src:
            soc_lookup[src["time"]] = src.get("value")
    ppv_lookup = {}
    for src in (ppv_y.get("data", []) or []) + (ppv_t.get("data", []) or []):
        if "time" in src:
            ppv_lookup[src["time"]] = src.get("value")

    # Filter to last 24h only (drop samples older than the cutoff)
    def _in_window(s):
        try:
            ts = datetime.strptime(s.get("time", ""), "%Y-%m-%d %H:%M:%S")
            ts = ts.replace(tzinfo=TIMEZONE)
            return ts >= cutoff
        except (ValueError, TypeError):
            return False

    ptouser_window = [s for s in ptouser_all if _in_window(s)]

    # Group consecutive non-zero pToUser samples into events.
    #   - Threshold lowered to 1W: anything the chart-data reports as
    #     a non-zero import counts. Smaller samples used to be filtered
    #     out, leaving a daily-total/event-sum discrepancy.
    #   - GAP_TOLERANCE: a single 0-W sample inside a stretch of imports
    #     no longer splits the event into two. The Luxpower chart-data
    #     resolution sometimes rounds brief imports to 0 even when the
    #     daily counter shows accumulation.
    IMPORT_THRESHOLD_W = 1
    GAP_TOLERANCE = 1   # allow one zero-sample gap before closing event
    events_raw = []
    cur = None
    gap_streak = 0
    for s in ptouser_window:
        v = s.get("value") or 0
        t = s.get("time")
        if not t:
            continue
        if v >= IMPORT_THRESHOLD_W:
            if cur is None:
                cur = {"start": t, "end": t, "vals": [v]}
            else:
                cur["end"] = t
                cur["vals"].append(v)
            gap_streak = 0
        else:
            if cur is not None:
                gap_streak += 1
                if gap_streak > GAP_TOLERANCE:
                    events_raw.append(cur)
                    cur = None
                    gap_streak = 0
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
        # 12-hour compact format: "1:15p", "9:31a". Prepend weekday abbrev
        # if the event is from yesterday so the row reads clearly.
        date_prefix = "" if ts_start.date() == datetime.now(TIMEZONE).date() \
                        else ts_start.strftime("%a ")
        out.append({
            "type": "import",
            "ts": ts_start.isoformat(),    # for cross-event sort
            "is_today": ts_start.date() == datetime.now(TIMEZONE).date(),
            "start": date_prefix + _fmt_12h(ts_start),
            "end":   date_prefix + _fmt_12h(ts_end),
            "duration_min": round(dur_min, 1),
            "peak_w": int(peak_w),
            "kwh": round(kwh, 3),
            "soc": int(soc_at) if soc_at is not None else None,
            "ppv": int(ppv_at) if ppv_at is not None else None,
            "reason": reason,
        })
    return out


# Backwards-compatible alias (old name)
_fetch_today_import_events = _fetch_recent_import_events


def _schedule_timeline_as_events(now: datetime) -> list[dict]:
    """Convert today's remaining schedule phases into events for the
    unified events list.

    Each phase becomes one event:
      - active phase → "NOW: <label>"   ts = now
      - future phase → "<label> starts" ts = phase start
    All marked is_today=True (we only emit today's remaining phases).
    """
    timeline = _build_schedule_timeline(now)
    out = []
    for phase in timeline:
        # Phase start hour. _build_schedule_timeline clips to "now" for
        # the active phase, so we use today's midnight + the formatted
        # start. Simpler: take h_now if active, else parse the phase's
        # start_h… but the formatted string is what we have.
        # Just use the formatted start string as event start.
        is_active = phase.get("active", False)
        # Build ISO ts: today's date + the phase's numeric start hour.
        # We don't have raw start_h here, so we conservatively set ts
        # to "now" for active phases and try to recover the hour from
        # the formatted label for future phases.
        if is_active:
            ts = now
        else:
            ts = _parse_12h_label_to_today(phase["start"], now)
        reason = (f"⏱ NOW: {phase['label']}" if is_active
                  else f"⏱ {phase['start']}: {phase['label']} starts")
        out.append({
            "type":     "schedule",
            "ts":       ts.isoformat(),
            "is_today": True,
            "active":   is_active,
            "start":    "→ " + phase["end"] if is_active else phase["start"],
            "end":      phase["end"],
            "kind":     phase.get("kind", "idle"),
            "reason":   reason,
            "detail":   phase.get("detail", ""),
        })
    return out


def _parse_12h_label_to_today(label: str, now: datetime) -> datetime:
    """Parse '4p' / '9p' / '12:30p' style label into today's datetime."""
    import re
    m = re.match(r"^(\d{1,2})(?::(\d{2}))?([ap])$", label.strip())
    if not m:
        return now
    h = int(m.group(1))
    mm = int(m.group(2) or 0)
    suf = m.group(3)
    if suf == "p" and h != 12: h += 12
    if suf == "a" and h == 12: h = 0
    return now.replace(hour=h, minute=mm, second=0, microsecond=0)


async def _fetch_bms_limit_events():
    """Detect BMS-reported charge/discharge limit changes in the last 24h.

    Field semantics (from inverter chart data):
        maxChgCurr     = BMS charge-current limit × 10 (so 1600 → 160 A)
        maxDischgCurr  = BMS discharge-current limit × 10

    When the BMS dynamically downgrades the limit (typically due to cell
    voltage, temperature, or balance concerns), it often correlates with
    PV-charging stopping abruptly — useful diagnostic context.

    Returns a list of events compatible with the import-event format,
    with type="bms_charge" or "bms_discharge".
    """
    from datetime import timedelta as _td
    now = datetime.now(TIMEZONE)
    today = now.date()
    yesterday = today - _td(days=1)
    cutoff = now - _td(hours=24)

    inverter = _inverter_cache.get("inverter")
    client = _inverter_cache.get("client")
    if not inverter or not client:
        return []
    serial = inverter.serial_number

    try:
        results = await asyncio.gather(
            client.analytics.get_chart_data(serial, "maxChgCurr",    yesterday.isoformat()),
            client.analytics.get_chart_data(serial, "maxDischgCurr", yesterday.isoformat()),
            client.analytics.get_chart_data(serial, "maxChgCurr",    today.isoformat()),
            client.analytics.get_chart_data(serial, "maxDischgCurr", today.isoformat()),
            # SOC + battery voltage for diagnostic context on the event row
            client.analytics.get_chart_data(serial, "soc",  yesterday.isoformat()),
            client.analytics.get_chart_data(serial, "soc",  today.isoformat()),
            client.analytics.get_chart_data(serial, "vBat", yesterday.isoformat()),
            client.analytics.get_chart_data(serial, "vBat", today.isoformat()),
        )
    except Exception as e:
        print(f"bms events fetch error: {e}", flush=True)
        return []

    # Build {time → soc} and {time → vBat} lookups for cross-reference
    soc_lookup, vbat_lookup = {}, {}
    for src in (results[4].get("data", []) or []) + (results[5].get("data", []) or []):
        t = src.get("time")
        if t: soc_lookup[t] = src.get("value")
    for src in (results[6].get("data", []) or []) + (results[7].get("data", []) or []):
        t = src.get("time")
        if t: vbat_lookup[t] = src.get("value")

    out = []
    # Each field: walk samples in time order, emit an event each time the
    # value changes from the previous sample.
    for field_idx, (field, kind, label_singular) in enumerate(
        (("maxChgCurr",    "bms_charge",    "BMS charge limit"),
         ("maxDischgCurr", "bms_discharge", "BMS discharge limit"))
    ):
        # Merge yesterday + today samples for this field
        all_samples = []
        for day_idx in (0, 1):
            r = results[field_idx + day_idx * 2]
            if isinstance(r, Exception):
                continue
            all_samples.extend(r.get("data", []) or [])
        # Sort by time
        try:
            all_samples.sort(key=lambda s: s.get("time", ""))
        except Exception:
            continue
        prev_val = None
        for s in all_samples:
            t = s.get("time")
            v = s.get("value")
            if not t or v is None:
                continue
            v = float(v) / 10.0      # convert deci-amps → amps
            try:
                ts = datetime.strptime(t, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TIMEZONE)
            except ValueError:
                continue
            if ts < cutoff:
                prev_val = v
                continue
            if prev_val is not None and v != prev_val:
                direction = "↓" if v < prev_val else "↑"
                date_prefix = "" if ts.date() == today \
                                else ts.strftime("%a ")
                soc_at = soc_lookup.get(t)
                vbat_at = vbat_lookup.get(t)
                out.append({
                    "type":   kind,
                    "ts":     ts.isoformat(),
                    "is_today": ts.date() == today,
                    "start":  date_prefix + _fmt_12h(ts),
                    "end":    date_prefix + _fmt_12h(ts),
                    "reason": f"{label_singular} {direction} {prev_val:.0f}A → {v:.0f}A",
                    "prev_a": prev_val,
                    "new_a":  v,
                    "soc":    int(soc_at) if soc_at is not None else None,
                    # vBat in chart-data is reported in volts directly
                    "vbat":   round(float(vbat_at), 2) if vbat_at is not None else None,
                })
            prev_val = v
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
    """Sync wrapper for the events fetch; reuses the dashboard's persistent loop
    via _LOOP_LOCK so it doesn't collide with get_inverter_data_sync."""
    today = datetime.now(TIMEZONE).date().isoformat()
    if (_EVENTS_CACHE["date"] == today and _EVENTS_CACHE["ts"] and
            (datetime.now() - _EVENTS_CACHE["ts"]).total_seconds() < 60):
        return _EVENTS_CACHE["events"]
    with _LOOP_LOCK:
        loop = _inverter_cache.get("loop")
        if loop is None or loop.is_closed():
            return _EVENTS_CACHE["events"]
        try:
            # Fetch both event types concurrently then merge in time order
            async def _gather_all():
                import_evs, bms_evs = await asyncio.gather(
                    _fetch_today_import_events(),
                    _fetch_bms_limit_events(),
                )
                # Schedule timeline → events (future transitions appear
                # interleaved with past imports + BMS changes).
                sched_evs = _schedule_timeline_as_events(datetime.now(TIMEZONE))
                merged = (import_evs or []) + (bms_evs or []) + sched_evs
                # Newest first (ts is ISO, so string sort works)
                merged.sort(key=lambda e: e.get("ts", ""), reverse=True)
                return merged
            events = loop.run_until_complete(_gather_all())
        except Exception as e:
            print(f"events sync error: {e}", flush=True)
            events = _EVENTS_CACHE.get("events", [])
    _EVENTS_CACHE["date"] = today
    _EVENTS_CACHE["ts"] = datetime.now()
    _EVENTS_CACHE["events"] = events
    return events


@app.route('/api/events')
def get_events():
    """Return today's classified import events, plus the inverter's
    continuous daily import counter so the client can show the
    chart-data sampling gap as an "unaccounted" row without racing
    against fetchData()."""
    daily_import_kwh = None
    inv_data = _inverter_cache.get("data")
    if inv_data:
        daily_import_kwh = inv_data.get("energy_today_import")
    return jsonify({
        "date": datetime.now(TIMEZONE).date().isoformat(),
        "events": _events_sync(),
        "daily_import_kwh": daily_import_kwh,
    })


# --- 24h history (for the chart at the bottom of the dashboard) -----------
#
# Pulls 4-min-sampled analytics series for yesterday + today, merges to a
# single timeline, and returns kW values (signed where appropriate) plus
# battery SOC %. Cached 60s so repeated chart redraws don't hammer the API.
_HISTORY_CACHE: dict = {"ts": None, "data": None}


async def _fetch_24h_history():
    from datetime import timedelta as _td
    now = datetime.now(TIMEZONE)
    today = now.date()
    yesterday = today - _td(days=1)
    cutoff = now - _td(hours=24)

    inverter = _inverter_cache.get("inverter")
    client = _inverter_cache.get("client")
    if not inverter or not client:
        return {"samples": []}
    serial = inverter.serial_number

    # Fields we want for each day. Notes on field-name oddities discovered
    # the hard way:
    #   - "ppv" (aggregate) returns empty — must sum ppv1+ppv2+ppv3 instead.
    #   - "pDisCharge" needs capital C, but lowercase also works.
    #   - There's no direct pLoad/home-consumption field; we reconstruct it
    #     from pInv + pToUser - pToGrid (matches consumption_power exactly).
    fields = ["ppv1", "ppv2", "ppv3",
              "pCharge", "pDisCharge",
              "pToUser", "pToGrid", "pInv", "soc"]
    tasks = []
    for d in (yesterday, today):
        for f in fields:
            tasks.append(client.analytics.get_chart_data(serial, f, d.isoformat()))
    try:
        all_results = await asyncio.gather(*tasks, return_exceptions=True)
    except Exception as e:
        print(f"history fetch error: {e}", flush=True)
        return {"samples": []}

    # Build {time -> {field: value}} dictionary across both days
    timeline: dict[str, dict[str, float]] = {}
    for i, res in enumerate(all_results):
        if isinstance(res, Exception):
            continue
        field = fields[i % len(fields)]
        for sample in (res.get("data", []) or []):
            t = sample.get("time")
            if not t:
                continue
            try:
                ts = datetime.strptime(t, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TIMEZONE)
            except ValueError:
                continue
            if ts < cutoff:
                continue
            timeline.setdefault(t, {})[field] = sample.get("value") or 0

    # Sort by time and emit one row per timestamp
    out_samples = []
    for t in sorted(timeline.keys()):
        v = timeline[t]
        pv = (float(v.get("ppv1", 0)) + float(v.get("ppv2", 0))
              + float(v.get("ppv3", 0)))            # W (sum the 3 MPPTs)
        charge  = float(v.get("pCharge", 0))         # W
        discharge = float(v.get("pDisCharge", 0))    # W  (NB: capital C — pDisCharge)
        to_user = float(v.get("pToUser", 0))         # W — grid import
        to_grid = float(v.get("pToGrid", 0))         # W — grid export
        pinv    = float(v.get("pInv", 0))            # W — inverter net AC output
        soc     = float(v.get("soc", 0))             # %

        # Home consumption reconstructed from the inverter's AC interface:
        #   home = inverter_output + grid_in - grid_out
        # Verified against the live "consumption_power" field; matches exactly.
        home = max(0.0, pinv + to_user - to_grid)

        out_samples.append({
            "t": t,
            "pv_kw":     round(pv / 1000.0, 3),
            "battery_kw": round((charge - discharge) / 1000.0, 3),  # +chg / -dis
            "import_kw": round(to_user / 1000.0, 3),
            "export_kw": round(to_grid / 1000.0, 3),
            "home_kw":   round(home / 1000.0, 3),
            "soc":       round(soc, 1),
        })
    return {"samples": out_samples}


def _history_sync():
    if _HISTORY_CACHE["ts"] and (datetime.now() - _HISTORY_CACHE["ts"]).total_seconds() < 60:
        return _HISTORY_CACHE["data"]
    with _LOOP_LOCK:
        loop = _inverter_cache.get("loop")
        if loop is None or loop.is_closed():
            return _HISTORY_CACHE["data"] or {"samples": []}
        try:
            data = loop.run_until_complete(_fetch_24h_history())
        except Exception as e:
            print(f"history sync error: {e}", flush=True)
            data = _HISTORY_CACHE.get("data") or {"samples": []}
    _HISTORY_CACHE["ts"] = datetime.now()
    _HISTORY_CACHE["data"] = data
    return data


@app.route('/api/history')
def get_history():
    """Return 24h of 4-min-sampled history for the chart at the bottom."""
    return jsonify(_history_sync())


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
