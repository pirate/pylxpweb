"""
Inverter Configuration Script

Configures the FlexBOSS inverter with the following settings:
- Mode: PV Charge Priority
- Battery Type: Lithium
- Battery Comms: None
- Battery Control Mode: Voltage
- Battery Min Voltage: 44V (NEVER EXCEED!)
- Battery Max Voltage: 48V (NEVER EXCEED!)
- Battery Max Charge Rate: 50A (2.4kW)
- Battery Max Discharge Rate: 50A (2.4kW)
- Export to Grid: Enabled
- Max Grid Export: 15kW

Shows before/after values and prompts for confirmation before applying changes.
"""

import asyncio
import math
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from pylxpweb import LuxpowerClient
from pylxpweb.devices.station import Station
from _env import USERNAME, PASSWORD, BASE_URL

# =============================================================================
# LOCATION AND SOLAR ARRAY CONFIGURATION
# =============================================================================

# Oakland, California coordinates
LATITUDE = 37.8044
LONGITUDE = -122.2712
TIMEZONE = ZoneInfo("America/Los_Angeles")

# Solar array configuration
ARRAY_SIZE_KW = 14.0  # Total array size in kW
PANEL_TILT = 20.0  # Assumed roof pitch in degrees (typical for Oakland)

# Two roof faces with different azimuths (0° = North, 90° = East, 180° = South, 270° = West)
ROOF_FACES = [
    {"name": "SW Face", "azimuth": 217.0, "fraction": 0.5},  # Half the array
    {"name": "NE Face", "azimuth": 37.0, "fraction": 0.5},   # Half the array
]


# =============================================================================
# SOLAR POSITION CALCULATIONS
# =============================================================================

def calculate_solar_position(dt: datetime, latitude: float, longitude: float) -> dict:
    """
    Calculate sun position (altitude and azimuth) for a given time and location.

    Uses standard astronomical algorithms for solar position.

    Args:
        dt: Datetime (timezone-aware)
        latitude: Latitude in degrees (positive = North)
        longitude: Longitude in degrees (positive = East, negative = West)

    Returns:
        Dictionary with solar position data
    """
    # Convert to UTC for calculations
    dt_utc = dt.astimezone(timezone.utc)

    # Julian Day calculation
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

    # Julian Century
    T = (JD - 2451545.0) / 36525.0

    # Solar coordinates
    # Mean longitude of the sun
    L0 = (280.46646 + 36000.76983 * T + 0.0003032 * T**2) % 360

    # Mean anomaly of the sun
    M = (357.52911 + 35999.05029 * T - 0.0001537 * T**2) % 360
    M_rad = math.radians(M)

    # Eccentricity of Earth's orbit
    e = 0.016708634 - 0.000042037 * T - 0.0000001267 * T**2

    # Equation of center
    C = ((1.914602 - 0.004817 * T - 0.000014 * T**2) * math.sin(M_rad) +
         (0.019993 - 0.000101 * T) * math.sin(2 * M_rad) +
         0.000289 * math.sin(3 * M_rad))

    # Sun's true longitude
    sun_lon = L0 + C

    # Sun's apparent longitude (corrected for nutation and aberration)
    omega = 125.04 - 1934.136 * T
    sun_lon_apparent = sun_lon - 0.00569 - 0.00478 * math.sin(math.radians(omega))

    # Obliquity of the ecliptic
    obliquity = 23.439291 - 0.013004 * T
    obliquity_corrected = obliquity + 0.00256 * math.cos(math.radians(omega))
    obliquity_rad = math.radians(obliquity_corrected)

    # Sun's right ascension and declination
    sun_lon_rad = math.radians(sun_lon_apparent)

    declination = math.degrees(math.asin(math.sin(obliquity_rad) * math.sin(sun_lon_rad)))

    # Equation of time (in minutes)
    y = math.tan(obliquity_rad / 2) ** 2
    L0_rad = math.radians(L0)
    eq_time = 4 * math.degrees(
        y * math.sin(2 * L0_rad) -
        2 * e * math.sin(M_rad) +
        4 * e * y * math.sin(M_rad) * math.cos(2 * L0_rad) -
        0.5 * y**2 * math.sin(4 * L0_rad) -
        1.25 * e**2 * math.sin(2 * M_rad)
    )

    # Solar noon (in minutes from midnight UTC)
    solar_noon_utc = 720 - 4 * longitude - eq_time

    # Current time in minutes from midnight UTC
    current_time_utc = hour * 60

    # Hour angle
    hour_angle = (current_time_utc - solar_noon_utc) / 4  # degrees
    hour_angle_rad = math.radians(hour_angle)

    # Convert latitude to radians
    lat_rad = math.radians(latitude)
    dec_rad = math.radians(declination)

    # Solar altitude (elevation)
    sin_altitude = (math.sin(lat_rad) * math.sin(dec_rad) +
                    math.cos(lat_rad) * math.cos(dec_rad) * math.cos(hour_angle_rad))
    altitude = math.degrees(math.asin(max(-1, min(1, sin_altitude))))

    # Solar azimuth
    cos_azimuth = ((math.sin(dec_rad) - math.sin(lat_rad) * sin_altitude) /
                   (math.cos(lat_rad) * math.cos(math.radians(altitude))))
    cos_azimuth = max(-1, min(1, cos_azimuth))

    azimuth = math.degrees(math.acos(cos_azimuth))
    if hour_angle > 0:
        azimuth = 360 - azimuth

    # Calculate sunrise and sunset times
    cos_hour_angle_sunrise = -math.tan(lat_rad) * math.tan(dec_rad)
    if cos_hour_angle_sunrise >= 1:
        sunrise_hour = None  # No sunrise (polar night)
        sunset_hour = None
    elif cos_hour_angle_sunrise <= -1:
        sunrise_hour = 0  # No sunset (midnight sun)
        sunset_hour = 24
    else:
        hour_angle_sunrise = math.degrees(math.acos(cos_hour_angle_sunrise))
        sunrise_utc = solar_noon_utc - hour_angle_sunrise * 4
        sunset_utc = solar_noon_utc + hour_angle_sunrise * 4

        # Convert to local time
        local_offset = dt.utcoffset().total_seconds() / 3600 if dt.utcoffset() else 0
        sunrise_hour = (sunrise_utc / 60 + local_offset) % 24
        sunset_hour = (sunset_utc / 60 + local_offset) % 24

    return {
        "altitude": altitude,  # degrees above horizon
        "azimuth": azimuth,    # degrees from North (clockwise)
        "declination": declination,
        "hour_angle": hour_angle,
        "sunrise_hour": sunrise_hour,
        "sunset_hour": sunset_hour,
        "is_daylight": altitude > 0,
    }


def calculate_clear_sky_irradiance(altitude: float) -> float:
    """
    Calculate clear-sky Direct Normal Irradiance (DNI) based on sun altitude.

    Uses a simplified clear-sky model based on air mass.

    Args:
        altitude: Sun altitude in degrees above horizon

    Returns:
        Clear-sky DNI in W/m²
    """
    if altitude <= 0:
        return 0.0

    # Solar constant (W/m²)
    SOLAR_CONSTANT = 1361.0

    # Air mass calculation (Kasten-Young formula)
    altitude_rad = math.radians(altitude)
    if altitude > 0:
        # Air mass formula that works well for low sun angles
        air_mass = 1.0 / (math.sin(altitude_rad) + 0.50572 * (altitude + 6.07995) ** -1.6364)
    else:
        return 0.0

    # Clear-sky transmittance (simplified model)
    # Accounts for Rayleigh scattering and atmospheric absorption
    # This is a simplified version - real models use more complex factors
    transmittance = 0.7 ** (air_mass ** 0.678)

    # Direct Normal Irradiance
    dni = SOLAR_CONSTANT * transmittance

    return dni


def calculate_panel_irradiance(
    sun_altitude: float,
    sun_azimuth: float,
    panel_tilt: float,
    panel_azimuth: float,
    dni: float
) -> float:
    """
    Calculate irradiance on a tilted panel surface.

    Args:
        sun_altitude: Sun altitude in degrees
        sun_azimuth: Sun azimuth in degrees (from North)
        panel_tilt: Panel tilt from horizontal in degrees
        panel_azimuth: Panel azimuth in degrees (from North, direction panel faces)
        dni: Direct Normal Irradiance in W/m²

    Returns:
        Irradiance on panel surface in W/m²
    """
    if sun_altitude <= 0 or dni <= 0:
        return 0.0

    # Convert to radians
    sun_alt_rad = math.radians(sun_altitude)
    sun_az_rad = math.radians(sun_azimuth)
    panel_tilt_rad = math.radians(panel_tilt)
    panel_az_rad = math.radians(panel_azimuth)

    # Calculate angle of incidence on tilted surface
    # Using the formula for angle between sun vector and panel normal
    cos_incidence = (
        math.sin(sun_alt_rad) * math.cos(panel_tilt_rad) +
        math.cos(sun_alt_rad) * math.sin(panel_tilt_rad) *
        math.cos(sun_az_rad - panel_az_rad)
    )

    # If sun is behind the panel, no direct irradiance
    if cos_incidence <= 0:
        return 0.0

    # Direct irradiance on tilted surface
    direct_irradiance = dni * cos_incidence

    # Add diffuse component (simplified - typically 10-15% of global on clear days)
    # Global Horizontal Irradiance
    ghi = dni * math.sin(sun_alt_rad)
    diffuse_fraction = 0.1  # Assume 10% diffuse on clear day
    diffuse_irradiance = ghi * diffuse_fraction * (1 + math.cos(panel_tilt_rad)) / 2

    return direct_irradiance + diffuse_irradiance


def calculate_expected_power(solar_position: dict) -> dict:
    """
    Calculate expected PV power output based on solar position and array configuration.

    Args:
        solar_position: Dictionary from calculate_solar_position()

    Returns:
        Dictionary with expected power and irradiance data
    """
    if not solar_position["is_daylight"]:
        return {
            "expected_power_w": 0,
            "clear_sky_dni": 0,
            "effective_irradiance": 0,
            "face_details": [],
            "notes": "Sun is below horizon",
        }

    sun_altitude = solar_position["altitude"]
    sun_azimuth = solar_position["azimuth"]

    # Clear-sky DNI
    dni = calculate_clear_sky_irradiance(sun_altitude)

    # Calculate irradiance for each roof face
    face_details = []
    total_weighted_irradiance = 0.0

    for face in ROOF_FACES:
        face_irradiance = calculate_panel_irradiance(
            sun_altitude=sun_altitude,
            sun_azimuth=sun_azimuth,
            panel_tilt=PANEL_TILT,
            panel_azimuth=face["azimuth"],
            dni=dni
        )

        weighted_irradiance = face_irradiance * face["fraction"]
        total_weighted_irradiance += weighted_irradiance

        face_details.append({
            "name": face["name"],
            "azimuth": face["azimuth"],
            "fraction": face["fraction"],
            "irradiance": face_irradiance,
            "weighted_irradiance": weighted_irradiance,
        })

    # Expected power (assuming typical system efficiency of ~90%)
    # This accounts for inverter efficiency, wiring losses, temperature derating, etc.
    system_efficiency = 0.9
    expected_power_w = ARRAY_SIZE_KW * 1000 * (total_weighted_irradiance / 1000) * system_efficiency

    # Generate notes
    notes = []
    if sun_altitude < 10:
        notes.append("Very low sun angle - reduced output expected")
    if sun_altitude < 5:
        notes.append("Sun near horizon - minimal output expected")

    # Check if sun is behind any face
    for face in face_details:
        if face["irradiance"] < 10:
            notes.append(f"{face['name']} receiving minimal direct sun")

    return {
        "expected_power_w": expected_power_w,
        "clear_sky_dni": dni,
        "effective_irradiance": total_weighted_irradiance,
        "face_details": face_details,
        "notes": "; ".join(notes) if notes else "Good solar conditions",
    }


def format_time(hour: float) -> str:
    """Format decimal hour as HH:MM."""
    if hour is None:
        return "N/A"
    h = int(hour)
    m = int((hour - h) * 60)
    return f"{h:02d}:{m:02d}"


# Target configuration values
# IMPORTANT: This script is intentionally configured as a no-op against the
# current live inverter — every TARGET_CONFIG value below matches what's
# already on the device. Running this script will produce zero writes unless
# you've explicitly changed the inverter elsewhere. Keep this invariant when
# editing: change the live inverter via a one-shot apply_*.py script first,
# then mirror the new value here.
#
# NOTE: lead-acid params are not actively used in lithium-BMS mode (Pylon CAN
# comms control voltage/current limits), but kept aligned with current device
# values so running this script never silently rewrites them to stale defaults.
TARGET_CONFIG = {
    # Battery voltage limits (in volts) — matched to live BMS-mode device values
    "battery_min_voltage": 48.0,           # HOLD_LEAD_ACID_DISCHARGE_CUT_OFF_VOLT
    "system_charge_volt_limit": 55.0,      # HOLD_SYSTEM_CHARGE_VOLT_LIMIT
    "ac_charge_end_voltage": 55.0,         # HOLD_AC_CHARGE_END_BATTERY_VOLTAGE (3.44V/cell, ~95% SOC)
    "floating_voltage": 54.0,              # HOLD_FLOATING_VOLTAGE (3.375V/cell, ~90% rest)
    "charge_volt_ref": 55.0,               # HOLD_LEAD_ACID_CHARGE_VOLT_REF (3.44V/cell, ~95% SOC)
    # Current limits in Amps — matched to live BMS-reported limits (180 A discharge)
    "battery_max_charge_rate": 175,        # HOLD_LEAD_ACID_CHARGE_RATE
    "battery_max_discharge_rate": 190,     # HOLD_LEAD_ACID_DISCHARGE_RATE
    # Grid export settings
    # IMPORTANT: FUNC_PV_SELL_TO_GRID_EN is misleadingly named — it's actually
    # the EG4 "Export PV Only" toggle. True = restrict export to PV only (battery
    # cannot export to grid). False = both PV and battery can export. We want it
    # FALSE so the forced-discharge schedule can actually push battery to grid
    # during peak hours.
    "pv_sell_to_grid": False,    # FUNC_PV_SELL_TO_GRID_EN ("Export PV Only") — keep FALSE
    "feed_in_grid": True,        # FUNC_FEED_IN_GRID_EN ("Sell-back to grid") — keep TRUE
    "feed_in_grid_power_kw": 12, # HOLD_FEED_IN_GRID_POWER_PERCENT — max export kW (max 12)
    # Mode settings — current live inverter state.
    # Note: FUNC_LSP_CHARGE_PRIORITY_EN was previously False but reverted to
    # True (likely a register-21 bit-flip side effect when other reg-21 bits
    # were written). Self-consumption is enabled and operating as load-priority
    # in practice; leaving charge-priority True here to mirror the actual device.
    "pv_charge_priority": True,   # FUNC_LSP_CHARGE_PRIORITY_EN  (current live state)
    "self_consumption": True,     # FUNC_LSP_SELF_CONSUMPTION_EN - load priority
    "grid_ct_connected": True,    # FUNC_GRID_CT_CONNECTION_EN - external CT clamps installed
    # NOTE: voltage_control_mode (FUNC_LSP_BATT_VOLT_OR_SOC) returns REMOTE_SET_ERROR
    # on FlexBOSS - must be changed via local LCD interface if needed
    "voltage_control_mode": False,  # Cannot be changed via API on FlexBOSS
}

# Safety limits - these MUST NOT be exceeded
SAFETY_LIMITS = {
    "battery_min_voltage_floor": 40.0,  # Absolute minimum - below this damages battery
    "battery_max_voltage_ceiling": 58.0,  # Absolute maximum - above this damages battery
    "max_charge_current": 250,  # API maximum
    "max_discharge_current": 250,  # API maximum
}


def format_value(value, unit: str = "") -> str:
    """Format a value with its unit."""
    if value is None:
        return "N/A"
    if isinstance(value, bool):
        return "Enabled" if value else "Disabled"
    if isinstance(value, float):
        return f"{value:.1f}{unit}"
    return f"{value}{unit}"


def print_section(title: str):
    """Print a section header."""
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print("=" * 60)


def print_comparison(name: str, current, target, unit: str = ""):
    """Print a comparison of current vs target values."""
    current_str = format_value(current, unit)
    target_str = format_value(target, unit)

    if current == target:
        status = "[OK]"
    else:
        status = "[CHANGE]"

    print(f"  {name:.<35} {current_str:>12} -> {target_str:<12} {status}")


def safe_int(value, default: int = 0) -> int:
    """Safely convert value to int."""
    if value is None:
        return default
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def safe_bool(value, default: bool = False) -> bool:
    """Safely convert value to bool."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in ("true", "1", "yes", "on")
    return bool(value)


async def read_current_config(client, serial_number: str) -> dict:
    """Read current inverter configuration parameters."""
    print("\nReading current inverter configuration...")

    # Read all parameter ranges
    params = await client.api.control.read_device_parameters_ranges(serial_number)

    return {
        # Battery voltage limits (API returns volts directly)
        "battery_min_voltage": float(safe_int(params.get("HOLD_LEAD_ACID_DISCHARGE_CUT_OFF_VOLT", 0))),
        "system_charge_volt_limit": float(safe_int(params.get("HOLD_SYSTEM_CHARGE_VOLT_LIMIT", 0))),
        "ac_charge_end_voltage": float(safe_int(params.get("HOLD_AC_CHARGE_END_BATTERY_VOLTAGE", 0))),
        "floating_voltage": float(safe_int(params.get("HOLD_FLOATING_VOLTAGE", 0))),
        # Current limits
        "battery_max_charge_rate": safe_int(params.get("HOLD_LEAD_ACID_CHARGE_RATE", 0)),
        "battery_max_discharge_rate": safe_int(params.get("HOLD_LEAD_ACID_DISCHARGE_RATE", 0)),
        # Grid export
        "pv_sell_to_grid": safe_bool(params.get("FUNC_PV_SELL_TO_GRID_EN", False)),
        # Mode settings
        "pv_charge_priority": safe_bool(params.get("FUNC_LSP_CHARGE_PRIORITY_EN", False)),
        "voltage_control_mode": safe_bool(params.get("FUNC_LSP_BATT_VOLT_OR_SOC", False)),
        # Additional info for display
        "charge_volt_ref": float(safe_int(params.get("HOLD_LEAD_ACID_CHARGE_VOLT_REF", 0))),
        # PV-related settings
        "self_consumption": safe_bool(params.get("FUNC_LSP_SELF_CONSUMPTION_EN", False)),
        "grid_ct_connected": safe_bool(params.get("FUNC_GRID_CT_CONNECTION_EN", False)),
        "feed_in_grid": safe_bool(params.get("FUNC_FEED_IN_GRID_EN", False)),
        "feed_in_grid_power_kw": safe_int(params.get("HOLD_FEED_IN_GRID_POWER_PERCENT", 0)),
        "start_pv_volt": safe_int(params.get("HOLD_START_PV_VOLT", 0)),
        "pv_grid_off": safe_bool(params.get("FUNC_PV_GRID_OFF_EN", False)),
        "run_without_grid": safe_bool(params.get("FUNC_RUN_WITHOUT_GRID", False)),
        "raw_params": params,
    }


async def apply_configuration(client, serial_number: str, current_config: dict) -> bool:
    """Apply the target configuration to the inverter."""
    print("\nApplying configuration changes...")

    changes_made = []
    errors = []

    # Helper to write parameter
    async def write_param(param_name: str, value: str, description: str):
        try:
            result = await client.api.control.write_parameter(serial_number, param_name, value)
            if result.success:
                changes_made.append(f"  [OK] {description}: {value}")
            else:
                errors.append(f"  [FAIL] {description}: {result}")
        except Exception as e:
            errors.append(f"  [ERROR] {description}: {e}")

    # Helper to write function parameter
    async def write_func(func_name: str, enable: bool, description: str):
        try:
            result = await client.api.control.control_function(serial_number, func_name, enable)
            if result.success:
                changes_made.append(f"  [OK] {description}: {'Enabled' if enable else 'Disabled'}")
            else:
                errors.append(f"  [FAIL] {description}: {result}")
        except Exception as e:
            errors.append(f"  [ERROR] {description}: {e}")

    # Apply battery voltage limits (only if different)
    if current_config["battery_min_voltage"] != TARGET_CONFIG["battery_min_voltage"]:
        # Validate safety limit
        if TARGET_CONFIG["battery_min_voltage"] < SAFETY_LIMITS["battery_min_voltage_floor"]:
            errors.append(f"  [SAFETY] Battery min voltage {TARGET_CONFIG['battery_min_voltage']}V is below safety floor!")
        else:
            await write_param(
                "HOLD_LEAD_ACID_DISCHARGE_CUT_OFF_VOLT",
                str(int(TARGET_CONFIG["battery_min_voltage"])),
                "Battery Min Voltage (Discharge Cutoff)"
            )

    if current_config.get("system_charge_volt_limit") != TARGET_CONFIG["system_charge_volt_limit"]:
        if TARGET_CONFIG["system_charge_volt_limit"] > SAFETY_LIMITS["battery_max_voltage_ceiling"]:
            errors.append(f"  [SAFETY] System charge volt limit {TARGET_CONFIG['system_charge_volt_limit']}V exceeds safety ceiling!")
        else:
            await write_param(
                "HOLD_SYSTEM_CHARGE_VOLT_LIMIT",
                str(int(TARGET_CONFIG["system_charge_volt_limit"])),
                "System Charge Voltage Limit"
            )

    if current_config.get("ac_charge_end_voltage") != TARGET_CONFIG["ac_charge_end_voltage"]:
        if TARGET_CONFIG["ac_charge_end_voltage"] > SAFETY_LIMITS["battery_max_voltage_ceiling"]:
            errors.append(f"  [SAFETY] AC charge end voltage {TARGET_CONFIG['ac_charge_end_voltage']}V exceeds safety ceiling!")
        else:
            await write_param(
                "HOLD_AC_CHARGE_END_BATTERY_VOLTAGE",
                str(int(TARGET_CONFIG["ac_charge_end_voltage"])),
                "AC Charge End Voltage"
            )

    if current_config["floating_voltage"] != TARGET_CONFIG["floating_voltage"]:
        if TARGET_CONFIG["floating_voltage"] > SAFETY_LIMITS["battery_max_voltage_ceiling"]:
            errors.append(f"  [SAFETY] Floating voltage {TARGET_CONFIG['floating_voltage']}V exceeds safety ceiling!")
        else:
            await write_param(
                "HOLD_FLOATING_VOLTAGE",
                str(int(TARGET_CONFIG["floating_voltage"])),
                "Floating Voltage"
            )

    if current_config.get("charge_volt_ref") != TARGET_CONFIG.get("charge_volt_ref"):
        if TARGET_CONFIG["charge_volt_ref"] > SAFETY_LIMITS["battery_max_voltage_ceiling"]:
            errors.append(f"  [SAFETY] Charge volt ref {TARGET_CONFIG['charge_volt_ref']}V exceeds safety ceiling!")
        else:
            await write_param(
                "HOLD_LEAD_ACID_CHARGE_VOLT_REF",
                str(int(TARGET_CONFIG["charge_volt_ref"])),
                "Charge Voltage Reference"
            )

    # Apply current limits
    if current_config["battery_max_charge_rate"] != TARGET_CONFIG["battery_max_charge_rate"]:
        await write_param(
            "HOLD_LEAD_ACID_CHARGE_RATE",
            str(TARGET_CONFIG["battery_max_charge_rate"]),
            "Battery Max Charge Rate"
        )

    if current_config["battery_max_discharge_rate"] != TARGET_CONFIG["battery_max_discharge_rate"]:
        await write_param(
            "HOLD_LEAD_ACID_DISCHARGE_RATE",
            str(TARGET_CONFIG["battery_max_discharge_rate"]),
            "Battery Max Discharge Rate"
        )

    # Apply grid export setting (PV sell to grid)
    if current_config["pv_sell_to_grid"] != TARGET_CONFIG["pv_sell_to_grid"]:
        await write_func(
            "FUNC_PV_SELL_TO_GRID_EN",
            TARGET_CONFIG["pv_sell_to_grid"],
            "PV Sell to Grid"
        )

    # Apply mode settings (these may fail on some devices - that's OK)
    if current_config["pv_charge_priority"] != TARGET_CONFIG["pv_charge_priority"]:
        await write_func(
            "FUNC_LSP_CHARGE_PRIORITY_EN",
            TARGET_CONFIG["pv_charge_priority"],
            "PV Charge Priority Mode"
        )

    if current_config.get("self_consumption") != TARGET_CONFIG.get("self_consumption"):
        await write_func(
            "FUNC_LSP_SELF_CONSUMPTION_EN",
            TARGET_CONFIG["self_consumption"],
            "Self-Consumption (Load Priority)"
        )

    if current_config.get("grid_ct_connected") != TARGET_CONFIG.get("grid_ct_connected"):
        await write_func(
            "FUNC_GRID_CT_CONNECTION_EN",
            TARGET_CONFIG["grid_ct_connected"],
            "Grid CT Connected"
        )

    if current_config["voltage_control_mode"] != TARGET_CONFIG["voltage_control_mode"]:
        await write_func(
            "FUNC_LSP_BATT_VOLT_OR_SOC",
            TARGET_CONFIG["voltage_control_mode"],
            "Voltage Control Mode"
        )

    # Apply grid export settings
    if current_config.get("feed_in_grid") != TARGET_CONFIG.get("feed_in_grid"):
        await write_func(
            "FUNC_FEED_IN_GRID_EN",
            TARGET_CONFIG["feed_in_grid"],
            "Feed In Grid"
        )

    if current_config.get("feed_in_grid_power_kw") != TARGET_CONFIG.get("feed_in_grid_power_kw"):
        await write_param(
            "HOLD_FEED_IN_GRID_POWER_PERCENT",
            str(TARGET_CONFIG["feed_in_grid_power_kw"]),
            "Max Grid Export Power (kW)"
        )

    # Report results
    if changes_made:
        print("\nChanges applied:")
        for change in changes_made:
            print(change)

    if errors:
        print("\nErrors encountered:")
        for error in errors:
            print(error)
        return False

    if not changes_made:
        print("\nNo changes needed - configuration already matches target.")

    return True


async def display_live_stats(inverter):
    """Display live statistics for the inverter."""

    # Get current time and calculate solar position
    now = datetime.now(TIMEZONE)
    solar_pos = calculate_solar_position(now, LATITUDE, LONGITUDE)
    expected = calculate_expected_power(solar_pos)

    # Solar Position Section
    print_section("SOLAR POSITION & EXPECTED OUTPUT")
    print(f"\n  Location: Oakland, CA ({LATITUDE:.4f}°N, {LONGITUDE:.4f}°W)")
    print(f"  Current Time: {now.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"  Sunrise: {format_time(solar_pos['sunrise_hour'])}  |  Sunset: {format_time(solar_pos['sunset_hour'])}")

    print(f"\n  Sun Position:")
    print(f"    Altitude: {solar_pos['altitude']:.1f}° {'(above horizon)' if solar_pos['is_daylight'] else '(BELOW HORIZON)'}")
    print(f"    Azimuth: {solar_pos['azimuth']:.1f}° (from North)")

    print(f"\n  Clear-Sky Irradiance Model:")
    print(f"    Direct Normal Irradiance (DNI): {expected['clear_sky_dni']:.0f} W/m²")

    print(f"\n  Panel Irradiance by Roof Face:")
    for face in expected["face_details"]:
        status = "OK" if face["irradiance"] > 50 else "LOW" if face["irradiance"] > 0 else "NONE"
        # Calculate angle difference between sun and panel
        angle_diff = abs(solar_pos["azimuth"] - face["azimuth"])
        if angle_diff > 180:
            angle_diff = 360 - angle_diff
        print(f"    {face['name']} ({face['azimuth']:.0f}°): {face['irradiance']:.0f} W/m² [{status}] (sun {angle_diff:.0f}° away)")

    # Show when peak production will occur
    print(f"\n  Peak Production Timing:")
    # SW panels (217°) will peak when sun is at ~217° azimuth
    # This happens in the afternoon
    # NE panels (37°) get best sun in early morning when sun is in the NE/E
    current_hour = now.hour + now.minute / 60

    # Rough estimate: sun moves ~15° per hour
    # SW panels (217°): sun needs to move from current azimuth to 217°
    sw_angle_to_go = 217 - solar_pos["azimuth"]
    if sw_angle_to_go > 0:
        hours_to_sw_peak = sw_angle_to_go / 15
        sw_peak_time = current_hour + hours_to_sw_peak
        if sw_peak_time < solar_pos["sunset_hour"]:
            sw_peak_h = int(sw_peak_time)
            sw_peak_m = int((sw_peak_time - sw_peak_h) * 60)
            print(f"    SW Face (7kW): Best around ~{sw_peak_h}:{sw_peak_m:02d} when sun reaches SW")
        else:
            print(f"    SW Face (7kW): Sun won't reach optimal angle today")
    else:
        print(f"    SW Face (7kW): Past peak for today")

    # NE panels peak in early morning
    if solar_pos["azimuth"] < 90:
        print(f"    NE Face (7kW): Currently near peak (early morning)")
    elif solar_pos["azimuth"] < 180:
        print(f"    NE Face (7kW): Past peak - sun moved to south/west")
    else:
        print(f"    NE Face (7kW): Getting afternoon reflected/diffuse light only")

    # Calculate theoretical max if panels faced sun directly
    theoretical_max = ARRAY_SIZE_KW * 1000 * (expected['clear_sky_dni'] / 1000) * 0.9
    orientation_loss = ((theoretical_max - expected['expected_power_w']) / theoretical_max) * 100 if theoretical_max > 0 else 0

    print(f"\n  Expected vs Actual Output:")
    print(f"    If panels faced sun directly: {theoretical_max:.0f}W")
    print(f"    Expected with your roof angles: {expected['expected_power_w']:.0f}W ({orientation_loss:.0f}% orientation loss)")

    # PV (Solar) Status - show first for quick visibility
    print_section("ACTUAL PV OUTPUT")

    # MPPT 1
    pv1_v = inverter.pv1_voltage
    pv1_p = inverter.pv1_power
    pv1_i = pv1_p / pv1_v if pv1_v > 0 else 0
    print(f"\n    MPPT 1: {pv1_v:.1f}V, {pv1_p}W, {pv1_i:.2f}A")

    # MPPT 2
    pv2_v = inverter.pv2_voltage
    pv2_p = inverter.pv2_power
    pv2_i = pv2_p / pv2_v if pv2_v > 0 else 0
    print(f"    MPPT 2: {pv2_v:.1f}V, {pv2_p}W, {pv2_i:.2f}A")

    # MPPT 3 (if available)
    pv3_v = inverter.pv3_voltage
    pv3_p = inverter.pv3_power
    if pv3_v > 0 or pv3_p > 0:
        pv3_i = pv3_p / pv3_v if pv3_v > 0 else 0
        print(f"    MPPT 3: {pv3_v:.1f}V, {pv3_p}W, {pv3_i:.2f}A")

    total_pv = inverter.pv_total_power
    print(f"\n    Total Actual PV: {total_pv}W")
    print(f"    Total Expected:  {expected['expected_power_w']:.0f}W")

    # Compare actual vs expected
    if expected["expected_power_w"] > 0:
        ratio = (total_pv / expected["expected_power_w"]) * 100
        if ratio > 90:
            status = "EXCELLENT - Performing as expected"
        elif ratio > 70:
            status = "GOOD - Minor losses (clouds, dirt, etc.)"
        elif ratio > 50:
            status = "FAIR - Check for shading or issues"
        elif ratio > 20:
            status = "LOW - Possible throttling or obstruction"
        else:
            status = "VERY LOW - Check system for issues"
        print(f"    Performance: {ratio:.0f}% of expected ({status})")
    else:
        if total_pv > 0:
            print(f"    Performance: Producing power despite low expected (diffuse light)")
        else:
            print(f"    Performance: No output expected (sun below horizon or very low)")

    # Notes
    if expected["notes"]:
        print(f"\n    Notes: {expected['notes']}")

    # Battery Status
    print_section("BATTERY & GRID STATUS")
    print("\n  Battery:")
    print(f"    Voltage: {inverter.battery_voltage:.1f}V")
    print(f"    SOC: {inverter.battery_soc}%")
    print(f"    Charge Power: {inverter.battery_charge_power}W")
    print(f"    Discharge Power: {inverter.battery_discharge_power}W")
    print(f"    Temperature: {inverter.battery_temperature}C")
    print(f"    Max Charge Current: {inverter.max_charge_current:.1f}A")
    print(f"    Max Discharge Current: {inverter.max_discharge_current:.1f}A")

    # Grid Status
    print("\n  Grid:")
    print(f"    Voltage: {inverter.grid_voltage_r:.1f}V")
    print(f"    Frequency: {inverter.grid_frequency:.2f}Hz")
    print(f"    Power to Grid: {inverter.power_to_grid}W")
    print(f"    Power to User: {inverter.power_to_user}W")

    # Inverter Status
    print("\n  Inverter:")
    print(f"    Output Power: {inverter.inverter_power}W")
    print(f"    Temperature: {inverter.inverter_temperature}C")
    print(f"    Status: {inverter.status_text}")

    # Return data for throttling analysis
    return {
        "expected_power_w": expected["expected_power_w"],
        "actual_power_w": total_pv,
        "battery_soc": inverter.battery_soc,
        "power_to_grid": inverter.power_to_grid,
        "power_to_user": inverter.power_to_user,
    }


def analyze_throttling(stats: dict, config: dict):
    """Analyze if PV is being throttled and why."""
    print_section("THROTTLING ANALYSIS")

    issues = []
    recommendations = []

    expected = stats["expected_power_w"]
    actual = stats["actual_power_w"]
    battery_soc = stats["battery_soc"]
    power_to_grid = stats["power_to_grid"]
    power_to_user = stats["power_to_user"]

    # Calculate performance ratio
    if expected > 100:  # Only analyze if expecting significant power
        ratio = (actual / expected) * 100

        if ratio < 70:
            print(f"\n  ⚠️  PV THROTTLING DETECTED")
            print(f"      Producing {actual}W but could produce ~{expected:.0f}W")
            print(f"      Performance: {ratio:.0f}% of potential")

            # Check why throttling might be occurring
            if battery_soc >= 99:
                issues.append("Battery at 100% SOC - cannot absorb more charge")

            if power_to_grid == 0:
                issues.append("Not exporting to grid (0W to grid)")

            if not config.get("feed_in_grid"):
                issues.append("FUNC_FEED_IN_GRID_EN is DISABLED")
                recommendations.append("Enable FUNC_FEED_IN_GRID_EN to allow grid export")

            feed_in_power = config.get("feed_in_grid_power_kw", 0)
            if feed_in_power == 0:
                issues.append(f"HOLD_FEED_IN_GRID_POWER_PERCENT is 0 kW (no export allowed)")
                recommendations.append("Set HOLD_FEED_IN_GRID_POWER_PERCENT to allow export (e.g., 12 kW)")

            if not config.get("pv_sell_to_grid"):
                issues.append("FUNC_PV_SELL_TO_GRID_EN is DISABLED")
                recommendations.append("Enable FUNC_PV_SELL_TO_GRID_EN")

            # Check self-consumption conflict
            if config.get("self_consumption") and battery_soc >= 99 and power_to_grid == 0:
                issues.append("Self-consumption mode with full battery and no grid export = PV must throttle")

            if issues:
                print(f"\n  Likely causes:")
                for issue in issues:
                    print(f"    • {issue}")

            if recommendations:
                print(f"\n  Recommendations:")
                for rec in recommendations:
                    print(f"    → {rec}")

            # Estimate lost power
            lost_power = expected - actual
            if lost_power > 0:
                # Assume 6 peak sun hours per day in Oakland
                lost_daily_kwh = (lost_power / 1000) * 6
                print(f"\n  Estimated impact:")
                print(f"    Lost power right now: ~{lost_power:.0f}W")
                print(f"    Potential daily loss: ~{lost_daily_kwh:.1f} kWh (if throttled all day)")

        else:
            print(f"\n  ✓ PV output looks normal ({ratio:.0f}% of expected)")
            print(f"    Any difference may be due to clouds, dirt, or temperature effects")
    else:
        print(f"\n  ℹ️  Low solar conditions - not enough data for throttling analysis")


async def display_config_comparison(current_config: dict):
    """Display comparison of current vs target configuration."""
    print_section("CONFIGURATION COMPARISON")
    print("\n  Current -> Target")
    print("  " + "-" * 56)

    print_comparison(
        "Battery Min Voltage",
        current_config["battery_min_voltage"],
        TARGET_CONFIG["battery_min_voltage"],
        "V"
    )
    print_comparison(
        "System Charge Volt Limit",
        current_config.get("system_charge_volt_limit", 0),
        TARGET_CONFIG["system_charge_volt_limit"],
        "V"
    )
    print_comparison(
        "AC Charge End Voltage",
        current_config.get("ac_charge_end_voltage", 0),
        TARGET_CONFIG["ac_charge_end_voltage"],
        "V"
    )
    print_comparison(
        "Floating Voltage",
        current_config["floating_voltage"],
        TARGET_CONFIG["floating_voltage"],
        "V"
    )
    print_comparison(
        "Charge Voltage Reference",
        current_config.get("charge_volt_ref", 0),
        TARGET_CONFIG.get("charge_volt_ref", 48),
        "V"
    )
    print_comparison(
        "Max Charge Rate",
        current_config["battery_max_charge_rate"],
        TARGET_CONFIG["battery_max_charge_rate"],
        "A"
    )
    print_comparison(
        "Max Discharge Rate",
        current_config["battery_max_discharge_rate"],
        TARGET_CONFIG["battery_max_discharge_rate"],
        "A"
    )
    print_comparison(
        "PV Sell to Grid",
        current_config["pv_sell_to_grid"],
        TARGET_CONFIG["pv_sell_to_grid"],
        ""
    )
    print_comparison(
        "PV Charge Priority",
        current_config["pv_charge_priority"],
        TARGET_CONFIG["pv_charge_priority"],
        ""
    )
    print_comparison(
        "Self-Consumption (Load Priority)",
        current_config.get("self_consumption", False),
        TARGET_CONFIG.get("self_consumption", True),
        ""
    )
    print_comparison(
        "Grid CT Connected",
        current_config.get("grid_ct_connected", False),
        TARGET_CONFIG.get("grid_ct_connected", True),
        ""
    )
    print_comparison(
        "Voltage Control Mode",
        current_config["voltage_control_mode"],
        TARGET_CONFIG["voltage_control_mode"],
        ""
    )
    print_comparison(
        "Feed In Grid",
        current_config.get("feed_in_grid", False),
        TARGET_CONFIG.get("feed_in_grid", True),
        ""
    )
    print_comparison(
        "Max Grid Export Power",
        current_config.get("feed_in_grid_power_kw", 0),
        TARGET_CONFIG.get("feed_in_grid_power_kw", 12),
        " kW"
    )

    # Show additional info
    if "charge_volt_ref" in current_config:
        print(f"\n  Additional Info:")
        print(f"    HOLD_LEAD_ACID_CHARGE_VOLT_REF: {current_config['charge_volt_ref']}V")

    # Show PV-related settings
    print(f"\n  PV/Grid Export Settings:")
    print(f"    Self Consumption Mode: {'Enabled' if current_config.get('self_consumption') else 'Disabled'}")
    print(f"    Grid CT Connected: {'Yes' if current_config.get('grid_ct_connected') else 'No'}")
    print(f"    Feed In Grid: {'Enabled' if current_config.get('feed_in_grid') else 'Disabled'}")
    print(f"    Max Feed In Power: {current_config.get('feed_in_grid_power_kw', 0)} kW")
    print(f"    Start PV Voltage: {current_config.get('start_pv_volt', 0)}V")
    print(f"    PV Grid Off: {'Enabled' if current_config.get('pv_grid_off') else 'Disabled'}")
    print(f"    Run Without Grid: {'Enabled' if current_config.get('run_without_grid') else 'Disabled'}")


async def main():
    # Create client with credentials
    async with LuxpowerClient(
        username=USERNAME,
        password=PASSWORD,
        base_url=BASE_URL
    ) as client:
        # Load all stations with device hierarchy
        print("Connecting to EG4 monitoring system...")
        stations = await Station.load_all(client)
        print(f"Found {len(stations)} stations")

        # Work with first station
        station = stations[0]
        print(f"\nStation: {station.name}")

        # Find the first regular inverter (not GridBOSS)
        inverter = None
        for inv in station.all_inverters:
            inverter = inv
            break

        if not inverter:
            print("No inverter found!")
            return

        print(f"Inverter: {inverter.model} ({inverter.serial_number})")

        # Refresh inverter data
        await inverter.refresh()

        # Read current configuration
        current_config = await read_current_config(client, inverter.serial_number)

        # Display configuration comparison
        await display_config_comparison(current_config)

        # Display live statistics
        stats = await display_live_stats(inverter)

        # Analyze throttling
        analyze_throttling(stats, current_config)

        # Check if any changes are needed
        changes_needed = (
            current_config["battery_min_voltage"] != TARGET_CONFIG["battery_min_voltage"] or
            current_config.get("system_charge_volt_limit") != TARGET_CONFIG["system_charge_volt_limit"] or
            current_config.get("ac_charge_end_voltage") != TARGET_CONFIG["ac_charge_end_voltage"] or
            current_config["floating_voltage"] != TARGET_CONFIG["floating_voltage"] or
            current_config.get("charge_volt_ref") != TARGET_CONFIG.get("charge_volt_ref") or
            current_config["battery_max_charge_rate"] != TARGET_CONFIG["battery_max_charge_rate"] or
            current_config["battery_max_discharge_rate"] != TARGET_CONFIG["battery_max_discharge_rate"] or
            current_config["pv_sell_to_grid"] != TARGET_CONFIG["pv_sell_to_grid"] or
            current_config["pv_charge_priority"] != TARGET_CONFIG["pv_charge_priority"] or
            current_config.get("self_consumption") != TARGET_CONFIG.get("self_consumption") or
            current_config.get("grid_ct_connected") != TARGET_CONFIG.get("grid_ct_connected") or
            current_config["voltage_control_mode"] != TARGET_CONFIG["voltage_control_mode"] or
            current_config.get("feed_in_grid") != TARGET_CONFIG.get("feed_in_grid") or
            current_config.get("feed_in_grid_power_kw") != TARGET_CONFIG.get("feed_in_grid_power_kw")
        )

        if not changes_needed:
            print_section("NO CHANGES NEEDED")
            print("\nConfiguration already matches target values.")
            return

        # Safety warnings
        print_section("SAFETY WARNINGS")
        print("""
  *** IMPORTANT BATTERY SAFETY LIMITS ***

  These settings control battery voltage and current limits.
  INCORRECT VALUES CAN DAMAGE YOUR BATTERY OR CAUSE FIRE!

  Target Configuration:
  - Min Voltage: 44.0V (protects battery from over-discharge)
  - Max Voltage: 48.0V (protects battery from over-charge)
  - Max Charge/Discharge: 50A (2.4kW at 48V nominal)

  Verify these values are appropriate for your battery system
  before proceeding!
        """)

        # Ask for confirmation
        print_section("CONFIRMATION REQUIRED")
        response = input("\nApply these configuration changes? (yes/no): ").strip().lower()

        if response != "yes":
            print("\nConfiguration changes cancelled.")
            return

        # Apply configuration
        success = await apply_configuration(client, inverter.serial_number, current_config)

        if success:
            print_section("CONFIGURATION COMPLETE")

            # Wait a moment for changes to take effect
            print("\nWaiting for changes to take effect...")
            await asyncio.sleep(2)

            # Re-read and display final configuration
            print("\nVerifying final configuration...")
            final_config = await read_current_config(client, inverter.serial_number)
            await display_config_comparison(final_config)

            # Refresh and show final stats
            await inverter.refresh(force=True)
            await display_live_stats(inverter)
        else:
            print_section("CONFIGURATION FAILED")
            print("\nSome changes could not be applied. Please check errors above.")


if __name__ == "__main__":
    asyncio.run(main())
