"""Open-Meteo weather fetch for the Daily Flash.

No API key needed. Free tier is perfectly adequate for 3-day forecasts.
Coords default to Daios Cove (~35.24°N, 25.75°E — Agios Nikolaos, Crete).

Maps WMO weather codes to a compact condition + icon slug the frontend
renders as a hand-drawn SVG.
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from datetime import date, timedelta
from typing import Optional


# WMO 4677/4680 weather-code → (condition label, icon slug)
_WMO: dict[int, tuple[str, str]] = {
    0: ("Clear", "sun"),
    1: ("Mainly clear", "sun"),
    2: ("Partly cloudy", "cloud-sun"),
    3: ("Overcast", "cloud"),
    45: ("Fog", "fog"),
    48: ("Rime fog", "fog"),
    51: ("Light drizzle", "drizzle"),
    53: ("Drizzle", "drizzle"),
    55: ("Heavy drizzle", "drizzle"),
    56: ("Freezing drizzle", "drizzle"),
    57: ("Freezing drizzle", "drizzle"),
    61: ("Light rain", "rain"),
    63: ("Rain", "rain"),
    65: ("Heavy rain", "rain"),
    66: ("Freezing rain", "rain"),
    67: ("Freezing rain", "rain"),
    71: ("Light snow", "snow"),
    73: ("Snow", "snow"),
    75: ("Heavy snow", "snow"),
    77: ("Snow grains", "snow"),
    80: ("Rain showers", "rain"),
    81: ("Heavy showers", "rain"),
    82: ("Violent showers", "rain"),
    85: ("Snow showers", "snow"),
    86: ("Heavy snow showers", "snow"),
    95: ("Thunderstorm", "storm"),
    96: ("Thunderstorm with hail", "storm"),
    99: ("Severe thunderstorm", "storm"),
}


_DEFAULT_LAT = float(os.environ.get("WEATHER_LAT", "35.2401"))
_DEFAULT_LON = float(os.environ.get("WEATHER_LON", "25.7500"))
_DEFAULT_TZ = os.environ.get("WEATHER_TZ", "Europe/Athens")


def _label_for_offset(i: int) -> str:
    return ("TODAY", "TOMORROW", "FOLLOWING")[i] if i < 3 else f"+{i}"


def fetch_weather(
    report_date: date,
    *,
    lat: float = _DEFAULT_LAT,
    lon: float = _DEFAULT_LON,
    tz: str = _DEFAULT_TZ,
    days: int = 3,
) -> Optional[list[dict]]:
    """Return a 3-day forecast aligned with the occupancy trio.

    [
      {label: 'TODAY',     date: '2026-04-20', day_name: 'Mon',
       high: 22, low: 13, condition: 'Partly cloudy',
       icon: 'cloud-sun', code: 2},
      ...
    ]

    Returns None if the Open-Meteo call fails — pipeline should continue.
    """
    params = urllib.parse.urlencode({
        "latitude": lat,
        "longitude": lon,
        "timezone": tz,
        "start_date": report_date.isoformat(),
        "end_date": (report_date + timedelta(days=days - 1)).isoformat(),
        "daily": "weather_code,temperature_2m_max,temperature_2m_min",
    })
    url = f"https://api.open-meteo.com/v1/forecast?{params}"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            body = json.loads(r.read())
    except Exception as e:
        print(f"  weather fetch failed: {e}")
        return None

    daily = body.get("daily") or {}
    times = daily.get("time") or []
    codes = daily.get("weather_code") or []
    highs = daily.get("temperature_2m_max") or []
    lows = daily.get("temperature_2m_min") or []
    if not times:
        return None

    out = []
    for i, iso_date in enumerate(times):
        try:
            d = date.fromisoformat(iso_date)
        except ValueError:
            continue
        code = int(codes[i]) if i < len(codes) else 0
        condition, icon = _WMO.get(code, ("—", "cloud"))
        out.append({
            "label": _label_for_offset(i),
            "date": iso_date,
            "day_name": d.strftime("%a"),   # 'Mon', 'Tue', …
            "high": round(float(highs[i])) if i < len(highs) else None,
            "low": round(float(lows[i])) if i < len(lows) else None,
            "condition": condition,
            "icon": icon,
            "code": code,
        })
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    args = ap.parse_args()
    d = date.fromisoformat(args.date) if args.date else date.today()
    print(json.dumps(fetch_weather(d), indent=2))
