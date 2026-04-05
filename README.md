# IP Camera Recorder — versión con preview compartido

Proyecto para grabar cámaras IP y visualizar streams en vivo desde una interfaz web sin abrir RTSP desde el frontend.

## Qué cambia en esta versión

### 1) Se elimina la doble conexión RTSP
Antes, el `recorder` y la `web` abrían la cámara por separado. Ahora:

- `recorder.py` abre **una sola** conexión RTSP por cámara.
- El grabador publica previews JPEG en `/live_cache/<cam>/`.
- `web/app.py` sirve esos JPEG como MJPEG al navegador.

Resultado:

- menos carga sobre la cámara
- menos CPU por doble decodificación
- menos riesgo de freeze al abrir `/live`

### 2) Preview más liviano y controlado
Cada cámara puede definir:

- `preview_width`
- `preview_fps`
- `preview_jpeg_quality`

Eso permite mantener la web fluida sin obligar a codificar JPEG a resolución completa.

### 3) Grabación más estable
Se mantiene una sola captura RTSP y la grabación continúa desacoplada de la captura.
Además:

- reconexión agresiva si no llegan frames válidos
- detección de stream congelado
- publicación de `status.json` por cámara en `/live_cache`
- métricas por segmento: `capture_fps`, `write_fps`, `repeated_writes`, `read_failures`

## Nueva arquitectura

```text
RTSP cámara
   |
   v
recorder.py
   |-- captura única OpenCV/FFmpeg backend
   |-- grabación segmentada a /recordings
   '-- preview JPEG a /live_cache

web/app.py
   '-- lee /live_cache y entrega /stream/<cam> al navegador
```

## Estructura del preview compartido

```text
live_cache/
└── cam_entrada/
    ├── full.jpg
    └── status.json

live_cache/
└── cam_doble_parking/
    ├── full.jpg
    ├── side_0.jpg
    ├── side_1.jpg
    └── status.json
```

## Configuración YAML

Ejemplo:

```yaml
cameras:
  - name: "cam_entrada"
    url: "${CAM_ENTRADA_URL}"
    enabled: true
    split: false
    fps: 15
    output_fps: 15
    width: 1920
    height: 1080
    preview_width: 960
    preview_fps: 5
    preview_jpeg_quality: 70

  - name: "cam_doble_parking"
    url: "${CAM_DOBLE_PARKING_URL}"
    enabled: true
    split: true
    split_axis: horizontal
    split_names:
      - "cam_parking_izquierda"
      - "cam_parking_derecha"
    fps: 15
    output_fps: 12
    width: 1280
    height: 720
    preview_width: 960
    preview_fps: 4
    preview_jpeg_quality: 65

recording:
  segment_duration_minutes: 60
  output_dir: "/recordings"
  live_cache_dir: "/live_cache"
  video_format: "mp4"
  codec: "mp4v"
  fallback_codecs: ["mp4v", "XVID", "MJPG"]
  reconnect_delay_seconds: 10
  reconnect_delay_max_seconds: 60
  startup_frame_timeout_seconds: 20
  stale_stream_timeout_seconds: 12
  min_disk_free_gb: 1.0
  status_write_interval_seconds: 2.0
  log_level: "INFO"
```

## Levantar con Docker Compose

```bash
cp .env.example .env  # o cp env.example .env si tu extractor oculta dotfiles
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

## URLs

- En vivo: `http://IP_DEL_SERVIDOR:8080/live`
- Grabaciones: `http://IP_DEL_SERVIDOR:8080/recordings`
- Health: `http://IP_DEL_SERVIDOR:8080/health`
- Estado cámara: `http://IP_DEL_SERVIDOR:8080/api/cameras`

## Recomendaciones operacionales

### Para cámaras normales
- usa `preview_fps` entre `4` y `6`
- usa `preview_width` entre `854` y `960`
- usa `preview_jpeg_quality` entre `60` y `72`

### Para cámaras split
- evita resoluciones demasiado altas si el host es CPU-only
- baja `output_fps` a `10-12` si ves `repeated_writes` altos
- define `width` y `height`; no dejes `0/0` si el stream negocia mal

### Cómo revisar si mejoró
Observa en logs del `recorder`:

- `capture_fps`
- `write_fps`
- `repeated_writes`
- `read_failures`

Si al abrir `/live` ya no sube fuerte el uso de CPU, la eliminación de la doble captura quedó bien aplicada.
