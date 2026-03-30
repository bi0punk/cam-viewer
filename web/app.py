#!/usr/bin/env python3
"""
Interfaz web para cámaras en vivo y grabaciones.
"""

import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path

import cv2
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
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("web")
app = Flask(__name__)


def load_cameras():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
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
                "split": cam.get("split", False),
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
logger.info("Cámaras cargadas: %s", [c["name"] for c in CAMERAS])


class StreamManager:
    def __init__(self):
        self._caps = {}
        self._frames = {}
        self._locks = {}
        self._threads = {}
        self._running = {}
        self._status = {}

    def _init_camera(self, name: str, url: str):
        self._locks[name] = threading.Lock()
        self._frames[name] = None
        self._running[name] = True
        self._status[name] = "connecting"
        thread = threading.Thread(target=self._capture_loop, args=(name, url), daemon=True)
        self._threads[name] = thread
        thread.start()

    def get_or_init(self, name: str, url: str):
        if name not in self._threads or not self._threads[name].is_alive():
            self._init_camera(name, url)

    def _capture_loop(self, name: str, url: str):
        logger.info("[%s] Abriendo stream para vista en vivo...", name)
        while self._running.get(name, False):
            cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

            if not cap.isOpened():
                logger.warning("[%s] No se pudo conectar. Reintentando en 5s...", name)
                self._status[name] = "error"
                time.sleep(5)
                continue

            self._status[name] = "ok"
            logger.info("[%s] Stream de vista en vivo conectado.", name)

            while self._running.get(name, False):
                ok, frame = cap.read()
                if not ok or frame is None:
                    logger.warning("[%s] Frame perdido, reconectando...", name)
                    self._status[name] = "error"
                    break

                h, w = frame.shape[:2]
                max_w = 1280
                if w > max_w:
                    scale = max_w / w
                    frame = cv2.resize(frame, (max_w, int(h * scale)))

                ok2, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
                if ok2:
                    with self._locks[name]:
                        self._frames[name] = buf.tobytes()

            cap.release()
            if self._running.get(name, False):
                time.sleep(3)

    def get_frame(self, name: str):
        lock = self._locks.get(name)
        if lock is None:
            return None
        with lock:
            return self._frames.get(name)

    def status(self, name: str):
        return self._status.get(name, "connecting")


stream_mgr = StreamManager()
for cam in CAMERAS:
    stream_mgr.get_or_init(cam["name"], cam["url"])


def _human_size(size_bytes: int) -> str:
    for unit in ["B", "KB", "MB", "GB"]:
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


def get_camera_recordings(cam_name: str) -> list:
    folder = Path(RECORDINGS_DIR) / cam_name
    if not folder.exists():
        return []

    files = []
    for f in sorted(folder.rglob("*"), key=lambda p: p.stat().st_mtime, reverse=True):
        if not f.is_file() or f.suffix.lower() not in (".mp4", ".mkv", ".avi"):
            continue

        stat = f.stat()
        rel_path = f.relative_to(folder).as_posix()
        try:
            parts = f.stem.rsplit("_", 2)
            dt = datetime.strptime(f"{parts[-2]}_{parts[-1]}", "%Y%m%d_%H%M%S")
            date_str = dt.strftime("%d/%m/%Y %H:%M:%S")
        except Exception:
            date_str = datetime.fromtimestamp(stat.st_mtime).strftime("%d/%m/%Y %H:%M:%S")

        files.append(
            {
                "name": f.name,
                "relative_path": rel_path,
                "folder": str(f.parent.relative_to(folder)).replace(".", ""),
                "date": date_str,
                "size": _human_size(stat.st_size),
                "size_raw": stat.st_size,
                "mtime": stat.st_mtime,
            }
        )
    return files


def get_all_recordings_summary() -> list:
    summary = []
    rec_root = Path(RECORDINGS_DIR)
    if not rec_root.exists():
        return summary

    for folder in sorted(rec_root.iterdir()):
        if not folder.is_dir():
            continue
        files = [p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in (".mp4", ".mkv", ".avi")]
        total = sum(f.stat().st_size for f in files)
        summary.append(
            {
                "name": folder.name,
                "count": len(files),
                "total": _human_size(total),
                "latest": max((f.stat().st_mtime for f in files), default=0),
            }
        )
    return summary


@app.route("/")
def index():
    return redirect(url_for("live"))


@app.route("/live")
def live():
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
    return render_template("live.html", cameras=display_cams)


@app.route("/stream/<name>")
def stream(name):
    cam_cfg = next((c for c in CAMERAS if c["name"] == name), None)
    if cam_cfg is None:
        abort(404)

    split_side = request.args.get("side", type=int, default=None)
    split_axis = request.args.get("axis", default=cam_cfg.get("split_axis", "vertical"))
    stream_mgr.get_or_init(name, cam_cfg["url"])

    def generate():
        placeholder = _placeholder_jpeg()
        while True:
            frame_bytes = stream_mgr.get_frame(name)
            if frame_bytes is None:
                frame_bytes = placeholder
            elif split_side is not None:
                frame_bytes = _crop_half(frame_bytes, split_side, split_axis)

            yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame_bytes + b"\r\n"
            time.sleep(1 / 15)

    return Response(generate(), mimetype="multipart/x-mixed-replace; boundary=frame")


def _crop_half(jpeg_bytes: bytes, side: int, axis: str = "vertical") -> bytes:
    try:
        import numpy as np

        buf = np.frombuffer(jpeg_bytes, dtype=np.uint8)
        frame = cv2.imdecode(buf, cv2.IMREAD_COLOR)
        if frame is None:
            return jpeg_bytes

        h, w = frame.shape[:2]
        axis = (axis or "vertical").lower()
        if axis == "horizontal":
            mid = h // 2
            half = frame[:mid, :] if side == 0 else frame[mid:, :]
        else:
            mid = w // 2
            half = frame[:, :mid] if side == 0 else frame[:, mid:]

        ok, enc = cv2.imencode(".jpg", half, [cv2.IMWRITE_JPEG_QUALITY, 80])
        return enc.tobytes() if ok else jpeg_bytes
    except Exception:
        return jpeg_bytes


def _placeholder_jpeg() -> bytes:
    import numpy as np

    img = np.zeros((360, 640, 3), dtype=np.uint8)
    cv2.putText(img, "Conectando...", (200, 180), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 200, 0), 2)
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
    return send_file(
        str(filepath),
        as_attachment=as_attachment,
        download_name=filepath.name,
        mimetype="video/mp4",
    )


@app.route("/api/cameras")
def api_cameras():
    result = []
    for cam in CAMERAS:
        result.append(
            {
                "name": cam["name"],
                "status": stream_mgr.status(cam["name"]),
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
