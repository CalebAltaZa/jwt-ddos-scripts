# jwt-ddos-scripts

Generador de carga HTTP (`ddos_load.py`) usado en la actividad de seguridad
para medir la resistencia de los servicios de **PracticaJWT** y alimentar
Fail2Ban con tráfico de prueba.

> ⚠️ **Uso solo autorizado.** Ejecuta este script únicamente contra servicios
> que tú administres o tengas permiso explícito para probar. Probarlo contra
> sistemas de terceros es ilegal.

## Requisitos

- Python 3.10 o superior
- Sin dependencias externas (solo stdlib)

## Uso

```bash
python3 ddos_load.py --host 127.0.0.1 --port 8080
```

### Parámetros

| Parámetro        | Por defecto | Descripción                                        |
|------------------|-------------|----------------------------------------------------|
| `--host`         | (oblig.)    | IP o hostname del objetivo                         |
| `--port`         | (oblig.)    | Puerto del objetivo                                |
| `--path`         | `/`         | Ruta HTTP                                          |
| `--method`       | `GET`       | Método HTTP (`GET/POST/PUT/DELETE`)                |
| `--concurrency`  | `50`        | Solicitudes en vuelo simultáneas                   |
| `--rate`         | `100`       | Solicitudes/segundo objetivo (`0` = sin límite)    |
| `--duration`     | `15`        | Duración total en segundos                        |
| `--ramp`         | `0`         | Segundos para subir linealmente la concurrencia    |
| `--timeout`      | `5`         | Timeout por solicitud (segundos)                   |
| `--payload`      | (vacío)     | Cuerpo de la solicitud                             |
| `--header`       | —           | Cabecera extra (repetible), ej. `--header 'Authorization: Bearer <token>'` |
| `--verify-tls`   | off         | Verificar certificados TLS                         |

### Ejemplos

```bash
# Frontend (nginx) — 200 req/s durante 20 s
python3 ddos_load.py --host 127.0.0.1 --port 8080 --path /login \
  --concurrency 100 --rate 200 --duration 20

# Servicio auth — rampa de 0 a 50 hilos en 5 s
python3 ddos_load.py --host 127.0.0.1 --port 8000 --path /health \
  --concurrency 50 --ramp 5 --rate 0 --duration 30

# Servicio items con POST y payload
python3 ddos_load.py --host 127.0.0.1 --port 8001 --path /api/items \
  --method POST --payload '{"nombre":"carga"}' --concurrency 20 --duration 15

# Endpoint autenticado (items): con el token real
TOKEN=$(curl -s -X POST http://127.0.0.1:8000/auth/login -H 'Content-Type: application/json' \
  -d '{"username":"caleb","password":"caleb123"}' | sed 's/.*"access_token":"\([^"]*\)".*/\1/')
python3 ddos_load.py --host 127.0.0.1 --port 8001 --path /items \
  --concurrency 200 --rate 200 --duration 10 \
  --header "Authorization: Bearer $TOKEN"
```

## Salida

Cada segundo imprime el ritmo real, total de solicitudes, errores y workers
activos; al finalizar muestra:

- total de solicitudes y req/s reales
- aciertos (2xx/3xx) vs fallos
- desglose por código HTTP
- latencias p50 / p95 / p99 y media

## Valores de carga que NO tumban cada servicio

Pruebas del 2026-10-07 (stack PracticaJWT, detalle en la carpeta
`ActividadFail2Ban/evidence/`):

| Servicio | Carga máxima estable (sin errores) | Comportamiento al exceder |
|---|---|---|
| Frontend nginx :8080 (`/health`) | **~1600 req/s** (p95 < 50 ms) | satura ~1900 req/s; sigue sirviendo 200 incluso con 3000 conexiones (~1.8 s de latencia) |
| auth :8000 (`/health`) | **~600 req/s** (p95 ~58 ms) | responde sin errores a mayor volumen |
| auth :8000 (`POST /auth/login`) | **~10 logins/s @20 conn** (p95 ~2.6 s) | **tumba con 50 conn**: ~60 % de timeouts (cuello de botella = bind LDAPS por petición) |
| items :8001 (`GET /items` con JWT) | **~200 req/s** (p95 < 10 ms) | satura ~260 req/s; no cae con 800 conexiones |

Conclusión práctica para Fail2Ban: el vector DDoS más eficaz no es saturar
nginx (muy resistente), es inundar `POST /api/auth/login`; por eso el jail
`jwt-loginflood` usa un umbral mucho más bajo que el flood genérico.

## Notas

- El script se ejecuta **fuera** de las imágenes Docker (desde el host), tal
  como pide la consigna: los contenedores ven el origen como `172.21.0.1`
  (loopback vía docker-proxy) o como la IP real de LAN.
- Se usa `Connection: close` por solicitud para que cada petición sea una
  conexión TCP nueva (estresa también el accept-queue de nginx/uvicorn).
