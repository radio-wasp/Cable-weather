import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

from app import config

logger = logging.getLogger("retro-weather.settings")


def _hash_password(password: str, salt: str) -> str:
    dk = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        120_000,
    )
    return dk.hex()


def _default_settings() -> Dict[str, Any]:
    default_preset_id = "preset-ckulfm"
    presets = []
    if config.AUDIO_STREAM_URL:
        presets.append({
            "id": default_preset_id,
            "name": "CKUL-FM (Default Stream)",
            "url": config.AUDIO_STREAM_URL,
        })

    salt = secrets.token_hex(16)
    return {
        "locations": [
            {
                "id": "loc-halifax",
                "name": "HALIFAX",
                "query": "Halifax, NS, Canada",
                "latitude": 44.6488,
                "longitude": -63.5752,
                "enabled": True,
            },
            {
                "id": "loc-stjohns",
                "name": "ST. JOHN'S",
                "query": "St. John's, NL, Canada",
                "latitude": 47.5615,
                "longitude": -52.7126,
                "enabled": True,
            },
            {
                "id": "loc-moncton",
                "name": "MONCTON",
                "query": "Moncton, NB, Canada",
                "latitude": 46.0878,
                "longitude": -64.7782,
                "enabled": True,
            },
        ],
        "units": config.UNITS if config.UNITS in ("metric", "imperial") else "metric",
        "rss_feeds": [],
        "radio_presets": presets,
        "active_radio_id": default_preset_id if presets else None,
        "audio_mode": config.AUDIO_MODE if config.AUDIO_MODE in ("stream", "hum", "silent") else "hum",
        "video": {
            "width": config.WIDTH,
            "height": config.HEIGHT,
            "fps": config.FPS,
            "video_bitrate": config.VIDEO_BITRATE,
        },
        "display": {
            "channel_number": config.CHANNEL_NUMBER,
            "channel_name": config.CHANNEL_NAME,
            "subheader_text": config.SUBHEADER_TEXT,
            "footer_text": config.FOOTER_TEXT,
            "page_duration": config.PAGE_DURATION,
            "typewriter_duration": config.TYPEWRITER_DURATION,
            "crt_bezel": config.CRT_BEZEL,
            "scanlines": config.SCANLINES,
            "crt_noise": config.CRT_NOISE,
        },
        "auth": {
            "username": config.ADMIN_USERNAME,
            "salt": salt,
            "password_hash": _hash_password(config.ADMIN_PASSWORD, salt),
            "session_secret": secrets.token_hex(32),
        },
    }


class SettingsStore:
    """
    Thread-safe persistent configuration store for the Retro Weather System:
    - Up to 5 weather locations (geocoded via Open-Meteo) + Metric/Imperial units
    - Up to 5 optional RSS feeds (weather alerts or news wire)
    - Radio station presets + active station / audio mode
    - Video resolution, FPS, and bitrate settings
    - 1980s Multi-Band CRT display settings
    - Operator login credentials & session token verification
    """

    def __init__(self, path: str = config.SETTINGS_FILE) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._data: Dict[str, Any] = _default_settings()
        self._load()

    def _load(self) -> None:
        with self._lock:
            try:
                if os.path.exists(self._path):
                    with open(self._path, "r", encoding="utf-8") as f:
                        loaded = json.load(f)
                    if isinstance(loaded, dict):
                        defaults = _default_settings()
                        for section in ("display", "video", "auth"):
                            if section in loaded and isinstance(loaded[section], dict):
                                merged = dict(defaults[section])
                                merged.update(loaded[section])
                                loaded[section] = merged
                        defaults.update(loaded)
                        defaults["locations"] = list(defaults.get("locations", []))[: config.MAX_LOCATIONS]
                        defaults["rss_feeds"] = list(defaults.get("rss_feeds", []))[: config.MAX_RSS_FEEDS]
                        self._data = defaults
                        logger.info("Loaded settings from %s", self._path)
                        return
            except Exception as exc:
                logger.warning("Could not load settings from %s: %s", self._path, exc)
            self._save_unlocked()

    def _save_unlocked(self) -> None:
        try:
            parent = os.path.dirname(self._path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            tmp_path = f"{self._path}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2)
            os.replace(tmp_path, self._path)
        except Exception as exc:
            logger.warning("Could not persist settings to %s: %s", self._path, exc)

    def get_public_settings(self) -> Dict[str, Any]:
        with self._lock:
            data = json.loads(json.dumps(self._data))
            auth = data.pop("auth", {})
            data["admin_username"] = auth.get("username", config.ADMIN_USERNAME)
            return data

    # --- Authentication & Session Management ---

    def verify_credentials(self, username: str, password: str) -> bool:
        with self._lock:
            auth = self._data.get("auth", {})
            expected_user = auth.get("username", config.ADMIN_USERNAME)
            salt = auth.get("salt", "")
            expected_hash = auth.get("password_hash", "")
            if not salt or not expected_hash:
                return False
            user_ok = hmac.compare_digest(username.strip(), expected_user)
            cand_hash = _hash_password(password, salt)
            pass_ok = hmac.compare_digest(cand_hash, expected_hash)
            return user_ok and pass_ok

    def update_credentials(self, new_username: str, new_password: str) -> str:
        new_username = new_username.strip()
        if not new_username:
            raise ValueError("Username cannot be empty.")
        if len(new_password) < 4:
            raise ValueError("Password must be at least 4 characters.")
        with self._lock:
            salt = secrets.token_hex(16)
            self._data["auth"] = {
                "username": new_username[:40],
                "salt": salt,
                "password_hash": _hash_password(new_password, salt),
                "session_secret": secrets.token_hex(32),
            }
            self._save_unlocked()
            return self._data["auth"]["username"]

    def create_session_token(self, ttl_seconds: int = 86400 * 7) -> str:
        with self._lock:
            auth = self._data.get("auth", {})
            username = auth.get("username", config.ADMIN_USERNAME)
            secret = auth.get("session_secret", "fallback-secret")
            exp = int(time.time()) + ttl_seconds
            payload = f"{username}:{exp}"
            sig = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
            return f"{payload}:{sig}"

    def verify_session_token(self, token: Optional[str]) -> Optional[str]:
        if not token or ":" not in token:
            return None
        parts = token.split(":")
        if len(parts) != 3:
            return None
        username, exp_str, sig = parts
        try:
            exp = int(exp_str)
        except ValueError:
            return None
        if time.time() > exp:
            return None
        with self._lock:
            auth = self._data.get("auth", {})
            secret = auth.get("session_secret", "fallback-secret")
            expected_user = auth.get("username", config.ADMIN_USERNAME)
            if not hmac.compare_digest(username, expected_user):
                return None
            payload = f"{username}:{exp_str}"
            expected_sig = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
            if hmac.compare_digest(sig, expected_sig):
                return username
            return None

    # --- Weather Locations (Max 5) & Units ---

    def get_locations(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._data.get("locations", []))[: config.MAX_LOCATIONS]

    def get_units(self) -> str:
        with self._lock:
            return str(self._data.get("units", "metric"))

    def set_units(self, units: str) -> str:
        u = units.strip().lower()
        if u not in ("metric", "imperial"):
            raise ValueError("units must be 'metric' or 'imperial'")
        with self._lock:
            self._data["units"] = u
            self._save_unlocked()
            return u

    def add_location(
        self, name: str, query: str, latitude: float, longitude: float, enabled: bool = True
    ) -> Dict[str, Any]:
        with self._lock:
            locs = list(self._data.get("locations", []))
            if len(locs) >= config.MAX_LOCATIONS:
                raise ValueError(f"Maximum of {config.MAX_LOCATIONS} weather locations reached. Remove one first.")
            new_loc = {
                "id": f"loc-{uuid.uuid4().hex[:8]}",
                "name": (name.strip() or query.split(",")[0].strip()).upper()[:18],
                "query": query.strip()[:80],
                "latitude": round(float(latitude), 4),
                "longitude": round(float(longitude), 4),
                "enabled": bool(enabled),
            }
            locs.append(new_loc)
            self._data["locations"] = locs
            self._save_unlocked()
            return new_loc

    def toggle_location(self, loc_id: str, enabled: bool) -> Optional[Dict[str, Any]]:
        with self._lock:
            for loc in self._data.get("locations", []):
                if loc.get("id") == loc_id:
                    loc["enabled"] = bool(enabled)
                    self._save_unlocked()
                    return dict(loc)
            return None

    def delete_location(self, loc_id: str) -> bool:
        with self._lock:
            locs = list(self._data.get("locations", []))
            updated = [loc for loc in locs if loc.get("id") != loc_id]
            if len(updated) == len(locs):
                return False
            self._data["locations"] = updated
            self._save_unlocked()
            return True

    # --- Optional Custom RSS Feeds (Max 5) ---

    def get_rss_feeds(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._data.get("rss_feeds", []))[: config.MAX_RSS_FEEDS]

    def add_rss_feed(self, name: str, url: str, enabled: bool = True) -> Dict[str, Any]:
        url = url.strip()
        if not url:
            raise ValueError("RSS feed URL is required.")
        with self._lock:
            feeds = list(self._data.get("rss_feeds", []))
            if len(feeds) >= config.MAX_RSS_FEEDS:
                raise ValueError(f"Maximum of {config.MAX_RSS_FEEDS} RSS feeds reached.")
            new_feed = {
                "id": f"feed-{uuid.uuid4().hex[:8]}",
                "name": (name.strip() or f"Alert Feed {len(feeds) + 1}")[:40],
                "url": url,
                "enabled": bool(enabled),
            }
            feeds.append(new_feed)
            self._data["rss_feeds"] = feeds
            self._save_unlocked()
            return new_feed

    def toggle_rss_feed(self, feed_id: str, enabled: bool) -> Optional[Dict[str, Any]]:
        with self._lock:
            for f in self._data.get("rss_feeds", []):
                if f.get("id") == feed_id:
                    f["enabled"] = bool(enabled)
                    self._save_unlocked()
                    return dict(f)
            return None

    def delete_rss_feed(self, feed_id: str) -> bool:
        with self._lock:
            feeds = list(self._data.get("rss_feeds", []))
            updated = [f for f in feeds if f.get("id") != feed_id]
            if len(updated) == len(feeds):
                return False
            self._data["rss_feeds"] = updated
            self._save_unlocked()
            return True

    # --- Radio Station Presets & Audio Mode ---

    def get_radio_presets(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._data.get("radio_presets", []))

    def add_radio_preset(self, name: str, url: str, activate: bool = False) -> Dict[str, Any]:
        url = url.strip()
        if not url:
            raise ValueError("Radio stream URL is required.")
        with self._lock:
            presets = list(self._data.get("radio_presets", []))
            preset = {
                "id": f"radio-{uuid.uuid4().hex[:8]}",
                "name": (name.strip() or f"Station {len(presets) + 1}")[:50],
                "url": url,
            }
            presets.append(preset)
            self._data["radio_presets"] = presets
            if activate or not self._data.get("active_radio_id"):
                self._data["active_radio_id"] = preset["id"]
                self._data["audio_mode"] = "stream"
            self._save_unlocked()
            return preset

    def delete_radio_preset(self, preset_id: str) -> bool:
        with self._lock:
            presets = list(self._data.get("radio_presets", []))
            updated = [p for p in presets if p.get("id") != preset_id]
            if len(updated) == len(presets):
                return False
            self._data["radio_presets"] = updated
            if self._data.get("active_radio_id") == preset_id:
                self._data["active_radio_id"] = updated[0]["id"] if updated else None
            self._save_unlocked()
            return True

    def set_audio_selection(self, audio_mode: str, active_radio_id: Optional[str] = None) -> Dict[str, Any]:
        mode = audio_mode.strip().lower()
        if mode not in ("stream", "hum", "silent"):
            raise ValueError("audio_mode must be 'stream', 'hum', or 'silent'")
        with self._lock:
            self._data["audio_mode"] = mode
            if active_radio_id is not None:
                self._data["active_radio_id"] = active_radio_id
            self._save_unlocked()
            return self.get_active_audio()

    def get_active_audio(self) -> Dict[str, Any]:
        with self._lock:
            mode = self._data.get("audio_mode", "hum")
            active_id = self._data.get("active_radio_id")
            presets = self._data.get("radio_presets", [])
            active_preset = next((p for p in presets if p.get("id") == active_id), None)
            if active_preset is None and presets:
                active_preset = presets[0]
            return {
                "audio_mode": mode,
                "active_radio_id": active_preset["id"] if active_preset else None,
                "active_radio_name": active_preset["name"] if active_preset else None,
                "active_radio_url": active_preset["url"] if active_preset else "",
            }

    # --- Video Resolution, Framerate & Bitrate ---

    def get_video(self) -> Dict[str, Any]:
        with self._lock:
            v = dict(self._data.get("video", {}))
            return {
                "width": int(v.get("width", config.WIDTH)),
                "height": int(v.get("height", config.HEIGHT)),
                "fps": int(v.get("fps", config.FPS)),
                "video_bitrate": str(v.get("video_bitrate", config.VIDEO_BITRATE)),
            }

    def update_video(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            v = self.get_video()
            if "width" in updates and updates["width"] is not None:
                w = int(updates["width"])
                v["width"] = max(320, min(1920, w - (w % 2)))
            if "height" in updates and updates["height"] is not None:
                h = int(updates["height"])
                v["height"] = max(240, min(1080, h - (h % 2)))
            if "fps" in updates and updates["fps"] is not None:
                v["fps"] = max(5, min(60, int(updates["fps"])))
            if "video_bitrate" in updates and updates["video_bitrate"] is not None:
                br = str(updates["video_bitrate"]).strip().lower()
                if not br.endswith("k") and not br.endswith("m"):
                    br = f"{int(br)}k"
                v["video_bitrate"] = br
            self._data["video"] = v
            self._save_unlocked()
            return dict(v)

    # --- Display & CRT Band Settings ---

    def get_display(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._data.get("display", {}))

    def update_display(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            disp = dict(self._data.get("display", {}))
            for key in ("channel_number", "channel_name", "subheader_text", "footer_text"):
                if key in updates and updates[key] is not None:
                    disp[key] = str(updates[key]).strip()[:56]
            for float_key in ("page_duration", "typewriter_duration"):
                if float_key in updates and updates[float_key] is not None:
                    disp[float_key] = max(0.0, float(updates[float_key]))
            for bool_key in ("crt_bezel", "scanlines", "crt_noise"):
                if bool_key in updates and updates[bool_key] is not None:
                    disp[bool_key] = bool(updates[bool_key])
            self._data["display"] = disp
            self._save_unlocked()
            return dict(disp)


settings_store = SettingsStore()
