#!/usr/bin/env python3
"""Generador de carga HTTP para pruebas de estrés (solo servicios PROPIOS).

Uso autorizado ÚNICAMENTE contra sistemas que tú administres o tengas
permiso explícito para probar. Ejecutarlo contra terceros es ilegal.

Sin dependencias externas: solo Python 3.10+ (stdlib).

Ejemplos:
  python3 ddos_load.py --host 127.0.0.1 --port 8080
  python3 ddos_load.py --host 127.0.0.1 --port 8000 --path /health \
      --concurrency 50 --rate 200 --duration 20
  python3 ddos_load.py --host 127.0.0.1 --port 8001 --path /api/items \
      --method GET --duration 30 --ramp 5
"""

import argparse
import asyncio
import ssl
import statistics
import sys
import time

USER_AGENT = "ddos_load/1.0 (prueba-de-carga-propia)"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Generador de carga HTTP contra servicios propios (PracticaJWT).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--host", required=True, help="IP o hostname del objetivo")
    p.add_argument("--port", required=True, type=int, help="Puerto del objetivo")
    p.add_argument("--path", default="/", help="Ruta HTTP a golpear")
    p.add_argument("--method", default="GET", choices=["GET", "POST", "PUT", "DELETE"],
                   help="Método HTTP")
    p.add_argument("--concurrency", type=int, default=50,
                   help="Solicitudes en vuelo simultáneas")
    p.add_argument("--rate", type=int, default=100,
                   help="Solicitudes por segundo objetivo (0 = sin límite)")
    p.add_argument("--duration", type=float, default=15.0,
                   help="Duración total de la prueba en segundos")
    p.add_argument("--ramp", type=float, default=0.0,
                   help="Segundos para subir linealmente de 0 a --concurrency (0 = fijo)")
    p.add_argument("--timeout", type=float, default=5.0,
                   help="Timeout por solicitud en segundos")
    p.add_argument("--payload", default=None,
                   help="Cuerpo de la solicitud (si se omite se envía vacío)")
    p.add_argument("--header", action="append", default=[],
                   help="Cabecera HTTP adicional, ej. --header 'Authorization: Bearer xxx' "
                        "(se puede repetir)")
    p.add_argument("--verify-tls", action="store_true",
                   help="Verificar certificados TLS (por defecto se desactiva en local)")
    return p.parse_args()


class Stats:
    def __init__(self) -> None:
        self.total = 0
        self.ok = 0
        self.errors = 0
        self.status: dict[int, int] = {}
        self.latencies: list[float] = []
        self.start = time.perf_counter()
        self._lock = asyncio.Lock()

    async def record(self, status: int | None, elapsed: float) -> None:
        async with self._lock:
            self.total += 1
            if status is not None and 200 <= status < 400:
                self.ok += 1
                self.latencies.append(elapsed)
            else:
                self.errors += 1
            if status is not None:
                self.status[status] = self.status.get(status, 0) + 1

    def report(self, args: argparse.Namespace) -> str:
        dur = max(time.perf_counter() - self.start, 1e-9)
        lines = [
            "",
            "=" * 62,
            f" RESULTADO  {args.method} http://{args.host}:{args.port}{args.path}",
            "=" * 62,
            f" Duración           : {dur:.1f} s",
            f" Solicitudes totales: {self.total}  ({self.total / dur:.0f} req/s reales)",
            f" Correctas (2xx/3xx): {self.ok}",
            f" Fallidas            : {self.errors}",
            f" Códigos            : {dict(sorted(self.status.items())) or '—'}",
        ]
        if self.latencies:
            lat = sorted(self.latencies)
            lines += [
                f" Latencia p50/p95/p99: {self._pct(lat, 50):.1f} / "
                f"{self._pct(lat, 95):.1f} / {self._pct(lat, 99):.1f} ms",
                f" Latencia media       : {statistics.fmean(lat) * 1000:.2f} ms",
            ]
        lines.append("=" * 62)
        return "\n".join(lines)

    @staticmethod
    def _pct(sorted_lat: list[float], pct: float) -> float:
        idx = min(int(len(sorted_lat) * pct / 100), len(sorted_lat) - 1)
        return sorted_lat[idx] * 1000


class Limiter:
    """Token bucket compartido: limita la tasa global de envío de solicitudes."""

    def __init__(self, rate: int) -> None:
        self.interval = 1.0 / rate if rate > 0 else 0.0
        self.next_at = time.perf_counter()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        if not self.interval:
            return
        async with self._lock:
            now = time.perf_counter()
            wait = self.next_at - now
            self.next_at = max(self.next_at, now) + self.interval
        if wait > 0:
            await asyncio.sleep(wait)


def build_ssl_context(args: argparse.Namespace) -> ssl.SSLContext | None:
    if args.port not in (443, 636) and not args.path.startswith("https"):
        return None
    ctx = ssl.create_default_context()
    if not args.verify_tls:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


async def send_request(args: argparse.Namespace, ssl_ctx: ssl.SSLContext | None,
                       payload: bytes) -> int | None:
    """Envía una petición HTTP/1.1 con Connection: close y devuelve el código."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(args.host, args.port, ssl=ssl_ctx),
            timeout=args.timeout,
        )
    except Exception:
        return None
    try:
        body = "" if args.method == "GET" or not payload else payload.decode()
        headers = [
            f"{args.method} {args.path} HTTP/1.1",
            f"Host: {args.host}:{args.port}",
            f"User-Agent: {USER_AGENT}",
            "Accept: */*",
            "Connection: close",
        ]
        headers += list(args.header)
        if body:
            headers += [f"Content-Length: {len(payload)}",
                        "Content-Type: application/json"]
        writer.write(("\r\n".join(headers) + "\r\n\r\n").encode() + payload)
        await asyncio.wait_for(writer.drain(), timeout=args.timeout)
        line = await asyncio.wait_for(reader.readline(), timeout=args.timeout)
        parts = line.split()
        if len(parts) >= 2 and parts[1].isdigit():
            return int(parts[1])
        return None
    except Exception:
        return None
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def worker(wid: int, args: argparse.Namespace, stats: Stats,
                 stop_at: float, limiter: Limiter, ssl_ctx: ssl.SSLContext | None,
                 payload: bytes, start: float) -> None:
    if args.ramp > 0:
        release = start + args.ramp * (wid + 1) / args.concurrency
        delay = release - time.perf_counter()
        if delay > 0:
            await asyncio.sleep(delay)
        if time.perf_counter() >= stop_at:
            return
    while time.perf_counter() < stop_at:
        await limiter.acquire()
        if time.perf_counter() >= stop_at:
            break
        t0 = time.perf_counter()
        status = await send_request(args, ssl_ctx, payload)
        await stats.record(status, time.perf_counter() - t0)


async def main() -> None:
    args = parse_args()
    if args.host not in ("localhost", "127.0.0.1"):
        print(f"[!] Objetivo no-local: {args.host} — asegúrate de tener autorización.")

    print(f" Objetivo     : {args.method} http://{args.host}:{args.port}{args.path}")
    print(f" Concurrencia : {args.concurrency}   (ramp={args.ramp}s)")
    print(f" Rate objetivo: {args.rate or 'sin límite'} req/s")
    print(f" Duración     : {args.duration}s")
    print(" ¡Solo contra servicios propios!\n")

    stats = Stats()
    start = time.perf_counter()
    stop_at = start + args.duration
    limiter = Limiter(args.rate)
    ssl_ctx = build_ssl_context(args)
    payload = args.payload.encode() if args.payload else b""
    workers = [asyncio.create_task(
        worker(i, args, stats, stop_at, limiter, ssl_ctx, payload, start))
        for i in range(args.concurrency)]

    last_total, last_t = stats.total, time.perf_counter()
    while not all(w.done() for w in workers):
        await asyncio.sleep(1.0)
        now = time.perf_counter()
        rate = (stats.total - last_total) / max(now - last_t, 1e-9)
        active = sum(1 for w in workers if not w.done())
        print(f"  t={now - start:>5.1f}s  req/s={rate:>7.1f}  "
              f"total={stats.total}  errores={stats.errors}  activos={active}")
        last_total, last_t = stats.total, now

    await asyncio.gather(*workers, return_exceptions=True)
    print(stats.report(args))


if __name__ == "__main__":
    asyncio.run(main())
