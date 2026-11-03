"""
research/api/routes_ft_probabilidad.py — 🎲 FT PROBABILIDAD en el panel (30/10/2026).

FT_PROBABILIDAD es un servicio APARTE (otro servicio de Railway, su propio volumen). Esta pestaña solo MUESTRA lo que
ese servicio ya calculó: la lista de partidos, la probabilidad experimental de cada uno y su reporte.

REGLAS (a diferencia del BOT BUSCADOR, aquí el servidor de RankingFTR sí le pregunta al servicio, por la red
privada de Railway, y por eso lleva estos límites):
  1. Solo se llama cuando un admin abre la pestaña (o la refresca). Nada en segundo plano, ninguna tarea nueva.
  2. Tiempo máximo corto (2 s para conectar, 4 s en total) y una copia de 10 s: aunque varias personas abran la pestaña,
     el servicio recibe como mucho una pregunta cada 10 s.
  3. Si el servicio no responde o no está configurado, la pestaña lo dice y nada más cambia: ninguna otra pantalla
     ni alerta depende de esto.
  4. SOLO LECTURA. Ninguna escritura en la base de RankingFTR ni en el servicio. El interruptor sigue siendo
     Admin → Estrategias → FT PROBABILIDAD.
  5. El secreto (FTR_INTERNAL_SECRET) viaja solo en una cabecera hacia el servicio; nunca llega al navegador.

Variable necesaria en RankingFTR:  FT_PROBABILIDAD_URL  (por ejemplo http://NOMBRE-DEL-SERVICIO.railway.internal:8080)

  GET /research/api/ft-probabilidad/resumen          estado del servicio + últimos partidos (JSON)
  GET /research/api/ft-probabilidad/partido/{id}     detalle JSON de un partido (lo muestra el modal)
  GET /research/api/ft-probabilidad/reporte/{id}     el panel HTML de un partido (enlace dentro del modal)

Permiso: ver_tab_ftprobabilidad (permisos.py; de fábrica solo admin).
Para quitarlo: este archivo, su include en app.py, la pestaña de research.html y las líneas de permisos.py.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from typing import Optional
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

log = logging.getLogger(__name__)
router = APIRouter(prefix="/research", tags=["ft-probabilidad"])

_TIMEOUT = httpx.Timeout(4.0, connect=2.0)
_TTL_S = 10.0
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_cache: dict = {"t": 0.0, "datos": None}
_lock = threading.Lock()


def _origen() -> Optional[str]:
    """El origen configurado ('http://host[:puerto]') o None si falta o no es válido."""
    crudo = (os.getenv("FT_PROBABILIDAD_URL", "") or "").strip()
    if not crudo or any(c in crudo for c in " \t\r\n;,'\"<>\\"):
        return None
    try:
        p = urlsplit(crudo)
        puerto = p.port
    except ValueError:
        return None
    if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password:
        return None
    if p.path not in ("", "/") or p.query or p.fragment:
        return None
    host = p.hostname if ":" not in p.hostname else f"[{p.hostname}]"
    return f"{p.scheme}://{host}" + (f":{puerto}" if puerto else "")


def _secreto() -> str:
    from production.api.deps import FTR_INTERNAL_SECRET
    return FTR_INTERNAL_SECRET or ""


def _acceso(request: Request, token: str) -> None:
    from research.api.routes_research import _check_token_o_sesion
    _check_token_o_sesion(request, token, ["ftprobabilidad"])


def _pedir(ruta: str, params: Optional[dict] = None):
    """GET al servicio. Devuelve (respuesta, None) o (None, dict_con_el_motivo). Nunca lanza."""
    base = _origen()
    if not base:
        return None, {"ok": False, "motivo": "sin_configurar",
                      "mensaje": "Falta la variable FT_PROBABILIDAD_URL en RankingFTR (o no es una dirección http válida)."}
    try:
        r = httpx.get(base + ruta, params=params, headers={"X-FT-Status-Secret": _secreto()},
                      timeout=_TIMEOUT, follow_redirects=False, trust_env=False)
    except httpx.HTTPError as e:
        log.warning("[FT PROBABILIDAD] sin conexión con el servicio: %s", type(e).__name__)
        return None, {"ok": False, "motivo": "sin_conexion",
                      "mensaje": "No se pudo conectar con el servicio FT_PROBABILIDAD (¿está encendido y bien escrita la URL?)."}
    if r.status_code == 401:
        return None, {"ok": False, "motivo": "no_autorizado",
                      "mensaje": "El servicio rechazó el secreto: FTR_INTERNAL_SECRET debe ser el mismo en los dos servicios."}
    return r, None


@router.get("/api/ft-probabilidad/resumen")
def ft_probabilidad_resumen(request: Request, token: str = ""):
    _acceso(request, token)
    with _lock:
        if _cache["datos"] is not None and time.monotonic() - _cache["t"] < _TTL_S:
            return _cache["datos"]
        r, error = _pedir("/resumen", {"limite": 60})
        if error:
            return error
        if r.status_code != 200:
            # Diagnóstico: quién contestó (cabecera server / primeras letras del cuerpo), sin exponer secretos.
            quien = (r.headers.get("server", "") if hasattr(r, "headers") else "")[:40]
            cuerpo = (r.text or "")[:120].replace("\n", " ")
            log.warning("[FT PROBABILIDAD] respuesta %s de %s | server=%r | cuerpo=%r",
                        r.status_code, _origen(), quien, cuerpo)
            return {"ok": False, "motivo": f"http_{r.status_code}",
                    "mensaje": f"El servicio respondió con error {r.status_code}."
                               + (f" Respondió: {quien or 'sin cabecera server'} · {cuerpo}" if cuerpo or quien else "")}
        try:
            datos = r.json()
        except ValueError:
            return {"ok": False, "motivo": "respuesta_invalida", "mensaje": "El servicio devolvió algo que no es JSON."}
        if not isinstance(datos, dict) or not isinstance(datos.get("partidos"), list):
            return {"ok": False, "motivo": "respuesta_invalida", "mensaje": "El servicio devolvió un formato inesperado."}
        datos = {"ok": True, **datos}
        _cache["datos"], _cache["t"] = datos, time.monotonic()
        return datos


@router.get("/api/ft-probabilidad/partido/{fixture_id}")
def ft_probabilidad_partido(fixture_id: str, request: Request, token: str = ""):
    """Detalle de un partido (JSON) para el modal de la pestaña. Solo lectura, sin copia."""
    _acceso(request, token)
    if not _ID.fullmatch(fixture_id):
        raise HTTPException(status_code=400, detail="Identificador no válido")
    r, error = _pedir(f"/partido/{fixture_id}")
    if error:
        return error
    if r.status_code == 404:
        return {"ok": False, "motivo": "no_encontrado", "mensaje": "Ese partido ya no tiene informe en el servicio."}
    if r.status_code != 200:
        return {"ok": False, "motivo": f"http_{r.status_code}", "mensaje": f"El servicio respondió con error {r.status_code}."}
    try:
        datos = r.json()
    except ValueError:
        return {"ok": False, "motivo": "respuesta_invalida", "mensaje": "El servicio devolvió algo que no es JSON."}
    if not isinstance(datos, dict):
        return {"ok": False, "motivo": "respuesta_invalida", "mensaje": "El servicio devolvió un formato inesperado."}
    return {"ok": True, **datos}


@router.get("/api/ft-probabilidad/reporte/{fixture_id}", response_class=HTMLResponse)
def ft_probabilidad_reporte(fixture_id: str, request: Request, token: str = ""):
    _acceso(request, token)
    if not _ID.fullmatch(fixture_id):
        raise HTTPException(status_code=400, detail="Identificador no válido")
    r, error = _pedir(f"/reporte/{fixture_id}")
    if error:
        raise HTTPException(status_code=502, detail=error["mensaje"])
    if r.status_code == 404:
        raise HTTPException(status_code=404, detail="Ese reporte ya no está en el servicio.")
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"El servicio respondió con error {r.status_code}.")
    resp = HTMLResponse(r.text)
    # El panel es HTML autocontenido sin scripts: se sirve sin permitir nada más.
    resp.headers["Content-Security-Policy"] = ("default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
                                               "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Cache-Control"] = "no-store"
    return resp
