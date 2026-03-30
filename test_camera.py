#!/usr/bin/env python3
"""
Prueba simple de cámara y snapshots.
"""

import sys
from datetime import datetime
from pathlib import Path

import cv2


def split_frame(frame, axis: str = "vertical"):
    h, w = frame.shape[:2]
    axis = (axis or "vertical").lower()
    if axis == "horizontal":
        mid = h // 2
        return [("top", frame[:mid, :]), ("bottom", frame[mid:, :])]
    mid = w // 2
    return [("left", frame[:, :mid]), ("right", frame[:, mid:])]


def test_camera(url: str, split: bool = False, split_axis: str = "vertical", save_snapshot: bool = True):
    print(f"\n{'='*60}")
    print(f"  Probando conexión a: {url}")
    print(f"{'='*60}")

    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    if not cap.isOpened():
        print("ERROR: No se pudo conectar a la cámara.")
        sys.exit(1)

    print("Conexión establecida.")
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS)
    backend = cap.getBackendName()

    print(f"\n  Resolución : {w} x {h}")
    print(f"  FPS        : {fps}")
    print(f"  Backend    : {backend}")

    ok, frame = cap.read()
    if not ok or frame is None:
        print("ERROR: No se pudo leer ningún frame.")
        cap.release()
        sys.exit(1)

    print(f"\n  Frame leído correctamente — shape: {frame.shape}")

    if save_snapshot:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = Path("snapshots")
        out_dir.mkdir(exist_ok=True)

        if split:
            for name, img in split_frame(frame, split_axis):
                path = out_dir / f"snapshot_{name}_{ts}.jpg"
                cv2.imwrite(str(path), img)
                print(f"  Snapshot guardado: {path}")
        else:
            path = out_dir / f"snapshot_{ts}.jpg"
            cv2.imwrite(str(path), frame)
            print(f"  Snapshot guardado: {path}")

    cap.release()
    print(f"\n{'='*60}")
    print("  Prueba completada exitosamente.")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Uso: python test_camera.py <url> [--split] [--split-axis vertical|horizontal]")
        sys.exit(1)

    url = sys.argv[1]
    do_split = "--split" in sys.argv
    split_axis = "vertical"
    if "--split-axis" in sys.argv:
        idx = sys.argv.index("--split-axis")
        if idx + 1 < len(sys.argv):
            split_axis = sys.argv[idx + 1]

    test_camera(url, split=do_split, split_axis=split_axis)
