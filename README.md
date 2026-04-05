# IP Camera Recorder — versión endurecida

Proyecto para grabar cámaras IP y visualizar streams en vivo desde una interfaz web.

## Qué mejoré

### Grabación
- Segmentación alineada al reloj: si el proceso inicia a las `10:23`, el primer clip termina a las `11:00`; luego crea clips `11:00-12:00`, `12:00-13:00`, etc.
- Escritura desacoplada de la captura para reducir jitter y lag.
- FPS de salida estables por clip: evita que un stream irregular termine generando videos con duración de reproducción incorrecta.
- Detección de stream congelado y reconexión automática.
- Frames normalizados a dimensiones pares para evitar fallos intermitentes de `VideoWriter`.
- Fallback de codecs para mejorar compatibilidad.
- Aviso de poco espacio en disco.
- Soporte de variables de entorno en YAML para no dejar credenciales duras en el repositorio.

### Vista web
- Carga lazy: no abre RTSP hasta que un cliente realmente entra a `/live`.
- Cierre automático de streams inactivos.
- Cachea JPEG completo y mitades split en memoria para evitar recortar y recomprimir por cada cliente.
- Cache TTL para listados de grabaciones.
- Endpoint `/health` para healthcheck real.

### Operación
- Dockerfile y Compose más limpios.
- `init: true` y `stop_grace_period` para cierres más limpios.
- Archivo `.env.example`.
- Script `validate_config.py`.

---

## Estructura de grabaciones

```text
recordings/
└── cam_parking_izquierda/
    └── 2026-03/
        └── 30/
            ├── cam_parking_izquierda_20260330_102300.mp4
            ├── cam_parking_izquierda_20260330_110000.mp4
            └── cam_parking_izquierda_20260330_120000.mp4
```

## Comportamiento de segmentación

```text
Inicio real:         10:23:14
Primer clip:         10:23:14 -> 11:00:00
Segundo clip:        11:00:00 -> 12:00:00
Tercer clip:         12:00:00 -> 13:00:00
```

Si el proceso parte exactamente a una hora cerrada, por ejemplo `14:00:00`, el primer clip será `14:00:00 -> 15:00:00`.

---

## Configuración YAML

Ejemplo:

```yaml
cameras:
  - name: "cam_doble_parking"
    url: "${CAM_DOBLE_PARKING_URL}"
    enabled: true
    split: true
    split_axis: vertical
    split_names:
      - "cam_parking_izquierda"
      - "cam_parking_derecha"
    fps: 15
    output_fps: 15
    width: 0
    height: 0

recording:
  segment_duration_minutes: 60
  output_dir: "/recordings"
  video_format: "mp4"
  codec: "mp4v"
  fallback_codecs: ["mp4v", "XVID", "MJPG"]
  reconnect_delay_seconds: 10
  reconnect_delay_max_seconds: 60
  startup_frame_timeout_seconds: 20
  stale_stream_timeout_seconds: 12
  min_disk_free_gb: 1.0
  log_level: "INFO"
```

---

## Levantar con Docker Compose

1. Copia variables de ejemplo:

```bash
cp .env.example .env
```

2. Ajusta URLs RTSP reales en `.env`.

3. Levanta servicios:

```bash
docker compose up -d --build
```

Ver logs:

```bash
docker compose logs -f recorder
docker compose logs -f web
```

Validar config:

```bash
python validate_config.py config/cameras.yaml
```

Bajar servicios:

```bash
docker compose down
```

---

## Vista web

- En vivo: `http://IP_DEL_SERVIDOR:8080/live`
- Grabaciones: `http://IP_DEL_SERVIDOR:8080/recordings`
- Health: `http://IP_DEL_SERVIDOR:8080/health`

---

## Prueba rápida de una cámara

```bash
python test_camera.py "rtsp://usuario:clave@IP:554/stream1"
python test_camera.py "rtsp://usuario:clave@IP:554/stream1" --split --split-axis vertical
```

---

## Notas operacionales

- El proyecto sigue grabando solo video.
- Para producción con muchas cámaras, conviene monitorear CPU, I/O de disco, uso de RAM y ancho de banda RTSP.
- Si una cámara entrega FPS muy inestables, esta versión prioriza duración correcta del clip y continuidad visual, aunque pueda repetir el último frame cuando la cámara se atrasa.
- Si quieres grabación más eficiente aún, el siguiente salto natural es migrar ciertos casos a FFmpeg con `-c copy` para cámaras sin split.
