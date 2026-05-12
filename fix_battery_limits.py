#!/usr/bin/env python3
"""Battery limit verification (READ-ONLY snapshot).

Originally a one-shot script that wrote Tesla 12S74P (44–48 V / 50 A) limits
back to the inverter. That config is **completely wrong** for the current
LiFePO4 + Pylon CAN BMS setup: those values would clobber the BMS-aligned
operating range and force the system into a 1/4-capacity, 1/3-current state.

This script is now a no-write verifier:
  - Reads current battery-related parameters from the live inverter
  - Compares them against the documented current-state values (LiFePO4 / BMS)
  - Reports any mismatches so you know if something has drifted
  - **Never writes anything**

If the verifier reports drift, investigate the cause and use a dedicated
one-shot apply_*.py script (with explicit before/after verification and
register-21 awareness) to correct it.
"""

import asyncio

# Expected current-state values for this system:
#
#   - Battery pack: 46 kWh total combined, 16S LiFePO4
#       * 32 kWh Docan Panda (BMS connected via Pylon CAN — what the
#         inverter reports SOC/limits for)
#       * 14 kWh EG4 battery (passive parallel on DC bus, internal BMS but
#         no comms to inverter). Both batteries share the same bus voltage
#         and discharge together; the Docan's SOC is a reasonable proxy for
#         the combined pack.
#   - Solar: 17.6 kW DC nameplate (7.7 + 7.7 + 2.2 kW across 3 MPPTs)
#   - Inverter: FlexBOSS21 (21 kW AC, 12 kW grid sellback cap)
#   - GridBOSS MID device (SN 4434850408) — 4 smart ports
#   - PG&E E-TOU-C + NBT (NEM 3.0), Ava Community Energy generation
#   - Peak window: 16:00–20:59 (PG&E peak + Ava bonus 15:00–20:00)
#
# Combined-pack SOC math reference (use this for overnight margin checks):
#   1% Docan-reported SOC ≈ 460 Wh of combined-pack energy.
#   55% forced-discharge floor leaves (55–8)% × 46 kWh = ~21.6 kWh reserve.
EXPECTED = {
    # === Voltage limits (LiFePO4 16S, ~5%-95% SOC operating range) ===
    "HOLD_LEAD_ACID_DISCHARGE_CUT_OFF_VOLT":   "48",   # 3.00 V/cell
    "HOLD_SYSTEM_CHARGE_VOLT_LIMIT":           "55",   # 3.44 V/cell
    "HOLD_AC_CHARGE_END_BATTERY_VOLTAGE":      "55",   # 3.44 V/cell (matches system limit)
    "HOLD_FLOATING_VOLTAGE":                   "54",   # 3.375 V/cell
    "HOLD_LEAD_ACID_CHARGE_VOLT_REF":          "55",   # 3.44 V/cell

    # === Current limits (BMS reports 160 A chg / 180 A dis; inverter set
    # slightly above so BMS does the throttling) ===
    "HOLD_LEAD_ACID_CHARGE_RATE":              "175",
    "HOLD_LEAD_ACID_DISCHARGE_RATE":           "190",

    # === SOC limits ===
    "HOLD_SYSTEM_CHARGE_SOC_LIMIT":            "95",   # charge ceiling (~95%)
    "HOLD_DISCHG_CUT_OFF_SOC_EOD":             "8",    # absolute discharge floor
    "HOLD_FORCED_DISCHG_SOC_LIMIT":            "55",   # leaves ≥15 kWh reserve to avoid AC-charge trigger
    "HOLD_FORCED_CHG_SOC_LIMIT":               "88",

    # === AC charge guard rails ===
    # The inverter has an auto-AC-charge-from-grid behavior that fires when
    # battery hits HOLD_AC_CHARGE_START_BATTERY_SOC during a BAT_FIRST time
    # slot. Lowered from 10 to 5 to avoid triggering in normal operation.
    "HOLD_AC_CHARGE_START_BATTERY_SOC":        "5",
    "HOLD_AC_CHARGE_START_BATTERY_VOLTAGE":    "40",   # voltage trigger (below this AC charges)
    "HOLD_AC_CHARGE_SOC_LIMIT":                "100",  # max SOC AC charging will fill to

    # === Mode flags ===
    "FUNC_LSP_SELF_CONSUMPTION_EN":            True,   # load priority
    "FUNC_LSP_CHARGE_PRIORITY_EN":             True,   # NB: keeps flipping back to True; live with it
    "FUNC_LSP_BATT_VOLT_OR_SOC":               False,  # SOC-based control (BMS reports SOC)
    "FUNC_GRID_CT_CONNECTION_EN":              True,
    "FUNC_GRID_PEAK_SHAVING":                  False,  # disabled — was shadowing forced discharge
    "FUNC_FORCED_DISCHG_EN":                   True,   # TOU peak-export schedule
    "FUNC_FORCED_CHG_EN":                      False,

    # === Export configuration ===
    # FUNC_PV_SELL_TO_GRID_EN is the EG4 "Export PV Only" toggle — its
    # name is misleading. True = battery is BLOCKED from exporting; False =
    # battery + PV can both export. Must be FALSE for forced discharge to work.
    "FUNC_PV_SELL_TO_GRID_EN":                 False,  # "Export PV Only" — must stay FALSE
    "FUNC_FEED_IN_GRID_EN":                    True,   # "Sell-back to grid" — must stay TRUE
    "HOLD_FEED_IN_GRID_POWER_PERCENT":         "12",   # export cap kW (max 12 = inverter limit)
    "HOLD_FORCED_DISCHG_POWER_CMD":            "6",    # forced-discharge target kW

    # === Forced-discharge schedule (16:00–20:59 PG&E peak window) ===
    "HOLD_FORCED_DISCHARGE_START_HOUR":        "16",
    "HOLD_FORCED_DISCHARGE_START_MINUTE":      "00",
    "HOLD_FORCED_DISCHARGE_END_HOUR":          "20",
    "HOLD_FORCED_DISCHARGE_END_MINUTE":        "59",
    # Slot 1 should remain disabled (overnight = off-peak, lowest export value)
    "HOLD_FORCED_DISCHARGE_START_HOUR_1":      "00",
    "HOLD_FORCED_DISCHARGE_END_HOUR_1":        "00",
    "HOLD_FORCED_DISCHARGE_END_MINUTE_1":      "00",

    # === FUNC_LSP_BAT_FIRST_N_EN slots (48 slots, current as observed) ===
    # These 9 slots are the "battery-first" time periods. Slots 3-6 (01:00-03:00)
    # were responsible for grid-charging the battery overnight; slots 34/37/38
    # appear to be afternoon/evening battery-preserve overrides that conflict
    # with forced-discharge during the peak window. Tracking the full set so
    # any drift is visible.
    **{f"FUNC_LSP_BAT_FIRST_{n}_EN": (n in {3, 4, 5, 6, 17, 19, 34, 37, 38})
       for n in range(1, 49)},
}


async def verify_battery_limits():
    from pylxpweb import LuxpowerClient

    from _env import USERNAME as username, PASSWORD as password, BASE_URL as base_url, INVERTER_SN as inverter_sn

    print("=" * 72)
    print("Battery limit verifier (read-only)")
    print("  Pack: 46 kWh combined (32 kWh Docan Pylon-BMS + 14 kWh EG4 paralleled)")
    print("  Solar: 17.6 kW DC across 3 MPPTs")
    print("  Inverter: FlexBOSS21 + GridBOSS")
    print("=" * 72)
    print(f"Inverter: {inverter_sn}")
    print(f"Expected operating range: ~5%–95% SOC, ~48–55 V pack")
    print("=" * 72)

    async with LuxpowerClient(username, password, base_url=base_url) as client:
        # Force-fresh read so we don't see stale cached values
        client.invalidate_cache_for_device(inverter_sn)
        params = await client.api.control.read_device_parameters_ranges(inverter_sn)

        print(f"\n{'Parameter':<46} {'Live':>8}   {'Expected':>10}   Status")
        print("-" * 80)

        match_count = 0
        drift_count = 0
        missing_count = 0

        def _norm(v):
            """Normalize to a comparable string. Bools as 'True'/'False'; everything else as str()."""
            if isinstance(v, bool):
                return "True" if v else "False"
            return str(v)

        for key, expected_val in EXPECTED.items():
            live_val = params.get(key)
            if live_val is None:
                status = "⚠ missing from device"
                missing_count += 1
                live_str = "—"
                expected_str = _norm(expected_val)
            else:
                live_str = _norm(live_val)
                expected_str = _norm(expected_val)
                if live_str == expected_str:
                    status = "✓ match"
                    match_count += 1
                else:
                    status = "✗ DRIFT"
                    drift_count += 1
            print(f"  {key:<46} {live_str:>8}   {expected_str:>10}   {status}")

        print("\n" + "=" * 72)
        print(f"Summary: {match_count} match, {drift_count} drift, "
              f"{missing_count} missing")
        print("=" * 72)
        if drift_count:
            print("\nDrift detected. Investigate before applying any fix — the")
            print("expected values above describe the desired state, but the live")
            print("inverter may have changed for a reason. Use a one-shot apply_*.py")
            print("script if you confirm the live values are wrong.")
        else:
            print("\nAll battery limits match expected current-state values.")


if __name__ == "__main__":
    asyncio.run(verify_battery_limits())
