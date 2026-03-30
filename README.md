# IP Camera Recorder

Proyecto para grabar cámaras IP y visualizar streams en vivo desde una interfaz web.

## Qué quedó corregido

- El primer clip ya no dura 60 minutos fijos desde que parte la grabación.
  Ahora termina en la próxima frontera horaria. Ejemplo: si inicia a las `10:23`, el primer archivo termina a las `11:00`. Desde ahí en adelante quedan clips `11:00-12:00`, `12:00-13:00`, etc.
- La división de cámaras dobles quedó corregida para `izquierda/derecha` cuando el stream viene lado a lado.
- Se añadió soporte opcional `split_axis: horizontal` para cámaras que entregan `arriba/abajo`.
- Las grabaciones ahora se guardan ordenadas por cámara, mes y día.
- La interfaz web ahora lista grabaciones de forma recursiva respetando la nueva estructura.

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

Caso real:

```text
Inicio real:         10:23:14
Primer clip:         10:23:14 -> 11:00:00
Segundo clip:        11:00:00 -> 12:00:00
Tercer clip:         12:00:00 -> 13:00:00
```

Si el proceso parte exactamente a una hora cerrada, por ejemplo `14:00:00`, el primer clip será `14:00:00 -> 15:00:00`.

## Configuración YAML

Ejemplo:

```yaml
cameras:
  - name: "cam_doble_parking"
    url: "rtsp://usuario:clave@192.168.1.13:554/live/ch00_1"
    enabled: true
    split: true
    split_axis: vertical   # vertical = izquierda/derecha
    split_names:
      - "cam_parking_izquierda"
      - "cam_parking_derecha"
    fps: 15
    width: 0
    height: 0

recording:
  segment_duration_minutes: 60
  output_dir: "/recordings"
  video_format: "mp4"
  codec: "mp4v"
```

## Levantar con Docker Compose

```bash
docker compose up -d --build
```

Ver logs:

```bash
docker compose logs -f recorder
docker compose logs -f web
```

Bajar servicios:

```bash
docker compose down
```

## Vista web

- En vivo: `http://IP_DEL_SERVIDOR:8080/live`
- Grabaciones: `http://IP_DEL_SERVIDOR:8080/recordings`

## Prueba rápida de una cámara

```bash
python test_camera.py "rtsp://usuario:clave@IP:554/stream1"
python test_camera.py "rtsp://usuario:clave@IP:554/stream1" --split --split-axis vertical
```

## Notas operacionales

- El proyecto graba solo video.
- Si quieres audio, conviene migrar el writer a FFmpeg con `copy` o transcodificación controlada.
- `mp4v` funciona como base general, pero si una cámara produce archivos incompatibles, prueba `XVID` o valida una versión con FFmpeg.
- Para producción con muchas cámaras, conviene monitorear CPU, I/O de disco y latencia de red RTSP.
