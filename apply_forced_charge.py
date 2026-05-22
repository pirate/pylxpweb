"""Enable FUNC_FORCED_CHG_EN — the actual PV→battery priority mechanism.

I had the wrong mental model earlier: BAT_FIRST_N_EN slots control
TIME-OF-USE grid charging schedules, not PV-charging behavior. The
mechanism that actually says "charge the battery during this window"
(via PV, since FUNC_AC_CHARGE_EN is False) is the forced-charge
schedule.

The schedule is already configured as 00:00 – 23:59 (i.e. always), and
HOLD_FORCED_CHG_SOC_LIMIT = 88 stops the charging at 88% SOC. So all we
need to flip is the enable flag.

After this:
  - Whenever PV is producing more than the home load, the inverter
    routes the excess into the battery instead of dumping it to the
    grid (until SOC hits 88%).
  - At 88% the inverter falls back to default mode (PV → grid).
  - The forced-discharge window (16:00–21:00) still wins for peak
    export — forced-discharge has priority over forced-charge.

Usage: uv run python apply_forced_charge.py
"""

import asyncio

from pylxpweb import LuxpowerClient
from pylxpweb.devices.station import Station

from _env import USERNAME, PASSWORD, BASE_URL


async def main():
    async with LuxpowerClient(
        username=USERNAME, password=PASSWORD, base_url=BASE_URL
    ) as client:
        stations = await Station.load_all(client)
        inverter = next(iter(stations[0].all_inverters))
        serial = inverter.serial_number
        print(f"Inverter: {inverter.model} ({serial})\n")

        # Read context before the flip
        keys = [
            "FUNC_FORCED_CHG_EN",
            "HOLD_FORCED_CHARGE_START_HOUR",
            "HOLD_FORCED_CHARGE_END_HOUR",
            "HOLD_FORCED_CHG_SOC_LIMIT",
            "FUNC_FORCED_DISCHG_EN",
            "FUNC_FEED_IN_GRID_EN",
            "FUNC_LSP_CHARGE_PRIORITY_EN",
        ]
        client.invalidate_cache_for_device(serial)
        before = await client.api.control.read_device_parameters_ranges(serial)
        print("Context:")
        for k in keys:
            print(f"  {k:<36} = {before.get(k)}")

        print("\nWriting FUNC_FORCED_CHG_EN -> True ...")
        result = await client.api.control.control_function(
            serial, "FUNC_FORCED_CHG_EN", True
        )
        ok = getattr(result, "success", None)
        print(f"  result: {'OK' if ok else 'FAIL: ' + str(result)}")

        print("\nWaiting 5 s, then cache-bypass read...")
        await asyncio.sleep(5)
        client.invalidate_cache_for_device(serial)
        after = await client.api.control.read_device_parameters_ranges(serial)

        print("\nAFTER:")
        for k in keys:
            v_before = before.get(k)
            v_after = after.get(k)
            mark = "" if str(v_before) == str(v_after) else "  ← CHANGED"
            print(f"  {k:<36} = {v_after}{mark}")

        if str(after.get("FUNC_FORCED_CHG_EN")) != "True":
            print("\nFlag didn't stick. The firmware may have side-effected it off.")
            return

        # Side-effect guard: forced-discharge + export flags must still be ON
        regressions = []
        for k in ("FUNC_FORCED_DISCHG_EN", "FUNC_FEED_IN_GRID_EN",
                  "FUNC_LSP_CHARGE_PRIORITY_EN"):
            if str(after.get(k)) != "True":
                regressions.append(k)
        if regressions:
            print(f"\n⚠ REGRESSION: these flags got side-effected off: {regressions}")
            print("Run apply_restore_export.py to restore.")
        else:
            print("\nDone — forced-charge enabled, no side effects.")
            print("Watch /api/data for the next 30-60s — battery should start charging.")


if __name__ == "__main__":
    asyncio.run(main())
