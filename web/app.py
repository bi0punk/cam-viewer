#!/usr/bin/env python3
"""
Interfaz web para cámaras en vivo y grabaciones.

Esta versión NO abre RTSP desde la web. Consume previews JPEG publicados por
el grabador en un directorio compartido, eliminando la doble conexión a cámara.
"""

from __future__ import annotations

import json
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

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/config/cameras.yaml")
RECORDINGS_DIR = os.environ.get("RECORDINGS_DIR", "/recordings")
LIVE_CACHE_DIR = os.environ.get("LIVE_CACHE_DIR", "/live_cache")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
RECORDINGS_CACHE_TTL = int(os.environ.get("RECORDINGS_CACHE_TTL", "10"))
STATUS_CACHE_TTL = float(os.environ.get("STATUS_CACHE_TTL", "1.0"))

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
                "split": bool(cam.get("split", False)),
                "split_names": cam.get(
                    "split_names",
                    [f"{cam['name']}_izquierda", f"{cam['name']}_derecha"],
                ),
                "split_axis": str(cam.get("split_axis", "vertical")).lower(),
                "fps": float(cam.get("preview_fps", cam.get("fps", 5))),
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


class SnapshotCache:
    def __init__(self, live_cache_dir: str, status_cache_ttl: float = 1.0):
        self.live_cache_dir = Path(live_cache_dir)
        self.status_cache_ttl = status_cache_ttl
        self._lock = threading.Lock()
        self._file_cache: Dict[str, dict] = {}
        self._status_cache: Dict[str, dict] = {}

    def get_frame(self, name: str, side: Optional[int] = None) -> Optional[bytes]:
        filename = f"side_{side}.jpg" if side is not None else "full.jpg"
        path = self.live_cache_dir / name / filename
        return self._read_bytes(path)

    def get_status(self, name: str) -> dict:
        path = self.live_cache_dir / name / "status.json"
        cache_key = str(path)
        now = time.time()
        with self._lock:
            cached = self._status_cache.get(cache_key)
            if cached and now < cached["expires_at"]:
                return dict(cached["data"])

        if not path.exists():
            return {"status": "connecting", "message": "esperando preview", "last_frame_at": None}

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {"status": "error", "message": "status.json inválido", "last_frame_at": None}

        with self._lock:
            self._status_cache[cache_key] = {
                "expires_at": now + self.status_cache_ttl,
                "data": data,
            }
        return dict(data)

    def _read_bytes(self, path: Path) -> Optional[bytes]:
        cache_key = str(path)
        try:
            stat = path.stat()
        except FileNotFoundError:
            return None

        with self._lock:
            cached = self._file_cache.get(cache_key)
            if cached and cached["mtime_ns"] == stat.st_mtime_ns and cached["size"] == stat.st_size:
                return cached["data"]

        data = path.read_bytes()
        with self._lock:
            self._file_cache[cache_key] = {
                "mtime_ns": stat.st_mtime_ns,
                "size": stat.st_size,
                "data": data,
            }
        return data


class ClientRegistry:
    def __init__(self):
        self._lock = threading.Lock()
        self._clients: Dict[str, int] = {}

    def acquire(self, name: str):
        with self._lock:
            self._clients[name] = self._clients.get(name, 0) + 1

    def release(self, name: str):
        with self._lock:
            self._clients[name] = max(0, self._clients.get(name, 0) - 1)

    def get(self, name: str) -> int:
        with self._lock:
            return self._clients.get(name, 0)


snapshot_cache = SnapshotCache(LIVE_CACHE_DIR, STATUS_CACHE_TTL)
client_registry = ClientRegistry()


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
            "live_cache_dir": LIVE_CACHE_DIR,
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
    target_fps = max(1, int(cam_cfg.get("fps", 5)))
    client_registry.acquire(name)

    def generate():
        placeholder = _placeholder_jpeg()
        try:
            while True:
                frame_bytes = snapshot_cache.get_frame(name, split_side)
                if frame_bytes is None:
                    frame_bytes = placeholder
                yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame_bytes + b"\r\n"
                time.sleep(1 / target_fps)
        except GeneratorExit:
            logger.info("[%s] Cliente de stream desconectado.", name)
        finally:
            client_registry.release(name)

    return Response(generate(), mimetype="multipart/x-mixed-replace; boundary=frame")


def _placeholder_jpeg() -> bytes:
    img = np.zeros((360, 640, 3), dtype=np.uint8)
    cv2.putText(img, "Esperando preview...", (130, 180), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 200, 0), 2)
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
        status = snapshot_cache.get_status(cam["name"])
        result.append(
            {
                "name": cam["name"],
                "status": status.get("status", "connecting"),
                "clients": client_registry.get(cam["name"]),
                "last_frame_age_seconds": _last_frame_age(status.get("last_frame_at")),
                "split": cam["split"],
                "split_names": cam["split_names"] if cam["split"] else [],
                "split_axis": cam.get("split_axis", "vertical"),
                "message": status.get("message", ""),
                "capture_fps": status.get("capture_fps"),
                "write_fps": status.get("write_fps"),
            }
        )
    return jsonify(result)


def _last_frame_age(last_frame_at: Optional[str]):
    if not last_frame_at:
        return None
    try:
        dt = datetime.fromisoformat(last_frame_at)
        return round((datetime.now() - dt).total_seconds(), 2)
    except Exception:
        return None


@app.route("/api/recordings/<cam_name>")
def api_recordings(cam_name):
    return jsonify(get_camera_recordings(cam_name))


if __name__ == "__main__":
    port = int(os.environ.get("WEB_PORT", 8080))
    logger.info("Iniciando servidor web en http://0.0.0.0:%s", port)
    app.run(host="0.0.0.0", port=port, threaded=True)
