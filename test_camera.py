#!/usr/bin/env python3
"""
test_camera.py — Herramienta de prueba de conexión a cámara
============================================================
Uso:
    python test_camera.py <url_rtsp>
    python test_camera.py rtsp://admin:password@192.168.1.100:554/stream1

Muestra información del stream y hace una captura de prueba.
Si la cámara tiene split habilitado, también prueba la división.
"""

import cv2
import sys
import os
from datetime import datetime
from pathlib import Path


def test_camera(url: str, split: bool = False, save_snapshot: bool = True):
    print(f"\n{'='*60}")
    print(f"  Probando conexión a: {url}")
    print(f"{'='*60}")

    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)

    if not cap.isOpened():
        print("❌  ERROR: No se pudo conectar a la cámara.")
        sys.exit(1)

    print("✅  Conexión establecida.")

    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS)
    backend = cap.getBackendName()

    print(f"\n  Resolución : {w} x {h}")
    print(f"  FPS        : {fps}")
    print(f"  Backend    : {backend}")

    ok, frame = cap.read()
    if not ok:
        print("❌  ERROR: No se pudo leer ningún frame.")
        cap.release()
        sys.exit(1)

    print(f"\n  Frame leído correctamente — shape: {frame.shape}")

    if save_snapshot:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = Path("snapshots")
        out_dir.mkdir(exist_ok=True)

        if split:
            mid = w // 2
            left = frame[:, :mid]
            right = frame[:, mid:]
            for name, img in [("left", left), ("right", right)]:
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
        print("Uso: python test_camera.py <url> [--split]")
        sys.exit(1)

    url = sys.argv[1]
    do_split = "--split" in sys.argv
    test_camera(url, split=do_split)
