"""Verify/preserve the forced-discharge schedule (PG&E peak window 16:00–21:00).

Targets match the current known-good state — re-running this script writes
the same values back (idempotent). Originally moved from a 21:00–23:59 +
00:00–05:30 overnight schedule to a single peak-aligned window.

  Slot 0:  16:00–20:59   (active, PG&E peak window)
  Slot 1:  disabled (00:00–00:00)
  Slot 2:  disabled (untouched)
  Power command and SOC floor preserved separately.
"""

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from pylxpweb import LuxpowerClient
from pylxpweb.devices.station import Station

from _env import USERNAME, PASSWORD, BASE_URL
TZ = ZoneInfo("America/Los_Angeles")

# (param_name, new_value_str)
CHANGES = [
    # Slot 0: shift to 16:00-20:59
    ("HOLD_FORCED_DISCHARGE_START_HOUR", "16"),
    ("HOLD_FORCED_DISCHARGE_START_MINUTE", "0"),
    ("HOLD_FORCED_DISCHARGE_END_HOUR", "20"),
    ("HOLD_FORCED_DISCHARGE_END_MINUTE", "59"),
    # Slot 1: disable (was 00:00-05:30)
    ("HOLD_FORCED_DISCHARGE_START_HOUR_1", "0"),
    ("HOLD_FORCED_DISCHARGE_START_MINUTE_1", "0"),
    ("HOLD_FORCED_DISCHARGE_END_HOUR_1", "0"),
    ("HOLD_FORCED_DISCHARGE_END_MINUTE_1", "0"),
]

WATCH = [k for k, _ in CHANGES] + [
    "HOLD_FORCED_DISCHARGE_START_HOUR_2",
    "HOLD_FORCED_DISCHARGE_END_HOUR_2",
    "FUNC_FORCED_DISCHG_EN",
    "HOLD_FORCED_DISCHG_POWER_CMD",
    "HOLD_FORCED_DISCHG_SOC_LIMIT",
    "FUNC_LSP_SELF_CONSUMPTION_EN",
    "FUNC_LSP_CHARGE_PRIORITY_EN",
    "HOLD_FEED_IN_GRID_POWER_PERCENT",
    "HOLD_SYSTEM_CHARGE_SOC_LIMIT",
]


async def fresh(client, serial):
    client.invalidate_cache_for_device(serial)
    p = await client.api.control.read_device_parameters_ranges(serial)
    return {k: p.get(k) for k in WATCH}


def fmt_window(start_h, start_m, end_h, end_m):
    try:
        sh, sm, eh, em = (int(start_h), int(start_m), int(end_h), int(end_m))
    except (TypeError, ValueError):
        return "?"
    if (sh, sm, eh, em) == (0, 0, 0, 0):
        return "disabled"
    return f"{sh:02d}:{sm:02d} – {eh:02d}:{em:02d}"


def show_schedule(label, p):
    print(f"\n{label}")
    print(f"  FUNC_FORCED_DISCHG_EN          = {p.get('FUNC_FORCED_DISCHG_EN')}")
    print(f"  Slot 0:  {fmt_window(p.get('HOLD_FORCED_DISCHARGE_START_HOUR'), p.get('HOLD_FORCED_DISCHARGE_START_MINUTE'), p.get('HOLD_FORCED_DISCHARGE_END_HOUR'), p.get('HOLD_FORCED_DISCHARGE_END_MINUTE'))}")
    print(f"  Slot 1:  {fmt_window(p.get('HOLD_FORCED_DISCHARGE_START_HOUR_1'), p.get('HOLD_FORCED_DISCHARGE_START_MINUTE_1'), p.get('HOLD_FORCED_DISCHARGE_END_HOUR_1'), p.get('HOLD_FORCED_DISCHARGE_END_MINUTE_1'))}")
    print(f"  Slot 2:  {fmt_window(p.get('HOLD_FORCED_DISCHARGE_START_HOUR_2'), '00', p.get('HOLD_FORCED_DISCHARGE_END_HOUR_2'), '00')}")
    print(f"  SOC floor: {p.get('HOLD_FORCED_DISCHG_SOC_LIMIT')}%")
    print(f"  Power cmd: {p.get('HOLD_FORCED_DISCHG_POWER_CMD')}")


async def main():
    async with LuxpowerClient(
        username=USERNAME, password=PASSWORD, base_url=BASE_URL
    ) as client:
        stations = await Station.load_all(client)
        inverter = next(iter(stations[0].all_inverters))
        serial = inverter.serial_number
        print(f"Inverter: {inverter.model} ({serial})")
        print(f"Now: {datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S %Z')}")

        before = await fresh(client, serial)
        show_schedule("BEFORE:", before)

        print("\nWriting 8 schedule params...")
        for key, val in CHANGES:
            r = await client.api.control.write_parameter(serial, key, val)
            ok = getattr(r, "success", False)
            print(f"  {key:<48} -> {val:<3}  {'OK' if ok else 'FAIL: ' + str(r)}")

        print("\nWaiting 90 s for inverter to settle...")
        await asyncio.sleep(90)

        after = await fresh(client, serial)
        show_schedule("AFTER:", after)

        # Verify each change stuck and nothing else flipped
        print("\n=== VERIFY ===")
        all_good = True
        for key, val in CHANGES:
            cur = after.get(key)
            ok = str(cur) == str(int(val)) or str(cur).zfill(2) == str(int(val)).zfill(2)
            mark = "✓" if ok else "✗"
            if not ok:
                all_good = False
            print(f"  {key:<48} = {cur}   {mark}")

        # Make sure unrelated knobs didn't flip
        watch_unchanged = [
            ("FUNC_LSP_SELF_CONSUMPTION_EN", before.get("FUNC_LSP_SELF_CONSUMPTION_EN")),
            ("HOLD_FEED_IN_GRID_POWER_PERCENT", before.get("HOLD_FEED_IN_GRID_POWER_PERCENT")),
            ("HOLD_SYSTEM_CHARGE_SOC_LIMIT", before.get("HOLD_SYSTEM_CHARGE_SOC_LIMIT")),
            ("HOLD_FORCED_DISCHG_SOC_LIMIT", before.get("HOLD_FORCED_DISCHG_SOC_LIMIT")),
            ("HOLD_FORCED_DISCHG_POWER_CMD", before.get("HOLD_FORCED_DISCHG_POWER_CMD")),
        ]
        print("\nUnrelated knobs (should be unchanged):")
        for k, prev in watch_unchanged:
            cur = after.get(k)
            mark = "✓" if cur == prev else "⚠ changed"
            print(f"  {k:<46} {prev}  ->  {cur}   {mark}")

        if all_good:
            print("\n✓ Schedule updated. Forced discharge will run 16:00–21:00 daily.")
        else:
            print("\n✗ Some writes did not stick — re-run or investigate.")


if __name__ == "__main__":
    asyncio.run(main())
