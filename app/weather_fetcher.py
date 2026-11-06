from __future__ import annotations

import html
import json
import logging
import re
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from app import config
from app.settings_store import settings_store

logger = logging.getLogger("retro-weather.fetcher")

TAG_RE = re.compile(r"<[^>]+>")
WHITESPACE_RE = re.compile(r"\s+")

WMO_CODES: Dict[int, str] = {
    0: "CLEAR SKIES",
    1: "MAINLY CLEAR",
    2: "PARTLY CLOUDY",
    3: "OVERCAST",
    45: "FOG PATCHES",
    48: "FREEZING FOG",
    51: "LIGHT DRIZZLE",
    53: "MODERATE DRIZZLE",
    55: "DENSE DRIZZLE",
    56: "FREEZING DRIZZLE",
    57: "HEAVY FRZ DRIZZLE",
    61: "LIGHT RAIN",
    63: "MODERATE RAIN",
    65: "HEAVY RAIN",
    66: "FREEZING RAIN",
    67: "HEAVY FRZ RAIN",
    71: "LIGHT SNOW",
    73: "MODERATE SNOW",
    75: "HEAVY SNOW",
    77: "SNOW GRAINS",
    80: "RAIN SHOWERS",
    81: "HEAVY SHOWERS",
    82: "VIOLENT SHOWERS",
    85: "SNOW FLURRIES",
    86: "HEAVY FLURRIES",
    95: "THUNDERSTORMS",
    96: "TSTMS WITH HAIL",
    99: "SEVERE TSTMS",
}


def wmo_to_text(code: Optional[int]) -> str:
    if code is None:
        return "FAIR"
    return WMO_CODES.get(int(code), "CLOUDY")


def deg_to_compass(deg: Optional[float]) -> str:
    if deg is None:
        return "CALM"
    dirs = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    idx = int(round(float(deg) / 22.5)) % 16
    return dirs[idx]


def clean_text(raw: Optional[str], max_chars: int = 340) -> str:
    if not raw:
        return ""
    text = html.unescape(raw)
    text = TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = WHITESPACE_RE.sub(" ", text).strip()
    if len(text) > max_chars:
        cut = text[: max_chars - 3].rsplit(" ", 1)[0]
        text = f"{cut}..."
    return text.upper()


def geocode_city(query: str) -> Dict[str, Any]:
    """
    Geocode a city query (e.g. 'Halifax, NS' or 'St. John's, NL') using Open-Meteo's
    free geocoding endpoint, with fallback to Nominatim.
    """
    clean_q = query.strip()
    if not clean_q:
        raise ValueError("City query cannot be empty.")

    # Extract primary city name before comma for Open-Meteo search
    parts = [p.strip() for p in clean_q.split(",") if p.strip()]
    search_name = parts[0]

    url = (
        "https://geocoding-api.open-meteo.com/v1/search?"
        + urllib.parse.urlencode({"name": search_name, "count": 8, "language": "en", "format": "json"})
    )
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "RetroWeatherHLS/1.0 (+https://localhost)"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        results = data.get("results") or []
        if results:
            # If state/province/country hint was provided after comma, try to match it
            chosen = results[0]
            if len(parts) > 1:
                hint = " ".join(parts[1:]).lower()
                for cand in results:
                    admin1 = str(cand.get("admin1", "")).lower()
                    country = str(cand.get("country", "")).lower()
                    cc = str(cand.get("country_code", "")).lower()
                    if any(tok in admin1 or tok in country or tok == cc for tok in hint.split()):
                        chosen = cand
                        break
            return {
                "name": str(chosen.get("name", search_name)).upper()[:18],
                "query": f"{chosen.get('name')}, {chosen.get('admin1', '')}, {chosen.get('country_code', '')}".strip(", "),
                "latitude": float(chosen["latitude"]),
                "longitude": float(chosen["longitude"]),
            }
    except Exception as exc:
        logger.warning("Open-Meteo geocoding error for %s: %s", clean_q, exc)

    raise ValueError(f"Could not geocode location '{clean_q}'. Try 'City, Country' format.")


@dataclass
class WeatherPage:
    page_type: str                 # "DISPATCH", "EXTENDED", "RSS_ALERT"
    location_name: str             # e.g. "HALIFAX"
    subheader: str                 # Strip 2 text: e.g. "ENVIRONMENT CANADA WEATHER WIRE"
    green_row_1: str               # Upper green band row 1
    green_row_2: str               # Upper green band row 2
    blue_lines: List[str] = field(default_factory=list)  # Up to 8 lines for the royal-blue middle band
    raw_paragraph: str = ""        # Optional paragraph to auto-wrap into 8 lines


class WeatherFeedManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pages: List[WeatherPage] = self._build_standby_pages()
        self._city_summaries: List[Dict[str, Any]] = []
        self._rss_stats: List[Dict[str, Any]] = []
        self._last_updated: Optional[datetime] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def _build_standby_pages(self) -> List[WeatherPage]:
        return [
            WeatherPage(
                page_type="DISPATCH",
                location_name="HALIFAX",
                subheader=config.SUBHEADER_TEXT,
                green_row_1="HFX    TEMP     FEELS    WIND       HUMID    PRESS",
                green_row_2="OBS    SYNC     SYNC     CALM       ---%     101.3K",
                raw_paragraph=(
                    "HALIFAX: REGIONAL METEOROLOGICAL TELETYPE TERMINAL "
                    "INITIALIZING SATELLITE WEATHER OBSERVATIONS AND 5-DAY "
                    "EXTENDED FORECASTS. LIVE CONDITIONS WILL APPEAR MOMENTARILY."
                ),
            )
        ]

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    def _loop(self) -> None:
        self.refresh_now()
        interval_sec = max(60, config.REFRESH_INTERVAL_MINUTES * 60)
        while self._running:
            time.sleep(interval_sec)
            if not self._running:
                break
            self.refresh_now()

    def _fetch_location_weather(
        self, loc: Dict[str, Any], units: str, subheader_default: str
    ) -> Tuple[List[WeatherPage], Optional[Dict[str, Any]]]:
        lat = loc.get("latitude", 44.6488)
        lon = loc.get("longitude", -63.5752)
        name = str(loc.get("name", "LOCAL")).upper()
        code3 = re.sub(r"[^A-Z]", "", name)[:3].ljust(3, "X")

        is_imperial = units == "imperial"
        temp_unit = "fahrenheit" if is_imperial else "celsius"
        wind_unit = "mph" if is_imperial else "kmh"
        precip_unit = "inch" if is_imperial else "mm"
        t_sym = "F" if is_imperial else "C"
        w_sym = "MPH" if is_imperial else "KPH"

        params = {
            "latitude": lat,
            "longitude": lon,
            "current": (
                "temperature_2m,relative_humidity_2m,apparent_temperature,"
                "precipitation,weather_code,surface_pressure,wind_speed_10m,"
                "wind_direction_10m,wind_gusts_10m"
            ),
            "daily": (
                "weather_code,temperature_2m_max,temperature_2m_min,"
                "sunrise,sunset,uv_index_max,precipitation_probability_max,"
                "wind_speed_10m_max"
            ),
            "temperature_unit": temp_unit,
            "wind_speed_unit": wind_unit,
            "precipitation_unit": precip_unit,
            "timezone": "auto",
            "forecast_days": 6,
        }
        url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "RetroWeatherHLS/1.0 (+https://localhost)"},
        )

        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        curr = data.get("current", {})
        daily = data.get("daily", {})

        temp = curr.get("temperature_2m", 0.0)
        feels = curr.get("apparent_temperature", temp)
        hum = int(round(curr.get("relative_humidity_2m", 0)))
        wcode = curr.get("weather_code", 0)
        cond_str = wmo_to_text(wcode)
        wspeed = int(round(curr.get("wind_speed_10m", 0)))
        wgust = int(round(curr.get("wind_gusts_10m", wspeed)))
        wdir = deg_to_compass(curr.get("wind_direction_10m"))
        press_hpa = curr.get("surface_pressure", 1013.0)
        if is_imperial:
            press_str = f"{press_hpa * 0.02953:.2f}IN"
        else:
            press_str = f"{press_hpa / 10.0:.1f}KPA"

        # Build the iconic 2-row Upper Green Band telemetry string (aligned columns)
        # Matching the reference photo:
        # Row 1: HFX   TEMP    FEELS   WIND       HUMID   PRESS
        # Row 2: OBS   -1.8C   -6.4C   NW 24KPH   78%     101.4KPA
        g_row1 = f"{code3:<5} {'TEMP':<8} {'FEELS':<8} {'WIND':<11} {'HUMID':<7} {'PRESS'}"
        wind_short = f"{wdir} {wspeed}{w_sym}"
        g_row2 = (
            f"{'OBS':<5} "
            f"{f'{temp:.1f}{t_sym}':<8} "
            f"{f'{feels:.1f}{t_sym}':<8} "
            f"{wind_short:<11} "
            f"{f'{hum}%':<7} "
            f"{press_str}"
        )

        # Extract today & tomorrow from daily arrays
        d_times = daily.get("time", [])
        d_max = daily.get("temperature_2m_max", [])
        d_min = daily.get("temperature_2m_min", [])
        d_code = daily.get("weather_code", [])
        d_pop = daily.get("precipitation_probability_max", [])
        d_wind = daily.get("wind_speed_10m_max", [])

        hi_today = int(round(d_max[0])) if d_max else int(round(temp))
        lo_tonight = int(round(d_min[0])) if d_min else int(round(temp - 4))
        pop_today = int(round(d_pop[0] or 0)) if d_pop else 0

        tmrw_cond = wmo_to_text(d_code[1]) if len(d_code) > 1 else "PARTLY CLOUDY"
        hi_tmrw = int(round(d_max[1])) if len(d_max) > 1 else hi_today
        lo_tmrw = int(round(d_min[1])) if len(d_min) > 1 else lo_tonight
        wind_tmrw = int(round(d_wind[1] or 15)) if len(d_wind) > 1 else wspeed

        gust_clause = f" GUSTING TO {wgust}" if wgust > wspeed + 8 else ""
        paragraph = (
            f"{name}: REGIONAL WEATHER OBSERVATION AND FORECAST DISPATCH. "
            f"CURRENTLY {cond_str} AT {temp:.1f}{t_sym} (FEELS LIKE {feels:.1f}{t_sym}) "
            f"WITH {wdir} WINDS AT {wspeed} {w_sym}{gust_clause}. "
            f"HUMIDITY {hum} PCT, PRESSURE {press_str}. "
            f"TODAY/TONIGHT: HIGH {hi_today}{t_sym}, LOW {lo_tonight}{t_sym} "
            f"({pop_today} PCT PRECIP). "
            f"TOMORROW: {tmrw_cond}, HIGH {hi_tmrw}{t_sym}, LOW {lo_tmrw}{t_sym}, "
            f"WINDS UP TO {wind_tmrw} {w_sym}."
        )

        page_dispatch = WeatherPage(
            page_type="DISPATCH",
            location_name=name,
            subheader=subheader_default,
            green_row_1=g_row1,
            green_row_2=g_row2,
            raw_paragraph=paragraph,
        )

        # Build Page 2: 5-Day Extended Outlook (8 lines in the royal-blue band)
        ext_lines = [
            f"{name}: 5-DAY EXTENDED METEOROLOGICAL OUTLOOK",
            "DAY        CONDITION          HIGH    LOW    POP",
        ]
        for i in range(1, min(6, len(d_times))):
            try:
                dt_obj = datetime.strptime(d_times[i], "%Y-%m-%d")
                day_lbl = dt_obj.strftime("%a %b %d").upper()
            except Exception:
                day_lbl = d_times[i][:10].upper()
            c_txt = wmo_to_text(d_code[i] if i < len(d_code) else 0)[:17]
            hi_v = f"{int(round(d_max[i]))}{t_sym}" if i < len(d_max) else "--"
            lo_v = f"{int(round(d_min[i]))}{t_sym}" if i < len(d_min) else "--"
            pop_v = f"{int(round(d_pop[i] or 0))}%" if i < len(d_pop) else "0%"
            ext_lines.append(
                f"{day_lbl:<10} {c_txt:<18} {hi_v:>5}  {lo_v:>5}  {pop_v:>4}"
            )

        # Line 8: Sunrise / Sunset / UV
        sr_raw = (daily.get("sunrise") or [""])[0]
        ss_raw = (daily.get("sunset") or [""])[0]
        sr_str = sr_raw.split("T")[-1] if "T" in sr_raw else "06:45"
        ss_str = ss_raw.split("T")[-1] if "T" in ss_raw else "17:15"
        uv_val = (daily.get("uv_index_max") or [2])[0] or 0
        ext_lines.append(
            f"SUNRISE: {sr_str}   SUNSET: {ss_str}   MAX UV INDEX: {uv_val:.0f}"
        )

        page_extended = WeatherPage(
            page_type="EXTENDED",
            location_name=name,
            subheader=subheader_default,
            green_row_1=g_row1,
            green_row_2=g_row2,
            blue_lines=ext_lines[:8],
        )

        summary_info = {
            "id": loc.get("id"),
            "name": name,
            "code": code3,
            "temp": f"{temp:.1f}{t_sym}",
            "feels": f"{feels:.1f}{t_sym}",
            "condition": cond_str,
            "wind": wind_short,
            "humidity": f"{hum}%",
            "pressure": press_str,
        }
        return [page_dispatch, page_extended], summary_info

    def _fetch_rss_alerts(
        self, subheader_default: str, default_g1: str, default_g2: str
    ) -> Tuple[List[WeatherPage], List[Dict[str, Any]]]:
        pages: List[WeatherPage] = []
        stats: List[Dict[str, Any]] = []
        feeds = settings_store.get_rss_feeds()

        for fcfg in feeds:
            enabled = bool(fcfg.get("enabled", True))
            url = str(fcfg.get("url", "")).strip()
            name = str(fcfg.get("name", "ALERT WIRE")).strip().upper()
            if not enabled or not url:
                stats.append({"id": fcfg.get("id"), "name": name, "url": url, "enabled": enabled, "item_count": 0, "error": None})
                continue

            req = urllib.request.Request(
                url,
                headers={"User-Agent": "RetroWeatherHLS/1.0 (+https://localhost)"},
            )
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    raw_xml = resp.read()
                root = ET.fromstring(raw_xml)
                items = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")
                count = 0
                for elem in items[:6]:
                    t_el = elem.find("title")
                    if t_el is None:
                        t_el = elem.find("{http://www.w3.org/2005/Atom}title")
                    d_el = elem.find("description")
                    if d_el is None:
                        d_el = elem.find("{http://www.w3.org/2005/Atom}summary")
                    title = clean_text(t_el.text if t_el is not None else "", max_chars=140)
                    desc = clean_text(d_el.text if d_el is not None else "", max_chars=260)
                    if not title:
                        continue
                    body = f"{name}: {title}. {desc}" if desc and desc != title else f"{name}: {title}."
                    pages.append(
                        WeatherPage(
                            page_type="RSS_ALERT",
                            location_name=name[:18],
                            subheader=subheader_default,
                            green_row_1=default_g1,
                            green_row_2=default_g2,
                            raw_paragraph=body,
                        )
                    )
                    count += 1
                stats.append({"id": fcfg.get("id"), "name": name, "url": url, "enabled": True, "item_count": count, "error": None})
            except Exception as exc:
                logger.warning("RSS fetch error for %s: %s", url, exc)
                stats.append({"id": fcfg.get("id"), "name": name, "url": url, "enabled": True, "item_count": 0, "error": str(exc)})

        return pages, stats

    def refresh_now(self) -> dict:
        locations = settings_store.get_locations()
        units = settings_store.get_units()
        disp = settings_store.get_display()
        subheader = str(disp.get("subheader_text", config.SUBHEADER_TEXT)).upper()

        new_pages: List[WeatherPage] = []
        summaries: List[Dict[str, Any]] = []

        for loc in locations:
            if not loc.get("enabled", True):
                continue
            try:
                loc_pages, summary = self._fetch_location_weather(loc, units, subheader)
                new_pages.extend(loc_pages)
                if summary:
                    summaries.append(summary)
            except Exception as exc:
                logger.warning("Failed to fetch weather for %s: %s", loc.get("name"), exc)

        default_g1 = new_pages[0].green_row_1 if new_pages else "WX    TEMP     FEELS    WIND       HUMID    PRESS"
        default_g2 = new_pages[0].green_row_2 if new_pages else "OBS   ---      ---      CALM       ---%     ---"

        rss_pages, rss_stats = self._fetch_rss_alerts(subheader, default_g1, default_g2)
        new_pages.extend(rss_pages)

        with self._lock:
            if new_pages:
                self._pages = new_pages
            self._city_summaries = summaries
            self._rss_stats = rss_stats
            self._last_updated = datetime.now()

        logger.info("Weather refresh complete: %d cities, %d total broadcast pages", len(summaries), len(self._pages))
        return self.get_status()

    def get_pages(self) -> List[WeatherPage]:
        with self._lock:
            return list(self._pages)

    def get_status(self) -> dict:
        with self._lock:
            return {
                "total_pages": len(self._pages),
                "cities": list(self._city_summaries),
                "rss_feeds": list(self._rss_stats),
                "units": settings_store.get_units(),
                "last_updated": self._last_updated.isoformat() if self._last_updated else None,
            }


weather_manager = WeatherFeedManager()
