"""Read current inverter config focused on import-causing settings.

Goal: confirm load-priority / AC-charge / CT / discharge config is what
we want to eliminate Pattern 2 (mid-day spillover) imports.
"""

import asyncio
from pylxpweb import LuxpowerClient
from pylxpweb.devices.station import Station

from _env import USERNAME, PASSWORD, BASE_URL


# Settings to inspect, grouped by topic. Each entry: (key, want_value_or_note)
GROUPS = {
    "Load priority / self-consumption (Pattern 2)": [
        ("FUNC_LSP_SELF_CONSUMPTION_EN", "should be ENABLED for load priority"),
        ("FUNC_LSP_CHARGE_PRIORITY_EN", "PV-charge-priority — competes with self-consumption"),
        ("FUNC_LSP_BATT_VOLT_OR_SOC", "should be SOC (BMS reports it) — i.e. False for SOC mode"),
    ],
    "Grid CT (must work correctly to drive import to zero)": [
        ("FUNC_GRID_CT_CONNECTION_EN", "should be ENABLED"),
        ("FUNC_PV_CT_INSTALLED", "PV CT installed?"),
    ],
    "AC charge from grid (should usually be off)": [
        ("FUNC_AC_CHARGE_EN", "should be DISABLED unless TOU charging"),
        ("HOLD_AC_CHARGE_START_HOUR", "AC charge schedule"),
        ("HOLD_AC_CHARGE_END_HOUR", "AC charge schedule"),
        ("HOLD_AC_CHARGE_START_MINUTE", "AC charge schedule"),
        ("HOLD_AC_CHARGE_END_MINUTE", "AC charge schedule"),
        ("HOLD_AC_CHARGE_SOC_LIMIT", "AC charge stop SOC"),
        ("HOLD_AC_CHARGE_BATTERY_CURRENT", "AC charge current"),
        ("HOLD_AC_CHARGE_END_BATTERY_VOLTAGE", "AC charge cutoff voltage"),
    ],
    "Forced charge / forced discharge schedules (stale schedules cause weirdness)": [
        ("FUNC_FORCED_CHG_EN", "forced charge schedule enabled?"),
        ("FUNC_FORCED_DISCHG_EN", "forced discharge schedule enabled?"),
        ("HOLD_FORCED_CHG_START_HOUR", "forced charge schedule"),
        ("HOLD_FORCED_CHG_END_HOUR", "forced charge schedule"),
        ("HOLD_FORCED_DISCHG_START_HOUR", "forced discharge schedule"),
        ("HOLD_FORCED_DISCHG_END_HOUR", "forced discharge schedule"),
        ("HOLD_FORCED_DISCHG_SOC_LIMIT", "forced discharge SOC limit"),
        ("HOLD_FORCED_CHG_SOC_LIMIT", "forced charge SOC limit"),
    ],
    "SOC limits (the 95% ceiling and discharge floor)": [
        ("HOLD_SYSTEM_CHARGE_SOC_LIMIT", "max charge SOC — the 95% ceiling?"),
        ("HOLD_DISCHG_CUT_OFF_SOC_EOD", "discharge SOC floor"),
        ("HOLD_ON_GRID_DISCHG_CUT_OFF_SOC_LIMIT", "on-grid discharge floor"),
        ("HOLD_OFF_GRID_DISCHG_CUT_OFF_SOC_LIMIT", "off-grid discharge floor"),
    ],
    "Grid export": [
        ("FUNC_FEED_IN_GRID_EN", "feed-in enabled"),
        ("FUNC_PV_SELL_TO_GRID_EN", "PV sell to grid"),
        ("HOLD_FEED_IN_GRID_POWER_PERCENT", "max grid feed-in power"),
    ],
    "Battery type / BMS": [
        ("HOLD_BATTERY_TYPE", "battery type code"),
        ("HOLD_LEAD_ACID_DISCHARGE_CUT_OFF_VOLT",
         "(should be irrelevant in lithium BMS mode)"),
        ("HOLD_LEAD_ACID_CHARGE_VOLT_REF",
         "(should be irrelevant in lithium BMS mode)"),
    ],
    "Working mode": [
        ("FUNC_PV_GRID_OFF_EN", "PV-grid-off mode"),
        ("FUNC_RUN_WITHOUT_GRID", "run without grid"),
        ("FUNC_NORMAL_OR_STANDBY", "normal vs standby"),
        ("FUNC_TAKE_LOAD_TOGETHER", "load-together mode"),
        ("FUNC_NO_BATTERY", "no-battery mode"),
    ],
}


def fmt(v):
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "ENABLED" if v else "disabled"
    s = str(v)
    if s.lower() in ("true", "1"):
        return "ENABLED"
    if s.lower() in ("false", "0"):
        return "disabled"
    return s


async def main():
    async with LuxpowerClient(
        username=USERNAME, password=PASSWORD, base_url=BASE_URL
    ) as client:
        stations = await Station.load_all(client)
        inverter = next(iter(stations[0].all_inverters))
        serial = inverter.serial_number
        print(f"Inverter: {inverter.model} ({serial})\n")

        params = await client.api.control.read_device_parameters_ranges(serial)
        print(f"Read {len(params)} parameters from inverter\n")

        # Print each group
        for group_name, keys in GROUPS.items():
            print(f"=== {group_name} ===")
            for key, note in keys:
                val = params.get(key)
                marker = "" if key in params else "  (not in returned set)"
                print(f"  {key:<46} {fmt(val):<12} — {note}{marker}")
            print()

        # Look for any *DISCHG_CUT_OFF*, *AC_CHARGE*, *FORCED_*, *SOC_LIMIT* keys
        # we might have missed
        print("=== Other potentially relevant keys present ===")
        seen = {k for keys in GROUPS.values() for k, _ in keys}
        patterns = ["DISCHG", "CHARGE", "FORCED", "SOC", "GRID_CT",
                    "BMS", "PRIORITY", "SELL", "FEED_IN", "PEAK_SHAV"]
        for k in sorted(params.keys()):
            if k in seen:
                continue
            if any(p in k.upper() for p in patterns):
                print(f"  {k:<46} = {fmt(params[k])}")


if __name__ == "__main__":
    asyncio.run(main())
