#!/usr/bin/env python3
"""
Grabador de cámaras IP con segmentación alineada al reloj y escritura robusta.

Mejoras principales sobre la versión anterior:
- Segmentación exacta por hora usando reloj de pared.
- Escritura desacoplada de la captura para reducir lag y jitter.
- Clips con duración consistente aunque la cámara entregue FPS variables.
- Reintentos de conexión con backoff.
- Normalización de frames a dimensiones pares para evitar fallos de VideoWriter.
- Detección de stream congelado y bajo espacio en disco.
- Sustitución de variables de entorno en YAML (${VAR}).
- Logs operacionales con métricas reales de captura/escritura.
"""

from __future__ import annotations

import logging
import math
import os
import re
import shutil
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Tuple

import cv2
import yaml

# Recomendado para RTSP inestable. No sobreescribe si el usuario ya definió algo.
os.environ.setdefault(
    "OPENCV_FFMPEG_CAPTURE_OPTIONS",
    "rtsp_transport;tcp|stimeout;5000000",
)

ENV_VAR_PATTERN = re.compile(r"\$\{([^}:]+)(?::-(.*?))?\}")
VIDEO_EXTS = {"mp4", "avi", "mkv"}
DEFAULT_FALLBACK_CODECS = ["mp4v", "XVID", "MJPG"]


@dataclass
class CameraConfig:
    name: str
    url: str
    enabled: bool = True
    split: bool = False
    split_names: List[str] = field(default_factory=lambda: ["left", "right"])
    split_axis: str = "vertical"  # vertical=izquierda/derecha, horizontal=arriba/abajo
    fps: float = 15.0
    width: int = 0
    height: int = 0
    output_fps: float = 0.0
    max_live_frame_age_seconds: float = 10.0


@dataclass
class RecordingConfig:
    segment_duration_minutes: int = 60
    output_dir: str = "/recordings"
    video_format: str = "mp4"
    codec: str = "mp4v"
    fallback_codecs: List[str] = field(default_factory=lambda: DEFAULT_FALLBACK_CODECS.copy())
    reconnect_delay_seconds: int = 10
    reconnect_delay_max_seconds: int = 60
    max_reconnect_attempts: int = 0
    log_level: str = "INFO"
    min_disk_free_gb: float = 1.0
    startup_frame_timeout_seconds: int = 20
    stale_stream_timeout_seconds: int = 12


@dataclass
class SegmentWriter:
    final_path: Path
    temp_path: Path
    writer: cv2.VideoWriter
    codec: str
    fps: float
    size: Tuple[int, int]

    def close(self, commit: bool = True):
        self.writer.release()
        if commit:
            self.temp_path.replace(self.final_path)
        else:
            try:
                self.temp_path.unlink(missing_ok=True)
            except Exception:
                pass


class CaptureBuffer:
    def __init__(self):
        self._lock = threading.Lock()
        self._frame = None
        self._frame_ts = 0.0
        self._frame_shape = None
        self.frames_captured = 0
        self.read_failures = 0

    def update(self, frame):
        now = time.monotonic()
        with self._lock:
            self._frame = frame
            self._frame_ts = now
            self._frame_shape = frame.shape[:2]
            self.frames_captured += 1

    def mark_failure(self):
        with self._lock:
            self.read_failures += 1

    def snapshot(self):
        with self._lock:
            return self._frame, self._frame_ts, self._frame_shape


def substitute_env_vars(text: str) -> str:
    def repl(match: re.Match[str]) -> str:
        var_name = match.group(1)
        default = match.group(2) or ""
        return os.environ.get(var_name, default)

    return ENV_VAR_PATTERN.sub(repl, text)


def load_config(config_path: str) -> Tuple[List[CameraConfig], RecordingConfig]:
    raw_text = Path(config_path).read_text(encoding="utf-8")
    raw = yaml.safe_load(substitute_env_vars(raw_text)) or {}

    rec_raw = raw.get("recording", {})
    fallback_codecs = rec_raw.get("fallback_codecs", DEFAULT_FALLBACK_CODECS)
    if isinstance(fallback_codecs, str):
        fallback_codecs = [c.strip() for c in fallback_codecs.split(",") if c.strip()]

    rec_cfg = RecordingConfig(
        segment_duration_minutes=int(rec_raw.get("segment_duration_minutes", 60)),
        output_dir=rec_raw.get("output_dir", "/recordings"),
        video_format=str(rec_raw.get("video_format", "mp4")).lower(),
        codec=str(rec_raw.get("codec", "mp4v")),
        fallback_codecs=list(dict.fromkeys([str(c) for c in fallback_codecs if c])),
        reconnect_delay_seconds=int(rec_raw.get("reconnect_delay_seconds", 10)),
        reconnect_delay_max_seconds=int(rec_raw.get("reconnect_delay_max_seconds", 60)),
        max_reconnect_attempts=int(rec_raw.get("max_reconnect_attempts", 0)),
        log_level=rec_raw.get("log_level", "INFO"),
        min_disk_free_gb=float(rec_raw.get("min_disk_free_gb", 1.0)),
        startup_frame_timeout_seconds=int(rec_raw.get("startup_frame_timeout_seconds", 20)),
        stale_stream_timeout_seconds=int(rec_raw.get("stale_stream_timeout_seconds", 12)),
    )

    cameras: List[CameraConfig] = []
    for cam in raw.get("cameras", []):
        if not cam.get("name") or not cam.get("url"):
            raise ValueError(f"Cámara inválida en config: {cam!r}")

        default_split_names = [f"{cam['name']}_izquierda", f"{cam['name']}_derecha"]
        split_names = cam.get("split_names", default_split_names)
        if cam.get("split", False) and len(split_names) < 2:
            raise ValueError(f"La cámara {cam['name']} tiene split=true pero menos de 2 split_names")

        cameras.append(
            CameraConfig(
                name=str(cam["name"]),
                url=str(cam["url"]),
                enabled=bool(cam.get("enabled", True)),
                split=bool(cam.get("split", False)),
                split_names=list(split_names),
                split_axis=str(cam.get("split_axis", "vertical")).lower(),
                fps=float(cam.get("fps", 15.0)),
                width=int(cam.get("width", 0)),
                height=int(cam.get("height", 0)),
                output_fps=float(cam.get("output_fps", 0.0)),
                max_live_frame_age_seconds=float(
                    cam.get("max_live_frame_age_seconds", rec_cfg.stale_stream_timeout_seconds)
                ),
            )
        )

    if rec_cfg.video_format not in VIDEO_EXTS:
        raise ValueError(f"Formato de video no soportado: {rec_cfg.video_format}")
    if rec_cfg.segment_duration_minutes <= 0:
        raise ValueError("segment_duration_minutes debe ser > 0")

    return cameras, rec_cfg


def get_fourcc(codec: str) -> int:
    if len(codec) != 4:
        raise ValueError(f"Codec inválido: {codec}")
    return cv2.VideoWriter_fourcc(*codec)


def setup_logger(name: str, log_level: str, log_dir: str = "/logs") -> logging.Logger:
    level = getattr(logging, log_level.upper(), logging.INFO)
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = False

    if logger.handlers:
        return logger

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(level)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    try:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(f"{log_dir}/{name}.log")
        fh.setLevel(level)
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except Exception:
        pass

    return logger


def next_segment_boundary(now: datetime, segment_minutes: int) -> datetime:
    minute_block = (now.minute // segment_minutes) * segment_minutes
    boundary = now.replace(minute=minute_block, second=0, microsecond=0)
    if boundary <= now:
        boundary += timedelta(minutes=segment_minutes)
    return boundary


def build_output_path(output_dir: str, camera_name: str, segment_start: datetime, fmt: str) -> Path:
    month_folder = segment_start.strftime("%Y-%m")
    day_folder = segment_start.strftime("%d")
    folder = Path(output_dir) / camera_name / month_folder / day_folder
    folder.mkdir(parents=True, exist_ok=True)
    filename = f"{camera_name}_{segment_start.strftime('%Y%m%d_%H%M%S')}.{fmt}"
    return folder / filename


def ensure_even_frame(frame):
    if frame is None:
        return None
    h, w = frame.shape[:2]
    new_h = h - (h % 2)
    new_w = w - (w % 2)
    if new_h <= 0 or new_w <= 0:
        return frame
    if new_h == h and new_w == w:
        return frame
    return frame[:new_h, :new_w]


class CameraRecorder:
    def __init__(self, cam: CameraConfig, rec: RecordingConfig):
        self.cam = cam
        self.rec = rec
        self.logger = setup_logger(cam.name, rec.log_level)
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._capture_thread: Optional[threading.Thread] = None
        self._capture_stop = threading.Event()
        self._capture_buffer = CaptureBuffer()
        self._cap: Optional[cv2.VideoCapture] = None

    def start(self):
        self._thread = threading.Thread(target=self._run, name=self.cam.name, daemon=True)
        self._thread.start()

    def stop(self):
        self.logger.info("Deteniendo grabación...")
        self._stop_event.set()
        self._stop_capture_thread()
        if self._thread:
            self._thread.join(timeout=20)

    def _run(self):
        attempt = 0
        while not self._stop_event.is_set():
            attempt += 1
            max_att = self.rec.max_reconnect_attempts
            if max_att > 0 and attempt > max_att:
                self.logger.error("Máximo de intentos (%s) alcanzado. Saliendo.", max_att)
                break

            self.logger.info("Conectando (intento %s) -> %s", attempt, self._masked_url())
            self._cap = self._open_capture()
            if self._cap is None or not self._cap.isOpened():
                self.logger.warning(
                    "No se pudo abrir la cámara. Reintentando en %ss...",
                    self._current_backoff(attempt),
                )
                self._wait(self._current_backoff(attempt))
                continue

            self._capture_buffer = CaptureBuffer()
            self._capture_stop.clear()
            self._capture_thread = threading.Thread(
                target=self._capture_loop,
                name=f"{self.cam.name}-capture",
                daemon=True,
            )
            self._capture_thread.start()

            if not self._wait_for_first_frame():
                self.logger.warning("No llegó un frame inicial en el tiempo esperado. Se forzará reconexión.")
                self._stop_capture_thread()
                self._wait(self._current_backoff(attempt))
                continue

            attempt = 0
            self.logger.info("Conexión establecida. Iniciando grabación.")
            self._record_loop()
            self._stop_capture_thread()

            if not self._stop_event.is_set():
                self.logger.warning(
                    "Conexión perdida o stream congelado. Reintentando en %ss...",
                    self.rec.reconnect_delay_seconds,
                )
                self._wait(self.rec.reconnect_delay_seconds)

    def _masked_url(self) -> str:
        return re.sub(r"://([^:@/]+):([^@/]+)@", r"://\1:***@", self.cam.url)

    def _current_backoff(self, attempt: int) -> int:
        base = max(1, self.rec.reconnect_delay_seconds)
        max_delay = max(base, self.rec.reconnect_delay_max_seconds)
        return min(max_delay, base * max(1, attempt))

    def _open_capture(self) -> Optional[cv2.VideoCapture]:
        cap = cv2.VideoCapture(self.cam.url, cv2.CAP_FFMPEG)
        if self.cam.width > 0:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.cam.width)
        if self.cam.height > 0:
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.cam.height)
        if self.cam.fps > 0:
            cap.set(cv2.CAP_PROP_FPS, self.cam.fps)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        return cap

    def _capture_loop(self):
        assert self._cap is not None
        while not self._capture_stop.is_set() and not self._stop_event.is_set():
            ok, frame = self._cap.read()
            if not ok or frame is None:
                self._capture_buffer.mark_failure()
                time.sleep(0.05)
                continue
            self._capture_buffer.update(frame)

    def _stop_capture_thread(self):
        self._capture_stop.set()
        if self._capture_thread:
            self._capture_thread.join(timeout=5)
            self._capture_thread = None
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:
                pass
            self._cap = None

    def _wait_for_first_frame(self) -> bool:
        deadline = time.monotonic() + self.rec.startup_frame_timeout_seconds
        while time.monotonic() < deadline and not self._stop_event.is_set():
            frame, frame_ts, _ = self._capture_buffer.snapshot()
            if frame is not None and frame_ts > 0:
                return True
            time.sleep(0.05)
        return False

    def _resolve_target_fps(self) -> float:
        if self.cam.output_fps > 0:
            return self.cam.output_fps
        if self.cam.fps > 0:
            return self.cam.fps
        return 15.0

    def _record_loop(self):
        target_fps = max(1.0, self._resolve_target_fps())
        frame_interval = 1.0 / target_fps
        self.logger.info("FPS objetivo de escritura: %.3f", target_fps)

        while not self._stop_event.is_set():
            frame, frame_ts, _ = self._capture_buffer.snapshot()
            if frame is None:
                self.logger.warning("No se pudo leer el frame inicial del segmento.")
                break

            frame_age = time.monotonic() - frame_ts
            if frame_age > self.cam.max_live_frame_age_seconds:
                self.logger.warning("El frame inicial está demasiado viejo (%.2fs). Se forzará reconexión.", frame_age)
                break

            now_wall = datetime.now()
            segment_end = next_segment_boundary(now_wall, self.rec.segment_duration_minutes)
            self._warn_if_low_disk()
            writers = self._create_writers(frame, target_fps, now_wall)
            if not writers:
                self.logger.error("No se pudieron crear los writers del segmento.")
                break

            self.logger.info(
                "Nuevo segmento desde %s hasta %s",
                now_wall.strftime("%Y-%m-%d %H:%M:%S"),
                segment_end.strftime("%Y-%m-%d %H:%M:%S"),
            )

            frames_written = 0
            writes_with_repeated_frame = 0
            segment_started_monotonic = time.monotonic()
            next_write_at = segment_started_monotonic
            segment_ok = True
            last_seen_capture_ts = frame_ts

            try:
                while not self._stop_event.is_set():
                    now_mono = time.monotonic()
                    now_wall = datetime.now()
                    if now_wall >= segment_end:
                        break

                    if now_mono < next_write_at:
                        self._stop_event.wait(min(0.02, next_write_at - now_mono))
                        continue

                    current_frame, current_capture_ts, _ = self._capture_buffer.snapshot()
                    if current_frame is None:
                        self.logger.warning("No hay frame disponible para escribir.")
                        segment_ok = False
                        break

                    frame_age = now_mono - current_capture_ts
                    if frame_age > self.cam.max_live_frame_age_seconds:
                        self.logger.warning(
                            "Stream congelado: último frame con %.2fs de antigüedad.",
                            frame_age,
                        )
                        segment_ok = False
                        break

                    if math.isclose(current_capture_ts, last_seen_capture_ts, rel_tol=0.0, abs_tol=1e-6):
                        writes_with_repeated_frame += 1
                    else:
                        last_seen_capture_ts = current_capture_ts

                    frames = self._split_frame(current_frame) if self.cam.split else [current_frame]
                    for i, seg_writer in enumerate(writers):
                        if i >= len(frames):
                            continue
                        prepared = ensure_even_frame(frames[i])
                        seg_writer.writer.write(prepared)
                    frames_written += 1
                    next_write_at += frame_interval

                    lag = time.monotonic() - next_write_at
                    if lag > frame_interval * 3:
                        # Re-sincroniza si la escritura quedó muy atrasada.
                        next_write_at = time.monotonic() + frame_interval
            finally:
                for seg_writer in writers:
                    try:
                        seg_writer.close(commit=frames_written > 0)
                        self.logger.info(
                            "Segmento guardado: %s (%s frames, codec=%s, fps=%.3f, size=%sx%s)",
                            seg_writer.final_path,
                            frames_written,
                            seg_writer.codec,
                            seg_writer.fps,
                            seg_writer.size[0],
                            seg_writer.size[1],
                        )
                    except Exception as exc:
                        self.logger.exception("Error cerrando segmento %s: %s", seg_writer.final_path, exc)

                elapsed = max(0.001, time.monotonic() - segment_started_monotonic)
                capture_fps = self._capture_buffer.frames_captured / max(0.001, elapsed)
                write_fps_real = frames_written / elapsed
                self.logger.info(
                    "Resumen segmento: elapsed=%.2fs, capture_fps=%.2f, write_fps=%.2f, repeated_writes=%s, read_failures=%s",
                    elapsed,
                    capture_fps,
                    write_fps_real,
                    writes_with_repeated_frame,
                    self._capture_buffer.read_failures,
                )

            if self._stop_event.is_set():
                break

            if not segment_ok:
                break

    def _create_writers(
        self,
        frame,
        fps: float,
        segment_start: datetime,
    ) -> Optional[List[SegmentWriter]]:
        frame = ensure_even_frame(frame)
        frame_h, frame_w = frame.shape[:2]
        if frame_w <= 0 or frame_h <= 0:
            return None

        writers: List[SegmentWriter] = []

        if self.cam.split:
            split_frames = self._split_frame(frame)
            names = self.cam.split_names[: len(split_frames)]
            for sub_name, sub_frame in zip(names, split_frames):
                sub_frame = ensure_even_frame(sub_frame)
                sh, sw = sub_frame.shape[:2]
                writer = self._open_segment_writer(
                    build_output_path(self.rec.output_dir, sub_name, segment_start, self.rec.video_format),
                    fps,
                    (sw, sh),
                )
                if writer is None:
                    for prev in writers:
                        prev.close(commit=False)
                    return None
                writers.append(writer)
            return writers

        writer = self._open_segment_writer(
            build_output_path(self.rec.output_dir, self.cam.name, segment_start, self.rec.video_format),
            fps,
            (frame_w, frame_h),
        )
        if writer is None:
            return None
        return [writer]

    def _open_segment_writer(self, final_path: Path, fps: float, size: Tuple[int, int]) -> Optional[SegmentWriter]:
        final_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = final_path.with_name(f"{final_path.stem}.part.{final_path.suffix.lstrip('.')}")
        codecs = list(dict.fromkeys([self.rec.codec] + self.rec.fallback_codecs))

        for codec in codecs:
            try:
                fourcc = get_fourcc(codec)
                writer = cv2.VideoWriter(str(temp_path), fourcc, fps, size)
                if writer.isOpened():
                    self.logger.info(
                        "Writer listo: path=%s codec=%s fps=%.3f size=%sx%s",
                        final_path,
                        codec,
                        fps,
                        size[0],
                        size[1],
                    )
                    return SegmentWriter(
                        final_path=final_path,
                        temp_path=temp_path,
                        writer=writer,
                        codec=codec,
                        fps=fps,
                        size=size,
                    )
            except Exception as exc:
                self.logger.warning("Falló codec %s para %s: %s", codec, final_path, exc)

        self.logger.error(
            "No se pudo abrir VideoWriter para %s (size=%sx%s, fps=%.3f, codecs=%s)",
            final_path,
            size[0],
            size[1],
            fps,
            codecs,
        )
        return None

    def _split_frame(self, frame) -> List:
        frame = ensure_even_frame(frame)
        h, w = frame.shape[:2]
        axis = (self.cam.split_axis or "vertical").lower()

        if axis == "horizontal":
            mid = (h // 2) - ((h // 2) % 2)
            top = frame[:mid, :]
            bottom = frame[mid:, :]
            return [ensure_even_frame(top), ensure_even_frame(bottom)]

        mid = (w // 2) - ((w // 2) % 2)
        left = frame[:, :mid]
        right = frame[:, mid:]
        return [ensure_even_frame(left), ensure_even_frame(right)]

    def _warn_if_low_disk(self):
        try:
            usage = shutil.disk_usage(self.rec.output_dir)
            free_gb = usage.free / (1024 ** 3)
            if free_gb < self.rec.min_disk_free_gb:
                self.logger.warning(
                    "Espacio libre bajo en %s: %.2f GB libres (mínimo recomendado %.2f GB)",
                    self.rec.output_dir,
                    free_gb,
                    self.rec.min_disk_free_gb,
                )
        except Exception:
            pass

    def _wait(self, seconds: float):
        self._stop_event.wait(timeout=max(0.0, seconds))


class RecorderOrchestrator:
    def __init__(self, config_path: str):
        cameras, self.rec = load_config(config_path)
        self.logger = setup_logger("orchestrator", self.rec.log_level)
        self.recorders: List[CameraRecorder] = []

        for cam in cameras:
            if cam.enabled:
                self.recorders.append(CameraRecorder(cam, self.rec))
            else:
                self.logger.info("Cámara desactivada, omitiendo: %s", cam.name)

    def start_all(self):
        self.logger.info("Iniciando %s grabador(es)...", len(self.recorders))
        for recorder in self.recorders:
            recorder.start()
        self.logger.info("Todos los grabadores iniciados.")

    def stop_all(self):
        self.logger.info("Señal de parada recibida. Cerrando grabadores...")
        for recorder in self.recorders:
            recorder.stop()
        self.logger.info("Todos los grabadores detenidos.")

    def wait(self):
        try:
            while True:
                time.sleep(5)
                alive = [r.cam.name for r in self.recorders if r._thread and r._thread.is_alive()]
                self.logger.debug("Grabadores activos: %s", alive)
        except KeyboardInterrupt:
            pass


def main():
    config_path = os.environ.get("CONFIG_PATH", "/config/cameras.yaml")

    if not Path(config_path).exists():
        print(f"ERROR: Archivo de configuración no encontrado: {config_path}", file=sys.stderr)
        sys.exit(1)

    orchestrator = RecorderOrchestrator(config_path)

    def _signal_handler(sig, frame):
        orchestrator.stop_all()
        sys.exit(0)

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    orchestrator.start_all()
    orchestrator.wait()
    orchestrator.stop_all()


if __name__ == "__main__":
    main()
