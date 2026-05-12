"""Verify/preserve the two charge-voltage cutoffs at 55 V (~95% SOC, 3.44 V/cell).

Originally lowered from 56 V → 55 V to give 1.8 V margin to BMS overvoltage
trip (~57 V). Targets now match the current known-good state — re-running this
script is a verification (writes the same value back, idempotent on the inverter).

  HOLD_AC_CHARGE_END_BATTERY_VOLTAGE   target: 55 V
  HOLD_LEAD_ACID_CHARGE_VOLT_REF       target: 55 V
"""

import asyncio
from pylxpweb import LuxpowerClient
from pylxpweb.devices.station import Station

from _env import USERNAME, PASSWORD, BASE_URL

CHANGES = [
    ("HOLD_AC_CHARGE_END_BATTERY_VOLTAGE", "55"),
    ("HOLD_LEAD_ACID_CHARGE_VOLT_REF", "55"),
]


async def read_keys(client, serial, keys, *, fresh=False):
    if fresh:
        client.invalidate_cache_for_device(serial)
    params = await client.api.control.read_device_parameters_ranges(serial)
    return {k: params.get(k) for k in keys}


async def main():
    async with LuxpowerClient(
        username=USERNAME, password=PASSWORD, base_url=BASE_URL
    ) as client:
        stations = await Station.load_all(client)
        inverter = next(iter(stations[0].all_inverters))
        serial = inverter.serial_number
        print(f"Inverter: {inverter.model} ({serial})\n")

        keys = [k for k, _ in CHANGES]
        before = await read_keys(client, serial, keys, fresh=True)
        print("BEFORE:")
        for k, _ in CHANGES:
            print(f"  {k:<40} = {before[k]}")

        print("\nWriting...")
        for key, target in CHANGES:
            result = await client.api.control.write_parameter(serial, key, target)
            ok = getattr(result, "success", None)
            print(f"  {key:<40} -> {target}   "
                  f"{'OK' if ok else 'FAIL: ' + str(result)}")

        print("\nWaiting 5 s, then cache-bypass read...")
        await asyncio.sleep(5)
        after = await read_keys(client, serial, keys, fresh=True)

        print("\nAFTER:")
        all_ok = True
        for k, target in CHANGES:
            cur = after[k]
            ok = str(cur) == str(target)
            mark = "✓" if ok else "✗ mismatch"
            print(f"  {k:<40} = {cur}   {mark}")
            if not ok:
                all_ok = False

        if all_ok:
            print("\nDone — both cutoffs now at 55V (~95% SOC, ~1.8V margin to BMS OV trip).")
        else:
            print("\nOne or more writes did not stick — check inverter logs.")


if __name__ == "__main__":
    asyncio.run(main())
