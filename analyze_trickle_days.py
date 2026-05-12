"""Drill into the *small* import days (0.1–1.0 kWh) over the last 90 days.

For each qualifying day, pull per-4-min pToUser / ppv / soc samples and
characterize each contiguous import event: start, duration, peak W, energy.
Then look for cross-day patterns (recurring time-of-day, common wattage).
"""

import asyncio
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from pylxpweb import LuxpowerClient
from pylxpweb.devices.station import Station

from _env import USERNAME, PASSWORD, BASE_URL
TZ = ZoneInfo("America/Los_Angeles")

MIN_DAY_KWH = 0.01
MAX_DAY_KWH = 3.0


def kwh(raw):
    return raw / 10.0


async def fetch_month_import(client, serial, year, month):
    resp = await client.analytics.get_energy_month_breakdown(
        serial, year, month, "eToUserDay"
    )
    return {int(p["day"]): kwh(p.get("energy", 0)) for p in resp.get("data", []) if "day" in p}


async def fetch_chart(client, serial, attr, date_str):
    resp = await client.analytics.get_chart_data(serial, attr, date_str)
    return resp.get("data", [])


def parse_time(t):
    # "2026-05-06 01:54:23" → datetime
    return datetime.strptime(t, "%Y-%m-%d %H:%M:%S")


def find_events(samples, min_w=10):
    """Group consecutive nonzero pToUser samples into events.

    Returns list of dicts with start/end/duration/peak/avg_w/energy_kwh.
    """
    events = []
    cur = None
    for s in samples:
        v = s.get("value", 0) or 0
        t = s.get("time")
        if not t:
            continue
        if v >= min_w:
            if cur is None:
                cur = {"start": t, "end": t, "vals": [v]}
            else:
                cur["end"] = t
                cur["vals"].append(v)
        else:
            if cur is not None:
                events.append(cur)
                cur = None
    if cur is not None:
        events.append(cur)

    # Compute summary stats per event
    out = []
    for e in events:
        try:
            start = parse_time(e["start"])
            end = parse_time(e["end"])
        except ValueError:
            continue
        dur_s = (end - start).total_seconds() + 240  # +1 sample window (~4 min)
        avg_w = sum(e["vals"]) / len(e["vals"])
        peak_w = max(e["vals"])
        # Crude energy estimate: each sample represents ~240 s
        energy_kwh = sum(e["vals"]) * 240 / 3600 / 1000
        out.append({
            "start": e["start"],
            "end": e["end"],
            "dur_min": dur_s / 60,
            "samples": len(e["vals"]),
            "avg_w": avg_w,
            "peak_w": peak_w,
            "kwh": energy_kwh,
        })
    return out


async def main():
    async with LuxpowerClient(
        username=USERNAME, password=PASSWORD, base_url=BASE_URL
    ) as client:
        stations = await Station.load_all(client)
        inverter = next(iter(stations[0].all_inverters))
        serial = inverter.serial_number
        today = datetime.now(TZ).date()

        # Pull 4 months of monthly import to find trickle days
        cursor = today.replace(day=1)
        months = []
        for _ in range(4):
            months.append((cursor.year, cursor.month))
            cursor = (cursor - timedelta(days=1)).replace(day=1)

        per_day_import = {}
        for year, month in months:
            data = await fetch_month_import(client, serial, year, month)
            for day, val in data.items():
                d = datetime(year, month, day).date()
                if d <= today:
                    per_day_import[d] = val

        # Find trickle days in 0.05–1.0 kWh range
        trickle_days = sorted(
            [d for d, v in per_day_import.items() if MIN_DAY_KWH <= v <= MAX_DAY_KWH],
            reverse=True,
        )
        print(f"Trickle days (0.05–1.0 kWh) in last 4 months: {len(trickle_days)}")
        for d in trickle_days:
            print(f"  {d}  {per_day_import[d]:.2f} kWh")

        if not trickle_days:
            print("No trickle days found.")
            return

        # Drill into each one
        all_events = []
        events_by_hour = defaultdict(int)
        peak_w_buckets = defaultdict(int)

        for d in trickle_days:
            ds = d.isoformat()
            try:
                ptouser, ppv, soc, pcharge, pdischarge, ptogrid = await asyncio.gather(
                    fetch_chart(client, serial, "pToUser", ds),
                    fetch_chart(client, serial, "ppv", ds),
                    fetch_chart(client, serial, "soc", ds),
                    fetch_chart(client, serial, "pCharge", ds),
                    fetch_chart(client, serial, "pDisCharge", ds),
                    fetch_chart(client, serial, "pToGrid", ds),
                )
            except Exception as e:
                print(f"  ERR {ds}: {e}")
                continue

            events = find_events(ptouser, min_w=10)
            print(f"\n--- {ds}  daily total {per_day_import[d]:.2f} kWh, "
                  f"{len(events)} import event(s) ---")

            # Build sample lookups by time
            soc_lookup = {s["time"]: s["value"] for s in soc if "time" in s}
            ppv_lookup = {s["time"]: s["value"] for s in ppv if "time" in s}
            pchg_lookup = {s["time"]: s["value"] for s in pcharge if "time" in s}
            pdis_lookup = {s["time"]: s["value"] for s in pdischarge if "time" in s}
            ptg_lookup = {s["time"]: s["value"] for s in ptogrid if "time" in s}

            def closest(lookup, target_time, max_secs=600):
                if target_time in lookup:
                    return lookup[target_time]
                try:
                    tt = parse_time(target_time)
                except ValueError:
                    return None
                best = None
                best_dt = None
                for k, v in lookup.items():
                    try:
                        kt = parse_time(k)
                    except ValueError:
                        continue
                    diff = abs((kt - tt).total_seconds())
                    if diff <= max_secs and (best_dt is None or diff < best_dt):
                        best = v
                        best_dt = diff
                return best

            for ev in events:
                soc_at = closest(soc_lookup, ev["start"])
                ppv_at = closest(ppv_lookup, ev["start"])
                pchg_at = closest(pchg_lookup, ev["start"])
                pdis_at = closest(pdis_lookup, ev["start"])
                ptg_at = closest(ptg_lookup, ev["start"])
                hh = ev["start"].split(" ")[1][:2]
                events_by_hour[hh] += 1
                pw = ev["peak_w"]
                if pw < 100:
                    pb = "<100W"
                elif pw < 300:
                    pb = "100-300W"
                elif pw < 700:
                    pb = "300-700W"
                elif pw < 1500:
                    pb = "700-1500W"
                elif pw < 3000:
                    pb = "1500-3000W"
                else:
                    pb = ">3000W"
                peak_w_buckets[pb] += 1

                soc_str = f"{soc_at:.0f}%" if soc_at is not None else "?"
                ppv_str = f"{ppv_at:.0f}" if ppv_at is not None else "?"
                pchg_str = f"{pchg_at:.0f}" if pchg_at is not None else "?"
                pdis_str = f"{pdis_at:.0f}" if pdis_at is not None else "?"
                ptg_str = f"{ptg_at:.0f}" if ptg_at is not None else "?"
                print(f"  {ev['start']} → {ev['end']}  "
                      f"dur={ev['dur_min']:>5.1f}m  "
                      f"peak={ev['peak_w']:>5.0f}W  ~{ev['kwh']:.3f}kWh  "
                      f"SOC={soc_str:<4} "
                      f"PV={ppv_str:>5}W BatChg={pchg_str:>5}W "
                      f"BatDis={pdis_str:>5}W ToGrid={ptg_str:>5}W")
                all_events.append({
                    **ev, "date": ds, "soc": soc_at, "ppv": ppv_at,
                    "pchg": pchg_at, "pdis": pdis_at, "ptg": ptg_at,
                })

        print("\n" + "=" * 80)
        print(f"AGGREGATE: {len(all_events)} import events across "
              f"{len(trickle_days)} trickle days")
        print("=" * 80)

        print("\nEvents by start hour (local):")
        for hh in sorted(events_by_hour.keys()):
            bar = "#" * events_by_hour[hh]
            print(f"  {hh}:00  {events_by_hour[hh]:>3} {bar}")

        print("\nPeak power distribution:")
        order = ["<100W", "100-300W", "300-700W", "700-1500W", "1500-3000W", ">3000W"]
        for pb in order:
            c = peak_w_buckets.get(pb, 0)
            if c:
                print(f"  {pb:<12} {c:>3} {'#' * c}")

        # Duration distribution
        print("\nDuration distribution:")
        dur_buckets = defaultdict(int)
        for ev in all_events:
            d = ev["dur_min"]
            if d <= 5:
                k = "≤5min"
            elif d <= 15:
                k = "5-15min"
            elif d <= 60:
                k = "15-60min"
            elif d <= 180:
                k = "1-3hr"
            else:
                k = ">3hr"
            dur_buckets[k] += 1
        for k in ["≤5min", "5-15min", "15-60min", "1-3hr", ">3hr"]:
            c = dur_buckets.get(k, 0)
            if c:
                print(f"  {k:<10} {c:>3} {'#' * c}")

        # SOC at start of import event
        print("\nSOC at import-event start:")
        soc_buckets = defaultdict(int)
        for ev in all_events:
            s = ev.get("soc")
            if s is None:
                soc_buckets["?"] += 1
                continue
            if s < 20:
                k = "<20%"
            elif s < 40:
                k = "20-40%"
            elif s < 60:
                k = "40-60%"
            elif s < 80:
                k = "60-80%"
            elif s < 95:
                k = "80-95%"
            else:
                k = "≥95%"
            soc_buckets[k] += 1
        for k in ["<20%", "20-40%", "40-60%", "60-80%", "80-95%", "≥95%", "?"]:
            c = soc_buckets.get(k, 0)
            if c:
                print(f"  {k:<8} {c:>3} {'#' * c}")


if __name__ == "__main__":
    asyncio.run(main())
