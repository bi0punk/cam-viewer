#!/usr/bin/env python3
"""
IP Camera Recorder
==================
Graba múltiples cámaras IP en segmentos de 1 hora.
Soporta división vertical de imagen para cámaras de doble lente.
"""

import cv2
import yaml
import os
import time
import logging
import signal
import sys
import threading
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional, List, Tuple


# ─────────────────────────────────────────────
# Modelos de datos
# ─────────────────────────────────────────────

@dataclass
class CameraConfig:
    name: str
    url: str
    enabled: bool = True
    split: bool = False
    split_names: List[str] = field(default_factory=lambda: ["left", "right"])
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


# ─────────────────────────────────────────────
# Carga de configuración
# ─────────────────────────────────────────────

def load_config(config_path: str) -> Tuple[List[CameraConfig], RecordingConfig]:
    """Carga y valida el archivo YAML de configuración."""
    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    rec_raw = raw.get("recording", {})
    rec_cfg = RecordingConfig(
        segment_duration_minutes=rec_raw.get("segment_duration_minutes", 60),
        output_dir=rec_raw.get("output_dir", "/recordings"),
        video_format=rec_raw.get("video_format", "mp4"),
        codec=rec_raw.get("codec", "mp4v"),
        reconnect_delay_seconds=rec_raw.get("reconnect_delay_seconds", 10),
        max_reconnect_attempts=rec_raw.get("max_reconnect_attempts", 0),
        log_level=rec_raw.get("log_level", "INFO"),
    )

    cameras = []
    for cam in raw.get("cameras", []):
        cameras.append(CameraConfig(
            name=cam["name"],
            url=cam["url"],
            enabled=cam.get("enabled", True),
            split=cam.get("split", False),
            split_names=cam.get("split_names", [cam["name"] + "_L", cam["name"] + "_R"]),
            fps=cam.get("fps", 15.0),
            width=cam.get("width", 0),
            height=cam.get("height", 0),
        ))

    return cameras, rec_cfg


# ─────────────────────────────────────────────
# Utilidades
# ─────────────────────────────────────────────

def get_fourcc(codec: str) -> int:
    return cv2.VideoWriter_fourcc(*codec)


def build_output_path(output_dir: str, name: str, fmt: str) -> str:
    """Construye la ruta del archivo de salida con timestamp."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    folder = Path(output_dir) / name
    folder.mkdir(parents=True, exist_ok=True)
    return str(folder / f"{name}_{ts}.{fmt}")


def setup_logger(name: str, log_level: str, log_dir: str = "/logs") -> logging.Logger:
    """Configura un logger con salida a consola y archivo."""
    level = getattr(logging, log_level.upper(), logging.INFO)
    logger = logging.getLogger(name)
    logger.setLevel(level)

    if logger.handlers:
        return logger

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Consola
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(level)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    # Archivo
    try:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(f"{log_dir}/{name}.log")
        fh.setLevel(level)
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except Exception:
        pass

    return logger


# ─────────────────────────────────────────────
# Grabador de una sola cámara
# ─────────────────────────────────────────────

class CameraRecorder:
    """
    Graba una cámara IP en segmentos.
    Si split=True, corta cada frame al 50% vertical y graba dos archivos.
    """

    def __init__(self, cam: CameraConfig, rec: RecordingConfig):
        self.cam = cam
        self.rec = rec
        self.logger = setup_logger(cam.name, rec.log_level)
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ── Control ──────────────────────────────

    def start(self):
        self._thread = threading.Thread(target=self._run, name=self.cam.name, daemon=True)
        self._thread.start()

    def stop(self):
        self.logger.info("Deteniendo grabación...")
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=15)

    # ── Bucle principal ──────────────────────

    def _run(self):
        attempt = 0
        while not self._stop_event.is_set():
            attempt += 1
            max_att = self.rec.max_reconnect_attempts
            if max_att > 0 and attempt > max_att:
                self.logger.error(f"Máximo de intentos ({max_att}) alcanzado. Saliendo.")
                break

            self.logger.info(f"Conectando (intento {attempt})... → {self.cam.url}")
            cap = self._open_capture()
            if cap is None or not cap.isOpened():
                self.logger.warning(f"No se pudo abrir la cámara. Reintentando en {self.rec.reconnect_delay_seconds}s...")
                self._wait(self.rec.reconnect_delay_seconds)
                continue

            attempt = 0  # reset al conectar con éxito
            self.logger.info("Conexión establecida. Iniciando grabación.")
            self._record_loop(cap)
            cap.release()

            if not self._stop_event.is_set():
                self.logger.warning(f"Conexión perdida. Reintentando en {self.rec.reconnect_delay_seconds}s...")
                self._wait(self.rec.reconnect_delay_seconds)

    def _open_capture(self) -> Optional[cv2.VideoCapture]:
        cap = cv2.VideoCapture(self.cam.url, cv2.CAP_FFMPEG)
        if self.cam.width > 0:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.cam.width)
        if self.cam.height > 0:
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.cam.height)
        cap.set(cv2.CAP_PROP_FPS, self.cam.fps)
        # Buffer mínimo para reducir latencia
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        return cap

    def _record_loop(self, cap: cv2.VideoCapture):
        """Graba segmentos de N minutos en bucle."""
        segment_seconds = self.rec.segment_duration_minutes * 60
        fps = cap.get(cv2.CAP_PROP_FPS) or self.cam.fps
        if fps <= 0:
            fps = self.cam.fps

        while not self._stop_event.is_set():
            writers = self._create_writers(cap, fps)
            if writers is None:
                break

            seg_start = time.time()
            frames_written = 0
            self.logger.info(f"Nuevo segmento: {[w[0] for w in writers]}")

            while not self._stop_event.is_set():
                ok, frame = cap.read()
                if not ok:
                    self.logger.warning("Frame no leído — posible corte de stream.")
                    break

                frames = self._split_frame(frame) if self.cam.split else [frame]

                for i, (path, writer) in enumerate(writers):
                    if i < len(frames):
                        writer.write(frames[i])

                frames_written += 1

                # ¿Terminó el segmento de 1 hora?
                if time.time() - seg_start >= segment_seconds:
                    break

            # Cerrar escritores del segmento
            for path, writer in writers:
                writer.release()
                self.logger.info(f"Segmento guardado: {path} ({frames_written} frames)")

    # ── VideoWriters ─────────────────────────

    def _create_writers(
        self, cap: cv2.VideoCapture, fps: float
    ) -> Optional[List[Tuple[str, cv2.VideoWriter]]]:
        """Crea uno o dos VideoWriters según configuración de split."""
        frame_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        frame_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

        if frame_w == 0 or frame_h == 0:
            self.logger.error("No se pudo obtener resolución del stream.")
            return None

        fourcc = get_fourcc(self.rec.codec)

        if self.cam.split:
            half_w = frame_w // 2
            names = self.cam.split_names
            writers = []
            for i, sub_name in enumerate(names[:2]):
                path = build_output_path(self.rec.output_dir, sub_name, self.rec.video_format)
                w = cv2.VideoWriter(path, fourcc, fps, (half_w, frame_h))
                writers.append((path, w))
            return writers
        else:
            path = build_output_path(self.rec.output_dir, self.cam.name, self.rec.video_format)
            w = cv2.VideoWriter(path, fourcc, fps, (frame_w, frame_h))
            return [(path, w)]

    # ── División de frame ────────────────────

    @staticmethod
    def _split_frame(frame) -> List:
        """Divide el frame exactamente a la mitad en vertical (eje X)."""
        h, w = frame.shape[:2]
        mid = w // 2
        left = frame[:, :mid]
        right = frame[:, mid:]
        return [left, right]

    # ── Helper ───────────────────────────────

    def _wait(self, seconds: float):
        """Espera interrumpible."""
        self._stop_event.wait(timeout=seconds)


# ─────────────────────────────────────────────
# Orquestador principal
# ─────────────────────────────────────────────

class RecorderOrchestrator:
    def __init__(self, config_path: str):
        cameras, self.rec = load_config(config_path)
        self.logger = setup_logger("orchestrator", self.rec.log_level)
        self.recorders: List[CameraRecorder] = []

        for cam in cameras:
            if cam.enabled:
                self.recorders.append(CameraRecorder(cam, self.rec))
            else:
                self.logger.info(f"Cámara desactivada, omitiendo: {cam.name}")

    def start_all(self):
        self.logger.info(f"Iniciando {len(self.recorders)} grabador(es)...")
        for r in self.recorders:
            r.start()
        self.logger.info("Todos los grabadores iniciados.")

    def stop_all(self):
        self.logger.info("Señal de parada recibida. Cerrando grabadores...")
        for r in self.recorders:
            r.stop()
        self.logger.info("Todos los grabadores detenidos.")

    def wait(self):
        """Bloquea el hilo principal hasta señal de salida."""
        try:
            while True:
                time.sleep(5)
                # Heartbeat: log cuántos hilos siguen vivos
                alive = [r.cam.name for r in self.recorders if r._thread and r._thread.is_alive()]
                self.logger.debug(f"Grabadores activos: {alive}")
        except KeyboardInterrupt:
            pass


# ─────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────

def main():
    config_path = os.environ.get("CONFIG_PATH", "/config/cameras.yaml")

    if not Path(config_path).exists():
        print(f"ERROR: Archivo de configuración no encontrado: {config_path}", file=sys.stderr)
        sys.exit(1)

    orchestrator = RecorderOrchestrator(config_path)

    # Manejo de señales del SO para parada limpia
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
