import os


def _env_bool(name: str, default: bool = True) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


PORT = int(os.getenv("PORT", "8080"))
DATA_DIR = os.getenv("DATA_DIR", "/app/data")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")

# Admin Portal Authentication Defaults
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin").strip()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "cable10").strip()
SESSION_COOKIE_NAME = "retro_weather_session"

# Channel & Header Branding (1980s Multi-Band CRT Character Generator)
CHANNEL_NUMBER = os.getenv("CHANNEL_NUMBER", "12")
CHANNEL_NAME = os.getenv("CHANNEL_NAME", "RETRO WEATHER WIRE")
SUBHEADER_TEXT = os.getenv("SUBHEADER_TEXT", "ENVIRONMENT CANADA WEATHER WIRE")
FOOTER_TEXT = os.getenv("FOOTER_TEXT", "")  # Empty = classic solid green lower band like reference photo
UNITS = os.getenv("UNITS", "metric").strip().lower()  # "metric" or "imperial"

# Video / HLS Settings
WIDTH = int(os.getenv("WIDTH", "1280"))
HEIGHT = int(os.getenv("HEIGHT", "720"))
FPS = int(os.getenv("FPS", "24"))
VIDEO_BITRATE = os.getenv("VIDEO_BITRATE", "2200k")
HLS_DIR = os.getenv("HLS_DIR", "/tmp/hls")
HLS_TIME = int(os.getenv("HLS_TIME", "2"))
HLS_LIST_SIZE = int(os.getenv("HLS_LIST_SIZE", "8"))

# Page & Ticker Timing
PAGE_DURATION = float(os.getenv("PAGE_DURATION", "10.0"))
TYPEWRITER_DURATION = float(os.getenv("TYPEWRITER_DURATION", "0.9"))

# Visual CRT Aesthetics (matching the 1980s curved CRT TV reference photo)
CRT_BEZEL = _env_bool("CRT_BEZEL", True)
SCANLINES = _env_bool("SCANLINES", True)
CRT_NOISE = _env_bool("CRT_NOISE", True)

# Default Audio Settings
AUDIO_MODE = os.getenv("AUDIO_MODE", "stream").strip().lower()
AUDIO_STREAM_URL = os.getenv(
    "AUDIO_STREAM_URL",
    "https://chi-master3-entry1.leanstream-hd.com:2113/CKULFM",
).strip()

# Weather & RSS Refresh Settings
MAX_LOCATIONS = 5
MAX_RSS_FEEDS = 5
REFRESH_INTERVAL_MINUTES = int(os.getenv("REFRESH_INTERVAL_MINUTES", "10"))
