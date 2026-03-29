#!/usr/bin/env python3
"""
Web Interface — IP Camera Recorder
====================================
Rutas:
  GET  /                          → redirige a /live
  GET  /live                      → vista en vivo (todas las cámaras)
  GET  /stream/<name>             → MJPEG stream de una cámara
  GET  /recordings                → lista de cámaras con grabaciones
  GET  /recordings/<name>         → grabaciones de una cámara
  GET  /download/<name>/<file>    → descarga/reproducción de un archivo
  GET  /api/cameras               → JSON con estado de todas las cámaras
  GET  /api/recordings/<name>     → JSON con archivos de una cámara
"""

import cv2
import yaml
import os
import time
import logging
import threading
from pathlib import Path
from datetime import datetime
from flask import (
    Flask, Response, render_template, redirect,
    url_for, abort, jsonify, send_file, request
)

# ─────────────────────────────────────────────
# Configuración
# ─────────────────────────────────────────────

CONFIG_PATH  = os.environ.get("CONFIG_PATH",  "/config/cameras.yaml")
RECORDINGS_DIR = os.environ.get("RECORDINGS_DIR", "/recordings")
LOG_LEVEL    = os.environ.get("LOG_LEVEL", "INFO")

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("web")

app = Flask(__name__)


# ─────────────────────────────────────────────
# Carga de cámaras desde YAML
# ─────────────────────────────────────────────

def load_cameras():
    """Carga la lista de cámaras habilitadas desde el archivo de configuración."""
    try:
        with open(CONFIG_PATH, "r") as f:
            raw = yaml.safe_load(f)
    except FileNotFoundError:
        logger.error(f"Config no encontrada: {CONFIG_PATH}")
        return []

    cameras = []
    for cam in raw.get("cameras", []):
        if not cam.get("enabled", True):
            continue

        entry = {
            "name": cam["name"],
            "url":  cam["url"],
            "split": cam.get("split", False),
            "split_names": cam.get("split_names", [cam["name"] + "_L", cam["name"] + "_R"]),
            "fps":   cam.get("fps", 15),
        }
        cameras.append(entry)
    return cameras


CAMERAS = load_cameras()
logger.info(f"Cámaras cargadas: {[c['name'] for c in CAMERAS]}")


# ─────────────────────────────────────────────
# Gestor de streams MJPEG
# ─────────────────────────────────────────────

class StreamManager:
    """
    Mantiene una captura de OpenCV por cámara.
    Múltiples clientes web comparten el mismo frame (no se re-abre la cámara por cliente).
    """

    def __init__(self):
        self._caps: dict   = {}          # name → VideoCapture
        self._frames: dict = {}          # name → bytes JPEG
        self._locks: dict  = {}          # name → Lock
        self._threads: dict = {}         # name → Thread
        self._running: dict = {}         # name → bool
        self._status: dict  = {}         # name → "ok" | "error" | "connecting"

    def _init_camera(self, name: str, url: str):
        self._locks[name]   = threading.Lock()
        self._frames[name]  = None
        self._running[name] = True
        self._status[name]  = "connecting"
        t = threading.Thread(target=self._capture_loop, args=(name, url), daemon=True)
        self._threads[name] = t
        t.start()

    def get_or_init(self, name: str, url: str):
        if name not in self._threads or not self._threads[name].is_alive():
            self._init_camera(name, url)

    def _capture_loop(self, name: str, url: str):
        """Hilo dedicado: captura frames continuamente."""
        logger.info(f"[{name}] Abriendo stream para vista en vivo...")
        while self._running.get(name, False):
            cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

            if not cap.isOpened():
                logger.warning(f"[{name}] No se pudo conectar. Reintentando en 5s...")
                self._status[name] = "error"
                time.sleep(5)
                continue

            self._status[name] = "ok"
            logger.info(f"[{name}] Stream de vista en vivo conectado.")

            while self._running.get(name, False):
                ok, frame = cap.read()
                if not ok:
                    logger.warning(f"[{name}] Frame perdido, reconectando...")
                    self._status[name] = "error"
                    break

                # Reducir resolución para el streaming web (máx 720p)
                h, w = frame.shape[:2]
                max_w = 1280
                if w > max_w:
                    scale = max_w / w
                    frame = cv2.resize(frame, (max_w, int(h * scale)))

                ok2, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
                if ok2:
                    with self._locks[name]:
                        self._frames[name] = buf.tobytes()

            cap.release()
            if self._running.get(name, False):
                time.sleep(3)  # pausa antes de reconectar

    def get_frame(self, name: str):
        with self._locks.get(name, threading.Lock()):
            return self._frames.get(name)

    def status(self, name: str) -> str:
        return self._status.get(name, "unknown")

    def stop(self, name: str):
        self._running[name] = False


stream_mgr = StreamManager()

# Pre-inicializar todos los streams al arrancar
for cam in CAMERAS:
    stream_mgr.get_or_init(cam["name"], cam["url"])


# ─────────────────────────────────────────────
# Helpers de grabaciones
# ─────────────────────────────────────────────

def _human_size(size_bytes: int) -> str:
    for unit in ["B", "KB", "MB", "GB"]:
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


def get_camera_recordings(cam_name: str) -> list:
    """Devuelve lista de archivos de grabación de una cámara, ordenados por fecha desc."""
    folder = Path(RECORDINGS_DIR) / cam_name
    if not folder.exists():
        return []

    files = []
    for f in sorted(folder.iterdir(), reverse=True):
        if f.suffix.lower() in (".mp4", ".mkv", ".avi"):
            stat = f.stat()
            # Intentar parsear fecha del nombre de archivo
            try:
                # Formato esperado: nombre_YYYYMMDD_HHMMSS.ext
                parts = f.stem.rsplit("_", 2)
                dt = datetime.strptime(f"{parts[-2]}_{parts[-1]}", "%Y%m%d_%H%M%S")
                date_str = dt.strftime("%d/%m/%Y %H:%M:%S")
            except Exception:
                date_str = datetime.fromtimestamp(stat.st_mtime).strftime("%d/%m/%Y %H:%M:%S")

            files.append({
                "name":     f.name,
                "date":     date_str,
                "size":     _human_size(stat.st_size),
                "size_raw": stat.st_size,
                "mtime":    stat.st_mtime,
            })
    return files


def get_all_recordings_summary() -> list:
    """Resumen de grabaciones por cámara."""
    summary = []
    rec_root = Path(RECORDINGS_DIR)
    if not rec_root.exists():
        return summary

    for folder in sorted(rec_root.iterdir()):
        if folder.is_dir():
            files = list(folder.glob("*.mp4")) + list(folder.glob("*.mkv")) + list(folder.glob("*.avi"))
            total = sum(f.stat().st_size for f in files)
            summary.append({
                "name":   folder.name,
                "count":  len(files),
                "total":  _human_size(total),
                "latest": max((f.stat().st_mtime for f in files), default=0),
            })
    return summary


# ─────────────────────────────────────────────
# Rutas
# ─────────────────────────────────────────────

@app.route("/")
def index():
    return redirect(url_for("live"))


@app.route("/live")
def live():
    # Para cámaras con split: mostramos las dos sub-cámaras como streams separados
    # La cámara física tiene un stream, pero creamos entradas visuales por cada mitad
    display_cams = []
    for cam in CAMERAS:
        if cam["split"]:
            for i, sname in enumerate(cam["split_names"][:2]):
                display_cams.append({
                    "name":        sname,
                    "source_name": cam["name"],   # nombre del stream real
                    "split_side":  i,             # 0=izquierda, 1=derecha
                    "is_split":    True,
                })
        else:
            display_cams.append({
                "name":        cam["name"],
                "source_name": cam["name"],
                "split_side":  None,
                "is_split":    False,
            })
    return render_template("live.html", cameras=display_cams)


@app.route("/stream/<name>")
def stream(name):
    """
    Stream MJPEG para una cámara. Si la cámara tiene split,
    el parámetro ?side=0 o ?side=1 recorta el frame.
    """
    # Buscar la cámara en config
    cam_cfg = next((c for c in CAMERAS if c["name"] == name), None)
    if cam_cfg is None:
        abort(404)

    split_side = request.args.get("side", type=int, default=None)
    stream_mgr.get_or_init(name, cam_cfg["url"])

    def generate():
        placeholder = _placeholder_jpeg()
        while True:
            frame_bytes = stream_mgr.get_frame(name)
            if frame_bytes is None:
                frame_bytes = placeholder
            else:
                # Recortar si es split
                if split_side is not None:
                    frame_bytes = _crop_half(frame_bytes, split_side)

            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n\r\n"
                + frame_bytes +
                b"\r\n"
            )
            time.sleep(1 / 15)  # ~15 fps al navegador

    return Response(
        generate(),
        mimetype="multipart/x-mixed-replace; boundary=frame"
    )


def _crop_half(jpeg_bytes: bytes, side: int) -> bytes:
    """Recorta el JPEG a la mitad izquierda (0) o derecha (1)."""
    try:
        import numpy as np
        buf = np.frombuffer(jpeg_bytes, dtype=np.uint8)
        frame = cv2.imdecode(buf, cv2.IMREAD_COLOR)
        if frame is None:
            return jpeg_bytes
        h, w = frame.shape[:2]
        mid = w // 2
        half = frame[:, :mid] if side == 0 else frame[:, mid:]
        _, enc = cv2.imencode(".jpg", half, [cv2.IMWRITE_JPEG_QUALITY, 75])
        return enc.tobytes()
    except Exception:
        return jpeg_bytes


def _placeholder_jpeg() -> bytes:
    """Genera un JPEG negro con texto 'Conectando...'"""
    import numpy as np
    img = np.zeros((360, 640, 3), dtype=np.uint8)
    cv2.putText(img, "Conectando...", (200, 180),
                cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 200, 0), 2)
    _, enc = cv2.imencode(".jpg", img)
    return enc.tobytes()


# ── Grabaciones ───────────────────────────────

@app.route("/recordings")
def recordings():
    summary = get_all_recordings_summary()
    return render_template("recordings.html", summary=summary, selected=None, files=None)


@app.route("/recordings/<cam_name>")
def recordings_cam(cam_name):
    summary = get_all_recordings_summary()
    files   = get_camera_recordings(cam_name)
    return render_template("recordings.html", summary=summary, selected=cam_name, files=files)


@app.route("/download/<cam_name>/<filename>")
def download_file(cam_name, filename):
    """Sirve el archivo de video para reproducción o descarga."""
    filepath = Path(RECORDINGS_DIR) / cam_name / filename
    if not filepath.exists():
        abort(404)
    # inline para reproducción en el navegador
    as_attachment = request.args.get("dl", "0") == "1"
    return send_file(str(filepath), as_attachment=as_attachment,
                     download_name=filename, mimetype="video/mp4")


# ── API JSON ──────────────────────────────────

@app.route("/api/cameras")
def api_cameras():
    result = []
    for cam in CAMERAS:
        result.append({
            "name":   cam["name"],
            "status": stream_mgr.status(cam["name"]),
            "split":  cam["split"],
            "split_names": cam["split_names"] if cam["split"] else [],
        })
    return jsonify(result)


@app.route("/api/recordings/<cam_name>")
def api_recordings(cam_name):
    return jsonify(get_camera_recordings(cam_name))


# ─────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("WEB_PORT", 8080))
    logger.info(f"Iniciando servidor web en http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, threaded=True)
