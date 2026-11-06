import glob
import logging
import os
import shutil
import subprocess
import threading
import time
from typing import Generator, Optional

from app import config
from app.renderer import renderer
from app.settings_store import settings_store

logger = logging.getLogger("retro-weather.streamer")


class HLSStreamManager:
    """
    Continuously renders RGB colour frames from WeatherCRTRenderer and pipes them
    into an FFmpeg process that outputs a live HLS stream (stream.m3u8 + .ts segments).
    Supports live resolution, FPS, bitrate, and radio stream switching.
    """

    def __init__(self) -> None:
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._proc: Optional[subprocess.Popen] = None
        self._frames_rendered = 0
        self._started_at: Optional[float] = None
        self._last_error: Optional[str] = None

    def start(self) -> None:
        if self._running:
            return
        os.makedirs(config.HLS_DIR, exist_ok=True)
        self._clean_hls_dir()

        if not shutil.which("ffmpeg"):
            self._last_error = "ffmpeg binary not found in PATH (running in preview-only mode)"
            logger.warning(self._last_error)
            return

        self._running = True
        self._started_at = time.time()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        logger.info("HLSStreamManager started (output dir: %s)", config.HLS_DIR)

    def stop(self) -> None:
        self._running = False
        self._terminate_proc()

    def restart_encoder(self) -> None:
        logger.info("Restarting FFmpeg encoder to apply updated stream settings...")
        self._terminate_proc()

    def _clean_hls_dir(self) -> None:
        for pattern in ("*.ts", "*.m3u8", "*.tmp"):
            for path in glob.glob(os.path.join(config.HLS_DIR, pattern)):
                try:
                    os.remove(path)
                except OSError:
                    pass

    def _build_ffmpeg_cmd(self, width: int, height: int, fps: int, video_bitrate: str) -> list:
        gop = max(fps, fps * config.HLS_TIME)
        seg_pattern = os.path.join(config.HLS_DIR, "seg_%05d.ts")
        m3u8_path = os.path.join(config.HLS_DIR, "stream.m3u8")

        audio_cfg = settings_store.get_active_audio()
        audio_mode = audio_cfg.get("audio_mode", "hum")
        stream_url = audio_cfg.get("active_radio_url", "").strip()

        cmd = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-thread_queue_size",
            "512",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-s",
            f"{width}x{height}",
            "-r",
            str(fps),
            "-i",
            "pipe:0",
        ]

        audio_filters = []
        if audio_mode == "stream" and stream_url:
            cmd.extend([
                "-thread_queue_size", "512",
                "-reconnect", "1",
                "-reconnect_streamed", "1",
                "-reconnect_delay_max", "5",
                "-i", stream_url,
            ])
        elif audio_mode == "hum":
            cmd.extend([
                "-f", "lavfi",
                "-i", "sine=frequency=60:sample_rate=44100",
            ])
            audio_filters.append("volume=0.015")
        else:
            cmd.extend([
                "-f", "lavfi",
                "-i", "anullsrc=r=44100:cl=stereo",
            ])

        cmd.extend([
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-tune", "zerolatency",
            "-profile:v", "main",
            "-level", "4.0",
            "-pix_fmt", "yuv420p",
            "-b:v", video_bitrate,
            "-maxrate", video_bitrate,
            "-bufsize", "4400k",
            "-g", str(gop),
            "-keyint_min", str(gop),
            "-sc_threshold", "0",
            "-c:a", "aac",
            "-b:a", "128k",
            "-ar", "44100",
            "-ac", "2",
        ])

        if audio_filters:
            cmd.extend(["-af", ",".join(audio_filters)])

        cmd.extend([
            "-f", "hls",
            "-hls_time", str(config.HLS_TIME),
            "-hls_list_size", str(config.HLS_LIST_SIZE),
            "-hls_flags", "delete_segments+append_list+independent_segments+temp_file",
            "-hls_segment_filename", seg_pattern,
            m3u8_path,
        ])
        return cmd

    def _terminate_proc(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def _run_loop(self) -> None:
        while self._running:
            v_cfg = settings_store.get_video()
            width = v_cfg["width"]
            height = v_cfg["height"]
            fps = v_cfg["fps"]
            video_bitrate = v_cfg["video_bitrate"]
            frame_interval = 1.0 / float(max(1, fps))

            renderer.reconfigure(width, height)
            self._clean_hls_dir()
            cmd = self._build_ffmpeg_cmd(width, height, fps, video_bitrate)
            logger.info("Spawning FFmpeg HLS encoder (%dx%d @ %dfps, %s)", width, height, fps, video_bitrate)

            try:
                self._proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    bufsize=width * height * 3 * 2,
                )
            except Exception as exc:
                self._last_error = str(exc)
                logger.error("Failed to start FFmpeg: %s", exc)
                time.sleep(3)
                continue

            stderr_thread = threading.Thread(
                target=self._log_stderr, args=(self._proc,), daemon=True
            )
            stderr_thread.start()

            next_frame_time = time.monotonic()

            try:
                while self._running and self._proc and self._proc.poll() is None:
                    now = time.monotonic()
                    img = renderer.render_frame(now_mono=now)
                    if img.width != width or img.height != height:
                        break
                    raw_bytes = img.tobytes()

                    if self._proc.stdin:
                        self._proc.stdin.write(raw_bytes)
                        self._frames_rendered += 1

                    next_frame_time += frame_interval
                    sleep_dur = next_frame_time - time.monotonic()
                    if sleep_dur > 0:
                        time.sleep(sleep_dur)
                    else:
                        if sleep_dur < -0.25:
                            next_frame_time = time.monotonic()
            except (BrokenPipeError, OSError) as exc:
                if self._running:
                    self._last_error = f"FFmpeg pipe closed: {exc}"
            except Exception as exc:
                self._last_error = f"Render loop error: {exc}"
                logger.exception("Unexpected error in HLS render loop")
            finally:
                self._terminate_proc()

            if self._running:
                time.sleep(0.5)

    def _log_stderr(self, proc: subprocess.Popen) -> None:
        if not proc.stderr:
            return
        try:
            for line in iter(proc.stderr.readline, b""):
                msg = line.decode("utf-8", errors="replace").strip()
                if msg:
                    logger.debug("[ffmpeg] %s", msg)
        except Exception:
            pass

    def is_ready(self) -> bool:
        m3u8_path = os.path.join(config.HLS_DIR, "stream.m3u8")
        return os.path.exists(m3u8_path) and os.path.getsize(m3u8_path) > 0

    def get_status(self) -> dict:
        audio_cfg = settings_store.get_active_audio()
        v_cfg = settings_store.get_video()
        return {
            "running": self._running and (self._proc is not None and self._proc.poll() is None),
            "hls_ready": self.is_ready(),
            "frames_rendered": self._frames_rendered,
            "resolution": f"{v_cfg['width']}x{v_cfg['height']}",
            "width": v_cfg["width"],
            "height": v_cfg["height"],
            "fps": v_cfg["fps"],
            "video_bitrate": v_cfg["video_bitrate"],
            "audio": audio_cfg,
            "uptime_seconds": int(time.time() - self._started_at) if self._started_at else 0,
            "last_error": self._last_error,
        }

    def iter_live_mpegts(self) -> Generator[bytes, None, None]:
        seen_segments = set()
        m3u8_path = os.path.join(config.HLS_DIR, "stream.m3u8")

        for _ in range(40):
            if os.path.exists(m3u8_path):
                break
            time.sleep(0.25)

        while True:
            try:
                if os.path.exists(m3u8_path):
                    with open(m3u8_path, "r", encoding="utf-8") as f:
                        lines = [ln.strip() for ln in f.readlines()]
                    seg_files = [ln for ln in lines if ln.endswith(".ts")]
                    if not seen_segments and len(seg_files) > 2:
                        for old_seg in seg_files[:-2]:
                            seen_segments.add(old_seg)

                    for seg_name in seg_files:
                        if seg_name in seen_segments:
                            continue
                        seg_path = os.path.join(config.HLS_DIR, seg_name)
                        if os.path.exists(seg_path):
                            seen_segments.add(seg_name)
                            with open(seg_path, "rb") as sf:
                                while True:
                                    chunk = sf.read(65536)
                                    if not chunk:
                                        break
                                    yield chunk
                    if len(seen_segments) > 100:
                        seen_segments = set(seg_files)
            except GeneratorExit:
                return
            except Exception as exc:
                logger.debug("MPEG-TS follower error: %s", exc)

            time.sleep(0.4)


stream_manager = HLSStreamManager()
