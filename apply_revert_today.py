"""Revert today's exploratory inverter changes.

Restores:
  FUNC_FORCED_CHG_EN                 True  → False
  FUNC_LSP_BAT_FIRST_18_EN           True  → False
  FUNC_LSP_BAT_FIRST_20_EN ... 24_EN True  → False
  FUNC_LSP_BAT_FIRST_34_EN           False → True
  FUNC_LSP_BAT_FIRST_37_EN           False → True

(Slot 38 stays True — the disable attempt failed earlier and that
matches the original expected state anyway.)

Has built-in retry on REMOTE_SET_ERROR since the inverter rate-limits
consecutive FUNC writes.

Usage: uv run python apply_revert_today.py
"""

import asyncio

from pylxpweb import LuxpowerClient
from pylxpweb.devices.station import Station

from _env import USERNAME, PASSWORD, BASE_URL


# (key, target_bool)
CHANGES = [
    ("FUNC_FORCED_CHG_EN",       False),
    ("FUNC_LSP_BAT_FIRST_18_EN", False),
    ("FUNC_LSP_BAT_FIRST_20_EN", False),
    ("FUNC_LSP_BAT_FIRST_21_EN", False),
    ("FUNC_LSP_BAT_FIRST_22_EN", False),
    ("FUNC_LSP_BAT_FIRST_23_EN", False),
    ("FUNC_LSP_BAT_FIRST_24_EN", False),
    ("FUNC_LSP_BAT_FIRST_34_EN", True),
    ("FUNC_LSP_BAT_FIRST_37_EN", True),
]


async def main():
    async with LuxpowerClient(
        username=USERNAME, password=PASSWORD, base_url=BASE_URL
    ) as client:
        stations = await Station.load_all(client)
        inverter = next(iter(stations[0].all_inverters))
        serial = inverter.serial_number
        print(f"Inverter: {inverter.model} ({serial})\n")

        keys = [k for k, _ in CHANGES]
        client.invalidate_cache_for_device(serial)
        before = await client.api.control.read_device_parameters_ranges(serial)
        pending = [(k, t) for k, t in CHANGES if str(before.get(k)) != str(t)]
        print(f"{len(pending)} of {len(CHANGES)} need writing")
        for k, t in CHANGES:
            mark = "→" if str(before.get(k)) != str(t) else "·"
            print(f"  {mark} {k:<32} cur={before.get(k)}  target={t}")
        if not pending:
            print("\nNothing to do.")
            return

        print(f"\nWriting (3s gap, retry on REMOTE_SET_ERROR)...")
        for key, target in pending:
            for attempt in range(6):
                try:
                    result = await client.api.control.control_function(serial, key, bool(target))
                    ok = getattr(result, "success", None)
                    print(f"  {key:<32} -> {target}   "
                          f"{'OK' if ok else 'FAIL: ' + str(result)}")
                    break
                except Exception as e:
                    if "REMOTE_SET_ERROR" in str(e) and attempt < 5:
                        wait = 8 * (attempt + 1)
                        print(f"  {key:<32} -> retry in {wait}s ({e})")
                        await asyncio.sleep(wait)
                        continue
                    print(f"  {key:<32} -> FAILED: {e}")
                    break
            await asyncio.sleep(3)

        await asyncio.sleep(5)
        client.invalidate_cache_for_device(serial)
        after = await client.api.control.read_device_parameters_ranges(serial)

        print("\nAFTER:")
        all_ok = True
        for k, target in CHANGES:
            cur = after.get(k)
            ok = str(cur) == str(target)
            print(f"  {k:<32} = {cur}   {'✓' if ok else '✗ mismatch'}")
            if not ok:
                all_ok = False

        # Guard
        for k in ("FUNC_FEED_IN_GRID_EN", "FUNC_LSP_CHARGE_PRIORITY_EN", "FUNC_FORCED_DISCHG_EN"):
            print(f"  GUARD {k:<32} = {after.get(k)}")

        if all_ok:
            print("\nRevert complete.")
        else:
            print("\nSome changes still pending — re-run.")


if __name__ == "__main__":
    asyncio.run(main())
