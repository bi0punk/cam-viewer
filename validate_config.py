#!/usr/bin/env python3
"""
Valida la configuración YAML del grabador.
"""

from pathlib import Path
import sys
import yaml
import re
import os

ENV_VAR_PATTERN = re.compile(r"\$\{([^}:]+)(?::-(.*?))?\}")


def substitute_env_vars(text: str) -> str:
    def repl(match):
        var_name = match.group(1)
        default = match.group(2) or ""
        return os.environ.get(var_name, default)
    return ENV_VAR_PATTERN.sub(repl, text)


def main(path: str):
    cfg_path = Path(path)
    if not cfg_path.exists():
        print(f"ERROR: no existe {cfg_path}")
        return 1

    raw = yaml.safe_load(substitute_env_vars(cfg_path.read_text(encoding='utf-8'))) or {}
    errors = []

    cameras = raw.get('cameras', [])
    if not isinstance(cameras, list) or not cameras:
        errors.append("'cameras' debe ser una lista no vacía")

    seen = set()
    for idx, cam in enumerate(cameras):
        name = cam.get('name')
        url = cam.get('url')
        if not name:
            errors.append(f"cameras[{idx}].name es obligatorio")
        if not url:
            errors.append(f"cameras[{idx}].url es obligatorio")
        if name in seen:
            errors.append(f"Nombre de cámara duplicado: {name}")
        seen.add(name)
        if cam.get('split') and len(cam.get('split_names', [])) < 2:
            errors.append(f"{name}: split=true requiere al menos 2 split_names")

        for field in ('fps', 'output_fps', 'preview_fps'):
            if field in cam and float(cam.get(field, 0)) < 0:
                errors.append(f"{name}: {field} no puede ser negativo")

        for field in ('width', 'height', 'preview_width'):
            if field in cam and int(cam.get(field, 0)) < 0:
                errors.append(f"{name}: {field} no puede ser negativo")

        quality = int(cam.get('preview_jpeg_quality', 70))
        if quality < 30 or quality > 95:
            errors.append(f"{name}: preview_jpeg_quality debe estar entre 30 y 95")

    recording = raw.get('recording', {})
    if int(recording.get('segment_duration_minutes', 60)) <= 0:
        errors.append("recording.segment_duration_minutes debe ser > 0")
    if float(recording.get('status_write_interval_seconds', 2.0)) <= 0:
        errors.append("recording.status_write_interval_seconds debe ser > 0")

    if errors:
        print("Configuración inválida:")
        for err in errors:
            print(f" - {err}")
        return 2

    print("Configuración OK")
    return 0


if __name__ == '__main__':
    cfg = sys.argv[1] if len(sys.argv) > 1 else 'config/cameras.yaml'
    raise SystemExit(main(cfg))
