#!/usr/bin/env python3
"""
Interfaz web para cámaras en vivo y grabaciones.

Mejoras principales:
- lazy loading: no abre RTSP hasta que un cliente lo solicita
- cierre automático de streams inactivos
- cachea JPEG completo y mitades split para evitar decodificar/re-encodificar por cliente
- cache TTL para el listado de grabaciones
- endpoint /health útil para healthcheck
"""

from __future__ import annotations

import logging
import mimetypes
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

import cv2
import numpy as np
import yaml
from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

os.environ.setdefault(
    "OPENCV_FFMPEG_CAPTURE_OPTIONS",
    "rtsp_transport;tcp|stimeout;5000000",
)

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/config/cameras.yaml")
RECORDINGS_DIR = os.environ.get("RECORDINGS_DIR", "/recordings")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
LIVE_JPEG_QUALITY = int(os.environ.get("LIVE_JPEG_QUALITY", "80"))
LIVE_MAX_WIDTH = int(os.environ.get("LIVE_MAX_WIDTH", "1280"))
LIVE_IDLE_SECONDS = int(os.environ.get("LIVE_IDLE_SECONDS", "30"))
RECORDINGS_CACHE_TTL = int(os.environ.get("RECORDINGS_CACHE_TTL", "10"))

ENV_VAR_PATTERN = re.compile(r"\$\{([^}:]+)(?::-(.*?))?\}")
ALLOWED_VIDEO_EXTS = {".mp4", ".mkv", ".avi"}

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("web")
app = Flask(__name__)


def substitute_env_vars(text: str) -> str:
    def repl(match):
        var_name = match.group(1)
        default = match.group(2) or ""
        return os.environ.get(var_name, default)

    return ENV_VAR_PATTERN.sub(repl, text)


def load_cameras():
    try:
        raw_text = Path(CONFIG_PATH).read_text(encoding="utf-8")
        raw = yaml.safe_load(substitute_env_vars(raw_text)) or {}
    except FileNotFoundError:
        logger.error("Config no encontrada: %s", CONFIG_PATH)
        return []

    cameras = []
    for cam in raw.get("cameras", []):
        if not cam.get("enabled", True):
            continue

        cameras.append(
            {
                "name": cam["name"],
                "url": cam["url"],
                "split": bool(cam.get("split", False)),
                "split_names": cam.get(
                    "split_names",
                    [f"{cam['name']}_izquierda", f"{cam['name']}_derecha"],
                ),
                "split_axis": str(cam.get("split_axis", "vertical")).lower(),
                "fps": float(cam.get("fps", 15)),
            }
        )
    return cameras


CAMERAS = load_cameras()
CAMERAS_BY_NAME = {c["name"]: c for c in CAMERAS}
logger.info("Cámaras cargadas: %s", [c["name"] for c in CAMERAS])


def build_live_display_list():
    display_cams = []
    for cam in CAMERAS:
        if cam["split"]:
            is_vertical = cam.get("split_axis", "vertical") != "horizontal"
            labels = ["IZQ", "DER"] if is_vertical else ["TOP", "BOT"]
            for i, sname in enumerate(cam["split_names"][:2]):
                display_cams.append(
                    {
                        "name": sname,
                        "source_name": cam["name"],
                        "split_side": i,
                        "is_split": True,
                        "split_axis": cam.get("split_axis", "vertical"),
                        "split_label": labels[i],
                    }
                )
        else:
            display_cams.append(
                {
                    "name": cam["name"],
                    "source_name": cam["name"],
                    "split_side": None,
                    "is_split": False,
                    "split_axis": None,
                    "split_label": None,
                }
            )
    return display_cams


class StreamManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._frames: Dict[str, Optional[bytes]] = {}
        self._split_frames: Dict[str, Dict[int, bytes]] = {}
        self._threads: Dict[str, threading.Thread] = {}
        self._stop_flags: Dict[str, threading.Event] = {}
        self._clients: Dict[str, int] = {}
        self._status: Dict[str, str] = {}
        self._last_frame_ts: Dict[str, float] = {}
        self._last_access_ts: Dict[str, float] = {}
        self._reaper_thread = threading.Thread(target=self._reaper_loop, daemon=True)
        self._reaper_thread.start()

    def acquire(self, name: str, url: str, split_axis: str = "vertical"):
        with self._lock:
            self._clients[name] = self._clients.get(name, 0) + 1
            self._last_access_ts[name] = time.time()
            if name not in self._threads or not self._threads[name].is_alive():
                stop_flag = threading.Event()
                self._stop_flags[name] = stop_flag
                self._frames[name] = None
                self._split_frames[name] = {}
                self._status[name] = "connecting"
                thread = threading.Thread(
                    target=self._capture_loop,
                    args=(name, url, split_axis, stop_flag),
                    daemon=True,
                    name=f"web-{name}",
                )
                self._threads[name] = thread
                thread.start()

    def release(self, name: str):
        with self._lock:
            self._clients[name] = max(0, self._clients.get(name, 0) - 1)
            self._last_access_ts[name] = time.time()

    def touch(self, name: str):
        with self._lock:
            self._last_access_ts[name] = time.time()

    def get_frame(self, name: str, side: Optional[int] = None):
        with self._lock:
            self._last_access_ts[name] = time.time()
            if side is None:
                return self._frames.get(name)
            return self._split_frames.get(name, {}).get(side) or self._frames.get(name)

    def status(self, name: str):
        with self._lock:
            age = time.time() - self._last_frame_ts.get(name, 0)
            return {
                "status": self._status.get(name, "connecting"),
                "clients": self._clients.get(name, 0),
                "last_frame_age_seconds": round(age, 2) if self._last_frame_ts.get(name) else None,
            }

    def _capture_loop(self, name: str, url: str, split_axis: str, stop_flag: threading.Event):
        logger.info("[%s] Abriendo stream para vista en vivo...", name)
        while not stop_flag.is_set():
            cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

            if not cap.isOpened():
                with self._lock:
                    self._status[name] = "error"
                logger.warning("[%s] No se pudo conectar. Reintentando en 5s...", name)
                stop_flag.wait(5)
                continue

            with self._lock:
                self._status[name] = "ok"
            logger.info("[%s] Stream de vista en vivo conectado.", name)

            while not stop_flag.is_set():
                ok, frame = cap.read()
                if not ok or frame is None:
                    logger.warning("[%s] Frame perdido, reconectando...", name)
                    with self._lock:
                        self._status[name] = "error"
                    break

                frame = self._prepare_frame(frame)
                full_jpeg, split_map = self._encode_frame_variants(frame, split_axis)
                with self._lock:
                    self._frames[name] = full_jpeg
                    self._split_frames[name] = split_map
                    self._last_frame_ts[name] = time.time()
                    self._status[name] = "ok"

            cap.release()
            if not stop_flag.is_set():
                stop_flag.wait(2)

        with self._lock:
            self._status[name] = "stopped"
        logger.info("[%s] Stream de vista en vivo detenido.", name)

    def _prepare_frame(self, frame):
        h, w = frame.shape[:2]
        if LIVE_MAX_WIDTH > 0 and w > LIVE_MAX_WIDTH:
            scale = LIVE_MAX_WIDTH / w
            frame = cv2.resize(frame, (LIVE_MAX_WIDTH, int(h * scale)))
        return frame

    def _encode_frame_variants(self, frame, split_axis: str):
        ok, full_enc = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, LIVE_JPEG_QUALITY])
        full_jpeg = full_enc.tobytes() if ok else b""

        h, w = frame.shape[:2]
        split_axis = (split_axis or "vertical").lower()
        if split_axis == "horizontal":
            mid = h // 2
            first = frame[:mid, :]
            second = frame[mid:, :]
        else:
            mid = w // 2
            first = frame[:, :mid]
            second = frame[:, mid:]

        split_map = {}
        for idx, half in enumerate((first, second)):
            ok_half, half_enc = cv2.imencode(".jpg", half, [cv2.IMWRITE_JPEG_QUALITY, LIVE_JPEG_QUALITY])
            split_map[idx] = half_enc.tobytes() if ok_half else full_jpeg
        return full_jpeg, split_map

    def _reaper_loop(self):
        while True:
            now = time.time()
            to_stop = []
            with self._lock:
                for name, thread in list(self._threads.items()):
                    if not thread.is_alive():
                        continue
                    clients = self._clients.get(name, 0)
                    last_access = self._last_access_ts.get(name, 0)
                    if clients <= 0 and (now - last_access) > LIVE_IDLE_SECONDS:
                        to_stop.append(name)
            for name in to_stop:
                logger.info("[%s] Cerrando stream inactivo de la web.", name)
                stop_flag = self._stop_flags.get(name)
                if stop_flag:
                    stop_flag.set()
            time.sleep(5)


stream_mgr = StreamManager()


class TTLCache:
    def __init__(self, ttl_seconds: int):
        self.ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self._data = {}

    def get(self, key):
        with self._lock:
            item = self._data.get(key)
            if not item:
                return None
            expires_at, value = item
            if time.time() > expires_at:
                self._data.pop(key, None)
                return None
            return value

    def set(self, key, value):
        with self._lock:
            self._data[key] = (time.time() + self.ttl_seconds, value)

    def invalidate_prefix(self, prefix: str = ""):
        with self._lock:
            for key in list(self._data.keys()):
                if str(key).startswith(prefix):
                    self._data.pop(key, None)


recordings_cache = TTLCache(RECORDINGS_CACHE_TTL)


def _human_size(size_bytes: int) -> str:
    for unit in ["B", "KB", "MB", "GB"]:
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


def _parse_recording_datetime(path: Path, fallback_mtime: float) -> str:
    try:
        parts = path.stem.rsplit("_", 2)
        dt = datetime.strptime(f"{parts[-2]}_{parts[-1]}", "%Y%m%d_%H%M%S")
        return dt.strftime("%d/%m/%Y %H:%M:%S")
    except Exception:
        return datetime.fromtimestamp(fallback_mtime).strftime("%d/%m/%Y %H:%M:%S")


def get_camera_recordings(cam_name: str) -> list:
    cache_key = f"cam:{cam_name}"
    cached = recordings_cache.get(cache_key)
    if cached is not None:
        return cached

    folder = Path(RECORDINGS_DIR) / cam_name
    if not folder.exists():
        recordings_cache.set(cache_key, [])
        return []

    files = []
    for f in sorted(folder.rglob("*"), key=lambda p: p.stat().st_mtime, reverse=True):
        if not f.is_file() or f.suffix.lower() not in ALLOWED_VIDEO_EXTS:
            continue

        stat = f.stat()
        rel_path = f.relative_to(folder).as_posix()
        files.append(
            {
                "name": f.name,
                "relative_path": rel_path,
                "folder": f.parent.relative_to(folder).as_posix(),
                "date": _parse_recording_datetime(f, stat.st_mtime),
                "size": _human_size(stat.st_size),
                "size_raw": stat.st_size,
                "mtime": stat.st_mtime,
                "ext": f.suffix.lower().lstrip("."),
            }
        )

    recordings_cache.set(cache_key, files)
    return files


def get_all_recordings_summary() -> list:
    cache_key = "summary"
    cached = recordings_cache.get(cache_key)
    if cached is not None:
        return cached

    summary = []
    rec_root = Path(RECORDINGS_DIR)
    if not rec_root.exists():
        recordings_cache.set(cache_key, summary)
        return summary

    for folder in sorted(rec_root.iterdir()):
        if not folder.is_dir():
            continue
        files = [p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in ALLOWED_VIDEO_EXTS]
        total = sum(f.stat().st_size for f in files)
        summary.append(
            {
                "name": folder.name,
                "count": len(files),
                "total": _human_size(total),
                "latest": max((f.stat().st_mtime for f in files), default=0),
            }
        )
    recordings_cache.set(cache_key, summary)
    return summary


@app.route("/")
def index():
    return redirect(url_for("live"))


@app.route("/health")
def health():
    return jsonify(
        {
            "ok": True,
            "recordings_dir": RECORDINGS_DIR,
            "camera_count": len(CAMERAS),
            "time": datetime.now().isoformat(),
        }
    )


@app.route("/live")
def live():
    return render_template("live.html", cameras=build_live_display_list())


@app.route("/stream/<name>")
def stream(name):
    cam_cfg = CAMERAS_BY_NAME.get(name)
    if cam_cfg is None:
        abort(404)

    split_side = request.args.get("side", type=int, default=None)
    split_axis = request.args.get("axis", default=cam_cfg.get("split_axis", "vertical"))
    target_fps = max(1, int(cam_cfg.get("fps", 15)))
    stream_mgr.acquire(name, cam_cfg["url"], split_axis)

    def generate():
        placeholder = _placeholder_jpeg()
        try:
            while True:
                frame_bytes = stream_mgr.get_frame(name, split_side)
                if frame_bytes is None:
                    frame_bytes = placeholder

                yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame_bytes + b"\r\n"
                time.sleep(1 / target_fps)
        except GeneratorExit:
            logger.info("[%s] Cliente de stream desconectado.", name)
        finally:
            stream_mgr.release(name)

    return Response(generate(), mimetype="multipart/x-mixed-replace; boundary=frame")


def _placeholder_jpeg() -> bytes:
    img = np.zeros((360, 640, 3), dtype=np.uint8)
    cv2.putText(img, "Conectando...", (190, 180), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 200, 0), 2)
    _, enc = cv2.imencode(".jpg", img)
    return enc.tobytes()


@app.route("/recordings")
def recordings():
    summary = get_all_recordings_summary()
    return render_template("recordings.html", summary=summary, selected=None, files=None)


@app.route("/recordings/<cam_name>")
def recordings_cam(cam_name):
    summary = get_all_recordings_summary()
    files = get_camera_recordings(cam_name)
    return render_template("recordings.html", summary=summary, selected=cam_name, files=files)


@app.route("/download/<cam_name>/<path:relative_path>")
def download_file(cam_name, relative_path):
    base_dir = (Path(RECORDINGS_DIR) / cam_name).resolve()
    filepath = (base_dir / relative_path).resolve()
    if base_dir not in filepath.parents and filepath != base_dir:
        abort(403)
    if not filepath.exists() or not filepath.is_file():
        abort(404)

    as_attachment = request.args.get("dl", "0") == "1"
    mime_type, _ = mimetypes.guess_type(str(filepath))
    return send_file(
        str(filepath),
        as_attachment=as_attachment,
        download_name=filepath.name,
        mimetype=mime_type or "application/octet-stream",
        conditional=True,
        max_age=3600,
    )


@app.route("/api/cameras")
def api_cameras():
    result = []
    for cam in CAMERAS:
        status = stream_mgr.status(cam["name"])
        result.append(
            {
                "name": cam["name"],
                "status": status["status"],
                "clients": status["clients"],
                "last_frame_age_seconds": status["last_frame_age_seconds"],
                "split": cam["split"],
                "split_names": cam["split_names"] if cam["split"] else [],
                "split_axis": cam.get("split_axis", "vertical"),
            }
        )
    return jsonify(result)


@app.route("/api/recordings/<cam_name>")
def api_recordings(cam_name):
    return jsonify(get_camera_recordings(cam_name))


if __name__ == "__main__":
    port = int(os.environ.get("WEB_PORT", 8080))
    logger.info("Iniciando servidor web en http://0.0.0.0:%s", port)
    app.run(host="0.0.0.0", port=port, threaded=True)
