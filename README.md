# 📷 IP Camera Recorder

Grabador de cámaras IP en Python con soporte para:
- Múltiples cámaras simultáneas
- Grabación en segmentos de **1 hora**
- **División vertical** de imagen (para cámaras de doble lente)
- Reconexión automática
- Configuración por archivo YAML
- Despliegue con Docker Compose

---

## 📁 Estructura del Proyecto

```
ip-camera-recorder/
├── config/
│   └── cameras.yaml       ← Configuración de cámaras (editar aquí)
├── recordings/            ← Grabaciones (se crea automáticamente)
│   ├── cam_entrada/
│   │   ├── cam_entrada_20240315_080000.mp4
│   │   └── cam_entrada_20240315_090000.mp4
│   ├── cam_parking_izquierda/
│   └── cam_parking_derecha/
├── logs/                  ← Logs de cada cámara
├── recorder.py            ← Lógica principal
├── test_camera.py         ← Herramienta de prueba
├── requirements.txt
├── Dockerfile
└── docker-compose.yml
```

---

## 🚀 Inicio Rápido

### 1. Configurar cámaras

Edita `config/cameras.yaml`:

```yaml
cameras:
  - name: "cam_entrada"
    url: "rtsp://admin:password@192.168.1.100:554/stream1"
    enabled: true
    split: false
    fps: 15

  # Cámara de doble lente → divide en dos grabaciones
  - name: "cam_doble"
    url: "rtsp://admin:password@192.168.1.101:554/stream1"
    enabled: true
    split: true
    split_names:
      - "cam_izquierda"
      - "cam_derecha"
    fps: 15

recording:
  segment_duration_minutes: 60
  output_dir: "/recordings"
  video_format: "mp4"
```

### 2. Iniciar con Docker Compose

```bash
# Construir e iniciar
docker compose up -d

# Ver logs en tiempo real
docker compose logs -f

# Detener
docker compose down
```

### 3. Sin Docker (modo desarrollo)

```bash
pip install -r requirements.txt
python recorder.py
```

---

## ⚙️ Parámetros de Cámara

| Parámetro | Tipo | Descripción |
|-----------|------|-------------|
| `name` | string | Nombre identificador (sin espacios) |
| `url` | string | URL RTSP o HTTP de la cámara |
| `enabled` | bool | Activa/desactiva la grabación |
| `split` | bool | Divide la imagen verticalmente al 50% |
| `split_names` | list | Nombres para el canal izquierdo y derecho |
| `fps` | float | Fotogramas por segundo (default: 15) |
| `width` | int | Ancho de captura (0 = automático) |
| `height` | int | Alto de captura (0 = automático) |

---

## 🔀 División Vertical (Cámaras Doble Lente)

Algunas cámaras con dos lentes transmiten ambas imágenes **una al lado de la otra** en un solo stream:

```
┌────────────────────┬────────────────────┐
│                    │                    │
│   Lente Izquierda  │   Lente Derecha    │
│                    │                    │
└────────────────────┴────────────────────┘
         Stream único (ej: 3840x1080)
```

Con `split: true`, el grabador divide cada frame exactamente en el centro y genera **dos archivos de video independientes**:

- `cam_parking_izquierda_20240315_080000.mp4`
- `cam_parking_derecha_20240315_080000.mp4`

---

## 🛠️ Herramienta de Prueba

Verifica la conexión y guarda un snapshot antes de grabar:

```bash
# Desde el host (requiere pip install)
python test_camera.py rtsp://admin:password@192.168.1.100:554/stream1

# Con división vertical
python test_camera.py rtsp://admin:password@192.168.1.101:554/stream1 --split

# Desde dentro del contenedor
docker exec -it ip_camera_recorder \
  python test_camera.py rtsp://admin:password@192.168.1.100:554/stream1
```

Los snapshots se guardan en la carpeta `snapshots/`.

---

## 📝 URLs RTSP Comunes

| Marca | URL típica |
|-------|-----------|
| Hikvision | `rtsp://user:pass@ip:554/Streaming/Channels/101` |
| Dahua | `rtsp://user:pass@ip:554/cam/realmonitor?channel=1&subtype=0` |
| Reolink | `rtsp://user:pass@ip:554/h264Preview_01_main` |
| Amcrest | `rtsp://user:pass@ip:554/cam/realmonitor?channel=1&subtype=0` |
| Axis | `rtsp://user:pass@ip:554/axis-media/media.amp` |
| Foscam | `rtsp://user:pass@ip:554/videoMain` |
| TP-Link | `rtsp://user:pass@ip:554/stream1` |

---

## 📦 Estructura de Grabaciones

```
recordings/
└── cam_entrada/
    ├── cam_entrada_20240315_080000.mp4   ← 08:00 a 09:00
    ├── cam_entrada_20240315_090000.mp4   ← 09:00 a 10:00
    └── cam_entrada_20240315_100000.mp4   ← 10:00 a 11:00
```

Cada archivo dura exactamente **1 hora** (configurable con `segment_duration_minutes`).

---

## 🔧 Configuración Avanzada

### Zona horaria

Edita en `docker-compose.yml`:
```yaml
environment:
  - TZ=America/Bogota    # Cambia a tu zona horaria
```

### Recursos por cámara (referencia)

| Resolución | CPU aprox. | RAM aprox. |
|------------|-----------|-----------|
| 720p / 15fps | 5–10% | ~80 MB |
| 1080p / 15fps | 10–20% | ~150 MB |
| 4K / 15fps | 30–50% | ~400 MB |

### Múltiples grupos de cámaras

Puedes duplicar el servicio en `docker-compose.yml` con distintas configuraciones:

```yaml
services:
  recorder_exterior:
    build: .
    volumes:
      - ./config/exterior.yaml:/config/cameras.yaml:ro
      - ./recordings/exterior:/recordings

  recorder_interior:
    build: .
    volumes:
      - ./config/interior.yaml:/config/cameras.yaml:ro
      - ./recordings/interior:/recordings
```

---

## ❓ Solución de Problemas

**La cámara no conecta:**
```bash
# Verificar que el stream es accesible desde el host
ffplay rtsp://admin:password@192.168.1.100:554/stream1
```

**Video corrupto o sin audio:**
- El grabador no captura audio (solo video). Para audio, usar FFmpeg directamente.
- Si el video no abre, prueba cambiar `codec` a `XVID` o `avc1`.

**Alto consumo de CPU:**
- Reduce el `fps` en la configuración de la cámara.
- Cambia a resolución más baja con `width` y `height`.

**No encuentra el archivo de configuración:**
```bash
# Verificar que existe
ls -la ./config/cameras.yaml
```
