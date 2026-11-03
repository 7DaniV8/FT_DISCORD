"""
production/engines/ft_prob_registro.py — 🎲 Historial de los candidatos de FT PROBABILIDAD que van a Discord
(30/10/2026, Daniel: «nos falta colocar en la tab lo mismo que UTR VALUE, con los partidos que se envían al Discord»).

Un registro por partido y jugador candidato (clave `id FTR:jugador`), con la cuota y la probabilidad de la PRIMERA vez que se
envió a Discord. Se llena desde GET /ft-intel/probabilidad/candidatos (el mismo que lee FT Intelligence cada minuto) y se cierra
solo contra match_log, así que no hace falta ninguna tarea nueva.

«ENVIADO A DISCORD» (discord_sent=1): en el momento de registrar, Admin → Discord tenía encendido el texto o la voz del tipo
de esa etapa (PROB_CANDIDATO / PROB_CANDIDATO_INICIO) y la pausa general apagada. Es lo que se le ordenó a FT Discord; el acuse
real de FT Discord todavía no vuelve a RankingFTR para este tipo.

Resultado: 'W' ganó el candidato, 'L' perdió, 'P' retiro, walkover o marcador truncado (no cuenta: ni gana ni pierde).
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Optional

_TABLA = False
_ULTIMO_CIERRE = 0.0
CIERRE_CADA_S = 600
MARGEN_MIN_TRAS_INICIO = 30


def ensure_tabla(conn) -> None:
    global _TABLA
    if _TABLA:
        return
    conn.execute("""CREATE TABLE IF NOT EXISTS ft_prob_candidatos_registro (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        clave            TEXT NOT NULL UNIQUE,
        id_ftr           TEXT,
        jugador          TEXT NOT NULL,
        rival            TEXT NOT NULL,
        jugador1         TEXT,
        jugador2         TEXT,
        torneo           TEXT,
        superficie       TEXT,
        genero           TEXT,
        inicio_utc       TEXT,
        etapa            TEXT,
        prob             REAL,
        cuota            REAL,
        primera_vista_en TEXT NOT NULL,
        enviado_en       TEXT,
        discord_sent     INTEGER NOT NULL DEFAULT 0,
        result           TEXT,
        resultado_score  TEXT,
        resuelto_en      TEXT)""")
    conn.commit()
    _TABLA = True


def _norm(n) -> str:
    from shared.tennis_names import normalize_name
    return normalize_name(n or "")


def _genero_de(conn, nombre: str) -> Optional[str]:
    """Género del jugador si se identifica sin ambigüedad en `players` (para el filtro del dashboard)."""
    try:
        g = {(r[0] or "").strip().upper()[:1] for r in
             conn.execute("SELECT genero FROM players WHERE nombre_norm=?", (_norm(nombre),)).fetchall()}
    except Exception:
        return None
    g.discard("")
    return g.pop() if len(g) == 1 else None


def enviado_a_discord(conn, etapa: str) -> bool:
    """¿Admin → Discord tiene encendido el texto o la voz de esta etapa, y no hay pausa general?"""
    from production.engines import discord_config
    lst = discord_config.listar(conn)
    if lst.get("pausa"):
        return False
    tipo = "PROB_CANDIDATO_INICIO" if etapa == "INICIO" else "PROB_CANDIDATO"
    t = next((x for x in lst["tipos"] if x["tipo"] == tipo), None)
    return bool(t and (t["texto"] or t["voz"]))


def registrar(conn, candidatos: list, ahora: Optional[datetime] = None) -> int:
    """Registra los candidatos que se están enviando. Devuelve cuántas filas nuevas. No repite: una por partido y jugador."""
    from production.api.deps import ejecutar_con_reintento
    ensure_tabla(conn)
    ahora = ahora or datetime.now(timezone.utc)
    iso = ahora.isoformat()
    filas = []
    for c in candidatos or []:
        if not c.get("id_ftr") or not c.get("jugador"):
            continue
        env = enviado_a_discord(conn, c.get("etapa") or "DETECTADO")
        filas.append((f"{c['id_ftr']}:{_norm(c['jugador'])}", str(c["id_ftr"]), c["jugador"], c.get("rival") or "",
                      c.get("jugador1"), c.get("jugador2"), c.get("torneo"), c.get("superficie"), c.get("inicio_utc"),
                      c.get("etapa"), c.get("prob"), c.get("cuota"), iso, iso if env else None, 1 if env else 0,
                      _genero_de(conn, c["jugador"])))
    if not filas:
        return 0
    nuevas = [0]

    def _w():
        for f in filas:
            cur = conn.execute(
                "INSERT OR IGNORE INTO ft_prob_candidatos_registro (clave, id_ftr, jugador, rival, jugador1, jugador2, torneo, "
                "superficie, inicio_utc, etapa, prob, cuota, primera_vista_en, enviado_en, discord_sent, genero) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", f)
            if cur.rowcount:
                nuevas[0] += 1
            elif f[14]:
                # Ya estaba registrado SIN enviar (Discord apagado entonces) y ahora sí se envía: queda con la cuota,
                # la probabilidad y la etapa de este primer envío real.
                conn.execute("UPDATE ft_prob_candidatos_registro SET discord_sent=1, enviado_en=?, etapa=?, prob=?, cuota=? "
                             "WHERE clave=? AND discord_sent=0 AND result IS NULL", (iso, f[9], f[10], f[11], f[0]))
        conn.commit()
    ejecutar_con_reintento(_w, conn=conn, con_lock=True)
    return nuevas[0]


def _ids_jugador(conn, nombre: str) -> list:
    return [r[0] for r in conn.execute("SELECT player_id FROM players WHERE nombre_norm=?", (_norm(nombre),)).fetchall()]


def cerrar_pendientes(conn, ahora: Optional[datetime] = None, dias: int = 10, tope: int = 500, forzar: bool = False) -> dict:
    """Cierra contra match_log los candidatos ya jugados. Como mucho una vez cada 10 min por proceso (se llama desde la ruta
    que consulta FT Intelligence cada minuto). Un partido solo se cierra con pareja de jugadores ÚNICA en match_log."""
    global _ULTIMO_CIERRE
    from production.api.deps import ejecutar_con_reintento
    from shared.retiros import es_no_disputado_por_score, es_retiro_por_score, score_truncado
    if not forzar and time.monotonic() - _ULTIMO_CIERRE < CIERRE_CADA_S:
        return {"omitido": True}
    _ULTIMO_CIERRE = time.monotonic()
    ensure_tabla(conn)
    ahora = ahora or datetime.now(timezone.utc)
    pend = conn.execute(
        "SELECT id, jugador, rival, inicio_utc FROM ft_prob_candidatos_registro WHERE result IS NULL AND inicio_utc IS NOT NULL "
        "AND primera_vista_en >= ? ORDER BY id LIMIT ?", ((ahora - timedelta(days=dias)).isoformat(), tope)).fetchall()
    cierres = []
    for f in pend:
        try:
            ini = datetime.fromisoformat(str(f["inicio_utc"]).replace("Z", "+00:00"))
        except ValueError:
            continue
        if ahora < ini + timedelta(minutes=MARGEN_MIN_TRAS_INICIO):
            continue
        a, b = _ids_jugador(conn, f["jugador"]), _ids_jugador(conn, f["rival"])
        if len(a) != 1 or len(b) != 1:
            continue                                   # sin identidad única no se adivina
        p = None
        for pid, oid, invertir in ((a[0], b[0], False), (b[0], a[0], True)):
            # Se busca desde la perspectiva de cada jugador: si match_log solo guarda la fila del que ganó, la del rival
            # (invertida) es la que existe.
            p = conn.execute(
                "SELECT score, sets_ganados, sets_perdidos FROM match_log WHERE player_id=? AND opponent_id=? AND "
                "superficie='general' AND substr(fecha,1,10) >= date(substr(?,1,10),'-1 day') AND "
                "substr(fecha,1,10) <= date(substr(?,1,10),'+1 day') ORDER BY fecha ASC LIMIT 1",
                (pid, oid, f["inicio_utc"], f["inicio_utc"])).fetchone()
            if p:
                break
        if not p:
            continue
        score = p["score"] or ""
        if (not score or es_retiro_por_score(score) or es_no_disputado_por_score(score) or score_truncado(score)):
            res = "P"
        else:
            gano = (p["sets_ganados"] or 0) > (p["sets_perdidos"] or 0)
            res = "W" if gano != invertir else "L"
        cierres.append((res, score, ahora.isoformat(), f["id"]))
    if cierres:
        def _w():
            conn.executemany("UPDATE ft_prob_candidatos_registro SET result=?, resultado_score=?, resuelto_en=? "
                             "WHERE id=? AND result IS NULL", cierres)
            conn.commit()
        ejecutar_con_reintento(_w, conn=conn, con_lock=True)
    return {"pendientes": len(pend), "cerradas": len(cierres)}
