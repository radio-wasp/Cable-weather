import io
import logging
import math
import os
import random
import textwrap
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image, ImageChops, ImageDraw, ImageFont

from app import config
from app.settings_store import settings_store
from app.weather_fetcher import WeatherPage, weather_manager

logger = logging.getLogger("retro-weather.renderer")

ColorRGB = Tuple[int, int, int]

MONO_BOLD_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf",
    "/usr/share/fonts/truetype/courier-prime/CourierPrime-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeMonoBold.ttf",
    "/System/Library/Fonts/Menlo.ttc",
    "/System/Library/Fonts/Monaco.ttf",
]

SANS_BOLD_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
]


def _load_font(candidates: List[str], size: int) -> ImageFont.FreeTypeFont:
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, max(8, size))
            except Exception:
                continue
    return ImageFont.load_default()


class WeatherCRTRenderer:
    """
    Renders 24-bit RGB frames replicating the 1980s Multi-Band CRT Character Generator
    reference photo:
      1. Top pale steel-blue clock header ("THU DEC  8  10:49:05" in tall condensed blocky digits)
      2. Thin sky-blue sub-header strip ("↓   ENVIRONMENT CANADA WEATHER WIRE   ↓")
      3. Upper forest-green 2-row telemetry band (Station / Temp / Feels / Wind / Humid / Press)
      4. Center deep royal-blue 8-line all-caps teletype forecast / outlook band
      5. Lower forest-green band + curved CRT television bezel & scanlines
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._start_time = time.monotonic()
        v = settings_store.get_video()
        self._init_dimensions(v["width"], v["height"])

    def _init_dimensions(self, width: int, height: int) -> None:
        self.width = width
        self.height = height
        self.scale_x = width / 1280.0
        self.scale_y = height / 720.0
        self.scale = min(self.scale_x, self.scale_y)

        # Band Y-boundaries (matching the 5 bands in the 1980s CRT reference photo)
        self.y_sub_top = self._sy(110)     # End of Band 1 (Top clock bar)
        self.y_green1_top = self._sy(154)  # End of Band 2 (Thin sky-blue sub-header)
        self.y_blue_top = self._sy(308)    # End of Band 3 (Upper forest-green telemetry band)
        self.y_green2_top = self._sy(568)  # End of Band 4 (Center royal-blue 8-line forecast band)

        # Fonts
        self.font_clock = _load_font(MONO_BOLD_CANDIDATES, self._s(48))
        self.font_sub = _load_font(SANS_BOLD_CANDIDATES, self._s(26))
        self.font_green = _load_font(MONO_BOLD_CANDIDATES, self._s(34))
        self.font_blue = _load_font(MONO_BOLD_CANDIDATES, self._s(27))

        self._bg_template = self._build_bg_template()
        self._crt_full_mask = self._build_crt_mask(scanlines=True, bezel=True)
        self._crt_scan_only = self._build_crt_mask(scanlines=True, bezel=False)
        self._crt_bezel_only = self._build_crt_mask(scanlines=False, bezel=True)
        self._noise_frames = self._build_noise_frames(count=8)
        self._noise_idx = 0

    def reconfigure(self, width: int, height: int) -> None:
        with self._lock:
            if width != self.width or height != self.height:
                logger.info("Reconfiguring WeatherCRTRenderer to %dx%d", width, height)
                self._init_dimensions(width, height)

    def _s(self, val: float) -> int:
        return max(1, int(round(val * self.scale)))

    def _sx(self, val: float) -> int:
        return int(round(val * self.scale_x))

    def _sy(self, val: float) -> int:
        return int(round(val * self.scale_y))

    def _build_bg_template(self) -> Image.Image:
        W, H = self.width, self.height
        img = Image.new("RGB", (W, H), color=(20, 108, 22))
        draw = ImageDraw.Draw(img)

        # Band 1: Top pale steel-blue / silver-grey clock header
        draw.rectangle([0, 0, W, self.y_sub_top - 3], fill=(148, 172, 194))
        draw.rectangle([0, self.y_sub_top - 3, W, self.y_sub_top], fill=(24, 34, 44))

        # Band 2: Thin sky-blue sub-header strip
        draw.rectangle([0, self.y_sub_top, W, self.y_green1_top - 3], fill=(120, 152, 182))
        draw.rectangle([0, self.y_green1_top - 3, W, self.y_green1_top], fill=(14, 28, 18))

        # Band 3: Upper Forest-Green 2-row telemetry band
        draw.rectangle([0, self.y_green1_top, W, self.y_blue_top - 3], fill=(20, 108, 22))
        draw.rectangle([0, self.y_blue_top - 3, W, self.y_blue_top], fill=(8, 18, 42))

        # Band 4: Center Deep Royal-Blue 8-line forecast band
        draw.rectangle([0, self.y_blue_top, W, self.y_green2_top - 3], fill=(8, 36, 172))
        draw.rectangle([0, self.y_green2_top - 3, W, self.y_green2_top], fill=(8, 28, 16))

        # Band 5: Lower Forest-Green band
        draw.rectangle([0, self.y_green2_top, W, H], fill=(20, 108, 22))

        return img

    def _draw_cg_text(
        self,
        target: Image.Image,
        pos: Tuple[int, int],
        text: str,
        font: ImageFont.FreeTypeFont,
        fill: ColorRGB = (242, 248, 255),
        shadow_fill: ColorRGB = (10, 14, 20),
        shadow_offset: Tuple[int, int] = (4, 4),
        stretch_x: float = 1.0,
        stretch_y: float = 1.0,
    ) -> int:
        """
        Draws character-generator text with vertical/horizontal stretch (e.g., tall condensed
        clock/green-band characters or wide blue-band characters) and a crisp dark drop shadow.
        """
        if not text:
            return 0

        dummy = ImageDraw.Draw(target)
        bb = dummy.textbbox((0, 0), text, font=font)
        raw_w = max(1, bb[2] - bb[0] + self._s(12))
        raw_h = max(1, bb[3] - bb[1] + self._s(18))

        pad = self._s(8)
        lw = raw_w + pad * 2
        lh = raw_h + pad * 2

        mask = Image.new("L", (lw, lh), color=0)
        mdraw = ImageDraw.Draw(mask)
        mdraw.text((pad, pad), text, fill=255, font=font)

        if abs(stretch_x - 1.0) > 0.01 or abs(stretch_y - 1.0) > 0.01:
            sw = max(1, int(round(lw * stretch_x)))
            sh = max(1, int(round(lh * stretch_y)))
            mask = mask.resize((sw, sh), Image.NEAREST)

        x, y = pos
        sx, sy = shadow_offset
        target.paste(shadow_fill, (x - pad + sx, y - pad + sy), mask)
        target.paste(fill, (x - pad, y - pad), mask)
        return int(round(raw_w * stretch_x))

    def _build_crt_mask(self, scanlines: bool, bezel: bool) -> Image.Image:
        """
        Builds a multiplicative mask with horizontal CRT scanlines, radial phosphor glow,
        and a curved super-elliptical black CRT television bezel around the outer edges.
        """
        W, H = self.width, self.height
        mask_l = Image.new("L", (W, H), color=255)
        pixels = mask_l.load()
        cx, cy = W / 2.0, H / 2.0

        for y in range(H):
            # Pronounced 1980s CRT scanlines
            scan_factor = 0.80 if (scanlines and (y % 2 == 1)) else 1.0
            dy = (y - cy) / (cy * 0.97)
            dy2 = dy * dy
            dy4 = dy2 * dy2

            for x in range(W):
                dx = (x - cx) / (cx * 0.97)
                dx2 = dx * dx
                dx4 = dx2 * dx2

                if bezel:
                    # Super-ellipse CRT tube boundary: x^4 + y^4 + corner term
                    tube_r = dx4 + dy4 + 0.14 * dx2 * dy2
                    if tube_r > 1.04:
                        pixels[x, y] = 0
                        continue
                    elif tube_r > 0.96:
                        # Smooth bevel transition into black CRT bezel
                        edge_fade = max(0.0, (1.04 - tube_r) / 0.08)
                    else:
                        edge_fade = 1.0
                else:
                    edge_fade = 1.0

                # Subtle CRT center-to-corner phosphor brightness falloff
                vig = max(0.66, 1.0 - (dx2 * 0.18 + dy2 * 0.18))
                val = int(255 * scan_factor * vig * edge_fade)
                pixels[x, y] = max(0, min(255, val))

        return Image.merge("RGB", (mask_l, mask_l, mask_l))

    def _build_noise_frames(self, count: int = 8) -> List[Image.Image]:
        W, H = self.width, self.height
        nw, nh = max(1, W // 2), max(1, H // 2)
        frames: List[Image.Image] = []
        rng = random.Random(1983)
        for _ in range(count):
            buf = bytearray(nw * nh)
            for i in range(nw * nh):
                r = rng.randint(0, 100)
                if r > 96:
                    buf[i] = rng.randint(10, 20)
                elif r > 80:
                    buf[i] = rng.randint(3, 8)
                else:
                    buf[i] = 0
            small_l = Image.frombytes("L", (nw, nh), bytes(buf)).resize((W, H), Image.NEAREST)
            frames.append(Image.merge("RGB", (small_l, small_l, small_l)))
        return frames

    def render_frame(self, now_mono: Optional[float] = None) -> Image.Image:
        with self._lock:
            v = settings_store.get_video()
            if v["width"] != self.width or v["height"] != self.height:
                self._init_dimensions(v["width"], v["height"])

            if now_mono is None:
                now_mono = time.monotonic()
            elapsed = max(0.0, now_mono - self._start_time)

            disp = settings_store.get_display()
            W, H = self.width, self.height
            frame = self._bg_template.copy()

            # --- BAND 1: Top Steel-Blue Clock Header ("THU DEC  8  10:49:05") ---
            now_dt = datetime.now()
            day_part = f"{now_dt.strftime('%a %b').upper()} {now_dt.day:>2}"
            time_part = now_dt.strftime("%I:%M:%S")
            clock_str = f"{day_part}   {time_part}"

            # Render tall, condensed blocky characters centered in Band 1
            self._draw_cg_text(
                frame,
                (self._sx(160), self._sy(14)),
                clock_str,
                self.font_clock,
                fill=(244, 249, 255),
                shadow_fill=(26, 34, 44),
                shadow_offset=(self._s(5), self._s(4)),
                stretch_x=1.08,
                stretch_y=1.42,
            )

            # Determine active WeatherPage
            pages = weather_manager.get_pages()
            total_pages = max(1, len(pages))
            page_dur = max(4.0, float(disp.get("page_duration", config.PAGE_DURATION)))
            page_idx = int(elapsed // page_dur) % total_pages
            page_elapsed = elapsed % page_dur
            page: WeatherPage = pages[page_idx]

            # --- BAND 2: Thin Sky-Blue Sub-Header Strip ("↓   ENVIRONMENT CANADA WEATHER WIRE   ↓") ---
            sub_txt = str(disp.get("subheader_text") or page.subheader or config.SUBHEADER_TEXT).upper()
            full_sub = f"↓     {sub_txt}     ↓"
            dummy = ImageDraw.Draw(frame)
            s_bb = dummy.textbbox((0, 0), full_sub, font=self.font_sub)
            s_w = int((s_bb[2] - s_bb[0]) * 1.15)
            sub_x = max(self._sx(60), (W - s_w) // 2)
            self._draw_cg_text(
                frame,
                (sub_x, self.y_sub_top + self._sy(6)),
                full_sub,
                self.font_sub,
                fill=(242, 246, 255),
                shadow_fill=(22, 32, 46),
                shadow_offset=(self._s(3), self._s(3)),
                stretch_x=1.15,
                stretch_y=1.0,
            )

            # --- BAND 3: Upper Forest-Green 2-Row Telemetry Band ---
            # Tall condensed blocky telemetry characters like "BT  HR.WS.A.SLD  WYPA  MCL  MC"
            g_x = self._sx(74)
            self._draw_cg_text(
                frame,
                (g_x, self.y_green1_top + self._sy(12)),
                page.green_row_1,
                self.font_green,
                fill=(238, 248, 236),
                shadow_fill=(6, 30, 8),
                shadow_offset=(self._s(4), self._s(4)),
                stretch_x=0.96,
                stretch_y=1.42,
            )
            self._draw_cg_text(
                frame,
                (g_x, self.y_green1_top + self._sy(82)),
                page.green_row_2,
                self.font_green,
                fill=(238, 248, 236),
                shadow_fill=(6, 30, 8),
                shadow_offset=(self._s(4), self._s(4)),
                stretch_x=0.96,
                stretch_y=1.42,
            )

            # --- BAND 4: Center Deep Royal-Blue 8-Line Teletype Band ---
            if page.blue_lines:
                b_lines = page.blue_lines[:8]
            else:
                b_lines = textwrap.wrap(page.raw_paragraph.upper(), width=41)[:8]

            tw_dur = float(disp.get("typewriter_duration", config.TYPEWRITER_DURATION))
            reveal = min(1.0, page_elapsed / tw_dur) if tw_dur > 0.05 else 1.0
            total_chars = sum(len(ln) for ln in b_lines)
            chars_to_show = int(math.ceil(total_chars * reveal))

            bx = self._sx(74)
            by = self.y_blue_top + self._sy(10)
            b_line_h = self._sy(30)

            rem = chars_to_show
            for ln in b_lines:
                if rem <= 0:
                    break
                visible = ln[:rem]
                rem -= len(ln)
                self._draw_cg_text(
                    frame,
                    (bx, by),
                    visible,
                    self.font_blue,
                    fill=(238, 242, 255),
                    shadow_fill=(4, 8, 32),
                    shadow_offset=(self._s(3), self._s(3)),
                    stretch_x=1.14,
                    stretch_y=0.95,
                )
                by += b_line_h

            # --- BAND 5: Lower Forest-Green Band ---
            footer_txt = str(disp.get("footer_text", "")).strip().upper()
            if footer_txt:
                self._draw_cg_text(
                    frame,
                    (self._sx(74), self.y_green2_top + self._sy(34)),
                    footer_txt,
                    self.font_green,
                    fill=(238, 248, 236),
                    shadow_fill=(6, 30, 8),
                    shadow_offset=(self._s(4), self._s(4)),
                    stretch_x=1.0,
                    stretch_y=1.3,
                )

            # --- Apply Curved CRT Bezel, Scanlines & Analog Noise ---
            use_scan = bool(disp.get("scanlines", config.SCANLINES))
            use_bezel = bool(disp.get("crt_bezel", config.CRT_BEZEL))
            use_noise = bool(disp.get("crt_noise", config.CRT_NOISE))

            if use_noise and self._noise_frames:
                noise = self._noise_frames[self._noise_idx % len(self._noise_frames)]
                self._noise_idx += 1
                frame = ImageChops.add(frame, noise)

            if use_scan and use_bezel:
                frame = ImageChops.multiply(frame, self._crt_full_mask)
            elif use_scan:
                frame = ImageChops.multiply(frame, self._crt_scan_only)
            elif use_bezel:
                frame = ImageChops.multiply(frame, self._crt_bezel_only)

            return frame

    def render_png_bytes(self) -> bytes:
        img = self.render_frame()
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()


renderer = WeatherCRTRenderer()
