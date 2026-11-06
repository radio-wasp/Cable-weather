import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict
from xml.sax.saxutils import escape

from fastapi import Body, Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from app import config
from app.renderer import renderer
from app.settings_store import settings_store
from app.streamer import stream_manager
from app.weather_fetcher import geocode_city, weather_manager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("retro-weather.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting Vintage 1980s CRT Weather System (HLS)...")
    weather_manager.start()
    stream_manager.start()
    yield
    logger.info("Shutting down Vintage 1980s CRT Weather System...")
    stream_manager.stop()
    weather_manager.stop()


app = FastAPI(
    title="Vintage 1980s CRT Weather System (HLS)",
    description="1980s Multi-Band CRT Character Generator Weather Channel with configurable Cities, Alert Feeds, Radio Presets & Resolution",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}


def _get_base_url(request: Request) -> str:
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.headers.get("host", f"localhost:{config.PORT}"))
    return f"{scheme}://{host}"


def require_admin(request: Request) -> str:
    token = request.cookies.get(config.SESSION_COOKIE_NAME)
    username = settings_store.verify_session_token(token)
    if not username:
        raise HTTPException(status_code=401, detail="Authentication required.")
    return username


@app.get("/", response_class=HTMLResponse)
async def index_page(request: Request):
    base_url = _get_base_url(request)
    disp = settings_store.get_display()
    token = request.cookies.get(config.SESSION_COOKIE_NAME)
    authenticated_user = settings_store.verify_session_token(token)
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "channel_number": disp.get("channel_number", config.CHANNEL_NUMBER),
            "channel_name": disp.get("channel_name", config.CHANNEL_NAME),
            "base_url": base_url,
            "hls_url": f"{base_url}/hls/stream.m3u8",
            "m3u_url": f"{base_url}/playlist.m3u",
            "epg_url": f"{base_url}/epg.xml",
            "ts_url": f"{base_url}/live.ts",
            "max_locations": config.MAX_LOCATIONS,
            "max_feeds": config.MAX_RSS_FEEDS,
            "is_authenticated": bool(authenticated_user),
            "authenticated_user": authenticated_user or "",
        },
    )


# =====================================================================
# PUBLIC STREAMING & IPTV ENDPOINTS
# =====================================================================

async def _wait_for_m3u8(timeout_sec: float = 8.0) -> str:
    m3u8_path = os.path.join(config.HLS_DIR, "stream.m3u8")
    deadline = asyncio.get_event_loop().time() + timeout_sec
    while asyncio.get_event_loop().time() < deadline:
        if os.path.exists(m3u8_path) and os.path.getsize(m3u8_path) > 0:
            return m3u8_path
        await asyncio.sleep(0.2)
    if not os.path.exists(m3u8_path):
        raise HTTPException(status_code=503, detail="HLS stream is warming up, please retry in a moment.")
    return m3u8_path


@app.get("/hls/stream.m3u8")
async def get_hls_manifest():
    m3u8_path = await _wait_for_m3u8()
    return FileResponse(
        m3u8_path,
        media_type="application/vnd.apple.mpegurl",
        headers=NO_CACHE_HEADERS,
    )


@app.get("/stream.m3u8")
async def get_root_hls_manifest():
    m3u8_path = await _wait_for_m3u8()
    with open(m3u8_path, "r", encoding="utf-8") as f:
        content = f.read()
    rewritten = "\n".join(
        f"/hls/{line}" if line.strip().endswith(".ts") and not line.strip().startswith("/") else line
        for line in content.splitlines()
    ) + "\n"
    return Response(
        content=rewritten,
        media_type="application/vnd.apple.mpegurl",
        headers=NO_CACHE_HEADERS,
    )


@app.get("/hls/{segment_name}")
async def get_hls_segment(segment_name: str):
    if not segment_name.endswith(".ts") or "/" in segment_name or ".." in segment_name:
        raise HTTPException(status_code=404, detail="Invalid segment")
    seg_path = os.path.join(config.HLS_DIR, segment_name)
    if not os.path.exists(seg_path):
        raise HTTPException(status_code=404, detail="Segment not found")
    return FileResponse(
        seg_path,
        media_type="video/mp2t",
        headers={"Cache-Control": "public, max-age=10"},
    )


@app.get("/live.ts")
@app.get("/stream")
async def get_live_mpegts():
    return StreamingResponse(
        stream_manager.iter_live_mpegts(),
        media_type="video/mp2t",
        headers=NO_CACHE_HEADERS,
    )


@app.get("/playlist.m3u")
async def get_m3u_playlist(request: Request):
    base_url = _get_base_url(request)
    disp = settings_store.get_display()
    chno = disp.get("channel_number", config.CHANNEL_NUMBER)
    name = disp.get("channel_name", config.CHANNEL_NAME)
    ch_id = f"retro-weather-{chno}"
    m3u_content = (
        f'#EXTM3U url-tvg="{base_url}/epg.xml"\n'
        f'#EXTINF:-1 tvg-id="{ch_id}" tvg-chno="{chno}" tvg-name="{name}" '
        f'tvg-logo="{base_url}/preview.png" group-title="Weather & Information",{name}\n'
        f"{base_url}/hls/stream.m3u8\n"
    )
    return Response(content=m3u_content, media_type="audio/x-mpegurl")


@app.get("/epg.xml")
async def get_epg_xml(request: Request):
    base_url = _get_base_url(request)
    disp = settings_store.get_display()
    chno = disp.get("channel_number", config.CHANNEL_NUMBER)
    name = disp.get("channel_name", config.CHANNEL_NAME)
    ch_id = f"retro-weather-{chno}"
    now = datetime.now(timezone.utc)
    start_base = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<tv generator-info-name="RetroWeatherHLS">',
        f'  <channel id="{escape(ch_id)}">',
        f'    <display-name>{escape(name)}</display-name>',
        f'    <display-name>{escape(chno)}</display-name>',
        f'    <icon src="{escape(base_url)}/preview.png" />',
        '  </channel>',
    ]

    for h in range(26):
        slot_start = start_base + timedelta(hours=h)
        slot_end = slot_start + timedelta(hours=1)
        s_str = slot_start.strftime("%Y%m%d%H%M%S +0000")
        e_str = slot_end.strftime("%Y%m%d%H%M%S +0000")
        lines.extend([
            f'  <programme start="{s_str}" stop="{e_str}" channel="{escape(ch_id)}">',
            f'    <title lang="en">{escape(name)} - Regional Observations &amp; 5-Day Forecast</title>',
            '    <desc lang="en">Continuous 24-hour vintage 1980s CRT cable TV weather system '
            'displaying live regional observations, 5-day outlooks, and weather wire alerts.</desc>',
            '    <category lang="en">Weather</category>',
            '  </programme>',
        ])

    lines.append("</tv>")
    return Response(content="\n".join(lines), media_type="application/xml")


@app.get("/preview.png")
async def get_preview_png():
    png_bytes = renderer.render_png_bytes()
    return Response(content=png_bytes, media_type="image/png", headers=NO_CACHE_HEADERS)


@app.get("/health")
async def health():
    return {"status": "ok", "hls_ready": stream_manager.is_ready()}


# =====================================================================
# AUTHENTICATION API
# =====================================================================

@app.get("/api/auth/status")
async def api_auth_status(request: Request):
    token = request.cookies.get(config.SESSION_COOKIE_NAME)
    username = settings_store.verify_session_token(token)
    return {"authenticated": bool(username), "username": username}


@app.post("/api/auth/login")
async def api_login(payload: Dict[str, Any] = Body(...)):
    username = str(payload.get("username", ""))
    password = str(payload.get("password", ""))
    if not settings_store.verify_credentials(username, password):
        raise HTTPException(status_code=401, detail="Invalid username or password.")
    token = settings_store.create_session_token()
    resp = JSONResponse({"ok": True, "username": username.strip()})
    resp.set_cookie(
        key=config.SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        max_age=86400 * 7,
        path="/",
    )
    return resp


@app.post("/api/auth/logout")
async def api_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(key=config.SESSION_COOKIE_NAME, path="/")
    return resp


@app.put("/api/auth/credentials")
async def api_update_credentials(
    payload: Dict[str, Any] = Body(...),
    _user: str = Depends(require_admin),
):
    current_password = str(payload.get("current_password", ""))
    new_username = str(payload.get("new_username", "")).strip()
    new_password = str(payload.get("new_password", ""))

    if not settings_store.verify_credentials(_user, current_password):
        raise HTTPException(status_code=400, detail="Current password is incorrect.")

    try:
        saved_user = settings_store.update_credentials(new_username, new_password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    new_token = settings_store.create_session_token()
    resp = JSONResponse({"ok": True, "username": saved_user})
    resp.set_cookie(
        key=config.SESSION_COOKIE_NAME,
        value=new_token,
        httponly=True,
        samesite="lax",
        max_age=86400 * 7,
        path="/",
    )
    return resp


# =====================================================================
# PROTECTED ADMIN PORTAL REST API
# =====================================================================

def _build_admin_state() -> Dict[str, Any]:
    pages = weather_manager.get_pages()
    pub = settings_store.get_public_settings()
    locs = pub.get("locations", [])
    for loc in locs:
        if "code" not in loc:
            loc["code"] = (loc.get("name", "STN")[:3]).upper()
    pub["weather_locations"] = locs
    sample_pages = [
        {
            "page_type": p.page_type,
            "location_name": p.location_name,
            "city_name": p.location_name,
            "station_code": (p.location_name[:3]).upper(),
            "green_row_1": p.green_row_1,
            "green_row_2": p.green_row_2,
            "blue_lines": p.blue_lines if p.blue_lines else [p.raw_paragraph],
            "preview_text": "\n".join(p.blue_lines) if p.blue_lines else p.raw_paragraph,
        }
        for p in pages
    ]
    return {
        "stream": stream_manager.get_status(),
        "weather": weather_manager.get_status(),
        "settings": pub,
        "active_audio": settings_store.get_active_audio(),
        "pages": sample_pages,
        "sample_pages": sample_pages[:20],
        "max_locations": config.MAX_LOCATIONS,
        "max_rss_feeds": config.MAX_RSS_FEEDS,
    }


@app.get("/api/status")
@app.get("/api/state")
async def api_status(_user: str = Depends(require_admin)):
    return _build_admin_state()


@app.post("/api/refresh")
@app.post("/api/weather/refresh")
async def api_refresh(_user: str = Depends(require_admin)):
    st = await asyncio.to_thread(weather_manager.refresh_now)
    state = _build_admin_state()
    state["ok"] = True
    state["page_count"] = len(state["pages"])
    state["weather"] = st
    return state


# --- Weather Locations (Max 5 Cities) & Units ---

@app.post("/api/locations")
async def api_add_location(payload: Dict[str, Any] = Body(...), _user: str = Depends(require_admin)):
    query = str(payload.get("query") or payload.get("name") or "").strip()
    custom_name = str(payload.get("name") or "").strip()
    lat = payload.get("latitude")
    lon = payload.get("longitude")
    try:
        if lat is not None and lon is not None:
            loc = settings_store.add_location(
                name=custom_name or query,
                query=query or custom_name,
                latitude=float(lat),
                longitude=float(lon),
                enabled=bool(payload.get("enabled", True)),
            )
        else:
            geo = await asyncio.to_thread(geocode_city, query)
            loc = settings_store.add_location(
                name=custom_name or geo["name"],
                query=geo["query"],
                latitude=geo["latitude"],
                longitude=geo["longitude"],
                enabled=bool(payload.get("enabled", True)),
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await asyncio.to_thread(weather_manager.refresh_now)
    state = _build_admin_state()
    state["ok"] = True
    state["location"] = loc
    return state


@app.patch("/api/locations/{loc_id}")
async def api_toggle_location(loc_id: str, payload: Dict[str, Any] = Body(...), _user: str = Depends(require_admin)):
    updated = settings_store.toggle_location(loc_id, bool(payload.get("enabled", True)))
    if not updated:
        raise HTTPException(status_code=404, detail="Location not found")
    await asyncio.to_thread(weather_manager.refresh_now)
    state = _build_admin_state()
    state["ok"] = True
    state["location"] = updated
    return state


@app.delete("/api/locations/{loc_id}")
async def api_delete_location(loc_id: str, _user: str = Depends(require_admin)):
    removed = settings_store.delete_location(loc_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Location not found")
    await asyncio.to_thread(weather_manager.refresh_now)
    state = _build_admin_state()
    state["ok"] = True
    return state


@app.put("/api/units")
async def api_set_units(payload: Dict[str, Any] = Body(...), _user: str = Depends(require_admin)):
    try:
        u = settings_store.set_units(str(payload.get("units", "metric")))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await asyncio.to_thread(weather_manager.refresh_now)
    state = _build_admin_state()
    state["ok"] = True
    state["units"] = u
    return state


# --- Optional Weather Alert / News RSS Feeds (Max 5) ---

@app.post("/api/feeds")
async def api_add_feed(payload: Dict[str, Any] = Body(...), _user: str = Depends(require_admin)):
    try:
        new_feed = settings_store.add_rss_feed(
            name=str(payload.get("name", "")),
            url=str(payload.get("url", "")),
            enabled=bool(payload.get("enabled", True)),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await asyncio.to_thread(weather_manager.refresh_now)
    state = _build_admin_state()
    state["ok"] = True
    state["feed"] = new_feed
    return state


@app.patch("/api/feeds/{feed_id}")
async def api_toggle_feed(feed_id: str, payload: Dict[str, Any] = Body(...), _user: str = Depends(require_admin)):
    updated = settings_store.toggle_rss_feed(feed_id, bool(payload.get("enabled", True)))
    if not updated:
        raise HTTPException(status_code=404, detail="Feed not found")
    await asyncio.to_thread(weather_manager.refresh_now)
    state = _build_admin_state()
    state["ok"] = True
    state["feed"] = updated
    return state


@app.delete("/api/feeds/{feed_id}")
async def api_delete_feed(feed_id: str, _user: str = Depends(require_admin)):
    removed = settings_store.delete_rss_feed(feed_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Feed not found")
    await asyncio.to_thread(weather_manager.refresh_now)
    state = _build_admin_state()
    state["ok"] = True
    return state


# --- Radio Station Presets & Audio Control ---

@app.post("/api/radio/presets")
async def api_add_radio_preset(payload: Dict[str, Any] = Body(...), _user: str = Depends(require_admin)):
    activate = bool(payload.get("activate", payload.get("make_active", True)))
    try:
        preset = settings_store.add_radio_preset(
            name=str(payload.get("name", "")),
            url=str(payload.get("url", "")),
            activate=activate,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if activate:
        stream_manager.restart_encoder()
    return {
        "ok": True,
        "preset": preset,
        "radio_presets": settings_store.get_radio_presets(),
        "active_audio": settings_store.get_active_audio(),
    }


@app.delete("/api/radio/presets/{preset_id}")
async def api_delete_radio_preset(preset_id: str, _user: str = Depends(require_admin)):
    prev_active = settings_store.get_active_audio().get("active_radio_id")
    removed = settings_store.delete_radio_preset(preset_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Preset not found")
    if prev_active == preset_id:
        stream_manager.restart_encoder()
    return {
        "ok": True,
        "radio_presets": settings_store.get_radio_presets(),
        "active_audio": settings_store.get_active_audio(),
    }


@app.post("/api/radio/select")
async def api_select_radio(payload: Dict[str, Any] = Body(...), _user: str = Depends(require_admin)):
    audio_mode = str(payload.get("audio_mode", "stream"))
    active_radio_id = payload.get("active_radio_id") or payload.get("preset_id")
    try:
        active = settings_store.set_audio_selection(audio_mode, active_radio_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    stream_manager.restart_encoder()
    return {"ok": True, "active_audio": active}


# --- Video Resolution, FPS & Bitrate ---

@app.put("/api/video")
async def api_update_video(payload: Dict[str, Any] = Body(...), _user: str = Depends(require_admin)):
    try:
        updated = settings_store.update_video(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    renderer.reconfigure(updated["width"], updated["height"])
    stream_manager.restart_encoder()
    return {"ok": True, "video": updated}


# --- Display & CRT Band Settings ---

@app.put("/api/display")
async def api_update_display(payload: Dict[str, Any] = Body(...), _user: str = Depends(require_admin)):
    updated = settings_store.update_display(payload)
    return {"ok": True, "display": updated}
