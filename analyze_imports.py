"""Analyze grid imports to figure out why we're importing 0.5-1 kWh on most days.

Pulls the last 30 days of energy breakdowns from the EG4 API and, for any day
with a meaningful import, drills into hourly data for import / PV / load / SOC
to identify when and why grid power is being pulled.
"""

import asyncio
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from pylxpweb import LuxpowerClient
from pylxpweb.devices.station import Station

from _env import USERNAME, PASSWORD, BASE_URL
TZ = ZoneInfo("America/Los_Angeles")

# Threshold (kWh) above which a day is "interesting" enough to drill into
DAY_IMPORT_THRESHOLD = 0.1
HOUR_IMPORT_THRESHOLD = 0.05  # kWh in a single hour worth flagging


def kwh(raw: float) -> float:
    """API returns energy in 0.1 kWh units."""
    return raw / 10.0


def fmt_hour(period: str) -> str:
    """The dayColumn endpoint returns periods like '0', '1', ... '23' or '00:00'."""
    p = str(period).strip()
    if ":" in p:
        return p
    try:
        return f"{int(p):02d}:00"
    except ValueError:
        return p


async def fetch_month_series(client, serial: str, year: int, month: int, energy_type: str):
    """Returns dict {day: kwh} for the given month and energy type."""
    resp = await client.analytics.get_energy_month_breakdown(
        serial, year, month, energy_type
    )
    out: dict[int, float] = {}
    for p in resp.get("data", []):
        day = p.get("day")
        if day is None:
            continue
        out[int(day)] = kwh(p.get("energy", 0))
    return out


async def fetch_day_series(client, serial: str, date: str, energy_type: str):
    """Returns list of (hour_label, kwh) for the date."""
    resp = await client.analytics.get_energy_day_breakdown(serial, date, energy_type)
    out: list[tuple[str, float]] = []
    for p in resp.get("data", []):
        h = p.get("hour")
        if h is None:
            continue
        out.append((f"{int(h):02d}:00", kwh(p.get("energy", 0))))
    return out


async def main():
    async with LuxpowerClient(
        username=USERNAME, password=PASSWORD, base_url=BASE_URL
    ) as client:
        print("Connecting to EG4...")
        stations = await Station.load_all(client)
        station = stations[0]
        inverter = next(iter(station.all_inverters))
        serial = inverter.serial_number
        print(f"Station: {station.name}")
        print(f"Inverter: {inverter.model} ({serial})\n")

        today = datetime.now(TZ).date()
        # Pull current + 3 prior months so we have a solid 90+ days
        months: list[tuple[int, int]] = []
        cursor = today.replace(day=1)
        for _ in range(4):
            months.append((cursor.year, cursor.month))
            cursor = (cursor - timedelta(days=1)).replace(day=1)

        # Pull all 6 daily series in parallel for both months
        series_types = [
            ("eToUserDay", "Import"),
            ("eToGridDay", "Export"),
            ("eInvDay", "InvOut"),
            ("eAcChargeDay", "ACChrg"),
            ("eBatChargeDay", "BatChg"),
            ("eBatDischargeDay", "BatDch"),
        ]

        per_day = defaultdict(dict)  # date -> {label: kwh}
        for year, month in months:
            tasks = [
                fetch_month_series(client, serial, year, month, et)
                for et, _ in series_types
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for (et, label), result in zip(series_types, results):
                if isinstance(result, Exception):
                    print(f"  WARN: {label} {year}-{month}: {result}")
                    continue
                for day, value in result.items():
                    d = datetime(year, month, day).date()
                    per_day[d][label] = value

        # Sort by date desc, keep last 90 days
        all_days = sorted(per_day.keys(), reverse=True)
        all_days = [d for d in all_days if d <= today][:90]

        print("=" * 96)
        print(f"DAILY ENERGY (last {len(all_days)} days, kWh)")
        print("=" * 96)
        print(f"{'Date':<12} {'Import':>8} {'Export':>8} {'InvOut':>8} "
              f"{'ACChrg':>8} {'BatChg':>8} {'BatDch':>8}")
        print("-" * 96)
        total_import = 0.0
        days_with_import = 0
        suspicious_days: list = []
        for d in sorted(all_days):
            row = per_day[d]
            imp = row.get("Import", 0)
            exp = row.get("Export", 0)
            inv = row.get("InvOut", 0)
            acc = row.get("ACChrg", 0)
            bch = row.get("BatChg", 0)
            bdc = row.get("BatDch", 0)
            print(f"{d.isoformat():<12} {imp:>8.2f} {exp:>8.2f} {inv:>8.2f} "
                  f"{acc:>8.2f} {bch:>8.2f} {bdc:>8.2f}")
            total_import += imp
            if imp > DAY_IMPORT_THRESHOLD:
                days_with_import += 1
                suspicious_days.append((d, imp, acc))

        print("-" * 96)
        print(f"Days w/ import > {DAY_IMPORT_THRESHOLD} kWh: "
              f"{days_with_import}/{len(all_days)}")
        print(f"Total imported: {total_import:.2f} kWh "
              f"(avg {total_import/max(len(all_days),1):.2f} kWh/day)")

        # Drill into the worst 8 import days hour-by-hour
        suspicious_days.sort(key=lambda x: -x[1])
        drill_days = suspicious_days[:8]

        if not drill_days:
            print("\nNo days with meaningful import to drill into.")
            # Still try a fine-grained pToUser scan on yesterday to catch trickle
            yday = today - timedelta(days=1)
            print(f"\nFalling back to high-resolution pToUser scan for {yday}...")
            try:
                ptouser = await client.analytics.get_chart_data(
                    serial, "pToUser", yday.isoformat()
                )
                samples = ptouser.get("data", [])
                nonzero = [s for s in samples if s.get("value", 0) > 0]
                print(f"  Total samples: {len(samples)}, samples with import>0: "
                      f"{len(nonzero)}")
                if nonzero:
                    print(f"  Avg power-when-importing: "
                          f"{sum(s['value'] for s in nonzero)/len(nonzero):.1f} W")
                    print(f"  Max power: {max(s['value'] for s in nonzero):.0f} W")
                    print("  First 10 import events:")
                    for s in nonzero[:10]:
                        print(f"    {s.get('time')}: {s.get('value'):.0f} W")
            except Exception as e:
                print(f"  ERR: {e}")
            return

        print("\n" + "=" * 96)
        print(f"HOURLY DRILL-DOWN — top {len(drill_days)} import days")
        print("=" * 96)

        for d, imp, acc in drill_days:
            date_str = d.isoformat()
            print(f"\n--- {date_str}  (import {imp:.2f} kWh, AC charge {acc:.2f} kWh) ---")

            # Pull hourly: import, export, PV out, AC charge, bat charge, bat discharge
            hour_types = [
                ("eToUserDay", "Import"),
                ("eToGridDay", "Export"),
                ("eInvDay", "InvOut"),
                ("eAcChargeDay", "ACChrg"),
                ("eBatChargeDay", "BatChg"),
                ("eBatDischargeDay", "BatDch"),
            ]
            tasks = [
                fetch_day_series(client, serial, date_str, et)
                for et, _ in hour_types
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            hourly: dict[str, dict[str, float]] = defaultdict(dict)
            for (et, label), result in zip(hour_types, results):
                if isinstance(result, Exception):
                    print(f"   WARN: {label}: {result}")
                    continue
                for hour, value in result:
                    hourly[hour][label] = value

            # Pull SOC chart for the day
            soc_by_hour: dict[str, float] = {}
            try:
                soc_resp = await client.analytics.get_chart_data(
                    serial, "soc", date_str
                )
                # Group SOC samples by hour, take the avg
                hour_buckets: dict[str, list[float]] = defaultdict(list)
                for p in soc_resp.get("data", []):
                    hr = p.get("hour")
                    if hr is None:
                        continue
                    hour_buckets[f"{int(hr):02d}:00"].append(float(p.get("value", 0)))
                for k, vals in hour_buckets.items():
                    soc_by_hour[k] = sum(vals) / len(vals)
            except Exception as e:
                print(f"   WARN: SOC chart: {e}")

            print(f"   {'Hour':<6} {'Import':>7} {'Export':>7} {'InvOut':>7} "
                  f"{'ACChrg':>7} {'BatChg':>7} {'BatDch':>7} {'SOC%':>6}")
            for h in sorted(hourly.keys()):
                row = hourly[h]
                imp_h = row.get("Import", 0)
                exp_h = row.get("Export", 0)
                inv_h = row.get("InvOut", 0)
                acc_h = row.get("ACChrg", 0)
                bch_h = row.get("BatChg", 0)
                bdc_h = row.get("BatDch", 0)
                # find closest SOC
                soc_val = soc_by_hour.get(h, None)
                if soc_val is None:
                    # try matching just the HH
                    hh = h.split(":")[0]
                    for k, v in soc_by_hour.items():
                        if k.startswith(hh + ":"):
                            soc_val = v
                            break
                soc_str = f"{soc_val:.0f}" if soc_val is not None else "  - "
                marker = "  <<<" if imp_h > HOUR_IMPORT_THRESHOLD else ""
                print(f"   {h:<6} {imp_h:>7.2f} {exp_h:>7.2f} {inv_h:>7.2f} "
                      f"{acc_h:>7.2f} {bch_h:>7.2f} {bdc_h:>7.2f} {soc_str:>6}"
                      f"{marker}")

        # Heuristic interpretation
        print("\n" + "=" * 96)
        print("INTERPRETATION HINTS")
        print("=" * 96)
        print("""
  • If Import lines up with ACChrg in the same hour: inverter is AC-charging
    the battery from the grid (look at FUNC_AC_CHARGE_EN / charge schedule).
  • If Import > 0 at night with low PV and battery SOC dropping past min:
    battery hit discharge cutoff and load fell back to grid.
  • If Import > 0 mid-day with high SOC and PV available:
    likely brief load-spike (>inverter capacity), or LSP/transfer dead-band.
  • Constant ~30-100 W trickle 24h ≈ 0.7–2.4 kWh/day → grid CT offset / standby
    consumption that the inverter doesn't cover (e.g. transfer relay loss,
    dedicated grid loads outside the EPS panel).
""")


if __name__ == "__main__":
    asyncio.run(main())
