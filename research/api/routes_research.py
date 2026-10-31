"""
api/research_panel.py — FullTenis Research: Beta interna
==========================================================
Panel PRIVADO de investigación para revisar el motor Elo experimental
(core/elo_engine_v11.py) — NO es el ranking oficial del sitio, NO manda
señales, NO se conecta al bot. Es un panel de lectura para uso interno,
protegido por token (mismo patrón que /admin).

Fuente de datos: la DB generada por tools/build_elo_v11.py (offline,
no se actualiza sola con partidos en vivo — hay que re-correr el
backfill del laboratorio manualmente cuando se quiera refrescar).

Rutas:
  GET  /research                       — página HTML (requiere ?token=)
  GET  /research/api/rankings          — tabla de ranking Elo
  GET  /research/api/search            — buscador de jugador
  GET  /research/api/compare           — comparador A vs B (prob + cuota justa)
  GET  /research/api/leaderboard       — leaderboard de experimentos
  GET  /research/api/validator         — reporte del Validator (texto)
  GET  /research/api/ftr-delta         — diccionario ΔFTR -> win% histórico
"""
from __future__ import annotations
import csv
import json
import logging
import math
import os
import re
import difflib
import secrets
import sqlite3
import threading as _threading
import time as _time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.requests import Request
from pydantic import BaseModel

from research.engines.elo_v1_sackmann import confidence_from_matches, parse_score_sets
from shared.tennis_names import normalize_name, find_player_id
from shared.elo_lookup import (
    ELO_DB_PATH,
    expected_score as _expected_score,
    elo_col as _elo_col,
    matches_col as _matches_col,
    buscar_candidatos_elo as _buscar_candidatos,
    # Saca banderas/emojis del nombre antes de resolverlo (ver el caso
    # "Shnaider D 🇷🇺" documentado en shared/elo_lookup.py).
    _sin_simbolos as _sin_simbolos_nombre,
)

router = APIRouter(prefix="/research", tags=["research"])
log = logging.getLogger(__name__)
_templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "web" / "templates"))

RESEARCH_TOKEN = os.getenv("RESEARCH_TOKEN", "")

# FIX SEGURIDAD (08/07/2026): antes, si RESEARCH_TOKEN no estaba seteada,
# `if RESEARCH_TOKEN and token != RESEARCH_TOKEN` nunca disparaba -- el
# panel quedaba abierto a cualquiera sin token, en silencio. Ahora falla
# el arranque del servicio si falta, en vez de fallar en silencio.
if not RESEARCH_TOKEN:
    raise RuntimeError(
        "RESEARCH_TOKEN no está configurada -- el servicio no arranca así "
        "para evitar que /research quede sin autenticación por accidente."
    )

# Path de la DB del laboratorio Elo — NO es FTR_DB_PATH (esa es la DB
# oficial UTR). Se mantiene aparte a propósito: mezclar los dos
# esquemas en la misma DB sería confuso y arriesgado para el sistema
# en producción. Igual que con FTR_DB_PATH, si hay un volumen en
# Railway, esta variable debería apuntar ahí — el archivo pesa ~68MB,
# no se versiona en git (ver .gitignore).
_RESEARCH_ROOT = Path(__file__).resolve().parent.parent  # research/
LEADERBOARD_CSV = Path(os.getenv("FTR_LAB_LEADERBOARD_PATH", str(_RESEARCH_ROOT / "reports" / "ftr_lab" / "leaderboard.csv")))
VALIDATOR_REPORT = Path(os.getenv("FTR_ELO_VALIDATOR_REPORT_PATH", str(_RESEARCH_ROOT / "reports" / "fulltenis_elo_validacion.txt")))

# Diccionario ΔFTR -> win% histórico, generado offline por
# scripts/build_ftr_delta_dictionary.py (solo lectura sobre match_log,
# no depende del motor Elo de research ni de nada en vivo -- hay que
# re-correr el script a mano para refrescarlo con partidos nuevos).
FTR_DELTA_DICT_PATH = Path(os.getenv("FTR_DELTA_DICT_PATH", str(_RESEARCH_ROOT.parent / "data" / "ftr_delta_dictionary.json")))

SURFACES = ["general", "hard", "clay", "grass"]


def _check_token(token: str):
    if not secrets.compare_digest(token, RESEARCH_TOKEN):
        raise HTTPException(status_code=403, detail="Token inválido")


def _check_token_o_sesion(request, token: str, tabs=None):
    """
    Igual que _check_token, pero acepta también una SESIÓN válida cuyo rol
    pueda ver alguno de `tabs` (None = cualquier tab).

    Por qué hace falta (29/07/2026, bug real: "el ranking Elo no se
    visualiza en el usuario"): RESEARCH_TOKEN llega por `?token=` en la
    URL y queda en localStorage. Un admin lo tiene porque entra con ese
    link; una cuenta normal entra por login con sesión y NUNCA lo tiene.
    Resultado: /research/api/* respondía 403 a todos los usuarios, y como
    el JS no mira el status, `data.total` salía `undefined` y la tabla
    quedaba vacía sin ningún mensaje de error. El síntoma era
    "undefined jugadores".

    Esto NO afloja el control de acceso: el permiso que se exige es
    exactamente el mismo que decide si el tab se renderiza. Alguien que no
    puede ver el tab tampoco pasa por acá. Solo se aplica a endpoints de
    LECTURA; /api/analisis-ia sigue con _check_token a secas, porque
    gasta tokens de IA de verdad y su permiso es aparte (`analisis_ia`).
    """
    if token and secrets.compare_digest(token, RESEARCH_TOKEN):
        return
    from production.api.auth import usuario_actual
    from production.api.permisos import puede_ver_tab, tabs_de

    u = usuario_actual(request) if request is not None else None
    if not u:
        raise HTTPException(status_code=403, detail="Token inválido")
    rol = u.get("rol", "")
    permitidos = tabs_de(rol)
    if tabs is None:
        if not permitidos:
            raise HTTPException(status_code=403,
                                detail="Tu cuenta no tiene permiso para ver research")
        return
    if not any(puede_ver_tab(rol, t) for t in tabs):
        raise HTTPException(status_code=403,
                            detail="Tu cuenta no tiene permiso para esta sección")


def _check_token_o_sesion_analisis_ia(request, token: str):
    """
    FIX (01/08/2026, bug real reportado por Maria con captura: "Token
    inválido" en el IA Match Analyzer para una cuenta admin logueada
    por sesión). /api/analisis-ia se dejó a propósito con _check_token
    a secas (ver nota de _check_token_o_sesion, "gasta tokens de IA de
    verdad") -- pero eso raspaba dos veces: el middleware de rutas
    (permisos.py, regla ANALISIS_IA sobre /research/api/analisis-ia)
    YA exige el permiso `analisis_ia` por sesión antes de que este
    handler se ejecute. Encima de eso, el handler pedía TAMBIÉN el
    RESEARCH_TOKEN crudo -- que un admin logueado por sesión (entró por
    el formulario de login, no por un link con ?token=) nunca tiene en
    localStorage. Resultado: alguien con el permiso correcto (admin,
    rol con analisis_ia) igual recibía 403 acá adentro.

    Mismo patrón que _check_token_o_sesion de arriba, pero contra el
    permiso ANALISIS_IA en vez de una lista de tabs -- este endpoint no
    es una vista, es una acción que cuesta plata, así que lo que
    corresponde chequear es tiene_permiso(rol, ANALISIS_IA), no
    puede_ver_tab(). El token crudo sigue funcionando igual que antes
    para quien entra por un link con ?token= (no se le quita nada).
    """
    if token and secrets.compare_digest(token, RESEARCH_TOKEN):
        return
    from production.api.auth import usuario_actual
    from production.api.permisos import tiene_permiso, ANALISIS_IA

    u = usuario_actual(request) if request is not None else None
    if not u:
        raise HTTPException(status_code=403, detail="Token inválido")
    rol = u.get("rol", "")
    if not tiene_permiso(rol, ANALISIS_IA):
        raise HTTPException(status_code=403,
                            detail="Tu cuenta no tiene permiso para pedir análisis con IA")


# FIX PANEL-EN-VIVO (08/07/2026): partidos_en_vivo vive en la DB de
# PRODUCTION (ftr_utr.db, mismo FTR_DB_PATH que ya usa el resto del
# sitio oficial) -- no en la DB del laboratorio Elo. Investigar no
# escribe nunca ahí, solo lee, igual que ya hace build_research_db.py.
_PROD_DB_PATH = Path(os.getenv("FTR_DB_PATH", "production/data/ftr_utr.db"))


def _get_conn_production() -> sqlite3.Connection:
    if not _PROD_DB_PATH.exists():
        raise HTTPException(status_code=503, detail=f"DB de producción no encontrada en {_PROD_DB_PATH}.")
    conn = sqlite3.connect(_PROD_DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=20000")
    conn.row_factory = sqlite3.Row
    return conn


def _get_conn() -> sqlite3.Connection:
    if not ELO_DB_PATH.exists():
        raise HTTPException(
            status_code=503,
            detail=f"DB del laboratorio Elo no encontrada en {ELO_DB_PATH}. "
                   f"Corré tools/build_elo_v11.py o configurá FULLTENIS_ELO_DB_PATH."
        )
    conn = sqlite3.connect(ELO_DB_PATH)
    # FIX DATABASE-IS-LOCKED (07/07/2026): el live_sync escribe cada 15s
    # en esta misma DB — sin WAL, una lectura en el momento exacto de una
    # escritura choca y sqlite tira "database is locked". WAL permite que
    # lectores y escritores no se bloqueen entre sí; busy_timeout es un
    # respaldo para el caso borde de que igual coincidan justo.
    # ACTUALIZACIÓN (13/07/2026): subido de 5000 a 20000ms -- esta
    # conexión es la de MÁS tráfico de todas las que tocan esta DB (se
    # abre en CADA request a /research/api/* -- compare, rankings,
    # history -- que por los logs se disparan varias veces por segundo
    # mientras alguien usa el buscador). Mismo margen que el resto de
    # los escritores (backfill_incremental.py, backfill_player_bio.py,
    # update_elo_v1.py::sync()) después del incidente real del 13/07.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=20000")
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# API — datos
# ---------------------------------------------------------------------------

_NIVELES_TOUR_PRINCIPAL = (
    "atp250", "wta250", "atp500", "wta500", "masters", "grand_slam", "finals"
)

# FIX PERFORMANCE (28/07/2026): tour_counts era un CTE recalculado EN CADA
# request a /api/rankings -- un UNION ALL sobre las ~127k filas de
# matches_processed (dos veces, por ganador y por perdedor) + GROUP BY,
# sin cachear. El log real mostró 3344ms en un solo request ("[Perf]
# LENTO GET /research/api/rankings"). matches_processed solo cambia
# cuando corre el backfill del laboratorio (manual, no en vivo -- ver
# docstring del módulo), así que no hace falta recalcular esto en cada
# fixture del panel: se materializa en una tabla de verdad (indexada por
# PK, JOIN O(log n) en vez de escanear 254k filas) y se refresca como
# mucho una vez cada _TOUR_COUNTS_TTL_S segundos, sin importar cuántos
# requests lleguen en el medio.
_TOUR_COUNTS_TTL_S = int(os.getenv("FTR_TOUR_COUNTS_TTL_S", "600"))
_tour_counts_lock = _threading.Lock()
# -inf (no 0.0): time.monotonic() NO arranca en 0 (depende del sistema,
# puede arrancar ya en varios cientos de segundos) -- con 0.0 como
# sentinel, "ahora - 0.0 < TTL" podía dar True en el primerísimo request
# de cada proceso y saltearse el refresh inicial dejando la tabla vacía.
_tour_counts_computed_at = float("-inf")


def _ensure_tour_counts_table(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tour_counts_cache (
            nombre TEXT PRIMARY KEY,
            n      INTEGER NOT NULL
        )
    """)


def _refresh_tour_counts_cache(conn: sqlite3.Connection):
    placeholders = ",".join("?" * len(_NIVELES_TOUR_PRINCIPAL))
    filas = conn.execute(
        f"""
        SELECT nombre, COUNT(*) AS n FROM (
            SELECT ganador AS nombre FROM matches_processed WHERE nivel_norm IN ({placeholders})
            UNION ALL
            SELECT perdedor AS nombre FROM matches_processed WHERE nivel_norm IN ({placeholders})
        ) GROUP BY nombre
        """,
        list(_NIVELES_TOUR_PRINCIPAL) * 2,
    ).fetchall()
    conn.execute("DELETE FROM tour_counts_cache")
    conn.executemany(
        "INSERT INTO tour_counts_cache (nombre, n) VALUES (?, ?)",
        [(f["nombre"], f["n"]) for f in filas],
    )
    conn.commit()


def _ensure_tour_counts_fresh(conn: sqlite3.Connection):
    """Recalcula tour_counts_cache como mucho 1 vez cada _TOUR_COUNTS_TTL_S
    segundos, sin importar cuántos requests concurrentes lo pidan (el lock
    evita que dos workers disparen el mismo recálculo caro a la vez)."""
    global _tour_counts_computed_at
    _ensure_tour_counts_table(conn)
    ahora = _time.monotonic()
    if ahora - _tour_counts_computed_at < _TOUR_COUNTS_TTL_S:
        # Chequeo barato: si la tabla ya tiene filas, listo -- evita
        # recalcular en el primer request de cada worker nuevo si la
        # tabla persistida ya venía de un refresh reciente de OTRO worker.
        if conn.execute("SELECT 1 FROM tour_counts_cache LIMIT 1").fetchone():
            return
    with _tour_counts_lock:
        ahora = _time.monotonic()
        if ahora - _tour_counts_computed_at < _TOUR_COUNTS_TTL_S:
            return
        _refresh_tour_counts_cache(conn)
        _tour_counts_computed_at = ahora


@router.get("/api/rankings")
def api_rankings(
    request: Request,
    token: str = "",
    gender: str = Query("M", description="M o F"),
    surface: str = Query("general", description="general/hard/clay/grass"),
    limit: int = Query(100, le=500),
    # PAGINACIÓN (31/07/2026, a pedido de Rubén: "por qué en el ranking Elo
    # solo salen 200 personas"). La causa NO era el filtro de nivel: era
    # que el front pedía limit=200 fijo y que `total` de acá abajo era
    # `len(rows)` -- o sea, el tamaño de la página, no la cantidad real de
    # jugadores. La tabla decía "200 jugadores" porque había pedido 200,
    # no porque hubiera 200. Ahora `total` es un COUNT(*) de verdad sobre
    # el mismo WHERE, y `offset` permite traer el resto de a tandas en vez
    # de volcar miles de filas al DOM de una.
    offset: int = Query(0, ge=0),
    q: Optional[str] = Query(None, description="Filtrar por nombre"),
    min_partidos_tour: int = Query(
        10,
        description="Mínimo de partidos jugados a nivel ATP250/WTA250 o superior "
                     "para aparecer en el ranking general. Calibrado con datos reales: "
                     "con umbral 1, un jugador con una sola clasificación de tour colada "
                     "en 94 partidos de carrera igual pasaba el filtro. Pasar 0 para "
                     "desactivar (ver el ranking 'puro' sin este filtro)."),
):
    """
    Un jugador puede acumular Elo alto ganándole sistemáticamente a rivales
    de su mismo nivel (Challenger/ITF) sin nunca haber probado nivel
    ATP/WTA principal — no es evidencia de que sea top 2 del circuito
    real, es evidencia de que domina su categoría. Este filtro no toca
    el Elo (sigue calculado igual, visible buscando por nombre) — solo
    saca de la vista del ranking general a quien nunca jugó el nivel
    principal, mismo criterio que min_partidos en production/.
    """
    # Lectura: token de research O sesión con permiso del tab
    # (29/07/2026 -- ver _check_token_o_sesion). Antes era
    # _check_token a secas y las cuentas de usuario, que nunca tienen
    # RESEARCH_TOKEN, recibían 403 y veían la tabla vacía.
    _check_token_o_sesion(request, token, ("ranking", "compare"))
    if surface not in SURFACES:
        raise HTTPException(status_code=400, detail=f"superficie inválida: {surface}")
    col = _elo_col(surface)

    from research.engines.ajustes_contextuales import ensure_player_bio_table, edad_desde_birthday

    conn = _get_conn()
    try:
        ensure_player_bio_table(conn)
        params: list = []
        join_tour = ""
        cond_tour = ""

        if min_partidos_tour and min_partidos_tour > 0:
            # FIX PERFORMANCE (07/07/2026): la versión anterior hacía una
            # subconsulta CORRELACIONADA (una vez POR CADA jugador,
            # escaneando los 127k partidos cada vez) — con miles de
            # jugadores eso son ~mil millones de comparaciones y termina
            # en timeout.
            # FIX PERFORMANCE v2 (28/07/2026): la versión intermedia (un
            # CTE con GROUP BY) sacaba el timeout pero seguía escaneando
            # ~254k filas de matches_processed EN CADA request (confirmado
            # en logs: 3344ms). tour_counts_cache es esa misma cuenta,
            # materializada en una tabla real (PK = índice) y refrescada
            # como mucho una vez cada FTR_TOUR_COUNTS_TTL_S segundos --
            # ver _ensure_tour_counts_fresh(). El JOIN de acá pasa a ser
            # O(log n) por fila en vez de un scan completo.
            _ensure_tour_counts_fresh(conn)
            join_tour = "LEFT JOIN tour_counts_cache tc ON tc.nombre = r.name"
            cond_tour = "AND COALESCE(tc.n, 0) >= ?"

        # El FROM/WHERE se arma una sola vez y se reusa para el COUNT y
        # para la página: si divergieran, el total diría una cosa y la
        # tabla mostraría otra.
        desde = f"""
            FROM ratings r
            {join_tour}
            LEFT JOIN player_bio b ON b.player = r.name AND b.gender = r.gender
            WHERE r.gender = ?
        """
        params.append(gender.upper())

        if q:
            desde += " AND r.name LIKE ?"
            params.append(f"%{q}%")

        if cond_tour:
            desde += f" {cond_tour}"
            params.append(min_partidos_tour)

        total = conn.execute(f"SELECT COUNT(*) {desde}", params).fetchone()[0]

        sql = (f"SELECT r.name, r.gender, r.{col} AS elo, r.matches_total, "
               f"r.confidence_label, r.last_match_date, "
               f"b.birthday, b.height_cm, b.mano_dominante {desde} "
               f"ORDER BY r.{col} DESC LIMIT ? OFFSET ?")
        rows = conn.execute(sql, params + [limit, offset]).fetchall()
        return {
            "gender": gender.upper(),
            "surface": surface,
            # total = cuántos hay en TOTAL con este filtro.
            # mostrados/offset = qué trozo devuelve esta respuesta.
            "total": total,
            "offset": offset,
            "mostrados": len(rows),
            "hay_mas": offset + len(rows) < total,
            "jugadores": [
                {
                    "pos": offset + i + 1,
                    "name": r["name"],
                    "elo": round(r["elo"], 2),
                    "edad": edad_desde_birthday(r["birthday"]),
                    "altura_cm": r["height_cm"],
                    "mano_dominante": r["mano_dominante"],
                    "matches": r["matches_total"],
                    "confidence": r["confidence_label"],
                    "last_match": r["last_match_date"],
                }
                for i, r in enumerate(rows)
            ],
        }
    finally:
        conn.close()



@router.get("/api/buscar-jugador")
def api_buscar_jugador(
    request: Request,
    token: str = "",
    nombre: str = Query(..., description="Nombre a buscar, cualquier formato"),
    gender: str = Query("M"),
    surface: str = Query("general"),
):
    """
    Devuelve los candidatos que matchean un nombre (0, 1, o varios) --
    pensado para el Verificador FTR (09/07/2026): en vez de fallar en
    silencio con "no encontrado" cuando hay ambigüedad, esto le muestra
    las opciones a la persona para que elija la correcta.
    """
    # Lectura: token de research O sesión con permiso del tab
    # (29/07/2026 -- ver _check_token_o_sesion). Antes era
    # _check_token a secas y las cuentas de usuario, que nunca tienen
    # RESEARCH_TOKEN, recibían 403 y veían la tabla vacía.
    _check_token_o_sesion(request, token, None)
    if surface not in SURFACES:
        raise HTTPException(status_code=400, detail=f"superficie inválida: {surface}")
    col = _elo_col(surface)
    mcol = _matches_col(surface)

    conn = _get_conn()
    try:
        candidatos, confiable = _buscar_candidatos(conn, col, mcol, gender, nombre)
        return {
            "query": nombre,
            "confiable": confiable,
            "candidatos": [
                {"name": c["name"], "elo": round(c["elo"], 2), "matches": c["matches"]}
                for c in candidatos
            ],
        }
    finally:
        conn.close()


def _wins_losses(conn, player_name: str, surface: str) -> tuple[int, int]:
    """
    Victorias y derrotas de un jugador en una superficie, leyendo
    rating_history (una fila por jugador por partido, `result`: 1
    ganó / 0 perdió). Para 'general' no se filtra por superficie --
    en rating_history el campo `surface` siempre tiene el valor real
    del partido (hard/clay/grass), nunca 'general'; "general" en el
    resto de este archivo es un agregado de matches_total, así que
    acá el equivalente es simplemente no filtrar.
    """
    where_surface = "" if surface == "general" else "AND surface = ?"
    params = [player_name] if surface == "general" else [player_name, surface]
    row = conn.execute(
        f"""SELECT
              SUM(CASE WHEN result = 1 THEN 1 ELSE 0 END) AS wins,
              SUM(CASE WHEN result = 0 THEN 1 ELSE 0 END) AS losses
            FROM rating_history WHERE player = ? {where_surface}""",
        params,
    ).fetchone()
    return (row["wins"] or 0, row["losses"] or 0)


def _forma_reciente(conn, player_name: str, surface: str) -> dict:
    """
    Racha reciente de un jugador -- últimos 10 partidos (mas reciente
    primero, para pintar los puntitos de color) y récord de los
    últimos 3 meses, en la MISMA superficie que se está mirando (no
    'general' salvo que general sea justo lo pedido) -- mismo criterio
    de filtro que _wins_losses() de arriba.
    """
    where_surface = "" if surface == "general" else "AND surface = ?"
    params_base = [player_name] if surface == "general" else [player_name, surface]

    filas = conn.execute(
        f"""SELECT result FROM rating_history
            WHERE player = ? {where_surface}
            ORDER BY fecha DESC LIMIT 10""",
        params_base,
    ).fetchall()
    ultimos_10 = ["W" if f["result"] == 1 else "L" for f in filas]

    fecha_corte = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    params_3m = params_base + [fecha_corte]
    row_3m = conn.execute(
        f"""SELECT
              SUM(CASE WHEN result = 1 THEN 1 ELSE 0 END) AS wins,
              SUM(CASE WHEN result = 0 THEN 1 ELSE 0 END) AS losses
            FROM rating_history WHERE player = ? {where_surface} AND fecha >= ?""",
        params_3m,
    ).fetchone()

    return {
        "ultimos_10": ultimos_10,  # ej. ["W","W","L","W",...] -- indice 0 es el mas reciente
        "ultimos_3_meses": {"wins": row_3m["wins"] or 0, "losses": row_3m["losses"] or 0},
    }


# Categorías de carga de calendario -- acordadas con Maria el 10/07/2026,
# ancladas a partidos jugados en una ventana de 28 días (4 semanas). Esto
# mide VOLUMEN de calendario (proxy de fatiga/riesgo físico), no
# rendimiento -- para rendimiento ya están ultimos_10/ultimos_3_meses en
# _forma_reciente(). Se agrega como algo extra, no en reemplazo de nada:
# miden cosas distintas (ventana corta de cantidad vs. ventana larga de
# resultado). Mismo formato de tabla (lo, hi, nombre, descripción) que
# CATEGORIAS en shared/ftr_dictionary.py, para consistencia de estilo.
CATEGORIAS_CARGA = [
    (0, 0, "Sin actividad",
     "Ningún partido -- lesión, descanso, entre torneos"),
    (1, 3, "Actividad baja",
     "A lo sumo un torneo con eliminación temprana, o volviendo de un parate"),
    (4, 7, "Normal",
     "1-2 torneos con recorrido medio -- el calendario típico de un "
     "jugador de gira regular, sin clasificación"),
    (8, 11, "Carga",
     "2-3 torneos seguidos con buen recorrido, o entrando por "
     "clasificación en alguno"),
    (12, 15, "Sobrecarga",
     "3-4 torneos consecutivos yendo lejos en casi todos -- el "
     "\"grinder\" típico de Challenger/ITF en racha"),
    (16, math.inf, "Riesgo alto",
     "Casi el techo teórico -- semana tras semana sin parar, entrando "
     "por clasificación seguido"),
]


def _categoria_carga(n_partidos: int) -> dict:
    """
    Ubica un número de partidos (ventana de 28 días) en una de las 6
    bandas de carga de calendario. La última banda (16+) no tiene techo
    real -- si algún caso poco realista lo superara, sigue cayendo ahí
    en vez de quedar sin categoría.
    """
    for lo, hi, nombre, descripcion in CATEGORIAS_CARGA:
        if lo <= n_partidos <= hi:
            return {"nombre": nombre, "descripcion": descripcion}
    lo, hi, nombre, descripcion = CATEGORIAS_CARGA[-1]
    return {"nombre": nombre, "descripcion": descripcion}


def _carga_reciente(conn, player_name: str, surface: str) -> dict:
    """
    Carga de calendario: partidos en los últimos 28 días (4 semanas),
    con split ganados/perdidos Y la secuencia ordenada de resultados
    (para pintar la barra de progreso en el frontend, 10/07/2026) --
    orden cronológico ASC (más viejo primero), igual que se lee una
    barra de izquierda a derecha. Un partido en la semana 5 NO entra acá
    porque el filtro `fecha >= fecha_corte` ya lo excluye -- la ventana
    es por fecha, no por "últimos N partidos", así que la barra nunca
    puede mostrar algo fuera de las 4 semanas.

    Mismo criterio de filtro por superficie que _wins_losses()/
    _forma_reciente() de arriba -- para 'general' no se filtra porque en
    rating_history el campo surface siempre trae el valor real del
    partido, nunca 'general'.
    """
    where_surface = "" if surface == "general" else "AND surface = ?"
    params_base = [player_name] if surface == "general" else [player_name, surface]

    fecha_corte = (datetime.now(timezone.utc) - timedelta(days=28)).isoformat()
    params = params_base + [fecha_corte]
    filas = conn.execute(
        f"""SELECT result FROM rating_history
            WHERE player = ? {where_surface} AND fecha >= ?
            ORDER BY fecha ASC""",
        params,
    ).fetchall()

    resultados = ["W" if f["result"] == 1 else "L" for f in filas]
    wins = resultados.count("W")
    losses = resultados.count("L")
    total = len(resultados)
    categoria = _categoria_carga(total)

    return {
        "partidos": total,
        "wins": wins,
        "losses": losses,
        "resultados": resultados,  # cronológico ASC -- índice 0 es el más viejo dentro de la ventana
        "categoria": categoria["nombre"],
        "descripcion": categoria["descripcion"],
    }


from research.engines.ajustes_contextuales import (
    historial_para_ajustes as _historial_para_ajustes,
    historial_por_superficie as _historial_por_superficie,
    altura_desde_bio as _altura_desde_bio,
)


def _rendimiento_vs_mano(conn, player_name: str, gender: str, surface: str,
                         minimo_muestra: int = 3) -> dict:
    """
    Rendimiento vs. zurdos / diestros (15/07/2026, a pedido de Maria).
    100% Python -- reusa `mano_dominante` de player_bio (ya se trackea
    desde 13/07/2026 vía RapidAPI, ver scripts/backfill_player_bio.py)
    contra el historial de `rating_history`, cruzando por el nombre del
    rival. Valores guardados en player_bio: 'Right-Handed'/'Left-Handed'
    (el frontend los traduce a Diestro/Zurdo, ver NOMBRE_MANO en
    research.js).

    NO se agrega como factor puntuado del IA Match Analyzer sin hablarlo
    con Maria primero -- los 9 factores ya suman 100 puntos exactos
    (ver assert en analisis_ia.py); esto queda como stat informativo,
    igual que forma_reciente/carga_reciente.

    Con muestra chica (< minimo_muestra partidos contra esa mano) el
    win rate es ruido, no señal -- se marca `confiable: False` en vez
    de mostrar un 100% o 0% engañoso sacado de 1 partido.
    """
    where_surface = "" if surface == "general" else "AND h.surface = ?"
    params_base = [player_name] if surface == "general" else [player_name, surface]

    filas = conn.execute(
        f"""SELECT h.result, b.mano_dominante
            FROM rating_history h
            LEFT JOIN player_bio b ON b.player = h.opponent AND b.gender = ?
            WHERE h.player = ? {where_surface}""",
        [gender, player_name] + (params_base[1:] if surface != "general" else []),
    ).fetchall()

    def _resumen(mano: str) -> dict:
        pertinentes = [f for f in filas if f["mano_dominante"] == mano]
        total = len(pertinentes)
        wins = sum(1 for f in pertinentes if f["result"] == 1)
        return {
            "partidos": total,
            "wins": wins,
            "losses": total - wins,
            "win_rate": round(wins / total, 3) if total else None,
            "confiable": total >= minimo_muestra,
        }

    return {"vs_zurdos": _resumen("Left-Handed"), "vs_diestros": _resumen("Right-Handed")}


def _rendimiento_por_nivel(conn, player_name: str, surface: str,
                           minimo_muestra: int = 3) -> dict:
    """
    Rendimiento por nivel de torneo (15/07/2026, a pedido de Maria).
    100% Python -- `level` ya viene normalizado a categoría canónica
    para cada partido (ver shared/categories.py), no hace falta tocar
    nada de datos nuevos. Se agrupa directo por ese valor.
    """
    where_surface = "" if surface == "general" else "AND surface = ?"
    params = [player_name] if surface == "general" else [player_name, surface]
    filas = conn.execute(
        f"""SELECT level, result FROM rating_history
            WHERE player = ? {where_surface}""",
        params,
    ).fetchall()

    por_nivel: dict[str, dict] = {}
    for f in filas:
        nivel = f["level"] or "desconocido"
        d = por_nivel.setdefault(nivel, {"partidos": 0, "wins": 0})
        d["partidos"] += 1
        d["wins"] += 1 if f["result"] == 1 else 0

    return {
        nivel: {
            "partidos": d["partidos"], "wins": d["wins"], "losses": d["partidos"] - d["wins"],
            "win_rate": round(d["wins"] / d["partidos"], 3),
            "confiable": d["partidos"] >= minimo_muestra,
        }
        for nivel, d in por_nivel.items()
    }


def _rendimiento_sets_decisivos(conn, player_name: str, gender: str, surface: str,
                                minimo_muestra: int = 3) -> dict:
    """
    Rendimiento en partidos que llegaron a set decisivo (15/07/2026, a
    pedido de Maria). Reusa parse_score_sets() del motor Elo (mismo
    parser que ya usa el sistema para todo lo demás, no uno nuevo).

    "Decisivo" depende del formato del partido: grand_slam masculino
    se juega a 5 sets (decisivo = 5 sets jugados); todo lo demás
    (incluido grand_slam femenino) se juega a 3 (decisivo = 3 sets).
    Como el partido SIEMPRE lo gana quien gana el último set, `result`
    ya nos dice quién ganó ese set decisivo -- no hace falta parsear
    el marcador set por set, solo contar cuántos sets hubo en total.
    """
    where_surface = "" if surface == "general" else "AND surface = ?"
    params = [player_name] if surface == "general" else [player_name, surface]
    filas = conn.execute(
        f"""SELECT level, score, result FROM rating_history
            WHERE player = ? {where_surface}""",
        params,
    ).fetchall()

    partidos = 0
    wins = 0
    for f in filas:
        sets = parse_score_sets(f["score"])
        bo5 = (f["level"] == "grand_slam" and gender.upper() == "M")
        decisivo = len(sets) == (5 if bo5 else 3)
        if decisivo:
            partidos += 1
            wins += 1 if f["result"] == 1 else 0

    return {
        "partidos": partidos, "wins": wins, "losses": partidos - wins,
        "win_rate": round(wins / partidos, 3) if partidos else None,
        "confiable": partidos >= minimo_muestra,
    }


def _rendimiento_vs_bio(conn, player_name: str, gender: str, surface: str,
                        minimo_muestra: int = 3) -> dict:
    """
    Rendimiento vs. rivales más altos/bajos y más jóvenes/mayores
    (15/07/2026, a pedido de Maria) -- mismo patrón que
    _rendimiento_vs_mano(): cruza rating_history.opponent contra
    player_bio, esta vez comparando height_cm y birthday (edad) del
    rival CONTRA los propios del jugador en el momento de calcularlo
    (no en el momento del partido -- limitación aceptada, igual que el
    resto de los stats de bio del sistema, que ya usan la edad actual).
    """
    from research.engines.ajustes_contextuales import edad_desde_birthday

    bio_propia = conn.execute(
        "SELECT height_cm, birthday FROM player_bio WHERE player = ? AND gender = ?",
        (player_name, gender),
    ).fetchone()
    if not bio_propia or bio_propia["height_cm"] is None:
        altura_propia = None
    else:
        altura_propia = bio_propia["height_cm"]
    edad_propia = edad_desde_birthday(bio_propia["birthday"]) if bio_propia else None

    where_surface = "" if surface == "general" else "AND h.surface = ?"
    params = [player_name] if surface == "general" else [player_name, surface]
    filas = conn.execute(
        f"""SELECT h.result, b.height_cm, b.birthday
            FROM rating_history h
            LEFT JOIN player_bio b ON b.player = h.opponent AND b.gender = ?
            WHERE h.player = ? {where_surface}""",
        [gender, player_name] + (params[1:] if surface != "general" else []),
    ).fetchall()

    def _resumen(filtro) -> dict:
        pertinentes = [f for f in filas if filtro(f)]
        total = len(pertinentes)
        wins = sum(1 for f in pertinentes if f["result"] == 1)
        return {
            "partidos": total, "wins": wins, "losses": total - wins,
            "win_rate": round(wins / total, 3) if total else None,
            "confiable": total >= minimo_muestra,
        }

    resultado = {"altura_propia_cm": altura_propia, "edad_propia": edad_propia}

    if altura_propia is not None:
        resultado["vs_mas_altos"] = _resumen(lambda f: f["height_cm"] is not None and f["height_cm"] > altura_propia + 3)
        resultado["vs_mas_bajos"] = _resumen(lambda f: f["height_cm"] is not None and f["height_cm"] < altura_propia - 3)
    if edad_propia is not None:
        resultado["vs_mas_jovenes"] = _resumen(
            lambda f: edad_desde_birthday(f["birthday"]) is not None and edad_desde_birthday(f["birthday"]) < edad_propia - 2
        )
        resultado["vs_mas_grandes"] = _resumen(
            lambda f: edad_desde_birthday(f["birthday"]) is not None and edad_desde_birthday(f["birthday"]) > edad_propia + 2
        )
    return resultado


def _ensure_analisis_ia_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS analisis_ia_cache (
            jugador_a TEXT NOT NULL, jugador_b TEXT NOT NULL,
            gender TEXT NOT NULL, surface TEXT NOT NULL,
            diagnostico_json TEXT NOT NULL,
            prompt_texto TEXT NOT NULL,
            narrativa TEXT,
            proveedor_conectado INTEGER NOT NULL DEFAULT 0,
            generado_en TEXT NOT NULL,
            PRIMARY KEY (jugador_a, jugador_b, gender, surface)
        )
    """)
    # Columnas de la capa de búsqueda (15/07/2026, híbrido de Maria) --
    # ALTER guardado para no romper cachés viejas ya desplegadas.
    # `contexto` (16/07/2026, análisis para Valor en Vivo): 'pre' o
    # 'vivo'. Un análisis pre-partido y uno en vivo del MISMO
    # enfrentamiento NO son intercambiables (el en vivo sabe que el
    # favorito perdió el Set 1 y que las cuotas son repricing en vivo)
    # -- la PK sigue siendo (a, b, gender, surface), así que solo vive
    # UNA versión a la vez: si se pide con un contexto distinto al
    # guardado, se regenera y pisa (aceptable: cuando el partido ya
    # arrancó, el análisis pre quedó viejo de todas formas). NULL en
    # filas anteriores a hoy == 'pre'.
    for columna, tipo in (("capa_busqueda_json", "TEXT"), ("nota_final_json", "TEXT"),
                          ("contexto", "TEXT")):
        try:
            conn.execute(f"ALTER TABLE analisis_ia_cache ADD COLUMN {columna} {tipo}")
        except Exception:
            pass  # ya existe
    conn.commit()


def _cuotas_desde_produccion(nombre_a: str, nombre_b: str) -> tuple:
    """Busca las cuotas de mercado más recientes de este enfrentamiento
    en la DB de PRODUCCIÓN (fixtures_valor primero -- trae odd1/odd2 de
    los dos jugadores -- y valor_en_vivo como segundo intento). Idea de
    Maria (15/07/2026): "la diferencia la puede tomar de producción" --
    la cuota ya vive ahí, no hay que pedirla de nuevo. Devuelve
    (cuota_a, cuota_b) alineadas a los nombres pedidos; (None, None) si
    no se encuentra nada (no se adivina)."""
    try:
        conn_prod = _get_conn_production()
        try:
            fila = conn_prod.execute("""
                SELECT jugador1_nombre, jugador2_nombre, odd1, odd2 FROM fixtures_valor
                WHERE (jugador1_nombre=? AND jugador2_nombre=?) OR (jugador1_nombre=? AND jugador2_nombre=?)
                ORDER BY actualizado_en DESC LIMIT 1
            """, (nombre_a, nombre_b, nombre_b, nombre_a)).fetchone()
            if fila and fila["odd1"] is not None:
                if fila["jugador1_nombre"] == nombre_a:
                    return fila["odd1"], fila["odd2"]
                return fila["odd2"], fila["odd1"]
            fila = conn_prod.execute("""
                SELECT home, away, cuota_home, cuota_away FROM valor_en_vivo
                WHERE (home=? AND away=?) OR (home=? AND away=?)
                ORDER BY fecha DESC LIMIT 1
            """, (nombre_a, nombre_b, nombre_b, nombre_a)).fetchone()
            if fila and fila["cuota_home"] is not None:
                if fila["home"] == nombre_a:
                    return fila["cuota_home"], fila["cuota_away"]
                return fila["cuota_away"], fila["cuota_home"]
        finally:
            conn_prod.close()
    except Exception:
        pass  # producción no disponible -- sin dato, no se rompe nada
    return None, None


def _posicion_ftr_desde_produccion(nombre_a: str, nombre_b: str, gender: str, surface: str) -> tuple:
    """
    Posición de cada jugador en NUESTRO propio ranking de producción
    (UTR v1, ver /rankings) -- NO el ranking oficial ATP/WTA (16/07/2026,
    a pedido de Maria: "lo tomamos del ranking FTR que tenemos en
    producción" -- reemplaza la idea de salir a buscar el ranking
    oficial en la web, que además ya se había decidido sacar del todo
    el 15/07). Mismo criterio de filtro que el endpoint público
    (min_partidos=5, sin filtro de actividad reciente acá porque un
    jugador que dejó de jugar hace poco igual tiene una posición real
    hoy). Devuelve (pos_a, pos_b); None para el que no aparezca en la
    tabla (pocos partidos, o no existe en producción todavía) -- no se
    adivina ni se aproxima.
    """
    try:
        conn_prod = _get_conn_production()
        try:
            surf = surface if surface in ("hard", "clay", "grass") else "general"
            filas = conn_prod.execute("""
                SELECT p.nombre_canonico AS nombre
                FROM players p JOIN ratings r ON p.player_id = r.player_id
                WHERE r.superficie = ? AND p.genero = ? AND r.partidos_jugados >= 5
                ORDER BY r.rating DESC
            """, (surf, gender.upper())).fetchall()
            orden = [f["nombre"] for f in filas]
            pos_a = (orden.index(nombre_a) + 1) if nombre_a in orden else None
            pos_b = (orden.index(nombre_b) + 1) if nombre_b in orden else None
            return pos_a, pos_b
        finally:
            conn_prod.close()
    except Exception:
        return None, None  # producción no disponible -- sin dato, no se rompe nada


def _ftr_posicion_y_confianza(nombre: str, gender: str, surface: str) -> dict | None:
    """
    Valor FTR -- el RATING crudo de nuestro propio ranking de producción
    (UTR v1, mismo número que ya se ve en la pestaña Ranking FTR, columna
    "ftr-val") -- + nivel de confianza de ESE valor, para mostrar en el
    modal Comparador A vs B debajo del nombre de cada jugador (22/07/2026,
    a pedido de Maria: "colocar el valor FTR debajo del nombre en el
    modal por superficie, si no lo tiene por superficie en general y
    debajo me diga el nivel de confianza" -- después aclaró: "pero no la
    posición, el valor" -- se muestra el rating numérico, la posición
    queda solo como dato secundario por si en algún momento hace falta).

    FALLBACK A GENERAL: si el jugador no tiene ranking en la superficie
    pedida (menos de 5 partidos ahí, mismo piso que el resto del
    ranking), se reintenta en 'general' -- casi siempre va a tener más
    partidos ahí. `superficie_usada` en la respuesta le avisa a Maria
    cuál de las dos terminó mostrándose, para que no piense que es el
    valor de tierra/dura/césped cuando en realidad cayó a general.

    CONFIANZA = partidos_jugados / RATING_WINDOW_MAX_MATCHES (30) de
    production/engines/utr_v1.py -- el motor UTR v1 de producción usa
    EXACTAMENTE esa ventana de 30 partidos para calcular el rating (ver
    la nota completa en calcular_rating_ventana() de ese archivo): con
    menos de 30 partidos recientes en la ventana, el ajuste se aplica
    más suave, así que el propio valor es menos representativo.

    IMPORTANTE (22/07/2026, aclarado por Maria: "los partidos son por
    superficie, no por general, ese valor es por los partidos de esa
    superficie") -- `partidos` es el conteo de partidos jugados EN LA
    SUPERFICIE que terminó usándose (`superficie_usada`), NO el total
    general del jugador. Esto es así porque `ratings.partidos_jugados`
    ya se actualiza por separado por superficie en el motor UTR v1 (ver
    aplicar_partido() en utr_v1.py: el loop `for superf in [surf_key,
    "general"]` incrementa cada fila de superficie de forma
    independiente) -- no hace falta ningún cálculo extra acá, la
    columna ya viene filtrada por `r.superficie = surf` en la query de
    abajo. No confundir con "Partidos: 65" que se muestra más abajo en
    la tarjeta (ese es el total de research Elo, un motor distinto).
    Se expone ese mismo criterio como un % simple (partidos/30, tope
    100%), no como las etiquetas cualitativas de research Elo (que usan
    otra escala, 0-50+) -- son dos motores distintos, con sus propias
    nociones de confianza, y no hay que mezclarlas.

    Devuelve None si el jugador no aparece en producción en NINGUNA
    superficie (ni siquiera en general) -- no se adivina ni se aproxima.
    """
    from production.engines.utr_v1 import RATING_WINDOW_MAX_MATCHES

    surf_pedida = surface if surface in ("hard", "clay", "grass") else "general"
    superficies_a_probar = [surf_pedida] if surf_pedida == "general" else [surf_pedida, "general"]
    try:
        conn_prod = _get_conn_production()
        try:
            for surf in superficies_a_probar:
                fila = conn_prod.execute("""
                    SELECT r.partidos_jugados, r.rating
                    FROM players p JOIN ratings r ON p.player_id = r.player_id
                    WHERE p.nombre_canonico = ? AND p.genero = ? AND r.superficie = ? AND r.partidos_jugados >= 5
                """, (nombre, gender.upper(), surf)).fetchone()
                if not fila:
                    continue
                orden = conn_prod.execute("""
                    SELECT p.nombre_canonico AS nombre
                    FROM players p JOIN ratings r ON p.player_id = r.player_id
                    WHERE r.superficie = ? AND p.genero = ? AND r.partidos_jugados >= 5
                    ORDER BY r.rating DESC
                """, (surf, gender.upper())).fetchall()
                nombres_orden = [f["nombre"] for f in orden]
                if nombre not in nombres_orden:
                    continue
                partidos = fila["partidos_jugados"]
                return {
                    "valor": round(fila["rating"], 2),
                    "posicion": nombres_orden.index(nombre) + 1,
                    "superficie_usada": surf,
                    "partidos": partidos,
                    "confianza_pct": round(min(1.0, partidos / RATING_WINDOW_MAX_MATCHES) * 100),
                }
            return None
        finally:
            conn_prod.close()
    except Exception:
        return None  # producción no disponible -- sin dato, no se rompe nada


def _stats_set1_desde_produccion(nombre_a: str, nombre_b: str) -> dict | None:
    """
    Busca las estadísticas reales del Set 1 de este enfrentamiento en
    la DB de PRODUCCIÓN (valor_en_vivo.stats_set1_json, que el bot
    manda en un segundo request ~6s después del post-set1 -- ver
    /live-signal/stats-set1). Mismo patrón y mismos límites que
    _cuotas_desde_produccion(): match exacto de nombres en los dos
    órdenes, fila más reciente, y si no hay nada se devuelve None sin
    adivinar. Devuelve las stats RE-ORIENTADAS a los nombres pedidos:
    claves *_a para nombre_a y *_b para nombre_b (la fila las guarda
    como home/away -- acá se traducen para que el caller no tenga que
    saber quién era home).
    """
    try:
        conn_prod = _get_conn_production()
        try:
            fila = conn_prod.execute("""
                SELECT home, away, stats_set1_json FROM valor_en_vivo
                WHERE ((home=? AND away=?) OR (home=? AND away=?))
                  AND stats_set1_json IS NOT NULL
                ORDER BY fecha DESC LIMIT 1
            """, (nombre_a, nombre_b, nombre_b, nombre_a)).fetchone()
            if not fila:
                return None
            stats = json.loads(fila["stats_set1_json"])
            a_es_home = fila["home"] == nombre_a
            out = {}
            for clave, valor in stats.items():
                if clave.endswith("_home"):
                    out[clave[:-5] + ("_a" if a_es_home else "_b")] = valor
                elif clave.endswith("_away"):
                    out[clave[:-5] + ("_b" if a_es_home else "_a")] = valor
            return out or None
        finally:
            conn_prod.close()
    except Exception:
        return None  # producción no disponible o JSON roto -- sin dato, no se rompe nada


def _recolectar_datos_analisis_ia(conn, row_a, row_b, gender: str, surface: str,
                                  cuota_mercado=None) -> dict:
    """
    Junta lo que ya está calculado en el resto del panel -- no
    recalcula nada de cero. Ver research/engines/analisis_ia.py para
    qué hace cada pieza con esto. Devuelve el dict que espera
    calcular_diagnostico().
    """
    from research.engines.ajustes_contextuales import (
        calcular_probabilidad_ajustada, historial_para_ajustes, historial_por_superficie, dias_desde,
    )

    historial_general_a = historial_para_ajustes(conn, row_a["name"])
    historial_superficie_a = historial_por_superficie(conn, row_a["name"], surface)
    historial_superficie_b = historial_por_superficie(conn, row_b["name"], surface)

    # FIX (15/07/2026, a raíz de una observación de Maria): _carga_reciente
    # y el historial completo ya se calculaban para el panel Comparador,
    # pero nunca llegaban acá -- la capa de búsqueda terminaba
    # mandando a la IA a buscar en la web "carga física" y "cambio de
    # superficie" cuando ya los teníamos en la base. Ahora se pasan.
    carga_28d_a = _carga_reciente(conn, row_a["name"], surface)
    carga_28d_b = _carga_reciente(conn, row_b["name"], surface)
    # Resumen corto (últimos 5) del historial que YA se había consultado
    # arriba (historial_general_a) -- antes se descartaba todo salvo la
    # fecha del más reciente. Superficie por partido es justo lo que
    # permite ver un cambio de superficie reciente sin salir a buscarlo.
    # 'rival' (16/07/2026, a pedido de Maria: nombrar rivales puntuales
    # en el diagnóstico, no solo "calidad de rivales" genérica) --
    # `opponent` ya vive en rating_history, historial_para_ajustes() lo
    # trae desde hoy. Gratis, sin buscar nada.
    historial_reciente_resumen_a = [
        {"fecha": m["fecha"], "torneo": m["tournament"], "superficie": m["surface"], "nivel": m["level"],
         "rival": m.get("opponent"), "resultado": m["result"]}
        for m in historial_general_a[:5]
    ]

    ajuste = calcular_probabilidad_ajustada(
        elo_a=row_a["elo"], elo_b=row_b["elo"],
        historial_superficie_a=historial_superficie_a, historial_superficie_b=historial_superficie_b,
        dias_desde_ultimo_general_a=dias_desde(historial_general_a[0]["fecha"]) if historial_general_a else None,
        dias_desde_ultimo_general_b=None,  # no hace falta para este factor puntual (solo el de A)
        edad_a=None, edad_b=None, altura_a=None, altura_b=None,
        superficie=surface,
    )

    ajuste_racha_a = ajuste["ajustes"]["jugador_a"]["racha"]["value"]
    elo_rival_promedio = None
    recientes = historial_superficie_a[:10]
    if recientes:
        elo_rival_promedio = sum(m["elo_rival"] for m in recientes) / len(recientes)

    # SOS -- Strength of Schedule (16/07/2026, a pedido de Rubén): mismo
    # insumo que elo_rival_promedio de arriba, solo se combina distinto.
    from research.engines.analisis_ia import calcular_sos
    sos = calcular_sos(historial_superficie_a)

    wins_a, losses_a = _wins_losses(conn, row_a["name"], surface)

    # UTR de producción -- best effort, None si no se encuentra a
    # alguno de los dos (no se adivina).
    diferencia_utr = None
    try:
        from production.database.schema import find_player
        conn_prod = _get_conn_production()
        try:
            pa = find_player(conn_prod, row_a["name"], gender)
            pb = find_player(conn_prod, row_b["name"], gender)
            if pa and pb:
                utr_a = (pa.get("ratings") or {}).get(surface if surface != "general" else "hard", {}).get("rating")
                utr_b = (pb.get("ratings") or {}).get(surface if surface != "general" else "hard", {}).get("rating")
                if utr_a is not None and utr_b is not None:
                    diferencia_utr = utr_a - utr_b
        finally:
            conn_prod.close()
    except Exception:
        diferencia_utr = None  # producción no disponible -- se deja sin dato, no se rompe el análisis

    # Movimiento Pinnacle -- best effort contra producción, por nombre.
    movimiento_pinnacle = None
    try:
        conn_prod = _get_conn_production()
        try:
            fila = conn_prod.execute("""
                SELECT severidad, caida_pct, delta_pp, es_valor FROM movimientos_rentables
                WHERE (jugador1=? AND jugador2=?) OR (jugador1=? AND jugador2=?)
                ORDER BY fecha DESC LIMIT 1
            """, (row_a["name"], row_b["name"], row_b["name"], row_a["name"])).fetchone()
            if fila:
                movimiento_pinnacle = dict(fila)
            else:
                # FALLBACK A FIXTURES_VALOR (17/07/2026, a pedido de
                # Maria: "mostrar la cuota de apertura en Pinnacle y la
                # actual" -- movimientos_rentables es la tabla de OTRA
                # estrategia (Movimiento Rentable), casi nunca tiene
                # match para un par de Valor Pre-Partido. fixtures_valor
                # SÍ tiene la apertura real de este mismo partido desde
                # el fix de hoy (odd1_apertura/odd2_apertura, no se
                # pisan en los re-reportes). Se calcula la caída acá
                # mismo -- no hace falta duplicar la función de
                # severidad de movimiento_engine.py para un solo uso.
                fila_fix = conn_prod.execute("""
                    SELECT odd1, odd2, odd1_apertura, odd2_apertura FROM fixtures_valor
                    WHERE (jugador1_nombre=? AND jugador2_nombre=?) OR (jugador1_nombre=? AND jugador2_nombre=?)
                    ORDER BY actualizado_en DESC LIMIT 1
                """, (row_a["name"], row_b["name"], row_b["name"], row_a["name"])).fetchone()
                if fila_fix and fila_fix["odd1_apertura"] and fila_fix["odd2_apertura"]:
                    # ¿row_a es jugador1 o jugador2 de esta fila? -- para
                    # saber qué apertura/actual corresponde a cada lado.
                    es_a_jugador1 = None
                    fila_nombres = conn_prod.execute("""
                        SELECT jugador1_nombre FROM fixtures_valor
                        WHERE (jugador1_nombre=? AND jugador2_nombre=?) OR (jugador1_nombre=? AND jugador2_nombre=?)
                        ORDER BY actualizado_en DESC LIMIT 1
                    """, (row_a["name"], row_b["name"], row_b["name"], row_a["name"])).fetchone()
                    if fila_nombres:
                        es_a_jugador1 = (fila_nombres["jugador1_nombre"] == row_a["name"])
                    apertura_a = fila_fix["odd1_apertura"] if es_a_jugador1 else fila_fix["odd2_apertura"]
                    actual_a = fila_fix["odd1"] if es_a_jugador1 else fila_fix["odd2"]
                    apertura_b = fila_fix["odd2_apertura"] if es_a_jugador1 else fila_fix["odd1_apertura"]
                    actual_b = fila_fix["odd2"] if es_a_jugador1 else fila_fix["odd1"]
                    movimiento_pinnacle = {
                        "severidad": None, "es_valor": None,
                        "apertura_a": apertura_a, "actual_a": actual_a,
                        "apertura_b": apertura_b, "actual_b": actual_b,
                        # caida_pct/delta_pp del lado A, mismo criterio
                        # que movimiento_engine.py (positivo = la cuota
                        # bajó, se volvió más favorito).
                        "caida_pct": round((apertura_a - actual_a) / apertura_a * 100, 1) if apertura_a else None,
                        "delta_pp": None,
                    }
                else:
                    # FALLBACK A PINNODDS_PRICE_HISTORY (18/07/2026, a
                    # pedido de Maria: "el movimiento Pinnacle en Valor
                    # en Vivo siempre sale en ceros" -- un partido que
                    # arrancó y se reportó directo a Valor en Vivo, sin
                    # pasar por Movimiento Rentable (tabla anterior) ni
                    # por Valor Pre-Partido (fixtures_valor), no tiene
                    # fila en ninguna de las dos. pinnodds_price_history
                    # SÍ tiene el historial crudo de cuotas de este
                    # partido si PinnOdds lo vio en algún momento (pre-
                    # partido o ya en vivo, no importa) -- se compara la
                    # PRIMERA cuota vista (apertura, from_price del
                    # primer registro) contra la ÚLTIMA (actual, to_price
                    # del más reciente), por cada lado.
                    fila_hist = conn_prod.execute("""
                        SELECT jugador1 FROM pinnodds_price_history
                        WHERE (jugador1=? AND jugador2=?) OR (jugador1=? AND jugador2=?)
                        ORDER BY id DESC LIMIT 1
                    """, (row_a["name"], row_b["name"], row_b["name"], row_a["name"])).fetchone()
                    if fila_hist:
                        es_a_jugador1 = (fila_hist["jugador1"] == row_a["name"])
                        outcome_a = "Home" if es_a_jugador1 else "Away"
                        outcome_b = "Away" if es_a_jugador1 else "Home"

                        def _apertura_actual(outcome):
                            ap = conn_prod.execute("""
                                SELECT from_price FROM pinnodds_price_history
                                WHERE ((jugador1=? AND jugador2=?) OR (jugador1=? AND jugador2=?))
                                      AND outcome=? ORDER BY id ASC LIMIT 1
                            """, (row_a["name"], row_b["name"], row_b["name"], row_a["name"], outcome)).fetchone()
                            act = conn_prod.execute("""
                                SELECT to_price FROM pinnodds_price_history
                                WHERE ((jugador1=? AND jugador2=?) OR (jugador1=? AND jugador2=?))
                                      AND outcome=? ORDER BY id DESC LIMIT 1
                            """, (row_a["name"], row_b["name"], row_b["name"], row_a["name"], outcome)).fetchone()
                            return (
                                ap["from_price"] if ap else None,
                                act["to_price"] if act else None,
                            )

                        apertura_a, actual_a = _apertura_actual(outcome_a)
                        apertura_b, actual_b = _apertura_actual(outcome_b)
                        if apertura_a is not None and actual_a is not None:
                            movimiento_pinnacle = {
                                "severidad": None, "es_valor": None,
                                "apertura_a": apertura_a, "actual_a": actual_a,
                                "apertura_b": apertura_b, "actual_b": actual_b,
                                "caida_pct": round((apertura_a - actual_a) / apertura_a * 100, 1) if apertura_a else None,
                                "delta_pp": None,
                            }
        finally:
            conn_prod.close()
    except Exception:
        movimiento_pinnacle = None

    return {
        "prob_favorito": ajuste["prob_final_a"],
        "confianza_label": row_a["confidence"] if "confidence" in row_a.keys() else None,
        "confianza_factor": None,  # se resuelve abajo si hay label
        # FIX CUOTA (15/07/2026, reportado por Maria: "la cuota está en
        # pre y en live, ¿la estamos enviando?" -- no la enviábamos, por
        # eso "Valor de cuota --/20" siempre vacío): ahora llega desde la
        # alerta clickeada (frontend) o desde producción (fallback).
        "cuota_mercado": cuota_mercado,
        "cuota_justa": round(1 / ajuste["prob_final_a"], 3) if ajuste["prob_final_a"] > 0 else None,
        "probabilidad": round(ajuste["prob_final_a"], 4),
        "movimiento_pinnacle": movimiento_pinnacle,
        "elo_propio": row_a["elo"],
        "elo_rival_promedio_reciente": elo_rival_promedio,
        "diferencia_utr": diferencia_utr,
        "ajuste_racha_superficie": ajuste_racha_a,
        "dias_desde_ultimo": dias_desde(historial_general_a[0]["fecha"]) if historial_general_a else None,
        "wins_recientes": wins_a, "losses_recientes": losses_a,
        "carga_28d": carga_28d_a,
        "historial_reciente_resumen": historial_reciente_resumen_a,
        "sos": sos,
    }


# ── Concurrencia del IA Match Analyzer (15/07/2026, escalado) ────────
# Dos problemas resueltos acá, pensando en 250-500 usuarios:
#
# 1. ESTAMPIDA DE CACHÉ: sin esto, si 30 usuarios piden el mismo
#    partido no cacheado en el mismo minuto, se disparan 30 llamadas a
#    la API de Anthropic y se paga 30 veces el mismo análisis. El
#    candado garantiza UN solo trabajo por enfrentamiento: el primero
#    genera, los demás reciben "en_proceso" y hacen polling.
#
# 2. ENDPOINT BLOQUEANTE: la capa de búsqueda tarda 30-90s. Antes el
#    endpoint esperaba inline, ocupando un hilo del pool de FastAPI
#    por análisis -- con decenas simultáneos se agotaban los hilos y
#    la app ENTERA dejaba de responder (ranking incluido). Ahora el
#    trabajo corre en un executor propio y acotado (IA_MAX_CONCURRENTES,
#    default 3 -- que además acota el gasto y el rate hacia Anthropic),
#    y el endpoint responde al instante: "completo" o "en_proceso".
#
# NOTA DE DESPLIEGUE: el candado es EN MEMORIA DEL PROCESO -- válido
# porque Railway corre uvicorn con un solo proceso (ver railway.json,
# sin --workers). Si algún día se escala a varios workers/replicas,
# esto hay que moverlo a un candado compartido (fila en SQLite con
# INSERT OR IGNORE, o Redis) -- dejarlo así con múltiples procesos
# reabre la estampida entre procesos.
import threading
from concurrent.futures import ThreadPoolExecutor

IA_MAX_CONCURRENTES = int(os.getenv("IA_MAX_CONCURRENTES", "3"))
_ia_executor = ThreadPoolExecutor(max_workers=IA_MAX_CONCURRENTES, thread_name_prefix="analisis-ia")
_ia_trabajos: dict = {}          # (a, b, gen, surface) -> Future
_ia_trabajos_lock = threading.Lock()


def _armar_respuesta_analisis(paquete: dict, desde_cache: bool) -> dict:
    """Arma la respuesta del endpoint a partir del paquete de UN SOLO
    lado (v3, 15/07/2026) -- ya no recibe `seleccion` porque ya no hay
    nada que elegir: siempre es el favorito, sin importar qué nombre
    se haya mandado como `a` en la URL."""
    return {
        "estado": "completo",
        "seleccion_analizada": paquete.get("favorito"),
        # 'pre' o 'vivo' (16/07/2026) -- filas cacheadas viejas no lo
        # traen, todas eran pre-partido.
        "contexto": paquete.get("contexto", "pre"),
        "diagnostico": paquete["diagnostico"],
        "prompt": paquete.get("prompt_texto"),
        "narrativa": None,
        "capa_busqueda": paquete.get("capa_busqueda"),
        "capa_error": paquete.get("capa_error"),
        "nota": paquete["nota"],
        "proveedor_conectado": bool(paquete.get("capa_busqueda")),
        "desde_cache": desde_cache,
        "generado_en": paquete.get("generado_en"),
        # 16/07/2026: ver nota en el armado de `paquete` más arriba --
        # esto viaja SIEMPRE, no depende de si la capa de búsqueda
        # corrió o falló.
        "contexto_en_vivo": paquete.get("contexto_en_vivo"),
        "movimiento": paquete.get("movimiento"),
    }


def _trabajo_analisis_ia(p1: str, p2: str, gen: str, surface: str,
                         row_1: dict, row_2: dict,
                         cuota_1=None, cuota_2=None, torneo: str | None = None,
                         en_vivo: bool = False,
                         sets_1: int | None = None, sets_2: int | None = None,
                         movimiento: dict | None = None,
                         marcador_set1: str | None = None,
                         stats_partido: dict | None = None,
                         contexto_ftr_elo: dict | None = None) -> dict:
    """Trabajo completo de un análisis -- corre en un hilo del executor,
    NUNCA en el hilo del request. Abre sus propias conexiones y las
    cierra ANTES de la llamada larga a la API.

    UN SOLO LADO (15/07/2026, a pedido de Maria: "para qué vamos a
    armar un diagnóstico por alguien que no vamos a apostar -- tanto
    en vivo como en pre solo se debe crear el diagnóstico basándose en
    el recomendado"). Antes esto era bidireccional (calculaba y
    mostraba las dos selecciones, ver historial de este archivo) --
    ahora se calculan las dos bases determinísticas igual (es
    matemática gratis, y hace falta para SABER cuál de los dos es el
    favorito real por probabilidad ajustada), pero la capa de
    búsqueda -- la parte que cuesta plata y tarda -- se pide UNA SOLA
    VEZ, y solo para el favorito. El resultado es el mismo sin importar
    en qué orden se pida el partido (a, b) o (b, a) -- por eso sigue
    cacheándose por la clave canónica (p1, p2), no por quién se
    clickeó.

    EN VIVO (16/07/2026, para terminar el análisis IA de Valor en
    Vivo): `en_vivo=True` + `sets_1`/`sets_2` (marcador en SETS
    alineado a p1/p2, en Valor en Vivo siempre es 1-0 o 0-1 porque se
    dispara al cierre del Set 1). La base determinística se calcula
    IGUAL que en pre (la probabilidad de FullTenis es pre-partido por
    diseño, es justamente contra lo que se mide la remontada) -- lo
    que cambia es la CAPA DE BÚSQUEDA: se le dice explícitamente que
    el partido está en vivo, quién perdió el Set 1 y que las cuotas
    son repricing en vivo (no de apertura), para que no las lea como
    "valor pre-partido" ni busque previas viejas. Se guarda con
    contexto='vivo' en la caché."""
    from research.engines.capa_busqueda import (
        capa_disponible, ejecutar_capa_busqueda, combinar_nota_final, SaldoInsuficienteError,
    )
    from research.engines.analisis_ia import calcular_diagnostico, construir_prompt

    # Cuotas de mercado: si el frontend no las mandó (o mandó una sola),
    # se completa desde producción, donde ya viven (idea de Maria,
    # 15/07/2026 -- fixtures_valor tiene odd1/odd2, valor_en_vivo tiene
    # cuota_home/cuota_away).
    if cuota_1 is None or cuota_2 is None:
        prod_1, prod_2 = _cuotas_desde_produccion(p1, p2)
        cuota_1 = cuota_1 if cuota_1 is not None else prod_1
        cuota_2 = cuota_2 if cuota_2 is not None else prod_2

    conn = _get_conn()
    try:
        conf_1 = confidence_from_matches(row_1["matches"])
        conf_2 = confidence_from_matches(row_2["matches"])
        r1 = dict(row_1); r1["confidence"] = conf_1[0]
        r2 = dict(row_2); r2["confidence"] = conf_2[0]
        datos_1 = _recolectar_datos_analisis_ia(conn, r1, r2, gen, surface, cuota_mercado=cuota_1)
        datos_2 = _recolectar_datos_analisis_ia(conn, r2, r1, gen, surface, cuota_mercado=cuota_2)
    finally:
        conn.close()
    datos_1["confianza_factor"] = conf_1[1]
    datos_2["confianza_factor"] = conf_2[1]

    # Favorito real = mayor probabilidad ajustada (Elo + contexto), NO
    # necesariamente quien se haya clickeado primero en la UI. Empate
    # exacto (rarísimo, dos probabilidades a 0.5000): se queda con p1
    # por orden canónico, tiene que definirse algo.
    if datos_1["probabilidad"] >= datos_2["probabilidad"]:
        favorito, rival = p1, p2
        datos_favorito, datos_rival = datos_1, datos_2
    else:
        favorito, rival = p2, p1
        datos_favorito, datos_rival = datos_2, datos_1

    diag_favorito = calcular_diagnostico(datos_favorito)
    prompt = construir_prompt(favorito, rival, diag_favorito)

    # FTR + Matchup (31/07/2026, a pedido de Maria) -- reorientar al
    # favorito que ESTA función calculó (Elo + ajustes), que puede no
    # ser exactamente el mismo p1/p2 que decidió el motor FTR+Elo
    # (edge case raro, ya que la estrategia exige que ambos motores
    # coincidan de entrada) -- mismo patrón que sets_1/sets_2 más abajo
    # para contexto_en_vivo.
    contexto_ftr_elo_final = None
    if contexto_ftr_elo:
        ce = dict(contexto_ftr_elo)
        if ce.get("favorito_nombre") and ce["favorito_nombre"] != favorito:
            if ce.get("prob_ftr") is not None:
                ce["prob_ftr"] = 1 - ce["prob_ftr"]
            if ce.get("prob_matchup") is not None:
                ce["prob_matchup"] = 1 - ce["prob_matchup"]
        contexto_ftr_elo_final = ce

    # Posición FTR propia (16/07/2026, a pedido de Maria: "lo tomamos
    # del ranking FTR que tenemos en producción" -- no se busca el
    # ranking oficial ATP/WTA, se usa nuestra propia tabla).
    pos_favorito, pos_rival = _posicion_ftr_desde_produccion(favorito, rival, gen, surface)
    posiciones_ftr = {"favorito": pos_favorito, "rival": pos_rival}

    # SOS -- Strength of Schedule (16/07/2026, a pedido de Rubén): ya
    # viene calculado en Python puro dentro de cada datos_1/datos_2.
    sos_favorito, sos_rival = datos_favorito.get("sos"), datos_rival.get("sos")

    # Contexto EN VIVO alineado al favorito (16/07/2026): sets_1/sets_2
    # vienen alineados a p1/p2 -- se reordenan acá al par
    # favorito/rival, que es como los consume el prompt de la capa.
    contexto_en_vivo = None
    if en_vivo:
        if favorito == p1:
            sets_favorito, sets_rival = sets_1, sets_2
        else:
            sets_favorito, sets_rival = sets_2, sets_1
        contexto_en_vivo = {"sets_favorito": sets_favorito, "sets_rival": sets_rival,
                            "marcador_set1": marcador_set1}

        # Stats reales del Set 1 (16/07/2026, a pedido de Maria: "se
        # envían las estadísticas del primer set para el análisis, lo
        # que son BP y demás"). Se leen de producción -- misma idea que
        # las cuotas: el dato ya vive ahí, no hay que pedirlo de nuevo.
        # Llegan ~6s después de la alerta (segundo request del bot),
        # así que para cuando alguien abre el análisis ya están; si no
        # están (partido sin push de stats, nombres que no matchean),
        # el análisis corre igual sin este bloque.
        stats_ab = _stats_set1_desde_produccion(p1, p2)
        stats_orientadas = {}
        if stats_ab:
            suf_fav, suf_riv = ("_a", "_b") if favorito == p1 else ("_b", "_a")
            for clave, valor in stats_ab.items():
                if clave.endswith(suf_fav):
                    stats_orientadas[clave[:-2] + "_favorito"] = valor
                elif clave.endswith(suf_riv):
                    stats_orientadas[clave[:-2] + "_rival"] = valor

        # stats_partido (16/07/2026, a pedido de Maria: "no tenemos
        # nada de estadística" -- vía Verificador FTR + bot directo,
        # ver core/live_query_server.py). Se usa como FALLBACK si la
        # caché de 30s (stats_ab de arriba) no tenía nada -- esta
        # fuente es aces/dobles/saque/BP del PARTIDO completo, no
        # separado por set como stats_ab, pero es mejor que nada.
        # Nombres base remapeados a los mismos que ya usa el prompt
        # (ETIQUETAS_STATS en capa_busqueda.py) para reusar el mismo
        # renderizado sin duplicar nada ahí.
        if not stats_orientadas and stats_partido:
            MAPEO_NOMBRES = {
                "aces": "aces", "dobles_faltas": "double_faults",
                "primer_saque_pct": "first_srv_pct", "bp_conversion_pct": "bp_conv_pct",
                # 17/07/2026: del punto a punto real (/event/pbp, Tennis
                # Live API) -- bp_convertidos = "breaks" (romper el saque
                # rival ES convertir un BP restando, mismo número).
                "bp_creados": "bp_creados", "bp_convertidos": "breaks",
                "holds": "holds", "srv_games_won_pct": "srv_games_won_pct",
            }
            stats_fav_raw = stats_partido.get("1" if favorito == p1 else "2") or {}
            stats_riv_raw = stats_partido.get("2" if favorito == p1 else "1") or {}
            for base_partido, base_prompt in MAPEO_NOMBRES.items():
                if stats_fav_raw.get(base_partido) is not None:
                    stats_orientadas[f"{base_prompt}_favorito"] = stats_fav_raw[base_partido]
                if stats_riv_raw.get(base_partido) is not None:
                    stats_orientadas[f"{base_prompt}_rival"] = stats_riv_raw[base_partido]

        # BP generados, DERIVADO (18/07/2026, a pedido de Maria: "ya
        # tenemos el % de convertidos y los quiebres logrados, con esos
        # dos podemos obtener los generados") -- bp_creados real (del
        # punto a punto, /event/pbp) no siempre está disponible del
        # lado del bot; cuando falta, se estima con la relación inversa
        # de conversión: generados = logrados / (% convertidos / 100).
        # Aproximado a propósito -- bp_conv_pct es acumulado del
        # partido (no separado por Set 1 como breaks), así que esto es
        # una estimación, no el conteo exacto del Set 1; se usa solo
        # como fallback cuando no hay bp_creados real.
        for lado in ("favorito", "rival"):
            clave_creados = f"bp_creados_{lado}"
            clave_breaks  = f"breaks_{lado}"
            clave_conv    = f"bp_conv_pct_{lado}"
            if stats_orientadas.get(clave_creados) is not None:
                continue
            logrados = stats_orientadas.get(clave_breaks)
            conv_pct = stats_orientadas.get(clave_conv)
            if logrados is not None and conv_pct:
                stats_orientadas[clave_creados] = round(logrados / (conv_pct / 100))

        if stats_orientadas:
            contexto_en_vivo["stats_set1"] = stats_orientadas

        # Filtro DBP + Fortaleza de Servicio (19/07/2026, a pedido de
        # Maria: "agreguemoslo al modal para que podamos ver qué fue lo
        # que calculó"). Se recalcula ACÁ, fresco, con los mismos datos
        # que ya se muestran arriba en la tabla de stats_set1 -- NO es
        # el mismo resultado que ya haya quedado guardado en el
        # payload de Telegram (ese solo existe si el filtro alcanzó a
        # correr con las stats a tiempo, 10s después del post-set1);
        # acá se ve siempre que este modal se abra, sirve para
        # confirmar qué habría calculado el filtro incluso si en su
        # momento no llegó a tiempo para decidir la alerta.
        contexto_en_vivo["filtro_remontada"] = None
        if stats_orientadas:
            try:
                from production.engines.filtro_remontada import evaluar_filtro_remontada
                bp_fav = stats_orientadas.get("bp_creados_favorito")
                bp_riv = stats_orientadas.get("bp_creados_rival")
                primer_saque = stats_orientadas.get("first_srv_pct_favorito")
                juegos_fav = stats_orientadas.get("srv_games_won_pct_favorito")
                juegos_riv = stats_orientadas.get("srv_games_won_pct_rival")
                if None not in (bp_fav, bp_riv, primer_saque, juegos_fav, juegos_riv):
                    filtro = evaluar_filtro_remontada(
                        bp_favorito=bp_fav, bp_rival=bp_riv,
                        primer_saque_ganado=primer_saque,
                        juegos_saque_ganados=juegos_fav,
                        dobles_faltas=stats_orientadas.get("double_faults_favorito") or 0,
                        quiebres_recibidos=stats_orientadas.get("breaks_rival") or 0,
                        juegos_saque_rival=juegos_riv,
                        set1_score=marcador_set1,
                    )
                    contexto_en_vivo["filtro_remontada"] = {
                        "clasificacion": filtro.clasificacion_final.value,
                        "dbp": round(filtro.dbp * 100, 1),
                        "pasa_al_spi": filtro.pasa_al_spi,
                        "razon": filtro.razon,
                    }
            except Exception as e:
                log.error(f"[IA-Match-Analyzer] error recalculando filtro_remontada para {p1} vs {p2}: {e}", exc_info=True)

    # Contexto MOVIMIENTO (16/07/2026, análisis IA para Movimiento
    # Rentable + Elo y Descartes): el steam move de la alerta clickeada.
    # Lo único que se resuelve acá es la relación favorecido/favorito:
    # el mercado movió la cuota de UN lado, y ese lado puede coincidir
    # o no con el que recomienda el modelo -- el prompt trata cada caso
    # distinto (coincide = investigar qué sabe el dinero; no coincide =
    # bandera roja explícita). La comparación es por nombre exacto: si
    # el nombre del bot no matchea ninguno de los dos resueltos en Elo,
    # se marca como None y el prompt lo dice sin adivinar.
    contexto_movimiento = None
    if movimiento:
        favorecido = movimiento.get("favorecido")
        if favorecido == favorito:
            coincide = True
        elif favorecido == rival:
            coincide = False
        else:
            coincide = None  # nombre del bot no matchea los resueltos -- no se adivina
        contexto_movimiento = {**movimiento, "favorecido_es_favorito": coincide}

    # ── Capa de búsqueda -- UNA sola llamada, solo para el favorito.
    capa = None
    capa_error = None
    if capa_disponible():
        try:
            # La carga del rival también viaja -- ya se calculó gratis
            # arriba (hacía falta para saber quién es el favorito), así
            # que no hay motivo para no pasarla también.
            carga_conocida = {
                "a": {"carga_28d": datos_favorito.get("carga_28d"), "historial_reciente": datos_favorito.get("historial_reciente_resumen")},
                "b": {"carga_28d": datos_rival.get("carga_28d"), "historial_reciente": datos_rival.get("historial_reciente_resumen")},
            }
            capa = ejecutar_capa_busqueda(favorito, rival, gen, surface, diag_favorito,
                                           carga_conocida=carga_conocida, torneo_conocido=torneo,
                                           contexto_en_vivo=contexto_en_vivo,
                                           contexto_movimiento=contexto_movimiento,
                                           posiciones_ftr=posiciones_ftr,
                                           sos=(sos_favorito, sos_rival),
                                           contexto_ftr_elo=contexto_ftr_elo_final)
        except SaldoInsuficienteError:
            # Marcador fijo (no texto libre) -- el frontend lo detecta
            # por este string exacto para mostrar el aviso accionable
            # de "sin saldo" en vez del genérico de "pensando en el
            # mejor diagnóstico" (ver research.js/common.js). Se loguea
            # aparte para que sea fácil de grepear en Railway sin tener
            # que abrir el modal.
            log.warning(f"[IA-Match-Analyzer] {favorito} vs {rival}: saldo de Anthropic insuficiente")
            capa_error = "SALDO_INSUFICIENTE"
        except Exception as e:  # timeout, API caída, JSON roto, etc.
            # FIX ERROR-SIN-DETALLE (20/07/2026, reportado por Maria: un
            # 400 apareció en el modal como "Client error '400 Bad
            # Request' for url ... For more information check: [link
            # genérico de MDN]" -- sin ninguna pista de la causa real).
            # httpx.HTTPStatusError trae la respuesta completa en
            # `.response`, pero str(e) nunca la incluye -- se agrega acá
            # a mano (recortada, no hace falta el body entero en el
            # modal) para que el próximo 400/4xx sea diagnosticable sin
            # tener que ir a buscar los logs del servidor.
            detalle_extra = ""
            resp = getattr(e, "response", None)
            if resp is not None:
                try:
                    detalle_extra = f" -- {resp.text[:300]}"
                except Exception:
                    pass
            capa_error = f"{type(e).__name__}: {e}{detalle_extra}"
    else:
        capa_error = "ANTHROPIC_API_KEY no configurada -- capa de búsqueda apagada"

    paquete = {
        "v": 3,
        "favorito": favorito,
        "rival": rival,
        "contexto": "vivo" if en_vivo else ("movimiento" if movimiento else "pre"),
        "diagnostico": diag_favorito,
        "nota": combinar_nota_final(diag_favorito, capa),
        "capa_busqueda": capa,
        "capa_error": capa_error,
        "prompt_texto": prompt,
        "generado_en": diag_favorito["generado_en"],
        # contexto_en_vivo (16/07/2026, a pedido de Maria: "en ningún
        # sitio colocamos las estadísticas que le enviamos sobre el
        # partido y eso debe verse") -- antes esto solo vivía adentro
        # del prompt de texto (prompt_texto), invisible para el
        # frontend salvo que alguien lo fuera a leer crudo. Ahora
        # viaja aparte, estructurado, para mostrarlo SIEMPRE -- no
        # depende de si la capa de búsqueda corrió o falló (eso es
        # justo lo que se estaba perdiendo cuando había ReadTimeout).
        "contexto_en_vivo": contexto_en_vivo,
        "movimiento": movimiento,
    }

    conn = _get_conn()
    try:
        _ensure_analisis_ia_table(conn)
        conn.execute("""
            INSERT INTO analisis_ia_cache
                (jugador_a, jugador_b, gender, surface, diagnostico_json, prompt_texto,
                 narrativa, proveedor_conectado, generado_en, capa_busqueda_json, nota_final_json,
                 contexto)
            VALUES (?,?,?,?,?,?,NULL,?, datetime('now'),?,?,?)
            ON CONFLICT(jugador_a, jugador_b, gender, surface) DO UPDATE SET
                diagnostico_json=excluded.diagnostico_json, prompt_texto=excluded.prompt_texto,
                narrativa=NULL, proveedor_conectado=excluded.proveedor_conectado,
                generado_en=excluded.generado_en,
                capa_busqueda_json=excluded.capa_busqueda_json,
                nota_final_json=excluded.nota_final_json,
                contexto=excluded.contexto
        """, (p1, p2, gen, surface, json.dumps(paquete), prompt,
              1 if capa else 0,
              json.dumps(capa) if capa else None, None,
              "vivo" if en_vivo else ("movimiento" if movimiento else "pre")))
        conn.commit()
    finally:
        conn.close()

    return paquete


@router.get("/api/analisis-ia")
def api_analisis_ia(
    request: Request,
    token: str = "",
    a: str = Query(...), b: str = Query(...),
    gender: str = Query("M"), surface: str = Query("general"),
    forzar: bool = Query(False, description="Ignora la caché y regenera"),
    cuota_a: float | None = Query(None, description="Cuota de mercado del jugador a (de la alerta clickeada)"),
    cuota_b: float | None = Query(None, description="Cuota de mercado del jugador b (de la alerta clickeada)"),
    torneo: str | None = Query(None, description="Torneo/liga si ya se conoce (de la alerta clickeada) -- evita que la IA lo busque a ciegas"),
    en_vivo: bool = Query(False, description="El partido está EN VIVO (viene de una alerta de Valor en Vivo) -- la capa de búsqueda recibe el contexto y las cuotas se interpretan como repricing en vivo, no de apertura"),
    sets_a: int | None = Query(None, description="Sets ganados por el jugador a (solo con en_vivo=true; en Valor en Vivo es el marcador al cierre del Set 1)"),
    sets_b: int | None = Query(None, description="Sets ganados por el jugador b (solo con en_vivo=true)"),
    marcador_set1: str | None = Query(None, description="Marcador exacto del Set 1 perdido, ej. '6-4' (solo con en_vivo=true)"),
    stats_a: str | None = Query(None, description="JSON con stats del jugador a (aces, dobles_faltas, primer_saque_pct, bp_conversion_pct) -- del bot directo, 16/07/2026"),
    stats_b: str | None = Query(None, description="JSON con stats del jugador b, mismo formato que stats_a"),
    mov_apertura: float | None = Query(None, description="Cuota de apertura del favorecido (alerta de Movimiento Rentable / Descartes)"),
    mov_actual: float | None = Query(None, description="Cuota actual del favorecido tras la caída"),
    mov_caida_pct: float | None = Query(None, description="Caída porcentual del movimiento"),
    mov_delta_pp: float | None = Query(None, description="Cambio de probabilidad implícita en pp"),
    mov_severidad: str | None = Query(None, description="Severidad del movimiento (interesante/fuerte/muy_fuerte)"),
    mov_favorecido: str | None = Query(None, description="Nombre del jugador cuya cuota cayó (a quien el mercado favoreció)"),
):
    """
    "IA Match Analyzer" HÍBRIDO + ASÍNCRONO + BIDIRECCIONAL (15/07/2026).

    Capas: (1) base determinística en Python puro; (2) capa de búsqueda
    web con ajuste ACOTADO por Python (±15 máx., ±5 cobertura baja, 0
    nula). nota_final = (base_normalizada + ajuste) / 10 → "8.7/10".

    BIDIRECCIONAL: la caché y el candado usan la clave CANÓNICA del
    enfrentamiento (nombres RESUELTOS, ordenados alfabéticamente) -- 
    "Pérez vs López" y "López vs Pérez" son el mismo partido, se paga
    UNA vez y cada orden recibe la interpretación de su selección.
    Esto también elimina los duplicados por variantes de tipeo del
    mismo nombre ("perez" vs "Pérez"), porque la clave se arma después
    de resolver el jugador, no con el texto crudo.

    PROTOCOLO DE POLLING: "estado": "completo" → resultado entero;
    "estado": "en_proceso" → reintentar en ~5s con el mismo GET.

    Caché con TTL sincronizado a la retención real de cada tipo de
    alerta (ver valor_cleanup_task.py: 24h para pre-partido y
    movimiento rentable, 12h para en vivo). 10 minutos en vez de eso
    si la capa de búsqueda falló la última vez, para reintentar pronto.
    `forzar=true` saltea la caché pero NO duplica un trabajo en curso.
    """
    _check_token_o_sesion_analisis_ia(request, token)
    if surface not in SURFACES:
        raise HTTPException(status_code=400, detail=f"superficie inválida: {surface}")
    col = _elo_col(surface)

    from research.engines.capa_busqueda import capa_disponible
    # TTL del caché sincronizado con la retención real de cada tipo de
    # alerta (16/07/2026, a pedido de Maria: "el diagnóstico debe durar
    # lo mismo que dura la alerta visualizándose"; REFINADO 17/07/2026,
    # cuando la retención pasó de ser un solo valor global a una por
    # tabla -- ahora este TTL también se elige según el contexto
    # (pre/vivo/movimiento), no un solo número para los tres). Si el
    # día de mañana se cambia RETENCION_HORAS_TABLA, este TTL se mueve
    # solo con ella, en vez de quedar un número viejo dando vueltas acá.
    from production.tasks.valor_cleanup_task import RETENCION_HORAS_TABLA

    gen, mcol = gender.upper(), _matches_col(surface)

    # FIX (15/07/2026, a raíz de un error real reportado por Maria:
    # "Unexpected token 'I', 'Internal S'... is not valid JSON" en el
    # frontend). Causa real: esta sección (resolución de jugadores +
    # lookup de caché) NO estaba envuelta en try/except -- cualquier
    # excepción no prevista acá (el sospechoso más probable: SQLite
    # "database is locked", un problema recurrente ya conocido en este
    # proyecto, ver docs de WAL/busy_timeout) escapaba SIN CONVERTIRSE
    # en HTTPException, y Starlette devuelve en ese caso un 500 con
    # cuerpo de TEXTO PLANO "Internal Server Error" -- el frontend
    # siempre asume que el cuerpo del error es JSON (`(await
    # res.json()).detail`), así que el JSON.parse del propio texto
    # "Internal Server Error" es lo que tira el error visible. Ahora
    # cualquier excepción acá se convierte en HTTPException, que
    # SIEMPRE serializa a JSON -- el frontend ya sabe leer eso bien.
    try:
        conn = _get_conn()
        try:
            _ensure_analisis_ia_table(conn)

            # Resolución de jugadores SIEMPRE primero y sincrónica (es
            # rápida) -- hace falta para la clave canónica, y un nombre mal
            # escrito devuelve 404 al instante, no un "en_proceso" que
            # termina en error async dos polls después.
            def _resolver(nombre):
                candidatos, confiable = _buscar_candidatos(conn, col, mcol, gen, nombre)
                return candidatos[0] if len(candidatos) == 1 and confiable else None

            row_a_raw = _resolver(a)
            row_b_raw = _resolver(b)
            if not row_a_raw or not row_b_raw:
                raise HTTPException(status_code=404, detail="Jugador no encontrado o ambiguo")

            row_a = dict(row_a_raw)
            row_b = dict(row_b_raw)
            nombre_a, nombre_b = row_a["name"], row_b["name"]
            if nombre_a == nombre_b:
                raise HTTPException(status_code=400, detail="Los dos nombres resuelven al mismo jugador")

            # Clave CANÓNICA del enfrentamiento -- independiente del orden
            # en que se pidió y del tipeo exacto. `contexto` (16/07/2026)
            # también entra en la clave del candado: un pedido en vivo no
            # se puede colgar de un trabajo pre en curso (recibiría un
            # análisis sin el contexto del Set 1 perdido) ni al revés.
            # 'movimiento' (16/07/2026, mismo día): tercer contexto, para
            # alertas de Movimiento Rentable / Descartes -- el análisis
            # con el steam move como dato tampoco es intercambiable con
            # un pre "pelado". en_vivo tiene prioridad si viniera con
            # ambos (no debería pasar: Movimiento Rentable es
            # pre-partido por diseño en su versión que alimenta esta
            # pestaña, y el modal manda uno u otro, nunca los dos).
            hay_movimiento = mov_favorecido is not None and (mov_apertura is not None or mov_actual is not None)
            contexto = "vivo" if en_vivo else ("movimiento" if hay_movimiento else "pre")
            p1, p2 = sorted([nombre_a, nombre_b])
            row_1, row_2 = (row_a, row_b) if nombre_a == p1 else (row_b, row_a)
            clave = (p1, p2, gen, surface, contexto)

            # TTL según el contexto (17/07/2026): pre y movimiento comparten
            # la retención de sus tablas (fixtures_valor / movimientos_rentables,
            # 24h cada una); vivo usa la de valor_en_vivo (12h). Mismo mapeo
            # que ya hace _disparar_analisis_ia_automatico del lado de
            # producción -- ver RETENCION_HORAS_TABLA.
            _TABLA_POR_CONTEXTO = {
                "pre": "fixtures_valor", "vivo": "valor_en_vivo", "movimiento": "movimientos_rentables",
            }
            _TTL_ANALISIS_HORAS = RETENCION_HORAS_TABLA.get(_TABLA_POR_CONTEXTO[contexto], 24.0)

            if not forzar:
                # COALESCE(contexto,'pre'): filas cacheadas antes de hoy
                # no tienen la columna cargada -- todas eran pre-partido.
                # Un análisis pre cacheado NO sirve para un pedido en
                # vivo (y viceversa): se regenera y pisa, ver
                # _ensure_analisis_ia_table.
                cacheado = conn.execute("""
                    SELECT * FROM analisis_ia_cache
                    WHERE jugador_a=? AND jugador_b=? AND gender=? AND surface=?
                      AND COALESCE(contexto, 'pre') = ?
                      AND generado_en > datetime('now', ?)
                """, (p1, p2, gen, surface, contexto, f"-{_TTL_ANALISIS_HORAS} hours")).fetchone()
                # FIX (15/07/2026): un resultado cuya capa FALLÓ no puede
                # valer 6 horas -- TTL de 10 minutos para reintentar pronto.
                if cacheado and capa_disponible() and not cacheado["proveedor_conectado"]:
                    aun_fresco = conn.execute("""
                        SELECT 1 FROM analisis_ia_cache
                        WHERE jugador_a=? AND jugador_b=? AND gender=? AND surface=?
                          AND COALESCE(contexto, 'pre') = ?
                          AND generado_en > datetime('now', '-10 minutes')
                    """, (p1, p2, gen, surface, contexto)).fetchone()
                    if not aun_fresco:
                        cacheado = None  # se regenera, con capa incluida
                if cacheado:
                    try:
                        paquete = json.loads(cacheado["diagnostico_json"])
                    except (TypeError, ValueError):
                        paquete = {}
                    if paquete.get("v") == 3 and paquete.get("favorito"):
                        return _armar_respuesta_analisis(paquete, desde_cache=True)
                    # Formato viejo (v2 bidireccional o anterior) -- se
                    # trata como caché vencida y se regenera una única
                    # vez con el formato nuevo de un solo lado.
        finally:
            conn.close()
    except HTTPException:
        raise  # ya viene con el status/detail correcto, no lo pises
    except Exception as e:
        # Acá caía antes el "database is locked" y cualquier otra falla
        # de infraestructura no prevista -- ahora siempre JSON válido.
        raise HTTPException(status_code=500, detail=f"error resolviendo el partido: {type(e).__name__}: {e}")

    # ── Candado anti-estampida + encolado (clave canónica) ───────────
    with _ia_trabajos_lock:
        fut = _ia_trabajos.get(clave)
        if fut is not None:
            if not fut.done():
                return {"estado": "en_proceso", "detalle": "análisis en curso -- reintentá en unos segundos"}
            _ia_trabajos.pop(clave, None)
        else:
            cuota_1 = cuota_a if nombre_a == p1 else cuota_b
            cuota_2 = cuota_b if nombre_a == p1 else cuota_a
            # Sets alineados igual que las cuotas (16/07/2026, en vivo).
            sets_1 = sets_a if nombre_a == p1 else sets_b
            sets_2 = sets_b if nombre_a == p1 else sets_a
            # stats_partido (16/07/2026): mismo criterio de alineación,
            # JSON parseado con tolerancia -- si viene roto o vacío, se
            # sigue sin este dato en vez de romper todo el análisis.
            def _parsear_stats(crudo: str | None) -> dict:
                if not crudo:
                    return {}
                try:
                    return json.loads(crudo)
                except (ValueError, TypeError):
                    return {}
            stats_1_dict = _parsear_stats(stats_a if nombre_a == p1 else stats_b)
            stats_2_dict = _parsear_stats(stats_b if nombre_a == p1 else stats_a)
            stats_partido = {"1": stats_1_dict, "2": stats_2_dict} if (stats_1_dict or stats_2_dict) else None
            # Movimiento (16/07/2026): no necesita alineación a p1/p2 --
            # el favorecido viaja por NOMBRE y el trabajo lo contrasta
            # contra el favorito del modelo recién cuando lo determina.
            movimiento = None
            if hay_movimiento:
                movimiento = {
                    "favorecido": mov_favorecido, "apertura": mov_apertura, "actual": mov_actual,
                    "caida_pct": mov_caida_pct, "delta_pp": mov_delta_pp, "severidad": mov_severidad,
                }
            fut = _ia_executor.submit(_trabajo_analisis_ia, p1, p2, gen, surface, row_1, row_2,
                                      cuota_1, cuota_2, torneo, en_vivo, sets_1, sets_2, movimiento,
                                      marcador_set1, stats_partido)
            _ia_trabajos[clave] = fut
            if capa_disponible():
                return {"estado": "en_proceso", "detalle": "análisis encolado -- buscando contexto en la web"}
            # Sin capa el trabajo es puro cálculo local (<1s) -- se
            # espera inline para no obligar al frontend a un poll de más.

    try:
        paquete = fut.result(timeout=180)
    except Exception as e:
        with _ia_trabajos_lock:
            _ia_trabajos.pop(clave, None)
        raise HTTPException(status_code=500, detail=f"el análisis falló: {type(e).__name__}: {e}")
    with _ia_trabajos_lock:
        _ia_trabajos.pop(clave, None)
    return _armar_respuesta_analisis(paquete, desde_cache=False)


@router.get("/api/compare")
def api_compare(
    request: Request,
    token: str = "",
    a: str = Query(..., description="Nombre jugador A"),
    b: str = Query(..., description="Nombre jugador B"),
    gender: str = Query("M"),
    surface: str = Query("general"),
):
    # Lectura: token de research O sesión con permiso del tab
    # (29/07/2026 -- ver _check_token_o_sesion). Antes era
    # _check_token a secas y las cuentas de usuario, que nunca tienen
    # RESEARCH_TOKEN, recibían 403 y veían la tabla vacía.
    _check_token_o_sesion(request, token, None)
    if surface not in SURFACES:
        raise HTTPException(status_code=400, detail=f"superficie inválida: {surface}")
    col = _elo_col(surface)
    mcol = _matches_col(surface)

    conn = _get_conn()
    try:
        def buscar(nombre):
            # ALIAS (12/08/2026). Este camino llamaba a
            # _buscar_candidatos directo y se salteaba jugadores_alias,
            # que el camino del fixture SÍ resuelve
            # (buscar_unico_confiable). Con "Paulo Andre Saraiva Dos
            # Santos" registrado como variante de "Paulo Saraiva Dos
            # Santos", el fixture encontraba la ficha y el modal no --
            # la misma divergencia que se está cerrando del otro lado,
            # en espejo. Ahora los dos usan la misma función.
            from shared.elo_lookup import buscar_unico_confiable
            return buscar_unico_confiable(conn, col, mcol, gender, nombre)

        row_a = buscar(a)
        row_b = buscar(b)
        if not row_a:
            raise HTTPException(status_code=404, detail=f"No encontrado: {a}")
        if not row_b:
            raise HTTPException(status_code=404, detail=f"No encontrado: {b}")

        prob_a_elo_crudo = _expected_score(row_a["elo"], row_b["elo"])

        # CAPA DE AJUSTES CONTEXTUALES -- RECALIBRADA 14/07/2026 (a
        # pedido de Maria: se volvió a un modelo simple de 4 factores,
        # ver research/engines/ajustes_contextuales.py para el porqué
        # completo). El Elo guardado en `ratings` (y por lo tanto la
        # pestaña Ranking Elo) NO se toca -- esto solo afecta la
        # probabilidad de ESTA comparación.
        from research.engines.ajustes_contextuales import calcular_probabilidad_ajustada, dias_desde, edad_desde_bio

        historial_general_a = _historial_para_ajustes(conn, row_a["name"])
        historial_general_b = _historial_para_ajustes(conn, row_b["name"])
        historial_superficie_a = _historial_por_superficie(conn, row_a["name"], surface)
        historial_superficie_b = _historial_por_superficie(conn, row_b["name"], surface)
        edad_a = edad_desde_bio(conn, row_a["name"], gender)
        edad_b = edad_desde_bio(conn, row_b["name"], gender)
        altura_a = _altura_desde_bio(conn, row_a["name"], gender)
        altura_b = _altura_desde_bio(conn, row_b["name"], gender)
        # Inactividad se mide en GENERAL (cualquier superficie), no
        # filtrada -- por eso usa historial_general, no historial_superficie.
        dias_ultimo_a = dias_desde(historial_general_a[0]["fecha"]) if historial_general_a else None
        dias_ultimo_b = dias_desde(historial_general_b[0]["fecha"]) if historial_general_b else None

        # Índice de Fiabilidad del lado Elo (27/07/2026, AJUSTADA en la
        # tercera vuelta de Rubén -- ver shared/indice_fiabilidad.py
        # para la discusión completa). Ya NO ajusta el rating -- Elo
        # sigue crudo para la capa de ajustes contextuales de siempre.
        # Recién DESPUÉS de tener la probabilidad (con los 4 factores
        # ya aplicados) se la acerca a 50% según esta confianza.
        fiabilidad_elo_a = fiabilidad_elo_b = None
        try:
            from research.engines.ajustes_contextuales import calcular_fiabilidad_elo
            fiabilidad_elo_a = calcular_fiabilidad_elo(conn, row_a["name"], surface, row_a["matches"])
            fiabilidad_elo_b = calcular_fiabilidad_elo(conn, row_b["name"], surface, row_b["matches"])
        except Exception:
            pass  # sin fiabilidad disponible -- sigue sin ajustar, no rompe el modal

        ajuste = calcular_probabilidad_ajustada(
            elo_a=row_a["elo"], elo_b=row_b["elo"],
            historial_superficie_a=historial_superficie_a, historial_superficie_b=historial_superficie_b,
            dias_desde_ultimo_general_a=dias_ultimo_a, dias_desde_ultimo_general_b=dias_ultimo_b,
            edad_a=edad_a, edad_b=edad_b,
            altura_a=altura_a, altura_b=altura_b,
            superficie=surface,
        )
        prob_a_bruta = ajuste["prob_final_a"]
        prob_b_bruta = ajuste["prob_final_b"]

        # Ajuste por confianza (tercera vuelta de Rubén, ver
        # shared/indice_fiabilidad.py::ajustar_probabilidad_por_confianza)
        # -- SOBRE LA PROBABILIDAD, con el mínimo de fiabilidad de la
        # pareja. Sin fiabilidad disponible, prob_a/prob_b quedan
        # iguales a la bruta (equivalente a confianza=1.0).
        confianza_elo_pareja = (
            min(fiabilidad_elo_a["confianza"], fiabilidad_elo_b["confianza"])
            if fiabilidad_elo_a and fiabilidad_elo_b else None
        )
        if confianza_elo_pareja is not None:
            from shared.indice_fiabilidad import ajustar_probabilidad_por_confianza
            prob_a = ajustar_probabilidad_por_confianza(prob_a_bruta, confianza_elo_pareja)
            prob_b = ajustar_probabilidad_por_confianza(prob_b_bruta, confianza_elo_pareja)
        else:
            prob_a, prob_b = prob_a_bruta, prob_b_bruta

        # FIX CONFIANZA-POR-SUPERFICIE (08/07/2026): confidence_label
        # guardado en la tabla es UN SOLO valor calculado sobre
        # matches_total -- si alguien tiene 99 partidos en general pero 0
        # en césped, mostraba "muy_alta" igual, con el Elo de césped
        # siendo solo el valor inicial sin ningún partido real detrás.
        # confidence_from_matches() es la misma función que ya usa el
        # motor para calcular la confianza general -- acá se le pasa el
        # conteo de la superficie filtrada en vez del total.
        confianza_a, _ = confidence_from_matches(row_a["matches"])
        confianza_b, _ = confidence_from_matches(row_b["matches"])

        wins_a, losses_a = _wins_losses(conn, row_a["name"], surface)
        wins_b, losses_b = _wins_losses(conn, row_b["name"], surface)
        forma_a = _forma_reciente(conn, row_a["name"], surface)
        forma_b = _forma_reciente(conn, row_b["name"], surface)
        carga_a = _carga_reciente(conn, row_a["name"], surface)
        carga_b = _carga_reciente(conn, row_b["name"], surface)
        vs_mano_a = _rendimiento_vs_mano(conn, row_a["name"], gender, surface)
        vs_mano_b = _rendimiento_vs_mano(conn, row_b["name"], gender, surface)
        por_nivel_a = _rendimiento_por_nivel(conn, row_a["name"], surface)
        por_nivel_b = _rendimiento_por_nivel(conn, row_b["name"], surface)
        sets_decisivos_a = _rendimiento_sets_decisivos(conn, row_a["name"], gender, surface)
        sets_decisivos_b = _rendimiento_sets_decisivos(conn, row_b["name"], gender, surface)
        vs_bio_a = _rendimiento_vs_bio(conn, row_a["name"], gender, surface)
        vs_bio_b = _rendimiento_vs_bio(conn, row_b["name"], gender, surface)

        # Valor FTR + confianza (22/07/2026, a pedido de Maria) -- ver
        # _ftr_posicion_y_confianza para el detalle completo (fallback a
        # general si no hay ranking en la superficie pedida, confianza
        # sobre la ventana de 30 partidos del motor UTR v1). None si el
        # jugador no aparece en producción en ninguna superficie -- el
        # frontend lo muestra como "sin dato" en ese caso.
        ftr_a = _ftr_posicion_y_confianza(row_a["name"], gender, surface)
        ftr_b = _ftr_posicion_y_confianza(row_b["name"], gender, surface)

        # Probabilidad FTR (25/07/2026, a pedido de Maria: "me gustaría
        # añadir a todos los modales el porcentaje de UTR y de FTR") --
        # segunda probabilidad del modal (la primera, `prob_ganar`,
        # sale de research Elo + ajustes contextuales). Necesita el
        # valor FTR de LOS DOS jugadores en la MISMA superficie (si a
        # uno le tocó el fallback a 'general' y al otro no, igual se
        # comparan -- son los valores que el modal ya está mostrando en
        # pantalla). None si a cualquiera de los dos le falta el dato FTR.
        #
        # 26/07/2026 (mismo día, Maria con datos reales -- Sinner vs
        # Dellien seguía dando 65.9%, muy alejado de la realidad):
        # primero pasó por la curva isotónica (tenía un tramo entero
        # aplastado al mismo %, ver ftr_elo_engine.py VERSIÓN 5), y
        # este endpoint en particular quedó afuera cuando se corrigió
        # eso en la estrategia FTR + Elo -- quedó desalineado. Ahora
        # usa la misma calibración propia que ese motor
        # (shared/ftr_probabilidad.py), independiente de la conversión
        # a escala Elo -- para que el modal y la estrategia siempre
        # muestren el mismo número para el mismo par de jugadores.
        prob_ftr_a = prob_ftr_b = None
        # FIX 500-SIN-FTR (29/07/2026, caso real en logs: Patel M vs
        # Palavestra I): las variantes _bruta se agregaron después (los
        # campos prob_ganar_ftr_bruta del 27-28/07) pero solo se asignan
        # adentro del `if ftr_a and ftr_b` -- con cualquier jugador SIN
        # dato FTR de producción, el return de abajo las referenciaba
        # sin asignar -> UnboundLocalError -> 500 en /research/api/compare
        # y el modal entero sin probabilidad. Ahora arrancan en None,
        # igual que sus hermanas ajustadas.
        prob_ftr_a_bruta = prob_ftr_b_bruta = None
        racha_ftr_a_detalle = racha_ftr_b_detalle = None
        # 29/07/2026 (segundo caso de la misma familia, 500 real en el
        # log de las 03:49 con Patel M vs Palavestra I): fiabilidad_ftr_*
        # se inicializaba ADENTRO del `if ftr_a and ftr_b` de abajo pero
        # el return de esta función la usa SIEMPRE -> UnboundLocalError
        # con cualquier jugador sin FTR de producción. Verificado con un
        # chequeo AST sobre api_compare: con esta línea, NINGUNA variable
        # del bloque condicional queda sin inicializar en el return.
        fiabilidad_ftr_a = fiabilidad_ftr_b = None
        if ftr_a and ftr_b:
            from shared.ftr_probabilidad import probabilidad_ftr

            # Racha FTR (26/07/2026, a pedido de Maria: "y si añadimos
            # eso mismo [racha + calidad de rivales] al cálculo de
            # Prob. FTR") -- mismo criterio que ya usa la estrategia
            # FTR + Elo: usa production/match_log, NO research/
            # rating_history (esa alimenta la racha de Prob. Elo, más
            # arriba), para que las dos probabilidades del modal sigan
            # siendo independientes entre sí. Ver docstring de
            # production/engines/ftr_racha.py para el porqué completo.
            # Ninguna de las dos rachas es obligatoria para mostrar
            # Prob. FTR -- si falla o no hay suficiente historial en
            # producción, sigue con el ftr_a/ftr_b "crudo" de siempre.
            ftr_a_valor, ftr_b_valor = ftr_a["valor"], ftr_b["valor"]
            try:
                from production.engines.ftr_racha import historial_ftr_por_superficie, ajuste_racha_ftr, calcular_fiabilidad_ftr
                conn_prod_racha = _get_conn_production()
                try:
                    fila_a = conn_prod_racha.execute(
                        "SELECT player_id FROM players WHERE nombre_canonico=? AND genero=?",
                        (row_a["name"], gender.upper()),
                    ).fetchone()
                    fila_b = conn_prod_racha.execute(
                        "SELECT player_id FROM players WHERE nombre_canonico=? AND genero=?",
                        (row_b["name"], gender.upper()),
                    ).fetchone()
                    if fila_a and fila_b:
                        # Índice de Fiabilidad (27/07/2026, AJUSTADA en
                        # la tercera vuelta de Rubén -- ver
                        # shared/indice_fiabilidad.py). Ya NO ajusta el
                        # rating -- solo se guarda para más abajo
                        # acercar la PROBABILIDAD a 50%, después de la
                        # racha.
                        try:
                            fiabilidad_ftr_a = calcular_fiabilidad_ftr(conn_prod_racha, fila_a["player_id"], ftr_a["superficie_usada"], ftr_a["partidos"])
                            fiabilidad_ftr_b = calcular_fiabilidad_ftr(conn_prod_racha, fila_b["player_id"], ftr_b["superficie_usada"], ftr_b["partidos"])
                        except Exception:
                            pass  # sin fiabilidad FTR disponible -- sigue sin ajustar

                        hist_a = historial_ftr_por_superficie(conn_prod_racha, fila_a["player_id"], ftr_a["superficie_usada"])
                        hist_b = historial_ftr_por_superficie(conn_prod_racha, fila_b["player_id"], ftr_b["superficie_usada"])
                        racha_ftr_a_detalle = ajuste_racha_ftr(hist_a, ftr_a_valor)
                        racha_ftr_b_detalle = ajuste_racha_ftr(hist_b, ftr_b_valor)
                        ftr_a_valor += racha_ftr_a_detalle["value"]
                        ftr_b_valor += racha_ftr_b_detalle["value"]
                finally:
                    conn_prod_racha.close()
            except Exception:
                pass  # sin racha FTR disponible -- sigue con el valor crudo, no rompe el modal

            delta_ftr_ab = abs(ftr_a_valor - ftr_b_valor)
            prob_favorito_ftr = probabilidad_ftr(delta_ftr_ab)
            if ftr_a_valor >= ftr_b_valor:
                prob_ftr_a_bruta, prob_ftr_b_bruta = prob_favorito_ftr, 1 - prob_favorito_ftr
            else:
                prob_ftr_a_bruta, prob_ftr_b_bruta = 1 - prob_favorito_ftr, prob_favorito_ftr

            # Ajuste por confianza (tercera vuelta de Rubén) -- sobre la
            # PROBABILIDAD, con el mínimo de fiabilidad de la pareja.
            confianza_ftr_pareja = (
                min(fiabilidad_ftr_a["confianza"], fiabilidad_ftr_b["confianza"])
                if fiabilidad_ftr_a and fiabilidad_ftr_b else None
            )
            if confianza_ftr_pareja is not None:
                from shared.indice_fiabilidad import ajustar_probabilidad_por_confianza
                prob_ftr_a = ajustar_probabilidad_por_confianza(prob_ftr_a_bruta, confianza_ftr_pareja)
                prob_ftr_b = ajustar_probabilidad_por_confianza(prob_ftr_b_bruta, confianza_ftr_pareja)
            else:
                prob_ftr_a, prob_ftr_b = prob_ftr_a_bruta, prob_ftr_b_bruta

        return {
            "surface": surface,
            "prob_elo_sin_ajustar_a": round(prob_a_elo_crudo, 4),  # para comparar contra la ajustada
            "ajustes_contextuales": ajuste["diagnostico"],
            "ajustes_contextuales_detalle": ajuste["ajustes"],
            "racha_ftr_detalle": {"jugador_a": racha_ftr_a_detalle, "jugador_b": racha_ftr_b_detalle},
            # ΔUTR (08/10/2026, Rubén): el UTR de los dos y la diferencia
            # (A − B), exacta o como rango si alguno es "11.xx". Lo usan el
            # Comparador, el modal de historial y el Verificador.
            "utr": _utr_par_seguro(row_a["name"], row_b["name"], gender),
            "jugador_a": {
                "name": row_a["name"], "elo": round(row_a["elo"], 2),
                "matches": row_a["matches"], "confidence": confianza_a,
                "prob_ganar": round(prob_a, 4),
                "prob_ganar_bruta": round(prob_a_bruta, 4),
                "prob_ganar_ftr": round(prob_ftr_a, 4) if prob_ftr_a is not None else None,
                "prob_ganar_ftr_bruta": round(prob_ftr_a_bruta, 4) if prob_ftr_a_bruta is not None else None,
                "fiabilidad_elo": fiabilidad_elo_a,
                "fiabilidad_ftr": fiabilidad_ftr_a,
                "cuota_justa": round(1 / prob_a, 3) if prob_a > 0 else None,
                "wins": wins_a, "losses": losses_a,
                "forma_reciente": forma_a,
                "carga_reciente": carga_a,
                "vs_mano": vs_mano_a,
                "por_nivel": por_nivel_a,
                "sets_decisivos": sets_decisivos_a,
                "vs_bio": vs_bio_a,
                "edad": edad_a,
                "altura": altura_a,
                "ftr": ftr_a,
            },
            "jugador_b": {
                "name": row_b["name"], "elo": round(row_b["elo"], 2),
                "matches": row_b["matches"], "confidence": confianza_b,
                "prob_ganar": round(prob_b, 4),
                "prob_ganar_bruta": round(prob_b_bruta, 4),
                "prob_ganar_ftr": round(prob_ftr_b, 4) if prob_ftr_b is not None else None,
                "prob_ganar_ftr_bruta": round(prob_ftr_b_bruta, 4) if prob_ftr_b_bruta is not None else None,
                "fiabilidad_elo": fiabilidad_elo_b,
                "fiabilidad_ftr": fiabilidad_ftr_b,
                "cuota_justa": round(1 / prob_b, 3) if prob_b > 0 else None,
                "wins": wins_b, "losses": losses_b,
                "forma_reciente": forma_b,
                "carga_reciente": carga_b,
                "vs_mano": vs_mano_b,
                "por_nivel": por_nivel_b,
                "sets_decisivos": sets_decisivos_b,
                "vs_bio": vs_bio_b,
                "edad": edad_b,
                "altura": altura_b,
                "ftr": ftr_b,
            },
        }
    finally:
        conn.close()


@router.get("/api/leaderboard")
def api_leaderboard(token: str = ""):
    _check_token(token)
    if not LEADERBOARD_CSV.exists():
        return {"experimentos": [], "nota": f"No se encontró {LEADERBOARD_CSV} — correr tools/run_ftr_lab_fast.py"}
    with open(LEADERBOARD_CSV, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return {"experimentos": rows}


@router.get("/api/ftr-delta")
def api_ftr_delta(token: str = ""):
    """
    Diccionario empírico ΔFTR -> win% histórico del favorito, generado
    offline por scripts/build_ftr_delta_dictionary.py a partir de
    match_log (superficie='general'). Es un archivo estático -- este
    endpoint solo lo lee y lo devuelve, no calcula nada en caliente ni
    toca ninguna DB. Si todavía no se corrió el script, devuelve bins
    vacíos en vez de error (mismo criterio que /api/leaderboard).
    """
    _check_token(token)
    if not FTR_DELTA_DICT_PATH.exists():
        return {
            "bins": [],
            "nota": f"No se encontró {FTR_DELTA_DICT_PATH} — correr scripts/build_ftr_delta_dictionary.py",
        }
    with open(FTR_DELTA_DICT_PATH, encoding="utf-8") as f:
        return json.load(f)


@router.get("/api/ftr-por-cuota")
def api_ftr_por_cuota(
    token: str = "",
    cuota_min: float = Query(1.70, ge=1.0),
    cuota_max: float = Query(1.80, ge=1.0),
    paso: float = Query(0.02, gt=0),
):
    """
    Resumen indexado por cuota justa (no por ΔFTR) -- ver
    shared/ftr_dictionary.py::resumen_por_cuota(). Default 1.70-1.80,
    el rango válido más alto (techo matemático explicado ahí mismo) y
    donde cae la mayor cantidad de partidos.
    """
    _check_token(token)
    from shared.ftr_dictionary import resumen_por_cuota
    return resumen_por_cuota(cuota_min, cuota_max, paso)


@router.get("/api/ftr-resumen")
def api_ftr_resumen(token: str = ""):
    """
    Versión reducida de la curva ΔFTR -> win%: 6 categorías cualitativas
    (acordadas con Rubén, 08/07/2026), con el rango real de win%
    interpolado de la curva isotónica para cada una -- reemplaza a los
    bins fijos de v1 en la UI de research (ver shared/ftr_dictionary.py,
    resumen_categorias()).
    """
    _check_token(token)
    from shared.ftr_dictionary import resumen_categorias, _cargar_curva
    data = _cargar_curva()
    return {
        "total_partidos": data.get("total_partidos", 0),
        "generado_en": data.get("generado_en"),
        "categorias": resumen_categorias(),
    }


@router.get("/api/ftr-curva")
def api_ftr_curva(token: str = ""):
    """
    Curva isotónica cruda (knots de PAVA) -- ver
    scripts/build_ftr_isotonic_curve.py y shared/ftr_dictionary.py.
    Pensado para una futura pestaña "Curva ΔFTR" en research (pendiente,
    sesión 08/07/2026) que grafique esto junto al diccionario binned
    original -- por ahora solo expone los datos crudos.
    """
    _check_token(token)
    from shared.ftr_dictionary import CURVA_PATH
    if not CURVA_PATH.exists():
        return {"curva": [], "nota": f"No se encontró {CURVA_PATH} -- correr scripts/build_ftr_isotonic_curve.py"}
    with open(CURVA_PATH, encoding="utf-8") as f:
        return json.load(f)


@router.get("/api/validator", response_class=PlainTextResponse)
def api_validator(token: str = ""):
    _check_token(token)
    if not VALIDATOR_REPORT.exists():
        return "No se encontró el reporte del Validator — correr tools/validate_elo_v11.py"
    return VALIDATOR_REPORT.read_text(encoding="utf-8")


@router.get("/api/partido-en-vivo")
def api_partido_en_vivo(jugador1: str, jugador2: str, genero: str | None = None, token: str = ""):
    """
    ¿Este partido está en vivo ahora mismo? (16/07/2026, a pedido de
    Maria: "en el Verificador FTR, cuando coloque un partido, si ese
    partido está en vivo, que me traiga todos los datos que tenga y
    me haga un diagnóstico").

    DOS FUENTES, en orden (16/07/2026, agregada la primera a pedido de
    Maria: "preguntarle al bot directo"):

    1. BOT_LIVE_QUERY_URL -- si está configurada, le pregunta AL BOT
       directo por red privada (core/live_query_server.py del lado del
       bot, nuevo). Es instantáneo (el bot ya tiene esto en memoria) y
       CERO costo extra de cuota contra Tennis API -- no es una
       consulta nueva, es leer lo que el bot ya escuchó por
       Socket.IO. Encuentra partidos aunque nunca hayan disparado
       ninguna alerta.
    2. `partidos_en_vivo` (producción) -- fallback si el bot no
       respondió (timeout, servicio caído, o BOT_LIVE_QUERY_URL sin
       configurar). Tiene hasta ~30s de atraso (live_score_pusher.py
       del lado del bot) y solo ve partidos que el bot ya empujó.

    Cruza por NOMBRE normalizado en ambos casos porque el bot y este
    panel no comparten ningún ID en común.

    Devuelve {"en_vivo": false} si no se encontró nada en ninguna de
    las dos -- no es un error, es simplemente que ese partido no está
    en curso (o el bot no lo tiene en su feed de Tennis API).
    """
    _check_token(token)

    resultado = _buscar_en_vivo_directo_bot(jugador1, jugador2)
    if resultado is not None:
        return resultado

    n1, n2 = normalize_name(jugador1), normalize_name(jugador2)
    if not n1 or not n2:
        return {"en_vivo": False}

    conn_prod = _get_conn_production()
    try:
        query = "SELECT * FROM partidos_en_vivo WHERE estado = 'en_vivo'"
        params: list = []
        if genero:
            query += " AND genero = ?"
            params.append(genero)
        query += " ORDER BY ultima_actualizacion DESC"

        for fila in conn_prod.execute(query, params).fetchall():
            h, a = normalize_name(fila["home"]), normalize_name(fila["away"])
            if {h, a} != {n1, n2}:
                continue
            # home/away del bot no necesariamente coinciden en orden
            # con jugador1/jugador2 tal como los escribió Maria -- se
            # alinean acá para que el caller no tenga que adivinar.
            si_jugador1_es_home = (h == n1)
            return {
                "en_vivo": True,
                "fuente": "cache_30s",
                "liga": fila["liga"],
                "set_actual": fila["set_actual"],
                "set1_score": fila["set1_score"],
                "set2_score": fila["set2_score"],
                "set3_score": fila["set3_score"],
                "ultima_actualizacion": fila["ultima_actualizacion"],
                "sets_jugador1": _sets_ganados(fila, si_jugador1_es_home),
                "sets_jugador2": _sets_ganados(fila, not si_jugador1_es_home),
            }
        return {"en_vivo": False}
    finally:
        conn_prod.close()


BOT_LIVE_QUERY_URL = os.getenv("BOT_LIVE_QUERY_URL", "").strip().rstrip("/")


def _buscar_en_vivo_directo_bot(jugador1: str, jugador2: str) -> dict | None:
    """
    Le pregunta al bot directo por core/live_query_server.py (red
    privada de Railway). Devuelve el dict de respuesta ya traducido al
    formato de api_partido_en_vivo, o None si no se pudo consultar
    (BOT_LIVE_QUERY_URL sin configurar, timeout, o cualquier error) --
    None es la señal para que el caller caiga al fallback de caché,
    NO es lo mismo que {"en_vivo": False} (que sí es una respuesta
    válida del bot diciendo que no lo tiene).
    """
    if not BOT_LIVE_QUERY_URL:
        return None
    try:
        import httpx  # lazy: el resto del módulo no lo necesita siempre
        # Timeout corto a propósito -- es red privada dentro del mismo
        # proyecto de Railway, si no responde en 3s algo anda mal del
        # lado del bot y conviene caer al fallback ya, no hacer
        # esperar al usuario por una consulta que debería ser instantánea.
        r = httpx.get(
            f"{BOT_LIVE_QUERY_URL}/live/buscar",
            params={"jugador1": jugador1, "jugador2": jugador2},
            timeout=3,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.debug(f"[PartidoEnVivo] no se pudo consultar al bot directo -- cae a la caché: {e}")
        return None

    if not data.get("en_vivo"):
        return {"en_vivo": False}

    h, a = normalize_name(data.get("home", "")), normalize_name(data.get("away", ""))
    n1 = normalize_name(jugador1)
    si_jugador1_es_home = (h == n1)
    sets1 = data.get("home_sets") if si_jugador1_es_home else data.get("away_sets")
    sets2 = data.get("away_sets") if si_jugador1_es_home else data.get("home_sets")

    # stats (16/07/2026, a pedido de Maria: "no tenemos nada de
    # estadística" -- ahora el bot las manda, se reorientan acá a
    # jugador1/jugador2 igual que el resto de esta respuesta.
    stats_crudas = data.get("stats") or {}
    sufijo_1, sufijo_2 = ("home", "away") if si_jugador1_es_home else ("away", "home")
    stats = {
        "aces_jugador1": stats_crudas.get(f"aces_{sufijo_1}"), "aces_jugador2": stats_crudas.get(f"aces_{sufijo_2}"),
        "dobles_faltas_jugador1": stats_crudas.get(f"dobles_faltas_{sufijo_1}"), "dobles_faltas_jugador2": stats_crudas.get(f"dobles_faltas_{sufijo_2}"),
        "primer_saque_pct_jugador1": stats_crudas.get(f"primer_saque_pct_{sufijo_1}"), "primer_saque_pct_jugador2": stats_crudas.get(f"primer_saque_pct_{sufijo_2}"),
        "bp_conversion_pct_jugador1": stats_crudas.get(f"bp_conversion_pct_{sufijo_1}"), "bp_conversion_pct_jugador2": stats_crudas.get(f"bp_conversion_pct_{sufijo_2}"),
        # 17/07/2026: del punto a punto real (/event/pbp, Tennis Live
        # API) -- mucho más confiable que las de Socket.IO. bp_creados/
        # bp_convertidos/holds/srv_games_won_pct del PARTIDO completo
        # jugado hasta ahora (no solo Set 1).
        "bp_creados_jugador1": stats_crudas.get(f"bp_creados_{sufijo_1}"), "bp_creados_jugador2": stats_crudas.get(f"bp_creados_{sufijo_2}"),
        "bp_convertidos_jugador1": stats_crudas.get(f"bp_convertidos_{sufijo_1}"), "bp_convertidos_jugador2": stats_crudas.get(f"bp_convertidos_{sufijo_2}"),
        "holds_jugador1": stats_crudas.get(f"holds_{sufijo_1}"), "holds_jugador2": stats_crudas.get(f"holds_{sufijo_2}"),
        "srv_games_won_pct_jugador1": stats_crudas.get(f"srv_games_won_pct_{sufijo_1}"), "srv_games_won_pct_jugador2": stats_crudas.get(f"srv_games_won_pct_{sufijo_2}"),
    }

    return {
        "en_vivo": True,
        "fuente": "bot_directo",
        "liga": data.get("liga"),
        "set_actual": data.get("set_actual"),
        "set1_score": data.get("set1_score"),
        "set2_score": data.get("set2_score"),
        "set3_score": data.get("set3_score"),
        "ultima_actualizacion": None,  # es en vivo real, no hace falta -- no viene de una caché con timestamp
        "sets_jugador1": sets1,
        "sets_jugador2": sets2,
        "stats": stats,
    }


def _sets_ganados(fila_partido_en_vivo, es_home: bool) -> int:
    """
    Cuenta sets ganados a partir de set1_score/set2_score/set3_score
    (formato "6-4") -- compara los dos números de cada set cerrado
    para saber quién lo ganó. Sets sin cerrar (None/vacío) no cuentan.
    """
    ganados = 0
    for col in ("set1_score", "set2_score", "set3_score"):
        s = fila_partido_en_vivo[col]
        if not s or "-" not in s:
            continue
        try:
            g_home, g_away = (int(x) for x in s.split("-", 1))
        except ValueError:
            continue
        gano_home = g_home > g_away
        if gano_home == es_home:
            ganados += 1
    return ganados


@router.get("/api/recientes")
def api_recientes(token: str = "", limite: int = Query(20, le=100)):
    """
    Últimos partidos aplicados al motor Elo experimental -- mismo
    propósito que /rankings/recientes del lado oficial: confirmar a
    simple vista que live_sync.py efectivamente está trayendo partidos
    nuevos desde production, y con qué demora. match_id es compartido
    por las 2 filas de un mismo partido (result=1 ganador, result=0
    perdedor), a diferencia de match_log en production que no tiene
    ese id -- acá el join es directo.
    """
    _check_token(token)
    conn = _get_conn()
    try:
        rows = conn.execute(
            """
            SELECT
                w.fecha, w.score, w.level, w.surface,
                w.player AS ganador, w.elo_general_before AS ganador_antes,
                w.elo_general_after AS ganador_despues, w.delta_general AS ganador_delta,
                l.player AS perdedor, l.elo_general_before AS perdedor_antes,
                l.elo_general_after AS perdedor_despues, l.delta_general AS perdedor_delta
            FROM rating_history w
            JOIN rating_history l ON l.match_id = w.match_id AND l.result = 0
            WHERE w.result = 1
            ORDER BY w.fecha DESC
            LIMIT ?
            """,
            (limite,)
        ).fetchall()
        return {"partidos": [dict(r) for r in rows]}
    finally:
        conn.close()


def _norm_nombre_vivo(nombre) -> str:
    """
    Clave de comparación entre el nombre del bot en vivo y el del
    calendario: sin tildes, minúsculas y solo el apellido más largo.
    Las dos fuentes escriben distinto ("Ojakaar O" vs "Oliver Ojakaar"),
    y el apellido es la parte que ninguna abrevia.
    """
    import unicodedata
    n = unicodedata.normalize("NFKD", str(nombre or "")).encode("ascii", "ignore").decode()
    partes = [p for p in n.replace(",", " ").replace(".", " ").split() if len(p) > 2]
    return max(partes, key=len).lower() if partes else ""


@router.get("/api/en-vivo")
def api_en_vivo(token: str = ""):
    """
    Partidos que el bot está viendo en vivo ahora mismo, con su
    marcador -- lee directo de partidos_en_vivo en la DB de producción
    (la llena el bot vía POST /live-signal/marcador y /procesando, ver
    production/api/routes_public.py). Si la tabla todavía no existe
    (ningún partido reportado todavía en este deploy), devuelve lista
    vacía en vez de error -- no es una condición de fallo real.
    """
    _check_token(token)
    conn = _get_conn_production()
    try:
        existe_tabla = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='partidos_en_vivo'"
        ).fetchone()
        if not existe_tabla:
            return {"partidos": []}
        # 09/08/2026: se devuelve el marcador COMPLETO y las
        # estadísticas. Antes solo salían tres sets y el texto "Set 2",
        # así que el panel no podía mostrar el game en curso, el punto,
        # quién saca ni una sola estadística -- aunque el bot ya las
        # empujaba. SELECT * para no tener que tocar esta consulta cada
        # vez que se agrega una columna; las que no existan en una base
        # vieja simplemente no vienen.
        rows = conn.execute(
            """SELECT * FROM partidos_en_vivo
                ORDER BY estado ASC, ultima_actualizacion DESC"""
        ).fetchall()
        partidos = []
        for r in rows:
            d = dict(r)
            # stats y timeline viajan como JSON en la base; se entregan
            # ya parseados para que el front no tenga que hacerlo.
            for campo, destino in (("stats_json", "stats"), ("timeline_json", "timeline")):
                crudo = d.pop(campo, None)
                try:
                    d[destino] = json.loads(crudo) if crudo else None
                except Exception:
                    d[destino] = None
            partidos.append(d)

        # PROBABILIDADES FTR/Elo (09/08/2026) -- salen de fixtures_ftr_elo,
        # la MISMA fila que evalúan las alertas de Valor Pre-Partido. No
        # se recalculan acá: si se recalcularan, el panel podría mostrar
        # un número y la alerta haber usado otro.
        #
        # Se resuelve por nombre porque partidos_en_vivo y
        # fixtures_ftr_elo vienen de fuentes distintas (bot en vivo vs.
        # calendario) y no comparten id. Cuando no matchea se dice por
        # qué -- ese hueco es el mismo que silencia alertas, y conviene
        # que se vea.
        try:
            filas_fx = conn.execute(
                """SELECT jugador1, jugador2, favorito_nombre, prob_ftr, prob_elo,
                          muestra_suficiente
                     FROM fixtures_ftr_elo
                    WHERE fecha >= date('now', '-2 days')"""
            ).fetchall()
            def _clave(a, b):
                return tuple(sorted((_norm_nombre_vivo(a), _norm_nombre_vivo(b))))
            por_par = {}
            for f in filas_fx:
                f = dict(f)
                por_par[_clave(f["jugador1"], f["jugador2"])] = f
            for d in partidos:
                fx = por_par.get(_clave(d.get("home"), d.get("away")))
                if not fx:
                    d["prob_motivo"] = "sin evaluación pre-partido"
                    continue
                if not fx.get("muestra_suficiente"):
                    d["prob_motivo"] = "muestra insuficiente en la superficie"
                d["prob_ftr"] = fx.get("prob_ftr")
                d["prob_elo"] = fx.get("prob_elo")
                d["favorito_nombre"] = fx.get("favorito_nombre")
        except Exception as e:
            log.debug(f"[EnVivo] no se pudieron cruzar las probabilidades: {e}")

        return {"partidos": partidos}
    finally:
        conn.close()


# ── POST SET (26/08/2026, a pedido de Maria) ─────────────────────────
# Umbrales de Over que se miden. Fijos acá (no en config_estrategias) a
# propósito -- no son un corte que decida si algo alerta, son la grilla
# fija de la tabla de acierto histórico, igual que las columnas de una
# planilla no se editan desde Admin. Dos grillas separadas (27/08/2026,
# a pedido de Maria: "que se mostrara el over del set y el over del
# partido") porque son rangos de juegos completamente distintos: un set
# se mueve entre 6 y 13 juegos, un partido entero (2 o 3 sets) entre 12
# y ~30+.
# 26/08/2026, a pedido de Maria: se saca 13.5 del SET -- con tie-break a
# 6-6 (así se juega en ATP/WTA/Challenger/ITF) el techo real de UN set
# es 7-6 = 13 juegos, así que Over 13.5 pide 14+ y da 0% siempre. No es
# un corte que aporte nada, es un techo matemático.
_UMBRALES_POSTSET_SET2 = (8.5, 9.5, 10.5, 11.5, 12.5)
# El partido entero no tiene ese mismo techo (2 o 3 sets se van
# sumando), así que acá sí tiene sentido una grilla más ancha -- rango
# típico de las líneas de "total games" de partido que se usan en el
# mercado.
#
# AMPLIADA 18.5-24.5 -> 18.5-30.5 (27/08/2026, reportado por Rubén:
# "el over de juegos totales de partido serían 26.5"). La grilla vieja
# topaba en 24.5 y estaba apuntando DEBAJO del rango real: en la
# muestra de esos días los totales fueron 16, 17, 21, 24, 25, 25, 27,
# 29, 29 y 35 -- más de la mitad por encima del techo.
#
# El problema no era solo "falta una fila". Como 24.5 era la última,
# TODO lo que pasara de ahí se acumulaba en esa fila: un partido de 35
# juegos contaba igual que uno de 25, y la última línea siempre iba a
# tener el mejor porcentaje por construcción -- la estrella de "mejor
# línea" estaba premiando el borde de la tabla, no una línea buena.
# Mismo razonamiento que ya se aplicó al sacar el 13.5 del Set 2, pero
# al revés: allá el techo era matemático, acá era demasiado bajo.
_UMBRALES_POSTSET_PARTIDO = (18.5, 19.5, 20.5, 21.5, 22.5, 23.5, 24.5,
                             25.5, 26.5, 27.5, 28.5, 29.5, 30.5)


def _games_set2(resultado_score) -> int | None:
    """
    Total de juegos del SET 2 a partir del score final del partido
    (mismo texto que guarda match_log.score, ya reusado por
    parse_score_sets -- ver research/engines/elo_v1_sackmann.py). None
    si el partido no llegó a jugar un Set 2 (walkover / retiro en el
    Set 1) o el score no se pudo parsear -- esas filas NO entran a la
    tabla de acierto, no cuentan ni como Over ni como Under.
    """
    sets = parse_score_sets(resultado_score)
    if len(sets) < 2:
        return None
    a, b = sets[1]
    return a + b


def _games_partido(resultado_score) -> int | None:
    """
    Total de juegos del PARTIDO ENTERO (todos los sets jugados, no solo
    el Set 2) -- mismo score final, mismo parse_score_sets. None si el
    score no se pudo parsear en absoluto; a diferencia de _games_set2,
    acá alcanza con que haya CUALQUIER set (un partido que terminó en
    dos sets sigue siendo un total de partido válido).
    """
    sets = parse_score_sets(resultado_score)
    if not sets:
        return None
    return sum(a + b for a, b in sets)


def _grupo_leve_de_payload(payload_json) -> str:
    """Grupo 1 / 2 / 1_2 de Ventaja leve -- no es columna propia de
    descartes_ia, viaja adentro del payload (ver
    _detectar_ventaja_leve_pierde_set1 en routes_public.py)."""
    try:
        return (json.loads(payload_json or "{}") or {}).get("grupo_leve") or "?"
    except Exception:
        return "?"


def _dedup_partidos_postset(filas: list, conn=None) -> list:
    """
    Colapsa filas que son el MISMO partido real duplicado (26/08/2026,
    bug real reportado por Maria con captura: "Naho Sato vs Viktoria
    Morvayova" tres veces en el tablero).

    Causa: el candado que evita reinsertar (`leve_set1_guardado` /
    `fav_pre_alertado`) está guardado POR FID en partidos_en_vivo, no
    por partido real -- ver _detectar_ventaja_leve_pierde_set1 en
    routes_public.py. Si el feed en vivo le reasigna al MISMO partido
    un fid distinto (reconexión del bot, inestabilidad típica en ITF --
    mismo tipo de problema que ya se dio con fixture_id), cada fid
    nuevo dispara la detección de nuevo y queda OTRA fila en
    descartes_ia / remontada_alertas para el mismo partido. Arreglar la
    estabilidad del fid es un cambio de fondo en el tracking del bot;
    esto se soluciona acá, al leer, que es donde se puede hacer sin
    tocar nada que ya esté en producción.

    IDENTIDAD RESUELTA, NO STRINGS (27/08/2026, reportado por Rubén con
    captura: "Linang Xiao vs Muhamad Rifqi Fitriadi" y "Linang Xiao vs
    Fitriadi M Rifqi" convivían como dos filas, mismo 7-6 y mismos 29
    juegos -- el partido pesaba doble en el porcentaje). La clave vieja
    era normalize_name() de cada nombre, o sea comparación de texto: dos
    variantes del mismo jugador que el feed manda distinto ("Apellido
    Nombre" vs "Nombre Apellido", con o sin el segundo nombre) no
    colapsaban. Ahora la clave es el player_id que resuelve
    find_player_id() -- la MISMA función que usa el resto del proyecto
    para decidir si dos nombres son la misma persona, que es justo el
    caso que sabe resolver (tokens invertidos, apellido + inicial).

    Con `conn=None` (o si un nombre no resuelve a ningún jugador del
    padrón) cae al nombre normalizado de antes: peor, pero nunca peor
    que el comportamiento anterior.

    El par va ORDENADO, así que dos filas del mismo partido con home y
    away invertidos también colapsan -- eso no pierde nada, el favorito
    viaja en su propia columna. Y el DÍA sigue en la clave: una
    revancha real entre los mismos dos jugadores en otra fecha no se
    colapsa. Se queda con la fila de fecha MÁS RECIENTE de cada grupo
    (la que más chances tiene de ya tener ganador_final).
    """
    memo: dict = {}

    def _identidad(nombre, genero):
        """player_id si se puede resolver; si no, el nombre normalizado.
        Memoizado porque find_player_id hace varias consultas por nombre
        y acá se lo llama dos veces por fila."""
        norm = normalize_name(nombre)
        if not norm:
            return ""
        clave_memo = (norm, genero or "")
        if clave_memo not in memo:
            pid = None
            if conn is not None:
                try:
                    pid = find_player_id(conn, nombre, genero, tabla_players="players")
                except Exception:
                    pid = None   # dedup es cosmético: nunca debe tirar abajo el endpoint
            memo[clave_memo] = f"id:{pid}" if pid else f"nom:{norm}"
        return memo[clave_memo]

    por_clave = {}
    for f in filas:
        dia = (f.get("fecha") or "")[:10]
        gen = f.get("genero")
        par = tuple(sorted((_identidad(f.get("home"), gen), _identidad(f.get("away"), gen))))
        clave = (par, dia)
        actual = por_clave.get(clave)
        if actual is None or (f.get("fecha") or "") > (actual.get("fecha") or ""):
            por_clave[clave] = f
    return list(por_clave.values())


def _banda_elo(prob_elo) -> str | None:
    """Bandas de Prob. Elo para la grilla Elo×FTR (30/08/2026, a pedido
    de Maria: "el cruce Elo+FTR que encontró Rubén"). Los cortes (70,
    78) no son arbitrarios -- son los mismos puntos donde el diagnóstico
    de Rubén encontró el quiebre de comportamiento (ver capturas del
    30/08: Elo<70 se comporta distinto de Elo 70-78, que a su vez se
    comporta distinto de Elo>=78). None si no hay dato -- esa fila no
    entra a ninguna celda de la grilla, ni se cuenta como muestra."""
    if prob_elo is None:
        return None
    if prob_elo < 0.70:
        return "<70"
    if prob_elo < 0.78:
        return "70-78"
    return "≥78"


def _banda_ftr(prob_ftr) -> str | None:
    """Bandas de Prob. FTR para la grilla Elo×FTR -- mismos cortes que
    ya usa el resto del proyecto para "fuerte" (VIVO_FTR_MIN/MAX
    default 55/75), no números nuevos inventados para esta grilla."""
    if prob_ftr is None:
        return None
    if prob_ftr < 0.65:
        return "<65"
    if prob_ftr < 0.75:
        return "65-75"
    return "≥75"


_ORDEN_BANDA_ELO = ("<70", "70-78", "≥78")
_ORDEN_BANDA_FTR = ("<65", "65-75", "≥75")


def _resumen_elo_ftr(filas: list, campo: str, umbrales: tuple, minimo_muestra: int = 5) -> dict:
    """Grilla Elo×FTR (30/08/2026): por cada combinación de banda de
    Prob. Elo y Prob. FTR, cuántas veces `campo` (games_set2 o
    games_partido) superó cada umbral -- una grilla por umbral, mismo
    criterio de "una fila por umbral" que ya usa _resumen_over, para no
    reinventar el formato de respuesta a mitad de archivo.

    Por género (mismo bucle que _resumen_over): Rubén encontró que el
    patrón cambia bastante entre Hombres y Mujeres (ver capturas del
    30/08 -- "Mujer + NO PASA" da un patrón mucho más marcado que la
    misma condición en Hombres), así que promediar los dos géneros acá
    sería el mismo error de fondo que se está corrigiendo.

    `minimo_muestra` (default 5, bajo a propósito): con el volumen de
    hoy (recién arrancando esta segmentación) exigir mucho dejaría casi
    toda la grilla vacía. `n < minimo_muestra` igual se devuelve, pero
    marcado con `"floja": true` para que el frontend lo pinte tenue --
    no se esconde el dato, se avisa que es poca muestra, mismo criterio
    que ya usa Post Set con la estrella de "muestra suficiente"."""
    salida = {}
    for genero in ("M", "F"):
        del_genero = [f for f in filas if (f.get("genero") or "M") == genero]
        # Bandeado UNA sola vez por género (no por umbral): la banda de
        # un partido no depende del umbral que se esté mirando.
        baldes = {(be, bf): [] for be in _ORDEN_BANDA_ELO for bf in _ORDEN_BANDA_FTR}
        for f in del_genero:
            if f.get(campo) is None:
                continue
            be, bf = _banda_elo(f.get("prob_elo")), _banda_ftr(f.get("prob_ftr"))
            if be is None or bf is None:
                continue
            baldes[(be, bf)].append(f)
        if not any(baldes.values()):
            continue   # sin ningún dato bandeable para este género -- no mandar una grilla vacía

        por_umbral = []
        for umbral in umbrales:
            grilla = []
            for be in _ORDEN_BANDA_ELO:
                fila = {"banda_elo": be, "celdas": {}}
                for bf in _ORDEN_BANDA_FTR:
                    del_celda = baldes[(be, bf)]
                    n = len(del_celda)
                    aciertos = sum(1 for f in del_celda if f[campo] > umbral)
                    fila["celdas"][bf] = {
                        "n": n, "ganados": aciertos, "perdidos": n - aciertos,
                        "pct": round(aciertos / n * 100, 1) if n else None,
                        "floja": n < minimo_muestra,
                    }
                grilla.append(fila)
            por_umbral.append({"umbral": umbral, "grilla": grilla})
        salida[genero] = {"bandas_elo": list(_ORDEN_BANDA_ELO), "bandas_ftr": list(_ORDEN_BANDA_FTR),
                          "por_umbral": por_umbral}
    return salida


def _resumen_over(filas: list, campo: str, umbrales: tuple) -> dict:
    """
    De una lista de partidos YA resueltos (con 'genero', 'superficie', y
    el campo pedido -- 'games_set2' o 'games_partido') arma la tabla que
    pinta POST SET: por género, por superficie y un Total, cuántas veces
    ese total de juegos pasó cada umbral y el % de acierto -- mismo
    formato que Maria pidió replicar (Clay G-P / Hard G-P / Acierto /
    Total). Genérica en 'campo'/'umbrales' (27/08/2026) para servir
    tanto a "Over Set 2" como a "Over Partido" con la misma función --
    ver _UMBRALES_POSTSET_SET2 / _UMBRALES_POSTSET_PARTIDO.
    """
    salida = {}
    for genero in ("M", "F"):
        del_genero = [f for f in filas
                     if (f.get("genero") or "M") == genero and f.get(campo) is not None]
        if not del_genero:
            continue
        superficies = sorted({f["superficie"] for f in del_genero if f.get("superficie")})
        tabla = []
        for umbral in umbrales:
            fila = {"umbral": umbral, "por_superficie": {}}
            for sup in superficies:
                del_sup = [f for f in del_genero if f["superficie"] == sup]
                n = len(del_sup)
                aciertos = sum(1 for f in del_sup if f[campo] > umbral)
                fila["por_superficie"][sup] = {
                    "ganados": aciertos, "perdidos": n - aciertos,
                    "pct": round(aciertos / n * 100, 1) if n else None,
                }
            n_tot = len(del_genero)
            aciertos_tot = sum(1 for f in del_genero if f[campo] > umbral)
            fila["total"] = {
                "ganados": aciertos_tot, "perdidos": n_tot - aciertos_tot,
                "pct": round(aciertos_tot / n_tot * 100, 1) if n_tot else None,
            }
            tabla.append(fila)
        muestras_por_superficie = {
            sup: len([f for f in del_genero if f["superficie"] == sup]) for sup in superficies
        }
        salida[genero] = {
            "muestras": len(del_genero), "superficies": superficies,
            "muestras_por_superficie": muestras_por_superficie, "tabla": tabla,
        }
    return salida


def _resumen_ambas_lineas(filas: list) -> dict:
    """'set2' y 'partido' lado a lado para el mismo grupo (leve/pasa) --
    ver _resumen_over. Desde el 30/08/2026 suma también la grilla
    Elo×FTR de cada línea (ver _resumen_elo_ftr) -- el desglose que
    encontró Rubén, calculado sobre los MISMOS partidos, no un dataset
    aparte."""
    return {
        "set2": _resumen_over(filas, "games_set2", _UMBRALES_POSTSET_SET2),
        "partido": _resumen_over(filas, "games_partido", _UMBRALES_POSTSET_PARTIDO),
        "elo_ftr_set2": _resumen_elo_ftr(filas, "games_set2", _UMBRALES_POSTSET_SET2),
        "elo_ftr_partido": _resumen_elo_ftr(filas, "games_partido", _UMBRALES_POSTSET_PARTIDO),
    }


# DETALLE DE PARTIDOS (27/08/2026, a pedido de Rubén: "que me diga qué
# partidos son ventaja leve y qué partidos son pasan el filtro"). Las
# tablas de arriba dan porcentajes pero no dejan ver QUÉ partidos los
# formaron -- sin eso no hay forma de auditar un número raro ni de
# cruzar contra una planilla a mano.
#
# El cálculo del histórico usa SIEMPRE todo lo resuelto (ya no hay
# filtro de Periodo, se sacó en este mismo cambio); esto es solo lo que
# se MUESTRA. Se mandan más filas de las que se pintan porque el front
# filtra por Género/Superficie del lado del cliente y después recorta a
# 10 -- si se mandaran 10 justas, filtrar por "Clay" podría dejar 2.
# AMPLIADO 50 -> 500 (30/08/2026). El tope de 50 se puso cuando el
# detalle no se pintaba en ningún lado: mandaba de más "por las dudas".
# Desde que la tabla de detalle se despliega desde CADA celda (ver
# _psTablaDetalle en post_set.html) el detalle dejó de ser informativo y
# pasó a ser la AUDITORÍA del porcentaje de arriba: si la celda dice
# "9 de 25" y el detalle solo puede mostrar 50 filas de un grupo que ya
# tiene más, los dos números dejan de cerrar y el detalle miente sin
# avisar. Con el volumen de hoy (decenas de partidos por grupo) 500 no
# recorta nada; queda el tope igual porque el endpoint no debe poder
# devolver una lista sin techo, y `detalle_total` de abajo permite
# avisar en pantalla cuando efectivamente recorta.
_DETALLE_POSTSET_MAX = 500


def _set1_de_payload(payload_json) -> str | None:
    """Marcador del Set 1 de Ventaja leve -- no tiene columna propia en
    descartes_ia, viaja adentro del payload (mismo caso que
    grupo_leve, ver _grupo_leve_de_payload). El grupo "Pasan el filtro"
    no lo trae ni siquiera en el payload -- ese usa _set1_desde_score."""
    try:
        return (json.loads(payload_json or "{}") or {}).get("set1_score") or None
    except Exception:
        return None


def _set1_desde_score(resultado_score) -> str | None:
    """Marcador del Set 1 reconstruido del score final ("6-4 5-7 7-6(1)"
    -> "6-4"). Para las filas de "Pasan el filtro", que no guardan el
    Set 1 en ninguna columna ni en el payload -- el primer set del
    resultado final ES el Set 1, no hace falta nada más. None mientras
    el partido siga pendiente (no hay score todavía) o si no parsea."""
    sets = parse_score_sets(resultado_score)
    if not sets:
        return None
    a, b = sets[0]
    return f"{a}-{b}"


def _normalizar_grupo_set1(marcador) -> str | None:
    """
    Agrupa un marcador de Set 1 SIN IMPORTAR EL ORDEN (01/09/2026, a
    pedido de Rubén): "4-6" y "6-4" son el mismo grupo -- "6-4"
    (ganador-perdedor, mayor primero). None si `marcador` es None o no
    tiene forma "N-M".

    No hay una lista fija de grupos posibles -- cualquier marcador que
    aparezca se vuelve su propio grupo solo, a propósito ("que los
    grupos nuevos se vayan creando solos", Rubén 01/09/2026). Un 6-0
    que nunca se vio antes simplemente empieza a existir como grupo la
    primera vez que aparece.
    """
    if not marcador:
        return None
    m = re.match(r"(\d+)\s*-\s*(\d+)", str(marcador).strip())
    if not m:
        return None
    a, b = int(m.group(1)), int(m.group(2))
    return f"{max(a, b)}-{min(a, b)}"


def _detalle_resueltos(filas: list) -> list:
    """Los partidos ya resueltos de un grupo, del más nuevo al más
    viejo, en la forma mínima que necesita la tabla de detalle.

    Orden por string y no por fecha parseada: los dos grupos salen hoy
    de descartes_ia, que escribe la fecha siempre igual
    ('YYYY-MM-DD HH:MM:SS', datetime('now') de sqlite), así que el orden
    lexicográfico coincide con el cronológico."""
    ordenadas = sorted(filas, key=lambda f: (f.get("fecha") or ""), reverse=True)
    salida = []
    for f in ordenadas[:_DETALLE_POSTSET_MAX]:
        favorito = f.get("favorito_nombre") or ""
        ganador = f.get("ganador_final") or ""
        salida.append({
            "fecha": f.get("fecha"),
            "home": f.get("home"),
            "away": f.get("away"),
            "favorito_nombre": favorito,
            "genero": f.get("genero"),
            "superficie": f.get("superficie"),
            "grupo_leve": f.get("grupo_leve"),
            "set1_score": f.get("set1_score"),
            "resultado_score": f.get("resultado_score"),
            "games_set2": f.get("games_set2"),
            "games_partido": f.get("games_partido"),
            # PROBS Y BANDAS (30/08/2026). Las probs son las que ya vienen
            # del SELECT (nada nuevo que calcular) y son la única forma de
            # auditar una celda de la grilla Elo×FTR: sin ellas el detalle
            # puede explicar la tabla por superficie pero no el cruce.
            #
            # Las BANDAS se mandan calculadas desde acá, con las mismas
            # _banda_elo/_banda_ftr que usa _resumen_elo_ftr, en vez de
            # dejar que el front las derive de la prob. Si el front
            # repitiera los cortes (0.70/0.78, 0.65/0.75) habría dos
            # copias del mismo número y el día que se muevan los cortes
            # -- que es justo lo que quedó pendiente de decidir con
            # Rubén -- el detalle empezaría a mostrar partidos de otra
            # celda sin que nada falle. None = esta fila no entra a
            # ninguna celda de la grilla, igual que en el cálculo.
            "prob_ftr": f.get("prob_ftr"),
            "prob_elo": f.get("prob_elo"),
            "banda_elo": _banda_elo(f.get("prob_elo")),
            "banda_ftr": _banda_ftr(f.get("prob_ftr")),
            # None (y no False) cuando falta alguno de los dos nombres:
            # "no se sabe" y "el favorito perdió" son cosas distintas y
            # la tabla las pinta distinto.
            "favorito_gano": (normalize_name(favorito) == normalize_name(ganador)
                             if (favorito and ganador) else None),
        })
    return salida


def _games_partido_en_vivo(v: dict) -> int | None:
    """
    Total de juegos del partido acumulados HASTA AHORA (27/08/2026),
    para el tablero en vivo -- suma los sets ya cerrados
    (set1_score..set5_score, cada uno un score de UN set, ej. "4-6") más
    los juegos del set que está en curso (games_home/games_away). A
    diferencia de _games_partido, que lee el score FINAL ya resuelto,
    esto lee el marcador que va empujando el bot mientras el partido
    sigue -- por eso vive separado y no comparte código con el de
    arriba (una es una foto final, la otra un acumulador en movimiento).
    """
    total, tiene_dato = 0, False
    for campo in ("set1_score", "set2_score", "set3_score", "set4_score", "set5_score"):
        s = v.get(campo)
        if s:
            for a, b in parse_score_sets(s):
                total += a + b
                tiene_dato = True
    gh, ga = v.get("games_home"), v.get("games_away")
    if gh is not None and ga is not None:
        total += gh + ga
        tiene_dato = True
    return total if tiene_dato else None


def _archivar_5sets(conn, tipo: str, d: dict) -> None:
    """
    Archiva en `historial_fulltennis` un partido de POST SET que quedó
    afuera del cálculo por ser Grand Slam masculino (mejor de 5 sets) --
    ver el porqué en obtener_resueltos_postset() más abajo.

    01/09/2026, a pedido de Maria: "esos partidos los quiero guardar...
    para en un futuro hacer un análisis, pero por el momento no los
    vamos a usar, pero quiero que se guarde con toda la información".

    NO reusa guardar_en_historial() (production/api/routes_public.py):
    esa función resuelve la identidad del partido POR SU CUENTA (con su
    propia llamada a resolver_fixture), pensada para el momento en que
    una alerta recién dispara y todavía no se sabe el resultado. Acá ya
    tenemos la fila COMPLETA y ya resuelta -- reprocesarla por ese
    camino distinto podría, según el propio historial de bugs de esa
    función (ver su docstring, 28/08/2026), llegar a una identidad
    distinta de la que ya conocemos con certeza. Un INSERT directo,
    protegido por el mismo candado (origen, par_norm, dia_clave) que ya
    usa el resto de Historial FT, es más simple y más confiable acá.

    INSERT OR IGNORE: esta función se llama en cada lectura de
    obtener_resueltos_postset (panel + mensaje de Telegram), así que
    después de la primera vez el candado de abajo hace que sea un
    no-op barato -- no hace falta un flag de "ya archivado" aparte.
    """
    try:
        from production.api.routes_public import _ensure_historial_fulltennis
        from shared.resolver_fixture import norm
        _ensure_historial_fulltennis(conn)

        home, away = d.get("home") or "", d.get("away") or ""
        h_n, a_n = norm(home), norm(away)
        if not h_n or not a_n:
            return
        par_norm = "|".join(sorted((h_n, a_n)))
        fecha = d.get("fecha") or ""
        dia_clave = str(fecha)[:10] or datetime.now(timezone.utc).date().isoformat()
        origen = "post_set_leve_5sets" if tipo == "leve" else "post_set_pasa_5sets"

        payload = json.dumps({
            "games_partido": d.get("games_partido"),
            "prefijo_bloque": "Ventaja Leve" if tipo == "leve" else "Pasan el filtro",
        }, ensure_ascii=False)

        conn.execute("""
            INSERT OR IGNORE INTO historial_fulltennis (
                origen, par_norm, dia_clave, fecha_partido, home, away,
                home_norm, away_norm, torneo, superficie, genero,
                prob_ftr, prob_elo, favorito_nombre,
                ganador_final, resultado_score, motivo, payload_json,
                resuelto_en, cierre_fuente, creado_en
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            origen, par_norm, dia_clave, fecha, home, away, h_n, a_n,
            d.get("liga"), d.get("superficie"), d.get("genero"),
            d.get("prob_ftr"), d.get("prob_elo"), d.get("favorito_nombre"),
            d.get("ganador_final"), d.get("resultado_score"),
            "Excluido de POST SET -- Grand Slam masculino (mejor de 5 sets)",
            payload,
            datetime.now(timezone.utc).isoformat(), "post_set_5sets",
            datetime.now(timezone.utc).isoformat(),
        ))
        conn.commit()
    except Exception as e:
        log.warning(f"[PostSet→HistorialFT] no se pudo archivar "
                    f"{d.get('home')} vs {d.get('away')} ({tipo}): {e}")


def obtener_resueltos_postset(conn) -> dict:
    """
    Extraído de api_post_set() (01/09/2026, a pedido de Maria: mensaje
    de Telegram con el "Valor histórico" de POST SET). Arma y devuelve
    SOLO los partidos YA RESUELTOS de los dos grupos ('leve' y 'pasa'),
    deduplicados -- la misma consulta y el mismo criterio que ya usa el
    panel (ver el docstring largo de api_post_set para el porqué de
    cada JOIN/filtro), separada a función propia para que el mensaje de
    Telegram (production/engines/post_set_mensaje.py) calcule sobre
    EXACTAMENTE los mismos partidos que ve el panel -- panel y mensaje
    no pueden discrepar si leen del mismo lugar.

    No toca pendientes/tablero -- eso lo sigue armando api_post_set,
    que es lo único que lo necesita.

    Devuelve {"leve": [...], "pasa": [...]}, cada fila con 'genero',
    'prob_ftr', 'prob_elo', 'games_partido', 'favorito_nombre',
    'ganador_final', igual que resueltos_leve/resueltos_pasa de abajo.

    EXCLUYE los Grand Slam masculinos (mejor de 5 sets) -- ver
    _es_grand_slam_masculino más abajo.
    """
    from production.api.routes_public import (
        _ensure_descartes_ia_table, _ensure_remontada_alertas_table,
    )
    from production.engines.racha_api import _es_grand_slam_masculino
    _ensure_descartes_ia_table(conn)
    _ensure_remontada_alertas_table(conn)
    _leve = conn.execute(
        """SELECT id, ref_id, home, away, genero, superficie, favorito_nombre,
                  ganador_final, resultado_score, payload_json, fecha,
                  prob_ftr, prob_elo, liga
             FROM descartes_ia WHERE tipo='vivo_leve'"""
    ).fetchall()
    _pasa = conn.execute(
        """SELECT d.id, d.ref_id, d.home, d.away,
                  COALESCE(d.genero, fx.genero)              AS genero,
                  NULLIF(d.superficie, 'general')            AS superficie_propia,
                  fx.superficie                              AS superficie_fixture,
                  COALESCE(NULLIF(d.liga, ''), fx.torneo)    AS liga,
                  d.favorito_nombre,
                  d.ganador_final, d.resultado_score, d.fecha,
                  d.prob_ftr, d.prob_elo
             FROM descartes_ia d
             LEFT JOIN fixtures_ftr_elo fx
                    ON fx.fixture_id = CAST(
                           substr(d.ref_id, instr(d.ref_id, ':') + 1) AS INTEGER)
            WHERE d.tipo='vivo'
              AND (d.ref_id LIKE 'favorito_pre_partido:%'
                   OR d.razon LIKE 'Favorito Pre Partido%')"""
    ).fetchall()

    resueltos_leve = []
    for r in _leve:
        d = dict(r)
        _payload = d.pop("payload_json", None)
        d["games_partido"] = _games_partido(d.get("resultado_score"))
        # GRUPO DE SET 1 (01/09/2026, a pedido de Rubén): "4-6" y "6-4"
        # caen en el mismo grupo -- ver _normalizar_grupo_set1 arriba.
        # Para Ventaja Leve el marcador del Set 1 viaja en el payload
        # (mismo lugar que ya usa _set1_de_payload), así que hay que
        # leerlo ANTES de descartar el payload de arriba.
        # MARCADOR ORIGINAL, ADEMÁS DEL NORMALIZADO (04/09/2026, punto 6
        # del pedido de Rubén: "guardar también el marcador original para
        # saber quién ganó/perdió el Set 1"). El normalizado pone el
        # mayor primero y con eso se pierde de qué lado quedó cada uno;
        # este viene orientado home-away, que es lo que hace falta para
        # identificar al perdedor del Set 1 (ver postset_capas.py).
        d["set1_score"] = _set1_de_payload(_payload)
        d["grupo_set1"] = _normalizar_grupo_set1(d["set1_score"])
        # EXCLUSIÓN DE 5 SETS (01/09/2026, a pedido de Maria: "siento
        # que dañan los números reales que queremos tener") -- los
        # Grand Slam masculinos se juegan al mejor de 5, así que el
        # total de juegos del partido no es comparable con el resto
        # del circuito (bo3). Se saca ACÁ, en la función que alimenta
        # panel Y mensaje de Telegram por igual -- ver
        # _es_grand_slam_masculino en production/engines/racha_api.py,
        # MISMO criterio que ya usa Racha detallada (24/08/2026): por
        # nombre de torneo, porque el feed no manda un campo "mejor_de".
        #
        # No se pierden: se archivan en Historial FT con toda la info
        # (01/09/2026, a pedido de Maria: "los quiero guardar... para en
        # un futuro hacer un análisis") -- ver _archivar_5sets más abajo.
        if _es_grand_slam_masculino(d.get("genero"), d.get("liga")):
            if d.get("ganador_final"):
                _archivar_5sets(conn, "leve", d)
            continue
        if d.get("ganador_final"):
            resueltos_leve.append(d)

    resueltos_pasa = []
    for r in _pasa:
        d = dict(r)
        d["superficie"] = (d.pop("superficie_propia", None)
                          or d.pop("superficie_fixture", None))
        d.pop("superficie_fixture", None)
        d["games_partido"] = _games_partido(d.get("resultado_score"))
        # GRUPO DE SET 1 (01/09/2026, a pedido de Rubén) -- "Pasan el
        # filtro" no guarda el Set 1 en ningún lado, se reconstruye del
        # resultado final (mismo criterio que _set1_desde_score ya usa
        # para el panel).
        # Mismo marcador original que arriba, pero reconstruido del score
        # final -- que match_log guarda orientado al home (ver
        # cerrar_descartes_ia_pendientes: consulta con player_id=home_id),
        # así que "6-4" acá significa 6 juegos del home y 4 del away,
        # igual que el set1_score del payload de Ventaja leve.
        d["set1_score"] = _set1_desde_score(d.get("resultado_score"))
        d["grupo_set1"] = _normalizar_grupo_set1(d["set1_score"])
        if _es_grand_slam_masculino(d.get("genero"), d.get("liga")):
            if d.get("ganador_final"):
                _archivar_5sets(conn, "pasa", d)
            continue
        if d.get("ganador_final"):
            resueltos_pasa.append(d)

    return {
        "leve": _dedup_partidos_postset(resueltos_leve, conn),
        "pasa": _dedup_partidos_postset(resueltos_pasa, conn),
    }


@router.get("/api/post-set")
def api_post_set(request: Request, token: str = ""):
    """
    Pestaña POST SET: con qué frecuencia el Set 2 llega a cada umbral de
    Over, para los partidos donde el favorito perdió el Set 1 estando en
    'Ventaja leve' o 'Pasa el filtro' -- EXACTAMENTE los mismos partidos
    que ya se mandan a Sugerencias (ver _detectar_ventaja_leve_pierde_set1
    y _detectar_favorito_prepartido_pierde_set1 en routes_public.py). No
    hay una tabla nueva para esto: se lee directo de descartes_ia
    (tipo='vivo_leve' para Ventaja leve, tipo='vivo' para Pasan el
    filtro -- ver la nota larga en el SELECT de _pasa sobre por qué NO
    se usa remontada_alertas, que el limpiador borra cada 12h).

    Tres partes:
      - 'historico': % de acierto de Over 8.5-13.5 por grupo/género/
        superficie, sobre TODO el historial resuelto (ganador_final IS
        NOT NULL) -- sin ventana de tiempo desde el 27/08/2026, ver la
        nota donde se arma. Se actualiza SOLA a medida que corre el cierre de 12h
        (cerrar_descartes_ia_pendientes / cerrar_remontada_alertas_pendientes
        en routes_public.py, ver production/tasks/cierre_alertas_task.py)
        -- nada que tocar acá cuando termina un partido nuevo.
      - 'detalle': QUÉ partidos forman ese histórico, uno por fila, del
        más nuevo al más viejo (ver _detalle_resueltos). Es la misma
        lista con la que se calculó el porcentaje, no una consulta
        aparte -- si un número de arriba parece raro, acá están los
        casos que lo produjeron.
      - 'tablero': los mismos partidos que TODAVÍA no tienen
        ganador_final, cruzados por nombre contra el marcador en vivo de
        partidos_en_vivo (mismo cruce que /api/en-vivo) -- para ver en
        qué van los que hoy están jugando el Set 2.
    """
    _check_token_o_sesion(request, token, ["postset"])
    # Asegura tablas/columnas ANTES de leerlas (26/08/2026, bug real:
    # "Error cargando POST SET: Cannot read properties of undefined
    # (reading 'leve')" -- remontada_alertas.superficie/ganador_final/
    # resultado_score se agregan recién cuando corre
    # cerrar_remontada_alertas_pendientes() o se guarda la primera
    # alerta nueva; en un deploy donde el ciclo de 12h todavía no pasó,
    # esas columnas no existían y el SELECT de abajo tiraba "no such
    # column", el endpoint devolvía 500 con {detail: ...} sin
    # 'historico', y el front explotaba leyendo .leve de undefined).
    # Mismo criterio que ya usa cerrar_remontada_alertas_pendientes: se
    # llama acá también porque el endpoint de LECTURA no puede esperar
    # a que corra el cron para poder leer sin reventar.
    from production.api.routes_public import (
        _ensure_descartes_ia_table, _ensure_remontada_alertas_table,
        _ensure_partidos_en_vivo_table,
    )
    from production.engines.racha_api import _es_grand_slam_masculino
    conn = _get_conn_production()
    try:
        _ensure_descartes_ia_table(conn)
        _ensure_remontada_alertas_table(conn)
        _ensure_partidos_en_vivo_table(conn)
        _leve = conn.execute(
            """SELECT id, home, away, genero, superficie, favorito_nombre,
                      ganador_final, resultado_score, payload_json, fecha,
                      prob_ftr, prob_elo, liga
                 FROM descartes_ia WHERE tipo='vivo_leve'"""
        ).fetchall()
        # FUENTE DE "PASAN EL FILTRO" (27/08/2026, encontrado con Rubén).
        # ANTES esto leía remontada_alertas, y esa tabla LA BORRA el
        # limpiador cada 12h (FTR_RETENCION_REMONTADA_HORAS=12, ver
        # production/tasks/valor_cleanup_task.py) -- tenía 5 filas, no
        # porque se hubieran grabado 5 sino porque el resto ya se había
        # purgado, muchas veces antes de que el ciclo de cierre (también
        # de 12h) llegara a escribirles el resultado. Sobre esa tabla el
        # histórico no puede acumularse nunca.
        #
        # _procesar_alerta_remontada() escribe DOS filas por alerta: una
        # en remontada_alertas (con la columna `estrategia`, efímera) y
        # otra en descartes_ia como tipo='vivo' (permanente -- esta tabla
        # no se purga, ver el pedido de Maria del 22/07). O sea que
        # descartes_ia es un superconjunto que sobrevive, y por eso es la
        # fuente correcta: 66 filas desde el 08/08 en vez de 5.
        #
        # OJO: tipo='vivo' NO ES SOLO "Favorito Pre Partido" (31/08/2026,
        # reportado por Rubén: "no entiendo por qué salen esos partidos" --
        # apareció Djokovic vs Navone en los pendientes, GANANDO el Set 1
        # 7-6 y jugando el Set 5, o sea todo lo contrario a "el favorito
        # perdió el Set 1").
        #
        # 'vivo' es el tipo compartido de la familia VALOR EN VIVO: lo
        # escriben _registrar_alerta_historial(tipo="vivo", ...), el ruteo
        # genérico a Sugerencias, y el botón "Descartar" del Historial de
        # Alertas -- todos en routes_public.py. Las alertas post-Set 1 son
        # solo UNA parte de ese tipo.
        #
        # El filtro anterior era
        #   (d.razon IS NULL OR d.razon LIKE 'Favorito Pre Partido%')
        # y la rama `razon IS NULL` era la fuga: CUALQUIER fila tipo='vivo'
        # sin razón cargada entraba al grupo, viniera de donde viniera. El
        # comentario que había acá decía que se había verificado contra
        # producción que las 66 filas eran todas Favorito Pre Partido; eso
        # era cierto ese día y dejó de serlo cuando llegaron alertas de
        # Valor en Vivo con razon en NULL.
        #
        # Ahora se filtra por la FORMA DEL ref_id, que es estructural y no
        # depende de que un campo de texto esté cargado:
        # _procesar_alerta_remontada() lo arma SIEMPRE como
        # f"{estrategia}:{event_id}" (ver /live-signal/remontada en
        # routes_public.py), así que 'favorito_pre_partido:%' identifica
        # exactamente a esta estrategia y a ninguna otra -- ni a las otras
        # 5 remontada del bot, ni a Valor en Vivo, cuyo ref_id es el
        # match_id pelado. Se deja el LIKE por `razon` como segunda vía
        # (no como escape para NULL) por si alguna fila vieja tiene el
        # ref_id en otro formato pero la razón bien cargada.
        #
        # superficie viene HARDCODEADA como 'general' en el INSERT de
        # esas filas (ver _guardar_en_descartes("vivo", ...) en
        # routes_public.py), así que no sirve para las columnas Clay/Hard.
        # Se resuelve acá, al leer, contra fixtures_ftr_elo por el
        # fixture_id que va adentro de ref_id -- mismo cruce que ya hacía
        # el backfill de superficie para remontada_alertas, pero sin
        # escribir nada.
        # OJO CON ref_id (27/08/2026): estas filas NO lo guardan como el
        # fixture_id pelado. _procesar_alerta_remontada() lo arma como
        # ref_id = f"{estrategia}:{event_id}" -- o sea
        # "favorito_pre_partido:12345". Un CAST directo a INTEGER de eso
        # da 0 en SQLite (no empieza con dígito), el JOIN no matchea
        # NUNCA y todas las filas quedan sin superficie: el Total sale
        # bien pero Clay y Hard dan 0 partidos. Por eso se corta en el
        # ':' primero. instr() devuelve 0 cuando no hay ':', y
        # substr(x, 0+1) es la cadena entera -- así que un ref_id que ya
        # fuera numérico también funciona, sin caso especial.
        _pasa = conn.execute(
            """SELECT d.id, d.home, d.away,
                      COALESCE(d.genero, fx.genero)              AS genero,
                      NULLIF(d.superficie, 'general')            AS superficie_propia,
                      fx.superficie                              AS superficie_fixture,
                      COALESCE(NULLIF(d.liga, ''), fx.torneo)    AS liga,
                      d.favorito_nombre,
                      d.ganador_final, d.resultado_score, d.fecha,
                      d.prob_ftr, d.prob_elo
                 FROM descartes_ia d
                 LEFT JOIN fixtures_ftr_elo fx
                        ON fx.fixture_id = CAST(
                               substr(d.ref_id, instr(d.ref_id, ':') + 1) AS INTEGER)
                WHERE d.tipo='vivo'
                  AND (d.ref_id LIKE 'favorito_pre_partido:%'
                       OR d.razon LIKE 'Favorito Pre Partido%')"""
        ).fetchall()

        resueltos_leve, pendientes_leve = [], []
        for r in _leve:
            d = dict(r)
            _payload = d.pop("payload_json", None)
            d["grupo_leve"] = _grupo_leve_de_payload(_payload)
            # 27/08/2026: se saca ACÁ, del mismo payload que ya se lee
            # para grupo_leve, en vez de agregar una columna nueva a
            # descartes_ia -- el dato ya estaba guardado, solo que nadie
            # lo leía (ver _set1_de_payload).
            d["set1_score"] = _set1_de_payload(_payload)
            if d.get("ganador_final"):
                d["games_set2"] = _games_set2(d.get("resultado_score"))
                d["games_partido"] = _games_partido(d.get("resultado_score"))
                # EXCLUSIÓN DE 5 SETS (01/09/2026, a pedido de Maria) --
                # MISMO criterio y misma función que ya usa
                # obtener_resueltos_postset() para el mensaje de
                # Telegram (ver post_set_mensaje.py). FIX el mismo día:
                # esta pestaña tenía su PROPIA consulta, separada de esa
                # función, así que hasta ahora el filtro y el archivado
                # a Historial FT no le llegaban -- el panel seguía
                # mezclando Grand Slam de hombres con el resto.
                if _es_grand_slam_masculino(d.get("genero"), d.get("liga")):
                    _archivar_5sets(conn, "leve", d)
                else:
                    resueltos_leve.append(d)
            else:
                pendientes_leve.append(d)

        resueltos_pasa, pendientes_pasa = [], []
        for r in _pasa:
            d = dict(r)
            # La del fixture manda; la propia solo si el fixture no la
            # tiene (ver la nota del SELECT: 'general' ya vino filtrado
            # a NULL, así que acá "propia" significa una superficie real).
            d["superficie"] = (d.pop("superficie_propia", None)
                              or d.pop("superficie_fixture", None))
            d.pop("superficie_fixture", None)
            # Estas filas no guardan el marcador del Set 1 en ningún
            # lado (el payload de la alerta no lo trae -- claves
            # verificadas contra producción). Se deriva del score final
            # cuando el partido ya cerró: el primer set del resultado ES
            # el Set 1. Mientras esté pendiente queda en None y la tabla
            # muestra un guión, igual que cualquier dato que falta.
            d["set1_score"] = _set1_desde_score(d.get("resultado_score"))
            if d.get("ganador_final"):
                d["games_set2"] = _games_set2(d.get("resultado_score"))
                d["games_partido"] = _games_partido(d.get("resultado_score"))
                if _es_grand_slam_masculino(d.get("genero"), d.get("liga")):
                    _archivar_5sets(conn, "pasa", d)
                else:
                    resueltos_pasa.append(d)
            else:
                pendientes_pasa.append(d)

        # DEDUP (26/08/2026) -- ver _dedup_partidos_postset. Se aplica acá,
        # a las CUATRO listas por separado, antes de armar histórico y
        # tablero -- así ni el % de acierto ni el tablero cuentan dos veces
        # el mismo partido real.
        resueltos_leve = _dedup_partidos_postset(resueltos_leve, conn)
        pendientes_leve = _dedup_partidos_postset(pendientes_leve, conn)
        resueltos_pasa = _dedup_partidos_postset(resueltos_pasa, conn)
        pendientes_pasa = _dedup_partidos_postset(pendientes_pasa, conn)

        # TODO el historial resuelto, sin ventana de tiempo (27/08/2026,
        # a pedido de Rubén). El filtro de Periodo se sacó de la vista:
        # con el volumen actual (decenas de partidos, no miles) recortar
        # a 30 días dejaba las celdas por superficie con muestras de 2 o
        # 3 partidos, y un % sobre 3 casos no dice nada. Si algún día
        # hay volumen para justificarlo, vuelve -- pero como filtro de
        # la TABLA DE DETALLE, no del cálculo.
        historico = {
            "leve": _resumen_ambas_lineas(resueltos_leve),
            "pasa": _resumen_ambas_lineas(resueltos_pasa),
        }
        detalle = {
            "leve": _detalle_resueltos(resueltos_leve),
            "pasa": _detalle_resueltos(resueltos_pasa),
        }
        # Cuántos partidos resueltos hay DE VERDAD en cada grupo, antes
        # del tope de _DETALLE_POSTSET_MAX (30/08/2026). El histórico de
        # arriba se calcula sobre todos; el detalle puede venir recortado.
        # Mandar los dos números deja que la pantalla avise "estás viendo
        # 500 de 640" en vez de que los totales del detalle y los de la
        # celda no cierren sin explicación.
        detalle_total = {
            "leve": len(resueltos_leve),
            "pasa": len(resueltos_pasa),
        }

        # TABLERO -- mismo cruce por nombre normalizado que /api/en-vivo,
        # contra lo que el bot está viendo AHORA MISMO.
        vivos_por_par = {}
        for v in conn.execute("SELECT * FROM partidos_en_vivo WHERE estado='en_vivo'").fetchall():
            v = dict(v)
            clave = tuple(sorted((_norm_nombre_vivo(v.get("home")), _norm_nombre_vivo(v.get("away")))))
            vivos_por_par[clave] = v

        def _con_marcador(pendientes: list, grupo: str) -> list:
            tablero = []
            for d in pendientes:
                clave = tuple(sorted((_norm_nombre_vivo(d.get("home")), _norm_nombre_vivo(d.get("away")))))
                v = vivos_por_par.get(clave)
                fila = {**d, "grupo": grupo, "en_vivo": v is not None}
                if v:
                    fila["set_actual"] = v.get("set_actual")
                    # LOS CINCO SETS (31/08/2026). Antes viajaban solo el 1
                    # y el 2, que alcanzaba cuando el tablero solo miraba
                    # el Set 2. Con la columna de juegos acumulados del
                    # partido en pantalla quedaba incoherente: un partido a
                    # 5 sets mostraba "43 juegos" al lado de dos parciales,
                    # y parecía un error de suma cuando lo que faltaba eran
                    # los sets 3 a 5. _games_partido_en_vivo ya los leía;
                    # solo no se mandaban.
                    for _c in ("set1_score", "set2_score", "set3_score",
                               "set4_score", "set5_score"):
                        fila[_c] = v.get(_c)
                    fila["ultima_actualizacion"] = v.get("ultima_actualizacion")
                    gh, ga = v.get("games_home"), v.get("games_away")
                    # Solo tiene sentido comparar contra los umbrales
                    # mientras el Set 2 está EN CURSO (no cerrado
                    # todavía) -- si ya cerró, set2_score lo dice y esto
                    # queda en None a propósito (ese caso lo cubre
                    # 'historico' apenas cierre ganador_final).
                    en_set2 = (str(v.get("set_actual") or "").strip().endswith("2")
                              and not v.get("set2_score"))
                    fila["games_set2_en_curso"] = (
                        (gh + ga) if (en_set2 and gh is not None and ga is not None) else None
                    )
                    # Juegos de PARTIDO acumulados hasta ahora
                    # (27/08/2026) -- a diferencia del de arriba, este no
                    # se apaga cuando el set en curso cierra: el total
                    # del partido sigue sumando sets nuevos, así que se
                    # muestra mientras el partido siga en 'en_vivo'.
                    fila["games_partido_en_curso"] = _games_partido_en_vivo(v)
                tablero.append(fila)
            return tablero

        tablero = _con_marcador(pendientes_leve, "leve") + _con_marcador(pendientes_pasa, "pasa")
        tablero.sort(key=lambda f: (not f["en_vivo"], f.get("fecha") or ""))

        return {
            "umbrales": {
                "set2": list(_UMBRALES_POSTSET_SET2),
                "partido": list(_UMBRALES_POSTSET_PARTIDO),
            },
            "historico": historico,
            "detalle": detalle,
            "detalle_total": detalle_total,
            "tablero": tablero,
            "actualizado": datetime.now(timezone.utc).isoformat(),
        }
    finally:
        conn.close()


@router.get("/api/partidos-maestros")
def api_partidos_maestros(request: Request, token: str = "",
                          genero: str = "", superficie: str = "",
                          limit: int = Query(200, le=1000)):
    """
    Pestaña PARTIDOS MAESTROS (01/09/2026, a pedido de Maria): lista los
    partidos que calificaron -- ver production/engines/
    partidos_maestros.py para el criterio completo. Cada fixture entra
    UNA sola vez (la tabla se llena ahí, no acá), así que este endpoint
    es puramente de lectura, sin recalcular nada.

    genero/superficie (opcionales): mismo filtro simple que el resto de
    los tabs -- si vienen vacíos, no filtran.
    """
    _check_token_o_sesion(request, token, ["partidosmaestros"])
    from production.engines.partidos_maestros import _ensure_tabla
    conn = _get_conn_production()
    try:
        _ensure_tabla(conn)
        where, args = [], []
        if genero:
            where.append("genero=?")
            args.append(genero)
        if superficie:
            where.append("superficie=?")
            args.append(superficie)
        clausula = f"WHERE {' AND '.join(where)}" if where else ""
        # ORDER BY fecha, no creado_en (06/09/2026, a pedido de Maria:
        # "que lo pueda organizar de la fecha más reciente a la más
        # antigua") -- antes ordenaba por creado_en (cuándo el sistema
        # detectó el partido), pero la columna que se ve en pantalla es
        # `fecha` (cuándo se jugó) -- las dos pueden diferir (un
        # backfill detecta tarde un partido viejo), así que ordenar por
        # una y mostrar la otra se veía como si no estuviera ordenado.
        # `fecha` es ISO (ver _ya_paso() más arriba en este archivo),
        # así que ordena bien como texto sin convertir nada.
        # creado_en queda de segundo criterio, solo para desempatar
        # partidos con la misma fecha exacta.
        filas = [dict(r) for r in conn.execute(
            f"""SELECT fixture_id, fecha, torneo, superficie, genero,
                       jugador1, jugador2, favorito_nombre, cuota_favorito,
                       prob_ftr, prob_elo, ganador_final, resultado_score, creado_en,
                       insignia, regla_version
                  FROM partidos_maestros {clausula}
                 ORDER BY fecha DESC, creado_en DESC LIMIT ?""",
            args + [limit])]
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM partidos_maestros {clausula}", args).fetchone()["n"]
        # Estadísticas (23/09/2026): SOLO la regla congelada vigente y
        # SIN los filtros de género/superficie -- es el marcador de la
        # prueba Maestros vs TOP, no una vista filtrada. Ver
        # partidos_maestros.estadisticas().
        from production.engines.partidos_maestros import estadisticas
        return {"total": total, "partidos": filas,
                "estadisticas": estadisticas(conn),
                "actualizado": datetime.now(timezone.utc).isoformat()}
    finally:
        conn.close()


@router.get("/api/cuota-mal-puesta")
def api_cuota_mal_puesta(request: Request, token: str = "", grupo: str = "",
                         genero: str = "", limit: int = Query(200, le=1000)):
    """
    Pestaña CUOTA MAL PUESTA — DIAGNÓSTICO (07/10/2026, a pedido de Rubén
    Darío). Solo lectura: la tabla la llena production/engines/
    cuota_mal_puesta.py. `estadisticas` es SIEMPRE sobre la versión vigente
    del motor y sin los filtros de la lista (es el marcador, no una vista).

    grupo: ESTUDIO | CMP | PREMIUM | DESCARTE | '' (= todas las señales,
    sin los descartes de control).
    """
    _check_token_o_sesion(request, token, ["cuotamalpuesta"])
    from production.engines.cuota_mal_puesta import _ensure_tabla, estadisticas
    conn = _get_conn_production()
    try:
        _ensure_tabla(conn)
        where, args = [], []
        if grupo:
            where.append("grupo=?")
            args.append(grupo.upper())
        else:
            where.append("grupo<>'DESCARTE'")
        if genero:
            where.append("genero=?")
            args.append(genero)
        clausula = f"WHERE {' AND '.join(where)}"
        filas = [dict(r) for r in conn.execute(
            f"""SELECT id, fixture_id, timestamp_signal, fecha_partido, torneo, circuito,
                       superficie, genero, player_a, player_b, odds_a, odds_b, market_prob,
                       elo_prob, ftr_prob, elo_ftr_alineados, ft_prob_base, l10_score_player,
                       l10_score_opponent, l10_diff, l10_adjustment, l10_suficiente,
                       l10_contradice, l10_confirma, ft_prob_ajustada, ft_fair_odds,
                       mispricing_pp, classification, grupo, discord_nivel, discord_sent,
                       telegram_enviado, telegram_resultado, motivo_no_discord, cuota_cierre_a, clv_pct, ganador_final,
                       resultado_score, result, profit_loss, retiro, version_motor,
                       ajuste_oposicion_pp, carga_jugador, carga_rival, ajuste_carga_pp,
                       mto_jugador_24h, mto_rival_24h, ajuste_mto_pp, recomendado_modelo,
                       publicado_rankingftr, dicho_discord, motivo_no_dicho, motivo_codigos_json
                  FROM cmp_senales {clausula}
                 ORDER BY id DESC LIMIT ?""", args + [limit])]
        total = conn.execute(f"SELECT COUNT(*) AS n FROM cmp_senales {clausula}",
                             args).fetchone()["n"]
        return {"total": total, "senales": filas, "estadisticas": estadisticas(conn),
                "actualizado": datetime.now(timezone.utc).isoformat()}
    finally:
        conn.close()


@router.get("/api/cuota-mal-puesta/excel")
def api_cuota_mal_puesta_excel(request: Request, token: str = "", version: str = ""):
    """📗 Excel de CUOTA MAL PUESTA (V2, 08/10/2026): HISTORICO (todos los
    evaluados) + RESUMEN. Se arma al pedirlo desde la base (fuente de verdad);
    no se guarda. `version` vacía = todas."""
    _check_token_o_sesion(request, token, ["cuotamalpuesta"])
    from production.engines import cmp_excel
    conn = _get_conn_production()
    try:
        datos = cmp_excel.construir(conn, version or None)
    finally:
        conn.close()
    nombre = f"cuota_mal_puesta_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M')}.xlsx"
    return Response(content=datos,
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{nombre}"'})


@router.get("/api/cuota-mal-puesta/resumen")
def api_cuota_mal_puesta_resumen(request: Request, token: str = "", version: str = ""):
    """Lo mismo que la hoja RESUMEN del Excel, en JSON (grilla FT × EDGE,
    variantes, controles, tres marcas)."""
    _check_token_o_sesion(request, token, ["cuotamalpuesta"])
    from production.engines import cmp_excel
    from production.engines.cuota_mal_puesta import _ensure_tabla
    conn = _get_conn_production()
    try:
        _ensure_tabla(conn)
        return {"version": version or "todas", **cmp_excel.resumen(cmp_excel.leer_filas(conn, version or None))}
    finally:
        conn.close()


@router.get("/api/cuota-mal-puesta/aprendizaje")
def api_cuota_mal_puesta_aprendizaje(request: Request, token: str = ""):
    """Fase 7 (APAGADA): qué PROPONDRÍA el autoaprendizaje con la muestra de
    hoy, sin aplicar nada. Solo lectura; sirve para validar el método antes
    de encenderlo."""
    _check_token_o_sesion(request, token, ["cuotamalpuesta"])
    from production.engines import cmp_aprendizaje
    conn = _get_conn_production()
    try:
        return cmp_aprendizaje.proponer(conn)
    finally:
        conn.close()


@router.get("/api/cuota-mal-puesta/carga-distribucion")
def api_cuota_mal_puesta_carga_distribucion(request: Request, token: str = "",
                                            dias: int = Query(90, ge=7, le=365)):
    """🏃 CARGA Y RITMO V3 (10/10/2026, Rubén): la distribución histórica de
    partidos / sets / games / descanso / índice de carga, para definir con
    datos qué es NORMAL / CONSIDERABLE / ALTA / EXTREMA. Dos fuentes:
    match_log (cada partido = con qué carga llegó ese jugador) y las
    evaluaciones de Cuota Mal Puesta (carga_json). Solo lectura."""
    _check_token_o_sesion(request, token, ["cuotamalpuesta"])
    from production.engines import cmp_carga
    from production.engines.cuota_mal_puesta import _ensure_tabla, leer_config
    conn = _get_conn_production()
    try:
        _ensure_tabla(conn)
        return cmp_carga.distribucion(conn, leer_config(), dias=dias)
    finally:
        conn.close()


@router.get("/api/cuota-mal-puesta/{senal_id}")
def api_cuota_mal_puesta_detalle(senal_id: int, request: Request, token: str = ""):
    """La foto completa de una señal + los partidos L10 de los dos
    jugadores que produjeron sus scores (auditoría, punto 19)."""
    _check_token_o_sesion(request, token, ["cuotamalpuesta"])
    from production.engines.cuota_mal_puesta import _ensure_tabla
    conn = _get_conn_production()
    try:
        _ensure_tabla(conn)
        s = conn.execute("SELECT * FROM cmp_senales WHERE id=?", (senal_id,)).fetchone()
        if not s:
            raise HTTPException(404, "Señal no encontrada")
        det = [dict(r) for r in conn.execute(
            "SELECT * FROM cmp_l10_detalle WHERE senal_id=? ORDER BY lado, orden", (senal_id,))]
        senal = dict(s)
        # 09/10/2026 (Rubén: "un modal con todo lo relacionado a ese partido"):
        # los JSON guardados, ya abiertos, y el UTR de los dos SOLO INFORMATIVO
        # (el padrón; no interviene en Cuota Mal Puesta).
        import json as _json
        abiertos = {}
        for k in ("carga_json", "mto_json", "l10_resumen_json", "motivo_codigos_json", "revalidacion_json",
                  "config_json", "motivos_codigos_publicacion"):
            v = senal.get(k)
            if v:
                try:
                    abiertos[k] = _json.loads(v)
                except (TypeError, ValueError):
                    abiertos[k] = None
        # 10/10/2026 (Rubén): los tres factores en palabras (🧠 / 🏃 / 🩹), los
        # mismos textos que van a Discord, para el modal.
        try:
            from production.engines.cuota_mal_puesta import bloques_factores
            factores = bloques_factores(senal)
        except Exception as e:                                # pragma: no cover
            factores = [{"titulo": "Factores", "lineas": [f"no se pudieron armar: {e}"], "mostrar": False}]
        return {"senal": senal, "detalle": abiertos, "utr": _utr_informativo(conn, senal), "factores": factores,
                "l10": {"A": [d for d in det if d["lado"] == "A"], "B": [d for d in det if d["lado"] == "B"]}}
    finally:
        conn.close()


def _utr_informativo(conn, senal: dict) -> dict:
    """El UTR del padrón para los dos jugadores de una señal (solo para
    mostrarlo; ninguna estrategia que no sea UTR lo usa)."""
    try:
        from production.engines.utr_padron import perfil, perfil_por_nombre
    except Exception:                                       # pragma: no cover
        return {}
    out = {}
    for lado, nombre, pid in (("A", senal.get("player_a"), senal.get("player_a_id")),
                              ("B", senal.get("player_b"), senal.get("player_b_id"))):
        p = None
        try:
            p = perfil(conn, pid) if pid else None
            if not p:
                p = perfil_por_nombre(conn, nombre, senal.get("genero"))
        except Exception:
            p = None
        if not p:
            out[lado] = None
            continue
        utr = p.get("utr_singles")
        out[lado] = {"nombre": nombre, "nombre_utr": p.get("nombre_utr"), "utr": utr,
                     "display": p.get("utr_display") or (f"{utr:.2f}" if utr else None),
                     "estado_utr": p.get("estado_utr"), "identidad": p.get("estado"),
                     "confianza": p.get("nivel_confianza"), "usable": p.get("usable"),
                     "fuente": p.get("fuente") or p.get("origen"), "fecha_captura": p.get("fecha_captura"),
                     "utr_dura": p.get("utr_dura"), "utr_tierra": p.get("utr_tierra"), "utr_hierba": p.get("utr_hierba"),
                     "utr_player_id": p.get("utr_player_id")}
    a, b = out.get("A"), out.get("B")
    if a and b and a.get("utr") and b.get("utr"):
        out["delta"] = round(float(a["utr"]) - float(b["utr"]), 2)
    return out


def _estado_vivo(conn) -> dict:
    """Cuotas en vivo del circuito UTR (OddsPapi), para la pestaña. Nunca rompe el panel."""
    try:
        from production.engines import oddspapi_vivo
        return oddspapi_vivo.estado(conn)
    except Exception as e:                              # pragma: no cover
        return {"modo": "?", "ultimo_error": str(e)}


@router.get("/api/utr-value")
def api_utr_value(request: Request, token: str = "", grupo: str = "", motivo: str = "",
                  genero: str = "", limit: int = Query(300, le=2000)):
    """
    Pestaña UTR VALUE (07/10/2026, a pedido de Rubén Darío). Solo lectura:
    la tabla la llena production/engines/utr_value.py. `estadisticas` es
    SIEMPRE sobre la versión vigente y sin los filtros de la lista. `padron`
    = el estado del UTR cargado (production/engines/utr_padron.py).

    grupo: ESTUDIO | CANDIDATO | DISCORD | SIN_UTR | '' (= todo lo que tiene
    UTR de los dos, sin los SIN_UTR).
    """
    _check_token_o_sesion(request, token, ["utrvalue"])
    from production.engines.utr_value import _ensure_tabla, estadisticas
    from production.engines.utr_padron import estado as estado_padron
    from production.engines import oddspapi_utr
    conn = _get_conn_production()
    try:
        _ensure_tabla(conn)
        where, args = [], []
        if grupo:
            where.append("grupo=?")
            args.append(grupo.upper())
        else:
            where.append("grupo<>'SIN_UTR'")
        if motivo:
            where.append("motivo=?")
            args.append(motivo.upper())
        if genero:
            where.append("genero=?")
            args.append(genero)
        clausula = f"WHERE {' AND '.join(where)}"
        filas = [dict(r) for r in conn.execute(
            f"""SELECT id, fixture_id, primera_vista_en, fecha_partido, torneo, circuito, superficie,
                       genero, jugador_a, jugador_b, utr_a, utr_b, delta_utr, delta_sup,
                       confianza_a, confianza_b, captura_utr_a, captura_utr_b, odds_a, odds_b,
                       p_mkt_a, p_mkt_a_cand, p_utr, utr_value_pp, es_candidato, candidato_en,
                       odds_a_cand,
                       utr_value_pp_cand, prob_elo_a, prob_ftr_a, matchup_estado, prob_matchup_a,
                       motivo, motivos_json, grupo, decision_estado, discord_sent,
                       telegram_enviado, telegram_resultado, cuota_cierre_a, clv_pct,
                       ganador_final, resultado_score, result, cuota_evaluacion, profit_loss,
                       retiro, sin_utr_detalle, fuente, fuente_utr, utr_display_a, utr_display_b,
                       delta_min, delta_max, intervalo_caso, identidad_utr, revision_estado, revision_por
                  FROM utr_value_registro {clausula}
                 ORDER BY id DESC LIMIT ?""", args + [limit])]
        total = conn.execute(f"SELECT COUNT(*) AS n FROM utr_value_registro {clausula}",
                             args).fetchone()["n"]
        return {"total": total, "registros": filas, "estadisticas": estadisticas(conn),
                "padron": estado_padron(conn),
                "oddspapi": {**oddspapi_utr.estado(conn), "vivo": _estado_vivo(conn)},
                "actualizado": datetime.now(timezone.utc).isoformat()}
    finally:
        conn.close()


@router.get("/api/utr-anomalia")
def api_utr_anomalia(request: Request, token: str = "", solo: str = "activadas",
                     genero: str = "", limit: int = Query(300, le=2000)):
    """
    🚨 UTR MARKET ANOMALY (07/10/2026, a pedido de Rubén Darío), dentro de la
    pestaña UTR VALUE. Solo lectura: la llena production/engines/utr_anomalia.py.
    solo: activadas (ΔUTR ≥ 0.25 y cuota ≥ 4.00) | discord | todo (también los
    de estudio, con cuota menor). `estadisticas` siempre sobre todo.
    """
    _check_token_o_sesion(request, token, ["utrvalue"])
    from production.engines.utr_anomalia import _ensure_tabla, estadisticas
    conn = _get_conn_production()
    try:
        _ensure_tabla(conn)
        where, args = ["1=1"], []
        if solo == "activadas":
            where.append("activada=1")
        elif solo == "discord":
            where.append("discord_sent=1")
        if genero:
            where.append("genero=?")
            args.append(genero)
        clausula = f"WHERE {' AND '.join(where)}"
        filas = [dict(r) for r in conn.execute(
            f"""SELECT id, fixture_id, event_id, primera_vista_en, fecha_partido, torneo, superficie, genero,
                       fuente, fuente_activacion, jugador_a, jugador_b, utr_a, utr_b, delta_utr, p_utr,
                       confianza_a, confianza_b, cuota_inicial, cuota_pre, cuota_max, cuota_ultima,
                       fase_ultima, vivo_desde, activada, activacion_en, fase_activacion,
                       cuota_pre_activacion, cuota_activacion, cuota_rival_activacion, p_impl_activacion,
                       p_mkt_activacion, utr_value_pp_activacion, cumple_utr_market_anomaly,
                       cumple_utr_value, utr_value_motivo, motivo, motivos_json, discord_sent,
                       marcador_estado, marcador_activacion, games_activacion, minutos_desde_inicio,
                       telegram_enviado, result, resultado_score, profit_loss, clv_pct, retiro,
                       fuente_utr, utr_display_a, utr_display_b, delta_min, delta_max, intervalo_caso,
                       identidad_utr, revision_estado, revision_por
                  FROM utr_anomalia_registro {clausula}
                 ORDER BY id DESC LIMIT ?""", args + [limit])]
        total = conn.execute(f"SELECT COUNT(*) AS n FROM utr_anomalia_registro {clausula}",
                             args).fetchone()["n"]
        return {"total": total, "registros": filas, "estadisticas": estadisticas(conn),
                "actualizado": datetime.now(timezone.utc).isoformat()}
    finally:
        conn.close()


@router.get("/api/utr-anomalia/{registro_id}")
def api_utr_anomalia_detalle(registro_id: int, request: Request, token: str = ""):
    """La foto completa de una fila (cruces de umbral y controles incluidos)."""
    _check_token_o_sesion(request, token, ["utrvalue"])
    from production.engines.utr_anomalia import _ensure_tabla
    conn = _get_conn_production()
    try:
        _ensure_tabla(conn)
        r = conn.execute("SELECT * FROM utr_anomalia_registro WHERE id=?", (registro_id,)).fetchone()
        if not r:
            raise HTTPException(404, "Registro no encontrado")
        d = dict(r)
        for k in ("filtros_json", "motivos_json", "cruces_json"):
            try:
                d[k.replace("_json", "")] = json.loads(d.get(k) or "null")
            except ValueError:
                pass
        return {"registro": d}
    finally:
        conn.close()


@router.get("/api/utr-value/{registro_id}")
def api_utr_value_detalle(registro_id: int, request: Request, token: str = ""):
    """La foto completa de un partido (todas las columnas, incluidos los
    filtros de calidad uno por uno)."""
    _check_token_o_sesion(request, token, ["utrvalue"])
    from production.engines.utr_value import _ensure_tabla
    conn = _get_conn_production()
    try:
        _ensure_tabla(conn)
        r = conn.execute("SELECT * FROM utr_value_registro WHERE id=?", (registro_id,)).fetchone()
        if not r:
            raise HTTPException(404, "Registro no encontrado")
        d = dict(r)
        for k in ("filtros_json", "motivos_json"):
            try:
                d[k.replace("_json", "")] = json.loads(d.get(k) or "null")
            except ValueError:
                pass
        return {"registro": d}
    finally:
        conn.close()


@router.get("/api/bajon-mental")
def api_bajon_mental(request: Request, token: str = "",
                     jugador_sacaba: str = "", jugador: str = "", genero: str = "",
                     limit: int = Query(200, le=1000)):
    """
    Pestaña BAJÓN MENTAL (01/09/2026, a pedido de Maria/Rubén): casos
    donde un jugador tuvo punto de set/partido, lo perdió, y perdió el
    juego donde lo tenía -- ver core/bajon_mental.py (repo del bot)
    para el detector, y production/api/routes_public.py::
    bajon_mental_recibir para el guardado. Puramente de lectura.

    `jugador_sacaba`: separa las dos búsquedas de la pestaña --
    "1" = tenía el punto sacando (iba arriba, no cerró),
    "0" = tenía el punto restando (necesitaba quebrar, no lo hizo).
    Vacío = las dos juntas.

    `jugador` (opcional): filtra por nombre, sin importar mayúsculas ni
    acentos (compara contra jugador_norm, ya guardado así).
    """
    _check_token_o_sesion(request, token, ["bajonmental"])
    from production.api.routes_public import _ensure_bajon_mental_table
    conn = _get_conn_production()
    try:
        _ensure_bajon_mental_table(conn)
        where, args = [], []
        if jugador_sacaba in ("1", "0"):
            where.append("jugador_sacaba=?")
            args.append(int(jugador_sacaba))
        if jugador:
            from shared.tennis_names import normalize_name
            where.append("jugador_norm LIKE ?")
            args.append(f"%{normalize_name(jugador)}%")
        if genero:
            where.append("genero=?")
            args.append(genero)
        clausula = f"WHERE {' AND '.join(where)}" if where else ""
        filas = [dict(r) for r in conn.execute(
            f"""SELECT jugador, rival, set_numero, juego_en_set,
                       marcador_antes_del_punto, tipo_punto, jugador_sacaba,
                       juegos_antes_del_punto, gano_el_set,
                       juego_siguiente_tambien_perdido, ganador_partido,
                       perdedor_partido, resultado_partido, torneo, superficie,
                       genero, favorito_nombre, prob_ftr, prob_elo, prob_matchup,
                       creado_en
                  FROM bajon_mental {clausula}
                 ORDER BY creado_en DESC LIMIT ?""",
            args + [limit])]
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM bajon_mental {clausula}", args).fetchone()["n"]
        return {"total": total, "casos": filas,
                "actualizado": datetime.now(timezone.utc).isoformat()}
    finally:
        conn.close()


@router.get("/api/post-set/ventaja-leve-reglas")
def api_ventaja_leve_reglas(request: Request, token: str = "", casos: int = 0,
                            verificar: int = 0, corte: str = ""):
    """
    Ventaja Leve con reglas congeladas (23/09/2026): por regla VL01-VL05,
    HISTÓRICO INICIAL, DESDE ACTIVACIÓN y TOTAL por separado, más el
    conteo de silenciosas por género y marcador. Solo lectura. Con
    `casos=N` (máx. 500) agrega las últimas N filas del registro.
    Ver production/engines/ventaja_leve_reglas.py.

    `verificar=1` (24/09/2026): agrega "verificacion", la AUDITORÍA de
    solo lectura que recalcula VL01-VL05 sobre la base actual y la compara
    con el histórico inicial. No cambia ninguna regla. `corte=AAAA-MM-DD`
    (el día del análisis a mano) separa los casos del hueco entre el
    análisis y el deploy. Opcional porque lee toda la base de Ventaja
    Leve (la misma consulta que la pestaña POST SET).
    """
    _check_token_o_sesion(request, token, ["postset"])
    import re as _re
    if corte and not _re.fullmatch(r"\d{4}-\d{2}-\d{2}", corte):
        raise HTTPException(status_code=400, detail="corte debe ser AAAA-MM-DD")
    from production.engines.ventaja_leve_reglas import (estadisticas, _ensure_tabla,
                                                       verificar_base)
    conn = _get_conn_production()
    try:
        _ensure_tabla(conn)
        out = estadisticas(conn)
        if verificar:
            out["verificacion"] = verificar_base(conn, corte or None)
        if casos > 0:
            out["casos"] = [dict(r) for r in conn.execute(
                "SELECT id, fixture_id, fecha_partido, hora_partido, torneo, genero, "
                "favorito_leve, rival, cuota_pre_favorito, elo_pre, ftr_pre, set1_score, "
                "set1_favorito, set1_rival, regla, clasificacion, alerta_enviada, "
                "motivo_sin_alerta, ganador_final, resultado_score, sets_finales, "
                "estado_resultado, acierto, detectado_en "
                "FROM ventaja_leve_registro ORDER BY id DESC LIMIT ?",
                (min(casos, 500),))]
        return out
    finally:
        conn.close()


@router.get("/api/post-set/pasan-filtro-tendencia")
def api_pasan_filtro_tendencia(request: Request, token: str = "", casos: int = 0):
    """
    Pasan Filtro UNDER/OVER por sets (23/09/2026): base por género y
    marcador, todas las reglas con datos (activas primero), rendimiento de
    las alertas enviadas y activaciones/desactivaciones. Solo lectura.
    Ver production/engines/pasan_filtro_tendencia.py.
    """
    _check_token_o_sesion(request, token, ["postset"])
    from production.engines.pasan_filtro_tendencia import estadisticas
    conn = _get_conn_production()
    try:
        out = estadisticas(conn)
        if casos > 0:
            out["casos"] = [dict(r) for r in conn.execute(
                "SELECT id, fixture_id, fecha_partido, torneo, genero, favorito, rival, "
                "elo_pre, ftr_pre, set1_score, marcador, regla_descripcion, tendencia, "
                "clasificacion, pct_historico, n_historico, alerta_enviada, motivo_sin_alerta, "
                "reglas_coincidentes, resultado_score, sets_finales, estado_resultado, acierto, "
                "detectado_en FROM pasan_filtro_registro ORDER BY id DESC LIMIT ?",
                (min(casos, 500),))]
        return out
    finally:
        conn.close()


class CierreManualPostSet(BaseModel):
    home: str
    away: str
    ganador: str
    resultado_score: str
    genero: Optional[str] = None
    superficie: Optional[str] = None
    torneo: Optional[str] = None


@router.post("/api/post-set/cerrar-manual")
async def api_post_set_cerrar_manual(request: Request, datos: CierreManualPostSet):
    """
    Botón de emergencia (01/09/2026, a pedido de Maria: "colocamos un
    botón que solo vean los admins... guardar resultado o cerrar
    partido") -- para cuando el cierre automático (ver
    _evaluar_cierre_automatico en routes_public.py) todavía no actuó y
    un admin YA VE el resultado en la propia tabla de Partidos totales
    (el caso que disparó el pedido: un partido en vivo con 2 sets a 0
    que seguía sin cerrar). Mismo camino que el cierre automático y que
    /match-finished -- no es un tercer mecanismo de cierre, es el mismo
    disparado a mano.

    SOLO ADMIN -- se sirve de _es_admin(), el mismo chequeo que ya usa
    el resto del panel para lo sensible.
    """
    if not _es_admin(request):
        raise HTTPException(status_code=403, detail="Solo administradores")

    ganador = datos.ganador.strip()
    perdedor = datos.away if ganador == datos.home else datos.home
    if ganador not in (datos.home, datos.away):
        raise HTTPException(status_code=400, detail="ganador debe ser home o away tal cual se mandaron")

    conn = _get_conn_production()
    try:
        superficie = datos.superficie
        if not superficie:
            fila_fx = conn.execute(
                "SELECT superficie FROM fixtures_ftr_elo WHERE "
                "(jugador1=? AND jugador2=?) OR (jugador1=? AND jugador2=?) "
                "ORDER BY actualizado_en DESC LIMIT 1",
                (datos.home, datos.away, datos.away, datos.home)).fetchone()
            superficie = fila_fx["superficie"] if fila_fx else None
        if not superficie:
            raise HTTPException(status_code=400,
                                detail="No se pudo resolver la superficie -- mandala explícita")

        from shared.categories import normalize_level
        nivel = normalize_level(torneo=datos.torneo or "")

        from production.api.routes_public import MatchFinished, match_finished
        resultado = await match_finished(MatchFinished(
            ganador=ganador, perdedor=perdedor, score=datos.resultado_score,
            nivel=nivel, superficie=superficie, genero=datos.genero, torneo=datos.torneo,
        ))

        # Sugerencias, YA (01/09/2026, a pedido de Maria: "que se guarde
        # también en sugerencias") -- descartes_ia normalmente recién se
        # cierra en el ciclo de 12h (cerrar_descartes_ia_pendientes, ver
        # production/tasks/cierre_alertas_task.py); acá, como es un
        # cierre a mano y el admin quiere verlo reflejado ya, se
        # actualiza directo, mismo criterio que
        # _guardar_stats_partido_cerrado ya usa para las estadísticas.
        from shared.tennis_names import normalize_name
        g_n, p_n = normalize_name(ganador), normalize_name(perdedor)
        filas = conn.execute(
            "SELECT id, home, away FROM descartes_ia WHERE ganador_final IS NULL"
        ).fetchall()
        ids = [f["id"] for f in filas
               if {normalize_name(f["home"]), normalize_name(f["away"])} == {g_n, p_n}]
        if ids:
            conn.executemany(
                "UPDATE descartes_ia SET ganador_final=?, resultado_score=? WHERE id=?",
                [(ganador, datos.resultado_score, i) for i in ids])
            conn.commit()

        log.info(f"[CierreManual] admin cerró a mano "
                 f"{ganador} vs {perdedor} {datos.resultado_score}")
        return {"ok": True, "actualizadas_en_sugerencias": len(ids), **resultado}
    finally:
        conn.close()


@router.get("/api/history")
def api_history(
    request: Request,
    token: str = "",
    name: str = Query(..., description="Nombre del jugador (nombre_canonico)"),
    gender: str = Query("M"),
    surface: str = Query("general", description="general (todas) / hard / clay / grass"),
    limit: int = Query(20, le=100),
):
    """Últimos partidos de un jugador, desde rating_history — mismo dato
    que ya usa el motor para recalcular Elo, no algo nuevo que calcular.
    surface='general' no filtra (rating_history no tiene una fila
    'general' propia — general = todas las superficies combinadas)."""
    # Lectura: token de research O sesión con permiso del tab
    # (29/07/2026 -- ver _check_token_o_sesion). Antes era
    # _check_token a secas y las cuentas de usuario, que nunca tienen
    # RESEARCH_TOKEN, recibían 403 y veían la tabla vacía.
    _check_token_o_sesion(request, token, None)
    conn = _get_conn()
    try:
        # FIX HISTORIAL-NO-ENCONTRADO (20/07/2026, reportado por Maria:
        # el modal de MTO encontraba "Kiss C" para la probabilidad pero
        # decía "No se encontró a Kiss C en research Elo" para el
        # historial, mismo jugador). Causa real: este endpoint buscaba
        # el nombre EXACTO (`WHERE name = ?`), mientras que
        # /api/compare (la probabilidad de arriba en el modal) ya usa
        # buscar_candidatos_elo(), que resuelve variantes como
        # "Apellido + inicial" ("Kiss C" -> "Camille Kiss", paso 5, ver
        # shared/elo_lookup.py). Ahora los dos usan la MISMA resolución
        # -- se busca con el nombre resuelto (candidatos[0]["name"]),
        # no con el nombre crudo que vino en la URL.
        col = _elo_col(surface if surface in SURFACES else "general")
        mcol = _matches_col(surface if surface in SURFACES else "general")
        candidatos, _confiable = _buscar_candidatos(conn, col, mcol, gender, name)
        if not candidatos:
            raise HTTPException(status_code=404, detail=f"No encontrado: {name}")
        nombre_resuelto = candidatos[0]["name"]

        sql = """
            SELECT fecha, opponent, result, surface, level, tournament, score,
                   elo_general_before, elo_general_after, delta_general, prob_before,
                   surface_elo_before, surface_elo_after
            FROM rating_history
            WHERE player = ? COLLATE NOCASE
        """
        params: list = [nombre_resuelto]
        if surface and surface != "general":
            sql += " AND surface = ?"
            params.append(surface)
        sql += " ORDER BY fecha DESC LIMIT ?"
        params.append(limit)

        rows = conn.execute(sql, params).fetchall()

        # FIX ELO-POR-SUPERFICIE (08/07/2026): el motor ya calculaba y
        # guardaba elo_hard/elo_clay/elo_grass (y su historial en
        # surface_elo_before/after) desde siempre -- este endpoint solo
        # leía y devolvía el general, así que el historial cambiaba de
        # partidos al filtrar por superficie pero el Elo mostrado quedaba
        # pegado al general siempre. Con superficie != general, se usa
        # surface_elo_*; si por algún motivo esa fila no lo tiene
        # calculado (dato viejo), cae de vuelta al general en lugar de
        # mostrar null.
        usar_surface = bool(surface) and surface != "general"

        def _elo_antes(r):
            if usar_surface and r["surface_elo_before"] is not None:
                return r["surface_elo_before"]
            return r["elo_general_before"]

        def _elo_despues(r):
            if usar_surface and r["surface_elo_after"] is not None:
                return r["surface_elo_after"]
            return r["elo_general_after"]

        utr_jug, utr_rivales = _utr_historial_seguro(nombre_resuelto, gender, [r["opponent"] for r in rows])
        return {
            "nombre": name,
            # ΔUTR por partido (08/10/2026): UTR ACTUAL del jugador y de cada
            # rival (no el que tenían ese día: el histórico no existe).
            "utr_jugador": utr_jug,
            "partidos": [
                {
                    "utr_rival": utr_rivales[i],
                    "dif_utr": _utr_comparar(utr_jug, utr_rivales[i]),
                    "fecha": r["fecha"][:10] if r["fecha"] else "",
                    "rival": r["opponent"],
                    "gano": bool(r["result"]),
                    "superficie": r["surface"],
                    "nivel": r["level"],
                    "torneo": r["tournament"],
                    "score": r["score"],
                    "elo_antes": round(_elo_antes(r), 2),
                    "elo_despues": round(_elo_despues(r), 2),
                    "delta": round(_elo_despues(r) - _elo_antes(r), 2),
                    "prob_antes": round(r["prob_before"], 3) if r["prob_before"] is not None else None,
                }
                for i, r in enumerate(rows)
            ],
        }
    finally:
        conn.close()


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def research_page(request: Request):
    """
    La página se arma SEGÚN EL ROL: los tabs que la cuenta no puede ver no
    se renderizan, así que su HTML nunca sale del servidor. No es lo que
    protege los datos -- eso lo hace el control de permisos del portón
    (production/api/permisos.py) -- pero evita el caso incómodo de
    mostrarle a alguien un tab que al tocarlo le tira 403.
    """
    import json

    from production.api.auth import usuario_actual, horas_restantes
    from production.api.permisos import contexto_de_plantilla, puede_ver_tab

    u = usuario_actual(request) or {}
    rol = u.get("rol", "")
    ctx = contexto_de_plantilla(rol)
    return _templates.TemplateResponse(
        request=request, name="research.html",
        context={
            **ctx,
            # Para el header: con qué cuenta se está viendo el panel.
            # Ya viajaba en permisos_json para el JS; se expone también
            # a Jinja para que se pinte en el HTML inicial y no aparezca
            # medio segundo después.
            "usuario": u.get("usuario", ""),
            # Vencimiento de suscripción (01/08/2026, a pedido de Maria:
            # "que en el panel de la persona le diga cuánto tiempo le
            # queda... en horas"). Se calcula acá, server-side, para que
            # aparezca desde el primer render -- el latido de sesión
            # (sesion_pestana.js, cada 30s) lo va actualizando después.
            "horas_restantes": horas_restantes(u.get("vence_en")),
            "puede_ver": lambda tab: puede_ver_tab(rol, tab),
            # Para el JS: qué permisos tiene, qué tabs ve y quién es.
            "permisos_json": json.dumps({
                "rol": rol,
                "usuario": u.get("usuario", ""),
                "permisos": sorted(ctx["permisos"]),
                "tabs": ctx["tabs_permitidos"],
            }),
        })


# ═════════════════════════════════════════════════════════════════════
#  DEFENSA DE PUNTOS  (31/07/2026)
# ═════════════════════════════════════════════════════════════════════

@router.get("/api/defensa-semana")
def api_defensa_semana(
    request: Request,
    token: str = "",
    tour: str = Query("ATP", description="ATP o WTA"),
    limit: int = Query(300, le=1000),
):
    """
    Snapshot de defensa de puntos de la semana ISO en curso.

    LEE Y NADA MÁS. El cálculo lo hace el job semanal
    (production/tasks/defensa_snapshot_task.py) los lunes de madrugada.
    Si este endpoint recalculara al abrir la pestaña, cada visita
    dispararía miles de lecturas y el número podría cambiar entre una
    carga y otra -- adiós a poder auditar qué decía el sistema el lunes.

    Con la tabla vacía devuelve lista vacía y `snapshot_pendiente: true`.
    NO es un 500: que el job todavía no haya corrido es un estado normal.
    """
    _check_token_o_sesion(request, token, ("defensapuntos",))

    tour = (tour or "ATP").upper()
    if tour not in ("ATP", "WTA"):
        raise HTTPException(status_code=400, detail=f"tour invalido: {tour}")

    from datetime import date as _date
    season, semana, _ = _date.today().isocalendar()

    conn = _get_conn_production()
    try:
        filas = conn.execute(
            """
            SELECT s.player_id,
                   p.nombre_canonico        AS nombre,
                   p.rank_oficial           AS rank,
                   s.current_points         AS puntos,
                   s.dropping_points        AS defiende,
                   s.defense_percentage     AS presion,
                   s.minimum_round_to_equal AS iguala_en,
                   s.dropping_tournament_id AS sale_de,
                   s.previous_round         AS ronda_previa,
                   s.participation_status   AS participa,
                   s.points_source          AS origen,
                   s.estado                 AS estado,
                   s.scenarios_json         AS escenarios,
                   s.calculated_at          AS calculado_en
              FROM ranking_defense_snapshot s
              JOIN players p ON p.player_id = s.player_id
             WHERE s.season = ? AND s.ranking_week = ? AND s.tour = ?
             ORDER BY s.dropping_points DESC
             LIMIT ?
            """,
            (season, semana, tour, limit),
        ).fetchall()
    except sqlite3.OperationalError as e:
        # Todavia no se aplico schema_defensa.sql. Un mensaje claro vale
        # mas que un 500 generico que obliga a ir a mirar los logs.
        if "no such table" in str(e):
            raise HTTPException(
                status_code=503,
                detail="Falta aplicar schema_defensa.sql sobre la base de produccion.",
            )
        raise
    finally:
        conn.close()

    def _banda(pct):
        # Mismas bandas que defensa_snapshot_task.py. Se recalculan aca,
        # en vez de guardarse, para poder ajustarlas sin regenerar el
        # snapshot: son presentacion, no dato.
        if pct is None:
            return "sin_dato"
        if pct <= 0:
            return "sin_defensa"
        if pct <= 0.05:
            return "baja"
        if pct <= 0.12:
            return "media"
        if pct <= 0.20:
            return "alta"
        return "critica"

    jugadores = []
    for f in filas:
        d = dict(f)
        d["banda"] = _banda(d.get("presion"))
        try:
            d["escenarios"] = json.loads(d["escenarios"]) if d["escenarios"] else {}
        except (TypeError, ValueError):
            d["escenarios"] = {}
        jugadores.append(d)

    return {
        "tour": tour,
        "season": season,
        "semana": semana,
        "snapshot_pendiente": len(jugadores) == 0,
        "calculado_en": jugadores[0]["calculado_en"] if jugadores else None,
        "jugadores": jugadores,
    }


# ── Nombre de torneo: quitar la ronda pegada ─────────────────────────
# fixtures_valor guarda el torneo CON la ronda al final: "ATP Los Cabos -
# QF", "ATP Los Cabos - SF", "ATP Los Cabos - Doubles". Son la misma
# competición y sin agrupar aparecería tres veces en la lista.
#
# Los sufijos se quitan SOLO si están en esta lista. Un sufijo que no se
# reconoce se deja tal cual: preferimos un torneo repetido, que se ve y se
# arregla, a fusionar dos eventos distintos porque uno tenía un guion en
# el nombre.
_SUFIJOS_RONDA = {
    "qf", "sf", "f", "final", "finals", "r16", "r32", "r64", "r128",
    "1st round", "2nd round", "3rd round", "4th round",
    "quarterfinal", "semifinal", "qualification", "qualifying", "q",
    "qualifiers", "qualifier", "quals",
    "round of 16", "round of 32", "round of 64",
}
# Los dobles quedan fuera de toda la pestaña: reparten puntos de ranking
# de DOBLES, que es otra clasificación. Mezclarlos daría defensas que no
# se corresponden con el ranking individual que muestra la tabla.
_SUFIJOS_EXCLUIR = {"doubles", "dobles", "mixed doubles", "mixed"}


def _partir_torneo(nombre: str) -> tuple[str, bool]:
    """
    'ATP Los Cabos - QF' -> ('ATP Los Cabos', False)
    'ATP Los Cabos - Doubles' -> ('ATP Los Cabos', True)   <- excluir
    'Torneo Raro - Algo' -> ('Torneo Raro - Algo', False)  <- no se toca
    """
    if " - " not in (nombre or ""):
        return (nombre or "").strip(), False
    base, _, sufijo = (nombre or "").rpartition(" - ")
    suf = sufijo.strip().lower()
    if suf in _SUFIJOS_EXCLUIR:
        return base.strip(), True
    if suf in _SUFIJOS_RONDA:
        return base.strip(), False
    return (nombre or "").strip(), False


def _categoria_desde_nombre(nombre: str, tour: str) -> str | None:
    """
    Categoría a partir del nombre que guarda fixtures_valor.

    Hace falta porque cruzar solo por ciudad junta torneos distintos. En
    Brisbane 2025 se jugaron CATORCE eventos: M15 x2, M25 x2, tres
    Challengers, el ATP 250 y seis femeninos. Un LIKE '%brisbane%' los
    sumaba todos y producía podios imposibles -- ocho campeones en un
    torneo -- que pasaban el filtro de confianza como si fueran datos
    buenos.

    Devuelve None cuando no se puede determinar. Ante duda, sin
    categoría: preferimos ocultar un torneo bueno a mostrar uno cruzado
    con otro.
    """
    n = (nombre or "").upper()
    if "CHALLENGER" in n:
        return "challenger"          # el escalón exacto no se conoce
    if "ITF" in n or n.startswith(("M15", "M25", "W15", "W35", "W50", "W75", "W100")):
        return "itf"
    if "125" in n:
        return "wta125"
    if tour == "WTA":
        return "wta"
    return "atp"


# Tokens que NUNCA son la ciudad. El nombre del torneo llega en formatos
# distintos según de dónde venga la alerta:
#
#   fixtures_valor -> "WTA Washington - SF"
#   bot en vivo    -> "ATP. MONTREAL. HARD"
#   UTR            -> "UTR PRO. WOMEN. COLUMBUS"
#   qualy          -> "WTA. TORONTO. HARD. QUALIFICATION"
#
# Antes se sacaba la ciudad con `torneo.split()[-1]`, que sobre esos
# formatos devolvía "sf", "hard" y "qualification" -- nunca la ciudad.
# Ver _ciudad_y_categoria().
_TOKENS_RUIDO_TORNEO = {
    # circuitos / tours
    "atp", "wta", "itf", "utr", "pro", "challenger", "ch", "tour",
    # género
    "men", "mens", "women", "womens", "male", "female",
    # superficie
    "hard", "clay", "grass", "carpet", "indoor", "outdoor",
    # fase / modalidad
    "qualification", "qualifying", "qualy", "quali", "main", "draw",
    "singles", "single", "doubles", "dobles",
}


def _ciudad_y_categoria(torneo: str, tour_hint: str = "") -> tuple[str | None, str | None, str | None]:
    """
    De un nombre de torneo en CUALQUIER formato saca (ciudad, categoría, motivo).

    `motivo` es None cuando salió bien; si no, dice POR QUÉ no se pudo,
    y ese motivo viaja hasta la interfaz. Es el cambio de fondo respecto
    de antes: "no pude parsear el torneo", "el jugador no está en
    producción" y "el jugador no jugó acá el año pasado" son tres cosas
    distintas que se veían todas como un mismo "sin dato" mudo.

    La ciudad es el ÚLTIMO token útil, igual que hacía el listado
    (/api/defensa-torneos): el cruce contra el histórico es un
    LIKE '%ciudad%', así que "cabos" encuentra "Los Cabos" sin problema.

    La categoría se calcula sobre el nombre ORIGINAL, no sobre los
    tokens filtrados -- ahí "challenger", "125" y "M15" sí hacen falta.
    """
    base, es_dobles = _partir_torneo(torneo or "")
    if not base:
        return None, None, "torneo_vacio"
    if es_dobles:
        # Los dobles reparten puntos de otra clasificación (ver _SUFIJOS_EXCLUIR).
        return None, None, "dobles"

    categoria = _categoria_desde_nombre(base, (tour_hint or "").upper())

    # 04/08/2026: la ciudad cargada a mano en Admin → Torneos gana sobre
    # cualquier heurístico. Es la salida para los nombres que el parser
    # no puede resolver solo ("no pude identificar el torneo") -- en vez
    # de agregar otra regla más al parser, se carga una vez y listo.
    try:
        from production.api.torneos import buscar as _buscar_torneo
        fila = _buscar_torneo(torneo)
        if fila and fila.get("ciudad"):
            return fila["ciudad"], categoria, None
    except Exception:
        pass

    # Formato "Categoría | Ciudad | País | Ronda, Superficie" (visto en
    # 01/08/2026 en una alerta para Svrcina vs Droguet: "ATP Masters
    # 1000 | Montreal | Canada | Qualifying, Hard"). Acá la ciudad NO es
    # el último token útil -- es el país -- así que el heurístico
    # genérico de abajo la pisaba con "Canada". Y antes de eso, como el
    # regex de separadores no incluye "|", el pipe sobrevivía como
    # token propio de 1 carácter y quedaba último, disparando
    # "torneo_no_parseado" directamente. Se resuelve aparte: el campo 2
    # (índice 1) es la ciudad, siempre.
    if "|" in base:
        campos = [c.strip() for c in base.split("|") if c.strip()]
        ciudad = campos[1].lower() if len(campos) >= 2 else ""
        if len(ciudad) < 4:
            return None, categoria, "torneo_no_parseado"
        return ciudad, categoria, None

    # Los separadores varían por fuente: punto, guion, barra, coma.
    tokens = [t for t in re.split(r"[.\-_/,]+|\s+", base) if t]
    utiles = [
        t for t in tokens
        if t.lower() not in _TOKENS_RUIDO_TORNEO
        # "250", "125", "1000", "M15", "W75", "M25" -- nivel, no ciudad.
        # También el número de edición suelto: "M25 Sarreguemines 1" y
        # "M15 Brisbane 2" son dos torneos distintos en la misma sede, y
        # ese "1"/"2" quedaba como ciudad.
        and not re.fullmatch(r"[wmWM]?\d{1,4}k?", t, flags=re.IGNORECASE)
    ]
    if not utiles:
        return None, categoria, "torneo_no_parseado"

    ciudad = utiles[-1].lower()
    # Mismo guard que el listado: con menos de 4 letras el LIKE
    # '%ciudad%' engancha cualquier cosa. Preferimos decir "no sé".
    if len(ciudad) < 4:
        return None, categoria, "torneo_no_parseado"
    return ciudad, categoria, None


def _es_misma_categoria(cat_fixture: str | None, categoria_hist: str) -> bool:
    """¿La categoría del histórico encaja con la del torneo en curso?"""
    if not cat_fixture:
        return False
    h = (categoria_hist or "").lower()
    if cat_fixture == "challenger":
        return h.startswith("challenger")
    if cat_fixture == "itf":
        return h.startswith("itf_")
    if cat_fixture == "wta125":
        return h == "wta125"
    if cat_fixture == "wta":
        return h in ("grand_slam", "wta1000", "wta500", "wta250")
    return h in ("grand_slam", "masters", "atp500", "atp250")


def _es_admin(request) -> bool:
    """
    Un usuario ve torneos de confianza media solo si es admin.

    Se resuelve con el mismo sistema de permisos del panel, no con una
    lista aparte: si mañana Maria crea un rol nuevo con ADMINISTRAR, hereda
    el acceso sin tocar este archivo.
    """
    try:
        from production.api.auth import usuario_actual
        from production.api.permisos import tiene_permiso, ADMINISTRAR
        u = usuario_actual(request) if request is not None else None
        if not u:
            # Acceso por token de research (scripts, curl): se trata como
            # admin. Quien tiene el token ya puede leerlo todo por otras
            # vías, así que ocultarle torneos no protegería nada.
            return True
        return tiene_permiso(u.get("rol", ""), ADMINISTRAR)
    except Exception:
        # Ante cualquier duda, NO admin: es el lado seguro. Un usuario
        # normal viendo de menos es un inconveniente; viendo datos que no
        # debería, es un problema.
        return False


# Umbral para que un torneo sea visible a los administradores. Por debajo
# de esto no se muestra a nadie todavía: la cobertura del histórico en
# ITF es demasiado baja para que el número signifique algo, y una
# columna vacía se lee como "no defiende nada" en vez de "no lo sé".
#
# Cuando la cobertura mejore, se sube o se elimina.
CONFIANZA_ADMIN = 0.70
CONFIANZA_TODOS = 1.0


@router.get("/api/defensa-torneos")
def api_defensa_torneos(request: Request, token: str = "", dias: int = Query(3, le=14)):
    """
    Torneos con actividad reciente. Es el primer nivel de la pestaña:
    elegís un torneo y recién ahí se ve quién defiende.

    La lista sale de `fixtures_valor`, que es lo que el bot ya llena con
    los partidos del día. No hay listas de inscritos en ninguna parte del
    sistema, así que "los torneos que se están jugando" se deduce de que
    haya partidos cargados. Consecuencia a tener presente: un torneo
    aparece cuando arrancan sus partidos, no cuando se publica el cuadro.
    """
    _check_token_o_sesion(request, token, ("defensapuntos",))
    conn = _get_conn_production()
    try:
        filas = conn.execute(
            """
            SELECT torneo, tour, genero,
                   COUNT(*)            AS partidos,
                   MAX(actualizado_en) AS ultima
              FROM fixtures_valor
             WHERE torneo IS NOT NULL AND torneo != ''
               AND actualizado_en >= datetime('now', ?)
             GROUP BY torneo, tour, genero
            """,
            (f"-{int(dias)} days",),
        ).fetchall()
    finally:
        conn.close()

    # Agrupar las filas de un mismo torneo, que vienen partidas por ronda.
    agrupado: dict = {}
    for f in filas:
        base, es_dobles = _partir_torneo(f["torneo"])
        if es_dobles or not base:
            continue
        clave = (base, (f["tour"] or "").upper(), f["genero"])
        acc = agrupado.setdefault(clave, {
            "torneo": base, "tour": (f["tour"] or "").upper(),
            "genero": f["genero"], "partidos": 0, "ultima": "",
        })
        acc["partidos"] += f["partidos"]
        if (f["ultima"] or "") > acc["ultima"]:
            acc["ultima"] = f["ultima"]

    # Confianza = ¿reconstruí bien la EDICIÓN ANTERIOR de este torneo?
    #
    # Se mide contra el cuadro de 2025, NO contra los inscritos de hoy, y
    # esa distinción es la clave. Los resultados del año pasado son un
    # hecho consumado: si Alcaraz ganó Washington 2025, esa fila está y
    # no cambia. Que este año se lesione y no venga no borra nada -- solo
    # hace que no aparezca en la lista de inscritos.
    #
    # La alternativa (medir sobre los inscritos de hoy) tenía un defecto
    # grave: `fixtures_valor` solo contiene los partidos del DÍA, así que
    # el porcentaje bailaba con cada ronda. Un torneo al 100% el lunes
    # podía estar al 40% el martes sin que ningún dato hubiera cambiado.
    #
    # El indicador es el podio: campeón, finalista y ambos semifinalistas.
    # Si esos cuatro están con sus puntos, la escalera de rondas se
    # dedujo bien y el resto del cuadro es fiable. Si falta alguno, algo
    # se rompió al derivar y no se muestra.
    admin = _es_admin(request)
    from datetime import date as _date
    season = _date.today().isocalendar()[0]
    conn = _get_conn_production()

    salida = []
    try:
        # 25/08/2026, a raíz de que el panel de rendimiento marcó esta
        # ruta en 5.1s: antes, CADA torneo del loop de abajo pegaba su
        # propia consulta con "LOWER(tournament_nombre_fuente) LIKE
        # '%ciudad%'" contra player_ranking_results -- un LIKE con el
        # comodín adelante no puede usar ningún índice, así que cada
        # torneo activo pagaba un escaneo completo de la tabla. Con 20-30
        # torneos activos (lo normal en temporada), eran 20-30 escaneos
        # completos en una sola visita a la pestaña.
        #
        # `edition_year` es SIEMPRE season-1 (se calculó una sola vez,
        # arriba, antes del loop) -- lo único que cambiaba por iteración
        # era `tour` (2-3 valores posibles en la práctica) y la ciudad
        # del LIKE. Se trae TODO lo de esa temporada en una sola consulta
        # (con índice sobre edition_year, no un escaneo) y el filtro por
        # ciudad se hace en Python contra esa lista ya en memoria -- el
        # resultado es idéntico, solo que ahora es 1 consulta en vez de
        # 20-30.
        _filas_ranking_temporada = conn.execute(
            "SELECT tournament_nombre_fuente, categoria, ronda_alcanzada, tour,"
            "       COUNT(*), MIN(ranking_week)"
            "  FROM player_ranking_results"
            " WHERE edition_year=? AND es_qualy=0"
            "   AND estado='ok' AND puntos_derivados IS NOT NULL"
            " GROUP BY 1,2,3,4",
            (season - 1,),
        ).fetchall()
        # Se pre-baja el nombre a minúscula UNA vez acá, no una vez por
        # cada torneo del loop (antes lo hacía LOWER() del lado de SQL,
        # en cada una de las 20-30 consultas).
        _ranking_por_tour: dict[str, list[tuple]] = {}
        for nom, cat, ronda, tour_fila, n, sem in _filas_ranking_temporada:
            _ranking_por_tour.setdefault((tour_fila or "").upper(), []).append(
                ((nom or "").lower(), nom, cat, ronda, n, sem))

        for t in agrupado.values():
            ciudad = t["torneo"].split()[-1].lower()
            cat_fix = _categoria_desde_nombre(t["torneo"], t["tour"])
            podio = {}
            if len(ciudad) >= 4 and cat_fix:
                # Se agrupa por torneo Y ronda, no solo por ronda: si en
                # la misma ciudad hubo varias ediciones de la misma
                # categoría (M15 Brisbane 1 y 2), hay que quedarse con
                # UNA, no sumarlas. Se elige la que tenga el podio más
                # completo.
                por_torneo, semanas = {}, {}
                for nom_lower, nom, cat, ronda, n, sem in _ranking_por_tour.get(
                        (t["tour"] or "").upper(), []):
                    if ciudad not in nom_lower:
                        continue
                    if not _es_misma_categoria(cat_fix, cat):
                        continue
                    por_torneo.setdefault(nom, {})[ronda] = n
                    if sem:
                        semanas[nom] = sem
                if por_torneo:
                    # Elegir la edición por SEMANA DE CALENDARIO, no la
                    # más completa. Kursumlijska Banja encadenó 17
                    # torneos masculinos en 2025 y 21 femeninos: quedarse
                    # con "el que mejor pinta tenga" mostraba los puntos
                    # de un evento distinto al que se juega ahora, con
                    # números perfectamente creíbles.
                    #
                    # La edición correcta es la de la misma semana del
                    # año pasado. Se admite ±1 por si el calendario se
                    # movió; más allá, no se asume nada.
                    def _puntaje(d):
                        return (min(d.get("W", 0), 1) + min(d.get("F", 0), 1)
                                + min(d.get("SF", 0), 2))

                    semana_actual = _date.today().isocalendar()[1]
                    cercanas = {n: d for n, d in por_torneo.items()
                                if n in semanas
                                and abs(semanas[n] - semana_actual) <= 1}
                    candidatas = cercanas or por_torneo
                    elegido = max(candidatas, key=lambda n: _puntaje(candidatas[n]))
                    podio = candidatas[elegido]
                    t["edicion_previa"] = elegido
                    t["ediciones_candidatas"] = len(por_torneo)
                    t["semana_previa"] = semanas.get(elegido)
                    # Sin una edición en la semana correcta, no se puede
                    # saber cuál toca: se muestra pero rebajado, para que
                    # el admin lo revise en vez de creerlo a ciegas.
                    t["semana_incierta"] = (not cercanas) and len(por_torneo) > 1

            # "No se jugó el año pasado" y "no consigo emparejarlo" dan el
            # mismo resultado -- podio vacío -- pero significan cosas
            # opuestas. El primero es un dato cierto (nadie defiende); el
            # segundo es un fallo mío que se estaba mostrando como 100%.
            #
            # Se distinguen mirando si en esa ciudad hay ALGO en el
            # histórico. Si no hay nada, es plausible que el torneo sea
            # nuevo. Si hay torneos pero ninguno de la categoría correcta,
            # el emparejamiento falló y no se muestra.
            algo_en_ciudad = False
            if len(ciudad) >= 4:
                algo_en_ciudad = conn.execute(
                    "SELECT 1 FROM player_ranking_results"
                    " WHERE edition_year=? AND LOWER(tournament_nombre_fuente) LIKE ?"
                    " LIMIT 1", (season - 1, f"%{ciudad}%")).fetchone() is not None

            hubo_edicion = bool(podio)
            t["sin_emparejar"] = (not podio) and algo_en_ciudad
            # W=1, F=1, SF=2. Se piden por nombre de ronda porque así se
            # detecta también un cuadro mal deducido: si la escalera salió
            # corrida, las etiquetas no cuadran y el torneo cae solo.
            tiene = (podio.get("W", 0) >= 1 and podio.get("F", 0) >= 1
                     and podio.get("SF", 0) >= 2)

            t["hubo_edicion"] = hubo_edicion
            t["podio"] = {k: podio.get(k, 0) for k in ("W", "F", "SF")}

            if t["sin_emparejar"]:
                # Hay torneos en esa ciudad pero ninguno encaja. Es un
                # fallo de emparejamiento, no un torneo nuevo: fuera.
                t["confianza"], t["nivel"] = 0.0, "insuficiente"
            elif not hubo_edicion:
                # Nada en esa ciudad: plausiblemente un torneo nuevo.
                # "Nadie defiende nada" es un dato CIERTO, no un hueco.
                t["confianza"], t["nivel"] = 1.0, "nuevo"
            elif tiene and not t.get("semana_incierta"):
                t["confianza"], t["nivel"] = 1.0, "completo"
            elif tiene:
                # Podio completo pero de una edición que no es la de esta
                # semana. El dato existe; que sea EL correcto, no consta.
                t["confianza"], t["nivel"] = 0.75, "parcial"
            else:
                # Falta parte del podio. Se gradúa para que un admin pueda
                # ver los que están casi completos y detectar qué falla.
                presentes = (min(podio.get("W", 0), 1) + min(podio.get("F", 0), 1)
                             + min(podio.get("SF", 0), 2))
                t["confianza"] = presentes / 4
                t["nivel"] = ("parcial" if t["confianza"] >= CONFIANZA_ADMIN
                              else "insuficiente")

            # 'completo' y 'nuevo' los ve todo el mundo: en los dos casos
            # el dato es cierto. 'parcial' solo administradores, rotulado.
            # 'insuficiente' no se muestra todavía a nadie.
            if t["nivel"] == "insuficiente":
                continue
            if t["nivel"] == "parcial" and not admin:
                continue
            salida.append(t)
    finally:
        conn.close()

    # Primero los completos, y dentro de cada grupo los más recientes.
    orden = {"completo": 0, "nuevo": 1, "parcial": 2}
    salida.sort(key=lambda t: (orden.get(t["nivel"], 9), -(t["partidos"] or 0)))
    ocultos = len(agrupado) - len(salida)
    return {"dias": dias, "es_admin": admin, "ocultos": ocultos,
            "umbral_admin": CONFIANZA_ADMIN, "torneos": salida}


@router.get("/api/proximos-partidos")
def api_proximos_partidos(jugador: str, genero: str = ""):
    """
    Próximos partidos de UN jugador (08/08/2026, a pedido de Rubén:
    "en defensa de puntos, cuando selecciono una persona que está
    defendiendo, que me salgan los partidos que vienen").

    Sirve para leer la defensa con contexto: saber que alguien defiende
    500 puntos es la mitad de la historia; la otra mitad es contra quién
    juega y cuándo.

    Sale de `fixtures_ftr_elo`, o sea de lo que el BOT ya reportó. Eso
    tiene un límite que el JS muestra tal cual: el bot reporta con horas
    de anticipación, así que un jugador puede estar defendiendo un
    torneo entero y no tener todavía ningún partido acá. "Sin partidos
    reportados" NO significa "no juega".
    """
    from shared.tennis_names import normalize_name
    objetivo = normalize_name(jugador or "")
    if not objetivo:
        return {"jugador": jugador, "partidos": []}
    conn = _get_conn_production()
    try:
        filas = conn.execute(
            "SELECT fixture_id, fecha, torneo, superficie, jugador1, jugador2, "
            "       favorito_nombre, prob_ftr, prob_elo, es_valor, genero "
            "FROM fixtures_ftr_elo WHERE fecha >= date('now','-1 day') "
            "ORDER BY fecha ASC LIMIT 3000").fetchall()
    finally:
        conn.close()

    salida = []
    for f in filas:
        n1, n2 = normalize_name(f["jugador1"] or ""), normalize_name(f["jugador2"] or "")
        if objetivo not in (n1, n2):
            continue
        rival = f["jugador2"] if objetivo == n1 else f["jugador1"]
        salida.append({
            "fixture_id": f["fixture_id"], "fecha": f["fecha"], "torneo": f["torneo"],
            "superficie": f["superficie"], "rival": rival,
            "genero": f["genero"] or genero or "M",
            "favorito": f["favorito_nombre"],
            "es_favorito": (f["favorito_nombre"] or "") and
                           normalize_name(f["favorito_nombre"]) == objetivo,
            "prob_ftr": f["prob_ftr"], "prob_elo": f["prob_elo"],
            "es_valor": bool(f["es_valor"]),
        })
    return {"jugador": jugador, "total": len(salida), "partidos": salida[:10]}


@router.get("/api/defensa-torneo")
def api_defensa_torneo(request: Request, torneo: str, token: str = "",
                       tour_hint: str = ""):
    """
    Quién defiende puntos en UN torneo.

    Devuelve DOS números por jugador, que no son lo mismo:

      defiende_aqui      lo que ganó en ESTE torneo el año pasado. Es lo
                         que la gente entiende por "defiende puntos en
                         Toronto". Requiere haber podido emparejar el
                         nombre del torneo con el histórico.

      pierde_esta_semana lo que se le cae de la ventana de 52 semanas
                         esta semana, venga del torneo que venga. Este NO
                         depende de emparejar nada, así que está siempre.

    Cuando difieren es el caso más interesante: alguien que llega a
    Toronto sin defender nada acá, pero igual pierde 250 porque el año
    pasado esa semana jugaba otro torneo. Eso hoy no lo muestra nadie.
    """
    _check_token_o_sesion(request, token, ("defensapuntos",))
    if not (torneo or "").strip():
        raise HTTPException(status_code=400, detail="falta el parámetro torneo")

    from datetime import date as _date
    season, semana, _ = _date.today().isocalendar()
    lunes = _date.fromisocalendar(season, semana, 1).isoformat()
    domingo = _date.fromisocalendar(season, semana, 7).isoformat()

    # Nombre del torneo -> identidad canónica, con el normalizador que ya
    # existe. Si no resuelve, se sigue igual: `defiende_aqui` queda en
    # null y la interfaz lo muestra como "sin dato", no como cero.
    canon = None
    try:
        from shared.tournament_names import normalizar_torneo, canonical_id
        canon = canonical_id(normalizar_torneo(torneo))
    except Exception:
        canon = None

    # FIX 01/08/2026: antes era `torneo.split()[-1].lower()`, que sobre
    # "WTA Washington - SF" devolvía "sf" y sobre "ATP. MONTREAL. HARD"
    # devolvía "hard". El listado (/api/defensa-torneos) sí aplicaba
    # _partir_torneo() antes de sacar la ciudad; este endpoint no, así
    # que el cruce con el histórico fallaba siempre que el nombre
    # traía la ronda pegada -- que es casi siempre.
    ciudad_t, cat_fix_t, _motivo_t = _ciudad_y_categoria(torneo, tour_hint)

    conn = _get_conn_production()
    try:
        # Los jugadores salen de fixtures_valor. OJO: jugador1_id y
        # jugador2_id están SIEMPRE en NULL -- el bot guarda los nombres
        # pero nunca resuelve los ids. Así que se resuelve acá, por
        # nombre, con el mismo tennis_names.py que usa el derivador.
        patron = torneo + " - %"
        crudas = conn.execute(
            """
            SELECT torneo, jugador1_nombre AS nombre FROM fixtures_valor
             WHERE (torneo=? OR torneo LIKE ?) AND jugador1_nombre IS NOT NULL
            UNION
            SELECT torneo, jugador2_nombre FROM fixtures_valor
             WHERE (torneo=? OR torneo LIKE ?) AND jugador2_nombre IS NOT NULL
            """, (torneo, patron, torneo, patron)).fetchall()

        try:
            from shared.tennis_names import find_player_id
        except ImportError:
            find_player_id = None

        genero = "F" if (tour_hint or "").upper() == "WTA" else "M"
        vistos, jugadores = set(), []
        for c in crudas:
            _base, es_dobles = _partir_torneo(c["torneo"])
            nombre = (c["nombre"] or "").strip()
            # Los dobles traen "Bara / Karamoko" en un solo campo: no es
            # un jugador y no se puede resolver.
            if es_dobles or not nombre or "/" in nombre:
                continue
            pid = None
            if find_player_id:
                try:
                    pid = find_player_id(conn, nombre, genero)
                except Exception:
                    pid = None
            clave = pid if pid is not None else f"n:{nombre.lower()}"
            if clave in vistos:
                continue
            vistos.add(clave)
            jugadores.append({"pid": pid, "nombre": nombre})

        salida = []
        for j in jugadores:
            pid = j["pid"]
            base = None
            if pid is not None:
                base = conn.execute(
                    "SELECT nombre_canonico, rank_oficial, puntos_oficiales "
                    "FROM players WHERE player_id=?", (pid,)).fetchone()
            # Un jugador sin resolver SÍ se muestra, con guiones en las
            # columnas que dependen de la base. Descartarlo daría la
            # impresión de que no está inscrito, que es peor que decir
            # "está, pero no sé sus puntos".
            if base is None:
                salida.append({"player_id": None, "nombre": j["nombre"],
                               "rank": None, "puntos": None,
                               "defiende_aqui": None, "ronda_aqui": None,
                               "pierde_esta_semana": None, "presion": None})
                continue

            # Cruce por NOMBRE del torneo, no por id: el histórico usa
            # los ids de Matchstat ("21147") y fixtures_valor guarda
            # nombres ("ATP Washington"). No hay forma de cruzarlos por
            # clave, pero el derivador sí guardó `tournament_nombre_fuente`
            # ("M25 Sarreguemines 1"), y por ahí sí se puede.
            #
            # Se compara sobre la última palabra significativa -- la
            # ciudad -- porque los prefijos no coinciden entre fuentes:
            # "ATP Washington" contra "Washington DC". Es aproximado a
            # propósito, y por eso el resultado va rotulado como tal.
            # Mismo cruce que el listado: ciudad Y categoría. Sin filtrar
            # por categoría, un jugador que ganó el M15 de Brisbane
            # aparecería defendiendo en el ATP 250 de Brisbane, que es
            # otro torneo. En esa ciudad hubo catorce eventos distintos
            # en 2025.
            aqui = None
            if ciudad_t and cat_fix_t:
                # Ordenado por cercanía a la semana actual, no por
                # puntos: en sedes que encadenan torneos (Kursumlijska
                # Banja tuvo 17 en 2025) el mejor resultado del jugador
                # puede ser de otra semana y de otro evento.
                candidatas = []
                for r in conn.execute(
                        "SELECT puntos_derivados, ronda_alcanzada, categoria,"
                        "       tournament_nombre_fuente, ranking_week"
                        "  FROM player_ranking_results"
                        " WHERE player_id=? AND edition_year=? AND es_qualy=0"
                        "   AND estado='ok' AND LOWER(tournament_nombre_fuente) LIKE ?",
                        (pid, season - 1, f"%{ciudad_t}%")):
                    if _es_misma_categoria(cat_fix_t, r["categoria"]):
                        candidatas.append(dict(r))
                if candidatas:
                    sem = _date.today().isocalendar()[1]
                    candidatas.sort(key=lambda x: (abs((x["ranking_week"] or 99) - sem),
                                                   -(x["puntos_derivados"] or 0)))
                    aqui = candidatas[0]

            semana_r = conn.execute(
                "SELECT SUM(puntos_derivados) AS pts FROM player_ranking_results"
                " WHERE player_id=? AND drop_date BETWEEN ? AND ?"
                "   AND es_qualy=0 AND estado='ok'",
                (pid, lunes, domingo)).fetchone()

            total = base["puntos_oficiales"]
            cae = (semana_r["pts"] if semana_r else None) or 0
            salida.append({
                "player_id": pid,
                "nombre": base["nombre_canonico"] or j["nombre"],
                "rank": base["rank_oficial"],
                "puntos": total,
                "defiende_aqui": aqui["puntos_derivados"] if aqui else None,
                "ronda_aqui": aqui["ronda_alcanzada"] if aqui else None,
                "pierde_esta_semana": cae,
                "presion": (cae / total) if total else None,
            })
    except sqlite3.OperationalError as e:
        if "no such table" in str(e):
            raise HTTPException(
                status_code=503,
                detail="Falta aplicar schema_defensa.sql sobre la base de produccion.")
        raise
    finally:
        conn.close()

    # Primero los que más se juegan. Los que no defienden nada quedan
    # abajo, que es donde molestan menos.
    salida.sort(key=lambda x: (-(x["defiende_aqui"] or 0), -(x["pierde_esta_semana"] or 0)))
    return {
        "torneo": torneo,
        "canonical_id": canon,
        "torneo_resuelto": True,   # el cruce ahora es por nombre, siempre se intenta
        "season": season,
        "semana": semana,
        "ciudad_detectada": ciudad_t,
        "categoria_detectada": cat_fix_t,
        "jugadores": salida,
    }


# ── Defensa de puntos de DOS jugadores puntuales ─────────────────────
#
# Por qué existe (01/08/2026): el modal A vs B hacía una pregunta de dos
# jugadores a través de /api/defensa-torneo, que es un endpoint de torneo
# entero. La cadena era:
#
#   string torneo -> LIKE contra fixtures_valor -> lista de inscritos
#   -> find_player_id() -> nombre_canonico -> comparación exacta de
#      string en el JS del modal
#
# Seis saltos, y cuatro sobraban: el modal ya sabe exactamente qué dos
# jugadores quiere. Peor: el último salto comparaba nombres como texto,
# la misma clase de bug que el de "Shnaider D 🇷🇺" -- bastaba con que el
# nombre resuelto por research Elo difiriera del nombre_canonico de
# producción en un espacio para que dijera "sin dato" sin explicar nada.
#
# Acá se resuelve a cada jugador por separado y la respuesta viene
# indexada por "a"/"b", NO por nombre. El JS no vuelve a comparar
# strings nunca.
def _defensa_de_un_jugador(conn, nombre, genero, ciudad, categoria,
                           motivo_torneo, season, lunes, domingo):
    """
    Defensa de UN jugador. Devuelve siempre un dict con `motivo`, que
    distingue los casos que antes se veían todos iguales:

      None                  -> hay dato bueno
      "no_defiende"         -> jugó el año pasado acá y no sumó nada
      "sin_historial"       -> el torneo se identificó, el jugador no
                               tiene registro de esa ciudad/categoría
      "jugador_no_resuelto" -> no está en `players` de producción
      "torneo_no_parseado" / "dobles" / "torneo_vacio" -> el nombre del
                               torneo no dio ciudad utilizable

    `pierde_esta_semana` se calcula IGUAL en todos los casos: no depende
    de emparejar el torneo, solo de la ventana de 52 semanas. Es el
    número más robusto de los dos y por eso se devuelve siempre que el
    jugador se haya podido resolver.
    """
    base_out = {
        "nombre_consultado": nombre, "player_id": None, "nombre": nombre,
        "rank": None, "puntos": None, "defiende_aqui": None,
        "ronda_aqui": None, "pierde_esta_semana": None, "presion": None,
        "motivo": None,
    }
    nombre = _sin_simbolos_nombre(nombre)
    if not nombre:
        base_out["motivo"] = "jugador_no_resuelto"
        return base_out

    try:
        from shared.tennis_names import find_player_id
        pid = find_player_id(conn, nombre, genero)
    except Exception:
        pid = None
    if pid is None:
        base_out["motivo"] = "jugador_no_resuelto"
        return base_out

    fila = conn.execute(
        "SELECT nombre_canonico, rank_oficial, puntos_oficiales "
        "FROM players WHERE player_id=?", (pid,)).fetchone()
    base_out["player_id"] = pid
    if fila:
        base_out["nombre"] = fila["nombre_canonico"] or nombre
        base_out["rank"] = fila["rank_oficial"]
        base_out["puntos"] = fila["puntos_oficiales"]

    # Lo que se cae de la ventana de 52 semanas ESTA semana, venga del
    # torneo que venga. No depende de identificar el torneo.
    sem = conn.execute(
        "SELECT SUM(puntos_derivados) AS pts FROM player_ranking_results"
        " WHERE player_id=? AND drop_date BETWEEN ? AND ?"
        "   AND es_qualy=0 AND estado='ok'",
        (pid, lunes, domingo)).fetchone()
    cae = (sem["pts"] if sem else None) or 0
    base_out["pierde_esta_semana"] = cae
    total = base_out["puntos"]
    base_out["presion"] = (cae / total) if total else None

    if motivo_torneo or not ciudad or not categoria:
        base_out["motivo"] = motivo_torneo or "torneo_no_parseado"
        return base_out

    # Lo que ganó en ESTE torneo el año pasado. Mismo criterio que
    # /api/defensa-torneo: ciudad Y categoría, y entre varias ediciones
    # de la misma sede se elige la de la semana de calendario más
    # cercana (Kursumlijska Banja encadenó 17 torneos en 2025).
    candidatas = []
    for r in conn.execute(
            "SELECT puntos_derivados, ronda_alcanzada, categoria,"
            "       tournament_nombre_fuente, ranking_week"
            "  FROM player_ranking_results"
            " WHERE player_id=? AND edition_year=? AND es_qualy=0"
            "   AND estado='ok' AND LOWER(tournament_nombre_fuente) LIKE ?",
            (pid, season - 1, f"%{ciudad}%")):
        if _es_misma_categoria(categoria, r["categoria"]):
            candidatas.append(dict(r))

    if not candidatas:
        base_out["motivo"] = "sin_historial"
        return base_out

    from datetime import date as _date
    semana_hoy = _date.today().isocalendar()[1]
    candidatas.sort(key=lambda x: (abs((x["ranking_week"] or 99) - semana_hoy),
                                   -(x["puntos_derivados"] or 0)))
    elegida = candidatas[0]
    base_out["defiende_aqui"] = elegida["puntos_derivados"]
    base_out["ronda_aqui"] = elegida["ronda_alcanzada"]
    base_out["torneo_previo"] = elegida["tournament_nombre_fuente"]
    if not elegida["puntos_derivados"]:
        base_out["motivo"] = "no_defiende"
    return base_out


@router.get("/api/defensa-jugadores")
def api_defensa_jugadores(
    request: Request,
    a: str,
    b: str = "",
    torneo: str = "",
    genero: str = "M",
    token: str = "",
):
    """
    Defensa de puntos de DOS jugadores en un torneo. Pensado para el
    modal A vs B (ver _defensa_de_un_jugador para el porqué).

    Respuesta indexada por "a"/"b". Nunca 404 ni 500 por falta de dato:
    lo que no se pudo resolver viene con `motivo`, para que la interfaz
    pueda decir QUÉ falló en vez de un "sin dato" mudo.
    """
    _check_token_o_sesion(request, token, ("defensapuntos",))
    if not (a or "").strip():
        raise HTTPException(status_code=400, detail="falta el parámetro a")

    from datetime import date as _date
    season, semana, _ = _date.today().isocalendar()
    lunes = _date.fromisocalendar(season, semana, 1).isoformat()
    domingo = _date.fromisocalendar(season, semana, 7).isoformat()

    genero = "F" if (genero or "M").upper() == "F" else "M"
    tour_hint = "WTA" if genero == "F" else "ATP"
    ciudad, categoria, motivo_torneo = _ciudad_y_categoria(torneo, tour_hint)

    conn = _get_conn_production()
    try:
        jugadores = {}
        for clave, nombre in (("a", a), ("b", b)):
            if not (nombre or "").strip():
                continue
            jugadores[clave] = _defensa_de_un_jugador(
                conn, nombre, genero, ciudad, categoria, motivo_torneo,
                season, lunes, domingo)
    except sqlite3.OperationalError as e:
        if "no such table" in str(e):
            raise HTTPException(
                status_code=503,
                detail="Falta aplicar schema_defensa.sql sobre la base de produccion.")
        raise
    finally:
        conn.close()

    return {
        "torneo": torneo,
        "ciudad_detectada": ciudad,
        "categoria_detectada": categoria,
        "motivo_torneo": motivo_torneo,
        "season": season,
        "semana": semana,
        "jugadores": jugadores,
    }


# ═════════════════════════════════════════════════════════════════════
#  🎯 UTR: RANKING PROPIO Y ΔUTR EN TODOS LOS PARTIDOS  (08/10/2026)
# ═════════════════════════════════════════════════════════════════════
#
# Rubén: "que el UTR sea una nueva tab con su propio ranking · en el modal y
# comparador A vs B y demás calcular la diferencia de UTR en todos los
# partidos". Todo sale de production/engines/utr_diferencia.py, que lee el
# padrón igual que los motores (exacto, o "11.xx" como rango, sin inventar
# decimales). Solo lectura. Si el padrón no está, estas piezas devuelven
# None y la pantalla sigue igual que antes.

def _utr_par_seguro(n1: str, n2: str, genero: str) -> Optional[dict]:
    try:
        from production.engines.utr_diferencia import Resolvedor
        conn = _get_conn_production()
        try:
            return Resolvedor(conn).par(n1, n2, genero)
        finally:
            conn.close()
    except Exception as e:
        logging.getLogger(__name__).debug(f"[UTRDif] compare sin UTR: {e}")
        return None


def _utr_comparar(a, b):
    from production.engines.utr_diferencia import comparar
    return comparar(a, b)


def _utr_historial_seguro(nombre: str, genero: str, rivales: list) -> tuple:
    try:
        from production.engines.utr_diferencia import Resolvedor
        conn = _get_conn_production()
        try:
            res = Resolvedor(conn)
            yo = res.jugador(nombre, genero)
            return yo, [res.jugador(r, genero) for r in rivales]
        finally:
            conn.close()
    except Exception as e:
        logging.getLogger(__name__).debug(f"[UTRDif] historial sin UTR: {e}")
        return None, [None] * len(rivales)


def _completar_bio_utr(conn, genero: str, filas: list) -> None:
    """Edad, altura y mano de cada fila del Ranking UTR (las mismas tres
    columnas del Ranking FTR). Salen de lo que FullTenis ya tiene: `players.edad`
    por player_id y `player_bio` por nombre. Nunca rompe el ranking: sin dato,
    queda en None y la pantalla muestra "—"."""
    for f in filas:
        f.setdefault("edad", None); f.setdefault("altura_cm", None); f.setdefault("mano_dominante", None)
    try:
        from production.api.routes_public import _bio_bulk_por_genero, _bio_de
        indice = _bio_bulk_por_genero(genero)
        ids = [f["player_id"] for f in filas if f.get("player_id")]
        edad_pid: dict = {}
        if ids:
            marcas = ",".join("?" * len(ids))
            for r in conn.execute(f"SELECT player_id, edad FROM players WHERE player_id IN ({marcas})", ids).fetchall():
                edad_pid[r[0]] = r[1]
        for f in filas:
            bio = _bio_de(indice, f.get("nombre") or "", genero)
            e = edad_pid.get(f.get("player_id"))
            f["edad"] = e if e is not None else bio.get("edad")
            f["altura_cm"] = bio.get("altura_cm")
            f["mano_dominante"] = bio.get("mano_dominante")
    except Exception as e:
        logging.getLogger(__name__).debug(f"[UTRRank] sin bio: {e}")


@router.get("/api/utr/ranking")
def api_utr_ranking(request: Request, token: str = "", genero: str = "M", q: str = "",
                    solo_exactos: bool = False, solo_confirmados: bool = False, fuente: str = "",
                    superficie: str = "general",
                    pagina: int = 1, por_pagina: int = Query(100, le=500)):
    """🎯 Ranking UTR: todos los jugadores con UTR que conoce FullTenis, de
    mayor a menor, por sexo. La posición es la de todo el sexo aunque se
    busque o filtre. `superficie` (general/clay/hard/grass) arma el ranking de
    esa cancha con el UTR de superficie de cada jugador. Cada fila trae edad,
    altura y mano como el Ranking FTR."""
    _check_token_o_sesion(request, token, ["utr"])
    from production.engines.utr_diferencia import ranking
    conn = _get_conn_production()
    try:
        r = ranking(conn, genero=genero, q=q, solo_exactos=solo_exactos,
                    solo_confirmados=solo_confirmados, fuente=fuente, pagina=pagina,
                    por_pagina=por_pagina, superficie=superficie)
        _completar_bio_utr(conn, r["genero"], r["filas"])
        return r
    finally:
        conn.close()


# ═════════════════════════════════════════════════════════════════════
#  📰 FT NEWS INTELLIGENCE · Fase 0 (08/10/2026). La pestaña nace vacía y
#  solo admin: muestra el estado del servicio FT_NEWS (apagado) y el
#  informe de calibración de bandas con datos propios. No escribe nada.
# ═════════════════════════════════════════════════════════════════════
@router.get("/api/news/calibracion")
def api_news_calibracion(request: Request, token: str = "", desde: str = "", formato: str = "json"):
    """Informe de calibración de bandas UTR/cuota (production/engines/news_calibracion.py). Solo admin."""
    _check_token_o_sesion(request, token, ["news"])
    from production.api.auth import requiere_admin
    requiere_admin(request)
    from production.engines import news_calibracion as nc
    conn = _get_conn_production()
    try:
        inf = nc.informe(conn, desde or None)
    finally:
        conn.close()
    if formato == "md":
        return PlainTextResponse(nc.informe_markdown(inf), media_type="text/markdown; charset=utf-8")
    return inf


def _ft_news(ruta: str, metodo: str = "GET", cuerpo: Optional[dict] = None) -> dict:
    """Lectura del servicio FT_NEWS (Railway), si está configurado: FT_NEWS_URL y
    FT_NEWS_SECRET. Sin eso, lo dice y no llama a nadie."""
    url = os.getenv("FT_NEWS_URL", "").strip().rstrip("/")
    secreto = os.getenv("FT_NEWS_SECRET", "").strip()
    if not url or not secreto:
        return {"configurado": False, "motivo": "FT_NEWS_URL / FT_NEWS_SECRET sin configurar en Railway"}
    import httpx
    try:
        h = {"X-FT-News-Secret": secreto}
        r = httpx.post(url + ruta, headers=h, json=cuerpo or {}, timeout=15) if metodo == "POST" \
            else httpx.get(url + ruta, headers=h, timeout=15)
        if r.status_code != 200:
            return {"configurado": True, "ok": False, "motivo": f"FT_NEWS respondió {r.status_code}: {r.text[:200]}"}
        return {"configurado": True, "ok": True, "estado": r.json()}
    except Exception as e:
        return {"configurado": True, "ok": False, "motivo": f"No se pudo hablar con FT_NEWS: {e}"}


@router.get("/api/news/estado")
def api_news_estado(request: Request, token: str = ""):
    _check_token_o_sesion(request, token, ["news"])
    from production.api.auth import requiere_admin
    requiere_admin(request)                       # 08/10/2026 noche: solo admin ve el panel completo
    return _ft_news("/news/estado")


@router.get("/api/news/control")
def api_news_control(request: Request, token: str = ""):
    """📋 v54j (16/10/2026, Rubén: «no sé si están funcionando o leyendo o qué están haciendo»): partido por partido,
    qué está mirando y haciendo FT NEWS (FT_NEWS v7.9, /news/control). Solo admin."""
    _check_token_o_sesion(request, token, ["news"])
    from production.api.auth import requiere_admin
    requiere_admin(request)
    r = _ft_news("/news/control")
    if r.get("ok") is False and "404" in str(r.get("motivo") or ""):
        r["motivo"] = "FT_NEWS todavía no tiene el control: hay que subir FT_NEWS v7.9."
    return r


@router.get("/api/news/portada")
def api_news_portada(request: Request, token: str = ""):
    """v52 (13/10/2026, Rubén: «que FT NEWS se vea así como lo habíamos planeado»): la portada tipo periódico.
    Lo ve quien ve la pestaña: FT_NEWS (v7.4, /news/portada) ya la arma solo con datos para usuarios. Un fallo
    se cuenta con detalle solo a un admin."""
    _check_token_o_sesion(request, token, ["news"])
    r = _ft_news("/news/portada")
    if r.get("ok"):
        _news_fotos(r["estado"])
        return {"ok": True, "portada": r["estado"]}
    try:
        from production.api.auth import requiere_admin
        requiere_admin(request)
        detalle = r.get("motivo")
    except HTTPException:
        detalle = None
    return {"ok": False, "motivo": "La portada de FT NEWS no está disponible en este momento.", "detalle_admin": detalle}


def _news_fotos(datos) -> None:
    """18/10/2026 (Rubén: «fotos de fuentes no pagas»): donde FT_NEWS no trae foto, la de Wikimedia Commons (vía
    Wikidata) con licencia libre y su crédito (production/engines/news_fotos.py). Si algo falla, la portada sale igual."""
    try:
        from production.api.deps import get_conn as _gc
        from production.engines import news_fotos
        c = _gc()
        pend = []
        try:
            # v2: en la carga solo la caché (la página no espera); lo que falta se busca en un hilo aparte
            news_fotos.completar(c, datos, max_nuevas=0, pendientes=pend)
        finally:
            c.close()
        news_fotos.buscar_en_segundo_plano(_gc, pend)
    except Exception as e:  # noqa: BLE001
        logging.getLogger(__name__).warning(f"[News] fotos: {e}")


def _news_portada_detalle(request: Request, ruta: str) -> dict:
    r = _ft_news(ruta)
    if r.get("ok"):
        _news_fotos(r["estado"])
        return {"ok": True, "detalle": r["estado"]}
    try:
        from production.api.auth import requiere_admin
        requiere_admin(request)
        detalle = r.get("motivo")
    except HTTPException:
        detalle = None
    no_existe = "404" in str(r.get("motivo") or "")
    return {"ok": False, "motivo": "Eso ya no está en la portada." if no_existe else "El detalle no está disponible en este momento.",
            "detalle_admin": detalle}


@router.get("/api/news/portada/partido/{alerta_id}")
def api_news_portada_partido(request: Request, alerta_id: int, token: str = ""):
    """v53 (13/10/2026, Rubén: «si le doy clic se debe abrir el modal con todo lo que encontró»): el detalle de un 🔎/🔥
    de la portada (FT_NEWS v7.6). Lo ve quien ve la pestaña: FT_NEWS ya lo arma solo con datos para usuarios."""
    _check_token_o_sesion(request, token, ["news"])
    return _news_portada_detalle(request, f"/news/portada/partido/{int(alerta_id)}")


@router.get("/api/news/portada/agenda/{partido_id}")
def api_news_portada_agenda(request: Request, partido_id: int, token: str = ""):
    """v54 (13/10/2026, Rubén: «que en estos partidos también tenga la posibilidad de ver el modal»): cualquier partido
    de la agenda (FT_NEWS v7.7): qué investigó FT NEWS de los dos y por qué no salió alerta."""
    _check_token_o_sesion(request, token, ["news"])
    return _news_portada_detalle(request, f"/news/portada/agenda/{int(partido_id)}")


@router.get("/api/news/portada/noticia/{evento_id}")
def api_news_portada_noticia(request: Request, evento_id: int, token: str = ""):
    """v53: el detalle de una noticia de la portada (FT_NEWS v7.6)."""
    _check_token_o_sesion(request, token, ["news"])
    return _news_portada_detalle(request, f"/news/portada/noticia/{int(evento_id)}")


@router.get("/api/news/fotos")
def api_news_fotos(request: Request, token: str = ""):
    """18/10/2026 · admin: qué fotos se encontraron en Wikimedia Commons y por qué faltan las otras."""
    _check_token_o_sesion(request, token, ["news"])
    from production.api.auth import requiere_admin
    requiere_admin(request)
    from production.api.deps import get_conn as _gc
    from production.engines import news_fotos
    c = _gc()
    try:
        return news_fotos.resumen(c)
    finally:
        c.close()


@router.get("/api/news/hoy")
def api_news_hoy(request: Request, token: str = ""):
    """Vista Hoy: evaluaciones (🔥/🔎), eventos recientes, cola y costos de FT_NEWS. Solo admin."""
    _check_token_o_sesion(request, token, ["news"])
    from production.api.auth import requiere_admin
    requiere_admin(request)
    return _ft_news("/news/hoy")


@router.get("/api/dashboard/ft-news")
def api_dashboard_ft_news(request: Request, token: str = "", vista: str = "discord", genero: str = "",
                          limite: int = Query(500, le=3000)):
    """📰 Dashboard de FT NEWS para todos los usuarios: solo las 🔥 dichas en Discord,
    ganadas/perdidas e historial (lo calcula FT_NEWS; acá se reenvía con la unidad de la cuenta)."""
    _check_token_o_sesion(request, token, ["news"])
    from production.engines import dashboard_estrategia as de
    conn = _get_conn_production()
    try:
        vu = de.valor_unidad_de(conn, request)
    finally:
        conn.close()
    r = _ft_news(f"/news/dashboard?genero={genero}&valor_unidad={vu}&limite={limite}")
    if not r.get("configurado") or not r.get("ok"):
        raise HTTPException(status_code=503, detail=r.get("motivo") or "FT_NEWS no disponible")
    est = r["estado"]
    # 10/10/2026 (Rubén): filas ocultas por admin (no se borran en FT_NEWS; se dejan de ver acá)
    conn = _get_conn_production()
    try:
        oc = de.ocultas(conn, "news")
    finally:
        conn.close()
    if oc and isinstance(est.get("historial"), list):
        est["historial"] = [p for p in est["historial"] if p.get("id") not in oc]
        apuestas = [p for p in est["historial"] if p.get("tipo") in ("FUEGO", "FUEGO_EXPERIMENTAL")]
        k = de.kpis(list(reversed(apuestas)), vu)
        k["alertas_dichas"] = len(est["historial"])
        k["seguimiento"] = len(est["historial"]) - len(apuestas)
        est["kpis"] = k
    return est


@router.get("/api/news/config")
def api_news_config(request: Request, token: str = ""):
    _check_token_o_sesion(request, token, ["news"])
    from production.api.auth import requiere_admin
    requiere_admin(request)                       # 08/10/2026 noche: solo admin ve el panel completo
    return _ft_news("/news/config")


class NewsConfigIn(BaseModel):
    clave: str
    valor: str


@router.post("/api/news/config")
def api_news_config_post(request: Request, body: NewsConfigIn, token: str = ""):
    """Interruptores de FT_NEWS desde la pestaña (solo admin: ADMINISTRAR por la regla de escrituras)."""
    _check_token_o_sesion(request, token, ["news"])
    from production.api.auth import requiere_admin
    u = requiere_admin(request)
    return _ft_news("/news/config", "POST", {"clave": body.clave, "valor": body.valor, "por": u.get("usuario") or "admin"})


# v48 (13/10/2026, Rubén: "no estoy recibiendo alertas y no sabemos si FT NEWS está
# funcionando"): semáforo, embudo de descartes y prueba REAL de Discord. Todo solo admin;
# FullTennis solo reenvía a FT_NEWS (v7.2), no calcula nada ni toca otros motores.
@router.get("/api/news/embudo")
def api_news_embudo(request: Request, token: str = "", dias: int = Query(7, ge=1, le=60)):
    """Semáforo FUNCIONANDO / SIN_DATOS / ERROR + las seis preguntas por día."""
    _check_token_o_sesion(request, token, ["news"])
    from production.api.auth import requiere_admin
    requiere_admin(request)
    return _ft_news(f"/news/embudo?dias={int(dias)}")


@router.post("/api/news/prueba")
def api_news_prueba(request: Request, token: str = ""):
    """Envía una alerta marcada PRUEBA por el recorrido real FT NEWS → FT Intelligence → FT Discord.
    No cuenta en historial ni estadísticas."""
    _check_token_o_sesion(request, token, ["news"])
    from production.api.auth import requiere_admin
    u = requiere_admin(request)
    return _ft_news("/news/prueba", "POST", {"por": u.get("usuario") or "admin"})


@router.get("/api/news/prueba/{pid}")
def api_news_prueba_estado(request: Request, pid: int, token: str = ""):
    _check_token_o_sesion(request, token, ["news"])
    from production.api.auth import requiere_admin
    requiere_admin(request)
    return _ft_news(f"/news/prueba/{int(pid)}")


@router.get("/api/news/revision")
def api_news_revision(request: Request, token: str = ""):
    _check_token_o_sesion(request, token, ["news"])
    from production.api.auth import requiere_admin
    requiere_admin(request)                       # 08/10/2026 noche: solo admin ve el panel completo
    return _ft_news("/news/revision")


class NewsRevisionIn(BaseModel):
    accion: str
    categoria: str = ""


@router.post("/api/news/revision/{rid}")
def api_news_revision_post(request: Request, rid: int, body: NewsRevisionIn, token: str = ""):
    _check_token_o_sesion(request, token, ["news"])
    from production.api.auth import requiere_admin
    u = requiere_admin(request)
    return _ft_news(f"/news/revision/{rid}", "POST", {"accion": body.accion, "categoria": body.categoria,
                                                      "por": u.get("usuario") or "admin"})


class NewsInteresanteIn(BaseModel):
    valor: str


@router.post("/api/news/evaluaciones/{eid}/interesante")
def api_news_interesante(request: Request, eid: int, body: NewsInteresanteIn, token: str = ""):
    """La métrica que vale (diseño §8): Rubén marca cada 🔥 como SI / NO / MERCADO_LO_SABIA."""
    _check_token_o_sesion(request, token, ["news"])
    from production.api.auth import requiere_admin
    u = requiere_admin(request)
    return _ft_news(f"/news/evaluaciones/{eid}/interesante", "POST", {"valor": body.valor, "por": u.get("usuario") or "admin"})


@router.get("/api/news/alertas")
def api_news_alertas(request: Request, token: str = "", tipo: str = "", limite: int = 100):
    """v38 (09/10/2026): panel de alertas de FT NEWS v5 (📰 / 🔎 / 🔥 / 🔥 experimental):
    enviadas, pendientes, omitidas; motivo de selección, evidencia, resultado."""
    _check_token_o_sesion(request, token, ["news"])
    from production.api.auth import requiere_admin
    requiere_admin(request)
    return _ft_news(f"/news/alertas?limite={int(limite)}&tipo={tipo}")


@router.get("/api/news/diagnostico")
def api_news_diagnostico(request: Request, token: str = "", dias: int = 7):
    """v38: búsquedas, artículos y descartados con motivo, fuentes, externos vs internos, costos."""
    _check_token_o_sesion(request, token, ["news"])
    from production.api.auth import requiere_admin
    requiere_admin(request)
    return _ft_news(f"/news/diagnostico?dias={int(dias)}")


@router.get("/api/news/jugadores/buscar")
def api_news_jugadores_buscar(request: Request, token: str = "", q: str = ""):
    """v38: buscador de jugador en FT NEWS (todo su historial de noticias)."""
    _check_token_o_sesion(request, token, ["news"])
    from production.api.auth import requiere_admin
    requiere_admin(request)
    from urllib.parse import quote
    return _ft_news(f"/news/jugadores/buscar?q={quote(q[:80])}")


@router.get("/api/news/evaluaciones/{eid}")
def api_news_evaluacion_detalle(request: Request, eid: int, token: str = ""):
    """10/10/2026 (Rubén): modal de un partido 🔥/🔎 de FT NEWS — qué noticias
    encontró y todo lo que analizó. Solo lectura; lo arma FT_NEWS."""
    _check_token_o_sesion(request, token, ["news"])
    from production.api.auth import requiere_admin
    requiere_admin(request)
    return _ft_news(f"/news/evaluaciones/{int(eid)}")


@router.get("/api/news/jugadores/{jugador_id}/memoria")
def api_news_jugador_memoria(request: Request, jugador_id: int, token: str = ""):
    _check_token_o_sesion(request, token, ["news"])
    from production.api.auth import requiere_admin
    requiere_admin(request)
    return _ft_news(f"/news/jugadores/{int(jugador_id)}/memoria")


@router.get("/api/news/excel")
def api_news_excel(request: Request, token: str = ""):
    """HISTORICO + RESUMEN de FT NEWS (lo arma FT_NEWS; acá solo se reenvía el archivo)."""
    _check_token_o_sesion(request, token, ["news"])
    url = os.getenv("FT_NEWS_URL", "").strip().rstrip("/")
    secreto = os.getenv("FT_NEWS_SECRET", "").strip()
    if not url or not secreto:
        raise HTTPException(status_code=503, detail="FT_NEWS_URL / FT_NEWS_SECRET sin configurar")
    import httpx
    r = httpx.get(url + "/news/excel", headers={"X-FT-News-Secret": secreto}, timeout=60)
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"FT_NEWS respondió {r.status_code}")
    return Response(r.content, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": 'attachment; filename="ft_news_historico.xlsx"'})


@router.get("/api/news/fuentes")
def api_news_fuentes(request: Request, token: str = ""):
    _check_token_o_sesion(request, token, ["news"])
    from production.api.auth import requiere_admin
    requiere_admin(request)                       # 08/10/2026 noche: solo admin ve el panel completo
    return _ft_news("/news/fuentes")


class NewsFuenteIn(BaseModel):
    dominio: str
    autorizada: bool
    estado_tc: str
    nota: str = ""


@router.post("/api/news/fuentes/autorizar")
def api_news_fuentes_autorizar(request: Request, body: NewsFuenteIn, token: str = ""):
    _check_token_o_sesion(request, token, ["news"])
    from production.api.auth import requiere_admin
    u = requiere_admin(request)
    return _ft_news("/news/fuentes/autorizar", "POST", {"dominio": body.dominio, "autorizada": body.autorizada,
                                                        "estado_tc": body.estado_tc, "nota": body.nota,
                                                        "por": u.get("usuario") or "admin"})


# ── 📡 FT NEWS · canales de Telegram (18/10/2026, Rubén: «que se puedan configurar todos desde el Admin») ─────────────
@router.get("/api/news/telegram")
def api_news_telegram(request: Request, token: str = ""):
    """Admin: los canales elegidos (con lo que informa el reenviador), los canales que tiene la cuenta y las últimas pistas
    que leyó FT NEWS."""
    _check_token_o_sesion(request, token, ["news"])
    from production.api.auth import requiere_admin
    requiere_admin(request)
    from production.engines import news_telegram
    conn = _get_conn_production()
    try:
        canales, disp = news_telegram.listar(conn), news_telegram.disponibles(conn)
    finally:
        conn.close()
    p = _ft_news("/news/telegram/pistas?limite=80")
    if p.get("ok") is False and "404" in str(p.get("motivo") or ""):
        p["motivo"] = "FT_NEWS todavía no lee pistas de Telegram: hay que subir FT_NEWS v8.0."
    return {"canales": canales, "disponibles": disp, "pistas": p.get("estado") if p.get("ok") else None,
            "pistas_motivo": None if p.get("ok") else p.get("motivo")}


class NewsTelegramCanalIn(BaseModel):
    canal: str
    modo: str = "lesiones"
    activo: bool = True
    nombre: Optional[str] = None
    id: Optional[int] = None


@router.post("/api/news/telegram/canales")
def api_news_telegram_guardar(request: Request, body: NewsTelegramCanalIn, token: str = ""):
    _check_token_o_sesion(request, token, ["news"])
    from production.api.auth import requiere_admin
    requiere_admin(request)
    from production.engines import news_telegram
    conn = _get_conn_production()
    try:
        return {"ok": True, "canal": news_telegram.guardar(conn, body.canal, body.modo, body.activo, body.nombre, body.id)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        conn.close()


@router.post("/api/news/telegram/canales/{cid}/borrar")
def api_news_telegram_borrar(request: Request, cid: int, token: str = ""):
    _check_token_o_sesion(request, token, ["news"])
    from production.api.auth import requiere_admin
    requiere_admin(request)
    from production.engines import news_telegram
    conn = _get_conn_production()
    try:
        return {"ok": news_telegram.borrar(conn, cid)}
    finally:
        conn.close()


@router.get("/api/utr/padron")
def api_utr_padron(request: Request, token: str = ""):
    """Estado del UTR cargado (lo que antes mostraba UTR VALUE arriba)."""
    _check_token_o_sesion(request, token, ["utrpadron"])
    from production.engines.utr_padron import estado as estado_padron
    conn = _get_conn_production()
    try:
        return {"padron": estado_padron(conn)}
    finally:
        conn.close()


@router.get("/api/utr/ranking/{uid}/historial")
def api_utr_ranking_historial(request: Request, uid: int, token: str = ""):
    """Cambios de UTR que trajo Power para ese UTR ID (más nuevo primero)."""
    _check_token_o_sesion(request, token, ["utr"])
    conn = _get_conn_production()
    try:
        try:
            filas = [dict(r) for r in conn.execute(
                "SELECT utr_antes, utr_ahora, display_antes, display_ahora, en FROM utr_power_historial "
                "WHERE utr_player_id=? ORDER BY en DESC LIMIT 30", (uid,)).fetchall()]
        except sqlite3.OperationalError:
            filas = []
        return {"utr_player_id": uid, "historial": filas}
    finally:
        conn.close()


@router.get("/api/utr/diferencia")
def api_utr_diferencia(request: Request, j1: str, j2: str, genero: str = "M", token: str = ""):
    """ΔUTR de un partido (j1 − j2)."""
    _check_token_o_sesion(request, token, None)
    from production.engines.utr_diferencia import Resolvedor
    conn = _get_conn_production()
    try:
        return Resolvedor(conn).par(j1, j2, genero)
    finally:
        conn.close()


@router.get("/api/utr/diferencias")
def api_utr_diferencias(request: Request, p: str = "", token: str = ""):
    """ΔUTR de muchos partidos en un pedido: p = "J1|J2|M;J1|J2|F;..." (hasta
    80). El front parte las listas largas en tandas."""
    _check_token_o_sesion(request, token, None)
    from production.engines.utr_diferencia import Resolvedor
    pares = [x.split("|") for x in (p or "").split(";") if x.count("|") == 2][:80]
    conn = _get_conn_production()
    try:
        res = Resolvedor(conn)
        return {"pares": [res.par(a, b, g) for a, b, g in pares]}
    finally:
        conn.close()


# ═════════════════════════════════════════════════════════════════════
#  DASHBOARD ESTILO "HISTORIAL DE PICKS" (08/10/2026)
#  Partidos Maestros · Cuota Mal Puesta · UTR VALUE
#  Ver production/engines/dashboard_estrategia.py (mismas fórmulas que
#  Picks). Prefijo /api/dashboard/ a propósito: /api/cuota-mal-puesta/{id}
#  y /api/utr-value/{id} ya existen y se tragarían ".../dashboard".
# ═════════════════════════════════════════════════════════════════════

# ── v54 (13/10/2026, Rubén: «agregar la columna de investigación y su funcionamiento a Partidos Maestros, Cuota
#    Mal Puesta y UTR Value»): la investigación de la selección, como la del Recordador. ──
_INV_PERMISO = {"maestros": "partidosmaestros", "cmp": "cuotamalpuesta", "utrv": "utrvalue", "utra": "utrvalue",
                "mov": "movimientorentable"}   # v54f: 📉 Movimiento Rentable


@router.get("/api/estrategia/{clave}/{fila_id}/investigacion")
def api_estrategia_investigacion(request: Request, clave: str, fila_id: int, token: str = ""):
    """Todas las versiones de la investigación de una fila del historial (la vigente primero), en palabras. Si la
    fila todavía no tiene investigación (p. ej. una señal anterior a esta función), se hace en ese momento, sin IA, y
    queda guardada. Los datos crudos, solo admin."""
    if clave not in _INV_PERMISO:
        raise HTTPException(status_code=404, detail="Esa estrategia no tiene investigación.")
    _check_token_o_sesion(request, token, [_INV_PERMISO[clave]])
    if fila_id <= 0:
        raise HTTPException(status_code=404, detail="Las alertas del canal externo no se investigan.")
    es_admin = False
    try:
        from production.api.auth import usuario_actual
        u = usuario_actual(request)
        es_admin = bool(u and u.get("rol") == "admin")
    except Exception:
        pass
    from production.engines import estrategia_investigacion
    conn = _get_conn_production()
    try:
        d = estrategia_investigacion.detalle(conn, clave, fila_id, es_admin=es_admin)
    finally:
        conn.close()
    if d is None:
        raise HTTPException(status_code=404, detail="Esa fila no existe.")
    d["es_admin"] = es_admin
    return d


@router.get("/api/estrategia/{clave}/{fila_id}/contexto")
def api_estrategia_contexto_utr(request: Request, clave: str, fila_id: int, token: str = ""):
    """v54l (17/10/2026, Rubén): 🔎 FT INTELLIGENCE · CONTEXTO DEL PARTIDO (formato corto) de UTR VALUE / UTR MARKET
    ANOMALY. Rápido y solo lectura: NO dispara la investigación completa (esa sigue en /investigacion, para admin)."""
    if clave not in ("utrv", "utra"):
        raise HTTPException(status_code=404, detail="El contexto corto es del bot UTR (utrv / utra).")
    _check_token_o_sesion(request, token, [_INV_PERMISO[clave]])
    if fila_id <= 0:
        raise HTTPException(status_code=404, detail="Las alertas del canal externo no tienen contexto.")
    es_admin = False
    try:
        from production.api.auth import usuario_actual
        u = usuario_actual(request)
        es_admin = bool(u and u.get("rol") == "admin")
    except Exception:
        pass
    from production.engines import contexto_utr
    conn = _get_conn_production()
    try:
        ctx = contexto_utr.armar(conn, clave, fila_id)
    finally:
        conn.close()
    if ctx is None:
        raise HTTPException(status_code=404, detail="Esa fila no existe.")
    # 17/10/2026: la auditoría por dato (fuente, cobertura, confianza, evidencia) y lo PARCIAL solo para admin
    return {**(ctx if es_admin else contexto_utr.solo_publico(ctx)), "es_admin": es_admin}


@router.get("/api/estrategia/cmp/{fila_id}/ficha")
def api_estrategia_cmp_ficha(request: Request, fila_id: int, token: str = ""):
    """17/10/2026 (Daniel/Rubén): 🚨 FICHA de Cuota Mal Puesta para el modal nuevo: los números CONGELADOS de la señal
    (FT final, mercado sin margen, ventaja, cuotas) + oposición, carga, salud y el contexto de FT Intelligence.
    Rápido y solo lectura; la investigación completa sigue en /investigacion (admin)."""
    _check_token_o_sesion(request, token, [_INV_PERMISO["cmp"]])
    es_admin = False
    try:
        from production.api.auth import usuario_actual
        u = usuario_actual(request)
        es_admin = bool(u and u.get("rol") == "admin")
    except Exception:
        pass
    from production.engines import cmp_ficha
    conn = _get_conn_production()
    try:
        f = cmp_ficha.ficha(conn, fila_id, es_admin=es_admin)
    finally:
        conn.close()
    if f is None:
        raise HTTPException(status_code=404, detail="Esa señal no existe.")
    return f


@router.get("/api/dashboard/partidos-maestros")
def api_dashboard_maestros(request: Request, token: str = "", vista: str = "vigente", genero: str = "",
                           limite: int = Query(500, le=3000)):
    _check_token_o_sesion(request, token, ["partidosmaestros"])
    from production.engines import dashboard_estrategia as de
    conn = _get_conn_production()
    try:
        return de.partidos_maestros(conn, vista, genero, de.valor_unidad_de(conn, request), limite)
    finally:
        conn.close()


@router.get("/api/dashboard/cuota-mal-puesta")
def api_dashboard_cmp(request: Request, token: str = "", vista: str = "discord", genero: str = "",
                      limite: int = Query(500, le=3000)):
    _check_token_o_sesion(request, token, ["cuotamalpuesta"])
    from production.engines import dashboard_estrategia as de
    vista = "discord"      # 08/10/2026 (Rubén): solo se cuenta lo que llegó a Discord
    conn = _get_conn_production()
    try:
        return de.cuota_mal_puesta(conn, vista, genero, de.valor_unidad_de(conn, request), limite)
    finally:
        conn.close()


@router.get("/api/dashboard/utr-value")
def api_dashboard_utrv(request: Request, token: str = "", vista: str = "discord", genero: str = "",
                       limite: int = Query(500, le=3000)):
    _check_token_o_sesion(request, token, ["utrvalue"])
    from production.engines import dashboard_estrategia as de
    vista = "discord"      # 08/10/2026 (Rubén): solo se cuenta lo que llegó a Discord
    conn = _get_conn_production()
    try:
        return de.utr_value(conn, vista, genero, de.valor_unidad_de(conn, request), limite)
    finally:
        conn.close()


@router.get("/api/dashboard/ft-probabilidad")
def api_dashboard_ft_probabilidad(request: Request, token: str = "", genero: str = "",
                                  limite: int = Query(500, le=3000)):
    """🎲 FT PROBABILIDAD (30/10/2026): candidatos enviados a Discord, con su resultado. Historial propio de la pestaña."""
    _check_token_o_sesion(request, token, ["ftprobabilidad"])
    from production.engines import dashboard_estrategia as de
    conn = _get_conn_production()
    try:
        return de.ft_probabilidad(conn, "discord", genero, de.valor_unidad_de(conn, request), limite)
    finally:
        conn.close()


@router.get("/api/dashboard/recordador")
def api_dashboard_recordador(request: Request, token: str = "", genero: str = "", limite: int = Query(500, le=3000)):
    """🩹 Recordador IA (v43d): las predicciones dichas en Discord cuyo favorito llega a
    RECORDADOR_DASH_PROB_MIN (60 %). Historial propio de la pestaña; nunca se borra."""
    _check_token_o_sesion(request, token, ["recordador"])
    from production.engines import dashboard_estrategia as de
    try:
        from production.api.config_estrategias import obtener_float
        prob_min = obtener_float("RECORDADOR_DASH_PROB_MIN")
    except Exception:
        prob_min = 60.0
    conn = _get_conn_production()
    try:
        return de.recordador(conn, "discord", genero, de.valor_unidad_de(conn, request), limite, prob_min)
    finally:
        conn.close()


@router.get("/api/dashboard/implicacion")
def api_dashboard_implicacion(request: Request, token: str = "", vista: str = "", genero: str = "",
                              limite: int = Query(500, le=3000)):
    """🏆 Implicación de Torneo (v45): dashboard e historial de PICKS (solo lo que el motor eligió).
    Vista por defecto: validados si hay; si no, los candidatos experimentales con su aviso."""
    _check_token_o_sesion(request, token, ["implicacion"])
    from production.engines import dashboard_estrategia as de
    conn = _get_conn_production()
    try:
        return de.implicacion(conn, vista, genero, de.valor_unidad_de(conn, request), limite)
    finally:
        conn.close()


# ═════════════════════════════════════════════════════════════════════
#  🔥 UNDER UTR (v54i, 15/10/2026, Rubén: «construirla desde cero y empezar a generar un histórico limpio»)
#  Ver production/engines/under_utr.py. Todo de lectura; ocultar / cerrar a mano usan las rutas de Admin de siempre.
# ═════════════════════════════════════════════════════════════════════

def _underutr_filtros(genero: str, solo_discord: int, solo_confirmado: int, desde: str) -> dict:
    g = (genero or "").strip().upper()[:1]
    return {"genero": "F" if g == "W" else g, "solo_discord": bool(solo_discord),
            "solo_confirmado": bool(solo_confirmado), "desde": (desde or "").strip()[:10] or None}


@router.get("/api/dashboard/under-utr")
def api_dashboard_under_utr(request: Request, token: str = "", vista: str = "todas", genero: str = "",
                            limite: int = Query(500, le=3000)):
    """🔥 UNDER UTR: dashboard e historial de las activaciones (todas por defecto; 📣 las que salieron a Discord)."""
    _check_token_o_sesion(request, token, ["underutr"])
    from production.engines import dashboard_estrategia as de
    conn = _get_conn_production()
    try:
        return de.under_utr(conn, vista, genero, de.valor_unidad_de(conn, request), limite)
    finally:
        conn.close()


@router.get("/api/under-utr/grupos")
def api_under_utr_grupos(request: Request, token: str = "", dimension: str = "linea", genero: str = "",
                         solo_discord: int = 0, solo_confirmado: int = 0, desde: str = ""):
    """Rendimiento separado por una dimensión (género, línea, cuota, ΔUTR, modalidad, circuito, fase, superficie…)."""
    _check_token_o_sesion(request, token, ["underutr"])
    from production.engines import under_utr as uu
    conn = _get_conn_production()
    try:
        return uu.por_grupo(conn, dimension, _underutr_filtros(genero, solo_discord, solo_confirmado, desde))
    finally:
        conn.close()


@router.get("/api/under-utr/sin-senal")
def api_under_utr_sin_senal(request: Request, token: str = "", dias: int = Query(30, ge=1, le=365)):
    """Los partidos con ΔUTR ≥ mínimo que NO dieron señal antes del primer punto, por motivo."""
    _check_token_o_sesion(request, token, ["underutr"])
    from production.engines import under_utr as uu
    conn = _get_conn_production()
    try:
        return uu.sin_senal(conn, dias)
    finally:
        conn.close()


@router.get("/api/under-utr/vigilando")
def api_under_utr_vigilando(request: Request, token: str = "", horas: int = Query(24, ge=1, le=72)):
    """Los partidos POR JUGARSE con ΔUTR ≥ mínimo, pendientes de que abra una línea que cumpla."""
    _check_token_o_sesion(request, token, ["underutr"])
    from production.engines import under_utr as uu
    conn = _get_conn_production()
    try:
        cfg = uu.leer_config()
        return {"vigilando": uu.vigilando(conn, horas), "regla": {k: cfg[k] for k in (
            "UNDERUTR_DELTA_MIN", "UNDERUTR_LINEA_MIN", "UNDERUTR_CUOTA_MIN")},
            "formato_confirmado": bool(cfg["UNDERUTR_FORMATO_CONFIRMADO"]), "activo": bool(cfg["UNDERUTR_ACTIVO"])}
    finally:
        conn.close()


@router.get("/api/movimiento-rentable/control")
def api_movimiento_rentable_control(request: Request, token: str = "", horas: int = Query(24, ge=1, le=72)):
    """📋 v54j (16/10/2026, Rubén: «no sé si están funcionando o leyendo o qué están haciendo»): si llegan cuotas de
    Pinnacle, qué partidos se evaluaron y por qué no dieron señal, y qué se salta antes de evaluar. Solo admin."""
    _check_token_o_sesion(request, token, ["movimientorentable"])
    if not _es_admin(request):
        raise HTTPException(status_code=403, detail="Solo administradores.")
    from production.engines import mov_control
    conn = _get_conn_production()
    try:
        return mov_control.control(conn, horas)
    finally:
        conn.close()


@router.get("/api/under-utr/control")
def api_under_utr_control(request: Request, token: str = "", horas: int = Query(24, ge=1, le=72),
                          solo_con_mercado: bool = False):
    """📋 Control (16/10/2026): TODOS los partidos vistos, pasen o no la regla, con la línea y la cuota leídas."""
    _check_token_o_sesion(request, token, ["underutr"])
    from production.engines import under_utr as uu
    conn = _get_conn_production()
    try:
        return uu.control(conn, horas, solo_con_mercado)
    finally:
        conn.close()


@router.get("/api/under-utr/fuentes")
def api_under_utr_fuentes(request: Request, token: str = "", horas: int = Query(24, ge=1, le=168)):
    """🆚 18/10/2026 (Rubén): partidos de Fonbet (OddsPapi) frente a los de SportsAPI365, por circuito, y qué dijo el
    motor de los que faltaban. Solo lectura."""
    _check_token_o_sesion(request, token, ["underutr"])
    from production.engines import fonbet_descubrir
    conn = _get_conn_production()
    try:
        return fonbet_descubrir.comparacion(conn, horas)
    finally:
        conn.close()


@router.get("/api/horarios/cambios")
def api_horarios_cambios(request: Request, token: str = "", horas: int = Query(48, ge=1, le=168),
                         solo_senales: int = Query(0, ge=0, le=1)):
    """🕒 19/10/2026 (Rubén): partidos que cambiaron de hora y qué señales se actualizaron con la hora nueva. Solo lectura."""
    _check_token_o_sesion(request, token, ["underutr"])
    from production.engines import horarios
    conn = _get_conn_production()
    try:
        filas = horarios.cambios(conn, horas=horas, limite=2000, solo_senales=bool(solo_senales))
        partidos = [f for f in filas if f["estrategia"] == "partido"]
        return {"partidos_que_cambiaron": len(partidos),
                "senales_actualizadas": len(filas) - len(partidos),
                "por_tabla": {t: sum(1 for f in filas if f["estrategia"] == t)
                              for t in sorted({f["estrategia"] for f in filas} - {"partido"})},
                "cambios": filas[-300:]}
    finally:
        conn.close()


@router.get("/api/under-utr/cruce-canal")
def api_under_utr_cruce_canal(request: Request, token: str = "", dias: int = Query(3, ge=1, le=14)):
    """🔍 18/10/2026 (Rubén: «si ese sonó y no lo detectó nuestro sistema es porque algo está fallando»): cada Under del
    canal 🐢UTR frente a lo que vio el 🔥 UNDER UTR, con el paso en que se quedó. Solo lectura."""
    _check_token_o_sesion(request, token, ["underutr"])
    from production.engines import under_utr_cruce
    conn = _get_conn_production()
    try:
        return under_utr_cruce.cruzar(conn, dias)
    finally:
        conn.close()


@router.get("/api/under-utr/diagnostico")
def api_under_utr_diagnostico(request: Request, token: str = ""):
    """Solo admin: muestras crudas del mercado «Over Under» y por qué se ignoran partidos (en memoria)."""
    _check_token_o_sesion(request, token, ["underutr"])
    if not _es_admin(request):
        raise HTTPException(status_code=403, detail="Solo administradores.")
    from production.engines import under_utr as uu
    conn = _get_conn_production()
    try:
        return uu.diagnostico(conn)
    finally:
        conn.close()


@router.get("/api/under-utr/exportar.csv")
def api_under_utr_exportar(request: Request, token: str = "", genero: str = "", solo_discord: int = 0,
                           solo_confirmado: int = 0, desde: str = ""):
    """Solo admin: el histórico completo en CSV (para estudiarlo en Excel)."""
    _check_token_o_sesion(request, token, ["underutr"])
    if not _es_admin(request):
        raise HTTPException(status_code=403, detail="Solo administradores.")
    import io
    from production.engines import under_utr as uu
    conn = _get_conn_production()
    try:
        filas = uu.historial(conn, _underutr_filtros(genero, solo_discord, solo_confirmado, desde), limite=100000)
    finally:
        conn.close()
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(uu.CSV_COLS)
    for f in filas:
        w.writerow(["" if f.get(c) is None else f.get(c) for c in uu.CSV_COLS])
    return Response(content="﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="under_utr_historico.csv"'})


@router.get("/api/dashboard/movimiento-rentable")
def api_dashboard_movimiento(request: Request, token: str = "", vista: str = "enviadas", genero: str = "",
                             limite: int = Query(500, le=3000)):
    """📉 Movimiento Rentable (v54f): dashboard e historial de lo que salió a Telegram (valor_historial,
    enviado_picks=1), a la cuota de ENTRADA. Nunca se borra; 🗑 solo oculta (admin)."""
    _check_token_o_sesion(request, token, ["movimientorentable"])
    from production.engines import dashboard_estrategia as de
    if vista not in de.MOV_VISTAS:
        vista = "enviadas"
    conn = _get_conn_production()
    try:
        return de.movimiento_rentable(conn, vista, genero, de.valor_unidad_de(conn, request), limite)
    finally:
        conn.close()


@router.get("/api/dashboard/under-vivo")
def api_dashboard_under_vivo(request: Request, token: str = "", vista: str = "enviadas", genero: str = ""):
    """▼ Under en Vivo (08/10/2026): dashboard e historial propios, desde under_vivo_registro."""
    _check_token_o_sesion(request, token, ["undervivo"])
    from production.engines import dashboard_estrategia as de
    conn = _get_conn_production()
    try:
        return de.under_vivo(conn, vista=vista, genero=genero, valor_unidad=de.valor_unidad_de(conn, request))
    finally:
        conn.close()


@router.get("/api/dashboard/utr-anomalia")
def api_dashboard_utr_anomalia(request: Request, token: str = "", vista: str = "discord", genero: str = ""):
    """🚨 UTR Market Anomaly (08/10/2026): dashboard e historial propios."""
    _check_token_o_sesion(request, token, ["utrvalue"])
    from production.engines import dashboard_estrategia as de
    vista = "discord"      # 08/10/2026 (Rubén): solo se cuenta lo que llegó a Discord
    conn = _get_conn_production()
    try:
        return de.utr_anomalia(conn, vista=vista, genero=genero, valor_unidad=de.valor_unidad_de(conn, request))
    finally:
        conn.close()


@router.get("/api/dashboard/utr-externo")
def api_dashboard_utr_externo(request: Request, token: str = "", vista: str = "discord", genero: str = ""):
    """📡 UTR Externo (09/10/2026): las alertas del canal TIRS que sonaron en Discord."""
    # 13/10/2026 (Rubén): el canal externo ya no se muestra en la página → solo admin (o token interno).
    _imp_solo_admin(request, token)
    from production.engines import dashboard_estrategia as de
    conn = _get_conn_production()
    try:
        return de.utr_externo(conn, vista=vista if vista in ("discord", "todas") else "discord", genero=genero,
                              valor_unidad=de.valor_unidad_de(conn, request))
    finally:
        conn.close()


@router.get("/api/utr-externo")
def api_utr_externo(request: Request, token: str = "", limit: int = Query(300, le=2000), solo: str = "todo"):
    """📡 UTR Externo: todo lo recibido del canal (sonara o no), con texto y motivo."""
    # 13/10/2026 (Rubén): el canal externo ya no se muestra en la página → solo admin (o token interno).
    _imp_solo_admin(request, token)
    from production.engines import utr_externo
    conn = _get_conn_production()
    try:
        return {"registros": utr_externo.ultimas(conn, limit, solo_discord=(solo == "discord")),
                "estadisticas": utr_externo.estadisticas(conn),
                "actualizado": datetime.now(timezone.utc).isoformat()}
    finally:
        conn.close()


# ═════════════════════════════════════════════════════════════════════
#  ⏰ RECORDADOR (08/10/2026) — ver production/engines/recordador.py
# ═════════════════════════════════════════════════════════════════════

def _quien(request: Request) -> dict:
    from production.api.auth import usuario_actual
    u = usuario_actual(request) if request is not None else None
    if not u:
        raise HTTPException(status_code=403, detail="Iniciá sesión con tu usuario para usar el Recordador.")
    return u


@router.get("/api/recordador")
def api_recordador_lista(request: Request, token: str = ""):
    _check_token_o_sesion(request, token, ["recordador"])
    from production.engines import recordador
    conn = _get_conn_production()
    try:
        d = recordador.listar(conn)
        from production.api.auth import usuario_actual
        u = usuario_actual(request) or {}
        d["yo"] = u.get("usuario")
        d["es_admin"] = u.get("rol") == "admin"
        # Quién pidió cada jugador: los nombres de usuario solo los ve un
        # admin; el resto ve si lo pidió él y cuántos lo pidieron.
        for r in d["recordatorios"]:
            r["n_pedidos"] = len(r["pedidos"])
            if not d["es_admin"]:
                r["pedidos"] = [p for p in r["pedidos"] if p.get("usuario") == d["yo"]]
        return d
    finally:
        conn.close()


@router.post("/api/recordador")
def api_recordador_agregar(request: Request, datos: dict):
    _check_token_o_sesion(request, "", ["recordador"])
    u = _quien(request)
    from production.engines import recordador
    conn = _get_conn_production()
    try:
        try:
            return recordador.agregar(conn, str(datos.get("nombre") or ""), str(datos.get("genero") or ""),
                                      u.get("usuario") or "?", nota=str(datos.get("nota") or ""),
                                      clase=str(datos.get("clase") or "PROXIMO"))
        except recordador.ErrorRecordador as e:
            raise HTTPException(status_code=e.codigo, detail=str(e))
    finally:
        conn.close()


@router.get("/api/recordador/sugerir")
def api_recordador_sugerir(request: Request, q: str = "", genero: str = "", token: str = ""):
    """Autocompletado del nombre (players + padrón UTR), por sexo."""
    _check_token_o_sesion(request, token, ["recordador"])
    from production.engines import recordador
    conn = _get_conn_production()
    try:
        return {"jugadores": recordador.sugerir(conn, q, genero)}
    finally:
        conn.close()


@router.post("/api/recordador/{rid}/nota")
def api_recordador_nota(request: Request, rid: int, datos: dict):
    _check_token_o_sesion(request, "", ["recordador"])
    u = _quien(request)
    from production.engines import recordador
    conn = _get_conn_production()
    try:
        try:
            return recordador.guardar_nota(conn, rid, str(datos.get("nota") or ""), u.get("usuario") or "?",
                                           u.get("rol") == "admin")
        except recordador.ErrorRecordador as e:
            raise HTTPException(status_code=e.codigo, detail=str(e))
    finally:
        conn.close()


@router.post("/api/recordador/{rid}/clase")
def api_recordador_clase(request: Request, rid: int, datos: dict):
    """08/10/2026 noche (Rubén): Próximo partido / Lista blanca / Lista negra."""
    _check_token_o_sesion(request, "", ["recordador"])
    u = _quien(request)
    from production.engines import recordador
    conn = _get_conn_production()
    try:
        try:
            return recordador.cambiar_clase(conn, rid, str(datos.get("clase") or ""), u.get("usuario") or "?",
                                            u.get("rol") == "admin")
        except recordador.ErrorRecordador as e:
            raise HTTPException(status_code=e.codigo, detail=str(e))
    finally:
        conn.close()


# ── v43 (11/10/2026, Rubén): investigación, casos cerrados, precisión e histórico físico ──
@router.get("/api/recordador/{rid}/investigacion")
def api_recordador_investigacion(request: Request, rid: int, token: str = ""):
    """Todas las versiones de la investigación del recordatorio (la vigente primero),
    con sus avisos y resultado. La lectura simple arriba; datos_json para el admin."""
    _check_token_o_sesion(request, token, ["recordador"])
    import json as _json
    from production.engines import recordador
    conn = _get_conn_production()
    try:
        recordador.ensure_tablas(conn)
        recordador._asegurar_nota(conn)
        rec = recordador.obtener(conn, rid)
        if rec is None:
            raise HTTPException(status_code=404, detail="Ese recordatorio no existe.")
        es_admin = False
        try:
            from production.api.auth import usuario_actual
            u = usuario_actual(request)
            es_admin = bool(u and u.get("rol") == "admin")
        except Exception:
            pass
        versiones = []
        from production.engines import recordador_ia
        for r in conn.execute("SELECT i.*, res.ganador, res.gano_jugador, res.marcador, res.retiro, res.w_o, res.brier_base, res.logloss_base, "
                              "res.fuente AS resultado_fuente, res.cerrado_en FROM recordador_investigaciones i "
                              "LEFT JOIN recordador_resultados res ON res.investigacion_id=i.id WHERE i.recordatorio_id=? "
                              "ORDER BY i.vigente DESC, i.id DESC LIMIT 30", (rid,)).fetchall():
            d = dict(r)
            d["observacion"] = _json.loads(d.pop("observacion_json") or "null")
            datos = d.pop("datos_json", None)
            try:
                datos_d = _json.loads(datos) if datos else None
            except ValueError:
                datos_d = None
            # v43c: lectura simple para TODOS (en palabras); los datos crudos, solo admin
            try:
                d["lectura"] = recordador_ia.lectura_simple(d, datos_d)
                d["ficha"] = recordador_ia.ficha_jugadores(d, datos_d)
            except Exception:
                d["lectura"], d["ficha"] = [], None
            d["datos"] = datos_d if es_admin else None
            versiones.append(d)
        avisos = [dict(a) for a in conn.execute("SELECT id, partido_clave, avisado_en, momento, tipo, prediccion_id, discord, dicho_discord, "
                                                "dicho_en, dicho_motivo, telegram, push, texto_discord, partido_json FROM recordador_avisos WHERE recordatorio_id=? "
                                                "ORDER BY id DESC LIMIT 20", (rid,)).fetchall()]
        for a_ in avisos:                                  # 26/10/2026: inicio según la fuente y demora del aviso
            try:
                pj = _json.loads(a_.pop("partido_json") or "{}")
            except ValueError:
                pj = {}
            a_.update(fuente=pj.get("fuente"), inicio_real=pj.get("inicio_real"), demora_min=pj.get("demora_min"))
        return {"recordatorio": rec, "versiones": versiones, "avisos": avisos, "es_admin": es_admin}
    finally:
        conn.close()


@router.get("/api/recordador/demoras")
def api_recordador_demoras(request: Request, token: str = "", dias: int = Query(14, ge=1, le=90)):
    """26/10/2026: demora del 🔴 aviso de inicio real, por fuente, y estado del vigilante de OddsPapi."""
    _check_token_o_sesion(request, token, ["recordador"])
    from production.engines import recordador
    conn = _get_conn_production()
    try:
        return recordador.demoras(conn, dias)
    finally:
        conn.close()


@router.get("/api/recordador/cerrados")
def api_recordador_cerrados(request: Request, token: str = "", limite: int = Query(200, le=1000)):
    """Casos cerrados (quitados del seguimiento) con investigación y resultado: para aprender."""
    _check_token_o_sesion(request, token, ["recordador"])
    from production.engines import recordador
    conn = _get_conn_production()
    try:
        return {"cerrados": recordador.listar_cerrados(conn, limite)}
    finally:
        conn.close()


@router.get("/api/recordador/precision")
def api_recordador_precision(request: Request, token: str = "", desde: str = "", hasta: str = ""):
    """Brier, log loss, acierto del favorito y calibración de la PROBABILIDAD
    FULLTENNIS BASE (y la ajustada cuando exista). Retiros aparte; W.O. fuera."""
    _check_token_o_sesion(request, token, ["recordador"])
    from production.engines import recordador, recordador_ia
    conn = _get_conn_production()
    try:
        recordador.ensure_tablas(conn)
        return recordador_ia.precision(conn, desde or None, hasta or None)
    finally:
        conn.close()


@router.get("/api/recordador/historico-fisico")
def api_recordador_historico(request: Request, token: str = "", recalcular: int = 0):
    """Cruces MTO → siguiente partido y retiro → siguiente partido (sin observación
    humana; aparte de los casos del Recordador). recalcular=1 (admin) actualiza."""
    _check_token_o_sesion(request, token, ["recordador"])
    from production.engines import recordador_historico as rh
    conn = _get_conn_production()
    try:
        hechos = None
        if recalcular:
            if not _es_admin(request):
                raise HTTPException(status_code=403, detail="Solo un administrador puede recalcular el histórico.")
            hechos = rh.recalcular(conn)
        out = rh.resumen(conn)
        out["recalculado"] = hechos
        return out
    finally:
        conn.close()


@router.post("/api/recordador/prueba-discord")
def api_recordador_prueba_discord(request: Request):
    """v43b (11/10/2026, Rubén: "prueba real de los dos avisos"): publica los dos
    avisos de prueba (🩹 5 min antes + 🔴 inicio real) por el camino real hasta
    Discord. Solo admin. No toca recordatorios reales ni la precisión."""
    _check_token_o_sesion(request, "", ["recordador"])
    if not _es_admin(request):
        raise HTTPException(status_code=403, detail="Solo un administrador puede mandar la prueba a Discord.")
    u = _quien(request)
    from production.engines import recordador_ia
    conn = _get_conn_production()
    try:
        return recordador_ia.prueba_discord(conn, u.get("usuario") or "admin")
    finally:
        conn.close()


@router.delete("/api/recordador/{rid}")
def api_recordador_quitar(request: Request, rid: int):
    _check_token_o_sesion(request, "", ["recordador"])
    u = _quien(request)
    from production.engines import recordador
    conn = _get_conn_production()
    try:
        try:
            return recordador.quitar(conn, rid, u.get("usuario") or "?", u.get("rol") == "admin")
        except recordador.ErrorRecordador as e:
            raise HTTPException(status_code=e.codigo, detail=str(e))
    finally:
        conn.close()


# ═════════════════════════════════════════════════════════════════════
#  🏆 IMPLICACIÓN DE TORNEO — Fase 0 (12/10/2026). Ver production/engines/implicacion/.
#  v45: la pestaña ve solo el dashboard de picks; la investigación y las acciones, solo admin.
# ═════════════════════════════════════════════════════════════════════
_IMP_TRABAJOS: dict = {}


def _imp_conn():
    from production.engines.implicacion.esquema import ensure_una_vez
    conn = _get_conn_production()
    ensure_una_vez(conn)
    return conn


def _imp_solo_admin(request, token: str = ""):
    """v45 (Rubén: "mueve los diagnósticos ... a Administración, únicamente para administradores"):
    la investigación (estado, partidos investigados, estudio, ranking) ya no es de la pestaña,
    es de Admin. El token interno de research sigue sirviendo (scripts)."""
    if token and secrets.compare_digest(token, RESEARCH_TOKEN):
        return
    if not _es_admin(request):
        raise HTTPException(status_code=403, detail="Solo un administrador.")


_IMP_SWITCHES = ("IMPLICACION_ACTIVO", "IMPLICACION_RANKING_ACTIVO", "DEFENSA_SNAPSHOT_ACTIVO")


def _imp_interruptores() -> dict:
    try:
        from production.api.config_estrategias import obtener_bool, obtener_int
        out = {k: obtener_bool(k) for k in _IMP_SWITCHES}
        out["IMPLICACION_CUOTA_VENTANA_SEG"] = obtener_int("IMPLICACION_CUOTA_VENTANA_SEG")
        out["IMPLICACION_VUELTA_SEG"] = obtener_int("IMPLICACION_VUELTA_SEG")
        return out
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:120]}


@router.get("/api/implicacion/control")
def api_implicacion_control(request: Request, token: str = ""):
    """📋 v54j (16/10/2026): TODOS los partidos del registro (24 h atrás, 36 h adelante), con qué datos tiene cada uno,
    el set 1, la remontada y si fue pick o por qué no. Solo admin (como el resto de la investigación)."""
    _imp_solo_admin(request, token)
    from production.engines.implicacion import control
    conn = _imp_conn()
    try:
        return control.control(conn)
    finally:
        conn.close()


@router.get("/api/implicacion/estado")
def api_implicacion_estado(request: Request, token: str = ""):
    """Qué está operativo: reglas, ranking oficial, reconstrucción, defensa y registro."""
    _imp_solo_admin(request, token)
    from production.engines.implicacion import ranking, reconstruccion, registro, reglas
    conn = _imp_conn()
    try:
        defensa = {}
        try:
            defensa["resultados_temporada_anterior"] = conn.execute("SELECT COUNT(*) FROM player_ranking_results").fetchone()[0]
            defensa["snapshots"] = [dict(zip(("season", "semana", "estado", "filas", "fin"), r)) for r in conn.execute(
                "SELECT season, ranking_week, status, row_count, completed_at FROM ranking_defense_runs ORDER BY season DESC, ranking_week DESC LIMIT 6").fetchall()]
        except sqlite3.OperationalError:
            defensa["error"] = "sin tablas de defensa"
        return {"reglas": reglas.resumen(conn), "ranking": ranking.estado(conn), "reconstruccion": reconstruccion.resumen(conn),
                "defensa": defensa, "registro": registro.estado(conn), "trabajos": _IMP_TRABAJOS,
                "interruptores": _imp_interruptores(), "subida": _imp_subida(conn),
                "api_configurada": bool(os.getenv("SPORTSAPI365_KEY") or os.getenv("RAPIDAPI_KEY"))}
    finally:
        conn.close()


@router.get("/api/implicacion/partidos")
def api_implicacion_partidos(request: Request, token: str = "", vista: str = "recientes", circuito: str = "",
                             limite: int = Query(50, le=500), offset: int = 0):
    _imp_solo_admin(request, token)
    from production.engines.implicacion import registro
    conn = _imp_conn()
    try:
        return {"partidos": registro.listar(conn, vista, limite, offset, circuito)}
    finally:
        conn.close()


@router.get("/api/implicacion/estudio")
def api_implicacion_estudio(request: Request, token: str = "", desde: str = "", hasta: str = ""):
    """v44d: estudio de remontadas tras perder el set 1 (favorito PRE, implicación provisional,
    cuota PRE, UTR). VIGILANCIA, no señal. desde/hasta para comparar periodos (fuera de muestra)."""
    _imp_solo_admin(request, token)
    from production.engines.implicacion import pre
    conn = _imp_conn()
    try:
        return pre.estudio(conn, desde or None, hasta or None)
    finally:
        conn.close()


@router.get("/api/implicacion/ranking")
def api_implicacion_ranking(request: Request, token: str = "", tour: str = "ATP", tipo: str = "singles", fecha: str = "",
                            limite: int = Query(200, le=2000)):
    """Ranking oficial guardado (el último, o el de una fecha) y, si existe, el reconstruido de la misma fecha."""
    _imp_solo_admin(request, token)
    from production.engines.implicacion import ranking
    conn = _imp_conn()
    try:
        f = fecha or ranking.ultima_fecha(conn, tour.upper(), tipo) or ""
        filas = [dict(zip(("posicion", "puntos", "nombre", "pais", "player_id", "fuente"), r)) for r in conn.execute(
            "SELECT posicion, puntos, nombre, pais, player_id, fuente FROM ranking_oficial_semanal WHERE tour=? AND tipo=? AND fecha_ranking=? "
            "ORDER BY posicion LIMIT ?", (tour.upper(), tipo, f, limite)).fetchall()]
        rec = {r[0]: (r[1], r[2]) for r in conn.execute(
            "SELECT player_id, puntos, posicion FROM ranking_reconstruido_semanal WHERE tour=? AND fecha_ranking=?", (tour.upper(), f)).fetchall()}
        for x in filas:
            if x["player_id"] in rec:
                x["puntos_reconstruidos"], x["posicion_reconstruida"] = rec[x["player_id"]]
        fechas = [r[0] for r in conn.execute("SELECT DISTINCT fecha_ranking FROM ranking_oficial_semanal WHERE tour=? AND tipo=? "
                                             "ORDER BY fecha_ranking DESC LIMIT 120", (tour.upper(), tipo)).fetchall()]
        return {"tour": tour.upper(), "tipo": tipo, "fecha": f, "filas": filas, "fechas": fechas}
    finally:
        conn.close()


@router.get("/api/implicacion/auditoria")
def api_implicacion_auditoria(request: Request, token: str = "", n: int = Query(20, ge=1, le=60), formato: str = "json"):
    """17/10/2026 (Rubén) · admin: 🧾 auditoría de N jugadores reales (ATP, WTA, Challenger; defienden / buscan subir)
    con cada dato y su fuente, fecha y estado (VERIFICADO / ESTIMADO / ❔ SIN CONFIRMAR). Solo lectura.
    formato=csv → planilla (';', UTF-8) para revisarla con las fuentes oficiales."""
    _imp_solo_admin(request, token)
    from production.engines.implicacion import auditoria
    conn = _imp_conn()
    try:
        aud = auditoria.auditar(conn, n=n)
    finally:
        conn.close()
    if formato == "csv":
        from fastapi.responses import Response
        return Response(content=auditoria.csv_bytes(aud), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="auditoria_implicacion_{aud["semana_desde"]}.csv"'})
    return aud


@router.post("/api/implicacion/recalcular")
def api_implicacion_recalcular(request: Request, datos: dict = None):
    """v55 (17/10/2026) · admin: ♻️ completa el ranking / la defensa de los partidos ya registrados sin ellos y recalcula
    el nivel de implicación de sus candidatos. aplicar=false (por defecto) = en seco: solo cuenta. No toca picks."""
    if not _es_admin(request):
        raise HTTPException(status_code=403, detail="Solo un administrador.")
    from production.engines.implicacion import recalculo
    aplicar = bool((datos or {}).get("aplicar"))
    dias = max(1, min(int((datos or {}).get("dias") or 30), 120))
    conn = _imp_conn()
    try:
        return recalculo.recalcular(conn, aplicar=aplicar, dias=dias)
    finally:
        conn.close()


@router.post("/api/implicacion/recalcular-v2")
def api_implicacion_recalcular_v2(request: Request, datos: dict = None):
    """v56 (17/10/2026) · admin: recalcula la implicación V2 (datos con estado + clasificación por consecuencias) de
    todos los candidatos de los últimos `dias`. aplicar=false = en seco. No toca picks ni resultados."""
    if not _es_admin(request):
        raise HTTPException(status_code=403, detail="Solo un administrador.")
    from production.engines.implicacion import recalculo
    aplicar = bool((datos or {}).get("aplicar"))
    dias = max(1, min(int((datos or {}).get("dias") or 30), 120))
    conn = _imp_conn()
    try:
        return recalculo.recalcular_v2(conn, aplicar=aplicar, dias=dias)
    finally:
        conn.close()


@router.get("/api/implicacion/cobertura")
def api_implicacion_cobertura(request: Request):
    """v57 (17/10/2026) · admin: qué % de los tenistas de la semana tiene cada dato OFICIAL/COMPROBADO y cuántos se pueden
    clasificar como hecho (implicación V2). Solo lectura."""
    if not _es_admin(request):
        raise HTTPException(status_code=403, detail="Solo un administrador.")
    from production.engines.implicacion import cobertura
    conn = _imp_conn()
    try:
        return cobertura.medir(conn)
    finally:
        conn.close()


@router.get("/api/implicacion/torneos-api/muestra")
def api_implicacion_torneos_api_muestra(request: Request, season_id: int, tour: str = "ATP"):
    """v60 (17/10/2026) · admin: la respuesta CRUDA del proveedor para un torneo (info con puntos por ronda y sus
    partidos con la ronda), para ver por qué la categoría o la ronda no se pudieron comprobar. 2 llamadas."""
    if not _es_admin(request):
        raise HTTPException(status_code=403, detail="Solo un administrador.")
    from production.engines.implicacion.ranking import BASE, get_json
    t = "wta" if str(tour).upper().startswith("W") else "atp"
    info, e1 = get_json(f"{BASE}/{t}/tournament/info/{int(season_id)}", {"include": "rating"})
    fx, e2 = get_json(f"{BASE}/{t}/fixtures/tournament/{int(season_id)}", {"include": "round", "pageSize": 60})
    return {"season_id": season_id, "tour": t.upper(), "info": info, "error_info": e1, "fixtures": fx, "error_fixtures": e2}


@router.post("/api/implicacion/torneos-api/sincronizar")
def api_implicacion_torneos_api(request: Request, datos: dict = None):
    """v57 (17/10/2026) · admin: trae ya del calendario del proveedor los torneos de esta semana y la siguiente con su
    categoría (tier + puntos por ronda cotejados con la tabla oficial). La tarea lo hace sola cada 6 h."""
    if not _es_admin(request):
        raise HTTPException(status_code=403, detail="Solo un administrador.")
    from production.engines.implicacion import torneos_api, torneos_semana
    conn = _imp_conn()
    try:
        r = torneos_api.sincronizar(conn, forzar=bool((datos or {}).get("forzar")))
        torneos_semana._CACHE.clear()
        r["torneos_semana"] = [dict(x) for x in conn.execute(
            "SELECT season_id, tour, nombre, tier, categoria, estado, nota FROM implicacion_torneos_api ORDER BY semana_lunes DESC, tour, nombre LIMIT 120")]
        return r
    finally:
        conn.close()


@router.get("/api/implicacion/vencimientos")
def api_implicacion_vencimientos(request: Request):
    """v61 (17/10/2026) · admin: cuántas fechas de vencimiento de resultados derivados se corrigieron (por el último
    partido del cuadro principal) y ejemplos antes → ahora."""
    if not _es_admin(request):
        raise HTTPException(status_code=403, detail="Solo un administrador.")
    from production.engines.implicacion import vencimientos
    conn = _imp_conn()
    try:
        return vencimientos.resumen(conn)
    finally:
        conn.close()


@router.post("/api/implicacion/vencimientos")
def api_implicacion_vencimientos_correr(request: Request, datos: dict = None):
    """v61 · admin: corre ya una tanda (por defecto 150 torneos; la tarea lo hace sola cada 30 min)."""
    if not _es_admin(request):
        raise HTTPException(status_code=403, detail="Solo un administrador.")
    from production.engines.implicacion import vencimientos
    conn = _imp_conn()
    try:
        return vencimientos.corregir(conn, limite=max(1, min(int((datos or {}).get("limite") or 150), 400)))
    finally:
        conn.close()


@router.get("/api/implicacion/resultados")
def api_implicacion_resultados(request: Request):
    """v62 (19/10/2026) · admin: cuántos torneos de las últimas 52 semanas ya tienen resultados derivados (por circuito y
    estado) y cuántos faltan."""
    if not _es_admin(request):
        raise HTTPException(status_code=403, detail="Solo un administrador.")
    from production.engines.implicacion import resultados_api
    conn = _imp_conn()
    try:
        return resultados_api.resumen(conn)
    finally:
        conn.close()


@router.post("/api/implicacion/resultados")
def api_implicacion_resultados_correr(request: Request, datos: dict = None):
    """v62 · admin: corre ya una tanda (por defecto 40 torneos; la tarea lo hace sola cada 15 min hasta completar)."""
    if not _es_admin(request):
        raise HTTPException(status_code=403, detail="Solo un administrador.")
    from production.engines.implicacion import resultados_api
    conn = _imp_conn()
    try:
        return resultados_api.actualizar(conn, limite=max(1, min(int((datos or {}).get("limite") or 40), 200)))
    finally:
        conn.close()


@router.get("/api/implicacion/validacion-semanal")
def api_implicacion_validacion_semanal(request: Request, tour: str = "", semana: str = "", grupo: str = "",
                                       solo_diferencias: bool = False, formato: str = ""):
    """v58 (17/10/2026) · admin: comparación semanal del cambio de puntos calculado contra el oficial. Sin `semana`:
    resumen de todas; con `tour` + `semana`: detalle por jugador (`formato=csv` para descargar)."""
    if not _es_admin(request):
        raise HTTPException(status_code=403, detail="Solo un administrador.")
    from production.engines.implicacion import validacion_semanal as vs
    conn = _imp_conn()
    try:
        if not (tour and semana):
            return vs.resumen(conn)
        filas = vs.detalle(conn, tour, semana, grupo, solo_diferencias, limite=5000 if formato == "csv" else 300)
    finally:
        conn.close()
    if formato == "csv":
        import csv as _csv
        import io as _io
        from fastapi.responses import Response
        buf = _io.StringIO()
        w = _csv.writer(buf, delimiter=";")
        if filas:
            w.writerow(list(filas[0]))
            for f in filas:
                w.writerow([f[k] for k in filas[0]])
        return Response(content=("\ufeff" + buf.getvalue()).encode("utf-8"), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="validacion_{tour}_{semana}.csv"'})
    return {"tour": tour.upper(), "semana": semana, "filas": filas}


@router.post("/api/implicacion/validacion-semanal")
def api_implicacion_validacion_semanal_correr(request: Request, datos: dict = None):
    """v58 · admin: corre ya la comparación (todas = true vuelve a calcular las semanas ya hechas)."""
    if not _es_admin(request):
        raise HTTPException(status_code=403, detail="Solo un administrador.")
    from production.engines.implicacion import validacion_semanal as vs
    conn = _imp_conn()
    try:
        return vs.validar(conn, solo_nuevas=not bool((datos or {}).get("todas")))
    finally:
        conn.close()


@router.post("/api/implicacion/ranking/capturar")
def api_implicacion_ranking_capturar(request: Request, datos: dict = None):
    """Admin: captura AHORA el ranking vigente (o el de `fecha`) de ATP y WTA. Prueba real contra la API."""
    if not _es_admin(request):
        raise HTTPException(status_code=403, detail="Solo un administrador.")
    from production.engines.implicacion import ranking
    tipo = str((datos or {}).get("tipo") or "singles")
    fecha = str((datos or {}).get("fecha") or "") or None
    conn = _imp_conn()
    try:
        res = []
        for t in ("ATP", "WTA"):
            ok, motivo = ranking.aprobada(conn, t)     # v44c: sin verificación OK no se guarda ranking
            res.append(ranking.capturar(conn, t, tipo, fecha) if ok else
                       {"tour": t, "tipo": tipo, "estado": "bloqueado", "problemas": [motivo]})
        return {"resultados": res}
    finally:
        conn.close()


def _imp_en_segundo_plano(nombre: str, fn) -> dict:
    import threading
    if (_IMP_TRABAJOS.get(nombre) or {}).get("estado") == "corriendo":
        raise HTTPException(status_code=409, detail=f"'{nombre}' ya está corriendo.")
    _IMP_TRABAJOS[nombre] = {"estado": "corriendo", "inicio": datetime.now(timezone.utc).isoformat(timespec="seconds")}

    def _run():
        conn = _imp_conn()
        try:
            _IMP_TRABAJOS[nombre].update(estado="terminado", resultado=fn(conn),
                                         fin=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        except Exception as e:  # noqa: BLE001
            _IMP_TRABAJOS[nombre].update(estado="error", error=str(e)[:300])
        finally:
            conn.close()
    threading.Thread(target=_run, daemon=True, name=f"implicacion-{nombre}").start()
    return {"ok": True, "trabajo": nombre, "nota": "corre en segundo plano; el estado se ve en la pestaña"}


@router.post("/api/implicacion/ranking/backfill")
def api_implicacion_ranking_backfill(request: Request, datos: dict = None):
    """Admin: ranking OFICIAL histórico (un lunes por semana) entre `desde` y `hasta`, para ATP y WTA."""
    if not _es_admin(request):
        raise HTTPException(status_code=403, detail="Solo un administrador.")
    from production.engines.implicacion import ranking
    desde = str((datos or {}).get("desde") or "2025-01-06")
    hasta = str((datos or {}).get("hasta") or datetime.now(timezone.utc).date().isoformat())
    top = int((datos or {}).get("top") or 0) or None
    return _imp_en_segundo_plano("backfill_ranking", lambda conn: {t: ranking.backfill(conn, t, desde, hasta, top=top) for t in ("ATP", "WTA")})


@router.post("/api/implicacion/reconstruir")
def api_implicacion_reconstruir(request: Request, datos: dict = None):
    """Admin: ranking RECONSTRUIDO (desde player_ranking_results) de los lunes entre `desde` y `hasta`,
    comparado contra el oficial de cada fecha cuando existe."""
    if not _es_admin(request):
        raise HTTPException(status_code=403, detail="Solo un administrador.")
    from datetime import date as _date, timedelta as _td
    from production.engines.implicacion import reconstruccion
    desde = _date.fromisoformat(str((datos or {}).get("desde") or "2025-01-06")[:10])
    hasta = _date.fromisoformat(str((datos or {}).get("hasta") or datetime.now(timezone.utc).date().isoformat())[:10])

    def _fn(conn):
        out, d = [], desde - _td(days=desde.weekday())
        while d <= hasta:
            for t in ("ATP", "WTA"):
                out.append(reconstruccion.reconstruir(conn, t, d.isoformat()))
            d += _td(days=7)
        return out[-10:]
    return _imp_en_segundo_plano("reconstruccion", _fn)


@router.post("/api/implicacion/ranking/verificar")
def api_implicacion_ranking_verificar(request: Request, datos: dict = None):
    """Admin: PRUEBA REAL en seco (no escribe) del ranking ATP y WTA contra la API: fecha,
    puntos, número de jugadores, contraste con un segundo endpoint y top 10 para cotejar."""
    if not _es_admin(request):
        raise HTTPException(status_code=403, detail="Solo un administrador.")
    from production.engines.implicacion import ranking
    from production.engines.implicacion.esquema import anotar
    conn = _imp_conn()
    try:
        por = _quien(request).get("usuario") or "admin"
        out = []
        for t in ("ATP", "WTA"):
            v = ranking.verificar(conn, t)
            # v44c: cada verificación queda asentada (bitácora permanente) y decide si se puede capturar
            anotar(conn, "ranking", v.get("veredicto"), t, por, v)
            v["captura_permitida"], v["captura_motivo"] = ranking.aprobada(conn, t)
            out.append(v)
        return {"verificaciones": out}
    finally:
        conn.close()


@router.post("/api/implicacion/interruptor")
def api_implicacion_interruptor(request: Request, datos: dict = None):
    """Admin: prende o apaga en caliente el módulo, el ranking o el snapshot de defensa
    (mismas claves que Admin → Estrategias; efecto en ≤ 30 s, sin redeploy)."""
    if not _es_admin(request):
        raise HTTPException(status_code=403, detail="Solo un administrador.")
    clave = str((datos or {}).get("clave") or "")
    if clave not in _IMP_SWITCHES:
        raise HTTPException(status_code=400, detail="Clave no válida.")
    from production.api.config_estrategias import set_valor
    set_valor(clave, "1" if (datos or {}).get("valor") else "0", por=_quien(request).get("usuario") or "admin")
    return {"ok": True, "interruptores": _imp_interruptores()}


# ── v45 · MOTOR DE PICKS: reglas, validación y prueba del circuito de Discord (solo admin) ─────

@router.get("/api/implicacion/picks/estado")
def api_implicacion_picks_estado(request: Request):
    """Admin: estado del motor de picks, reglas con sus métricas y criterios de validación."""
    if not _es_admin(request):
        raise HTTPException(status_code=403, detail="Solo un administrador.")
    from production.engines.implicacion import picks
    conn = _imp_conn()
    try:
        return picks.estado(conn)
    finally:
        conn.close()


@router.get("/api/implicacion/estadisticas")
def api_implicacion_estadisticas(request: Request, token: str = "", agrupar: str = "nivel", genero: str = "",
                                 desde: str = "", hasta: str = ""):
    """v46: estudio de TODOS los candidatos (favorito PRE que pierde el 1.er set) por nivel de implicación,
    cuota, UTR, Elo/FTR, MatchUp, circuito, superficie o torneo. Estudio, no señal. Lo ve quien ve la pestaña."""
    _check_token_o_sesion(request, token, ["implicacion"])
    from production.engines.implicacion import candidatos
    conn = _imp_conn()
    try:
        return candidatos.estadisticas(conn, agrupar, genero.upper()[:1] if genero else "", desde or None, hasta or None)
    finally:
        conn.close()


@router.get("/api/implicacion/seguimiento")
def api_implicacion_seguimiento(request: Request, token: str = "", vista: str = "en_juego", genero: str = ""):
    """v52 (13/10/2026, Rubén: «en lugar de Estadísticas, que me salga los partidos que está siguiendo»):
    🎾 partidos en seguimiento, en palabras sencillas. Solo lectura. Lo ve quien ve la pestaña."""
    _check_token_o_sesion(request, token, ["implicacion"])
    from production.engines.implicacion import seguimiento
    conn = _imp_conn()
    try:
        return seguimiento.listar(conn, vista, genero)
    finally:
        conn.close()


@router.get("/api/implicacion/torneos-semana")
def api_implicacion_torneos_semana(request: Request, token: str = "", genero: str = ""):
    """v55 (17/10/2026, Rubén): 🗓️ torneos de esta semana y la implicación de cada tenista (puntos que defiende, % de
    sus puntos, nivel con el MISMO motor de los picks) + cómo le fue (set 1, remontada, pick). Solo lectura.
    Lo ve quien ve la pestaña."""
    _check_token_o_sesion(request, token, ["implicacion"])
    from production.engines.implicacion import torneos_semana
    conn = _imp_conn()
    try:
        return torneos_semana.semana(conn, genero=genero.upper() if genero else "")
    finally:
        conn.close()


@router.get("/api/implicacion/candidatos")
def api_implicacion_candidatos(request: Request, token: str = "", limite: int = Query(50, le=500), nivel: str = ""):
    """v46 · admin: los últimos candidatos con TODO su análisis de implicación (para auditar los cálculos)."""
    _imp_solo_admin(request, token)
    from production.engines.implicacion import candidatos
    conn = _imp_conn()
    try:
        return {"candidatos": candidatos.listar(conn, limite, nivel.upper() if nivel else ""), "resumen": candidatos.resumen(conn)}
    finally:
        conn.close()


@router.get("/api/implicacion/diagnostico")
def api_implicacion_diagnostico(request: Request, token: str = ""):
    """v46b · admin: comprobación de la instalación (detecta, mide la implicación de los dos, cierra con cuota
    verificada, Discord apagado solo para Implicación) y los números para el informe."""
    _imp_solo_admin(request, token)
    from production.engines.implicacion import candidatos
    conn = _imp_conn()
    try:
        return candidatos.diagnostico(conn)
    finally:
        conn.close()


@router.post("/api/implicacion/candidatos/completar")
def api_implicacion_candidatos_completar(request: Request):
    """v46b · admin: registra YA los candidatos que el registro tenía (en segundo plano, por lotes)."""
    if not _es_admin(request):
        raise HTTPException(status_code=403, detail="Solo un administrador.")
    from production.engines.implicacion import candidatos
    return _imp_en_segundo_plano("candidatos_historia", lambda conn: candidatos.completar_historia(conn))


@router.post("/api/implicacion/reglas/nivel")
def api_implicacion_regla_nivel(request: Request, datos: dict = None):
    """Admin: pasa una regla a VALIDADA (solo si cumple los 5 criterios) o la vuelve a EXPERIMENTAL."""
    if not _es_admin(request):
        raise HTTPException(status_code=403, detail="Solo un administrador.")
    from production.engines.implicacion import picks
    conn = _imp_conn()
    try:
        return picks.cambiar_nivel(conn, str((datos or {}).get("regla") or ""), str((datos or {}).get("nivel") or ""),
                                   _quien(request).get("usuario") or "admin", str((datos or {}).get("nota") or "")[:300])
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    finally:
        conn.close()


@router.post("/api/implicacion/picks/prueba-discord")
def api_implicacion_prueba_discord(request: Request):
    """Admin: un pick de PRUEBA (🧪, no cuenta en nada) para probar el circuito
    RankingFTR → FT Intelligence → FT Discord. Sale aunque IMP_DISCORD_ACTIVO esté apagado."""
    if not _es_admin(request):
        raise HTTPException(status_code=403, detail="Solo un administrador.")
    from production.engines.implicacion import picks
    conn = _imp_conn()
    try:
        return picks.crear_prueba(conn, _quien(request).get("usuario") or "admin")
    finally:
        conn.close()


@router.get("/implicacion/admin", response_class=HTMLResponse)
def implicacion_admin_page(request: Request):
    """🏆 Implicación de Torneo · ADMINISTRACIÓN (v45): todo lo que antes estaba en la pestaña
    (subida controlada, verificación del ranking, validación, estudio, reglas de picks,
    interruptores y prueba de Discord). Solo admin. Se ve dentro de Admin (iframe del
    mismo origen: por eso esta página sola permite frame-ancestors 'self')."""
    if not _es_admin(request):
        raise HTTPException(status_code=403, detail="Solo un administrador.")
    from production.api.app import _CSP
    r = _templates.TemplateResponse(request=request, name="implicacion_admin.html", context={})
    r.headers["Content-Security-Policy"] = _CSP.replace("frame-ancestors 'none'", "frame-ancestors 'self'")
    r.headers["X-Frame-Options"] = "SAMEORIGIN"
    return r


@router.get("/news/admin", response_class=HTMLResponse)
def news_admin_page(request: Request):
    """📰 FT NEWS · ADMINISTRACIÓN DEL BOT (v53, 13/10/2026, Rubén: «Administración del bot no debe estar acá, debe
    salir en un apartado dentro de Admin»). Lo que estaba plegado al final de la pestaña 📰. Solo admin; se ve dentro
    de Admin → 📰 FT NEWS (iframe del mismo origen: por eso esta página sola permite frame-ancestors 'self')."""
    if not _es_admin(request):
        raise HTTPException(status_code=403, detail="Solo un administrador.")
    from production.api.app import _CSP
    r = _templates.TemplateResponse(request=request, name="news_admin.html", context={})
    r.headers["Content-Security-Policy"] = _CSP.replace("frame-ancestors 'none'", "frame-ancestors 'self'")
    r.headers["X-Frame-Options"] = "SAMEORIGIN"
    return r


# ── v48 · 🔎 FT INTELLIGENCE · ¿POR QUÉ LA CUOTA? — diagnóstico de SOLO LECTURA (Admin) ─────────

def _pqc_conn_ro() -> sqlite3.Connection:
    """La base de producción en modo SOLO LECTURA (mode=ro): el diagnóstico no puede escribir."""
    if not _PROD_DB_PATH.exists():
        raise HTTPException(status_code=503, detail=f"DB de producción no encontrada en {_PROD_DB_PATH}.")
    conn = sqlite3.connect(_PROD_DB_PATH.resolve().as_uri() + "?mode=ro", uri=True, timeout=20)
    conn.execute("PRAGMA busy_timeout=20000")
    conn.row_factory = sqlite3.Row
    return conn


@router.get("/api/por-que-cuota/buscar")
def api_pqc_buscar(request: Request, q: str = "", limite: int = Query(20, ge=1, le=100), token: str = ""):
    """Señales de 🚨 UTR Market Anomaly por nombre de jugador o torneo (para elegir cuál diagnosticar)."""
    _imp_solo_admin(request, token)
    from production.engines import por_que_cuota
    conn = _pqc_conn_ro()
    try:
        return {"senales": por_que_cuota.buscar_anomalias(conn, q, limite)}
    finally:
        conn.close()


@router.get("/api/por-que-cuota/diagnostico")
def api_pqc_diagnostico(request: Request, registro_id: Optional[int] = None, jugador_a: str = "", jugador_b: str = "",
                        superficie: str = "", torneo: str = "", fecha: str = "", genero: str = "", pais_torneo: str = "",
                        token: str = ""):
    """v48 (Rubén, 13/10/2026): COBERTURA → SUPERFICIE → INACTIVIDAD → CARGA → MTO/RETIROS → FT NEWS →
    INVESTIGACIÓN EXTERNA de los dos jugadores, con la fuente de cada dato. Una señal de UTR Market Anomaly
    (registro_id) o un partido cargado a mano. Solo lectura, 0 créditos (la búsqueda externa solo se planifica)."""
    _imp_solo_admin(request, token)
    from production.engines import por_que_cuota
    conn = _pqc_conn_ro()
    try:
        manual = None if registro_id else {"jugador_a": jugador_a.strip(), "jugador_b": jugador_b.strip(),
                                           "superficie": superficie.strip(), "torneo": torneo.strip() or None,
                                           "fecha": fecha.strip() or None, "genero": genero.strip() or None,
                                           "pais_torneo": pais_torneo.strip() or None}
        d = por_que_cuota.diagnostico(conn, registro_id=registro_id, manual=manual)
    finally:
        conn.close()
    if not d.get("ok"):
        raise HTTPException(status_code=400, detail=d.get("error") or "no se pudo diagnosticar")
    return d


@router.get("/api/por-que-cuota/senal/{registro_id}")
def api_pqc_senal(request: Request, registro_id: int, token: str = ""):
    """v54c: el estudio GUARDADO de una señal (datos internos al detectarla + búsqueda externa de FT Intelligence)."""
    _imp_solo_admin(request, token)
    from production.engines import pqc_contexto
    conn = _pqc_conn_ro()
    try:
        d = pqc_contexto.leer(conn, registro_id)
    finally:
        conn.close()
    if d is None:
        raise HTTPException(status_code=404, detail="Esa señal todavía no tiene estudio guardado.")
    d.pop("diagnostico_json", None)
    return d


@router.get("/api/por-que-cuota/estudio")
def api_pqc_estudio(request: Request, solo_discord: bool = True, token: str = ""):
    """v54c (Rubén, punto 15): SIN CONTEXTO vs SUPERFICIE vs SALUD vs INACTIVIDAD vs CARGA vs VIAJE… contra el
    resultado de las señales. Solo para estudiar: no cambia ninguna señal."""
    _imp_solo_admin(request, token)
    from production.engines import pqc_contexto
    conn = _get_conn_production()
    try:
        return pqc_contexto.estudio(conn, solo_discord=solo_discord)
    finally:
        conn.close()


@router.get("/por-que-cuota/admin", response_class=HTMLResponse)
def pqc_admin_page(request: Request):
    """🔎 ¿Por qué la cuota? · diagnóstico (v48). Solo admin; se ve dentro de Admin (iframe del mismo origen)."""
    if not _es_admin(request):
        raise HTTPException(status_code=403, detail="Solo un administrador.")
    from production.api.app import _CSP
    r = _templates.TemplateResponse(request=request, name="por_que_cuota_admin.html", context={})
    r.headers["Content-Security-Policy"] = _CSP.replace("frame-ancestors 'none'", "frame-ancestors 'self'")
    r.headers["X-Frame-Options"] = "SAMEORIGIN"
    return r


# ── v44c · SUBIDA CONTROLADA: respaldo → verificar → activar → validar ─────────

def _imp_subida(conn) -> dict:
    """Estado de los pasos de la subida controlada (para la pestaña)."""
    from production.engines import respaldo
    from production.engines.implicacion import ranking
    from production.engines.implicacion.esquema import ultima

    def _corta(v):
        if not v:
            return None
        d = v.get("detalle") if isinstance(v.get("detalle"), dict) else {}
        return {"en": v["en"], "veredicto": v["veredicto"], "por": v["por"], "fecha_ranking": d.get("fecha_ranking"),
                "filas": d.get("filas"), "chequeos": d.get("chequeos"), "top10": d.get("top10"), "problemas": d.get("problemas"),
                "coincidencia_top100_pct": d.get("coincidencia_top100_pct"), "cruzados_con_padron_pct": d.get("cruzados_con_padron_pct"),
                "cotejo_manual": d.get("cotejo_manual"), "nota": d.get("nota")}
    tours = {}
    for t in ("ATP", "WTA"):
        ok, motivo = ranking.aprobada(conn, t)
        tours[t] = {"aprobado": ok, "motivo": motivo, "verificacion": _corta(ultima(conn, "ranking", t)),
                    "aprobacion_manual": _corta(ultima(conn, "aprobacion_manual", t))}
    act, apag, val = ultima(conn, "activacion"), ultima(conn, "apagado"), ultima(conn, "validacion")
    try:
        resp = [r for r in respaldo.listar() if r["tipo"] == "respaldo"]
    except Exception as e:  # noqa: BLE001
        resp = [{"error": str(e)[:120]}]
    return {"tours": tours,
            "activacion": {"en": act["en"], "por": act["por"]} if act else None,
            "apagado": {"en": apag["en"], "por": apag["por"]} if apag else None,
            "activo": bool(act and (not apag or apag["id"] < act["id"])),
            "ultima_validacion": {"en": val["en"], "veredicto": val["veredicto"]} if val else None,
            "respaldos": resp[-6:], "respaldo_reciente": _imp_respaldo_reciente(resp)}


def _imp_respaldo_reciente(resp: list) -> Optional[dict]:
    lim = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    buenos = [r for r in resp if r.get("destino") == "volumen" and r.get("quick_check") == "ok" and (r.get("creado_en") or "") >= lim]
    return buenos[-1] if buenos else None


@router.post("/api/implicacion/ranking/aprobar")
def api_implicacion_ranking_aprobar(request: Request, datos: dict = None):
    """Admin: aprueba A MANO un tour cuya última verificación dio REVISAR (nunca FALLA), después
    de cotejar el top 10 con la web oficial. Exige una nota (qué se cotejó) y queda asentado."""
    if not _es_admin(request):
        raise HTTPException(status_code=403, detail="Solo un administrador.")
    from production.engines.implicacion.esquema import anotar, ultima
    tour = str((datos or {}).get("tour") or "").upper()
    nota = str((datos or {}).get("nota") or "").strip()
    if tour not in ("ATP", "WTA"):
        raise HTTPException(status_code=400, detail="tour = ATP o WTA.")
    if len(nota) < 10:
        raise HTTPException(status_code=400, detail="Escribí qué cotejaste (mínimo 10 caracteres).")
    conn = _imp_conn()
    try:
        v = ultima(conn, "ranking", tour)
        if not v:
            raise HTTPException(status_code=409, detail=f"{tour}: primero hay que correr «Verificar ranking».")
        if v["veredicto"] != "REVISAR":
            raise HTTPException(status_code=409, detail=f"{tour}: la última verificación dio {v['veredicto']}; "
                                                        "solo se aprueba a mano un REVISAR (un FALLA hay que corregirlo).")
        anotar(conn, "aprobacion_manual", "OK_MANUAL", tour, _quien(request).get("usuario") or "admin",
               {"nota": nota, "verificacion_id": v["id"], "chequeos": (v.get("detalle") or {}).get("chequeos")})
        return {"ok": True, "subida": _imp_subida(conn)}
    finally:
        conn.close()


@router.post("/api/implicacion/activar")
def api_implicacion_activar(request: Request, datos: dict = None):
    """Admin: «✅ Activar Fase 0». Prende juntos el módulo (registro, set 1, cuotas, cierre),
    la captura semanal del ranking y el snapshot de defensa. Exige ATP y WTA verificados y un
    respaldo reciente (o confirmar explícitamente que se sigue sin él). Toma una foto de
    Admin → Salud para comparar después que ningún otro bot empeoró. Sin alertas a Discord."""
    if not _es_admin(request):
        raise HTTPException(status_code=403, detail="Solo un administrador.")
    from production.api.config_estrategias import set_valor
    from production.engines.implicacion import ranking, validacion
    from production.engines.implicacion.esquema import anotar
    conn = _imp_conn()
    try:
        faltan = [m for ok, m in (ranking.aprobada(conn, t) for t in ("ATP", "WTA")) if not ok]
        if faltan:
            raise HTTPException(status_code=409, detail="No se activa: " + " · ".join(faltan))
        if not (os.getenv("SPORTSAPI365_KEY") or os.getenv("RAPIDAPI_KEY")):
            raise HTTPException(status_code=409, detail="No se activa: falta SPORTSAPI365_KEY en Railway.")
        sub = _imp_subida(conn)
        if not sub["respaldo_reciente"] and not (datos or {}).get("sin_respaldo_confirmado"):
            raise HTTPException(status_code=409, detail="No hay un respaldo en el volumen de las últimas 48 h que haya pasado "
                                                        "quick_check. Creá uno (paso 1) o confirmá seguir sin él.")
        por = _quien(request).get("usuario") or "admin"
        antes = _imp_interruptores()
        anotar(conn, "activacion", "OK", None, por, {"salud": validacion.resumen_salud(conn), "conteos": validacion.conteos(conn),
                                                     "interruptores_antes": antes, "respaldo": sub["respaldo_reciente"]})
        for k in _IMP_SWITCHES:
            set_valor(k, "1", por=por)
        return {"ok": True, "interruptores": _imp_interruptores(), "subida": _imp_subida(conn),
                "nota": "Prendido. La primera vuelta corre en ≤ 30 s (carga las reglas); el ranking se captura en la vuelta horaria."}
    finally:
        conn.close()


@router.post("/api/implicacion/apagar")
def api_implicacion_apagar(request: Request, datos: dict = None):
    """Admin: «⏹ Apagar todo» — los tres interruptores a 0 (efecto en ≤ 30 s). Lo guardado queda."""
    if not _es_admin(request):
        raise HTTPException(status_code=403, detail="Solo un administrador.")
    from production.api.config_estrategias import set_valor
    from production.engines.implicacion.esquema import anotar
    por = _quien(request).get("usuario") or "admin"
    for k in _IMP_SWITCHES:
        set_valor(k, "0", por=por)
    conn = _imp_conn()
    try:
        anotar(conn, "apagado", "OK", None, por, {"motivo": str((datos or {}).get("motivo") or "")[:300]})
        return {"ok": True, "interruptores": _imp_interruptores()}
    finally:
        conn.close()


@router.post("/api/implicacion/validar")
def api_implicacion_validar(request: Request, datos: dict = None):
    """Admin: 🩺 Validación de la Fase 0 con datos reales (rankings, partidos con y sin puntos a
    defender, cuotas, resultados, histórico sin pérdidas, errores de base y otros bots).
    Queda asentada para comparar con la siguiente."""
    if not _es_admin(request):
        raise HTTPException(status_code=403, detail="Solo un administrador.")
    from production.engines.implicacion import validacion
    from production.engines.implicacion.esquema import anotar
    conn = _imp_conn()
    try:
        inf = validacion.informe(conn)
        anotar(conn, "validacion", inf["veredicto"], None, _quien(request).get("usuario") or "admin",
               {"conteos": inf["conteos"], "bloques": {k: {"estado": b["estado"], "detalle": b.get("detalle")}
                                                       for k, b in inf["bloques"].items()}})
        return inf
    finally:
        conn.close()


@router.get("/api/implicacion/respaldo/lista")
def api_implicacion_respaldo_lista(request: Request, token: str = ""):
    if not _es_admin(request):
        raise HTTPException(status_code=403, detail="Solo un administrador.")
    from production.engines import respaldo
    return {"respaldos": respaldo.listar(), "espacio_volumen": respaldo.espacio("volumen"),
            "espacio_temporal": respaldo.espacio("temporal"), "base": str(respaldo.db_path()),
            "trabajo": _IMP_TRABAJOS.get("respaldo")}


@router.post("/api/implicacion/respaldo/crear")
def api_implicacion_respaldo_crear(request: Request, datos: dict = None):
    """Admin: respaldo consistente de la base de producción, en segundo plano."""
    if not _es_admin(request):
        raise HTTPException(status_code=403, detail="Solo un administrador.")
    from production.engines import respaldo
    from production.engines.implicacion.esquema import anotar
    destino = "temporal" if (datos or {}).get("destino") == "temporal" else "volumen"
    etiqueta = str((datos or {}).get("etiqueta") or "manual")
    esp = respaldo.espacio(destino)
    if not esp["alcanza"]:
        raise HTTPException(status_code=409, detail=f"No hay espacio en {destino}: libres {esp['libre_mb']} MB, "
                                                    f"hacen falta {esp['necesario_mb']} MB.")
    por = _quien(request).get("usuario") or "admin"

    def _fn(conn):
        r = respaldo.crear(destino, etiqueta)
        try:
            anotar(conn, "respaldo", "OK" if r.get("ok") else "FALLA", None, por, r)
        except Exception:  # noqa: BLE001
            pass
        return r
    return _imp_en_segundo_plano("respaldo", _fn)


@router.get("/api/implicacion/respaldo/descargar")
def api_implicacion_respaldo_descargar(request: Request, nombre: str = ""):
    if not _es_admin(request):
        raise HTTPException(status_code=403, detail="Solo un administrador.")
    from fastapi.responses import FileResponse
    from production.engines import respaldo
    p = respaldo.ruta_segura(nombre)
    if not p or ".reemplazada-" in p.name:
        raise HTTPException(status_code=404, detail="No existe ese respaldo.")
    return FileResponse(str(p), filename=p.name, media_type="application/octet-stream")


@router.post("/api/implicacion/respaldo/borrar")
def api_implicacion_respaldo_borrar(request: Request, datos: dict = None):
    """Admin: borra UN respaldo (o una base reemplazada) por su nombre exacto."""
    if not _es_admin(request):
        raise HTTPException(status_code=403, detail="Solo un administrador.")
    from production.engines import respaldo
    r = respaldo.borrar(str((datos or {}).get("nombre") or ""))
    if not r["ok"]:
        raise HTTPException(status_code=404, detail=r["motivo"])
    return r
