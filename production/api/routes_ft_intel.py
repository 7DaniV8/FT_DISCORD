"""
production/api/routes_ft_intel.py — Lectura para FT Intelligence
(21/09/2026).

FT Intelligence es un servicio APARTE: otro repositorio, otro servicio de
Railway, su propia base en su propio volumen. De FullTenis solo LEE, y
solo por este router. No escribe nada acá: la decisión se tomó con la
investigación de retención de escrituras todavía abierta (ESTADO del
09/09, pendiente 6) -- cada escritor nuevo sobre ftr_utr.db sería una
fuente más de contención, justo donde ya duele.

REGLAS DE ESTE ROUTER (todas comprobadas en tests/test_ft_intel_lectura.py)

  1. Solo GET. Ninguna escritura y ningún PROD_DB_WRITE_LOCK.
  2. Conexión de SOLO LECTURA (mode=ro). Aunque un bug lo intentara, no
     puede escribir en ftr_utr.db. En WAL, un lector no bloquea a los
     escritores.
  3. Paginado por id (rango del índice primario) o por una ventana de
     fechas acotada: cada consulta lee un tramo, nunca la tabla entera.
  4. Secreto interno obligatorio (X-FTR-Internal-Secret), el mismo que ya
     usan el bot y el reenviador. El portón de acceso de app.py deja
     pasar las peticiones que lo traen. Excepción (28/09/2026): BOT_BUSCADOR
     usa X-FTR-Lectura-Secret, válida solo para GET /fixtures y /en-vivo.
  5. Exento del limitador global. FT Intelligence llega desde otro
     servicio de Railway; si el límite por IP se aplicara con la IP del
     proxy (uvicorn arranca sin --forwarded-allow-ips), compartiría cupo
     con el bot. Ver la nota de CAMBIOS-20260921 sobre el limitador.

COLUMNAS REALES, NO SUPUESTAS (regla 9): las columnas que se devuelven
salen de PRAGMA table_info intersectado con las que se piden. Si una
columna agregada por un ALTER no existe en alguna base, simplemente no
viaja, en vez de romper la consulta.

QUÉ LEE FT INTELLIGENCE Y CADA CUÁNTO
  /ft-intel/padron     jugadores + rating por superficie   cada 6 h
  /ft-intel/alias      alias aprendidos + manuales + huella  cada 6 h
  /ft-intel/partidos   match_log (filas por superficie)     cada 30 min
  /ft-intel/partidos/ids  ids de una ventana, para reconciliar  1 vez/día
  /ft-intel/salud      MTO (historial_fulltennis) + retiros  cada 5 min
  /ft-intel/fixtures   fixtures próximos con cuotas         cada 10 min
  /ft-intel/en-vivo    partidos en vivo: horas reales       cada 2 min
  /ft-intel/cuota-mal-puesta  señales 🔥/💎 para Discord (07/10/2026)
  /ft-intel/utr-value  UTR VALUE aprobados para Discord (07/10/2026)
  /ft-intel/utr-value/inicios  cuándo empezaron los del circuito UTR (OddsPapi)
  /ft-intel/utr-anomalia       🚨 UTR MARKET ANOMALY disparadas para Discord
  /ft-intel/utr-anomalia/inicios  ídem, circuito UTR: cuándo empezaron
  /ft-intel/pqc/pendientes     🔎 ¿Por qué la cuota?: casos para el investigador UTR_ANOMALY (v54c)
  /ft-intel/pqc/{id}/externa   (POST) lo que encontró · /ft-intel/pqc/{id}/discord el bloque para el anuncio
  /ft-intel/recordador         ⏰ avisos del Recordador para Discord (08/10/2026)
  /ft-intel/implicacion        🏆 picks VALIDADOS de Implicación de Torneo (+ pruebas 🧪) (13/10/2026)
  /ft-intel/under-utr          🔥 UNDER UTR activadas para Discord (v54i, 15/10/2026) · POST .../dicho el acuse
"""
from __future__ import annotations

import hashlib
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from production.api.deps import verify_internal_o_lectura
from shared.rate_limit import limiter

log = logging.getLogger(__name__)

# 28/09/2026: además del secreto interno (FT Intelligence, sin cambios),
# acepta FTR_LECTURA_SECRET SOLO en GET /fixtures y GET /en-vivo, que es lo
# único que lee BOT_BUSCADOR. En las demás rutas de este router, 403.
router = APIRouter(prefix="/ft-intel", tags=["ft-intel"],
                   dependencies=[Depends(verify_internal_o_lectura)])

_RAIZ = Path(__file__).resolve().parents[2]


def _conn_lectura() -> sqlite3.Connection:
    from production.api.deps import ACTIVE_DB
    uri = Path(ACTIVE_DB).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=20)
    conn.row_factory = sqlite3.Row
    return conn


def _columnas(conn: sqlite3.Connection, tabla: str, pedidas: tuple) -> list:
    reales = {r["name"] for r in conn.execute(f'PRAGMA table_info("{tabla}")')}
    return [c for c in pedidas if c in reales]


def _existe(conn: sqlite3.Connection, tabla: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (tabla,)).fetchone() is not None


def huella_tennis_names() -> str:
    """Huella de shared/tennis_names.py. FT Intelligence usa una COPIA de
    ese archivo para resolver identidades igual que FullTenis; si esta
    huella deja de coincidir con la de su copia, avisa. Se normalizan los
    saltos de línea para que un checkout con CRLF no cambie la huella."""
    texto = (_RAIZ / "shared" / "tennis_names.py").read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(texto).hexdigest()


def _ahora() -> str:
    return datetime.now(timezone.utc).isoformat()


@router.get("/padron")
@limiter.exempt
def padron(desde_id: int = Query(0, ge=0), limite: int = Query(5000, ge=1, le=5000)):
    """
    Jugadores con player_id > desde_id, con su rating por superficie.

    `ratings` es el FTR VIGENTE: la tarea de las 04:00
    (ftr_ajuste_task_loop) publica ahí cada noche la foto de FTR-Ajuste,
    solo si pasa sus controles de sanidad. NO usar `ratings_ajuste`: es la
    tabla sombra de control y se escribe incluso cuando la corrida se
    rechaza. Tampoco match_log.rating_antes/despues (motor viejo, otra escala).
    """
    conn = _conn_lectura()
    try:
        cols = _columnas(conn, "players", ("player_id", "nombre_canonico", "nombre_norm",
                                           "genero"))
        filas = conn.execute(
            f"SELECT {', '.join(cols)} FROM players WHERE player_id > ? "
            f"ORDER BY player_id LIMIT ?", (desde_id, limite)).fetchall()
        jugadores = [dict(f) for f in filas]
        if jugadores:
            rcols = _columnas(conn, "ratings", ("player_id", "superficie", "rating",
                                                "partidos_jugados", "ultima_actualizacion"))
            por_jugador: dict = {}
            for r in conn.execute(
                    f"SELECT {', '.join(rcols)} FROM ratings WHERE player_id BETWEEN ? AND ?",
                    (jugadores[0]["player_id"], jugadores[-1]["player_id"])):
                d = dict(r)
                por_jugador.setdefault(d.pop("player_id"), {})[d.pop("superficie")] = d
            for j in jugadores:
                j["ratings"] = por_jugador.get(j["player_id"], {})
        siguiente = jugadores[-1]["player_id"] if len(jugadores) == limite else None
        return {"jugadores": jugadores, "siguiente_desde_id": siguiente,
                "generado_en": _ahora()}
    finally:
        conn.close()


@router.get("/alias")
@limiter.exempt
def alias():
    """Lo que usa find_player_id() además del padrón: los alias
    aprendidos (tabla player_aliases) y los manuales (KNOWN_ALIASES, que
    viven en el código). Viaja la huella de tennis_names.py para que FT
    Intelligence detecte si su copia quedó vieja."""
    from shared.tennis_names import KNOWN_ALIASES
    conn = _conn_lectura()
    try:
        aprendidos = []
        if _existe(conn, "player_aliases"):
            cols = _columnas(conn, "player_aliases", ("alias_norm", "player_id", "genero",
                                                      "fuente"))
            aprendidos = [dict(f) for f in conn.execute(
                f"SELECT {', '.join(cols)} FROM player_aliases ORDER BY alias_norm")]
        return {"player_aliases": aprendidos, "known_aliases": dict(KNOWN_ALIASES),
                "huella_tennis_names": huella_tennis_names(), "generado_en": _ahora()}
    finally:
        conn.close()


_COLS_PARTIDO = ("match_id", "player_id", "opponent_id", "fecha", "nivel_torneo",
                 "superficie", "score", "games_ganados", "games_perdidos", "sets_ganados",
                 "sets_perdidos", "fuente", "tournament_id", "round_id", "torneo")


@router.get("/partidos")
@limiter.exempt
def partidos(desde_id: int = Query(0, ge=0), limite: int = Query(5000, ge=1, le=5000)):
    """
    match_log con match_id > desde_id, SOLO las filas por superficie.
    Cada partido tiene cuatro filas (dos caras x superficie real y
    'general'); las de 'general' repiten el partido y pierden la
    superficie, que la oposición por superficie va a necesitar.
    """
    conn = _conn_lectura()
    try:
        cols = _columnas(conn, "match_log", _COLS_PARTIDO)
        # UNA lectura por rango del índice primario, y el filtro de
        # 'general' en Python: así el cursor avanza exactamente por las
        # filas leídas. Filtrando en SQL, la página y el cursor saldrían
        # de ventanas distintas y se repetirían filas entre páginas.
        leidas = conn.execute(
            f"SELECT {', '.join(cols)} FROM match_log WHERE match_id > ? "
            f"ORDER BY match_id LIMIT ?", (desde_id, limite)).fetchall()
        filas = [dict(f) for f in leidas if f["superficie"] != "general"]
        return {"partidos": filas,
                "siguiente_desde_id": leidas[-1]["match_id"] if leidas else desde_id,
                "hay_mas": len(leidas) == limite,
                "generado_en": _ahora()}
    finally:
        conn.close()


@router.get("/partidos/ids")
@limiter.exempt
def partidos_ids(desde_fecha: str = Query(..., min_length=10, max_length=10),
                 hasta_fecha: Optional[str] = Query(None, min_length=10, max_length=10)):
    """
    Los match_id (filas por superficie) de una ventana de fechas, para que
    FT Intelligence reconcilie su réplica: si FullTenis borró filas (la
    limpieza de duplicados del 09/09 borró 7.082), la réplica también
    tiene que perderlas, o la carga contaría partidos dos veces.
    Ventana máxima: 45 días.
    """
    try:
        d = datetime.strptime(desde_fecha, "%Y-%m-%d").date()
        h = (datetime.strptime(hasta_fecha, "%Y-%m-%d").date() if hasta_fecha
             else datetime.now(timezone.utc).date() + timedelta(days=1))
    except ValueError:
        raise HTTPException(status_code=400, detail="fechas en formato YYYY-MM-DD") from None
    if h <= d or (h - d).days > 45:
        raise HTTPException(status_code=400, detail="ventana inválida (máximo 45 días)")
    conn = _conn_lectura()
    try:
        ids = [r[0] for r in conn.execute(
            "SELECT match_id FROM match_log WHERE fecha >= ? AND fecha < ? "
            "AND superficie != 'general' ORDER BY match_id", (d.isoformat(), h.isoformat()))]
        return {"desde_fecha": d.isoformat(), "hasta_fecha": h.isoformat(), "ids": ids,
                "generado_en": _ahora()}
    finally:
        conn.close()


# mensaje_original (24/09/2026): FT Intelligence lo usa para saber si el
# partido tiene video -- el aviso de MTO trae 📺 junto a los jugadores.
_COLS_MTO = ("id", "fecha_partido", "dia_clave", "home", "away", "home_norm", "away_norm",
             "torneo", "superficie", "genero", "payload_json", "creado_en",
             "mensaje_original")
_COLS_RETIRO = ("id", "jugador", "jugador_norm", "genero", "rival", "torneo", "superficie",
                "score", "fecha_retiro", "fuente", "result_type", "match_id")


@router.get("/salud")
@limiter.exempt
def salud(desde_id_mto: int = Query(0, ge=0), desde_id_retiro: int = Query(0, ge=0),
          limite: int = Query(1000, ge=1, le=5000)):
    """
    MTO con jugador y retiros. Los MTO salen de historial_fulltennis
    (origen='mto'), NO de mto_alertas: esa se purga cada 12 h
    (FTR_RETENCION_MTO_HORAS) y la copia permanente es el historial,
    desde el 28/08. El payload trae `jugador_mto`: quién pidió el MTO,
    que es lo que Live Tennis API no sabe en ITF.
    """
    conn = _conn_lectura()
    try:
        mto, retiros = [], []
        if _existe(conn, "historial_fulltennis"):
            cols = _columnas(conn, "historial_fulltennis", _COLS_MTO)
            mto = [dict(f) for f in conn.execute(
                f"SELECT {', '.join(cols)} FROM historial_fulltennis "
                f"WHERE origen='mto' AND id > ? ORDER BY id LIMIT ?", (desde_id_mto, limite))]
        if _existe(conn, "retiros_jugadores"):
            cols = _columnas(conn, "retiros_jugadores", _COLS_RETIRO)
            retiros = [dict(f) for f in conn.execute(
                f"SELECT {', '.join(cols)} FROM retiros_jugadores WHERE id > ? "
                f"ORDER BY id LIMIT ?", (desde_id_retiro, limite))]
        return {"mto": mto, "retiros": retiros,
                "siguiente_desde_id_mto": mto[-1]["id"] if len(mto) == limite else None,
                "siguiente_desde_id_retiro": (retiros[-1]["id"] if len(retiros) == limite
                                              else None),
                "generado_en": _ahora()}
    finally:
        conn.close()


_COLS_FIXTURE = ("fixture_id", "fecha", "torneo", "superficie", "genero", "jugador1",
                 "jugador2", "odd1", "odd2", "ftr1", "ftr2", "elo1", "elo2", "prob_ftr",
                 "prob_elo", "favorito_nombre", "cuota_favorito", "actualizado_en")


@router.get("/under")
@limiter.exempt
def under(desde_id: int = Query(0, ge=0), limite: int = Query(50, ge=1, le=200)):
    """
    Los UNDER nuevos, CON su contexto histórico ya calculado (23/09/2026).
    FT Intelligence solo transporta: acá viven el historial y las cuentas.

    El contexto usa únicamente picks ANTERIORES a cada uno y ya resueltos
    (ver shared/under_contexto.py): nada del propio partido ni posterior.
    """
    from shared.under_contexto import contexto as calcular_contexto
    conn = _conn_lectura()
    try:
        if not _existe(conn, "valor_historial"):
            return {"under": [], "hay_mas": False, "generado_en": _ahora()}
        cols = _columnas(conn, "valor_historial",
                         ("id", "creado_en", "home", "away", "liga", "superficie", "genero",
                          "under_total", "under_cuota", "cuota_home", "cuota_away", "enviado_picks",
                          "resultado_score", "ganador_final"))
        # v54f (14/10/2026, Rubén: «que los que llegan al Telegram suenen en Discord»): solo los que de verdad
        # salieron a Telegram (enviado_picks=1). `ultimo_id` deja a FT Intelligence notar un cursor adelantado
        # (base resembrada: ids más chicos que los que ya vio) en vez de quedarse mudo para siempre.
        solo_enviados = " AND enviado_picks=1" if "enviado_picks" in cols else ""
        filas = [dict(f) for f in conn.execute(
            f"SELECT {', '.join(cols)} FROM valor_historial WHERE tipo='under'{solo_enviados} AND id > ? "
            f"ORDER BY id LIMIT ?", (desde_id, limite + 1)).fetchall()]
        hay_mas = len(filas) > limite
        salida = []
        for f in filas[:limite]:
            try:
                ctx = calcular_contexto(conn, f)
            except Exception as e:                     # un contexto roto no puede frenar a los demás Under
                log.warning(f"[FTIntel/under] contexto del under #{f.get('id')}: {e}")
                ctx = {}
            salida.append({**f, "contexto": ctx})
        ultimo = conn.execute(f"SELECT COALESCE(MAX(id), 0) FROM valor_historial WHERE tipo='under'{solo_enviados}").fetchone()[0]
        return {"under": salida, "hay_mas": hay_mas, "ultimo_id": ultimo, "generado_en": _ahora()}
    finally:
        conn.close()


@router.get("/movimiento-rentable")
@limiter.exempt
def movimiento_rentable(desde_id: int = Query(0, ge=0), horas: Optional[int] = Query(None, ge=1, le=168),
                        limite: int = Query(200, ge=1, le=500)):
    """📉 (v54h) Los Movimiento Rentable que salieron a Telegram (enviado_picks=1), con el texto de Discord (mismo
    encabezado y frase que Telegram) y la voz armados, la hora del partido (fixture) y el par. FT Intelligence los
    dice minutos antes o al empezar, como Cuota Mal Puesta y UTR VALUE."""
    from production.engines import mov_discord
    conn = _conn_lectura()
    try:
        return {"movimiento_rentable": mov_discord.feed(conn, desde_id, horas, limite), "generado_en": _ahora()}
    finally:
        conn.close()


@router.post("/movimiento-rentable/dicho")
@limiter.exempt
async def movimiento_rentable_dicho(request: Request):
    """📉 (v54h) FT Intelligence informa qué pasó con cada Movimiento Rentable en Discord (con el acuse de FT Discord).
    Solo escribe mov_discord. Cuerpo: {"avisos": [{"id": <valor_historial.id>, "dicho": true, "texto": true, ...}]}"""
    try:
        cuerpo = await request.json()
        avisos = cuerpo.get("avisos") or []
    except Exception:
        raise HTTPException(status_code=400, detail="Cuerpo inválido: se espera {'avisos': [...]}")
    if not isinstance(avisos, list) or len(avisos) > 500:
        raise HTTPException(status_code=400, detail="'avisos' tiene que ser una lista de hasta 500")
    from production.api.deps import ejecutar_con_reintento, get_conn
    from production.engines import mov_discord
    conn = get_conn()
    try:
        hechos, ignorados = ejecutar_con_reintento(lambda: mov_discord.marcar(conn, avisos), conn=conn, con_lock=True)
    finally:
        conn.close()
    return {"ok": True, "marcados": hechos, "ignorados": ignorados, "generado_en": _ahora()}


@router.get("/under-utr")
@limiter.exempt
def under_utr(desde_seq: int = Query(0, ge=0), limite: int = Query(100, ge=1, le=300)):
    """🔥 UNDER UTR (v54i, 15/10/2026): las activaciones que salen a Discord, con texto y voz armados (formato del
    pedido de Rubén). Cursor por `discord_seq` (orden de activación), no por id: la fila se crea al empezar a vigilar
    el partido y se activa mucho después. FT Intelligence las anuncia EN EL ACTO (es en vivo)."""
    from production.engines import under_utr as uu
    conn = _conn_lectura()
    try:
        return {**uu.para_discord(conn, desde_seq, limite), "generado_en": _ahora()}
    finally:
        conn.close()


@router.post("/under-utr/dicho")
@limiter.exempt
async def under_utr_dicho(request: Request):
    """🔥 (v54i) FT Intelligence informa qué pasó con cada UNDER UTR en Discord (con el acuse de FT Discord).
    Cuerpo: {"avisos": [{"id": <under_utr_registro.id>, "dicho": true, "texto": true, "voz": true, "motivo": null}]}"""
    try:
        cuerpo = await request.json()
        avisos = cuerpo.get("avisos") or []
    except Exception:
        raise HTTPException(status_code=400, detail="Cuerpo inválido: se espera {'avisos': [...]}")
    if not isinstance(avisos, list) or len(avisos) > 500:
        raise HTTPException(status_code=400, detail="'avisos' tiene que ser una lista de hasta 500")
    from production.api.deps import ejecutar_con_reintento, get_conn
    from production.engines import under_utr as uu
    conn = get_conn()
    try:
        hechos, ignorados = ejecutar_con_reintento(lambda: uu.marcar_dicho(conn, avisos), conn=conn, con_lock=True)
    finally:
        conn.close()
    return {"ok": True, "marcados": hechos, "ignorados": ignorados, "generado_en": _ahora()}


@router.post("/under/dicho")
@limiter.exempt
async def under_dicho(request: Request):
    """▼ (v54f) FT Intelligence informa qué pasó con cada Under en Discord: si lo anunció y el ACUSE de FT Discord
    (texto publicado / voz encolada) o por qué no salió. Solo escribe under_discord (la pestaña lo muestra).
    Cuerpo: {"avisos": [{"id": <valor_historial.id>, "senal_id": 9, "dicho": true, "texto": true, "voz": true,
    "motivo": null, "en": "..."}]}"""
    try:
        cuerpo = await request.json()
        avisos = cuerpo.get("avisos") or []
    except Exception:
        raise HTTPException(status_code=400, detail="Cuerpo inválido: se espera {'avisos': [...]}")
    if not isinstance(avisos, list) or len(avisos) > 500:
        raise HTTPException(status_code=400, detail="'avisos' tiene que ser una lista de hasta 500")
    from production.api.deps import ejecutar_con_reintento, get_conn
    from production.engines import under_discord
    conn = get_conn()
    try:
        hechos, ignorados = ejecutar_con_reintento(lambda: under_discord.marcar(conn, avisos), conn=conn, con_lock=True)
    finally:
        conn.close()
    return {"ok": True, "marcados": hechos, "ignorados": ignorados, "generado_en": _ahora()}


@router.get("/maestros")
@limiter.exempt
def maestros(desde_id: int = Query(0, ge=0), limite: int = Query(200, ge=1, le=500)):
    """
    Los Partidos Maestros detectados, para que FT Intelligence los avise en
    Discord (23/09/2026). Solo lectura, incremental por id: FT Intelligence
    guarda el último que vio y pide los siguientes.

    NO toca la lógica de Maestros ni su aviso de Telegram: es una salida
    más de lo mismo que ya se guarda en partidos_maestros.
    """
    conn = _conn_lectura()
    try:
        if not _existe(conn, "partidos_maestros"):
            return {"maestros": [], "hay_mas": False, "generado_en": _ahora()}
        cols = _columnas(conn, "partidos_maestros",
                         ("id", "fixture_id", "par_norm", "fecha", "torneo", "superficie", "genero",
                          "jugador1", "jugador2", "favorito_nombre", "cuota_favorito", "prob_ftr",
                          "prob_elo", "ganador_final", "resultado_score", "creado_en",
                          # 24/09/2026: la insignia ⭐/🔥 que decide RankingFTR,
                          # para que Discord diga lo mismo que Telegram.
                          "insignia", "regla_version"))
        filas = [dict(f) for f in conn.execute(
            f"SELECT {', '.join(cols)} FROM partidos_maestros WHERE id > ? ORDER BY id LIMIT ?",
            (desde_id, limite + 1)).fetchall()]
        hay_mas = len(filas) > limite
        return {"maestros": filas[:limite], "hay_mas": hay_mas, "generado_en": _ahora()}
    finally:
        conn.close()


@router.get("/ventaja-leve")
@limiter.exempt
def ventaja_leve(desde_id: int = Query(0, ge=0), limite: int = Query(200, ge=1, le=500)):
    """
    Ventaja Leve que SÍ sonaron (regla VL01-VL05 y alerta_enviada=1), para
    que FT Intelligence las lleve a Discord (23/09/2026). Incremental por
    id, igual que /maestros. Las SILENCIOSAS no viajan: en Discord no se
    habla de ellas, ni por texto ni por voz.

    `texto_discord` y `texto_voz` vienen armados de acá, así el % y la
    muestra son los de la regla que disparó y no se pueden desincronizar
    del mensaje de Telegram. La voz no lee Elo, FTR, muestra ni cuota.
    """
    conn = _conn_lectura()
    try:
        if not _existe(conn, "ventaja_leve_registro"):
            return {"ventaja_leve": [], "hay_mas": False, "generado_en": _ahora()}
        cols = _columnas(conn, "ventaja_leve_registro",
                         ("id", "fixture_id", "par_norm", "fecha_partido", "hora_partido", "torneo",
                          "genero", "home", "away", "favorito_leve", "rival", "cuota_pre_favorito",
                          "elo_pre", "ftr_pre", "set1_score", "set1_favorito", "set1_rival",
                          "regla", "clasificacion", "texto_discord", "texto_voz", "detectado_en"))
        filas = [dict(f) for f in conn.execute(
            f"SELECT {', '.join(cols)} FROM ventaja_leve_registro "
            f"WHERE id > ? AND regla IS NOT NULL AND alerta_enviada=1 ORDER BY id LIMIT ?",
            (desde_id, limite + 1)).fetchall()]
        return {"ventaja_leve": filas[:limite], "hay_mas": len(filas) > limite,
                "generado_en": _ahora()}
    finally:
        conn.close()


@router.get("/pasan-filtro")
@limiter.exempt
def pasan_filtro(desde_id: int = Query(0, ge=0), limite: int = Query(200, ge=1, le=500)):
    """
    Pasan Filtro con tendencia UNDER/OVER que SÍ sonó (23/09/2026), para
    Discord. Mismo contrato que /ventaja-leve: incremental por id, solo lo
    enviado, texto y voz ya armados. Lo silencioso no viaja.
    """
    conn = _conn_lectura()
    try:
        if not _existe(conn, "pasan_filtro_registro"):
            return {"pasan_filtro": [], "hay_mas": False, "generado_en": _ahora()}
        cols = _columnas(conn, "pasan_filtro_registro",
                         ("id", "fixture_id", "par_norm", "fecha_partido", "hora_partido", "torneo",
                          "genero", "home", "away", "favorito", "rival", "cuota_pre_favorito",
                          "elo_pre", "ftr_pre", "set1_score", "marcador", "regla_descripcion",
                          "tendencia", "pct_historico", "n_historico", "texto_discord",
                          "texto_voz", "detectado_en"))
        filas = [dict(f) for f in conn.execute(
            f"SELECT {', '.join(cols)} FROM pasan_filtro_registro "
            f"WHERE id > ? AND regla_clave IS NOT NULL AND alerta_enviada=1 ORDER BY id LIMIT ?",
            (desde_id, limite + 1)).fetchall()]
        return {"pasan_filtro": filas[:limite], "hay_mas": len(filas) > limite,
                "generado_en": _ahora()}
    finally:
        conn.close()


def _puerta_cmp(conn: sqlite3.Connection) -> dict:
    """La puerta de Discord de Cuota Mal Puesta vigente, leída con ESTA
    conexión de solo lectura (config_estrategias.obtener() abre una conexión
    de escritura: este router no le escribe nunca a la base). Misma
    prioridad: base > variable de entorno > default del registro."""
    import os
    from production.api.config_estrategias import REGISTRO
    claves = ("CMP_DISCORD_FT_MIN", "CMP_DISCORD_VENTAJA_MIN_PP")
    guardados = {}
    if _existe(conn, "ftr_config_estrategias"):
        guardados = {r[0]: r[1] for r in conn.execute(
            "SELECT clave, valor FROM ftr_config_estrategias WHERE clave IN (?, ?)", claves).fetchall()}
    out = {}
    for k in claves:
        try:
            out[k] = float(guardados.get(k, os.getenv(k, REGISTRO[k]["default"])))
        except (TypeError, ValueError):
            out[k] = float(REGISTRO[k]["default"])
    return out


@router.get("/cuota-mal-puesta")
@limiter.exempt
def cuota_mal_puesta(desde_id: int = Query(0, ge=0), limite: int = Query(200, ge=1, le=500)):
    """
    CUOTA MAL PUESTA V1 (07/10/2026) que SÍ pasaron los controles de
    Discord (🔥 CMP / 💎 PREMIUM, discord_sent=1). Mismo contrato que
    /ventaja-leve: incremental por id, solo lo publicable, texto y voz ya
    armados acá -- así Discord dice exactamente lo que decidió el motor.
    Las 🟡 EN ESTUDIO y los ❌ no viajan: se estudian en silencio.

    Discord avisa cuando el partido EMPIEZA (07/10/2026): FT Intelligence
    lo espera en su réplica del feed en vivo, buscándolo por player_a_id /
    player_b_id (y por nombre si faltan). Telegram ya salió al detectarla,
    desde RankingFTR.
    Ver production/engines/cuota_mal_puesta.py.
    """
    conn = _conn_lectura()
    try:
        if not _existe(conn, "cmp_senales"):
            return {"cuota_mal_puesta": [], "hay_mas": False, "generado_en": _ahora()}
        cols = _columnas(conn, "cmp_senales",
                         ("id", "fixture_id", "par_norm", "fecha_partido", "torneo", "superficie",
                          "genero", "player_a", "player_b",
                          # 07/10/2026: FT Intelligence reconoce el partido en
                          # vivo por estos ids (mismo padrón) para avisar en
                          # Discord cuando EMPIEZA.
                          "player_a_id", "player_b_id",
                          "odds_a", "market_prob", "elo_prob",
                          "ftr_prob", "l10_score_player", "l10_score_opponent", "l10_diff",
                          "ft_prob_ajustada", "ft_fair_odds", "mispricing_pp", "classification",
                          "discord_nivel", "texto_discord", "texto_voz", "timestamp_signal",
                          "version_motor",
                          # V3 (17/10/2026): la MISMA medición en todos lados (mercado sin margen, foto congelada)
                          "prob_ft_final", "edge_pp", "market_prob_sin_margen", "market_prob_bruta",
                          "mercado_medida", "odds_b", "foto_senal_json"))
        filas = [dict(f) for f in conn.execute(
            f"SELECT {', '.join(cols)} FROM cmp_senales "
            f"WHERE id > ? AND discord_sent=1 ORDER BY id LIMIT ?",
            (desde_id, limite + 1)).fetchall()]
        hay_mas = len(filas) > limite
        filas = filas[:limite]
        # 07/10/2026 noche: lo publicado por una versión anterior que no cumple
        # la puerta VIGENTE (FT ≥ 60 % y ventaja ≥ +10 pp) no viaja, aunque la
        # tarea de cierre todavía no la haya retirado. Ver
        # cuota_mal_puesta.revocar_fuera_de_puerta.
        if filas and "ft_prob_ajustada" in cols and "mispricing_pp" in cols:
            from production.engines.cuota_mal_puesta import fuera_de_puerta
            cfg = _puerta_cmp(conn)
            filas = [f for f in filas if not fuera_de_puerta(f, cfg)]
        return {"cuota_mal_puesta": filas, "hay_mas": hay_mas, "generado_en": _ahora()}
    finally:
        conn.close()


@router.get("/discord-config")
@limiter.exempt
def discord_config_feed():
    """📣 (08/10/2026) Lo que Admin → Discord decidió: por tipo, texto/voz,
    encabezado y frase de voz, más la pausa general. FT Intelligence lo copia
    y FT Discord lo aplica antes de escribir o hablar. Solo lectura."""
    from production.engines import discord_config
    from production.api.deps import get_conn
    conn = get_conn()          # crea la tabla si no existe (idempotente); después solo lee
    try:
        return discord_config.para_discord(conn)
    finally:
        conn.close()


@router.post("/cuota-mal-puesta/dicho")
@limiter.exempt
async def cuota_mal_puesta_dicho(request: Request):
    """
    (08/10/2026, V2) LA ÚNICA ESCRITURA de este router, y a propósito mínima:
    FT Intelligence avisa qué señal de Cuota Mal Puesta anunció DE VERDAD en
    Discord (o por qué no: ya salió por UTR VALUE, no se la vio empezar,
    repetida). Completa la tercera marca (dicho_discord / dicho_discord_en /
    motivo_no_dicho) de cmp_senales; no toca ninguna otra columna. Así el
    histórico distingue: recomendado_modelo (matemática) · publicado_rankingftr
    (lo que RankingFTR decidió) · dicho_discord (lo que llegó a Discord).
    Cuerpo: {"avisos": [{"id": 12, "dicho": true, "en": "...", "motivo": null}, ...]}
    Idempotente: repetir el mismo aviso deja la fila igual.
    """
    try:
        cuerpo = await request.json()
        avisos = cuerpo.get("avisos") or []
    except Exception:
        raise HTTPException(status_code=400, detail="Cuerpo inválido: se espera {'avisos': [...]}")
    if len(avisos) > 500:
        raise HTTPException(status_code=400, detail="Máximo 500 avisos por llamada")
    from production.api.deps import get_conn
    from production.engines.cuota_mal_puesta import marcar_dicho
    conn = get_conn()
    hechos, ignorados = 0, []
    try:
        for a in avisos:
            try:
                sid = int(a.get("id"))
            except (TypeError, ValueError):
                ignorados.append(a)
                continue
            if marcar_dicho(conn, sid, bool(a.get("dicho")), a.get("en"), a.get("motivo")):
                hechos += 1
            else:
                ignorados.append({"id": sid, "motivo": "no existe"})
    finally:
        conn.close()
    return {"ok": True, "marcados": hechos, "ignorados": ignorados, "generado_en": _ahora()}


@router.post("/cuota-mal-puesta/revalidar")
@limiter.exempt
async def cuota_mal_puesta_revalidar(request: Request):
    """
    FOTO 2 (09/10/2026, Rubén): FT Intelligence la llama ≈10 minutos antes del
    partido, ANTES de anunciar una señal detectada horas antes. RankingFTR
    recalcula todo con la información actual (Elo, FTR, oposición, carga,
    MTO <24h, cuota) y TODAS las reglas vigentes. Respuesta por señal:
    {"id", "aprobado", "motivos", "codigos", "texto_discord", "texto_voz", "nivel", "valores"}.
    Si no cumple, la señal queda guardada como descartada en revalidación
    (no se borra) y retirada de Discord. Cuerpo: {"ids": [12, 13]}.
    """
    try:
        cuerpo = await request.json()
        ids = [int(x) for x in (cuerpo.get("ids") or [])]
    except Exception:
        raise HTTPException(status_code=400, detail="Cuerpo inválido: se espera {'ids': [...]}")
    if len(ids) > 100:
        raise HTTPException(status_code=400, detail="Máximo 100 ids por llamada")
    from production.api.deps import get_conn
    from production.engines.cuota_mal_puesta import revalidar
    conn = get_conn()
    out = []
    try:
        for sid in ids:
            try:
                out.append(revalidar(conn, sid))
            except Exception as e:                       # una señal rota no frena a las demás
                log.warning(f"[CuotaMalPuesta] revalidar id={sid}: {e}", exc_info=True)
                out.append({"id": sid, "aprobado": False, "motivos": [f"error al revalidar: {e}"],
                            "codigos": ["ERROR_REVALIDACION"]})
    finally:
        conn.close()
    return {"ok": True, "resultados": out, "generado_en": _ahora()}


@router.get("/cuota-mal-puesta/revocadas")
@limiter.exempt
def cuota_mal_puesta_revocadas(horas: int = Query(72, ge=1, le=168)):
    """
    (07/10/2026 noche) Señales que se habían publicado para Discord y que
    NO deben anunciarse: retiradas por no cumplir la puerta vigente, o
    todavía publicadas pero fuera de ella. FT Intelligence las saca de su
    espera (no se anuncian al empezar el partido).
    """
    conn = _conn_lectura()
    try:
        if not _existe(conn, "cmp_senales"):
            return {"revocadas": [], "generado_en": _ahora()}
        from production.engines.cuota_mal_puesta import fuera_de_puerta
        cfg = _puerta_cmp(conn)
        desde = (datetime.now(timezone.utc) - timedelta(hours=horas)).isoformat()
        tiene_rev = "discord_revocado_en" in _columnas(conn, "cmp_senales", ("discord_revocado_en",))
        filas = conn.execute(
            "SELECT id, ft_prob_ajustada, mispricing_pp, discord_sent, motivo_no_discord"
            + (", discord_revocado_en" if tiene_rev else "")
            + " FROM cmp_senales WHERE timestamp_signal >= ? AND (discord_sent=1"
            + (" OR discord_revocado_en IS NOT NULL" if tiene_rev else "") + ")", (desde,)).fetchall()
        out = []
        for f in filas:
            if tiene_rev and f["discord_revocado_en"]:
                out.append({"id": f["id"], "motivo": f["motivo_no_discord"]})
            else:
                m = fuera_de_puerta(f, cfg)
                if m:
                    out.append({"id": f["id"], "motivo": f"no cumple la puerta vigente ({m})"})
        return {"revocadas": out, "generado_en": _ahora()}
    finally:
        conn.close()


@router.get("/utr-value")
@limiter.exempt
def utr_value(desde_id: int = Query(0, ge=0), limite: int = Query(200, ge=1, le=500),
              horas: Optional[int] = Query(None, ge=1, le=168)):
    """
    UTR VALUE (07/10/2026) APROBADOS para Discord (discord_sent=1): candidato
    (ΔUTR ≥ 0.75 y cuota ≥ 1.70) que pasó los filtros de calidad. Mismo
    contrato que /cuota-mal-puesta: incremental por id, texto y voz armados
    acá, player_a_id/player_b_id para reconocer el partido en vivo. FT
    Intelligence lo anuncia cuando el partido EMPIEZA. Ver
    production/engines/utr_value.py.
    """
    conn = _conn_lectura()
    try:
        if not _existe(conn, "utr_value_registro"):
            return {"utr_value": [], "hay_mas": False, "generado_en": _ahora()}
        cols = _columnas(conn, "utr_value_registro",
                         ("id", "fixture_id", "par_norm", "fecha_partido", "torneo", "superficie",
                          "genero", "jugador_a", "jugador_b", "player_a_id", "player_b_id",
                          "utr_a", "utr_b", "delta_utr", "odds_a_cand", "p_mkt_a_cand", "p_utr",
                          "utr_value_pp_cand", "motivo", "texto_discord", "texto_voz",
                          "decidido_en", "version_motor", "fuente"))
        # `horas` (07/10/2026): TODOS los aprobados de las últimas N horas, no
        # "id > desde_id". Un partido puede aprobarse DESPUÉS de que se
        # aprobaron filas más nuevas (candidato tardío: la cuota llegó a 1.70
        # más tarde); con el cursor por id, FT Intelligence nunca lo veía.
        # No cambia ninguna regla de UTR VALUE: solo que lo aprobado llegue.
        if isinstance(horas, int) and horas:          # (llamada directa: Query no es int)
            corte = (datetime.now(timezone.utc) - timedelta(hours=horas)).isoformat()
            filas = [dict(f) for f in conn.execute(
                f"SELECT {', '.join(cols)} FROM utr_value_registro "
                f"WHERE discord_sent=1 AND decidido_en >= ? ORDER BY id LIMIT ?",
                (corte, limite + 1)).fetchall()]
        else:
            filas = [dict(f) for f in conn.execute(
                f"SELECT {', '.join(cols)} FROM utr_value_registro "
                f"WHERE id > ? AND discord_sent=1 ORDER BY id LIMIT ?",
                (desde_id, limite + 1)).fetchall()]
        return {"utr_value": filas[:limite], "hay_mas": len(filas) > limite,
                "generado_en": _ahora()}
    finally:
        conn.close()


@router.get("/fixtures")
@limiter.exempt
def fixtures(horas_atras: int = Query(12, ge=0, le=72),
             horas_adelante: int = Query(48, ge=1, le=96),
             limite: int = Query(500, ge=1, le=1000), offset: int = Query(0, ge=0)):
    """
    Fixtures de fixtures_ftr_elo en una ventana alrededor de ahora, con
    cuotas. La cota inferior se compara por DÍA: los fixtures ITF llegan
    con la fecha sola, sin hora ('2026-09-21'), y con una cota con hora
    los del día de hoy quedarían afuera.
    Ojo: fixture_id sale de un CRC32 del bot y puede chocar (el propio
    /fixture/reportar lo registra como "CHOQUE de fixture_id"); por eso
    viajan también los nombres.
    """
    ahora = datetime.now(timezone.utc)
    desde = (ahora - timedelta(hours=horas_atras)).date().isoformat()
    hasta = (ahora + timedelta(hours=horas_adelante)).isoformat()
    conn = _conn_lectura()
    try:
        if not _existe(conn, "fixtures_ftr_elo"):
            return {"fixtures": [], "hay_mas": False, "generado_en": _ahora()}
        cols = _columnas(conn, "fixtures_ftr_elo", _COLS_FIXTURE)
        filas = [dict(f) for f in conn.execute(
            f"SELECT {', '.join(cols)} FROM fixtures_ftr_elo WHERE fecha >= ? AND fecha <= ? "
            f"ORDER BY fecha, fixture_id LIMIT ? OFFSET ?", (desde, hasta, limite + 1, offset))]
        return {"fixtures": filas[:limite], "hay_mas": len(filas) > limite,
                "ventana": {"desde": desde, "hasta": hasta}, "generado_en": _ahora()}
    finally:
        conn.close()


# Solo columnas livianas. timeline_json y stats_json quedan afuera a
# propósito: con decenas de partidos simultáneos y una consulta cada 2
# minutos, serían cientos de MB por día leídos del proceso sensible.
_COLS_EN_VIVO = ("fid", "home", "away", "genero", "tour", "liga", "estado", "fuente",
                 "set_actual", "set1_score", "set2_score", "set3_score", "set4_score",
                 "set5_score", "home_sets", "away_sets", "games_home", "games_away",
                 "primera_vez_visto", "ultima_actualizacion", "set1_cerrado_en",
                 "procesando_avisos")
_TOPE_EN_VIVO = 2000


@router.get("/en-vivo")
@limiter.exempt
def en_vivo():
    """
    Los partidos que el bot está siguiendo en vivo, con sus HORAS REALES:
      primera_vez_visto     primer marcador que empujó el bot (≈ inicio real)
      ultima_actualizacion  último marcador (cada ~30 s mientras se juega)
      set1_cerrado_en       cuándo se cerró el primer set
    FullTenis no guarda estas horas en ningún otro lado, y estas filas se
    BORRAN: al procesarse el resultado, o a las 3 h en 'procesando' / 8 h
    sin actualizar. FT Intelligence lee esto cada 2 minutos y las guarda en
    su propia base: es la única fuente de duraciones y de horas entre
    partidos (el backfill tiene las fechas a medianoche).
    La tabla es chica (lo que se está jugando y lo recién terminado).
    """
    conn = _conn_lectura()
    try:
        if not _existe(conn, "partidos_en_vivo"):
            return {"partidos": [], "truncado": False, "generado_en": _ahora()}
        cols = _columnas(conn, "partidos_en_vivo", _COLS_EN_VIVO)
        filas = [dict(f) for f in conn.execute(
            f"SELECT {', '.join(cols)} FROM partidos_en_vivo "
            f"ORDER BY primera_vez_visto LIMIT ?", (_TOPE_EN_VIVO + 1,))]
        return {"partidos": filas[:_TOPE_EN_VIVO], "truncado": len(filas) > _TOPE_EN_VIVO,
                "generado_en": _ahora()}
    finally:
        conn.close()


@router.get("/utr-value/inicios")
@limiter.exempt
def utr_value_inicios(horas: int = Query(48, ge=1, le=168)):
    """
    (07/10/2026) Los partidos del circuito UTR llegan por OddsPapi y NO están
    en el feed en vivo de FullTenis, así que FT Intelligence no puede ver
    cuándo empiezan. RankingFTR se lo pregunta a OddsPapi para las señales
    aprobadas (production/engines/oddspapi_utr.py::vigilar_inicios) y lo
    publica acá: [{id de utr_value_registro, inicio_real}], de las últimas
    `horas`. Solo lectura, como todo este router.
    """
    conn = _conn_lectura()
    try:
        if not (_existe(conn, "utr_value_registro") and _existe(conn, "oddspapi_utr_partidos")):
            return {"inicios": [], "generado_en": _ahora()}
        desde = (datetime.now(timezone.utc) - timedelta(hours=horas)).isoformat()
        filas = [dict(f) for f in conn.execute(
            "SELECT r.id, o.inicio_real, o.inicio_visto_en, o.estado FROM utr_value_registro r "
            "JOIN oddspapi_utr_partidos o ON o.fixture_id = r.fixture_id "
            "WHERE r.discord_sent=1 AND o.inicio_real IS NOT NULL AND o.inicio_visto_en >= ? "
            "ORDER BY r.id", (desde,)).fetchall()]
        return {"inicios": filas, "generado_en": _ahora()}
    finally:
        conn.close()


# ── 🔎 ¿POR QUÉ LA CUOTA? (v54c, 14/10/2026, Rubén) ─────────────────────────────────────────────
# El estudio de contexto de cada UTR Market Anomaly. SOLO REFERENCIA: estas rutas leen y escriben únicamente
# pqc_contexto; ninguna toca utr_anomalia_registro ni decide nada de la señal.

@router.get("/pqc/pendientes")
@limiter.exempt
def pqc_pendientes(limite: int = Query(2, ge=1, le=10)):
    """Los casos que el investigador de FT Intelligence (tipo UTR_ANOMALY) tiene que revisar: SOLO las preguntas que
    quedaron sin respuesta con nuestros datos, los idiomas calculados y los conceptos traducidos. Quedan EN_CURSO."""
    from production.api.deps import get_conn
    from production.engines import pqc_contexto
    conn = get_conn()
    try:
        conn.row_factory = sqlite3.Row
        return {"casos": pqc_contexto.tomar_externas(conn, limite), "generado_en": _ahora()}
    finally:
        conn.close()


@router.post("/pqc/{registro_id}/externa")
@limiter.exempt
async def pqc_externa(registro_id: int, request: Request):
    """Lo que encontró el investigador (evidencias con cita literal ya verificadas por FT Intelligence). Se guarda y se
    reclasifica el contexto. Nunca toca la señal."""
    try:
        cuerpo = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Cuerpo inválido")
    from production.api.deps import get_conn
    from production.engines import pqc_contexto
    conn = get_conn()
    try:
        conn.row_factory = sqlite3.Row
        r = pqc_contexto.recibir_externa(conn, registro_id, cuerpo if isinstance(cuerpo, dict) else {})
    finally:
        conn.close()
    if not r.get("ok"):
        raise HTTPException(status_code=404, detail=r.get("error") or "sin estudio")
    return r


@router.get("/pqc/{registro_id}/discord")
@limiter.exempt
def pqc_discord(registro_id: int):
    """El bloque «🔎 ¿POR QUÉ LA CUOTA?» para adjuntar al anunciar. Solo lectura. listo=False si todavía no hay estudio:
    FT Intelligence anuncia igual (no espera)."""
    from production.engines import pqc_contexto
    if _anomalia_corto_activo():
        # 18/10/2026: con el mensaje corto, los datos del contexto ya van DENTRO del aviso: no se adjunta otro bloque
        return {"listo": False, "texto": None, "motivo": "incluido en el aviso (UTRA_MENSAJE_CORTO)"}
    conn = _conn_lectura()
    try:
        if not _existe(conn, "pqc_contexto"):
            return {"listo": False, "texto": None}
        return pqc_contexto.bloque(conn, registro_id)
    finally:
        conn.close()


@router.get("/utr-contexto/{tipo}/{registro_id}")
@limiter.exempt
def utr_contexto(tipo: str, registro_id: int):
    """v54l (17/10/2026, Rubén): 🔎 FT INTELLIGENCE · CONTEXTO DEL PARTIDO en el formato estándar corto, para
    💎 UTR VALUE (tipo 'v' o 'utrv') y 🚨 UTR MARKET ANOMALY (tipo 'a' o 'utra'). Solo lectura. FT Intelligence lo
    adjunta DESPUÉS de anunciar la señal: nunca la espera, bloquea ni modifica. listo=False si la fila no existe."""
    clave = {"v": "utrv", "utrv": "utrv", "a": "utra", "utra": "utra"}.get((tipo or "").lower())
    if not clave:
        raise HTTPException(status_code=404, detail="tipo: v (UTR VALUE) o a (UTR MARKET ANOMALY)")
    if clave == "utra" and _anomalia_corto_activo():
        return {"listo": False, "texto": None, "contexto": None, "motivo": "incluido en el aviso (UTRA_MENSAJE_CORTO)",
                "generado_en": _ahora()}
    from production.engines import contexto_utr
    conn = _conn_lectura()
    try:
        ctx = contexto_utr.armar(conn, clave, registro_id)
    except Exception as e:
        log.warning(f"[FTIntel/utr-contexto] {clave} #{registro_id}: {e}")
        ctx = None
    finally:
        conn.close()
    return {"listo": bool(ctx), "texto": contexto_utr.texto(ctx), "contexto": contexto_utr.solo_publico(ctx),
            "generado_en": _ahora()}


_CORTO_CACHE: dict = {}


def _anomalia_corto_activo() -> bool:
    try:
        from production.api.config_estrategias import obtener_bool
        return bool(obtener_bool("UTRA_MENSAJE_CORTO"))
    except Exception:                                    # pragma: no cover
        return True


def _anomalia_corta(conn, rid) -> Optional[str]:
    """18/10/2026 (Rubén): el aviso de 🚨 UTR MARKET ANOMALY en el formato corto «CUOTA EXTREMA», con los datos
    VERIFICADOS del contexto (H2H, salud, regreso…) adentro. Se arma al entregarlo (contexto fresco, cortado en la hora
    de inicio) y se recuerda 10 min. None = apagado o error → queda el texto guardado de siempre."""
    import time as _t
    if rid is None or not _anomalia_corto_activo():
        return None
    hit = _CORTO_CACHE.get(rid)
    if hit and _t.time() - hit[0] < 600:
        return hit[1]
    try:
        from production.engines import contexto_utr, utr_anomalia
        r = conn.execute("SELECT * FROM utr_anomalia_registro WHERE id=?", (rid,)).fetchone()
        if not r:
            return None
        f = dict(r)
        try:
            ctx = contexto_utr.armar(conn, "utra", int(rid))
        except Exception as e:
            log.warning(f"[FTIntel/utr-anomalia] contexto #{rid}: {e}")
            ctx = None
        txt = utr_anomalia.armar_corto(f, ctx)
    except Exception as e:
        log.warning(f"[FTIntel/utr-anomalia] mensaje corto #{rid}: {e}")
        return None
    if len(_CORTO_CACHE) > 500:
        _CORTO_CACHE.clear()
    _CORTO_CACHE[rid] = (_t.time(), txt)
    return txt


@router.get("/utr-anomalia")
@limiter.exempt
def utr_anomalia(desde_id: int = Query(0, ge=0), limite: int = Query(200, ge=1, le=500),
                 horas: Optional[int] = Query(None, ge=1, le=168)):
    """
    🚨 UTR MARKET ANOMALY (07/10/2026) disparadas para Discord (discord_sent=1):
    ΔUTR ≥ 0.25 y cuota ≥ 4.00 que pasaron los controles técnicos. Mismo
    contrato que /utr-value. `fase_activacion`: 'vivo' → FT Intelligence la
    anuncia EN EL ACTO; 'pre' → cuando el partido empieza. Si UTR VALUE ya
    aprobó el mismo partido (un solo mensaje, prioridad UTR VALUE), no sale
    acá. Ver production/engines/utr_anomalia.py.
    """
    conn = _conn_lectura()
    try:
        if not _existe(conn, "utr_anomalia_registro"):
            return {"utr_anomalia": [], "hay_mas": False, "generado_en": _ahora()}
        cols = _columnas(conn, "utr_anomalia_registro",
                         ("id", "fixture_id", "event_id", "par_norm", "fecha_partido", "torneo",
                          "superficie", "genero", "jugador1", "jugador2", "jugador_a", "jugador_b",
                          "player_a_id", "player_b_id", "utr_a", "utr_b", "delta_utr",
                          "cuota_pre_activacion", "cuota_activacion", "fase_activacion",
                          "activacion_en", "publicado_en", "motivo", "texto_discord", "texto_voz",
                          "version_motor", "fuente", "fuente_activacion"))
        sin_utrv = ""
        if _existe(conn, "utr_value_registro"):
            # Un solo mensaje por partido: si UTR VALUE lo aprobó (aunque haya
            # sido después del disparo), la anomalía no se publica.
            sin_utrv = (" AND NOT EXISTS (SELECT 1 FROM utr_value_registro v WHERE "
                        "(v.par_norm = a.par_norm OR (v.player_a_id = a.player_a_id AND "
                        "v.player_b_id = a.player_b_id)) AND v.discord_sent=1 AND "
                        "v.primera_vista_en >= strftime('%Y-%m-%dT%H:%M:%S', a.primera_vista_en, '-48 hours'))")
        # `horas`: por hora de activación, no por id (la fila se crea al ver
        # el partido y se activa mucho después; ver /utr-value).
        if isinstance(horas, int) and horas:          # (llamada directa: Query no es int)
            corte = (datetime.now(timezone.utc) - timedelta(hours=horas)).isoformat()
            filtro, arg = "a.activacion_en >= ?", corte
        else:
            filtro, arg = "a.id > ?", desde_id
        filas = [dict(f) for f in conn.execute(
            f"SELECT {', '.join('a.' + c for c in cols)} FROM utr_anomalia_registro a "
            f"WHERE {filtro} AND a.discord_sent=1{sin_utrv} ORDER BY a.id LIMIT ?",
            (arg, limite + 1)).fetchall()]
        filas = filas[:limite + 1]
        for f in filas[:limite]:
            corto = _anomalia_corta(conn, f.get("id"))
            if corto:
                f["texto_discord"], f["formato"] = corto, "corto-20261018"
                try:
                    from production.engines.utr_anomalia import voz_corta
                    f["texto_voz"] = voz_corta(f)
                except Exception:                      # pragma: no cover — queda la voz de siempre
                    pass
        return {"utr_anomalia": filas[:limite], "hay_mas": len(filas) > limite,
                "generado_en": _ahora()}
    finally:
        conn.close()


@router.get("/utr-externo")
@limiter.exempt
def utr_externo(desde_id: int = Query(0, ge=0), limite: int = Query(200, ge=1, le=500),
                horas: Optional[int] = Query(None, ge=1, le=168)):
    """
    📡 UTR EXTERNO (09/10/2026): las alertas LIVE VALUE / PREMATCH VALUE del
    canal de Telegram DOC | TIRS aprobadas para Discord (discord_sent=1), ya en
    nuestro formato (texto_discord / texto_voz). `tipo`: LIVE → FT Intelligence
    la anuncia EN EL ACTO; PREMATCH → minutos antes si `fecha_partido` viene
    (el partido se encontró en nuestras fuentes), si no en el acto. Ver
    production/engines/utr_externo.py.
    """
    conn = _conn_lectura()
    try:
        if not _existe(conn, "utr_externo_senales"):
            return {"utr_externo": [], "hay_mas": False, "generado_en": _ahora()}
        from production.engines.utr_externo import para_discord
        h = horas if isinstance(horas, int) and horas else None
        filas = para_discord(conn, desde_id=desde_id, horas=h, limite=limite + 1)
        return {"utr_externo": filas[:limite], "hay_mas": len(filas) > limite, "generado_en": _ahora()}
    finally:
        conn.close()


@router.get("/horarios")
@limiter.exempt
def horarios_cambios(desde_id: int = Query(0, ge=0), horas: int = Query(48, ge=1, le=168),
                     limite: int = Query(500, ge=1, le=2000)):
    """
    🕒 (19/10/2026, Rubén) CAMBIOS DE HORARIO de los partidos con señal, para TODAS las estrategias. Incremental por
    `desde_id` (como los demás feeds). Cada fila: {id, estrategia (= tabla de la señal: cmp_senales, utr_value_registro,
    utr_anomalia_registro, under_utr_registro…; 'partido' = el cambio del partido en sí), ref_id (id de la fila de esa
    tabla = el id que trae su feed), fixture_id, de, a (hora vieja → nueva), minutos (+ = se retrasó), en, fuente}.
    `feeds` dice qué tabla corresponde a qué feed. FT Intelligence debe mirar esto antes de decir «EMPIEZA EN ~10 MIN»
    y usar `a` (la hora vigente) en lugar de la que guardó en caché.
    """
    from production.engines import horarios
    conn = _conn_lectura()
    try:
        if not _existe(conn, horarios.LOG):
            return {"cambios": [], "ultimo_id": desde_id, "feeds": _FEEDS_HORARIO, "generado_en": _ahora()}
        filas = horarios.cambios(conn, desde_id=desde_id, horas=horas, limite=limite)
        # El texto VIGENTE de cada señal (ya con la hora nueva y «⚠️ cambió de hora»), para que FT Intelligence
        # reemplace el que guardó. Solo tablas cuyo texto guardado es el que sale a Discord.
        for tabla in _TEXTO_HORARIO:
            ids = sorted({f["ref_id"] for f in filas if f["estrategia"] == tabla})
            if not ids or not _existe(conn, tabla):
                continue
            textos = {r[0]: (r[1], r[2]) for r in conn.execute(
                f"SELECT id, texto_discord, texto_voz FROM {tabla} WHERE id IN ({','.join('?' * len(ids))})",
                tuple(ids)).fetchall()}
            for f in filas:
                if f["estrategia"] == tabla and f["ref_id"] in textos:
                    f["texto_discord"], f["texto_voz"] = textos[f["ref_id"]]
        return {"cambios": filas, "ultimo_id": max([f["id"] for f in filas] or [desde_id]),
                "feeds": _FEEDS_HORARIO, "generado_en": _ahora()}
    finally:
        conn.close()


_TEXTO_HORARIO = ("cmp_senales", "utr_value_registro", "utr_externo_senales")
_FEEDS_HORARIO = {
    "cmp_senales": "/ft-intel/cuota-mal-puesta", "utr_value_registro": "/ft-intel/utr-value",
    "utr_anomalia_registro": "/ft-intel/utr-anomalia", "under_utr_registro": "/ft-intel/under-utr",
    "ventaja_leve_registro": "/ft-intel/ventaja-leve", "pasan_filtro_registro": "/ft-intel/pasan-filtro",
    "utr_externo_senales": "/ft-intel/utr-externo",
    "partido": "todas (por fixture_id): el cambio del partido en sí",
}


@router.get("/utr-anomalia/inicios")
@limiter.exempt
def utr_anomalia_inicios(horas: int = Query(48, ge=1, le=168)):
    """Como /utr-value/inicios, para las UTR MARKET ANOMALY PRE-partido del
    circuito UTR (OddsPapi): [{id de utr_anomalia_registro, inicio_real}]."""
    conn = _conn_lectura()
    try:
        if not (_existe(conn, "utr_anomalia_registro") and _existe(conn, "oddspapi_utr_partidos")):
            return {"inicios": [], "generado_en": _ahora()}
        desde = (datetime.now(timezone.utc) - timedelta(hours=horas)).isoformat()
        filas = [dict(f) for f in conn.execute(
            "SELECT a.id, o.inicio_real, o.inicio_visto_en, o.estado FROM utr_anomalia_registro a "
            "JOIN oddspapi_utr_partidos o ON o.fixture_id = a.fixture_id "
            "WHERE a.discord_sent=1 AND a.fase_activacion='pre' AND o.inicio_real IS NOT NULL "
            "AND o.inicio_visto_en >= ? ORDER BY a.id", (desde,)).fetchall()]
        return {"inicios": filas, "generado_en": _ahora()}
    finally:
        conn.close()


@router.get("/recordador")
@limiter.exempt
def recordador(horas: int = Query(6, ge=1, le=72), limite: int = Query(200, ge=1, le=500)):
    """
    ⏰ RECORDADOR (08/10/2026, Rubén: "se debe decir en el canal de Discord
    minutos antes o cuando ya estén en juego"). Los avisos que RankingFTR YA
    decidió (production/engines/recordador.py: faltan pocos minutos o el
    jugador está en juego), de las últimas `horas`, con texto y voz armados.
    FT Intelligence los publica en el acto, una vez cada uno (por id).
    momento = ANTES | EN_JUEGO. Solo lectura, como todo este router.
    """
    conn = _conn_lectura()
    try:
        if not _existe(conn, "recordador_avisos"):
            return {"recordador": [], "generado_en": _ahora()}
        corte = (datetime.now(timezone.utc) - timedelta(hours=horas)).isoformat()
        cols = _columnas(conn, "recordador_avisos",
                         ("id", "recordatorio_id", "nombre", "partido_json", "avisado_en", "momento",
                          "texto_discord", "texto_voz", "tipo", "prediccion_id"))
        import json as _json
        salida = []
        for f in conn.execute(f"SELECT {', '.join(cols)} FROM recordador_avisos "
                              "WHERE discord=1 AND avisado_en >= ? ORDER BY id LIMIT ?",
                              (corte, limite)).fetchall():
            d = dict(f)
            p = _json.loads(d.pop("partido_json", None) or "{}")
            # v43 (11/10/2026): tipo RECORDADOR (de siempre) | RECORDADOR_IA (5 min antes, con
            # investigación) | RECORDADOR_VIVO (inicio real). FT Intelligence emite ese tipo tal cual.
            d["tipo"] = d.get("tipo") or "RECORDADOR"
            d.update(jugador=p.get("jugador") or d.get("nombre"), rival=p.get("rival"),
                     torneo=p.get("torneo"), genero=p.get("genero"), inicio=p.get("inicio"),
                     minutos=p.get("minutos"), fuente=p.get("fuente"),
                     fixture_id=p.get("fixture_id"), prueba=bool(p.get("prueba")))   # v43b: aviso de prueba
            if d.get("prediccion_id") and _existe(conn, "recordador_investigaciones"):
                inv = conn.execute("SELECT p_base_jugador, p_base_rival, p_ajustada_jugador, version, version_modelo, metodo_base "
                                   "FROM recordador_investigaciones WHERE id=?", (d["prediccion_id"],)).fetchone()
                if inv:
                    # v54c: sin Elo ni FTR no hay probabilidad FullTennis (la tabla UTR de versiones viejas tampoco)
                    from production.engines.recordador_ia import es_sin_nivel
                    sn = inv["p_base_jugador"] is None or es_sin_nivel(inv["metodo_base"])
                    d["probabilidad"] = {"jugador": None if sn else inv["p_base_jugador"], "rival": None if sn else inv["p_base_rival"],
                                         "ajustada_jugador": None if sn else inv["p_ajustada_jugador"], "version": inv["version"],
                                         "modelo": inv["version_modelo"], "base": inv["p_ajustada_jugador"] is None, "sin_nivel": sn}
            salida.append(d)
        return {"recordador": salida, "generado_en": _ahora()}
    finally:
        conn.close()


@router.post("/recordador/dicho")
@limiter.exempt
async def recordador_dicho(request: Request):
    """v43 (11/10/2026): FT Intelligence informa qué avisos del Recordador anunció
    (o por qué no), igual que /cuota-mal-puesta/dicho. {"avisos": [{"id", "dicho",
    "en", "motivo"}]}. Escribe dicho_discord / dicho_en / dicho_motivo en
    recordador_avisos. Idempotente."""
    try:
        datos = await request.json()
        avisos = datos.get("avisos") or []
    except Exception:
        raise HTTPException(status_code=400, detail="Cuerpo inválido: se espera {'avisos': [...]}")
    if not isinstance(avisos, list):
        raise HTTPException(status_code=400, detail="avisos debe ser una lista")
    from production.api.deps import ejecutar_con_reintento, get_conn
    conn = get_conn()
    try:
        if not _existe(conn, "recordador_avisos"):
            return {"marcados": 0}
        cols = {c[1] for c in conn.execute("PRAGMA table_info(recordador_avisos)").fetchall()}
        if "dicho_discord" not in cols:
            # la tabla existe pero es anterior a v43: se agregan las columnas (misma migración que la pestaña)
            from production.engines.recordador import _asegurar_nota
            _asegurar_nota(conn)
            cols = {c[1] for c in conn.execute("PRAGMA table_info(recordador_avisos)").fetchall()}
        if "dicho_discord" not in cols:
            return {"marcados": 0, "detalle": "RankingFTR sin la marca dicho_discord (versión anterior)"}
        n = {"v": 0}

        def _w():
            for a in avisos[:500]:
                try:
                    aid = int(a.get("id") or 0)
                except (TypeError, ValueError):
                    continue
                n["v"] += conn.execute(
                    "UPDATE recordador_avisos SET dicho_discord=?, dicho_en=COALESCE(?, dicho_en), dicho_motivo=? "
                    "WHERE id=? AND COALESCE(dicho_discord, 0)=0",
                    (1 if a.get("dicho") else 2, a.get("en"), (a.get("motivo") or None), aid)).rowcount
            conn.commit()
        ejecutar_con_reintento(_w, conn=conn, con_lock=True)
        return {"marcados": n["v"]}
    finally:
        conn.close()


# ═════════════════════════════════════════════════════════════════════
#  📰 FT NEWS INTELLIGENCE · Fase 0 (08/10/2026) — lo ÚNICO que RankingFTR
#  agrega para el servicio FT_NEWS: dos lecturas más. Nada se escribe.
# ═════════════════════════════════════════════════════════════════════
_COLS_UTR_PADRON = ("player_id", "nombre_ftr", "genero", "estado", "nivel_confianza", "utr_player_id",
                    "nombre_utr", "utr_singles", "utr_display", "estado_utr", "fiabilidad", "utr_3_meses",
                    "utr_dura", "utr_tierra", "utr_hierba", "nacionalidad", "ranking_utr", "fecha_captura",
                    "usable", "origen", "importado_en")


@router.get("/implicacion")
@limiter.exempt
def implicacion(horas: int = Query(6, ge=1, le=48), limite: int = Query(200, ge=1, le=500)):
    """🏆 IMPLICACIÓN DE TORNEO (v45, 13/10/2026): picks PUBLICABLES de las últimas `horas`
    (regla VALIDADA + IMP_DISCORD_ACTIVO = 1 al detectarse) y las PRUEBAS del circuito
    (🧪, marcadas `prueba`). Los candidatos experimentales NUNCA viajan. Texto y voz ya
    armados. FT Intelligence lo dice EN EL ACTO (el partido ya está en juego)."""
    # v62 (18/10/2026, Rubén): antes de servir, la cuota LIVE de Pinnacle de cada señal pendiente se refresca (última
    # de los últimos IMP_CUOTA_FRESCA_SEG s) y se rehacen texto y voz; sin cuota actual no viaja (picks.refrescar_para_envio).
    try:
        from production.api.deps import get_conn as _gc
        from production.engines.implicacion.picks import refrescar_para_envio
        _cw = _gc()
        try:
            if _existe(_cw, "implicacion_picks"):
                refrescar_para_envio(_cw)
        finally:
            _cw.close()
    except Exception:  # noqa: BLE001 — si falla, simplemente no viaja nada nuevo (el filtro exige la cuota refrescada)
        pass
    conn = _conn_lectura()
    try:
        if not _existe(conn, "implicacion_picks"):
            return {"implicacion": [], "generado_en": _ahora()}
        desde = (datetime.now(timezone.utc) - timedelta(hours=horas)).isoformat(timespec="seconds")
        # Candado al servir (además de `publicable`, fijado al detectar): ver picks.filtro_feed. Los
        # experimentales SOLO viajan marcados aviso='experimental' y con la opción independiente prendida.
        try:
            from production.engines.implicacion.picks import cfg as _imp_cfg, filtro_feed
            filtro = filtro_feed(_imp_cfg())
        except Exception:  # noqa: BLE001 — sin config, lo seguro: solo pruebas
            filtro = "(prueba = 1)"
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(implicacion_picks)")}
        extra = ", ".join(c for c in ("aviso", "nivel_implicacion") if c in cols)
        if "aviso" not in cols:                       # base de antes de v47: solo pruebas o validados
            filtro = filtro.replace(" OR (nivel = 'experimental' AND aviso = 'experimental')", "")
        if "cuota_envio" in cols:
            from production.engines.implicacion.picks import FILTRO_CUOTA_ACTUAL
            filtro = f"{filtro} AND {FILTRO_CUOTA_ACTUAL}"
            col_cuota = "COALESCE(cuota_envio, cuota) AS cuota, cuota AS cuota_deteccion, cuota_envio_en"
        else:
            col_cuota = "cuota"
        filas = [dict(f) for f in conn.execute(
            "SELECT id, partido_id, regla, nivel, prueba, detectado_en, fecha_partido, torneo, genero, seleccion AS jugador, rival, "
            f"player_id_sel AS player_a_id, player_id_rival AS player_b_id, {col_cuota}, set1_marcador, fav_cuota_pre, defensa_sel, "
            "defensa_pct, utr_sel, utr_rival, texto_discord, texto_voz" + (", " + extra if extra else "") + " FROM implicacion_picks "
            f"WHERE publicable = 1 AND {filtro} AND detectado_en >= ? ORDER BY id LIMIT ?", (desde, limite)).fetchall()]
        for f in filas:
            f["tipo"] = "IMPLICACION"
        return {"implicacion": filas, "generado_en": _ahora()}
    finally:
        conn.close()


@router.post("/implicacion/dicho")
@limiter.exempt
async def implicacion_dicho(request: Request):
    """🏆 (v45) FT Intelligence informa qué pick de Implicación dijo DE VERDAD en Discord
    (o por qué no). Solo completa dicho_discord / dicho_en / motivo_no_dicho del pick.
    Cuerpo: {"avisos": [{"id": 12, "dicho": true, "en": "...", "motivo": null, "texto": true, "voz": true}, ...]}
    v47: `texto` / `voz` = el ACUSE de FT Discord (publicado en el canal / voz encolada). Una PRUEBA con los
    dos confirmados enciende los avisos de Implicación si IMP_DISCORD_AUTO_ACTIVAR = 1 (picks.auto_activar)."""
    try:
        cuerpo = await request.json()
        avisos = cuerpo.get("avisos") or []
    except Exception:
        raise HTTPException(status_code=400, detail="Cuerpo inválido: se espera {'avisos': [...]}")
    if len(avisos) > 500:
        raise HTTPException(status_code=400, detail="Máximo 500 avisos por llamada")
    from production.api.deps import get_conn
    from production.engines.implicacion.picks import marcar_dicho
    conn = get_conn()
    hechos, ignorados = 0, []
    try:
        for a in avisos:
            try:
                pid = int(a.get("id"))
            except (TypeError, ValueError):
                ignorados.append(a)
                continue
            if marcar_dicho(conn, pid, bool(a.get("dicho")), a.get("en"), a.get("motivo"),
                            a.get("texto") if "texto" in a else None, a.get("voz") if "voz" in a else None):
                hechos += 1
            else:
                ignorados.append({"id": pid, "motivo": "no existe"})
    finally:
        conn.close()
    return {"ok": True, "marcados": hechos, "ignorados": ignorados, "generado_en": _ahora()}


@router.get("/utr/padron")
@limiter.exempt
def utr_padron_feed(desde_id: int = Query(0, ge=0), limite: int = Query(2000, ge=1, le=5000)):
    """
    📰 FT NEWS (Fase 0): el padrón UTR (utr_perfiles) paginado por player_id,
    para que FT_NEWS tenga la identidad maestra (nombre + país + UTR ID) y
    la fecha de captura del UTR (NEWS_UTR_MAX_DIAS). Solo lectura.
    """
    conn = _conn_lectura()
    try:
        if not _existe(conn, "utr_perfiles"):
            return {"jugadores": [], "siguiente_desde_id": None, "generado_en": _ahora()}
        cols = _columnas(conn, "utr_perfiles", _COLS_UTR_PADRON)
        filas = [dict(f) for f in conn.execute(
            f"SELECT {', '.join(cols)} FROM utr_perfiles WHERE player_id > ? ORDER BY player_id LIMIT ?",
            (desde_id, limite)).fetchall()]
        siguiente = filas[-1]["player_id"] if len(filas) == limite else None
        return {"jugadores": filas, "siguiente_desde_id": siguiente, "generado_en": _ahora()}
    finally:
        conn.close()


@router.get("/news/calibracion")
@limiter.exempt
def news_calibracion(desde: Optional[str] = Query(None, min_length=10, max_length=32),
                     formato: str = Query("json", pattern="^(json|md)$")):
    """
    📰 FT NEWS (Fase 0): informe de calibración de bandas UTR/cuota con los
    datos propios (utr_value_registro). Ver production/engines/news_calibracion.py.
    Solo lectura; con formato=md devuelve el informe en Markdown.
    """
    from fastapi.responses import PlainTextResponse
    from production.engines import news_calibracion as nc
    conn = _conn_lectura()
    try:
        inf = nc.informe(conn, desde)
    finally:
        conn.close()
    if formato == "md":
        return PlainTextResponse(nc.informe_markdown(inf), media_type="text/markdown; charset=utf-8")
    return inf


# ── 📰 FT NEWS · Motor 3 y Motor 5 (08/10/2026, segunda entrega) ──────────
@router.get("/news/historial")
@limiter.exempt
def news_historial(desde_fecha: str = Query(..., min_length=10, max_length=10),
                   hasta_fecha: Optional[str] = Query(None, min_length=10, max_length=10),
                   limite: int = Query(20000, ge=1, le=50000)):
    """
    match_log en una ventana de fechas (una cara por partido: la del
    ganador, con superficie real), con nombres y país del torneo si se conoce.
    Para FT_NEWS: carga reciente, viajes, cierre de resultados. Solo lectura.
    """
    conn = _conn_lectura()
    try:
        cols = _columnas(conn, "match_log", ("match_id", "player_id", "opponent_id", "fecha", "nivel_torneo",
                                             "superficie", "score", "games_ganados", "games_perdidos",
                                             "sets_ganados", "sets_perdidos", "torneo", "tournament_id", "round_id"))
        hasta = hasta_fecha or "9999-12-31"
        filas = []
        paises = {}
        if _existe(conn, "ftr_torneos") and "pais" in {r["name"] for r in conn.execute('PRAGMA table_info("ftr_torneos")')}:
            paises = {r["clave"]: r["pais"] for r in conn.execute("SELECT clave, pais FROM ftr_torneos WHERE pais IS NOT NULL")}
        try:
            from production.api.torneos import clave_de
        except Exception:
            clave_de = lambda n: ""  # noqa: E731
        for f in conn.execute(
                f"SELECT m.{', m.'.join(cols)}, p.nombre_canonico AS jugador, o.nombre_canonico AS rival, "
                f"p.genero AS genero FROM match_log m JOIN players p ON p.player_id = m.player_id "
                f"JOIN players o ON o.player_id = m.opponent_id WHERE m.fecha >= ? AND m.fecha <= ? "
                f"AND m.superficie <> 'general' AND m.sets_ganados >= m.sets_perdidos "
                f"ORDER BY m.fecha, m.match_id LIMIT ?", (desde_fecha, hasta + "T23:59:59", limite)):
            d = dict(f)
            d["pais_torneo"] = paises.get(clave_de(d.get("torneo"))) if d.get("torneo") else None
            filas.append(d)
        return {"partidos": filas, "hay_mas": len(filas) == limite, "generado_en": _ahora()}
    finally:
        conn.close()


@router.get("/news/antecedentes")
@limiter.exempt
def news_antecedentes(player_id: int = Query(..., ge=1), torneo: str = Query(..., min_length=2, max_length=120),
                      limite: int = Query(200, ge=1, le=1000)):
    """Partidos de un jugador en ediciones anteriores de un torneo (por núcleo del nombre)."""
    conn = _conn_lectura()
    try:
        # Ediciones anteriores: por CIUDAD (núcleo del nombre), como hace el
        # historial de superficies; el cuadro puede cambiar de categoría.
        try:
            from shared.surfaces import _nucleo_torneo
            clave = (_nucleo_torneo(torneo) or "").lower()
        except Exception:
            clave = torneo.lower()
        if not clave:
            return {"partidos": [], "clave": clave, "generado_en": _ahora()}
        cols = _columnas(conn, "match_log", ("match_id", "player_id", "opponent_id", "fecha", "superficie", "score",
                                             "games_ganados", "games_perdidos", "torneo", "round_id"))
        filas = [dict(f) for f in conn.execute(
            f"SELECT {', '.join(cols)} FROM match_log WHERE player_id = ? AND superficie <> 'general' "
            f"AND LOWER(torneo) LIKE ? ORDER BY fecha DESC LIMIT ?", (player_id, f"%{clave}%", limite))]
        return {"partidos": filas, "clave": clave, "generado_en": _ahora()}
    finally:
        conn.close()


@router.get("/news/defensa")
@limiter.exempt
def news_defensa():
    """Snapshot semanal de Defensa de Puntos (lo calcula el job de los lunes):
    por jugador, puntos que defiende, de qué torneo, presión y estado. Si el
    snapshot no existe, lista vacía (no es error)."""
    from datetime import date as _date
    season, semana, _ = _date.today().isocalendar()
    conn = _conn_lectura()
    try:
        if not _existe(conn, "ranking_defense_snapshot"):
            return {"jugadores": [], "semana": semana, "season": season, "generado_en": _ahora()}
        filas = [dict(f) for f in conn.execute(
            "SELECT s.player_id, p.nombre_canonico AS nombre, p.genero, s.tour, s.current_points, s.dropping_points, "
            "s.dropping_tournament_id, s.canonical_tournament_id, s.defense_percentage, s.previous_round, "
            "s.participation_status, s.points_source, s.estado, s.calculated_at FROM ranking_defense_snapshot s "
            "JOIN players p ON p.player_id = s.player_id WHERE s.season = ? AND s.ranking_week = ? "
            "ORDER BY s.dropping_points DESC", (season, semana))]
        return {"jugadores": filas, "semana": semana, "season": season, "generado_en": _ahora()}
    finally:
        conn.close()


# FT PROBABILIDAD V16 · Única integración nueva en RankingFTR.
# Endpoint autenticado por verify_internal_o_lectura del router; SOLO lectura.
# No ejecuta motores, no crea tareas ni accede a las tablas de partidos.
# 30/10/2026: además del interruptor entrega los pesos del cálculo combinado (editables en
# Admin → Estrategias → FT PROBABILIDAD). Sigue siendo UNA lectura indexada por claves.
_DEFECTO_PESOS_FTP = {"FT_PROB_PESO_TENNIS_DATA": "60", "FT_PROB_PESO_CUOTA": "30", "FT_PROB_PESO_UTR": "10"}


def _pesos_ft_probabilidad(guardados: dict) -> tuple:
    """(pesos en fracción | None, pesos crudos en %, motivo | None). Lectura: base, entorno, default.
    Si no suman 100 se reparten en proporción (ver normalizar_pesos_ft_prob)."""
    import os
    from production.api.config_estrategias import CLAVES_PESOS_FT_PROB, normalizar_pesos_ft_prob
    crudos = []
    for clave in CLAVES_PESOS_FT_PROB:
        x = guardados.get(clave)
        if x is None or str(x).strip() == "":
            x = os.getenv(clave, _DEFECTO_PESOS_FTP[clave])
        crudos.append(x)
    pesos, motivo = normalizar_pesos_ft_prob(*crudos)
    pct = dict(zip(("TENNIS_DATA", "CUOTA", "UTR"), crudos))
    return pesos, pct, motivo


@router.get("/probabilidad/config")
@limiter.exempt
def ft_probabilidad_config():
    # ÚNICA consulta: lectura por clave (PRIMARY KEY) cada 60s.
    # Ni _ensure_tabla, ni get_conn de escritura, ni lectura de fixtures.
    claves = ["FT_PROBABILIDAD_ACTIVO", "FT_PROB_PESO_TENNIS_DATA", "FT_PROB_PESO_CUOTA", "FT_PROB_PESO_UTR"]
    conn = _conn_lectura()
    try:
        guardados = {}
        if _existe(conn, "ftr_config_estrategias"):
            marcas = ",".join("?" * len(claves))
            guardados = {f["clave"]: f["valor"] for f in conn.execute(
                f"SELECT clave, valor FROM ftr_config_estrategias WHERE clave IN ({marcas})", claves)}
        valor = str(guardados.get("FT_PROBABILIDAD_ACTIVO", "")).strip().lower() or None
        activo = valor in ("1", "true", "si", "sí")
        pesos, pct, motivo = _pesos_ft_probabilidad(guardados)
        return {
            "activo": activo,
            # None cuando los pesos no son válidos: el servicio conserva los últimos válidos.
            "pesos": pesos,
            "pesos_pct": pct,
            "pesos_validos": pesos is not None,
            "pesos_motivo": motivo,
            "generado_en": _ahora(),
        }
    finally:
        conn.close()


@router.get("/probabilidad/candidatos")
@limiter.exempt
def ft_probabilidad_candidatos():
    """
    (30/10/2026) Candidatos de FT PROBABILIDAD para Discord: jugador con probabilidad del modelo > FT_PROB_CAND_PROB_MIN
    y cuota ACTUAL > FT_PROB_CAND_CUOTA_MIN (Admin → Estrategias → FT PROBABILIDAD). Sin estado y solo lectura: cada
    llamada consulta el servicio (red privada) y la cuota de ahora en fixtures_ftr_elo; FT Intelligence es quien evita
    repetir avisos (clave única por partido, jugador y etapa). Cada candidato trae su `etapa` (DETECTADO / INICIO) y el
    texto y la frase de voz ya armados. Servicio apagado o sin respuesta: lista vacía con el motivo.
    """
    from production.api.config_estrategias import obtener_bool, obtener_float, obtener_int
    from production.engines import ft_prob_candidatos as fpc

    def vacio(motivo):
        return {"candidatos": [], "motivo": motivo, "generado_en": _ahora()}

    if not obtener_bool("FT_PROBABILIDAD_ACTIVO"):
        return vacio("apagado")
    try:
        from research.api.routes_ft_probabilidad import _pedir
    except ImportError:
        return vacio("sin_modulo_research")
    r, error = _pedir("/resumen", {"limite": 200})
    if error:
        return vacio(error.get("motivo") or "sin_servicio")
    if r.status_code != 200:
        return vacio(f"http_{r.status_code}")
    try:
        partidos = r.json().get("partidos") or []
    except (ValueError, AttributeError):
        return vacio("respuesta_invalida")
    ids = sorted({int(p["id_ftr"]) for p in partidos if str(p.get("id_ftr") or "").isdigit()})
    cuotas = {}
    if ids:
        conn = _conn_lectura()
        try:
            if _existe(conn, "fixtures_ftr_elo"):
                cols = _columnas(conn, "fixtures_ftr_elo", ("fixture_id", "odd1", "odd2", "actualizado_en"))
                if {"fixture_id", "odd1", "odd2"} <= set(cols):
                    marcas = ",".join("?" * len(ids))
                    for f in conn.execute(f"SELECT {', '.join(cols)} FROM fixtures_ftr_elo "
                                          f"WHERE fixture_id IN ({marcas})", ids):
                        cuotas[str(f["fixture_id"])] = dict(f)
        finally:
            conn.close()
    prob_min = obtener_float("FT_PROB_CAND_PROB_MIN") / 100.0
    cuota_min = obtener_float("FT_PROB_CAND_CUOTA_MIN")
    min_antes = obtener_int("FT_PROB_CAND_MIN_ANTES")
    res = fpc.evaluar(partidos, cuotas, datetime.now(timezone.utc), prob_min, cuota_min, min_antes)
    # Historial de la pestaña (dashboard «solo enviado a Discord»): se registra lo que se está enviando y se cierran los
    # ya jugados. Nunca debe romper la respuesta que lee FT Intelligence.
    try:
        from production.api.deps import get_conn
        from production.engines import ft_prob_registro
        wconn = get_conn()
        try:
            if res["candidatos"]:
                ft_prob_registro.registrar(wconn, res["candidatos"])
            ft_prob_registro.cerrar_pendientes(wconn)
        finally:
            wconn.close()
    except Exception:
        log.exception("ft_prob_registro: no se pudo registrar/cerrar candidatos")
    return {"candidatos": res["candidatos"], "revisados": res["revisados"],
            "regla": {"prob_min": prob_min, "cuota_min": cuota_min, "min_antes": min_antes},
            "generado_en": _ahora()}


@router.get("/probabilidad/resultados")
@limiter.exempt
def ft_probabilidad_resultados(dias: int = Query(5, ge=1, le=14),
                               limite: int = Query(3000, ge=1, le=5000)):
    """
    (30/10/2026) Partidos TERMINADOS de los últimos `dias` días con su ganador, para que FT PROBABILIDAD
    etiquete sus predicciones PRE y pueda medir (backtest / calibración). Solo lectura de match_log.

    Reglas, para no contaminar la medición:
      - un partido por pareja y día (match_log guarda dos caras por superficie);
      - se EXCLUYEN retiros, walkovers, abandonos y marcadores truncados: el ganador de un partido que no se
        completó no mide la fuerza de los jugadores;
      - `fecha` de match_log suele venir sin hora: el servicio decide la hora de confirmación, no esta ruta.
    """
    from shared.retiros import es_retiro_por_score, es_no_disputado_por_score, score_truncado
    desde = (datetime.now(timezone.utc) - timedelta(days=dias)).date().isoformat()
    conn = _conn_lectura()
    try:
        if not (_existe(conn, "match_log") and _existe(conn, "players")):
            return {"resultados": [], "excluidos": {}, "generado_en": _ahora()}
        filas = conn.execute(
            "SELECT m.*, w.nombre_canonico AS _ganador, l.nombre_canonico AS _perdedor "
            "FROM match_log m JOIN players w ON w.player_id = m.player_id "
            "JOIN players l ON l.player_id = m.opponent_id "
            "WHERE m.fecha >= ? AND m.superficie != 'general' AND m.sets_ganados > m.sets_perdidos "
            "ORDER BY m.match_id LIMIT ?", (desde, limite)).fetchall()
        excluidos = {"retiro_o_incompleto": 0, "duplicado": 0}
        vistos, salida = set(), []
        for f in filas:
            f = dict(f)
            score = f.get("score") or ""
            if (not score or es_retiro_por_score(score) or es_no_disputado_por_score(score)
                    or score_truncado(score)):
                excluidos["retiro_o_incompleto"] += 1
                continue
            clave = (f["player_id"], f["opponent_id"], str(f["fecha"])[:10])
            if clave in vistos:
                excluidos["duplicado"] += 1
                continue
            vistos.add(clave)
            salida.append({"match_id": f["match_id"], "fecha": str(f["fecha"])[:10],
                           "ganador": f["_ganador"], "perdedor": f["_perdedor"], "score": score,
                           "superficie": f.get("superficie"), "torneo": f.get("torneo"),
                           "nivel_torneo": f.get("nivel_torneo")})
        return {"resultados": salida, "excluidos": excluidos, "desde": desde, "generado_en": _ahora()}
    finally:
        conn.close()
