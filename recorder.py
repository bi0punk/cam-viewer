#!/usr/bin/env python3
"""
Grabador de cámaras IP con segmentos alineados al reloj.

Características clave:
- múltiples cámaras simultáneas
- primer clip parcial hasta la siguiente frontera horaria
- clips siguientes de 1 hora exacta
- estructura de salida por cámara/mes/día
- split correcto izquierda/derecha para cámaras side-by-side
- opción opcional split_axis=horizontal para cámaras top/bottom
- reconexión automática
"""

import logging
import os
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


@dataclass
class RecordingConfig:
    segment_duration_minutes: int = 60
    output_dir: str = "/recordings"
    video_format: str = "mp4"
    codec: str = "mp4v"
    reconnect_delay_seconds: int = 10
    max_reconnect_attempts: int = 0
    log_level: str = "INFO"


def load_config(config_path: str) -> Tuple[List[CameraConfig], RecordingConfig]:
    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    rec_raw = raw.get("recording", {})
    rec_cfg = RecordingConfig(
        segment_duration_minutes=int(rec_raw.get("segment_duration_minutes", 60)),
        output_dir=rec_raw.get("output_dir", "/recordings"),
        video_format=rec_raw.get("video_format", "mp4"),
        codec=rec_raw.get("codec", "mp4v"),
        reconnect_delay_seconds=int(rec_raw.get("reconnect_delay_seconds", 10)),
        max_reconnect_attempts=int(rec_raw.get("max_reconnect_attempts", 0)),
        log_level=rec_raw.get("log_level", "INFO"),
    )

    cameras: List[CameraConfig] = []
    for cam in raw.get("cameras", []):
        default_split_names = [f"{cam['name']}_izquierda", f"{cam['name']}_derecha"]
        cameras.append(
            CameraConfig(
                name=cam["name"],
                url=cam["url"],
                enabled=cam.get("enabled", True),
                split=cam.get("split", False),
                split_names=cam.get("split_names", default_split_names),
                split_axis=str(cam.get("split_axis", "vertical")).lower(),
                fps=float(cam.get("fps", 15.0)),
                width=int(cam.get("width", 0)),
                height=int(cam.get("height", 0)),
            )
        )

    return cameras, rec_cfg


def get_fourcc(codec: str) -> int:
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
    """
    Devuelve el siguiente límite alineado al reloj.

    Ejemplos con 60 minutos:
    - 10:23:10 -> 11:00:00
    - 10:00:00 -> 11:00:00
    - 23:40:00 -> 00:00:00 del día siguiente
    """
    if segment_minutes <= 0:
        raise ValueError("segment_minutes debe ser > 0")

    minute_block = (now.minute // segment_minutes) * segment_minutes
    boundary = now.replace(minute=minute_block, second=0, microsecond=0)
    if boundary <= now:
        boundary += timedelta(minutes=segment_minutes)
    return boundary


def build_output_path(output_dir: str, camera_name: str, segment_start: datetime, fmt: str) -> str:
    month_folder = segment_start.strftime("%Y-%m")
    day_folder = segment_start.strftime("%d")
    folder = Path(output_dir) / camera_name / month_folder / day_folder
    folder.mkdir(parents=True, exist_ok=True)
    filename = f"{camera_name}_{segment_start.strftime('%Y%m%d_%H%M%S')}.{fmt}"
    return str(folder / filename)


class CameraRecorder:
    def __init__(self, cam: CameraConfig, rec: RecordingConfig):
        self.cam = cam
        self.rec = rec
        self.logger = setup_logger(cam.name, rec.log_level)
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        self._thread = threading.Thread(target=self._run, name=self.cam.name, daemon=True)
        self._thread.start()

    def stop(self):
        self.logger.info("Deteniendo grabación...")
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=15)

    def _run(self):
        attempt = 0
        while not self._stop_event.is_set():
            attempt += 1
            max_att = self.rec.max_reconnect_attempts
            if max_att > 0 and attempt > max_att:
                self.logger.error("Máximo de intentos (%s) alcanzado. Saliendo.", max_att)
                break

            self.logger.info("Conectando (intento %s) -> %s", attempt, self.cam.url)
            cap = self._open_capture()
            if cap is None or not cap.isOpened():
                self.logger.warning(
                    "No se pudo abrir la cámara. Reintentando en %ss...",
                    self.rec.reconnect_delay_seconds,
                )
                self._wait(self.rec.reconnect_delay_seconds)
                continue

            attempt = 0
            self.logger.info("Conexión establecida. Iniciando grabación.")
            self._record_loop(cap)
            cap.release()

            if not self._stop_event.is_set():
                self.logger.warning(
                    "Conexión perdida. Reintentando en %ss...",
                    self.rec.reconnect_delay_seconds,
                )
                self._wait(self.rec.reconnect_delay_seconds)

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

    def _record_loop(self, cap: cv2.VideoCapture):
        fps = cap.get(cv2.CAP_PROP_FPS) or self.cam.fps
        if fps <= 0:
            fps = self.cam.fps if self.cam.fps > 0 else 15.0

        while not self._stop_event.is_set():
            ok, first_frame = cap.read()
            if not ok or first_frame is None:
                self.logger.warning("No se pudo leer el frame inicial del segmento.")
                break

            segment_start = datetime.now()
            segment_end = next_segment_boundary(segment_start, self.rec.segment_duration_minutes)
            writers = self._create_writers(first_frame, fps, segment_start)
            if not writers:
                self.logger.error("No se pudieron crear los writers del segmento.")
                break

            self.logger.info(
                "Nuevo segmento desde %s hasta %s",
                segment_start.strftime("%Y-%m-%d %H:%M:%S"),
                segment_end.strftime("%Y-%m-%d %H:%M:%S"),
            )

            frames_written = 0
            current_frame = first_frame

            try:
                while not self._stop_event.is_set():
                    frames = self._split_frame(current_frame) if self.cam.split else [current_frame]
                    for i, (path, writer) in enumerate(writers):
                        if i < len(frames):
                            writer.write(frames[i])
                    frames_written += 1

                    if datetime.now() >= segment_end:
                        break

                    ok, next_frame = cap.read()
                    if not ok or next_frame is None:
                        self.logger.warning("Frame no leído; se cerrará el segmento actual.")
                        break
                    current_frame = next_frame
            finally:
                for path, writer in writers:
                    writer.release()
                    self.logger.info("Segmento guardado: %s (%s frames)", path, frames_written)

            if self._stop_event.is_set():
                break

            # Si salimos por fallo de stream, terminamos para que _run reconecte.
            if datetime.now() < segment_end:
                break

    def _create_writers(
        self,
        frame,
        fps: float,
        segment_start: datetime,
    ) -> Optional[List[Tuple[str, cv2.VideoWriter]]]:
        frame_h, frame_w = frame.shape[:2]
        if frame_w <= 0 or frame_h <= 0:
            return None

        fourcc = get_fourcc(self.rec.codec)
        writers: List[Tuple[str, cv2.VideoWriter]] = []

        if self.cam.split:
            split_frames = self._split_frame(frame)
            names = self.cam.split_names[: len(split_frames)]
            for sub_name, sub_frame in zip(names, split_frames):
                sh, sw = sub_frame.shape[:2]
                path = build_output_path(self.rec.output_dir, sub_name, segment_start, self.rec.video_format)
                writer = cv2.VideoWriter(path, fourcc, fps, (sw, sh))
                if not writer.isOpened():
                    self.logger.error("No se pudo abrir VideoWriter para %s", path)
                    for _, prev_writer in writers:
                        prev_writer.release()
                    return None
                writers.append((path, writer))
            return writers

        path = build_output_path(self.rec.output_dir, self.cam.name, segment_start, self.rec.video_format)
        writer = cv2.VideoWriter(path, fourcc, fps, (frame_w, frame_h))
        if not writer.isOpened():
            self.logger.error("No se pudo abrir VideoWriter para %s", path)
            return None
        return [(path, writer)]

    def _split_frame(self, frame) -> List:
        h, w = frame.shape[:2]
        axis = (self.cam.split_axis or "vertical").lower()

        if axis == "horizontal":
            mid = h // 2
            top = frame[:mid, :]
            bottom = frame[mid:, :]
            return [top, bottom]

        mid = w // 2
        left = frame[:, :mid]
        right = frame[:, mid:]
        return [left, right]

    def _wait(self, seconds: float):
        self._stop_event.wait(timeout=seconds)


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
