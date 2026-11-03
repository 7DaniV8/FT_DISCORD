"""
production/engines/dashboard_estrategia.py — Dashboard "estilo Historial de
Picks" para 🎓 Partidos Maestros, 🚨 Cuota Mal Puesta, 📈 UTR VALUE, ▼ Under
en Vivo y 🚨 UTR Market Anomaly (08/10/2026, Rubén).

Mismas tarjetas y MISMAS fórmulas que Picks Pre-partido / Picks en Vivo
(production/api/picks.py): apuesta fija de 1 unidad, ganado = cuota − 1,
perdido = −1, % de acierto sobre las decididas, ROI = Yield = unidades ÷
arriesgado, rachas en orden cronológico. No se reescribe ninguna cuenta: se
traduce cada fila de la estrategia a un "pick" (selección, cuota, resultado)
y se reusan beneficio_unidades() y _rachas() de picks.py.

Qué cuenta como pick en cada estrategia (lo que la estrategia efectivamente
recomienda, a la cuota con la que lo recomienda):
  - Partidos Maestros: el favorito, a la cuota de apertura que lo hizo
    calificar. Por defecto la regla congelada vigente.
  - Cuota Mal Puesta: el candidato, a la cuota del momento de la detección
    (la misma de sus unidades). Por defecto las señales que salieron
    (📣 Discord), con selector para ver 🔥/💎/🟡.
  - UTR VALUE: el jugador de UTR superior, a la cuota de evaluación (la de
    candidato si lo fue). Por defecto los aprobados (📣 Discord), con
    selector para candidatos y estudio.
Solo lectura. Nada de esto cambia una regla ni un umbral.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Optional

from production.api.picks import (GANADO, PENDIENTE, PERDIDO, PUSH, _rachas,
                                  beneficio_unidades)


def _r(v, n=2):
    return None if v is None else round(v, n)


# 08/10/2026 (Rubén: "intento filtrar por mujeres y no cambia nada"): el género
# llega guardado de varias formas ('F', 'f', 'Femenino', 'women'...). Se compara
# por la inicial en mayúscula, y 'W' (women) cuenta como 'F'.
_GENERO_SQL = "(UPPER(SUBSTR(COALESCE(genero,''),1,1)) = ? OR (? = 'F' AND UPPER(SUBSTR(COALESCE(genero,''),1,1)) = 'W'))"


def _g(genero: str) -> str:
    g = (genero or "").strip().upper()[:1]
    return "F" if g == "W" else g


def kpis(picks: list[dict], valor_unidad: float = 1.0) -> dict:
    """picks: [{resultado: ganado|perdido|push|pendiente, cuota}], del más viejo
    al más nuevo. Mismas claves que picks.calcular_kpis (así el front reusa
    las tarjetas tal cual)."""
    res = [p["resultado"] for p in picks]
    g = res.count(GANADO)
    pe = res.count(PERDIDO)
    pu = res.count(PUSH)
    pend = res.count(PENDIENTE)
    liquidadas = g + pe + pu
    unidades, cuotas, cuotas_g = 0.0, [], []
    for p in picks:
        if p["resultado"] not in (GANADO, PERDIDO, PUSH):
            continue
        c = p.get("cuota")
        unidades += beneficio_unidades(p["resultado"], c)
        if c:
            cuotas.append(c)
            if p["resultado"] == GANADO:
                cuotas_g.append(c)
    dec = g + pe
    rend = (unidades / liquidadas * 100) if liquidadas else None
    mg, mp = _rachas(res)
    return {"total": len(picks), "ganadas": g, "perdidas": pe, "push": pu, "pendientes": pend,
            "indeterminados": 0, "liquidadas": liquidadas,
            "hit_rate": _r(g / dec * 100 if dec else None), "unidades": _r(unidades),
            "roi": _r(rend), "yield": _r(rend),
            "cuota_promedio": _r(sum(cuotas) / len(cuotas)) if cuotas else None,
            "cuota_promedio_ganadas": _r(sum(cuotas_g) / len(cuotas_g)) if cuotas_g else None,
            "racha_victorias": mg, "racha_derrotas": mp,
            "valor_unidad": _r(valor_unidad), "ganancia_dinero": _r(unidades * valor_unidad),
            "arriesgado_dinero": _r(liquidadas * valor_unidad)}


# ── 🗑 Filas ocultas (10/10/2026, Rubén: "colócame una opción de eliminar filas") ──
# No se borra nada: la fila queda en su tabla; solo deja de verse en el
# dashboard (y en sus KPI). Reversible con restaurar(). Solo admin.
OCULTABLES = ("maestros", "cmp", "utrv", "utra", "utrx", "under", "news", "recordador", "implicacion", "mov",
              "underutr",   # v54i: 🔥 UNDER UTR
              "ftprob")     # 30/10/2026: 🎲 FT PROBABILIDAD (candidatos enviados a Discord)


def _ensure_ocultas(conn) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS dashboard_filas_ocultas (estrategia TEXT NOT NULL, fila_id INTEGER NOT NULL, "
                 "hecho_por TEXT, hecho_en TEXT NOT NULL, PRIMARY KEY (estrategia, fila_id))")


def ocultas(conn, clave: str) -> set:
    _ensure_ocultas(conn)
    return {r[0] for r in conn.execute("SELECT fila_id FROM dashboard_filas_ocultas WHERE estrategia=?", (clave,)).fetchall()}


def ocultar(conn, clave: str, fila_id: int, por: str, restaurar: bool = False) -> dict:
    from datetime import datetime, timezone
    if clave not in OCULTABLES:
        raise ValueError(f"No se puede ocultar en '{clave}'")
    _ensure_ocultas(conn)
    if restaurar:
        n = conn.execute("DELETE FROM dashboard_filas_ocultas WHERE estrategia=? AND fila_id=?", (clave, int(fila_id))).rowcount
    else:
        n = conn.execute("INSERT OR IGNORE INTO dashboard_filas_ocultas (estrategia, fila_id, hecho_por, hecho_en) VALUES (?,?,?,?)",
                         (clave, int(fila_id), por, datetime.now(timezone.utc).isoformat())).rowcount
    conn.commit()
    return {"ok": True, "estrategia": clave, "id": int(fila_id), "restaurada" if restaurar else "oculta": bool(n),
            "ocultas": len(ocultas(conn, clave))}


def sin_ocultas(conn, clave: str, picks: list[dict]) -> list[dict]:
    oc = ocultas(conn, clave)
    return [p for p in picks if p.get("id") not in oc] if oc else picks


def _con_investigacion(conn, clave: str, picks: list[dict]) -> list[dict]:
    """v54 (13/10/2026, Rubén): la columna 🧠 Investigación de Partidos Maestros, Cuota Mal Puesta y UTR VALUE
    (y UTR Market Anomaly, que vive en la misma pestaña). Solo agrega `inv`; no cambia ninguna cuenta."""
    try:
        from production.engines import estrategia_investigacion
        return estrategia_investigacion.adjuntar(conn, clave, picks)
    except Exception:
        return picks


def _armar(picks: list[dict], valor_unidad: float, limite: int) -> dict:
    """picks llegan del más nuevo al más viejo (como se muestran)."""
    cron = list(reversed(picks))
    for p in picks:
        p["unidades"] = (None if p["resultado"] == PENDIENTE
                         else _r(beneficio_unidades(p["resultado"], p.get("cuota"))))
    return {"kpis": kpis(cron, valor_unidad), "historial": picks[:limite],
            "truncado": len(picks) > limite}


def _resultado_wl(result: Optional[str], retiro=None) -> str:
    if result == "W":
        return GANADO
    if result == "L":
        return PERDIDO
    return PENDIENTE


def _en_dos_sets(marcador: Optional[str]) -> Optional[bool]:
    """¿El partido se decidió en dos sets (2-0)? None si el marcador no se puede leer.
    Cuenta los sets por los parciales 'x-y'; un retiro ('RET', 'W/O') no es 2-0."""
    import re
    if not marcador:
        return None
    m = str(marcador).upper()
    if any(x in m for x in ("RET", "W/O", "WO", "DEF", "ABD")):
        return False
    sets = re.findall(r"\b\d+-\d+", m)
    if len(sets) < 2:
        return None
    return len(sets) == 2


def _ganador_perdio_set1(marcador: Optional[str]) -> Optional[bool]:
    """¿El que GANÓ el partido había perdido el primer set? (13/10/2026, Rubén: "% de
    partidos que empezaron perdiendo y remontaron"). No depende del orden del marcador:
    el lado que ganó más sets es el ganador; se mira si ese lado ganó o perdió el 1.er
    set. None si no se puede leer (sin marcador, menos de 2 sets, empate de sets) o si
    hubo retiro / W.O. (un retiro no es una remontada jugada)."""
    import re
    if not marcador:
        return None
    m = str(marcador).upper()
    if any(x in m for x in ("RET", "W/O", "WO", "DEF", "ABD")):
        return None
    sets = [(int(a), int(b)) for a, b in re.findall(r"\b(\d+)-(\d+)", re.sub(r"\([^)]*\)", "", m))]
    if len(sets) < 2 or sets[0][0] == sets[0][1]:
        return None
    sa, sb = sum(1 for a, b in sets if a > b), sum(1 for a, b in sets if b > a)
    if sa == sb:
        return None
    return (sets[0][0] > sets[0][1]) != (sa > sb)


def _remontada_pick(resultado: str, marcador: Optional[str]) -> Optional[str]:
    """Desde el lado del pick: 'remonto' (perdió el 1.er set y ganó), 'no_remonto'
    (perdió el 1.er set y perdió), 'gano_set1' o None si no se puede saber."""
    g = _ganador_perdio_set1(marcador)
    if g is None or resultado not in (GANADO, PERDIDO):
        return None
    if resultado == GANADO:
        return "remonto" if g else "gano_set1"
    return "gano_set1" if g else "no_remonto"   # perdió: el rival (ganador) perdió el set 1 → el pick lo había ganado


# ── 🎓 Partidos Maestros ─────────────────────────────────────────────

MAESTROS_VISTAS = {"vigente": "⭐ Regla vigente", "top": "🔥 Maestro TOP", "todas": "Todas (también la regla anterior)"}


def partidos_maestros(conn, vista: str = "vigente", genero: str = "", valor_unidad: float = 1.0,
                      limite: int = 500) -> dict:
    from production.engines.partidos_maestros import REGLA_VERSION, _ensure_tabla
    from shared.tennis_names import normalize_name
    _ensure_tabla(conn)
    where, args = [], []
    if vista in ("vigente", "top"):
        where.append("regla_version=?")
        args.append(REGLA_VERSION)
    if vista == "top":
        # 08/10/2026: por si la insignia quedó guardada con otra grafía ('TOP', 'MAESTRO_TOP'...).
        where.append("UPPER(COALESCE(insignia,'')) LIKE '%TOP%'")
    if genero:
        where.append(_GENERO_SQL)
        args += [_g(genero), _g(genero)]
    sql = ("SELECT fixture_id, fecha, torneo, genero, jugador1, jugador2, favorito_nombre, cuota_favorito, "
           "ganador_final, resultado_score, insignia, creado_en FROM partidos_maestros"
           + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY fecha DESC, creado_en DESC")
    picks = []
    for r in conn.execute(sql, args).fetchall():
        fav = r["favorito_nombre"]
        rival = r["jugador2"] if fav == r["jugador1"] else r["jugador1"]
        if r["ganador_final"]:
            res = GANADO if normalize_name(r["ganador_final"]) == normalize_name(fav or "") else PERDIDO
        else:
            res = PENDIENTE
        picks.append({"id": r["fixture_id"], "fecha": r["fecha"] or r["creado_en"],
                      "partido": f"{fav} vs {rival}", "seleccion": fav,
                      "etiqueta": "🔥 TOP" if "TOP" in (r["insignia"] or "").upper() else ("⭐" if r["insignia"] else ""),
                      "torneo": r["torneo"], "cuota": r["cuota_favorito"], "resultado": res,
                      "marcador": r["resultado_score"], "lado_a": fav, "lado_b": rival})
    picks = sin_ocultas(conn, "maestros", picks)
    picks = _con_investigacion(conn, "maestros", picks)          # v54: columna 🧠 Investigación
    out = {**_armar(picks, valor_unidad, limite), "vista": vista, "vistas": MAESTROS_VISTAS,
           "regla": REGLA_VERSION}
    # 08/10/2026 (Rubén): la pestaña muestra solo % de acierto y % de victoria en dos
    # sets (sobre las ganadas con marcador legible).
    ganadas = [p for p in picks if p["resultado"] == GANADO]
    legibles = [p for p in ganadas if _en_dos_sets(p.get("marcador")) is not None]
    dos = sum(1 for p in legibles if _en_dos_sets(p.get("marcador")))
    out["kpis"]["dos_sets"] = dos
    out["kpis"]["dos_sets_base"] = len(legibles)
    out["kpis"]["pct_dos_sets"] = _r(dos / len(legibles) * 100) if legibles else None
    # 13/10/2026 (Rubén): % de los que empezaron perdiendo (perdieron el 1.er set) y remontaron.
    rem = [_remontada_pick(p["resultado"], p.get("marcador")) for p in picks]
    empezaron = sum(1 for x in rem if x in ("remonto", "no_remonto"))
    remontaron = sum(1 for x in rem if x == "remonto")
    out["kpis"]["empezaron_perdiendo"] = empezaron
    out["kpis"]["remontaron"] = remontaron
    out["kpis"]["pct_remontada"] = _r(remontaron / empezaron * 100) if empezaron else None
    out["kpis"]["remontadas_sobre_ganadas"] = _r(remontaron / len(legibles) * 100) if legibles else None
    for p, x in zip(picks, rem):
        p["remonto"] = True if x == "remonto" else (False if x == "no_remonto" else None)
    return out


# ── 🚨 Cuota Mal Puesta ──────────────────────────────────────────────

CMP_VISTAS = {"discord": "📣 Publicadas (Discord)", "cmp_premium": "🔥 + 💎 (pasaron la puerta)",
              "premium": "💎 Premium", "estudio": "🟡 En estudio", "todas": "Todas las señales (sin descartes)"}


def cuota_mal_puesta(conn, vista: str = "discord", genero: str = "", valor_unidad: float = 1.0,
                     limite: int = 500) -> dict:
    from production.engines.cuota_mal_puesta import (CMP, DESCARTE, ESTUDIO, PREMIUM, VERSION_MOTOR,
                                                     _ensure_tabla)
    _ensure_tabla(conn)
    where, args = [], []
    cols0 = {c[1] for c in conn.execute("PRAGMA table_info(cmp_senales)").fetchall()}
    if vista == "discord":
        # 10/10/2026 (Rubén: "las alertas enviadas nunca se borran de sus vistas"):
        # lo que FT Intelligence confirmó dicho (dicho_discord=1) cuenta SIEMPRE,
        # sea de la versión del motor que sea y pase lo que pase después; lo
        # publicado y todavía no anunciado cuenta mientras no se retire antes
        # de Discord. Sin filtro por version_motor acá (antes v1/v2 se perdían).
        # 10/10/2026 16:06 (Rubén: "salen muchas más de las que se enviaron"): SOLO lo que
        # FT Intelligence confirmó que anunció (dicho_discord=1). Lo publicado y
        # todavía no anunciado (espera al inicio del partido) no se cuenta hasta
        # que suene; si RankingFTR no tuviera la marca (versión vieja), cae a
        # discord_sent=1 sin retirar.
        if "dicho_discord" in cols0:
            where.append("dicho_discord=1")
        else:                                            # pragma: no cover
            rev = " AND discord_revocado_en IS NULL" if "discord_revocado_en" in cols0 else ""
            where.append(f"(discord_sent=1{rev})")
    else:
        where.append("version_motor=?")
        args.append(VERSION_MOTOR)
    if vista == "cmp_premium":
        where.append("grupo IN (?, ?)")
        args += [CMP, PREMIUM]
    elif vista == "premium":
        where.append("grupo=?")
        args.append(PREMIUM)
    elif vista == "estudio":
        where.append("grupo=?")
        args.append(ESTUDIO)
    else:
        where.append("grupo<>?")
        args.append(DESCARTE)
    if genero:
        where.append(_GENERO_SQL)
        args += [_g(genero), _g(genero)]
    ico = {PREMIUM: "💎", CMP: "🔥", ESTUDIO: "🟡"}
    cols = cols0
    extra = [c for c in ("ft_prob_base", "elo_prob", "ftr_prob", "market_prob", "mercado_medida", "ajuste_oposicion_pp", "ajuste_carga_pp",
                         "ajuste_mto_pp", "elo_ftr_alineados", "factores_positivos", "aprobado_publicacion",
                         "ft_final_publicacion", "edge_publicacion", "cuota_publicacion") if c in cols]
    picks = []
    for r in conn.execute(
            "SELECT id, timestamp_signal, fecha_partido, torneo, player_a, player_b, odds_a, grupo, result, "
            "resultado_score, ft_prob_ajustada, mispricing_pp" + "".join(", " + c for c in extra)
            + " FROM cmp_senales WHERE " + " AND ".join(where) + " ORDER BY timestamp_signal DESC", args).fetchall():
        picks.append({"id": r["id"], "fecha": r["timestamp_signal"],
                      "partido": f"{r['player_a']} vs {r['player_b']}", "seleccion": r["player_a"],
                      "etiqueta": ico.get(r["grupo"], ""), "torneo": r["torneo"], "cuota": r["odds_a"],
                      "resultado": _resultado_wl(r["result"]), "marcador": r["resultado_score"],
                      "detalle": _detalle_cmp(r, extra), "lado_a": r["player_a"], "lado_b": r["player_b"]})
    picks = sin_ocultas(conn, "cmp", picks)
    picks = _con_investigacion(conn, "cmp", picks)          # v54: columna 🧠 Investigación
    return {**_armar(picks, valor_unidad, limite), "vista": vista, "vistas": CMP_VISTAS,
            "version_motor": VERSION_MOTOR}


def _detalle_cmp(r, extra: list) -> Optional[str]:
    """(09/10/2026, Rubén: "¿cómo sé que están ahí porque deberían estar?")
    La fila dice POR QUÉ pasó la puerta: FT final y de dónde sale (base
    Elo/FTR + factores con signo), la ventaja, qué factor la activó, si Elo y
    FTR coinciden y, si ya se revalidó antes de publicar (FOTO 2), con qué
    números salió."""
    if r["ft_prob_ajustada"] is None or r["mispricing_pp"] is None:
        return None
    g = (lambda k: r[k] if k in extra else None)
    partes = [f"FT {r['ft_prob_ajustada'] * 100:.0f}%"]
    base = g("ft_prob_base")
    ajustes = []
    for k, nombre in (("ajuste_oposicion_pp", "oposición"), ("ajuste_carga_pp", "carga"), ("ajuste_mto_pp", "MTO")):
        v = g(k)
        if v:
            ajustes.append(f"{nombre} {v:+.0f}")
    if base is not None:
        partes[0] += f" (base {base * 100:.0f}%" + (" " + ", ".join(ajustes) if ajustes else "") + ")"
    sm = (g("mercado_medida") or "") == "sin_margen"         # V3: misma medida que Discord y la página
    partes.append(f"+{r['mispricing_pp']:.1f} pp" + (f" vs mercado{' sin margen' if sm else ''} {g('market_prob') * 100:.1f}%"
                                                      if g("market_prob") else ""))
    fav = [n for k, n in (("ajuste_oposicion_pp", "OPOSICIÓN"), ("ajuste_carga_pp", "CARGA"), ("ajuste_mto_pp", "MTO"))
           if (g(k) or 0) > 0]
    if "factores_positivos" in extra or fav:
        partes.append("activó: " + (" + ".join(fav) if fav else "ninguno"))
    if g("elo_ftr_alineados") is not None:
        partes.append("Elo/FTR coinciden" if g("elo_ftr_alineados") else "Elo/FTR NO coinciden")
    if g("aprobado_publicacion") is not None:
        if g("aprobado_publicacion"):
            partes.append(f"revalidada ✓ (al publicar: FT {g('ft_final_publicacion') * 100:.0f}% · +{g('edge_publicacion'):.1f} pp @ {g('cuota_publicacion'):.2f})"
                          if g("ft_final_publicacion") is not None and g("edge_publicacion") is not None and g("cuota_publicacion")
                          else "revalidada ✓")
        else:
            partes.append("descartada en revalidación")
    return " · ".join(partes)


# ── 📈 UTR VALUE ─────────────────────────────────────────────────────

UTRV_VISTAS = {"discord": "📣 Aprobados (Discord)", "candidatos": "Candidatos (ΔUTR ≥ 0.75 y cuota ≥ 1.70)",
               "estudio": "Todo con UTR (estudio)"}


def _urls_utr(conn, r) -> dict:
    """(17/10/2026, Daniel: «que cuando le dé clic a un jugador me envíe al perfil UTR, como en Próximos
    partidos») Link al perfil UTR de cada lado: el UTR ID que se usó en la fila; si no quedó guardado, el del
    padrón SOLO si la identidad está confirmada (MATCH). Sin UTR ID → sin link (nunca se adivina)."""
    from production.engines.utr_padron import url_perfil
    out = {}
    for lado in ("a", "b"):
        uid = r[f"utr_player_id_{lado}"]
        if not uid and r[f"player_{lado}_id"]:
            try:
                f = conn.execute("SELECT utr_player_id FROM utr_perfiles WHERE player_id=? AND "
                                 "estado IN ('MATCH','MATCH_VARIANTE') AND identidad_conflicto IS NULL",
                                 (r[f"player_{lado}_id"],)).fetchone()
                uid = f[0] if f else None
            except Exception:                          # padrón todavía sin crear
                uid = None
        if uid:
            out[f"url_{lado}"] = url_perfil(uid)
    return out


def utr_value(conn, vista: str = "discord", genero: str = "", valor_unidad: float = 1.0,
              limite: int = 500) -> dict:
    from production.engines.utr_value import G_DISCORD, G_SIN_UTR, VERSION_MOTOR, _ensure_tabla
    _ensure_tabla(conn)
    where, args = ["grupo<>?"], [G_SIN_UTR]
    if vista == "discord":
        # 10/10/2026: lo enviado a Discord, de cualquier versión del motor, para siempre.
        where.append("discord_sent=1")
    else:
        where.append("version_motor=?")
        args.append(VERSION_MOTOR)
    if vista == "candidatos":
        where.append("es_candidato=1")
    if genero:
        where.append(_GENERO_SQL)
        args += [_g(genero), _g(genero)]
    picks = []
    for r in conn.execute(
            "SELECT id, primera_vista_en, fecha_partido, torneo, jugador_a, jugador_b, es_candidato, odds_a, "
            "odds_a_cand, cuota_evaluacion, result, resultado_score, delta_utr, delta_min, delta_max, "
            "fuente_utr, utr_player_id_a, utr_player_id_b, player_a_id, player_b_id FROM utr_value_registro WHERE "
            + " AND ".join(where)
            + " ORDER BY primera_vista_en DESC", args).fetchall():
        cuota = r["cuota_evaluacion"] or (r["odds_a_cand"] if r["es_candidato"] else r["odds_a"])
        if r["delta_utr"] is not None and (r["fuente_utr"] or "EXACTO") != "INTERVALO":
            d = f"ΔUTR +{r['delta_utr']:.2f}"
        elif r["delta_min"] is not None:
            d = f"ΔUTR +{r['delta_min']:.2f} … +{r['delta_max']:.2f}"
        else:
            d = None
        picks.append({"id": r["id"], "fecha": r["primera_vista_en"],
                      "partido": f"{r['jugador_a']} vs {r['jugador_b']}", "seleccion": r["jugador_a"],
                      "etiqueta": "", "torneo": r["torneo"], "cuota": cuota,
                      "resultado": _resultado_wl(r["result"]), "marcador": r["resultado_score"], "detalle": d,
                      "lado_a": r["jugador_a"], "lado_b": r["jugador_b"], **_urls_utr(conn, r)})
    if vista == "discord":
        picks = _mezclar(picks, externos_como(conn, "UTR_VALUE", genero))
    picks = sin_ocultas(conn, "utrv", picks)
    picks = _con_investigacion(conn, "utrv", picks)          # v54: columna 🧠 Investigación
    return {**_armar(picks, valor_unidad, limite), "vista": vista, "vistas": UTRV_VISTAS,
            "version_motor": VERSION_MOTOR}


# ── 🎲 FT PROBABILIDAD (30/10/2026, Daniel: «nos falta colocar en la tab lo mismo que UTR VALUE, con los partidos
#    que se envían al Discord») ──
FTPROB_VISTAS = {"discord": "📣 Candidatos enviados a Discord"}


def ft_probabilidad(conn, vista: str = "discord", genero: str = "", valor_unidad: float = 1.0,
                    limite: int = 500) -> dict:
    """Una fila por partido y jugador candidato enviado a Discord (ft_prob_candidatos_registro). Selección = el
    candidato; cuota = la de la primera vez que se envió; resultado = cierre contra match_log (retiro/W.O. = push)."""
    from production.engines import ft_prob_registro as reg
    reg.ensure_tabla(conn)
    where, args = ["discord_sent=1"], []
    if genero:
        where.append(_GENERO_SQL)
        args += [_g(genero), _g(genero)]
    picks = []
    for r in conn.execute(
            "SELECT id, enviado_en, primera_vista_en, torneo, jugador, rival, jugador1, jugador2, etapa, prob, cuota, "
            "result, resultado_score FROM ft_prob_candidatos_registro WHERE " + " AND ".join(where)
            + " ORDER BY COALESCE(enviado_en, primera_vista_en) DESC", args).fetchall():
        res = PUSH if r["result"] == "P" else _resultado_wl(r["result"])
        prob = f"{round((r['prob'] or 0) * 100)} %"
        picks.append({"id": r["id"], "fecha": r["enviado_en"] or r["primera_vista_en"],
                      "partido": f"{r['jugador1'] or r['jugador']} vs {r['jugador2'] or r['rival']}",
                      "seleccion": r["jugador"], "etiqueta": f"({prob})", "torneo": r["torneo"], "cuota": r["cuota"],
                      "resultado": res, "marcador": r["resultado_score"],
                      "detalle": f"Modelo {prob} · " + ("al empezar" if r["etapa"] == "INICIO" else "detectado"),
                      "lado_a": r["jugador"], "lado_b": r["rival"]})
    picks = sin_ocultas(conn, "ftprob", picks)
    return {**_armar(picks, valor_unidad, limite), "vista": "discord", "vistas": FTPROB_VISTAS}


# ── 🩹 Recordador IA (11/10/2026 v43d, Rubén: "que salga el dashboard en alertas que
#    pasen el 60 % de probabilidad, que lo sume en un historial propio de la tab") ──
RECORDADOR_VISTAS = {"discord": "📣 Dichas en Discord con favorito ≥ el mínimo"}


def recordador(conn, vista: str = "discord", genero: str = "", valor_unidad: float = 1.0,
               limite: int = 500, prob_min: float = 60.0) -> dict:
    """Una fila por PREDICCIÓN (versión vigente) cuyo favorito FullTennis llega a
    `prob_min` % y cuyo aviso se DIJO en Discord (dicho_discord=1). La selección es
    el favorito; la cuota, la suya en el fixture (si había); el resultado, el
    cierre de recordador_resultados (W.O. = push). Nunca se borra: 🗑 solo oculta."""
    from production.engines import recordador as rec_mod
    from production.engines.recordador_ia import es_sin_nivel
    rec_mod.ensure_tablas(conn)
    rec_mod._asegurar_nota(conn)
    where, args = ["a.dicho_discord=1", "a.prediccion_id IS NOT NULL", "a.recordatorio_id>0"], []
    if genero:
        where.append("(UPPER(SUBSTR(COALESCE(i.genero,''),1,1)) = ? OR (? = 'F' AND UPPER(SUBSTR(COALESCE(i.genero,''),1,1)) = 'W'))")
        args += [_g(genero), _g(genero)]
    filas = conn.execute(
        "SELECT i.id, i.recordatorio_id, i.jugador, i.rival, i.torneo, i.inicio_programado, i.p_base_jugador, i.p_ajustada_jugador, "
        "i.cuota_jugador, i.cuota_rival, i.metodo_base, i.observacion_json, MIN(a.avisado_en) AS avisado_en, "
        "SUM(CASE WHEN a.momento='EN_JUEGO' THEN 1 ELSE 0 END) AS con_inicio, "
        "r.ganador, r.gano_jugador, r.marcador, r.retiro, r.w_o "
        "FROM recordador_avisos a JOIN recordador_investigaciones i ON i.id=a.prediccion_id "
        "LEFT JOIN recordador_resultados r ON r.investigacion_id=i.id "
        "WHERE " + " AND ".join(where) + " GROUP BY i.id ORDER BY MIN(a.avisado_en) DESC", args).fetchall()
    picks = []
    umbral = float(prob_min) / 100.0
    for f in filas:
        p = f["p_ajustada_jugador"] if f["p_ajustada_jugador"] is not None else f["p_base_jugador"]
        if p is None or es_sin_nivel(f["metodo_base"]):
            continue                    # v54c: sin Elo ni FTR no hay probabilidad FullTennis (la tabla UTR no cuenta)
        fav_j = p >= 0.5
        p_fav = p if fav_j else 1 - p
        if p_fav + 1e-9 < umbral:
            continue
        sel = f["jugador"] if fav_j else f["rival"]
        cuota = f["cuota_jugador"] if fav_j else f["cuota_rival"]
        if f["w_o"]:
            res = PUSH
        elif f["ganador"] is None:
            res = PENDIENTE
        else:
            res = GANADO if (bool(f["gano_jugador"]) == fav_j) else PERDIDO
        try:
            obs = json.loads(f["observacion_json"] or "null") or {}
        except ValueError:
            obs = {}
        det = f"{round(p_fav * 100)} % FullTennis"
        if obs.get("categoria") and obs["categoria"] != "nada":
            from production.engines.recordador_ia import CATEGORIA_TXT
            det += f" · 👀 {f['jugador']}: {CATEGORIA_TXT.get(obs['categoria'], obs['categoria'])}"
        picks.append({"id": f["id"], "fecha": f["avisado_en"], "partido": f"{f['jugador']} vs {f['rival']}",
                      "seleccion": sel, "etiqueta": f"({round(p_fav * 100)}%)", "torneo": f["torneo"], "cuota": cuota,
                      "resultado": res, "marcador": f["marcador"], "detalle": det, "recordatorio_id": f["recordatorio_id"],
                      "lado_a": sel, "lado_b": f["rival"] if fav_j else f["jugador"]})
    picks = sin_ocultas(conn, "recordador", picks)
    return {**_armar(picks, valor_unidad, limite), "vista": "discord", "vistas": RECORDADOR_VISTAS, "prob_min": prob_min}


# ── ▼ Under en Vivo (08/10/2026, Rubén: "que tenga su propio dashboard") ────

UNDER_VISTAS = {"enviadas": "📣 Alertas enviadas", "observacion": "👀 En observación (cuota 1.85–1.89 y dobles)",
                "todas": "Todas las que cumplieron la estrategia"}


def under_vivo(conn, vista: str = "enviadas", genero: str = "", valor_unidad: float = 1.0,
               limite: int = 500) -> dict:
    """Un pick = UNDER a la línea y a la cuota UNDER del aviso. Ganado si el
    partido cerró UNDER, perdido si OVER, push si cayó justo en la línea.
    Fuente: under_vivo_registro (permanente; cerrar_pendientes() le pone el
    resultado desde match_log)."""
    from production.engines.under_vivo_registro import (ALERTA_ENVIADA, OBS_CUOTA, OBS_DOBLES, _ensure_tabla)
    _ensure_tabla(conn)
    where, args = [], []
    if vista == "enviadas":
        where.append("estado=?")
        args.append(ALERTA_ENVIADA)
    elif vista == "observacion":
        where.append("estado IN (?, ?)")
        args += [OBS_CUOTA, OBS_DOBLES]
    else:
        where.append("estado IN (?, ?, ?)")
        args += [ALERTA_ENVIADA, OBS_CUOTA, OBS_DOBLES]
    if genero:
        where.append(_GENERO_SQL)
        args += [_g(genero), _g(genero)]
    picks = []
    for r in conn.execute(
            "SELECT id, creado_en, torneo, home, away, jugador_destacado, under_total, under_cuota, linea_txt, "
            "estado, modalidad, resultado_under, resultado_score, games_totales, utr_diff FROM under_vivo_registro "
            "WHERE " + " AND ".join(where) + " ORDER BY creado_en DESC", args).fetchall():
        ru = r["resultado_under"]
        res = GANADO if ru == "UNDER" else PERDIDO if ru == "OVER" else PUSH if ru == "PUSH" else PENDIENTE
        linea = r["linea_txt"] or (f"UNDER {r['under_total']:g}" if r["under_total"] is not None else "UNDER")
        picks.append({"id": r["id"], "fecha": r["creado_en"], "partido": f"{r['home']} vs {r['away']}",
                      "seleccion": linea, "etiqueta": "👀" if r["estado"] != ALERTA_ENVIADA else "",
                      "torneo": r["torneo"], "cuota": r["under_cuota"], "resultado": res,
                      "marcador": (r["resultado_score"] or "") + (f" · {r['games_totales']} games" if r["games_totales"] else ""),
                      "detalle": (f"ΔUTR {r['utr_diff']:+.2f} · {r['jugador_destacado']}" if r["utr_diff"] is not None
                                  else r["jugador_destacado"]), "lado_a": r["home"], "lado_b": r["away"], "marcador_crudo": r["resultado_score"]})
    # v54f (14/10/2026, Rubén: «los Under no suenan en Discord»): qué pasó con cada uno en la cadena hacia Discord
    try:
        from production.engines import under_discord
        lv = {r["id"]: r["live_value_id"] for r in conn.execute(
            "SELECT id, live_value_id FROM under_vivo_registro WHERE estado=? AND enviado=1", (ALERTA_ENVIADA,)).fetchall()}
        est = under_discord.por_live_value(conn, lv.values())
        for p in picks:
            if p["id"] in lv:
                d = est.get(lv[p["id"]])
                p["discord"] = d
                t = under_discord.texto_estado(d, True)
                if t:
                    p["detalle"] = ((p.get("detalle") or "") + " · " + t).strip(" ·")
    except Exception:                                    # la columna nunca tumba el dashboard
        pass
    picks = sin_ocultas(conn, "under", picks)
    return {**_armar(picks, valor_unidad, limite), "vista": vista, "vistas": UNDER_VISTAS}


# ── 📉 Movimiento Rentable (v54f, 14/10/2026, Rubén: «que los movimientos rentables que se envían a Telegram
# tengan su propia tab como las nuevas estrategias, con su dashboard y su historial con la columna de investigación»)

MOV_VISTAS = {"enviadas": "📣 Enviadas a Telegram", "todas": "Todas las confirmadas (también las no enviadas)"}


def _cuota_entrada(r) -> Optional[float]:
    """La cuota a la que se entra: la ACTUAL (post-movimiento), la misma del mensaje «Cuota: 2.12 ➔ 1.88 (ENTRADA)».
    Sale de pick_json (lo que se mandó) y, si no está, de cuota_away (donde el archivo guarda la actual)."""
    try:
        c = (json.loads(r["pick_json"] or "{}") or {}).get("cuota")
        if c:
            return float(c)
    except (TypeError, ValueError):
        pass
    try:
        return float(r["cuota_away"]) if r["cuota_away"] else None
    except (TypeError, ValueError):
        return None


def movimiento_rentable(conn, vista: str = "enviadas", genero: str = "", valor_unidad: float = 1.0,
                        limite: int = 500) -> dict:
    """Un pick = el jugador favorecido por el movimiento, a la cuota de ENTRADA (la actual). Fuente: valor_historial
    tipo='movimiento_rentable' (lo que se archivó al sonar); «enviadas» = enviado_picks=1, lo que salió a Telegram.
    El resultado, el mismo cierre de siempre (ganador_final / resultado_score)."""
    from production.api.picks import INDETERMINADO, clasificar
    try:
        cols = {c[1] for c in conn.execute("PRAGMA table_info(valor_historial)").fetchall()}
    except sqlite3.OperationalError:
        cols = set()
    if not cols:
        return {**_armar([], valor_unidad, limite), "vista": vista, "vistas": MOV_VISTAS}
    where, args = ["tipo='movimiento_rentable'"], []
    if vista != "todas" and "enviado_picks" in cols:
        where.append("enviado_picks=1")
    if genero:
        where.append(_GENERO_SQL)
        args += [_g(genero), _g(genero)]
    extra = ", ".join(c for c in ("caida_pct", "prob_ftr", "prob_elo", "probabilidad_favorito") if c in cols)
    picks, indeterminados = [], 0
    for r in conn.execute(
            "SELECT id, tipo, creado_en, liga, genero, home, away, favorito_nombre, cuota_home, cuota_away, pick_json, "
            "ganador_final, resultado_score" + (", " + extra if extra else "") + " FROM valor_historial WHERE "
            + " AND ".join(where) + " ORDER BY creado_en DESC", args).fetchall():
        cuota = _cuota_entrada(r)
        fila = dict(r)
        fila["tipo"] = "movimiento_rentable"
        res = clasificar(fila)
        marcador = (r["resultado_score"] or "") if r["ganador_final"] else None
        if res == INDETERMINADO:
            indeterminados += 1
            res = PENDIENTE                       # no se puede medir: no entra en la plata (como en Picks)
            marcador = f"sin determinar (ganador: {r['ganador_final']})" if r["ganador_final"] else marcador
        partes = []
        if r["cuota_home"] and cuota:
            partes.append(f"{r['cuota_home']:g} → {cuota:g}")
        if "caida_pct" in r.keys() and r["caida_pct"] is not None:
            partes.append(f"caída {r['caida_pct']:.1f} %")
        pf = r["probabilidad_favorito"] if "probabilidad_favorito" in r.keys() else None
        if pf is not None:
            partes.append(f"Prob FullTennis {pf * 100 if pf <= 1 else pf:.1f} %")
        picks.append({"id": r["id"], "fecha": r["creado_en"], "partido": f"{r['home']} vs {r['away']}",
                      "seleccion": r["favorito_nombre"] or r["home"], "etiqueta": "", "torneo": r["liga"], "cuota": cuota,
                      "resultado": res, "marcador": marcador,
                      "marcador_crudo": r["resultado_score"], "detalle": " · ".join(partes) or None,
                      "lado_a": r["home"], "lado_b": r["away"]})
    # v54h: si salió a Discord (lo informa FT Intelligence con el acuse de FT Discord)
    try:
        from production.engines import mov_discord
        enviados = {r[0] for r in conn.execute("SELECT id FROM valor_historial WHERE tipo='movimiento_rentable' AND enviado_picks=1")}
        est = mov_discord.por_ids(conn, [p["id"] for p in picks if p["id"] in enviados])
        for p in picks:
            if p["id"] in enviados:
                p["discord"] = est.get(p["id"])
                p["detalle"] = ((p.get("detalle") or "") + " · " + mov_discord.texto_estado(est.get(p["id"]))).strip(" ·")
    except Exception:                                    # la columna nunca tumba el dashboard
        pass
    picks = sin_ocultas(conn, "mov", picks)
    picks = _con_investigacion(conn, "mov", picks)
    out = {**_armar(picks, valor_unidad, limite), "vista": vista, "vistas": MOV_VISTAS}
    out["kpis"]["indeterminados"] = indeterminados
    return out


# ── 🚨 UTR Market Anomaly (08/10/2026) ──────────────────────────────────

UTRA_VISTAS = {"discord": "📣 Publicadas (Discord)", "activadas": "Activadas (cumplieron la anomalía)",
               "todas": "Todo lo observado con UTR"}


# ── v50d (13/10/2026, 14:45, Rubén: «cuando unimos el UTR MARKET ANOMALY se perdieron o no se muestran las
#    apuestas que venían del canal de Telegram»). El 13/10 (00:44) se pidió «unir UTR MARKET ANOMALY con UTR
#    externo… sigue funcionando pero no quiero que en la página se muestre un canal externo»: la sección
#    📡 UTR EXTERNO se quitó de la página, y con ella esas apuestas. Ahora las alertas del canal que salieron a
#    Discord con el formato de 🚨 UTR MARKET ANOMALY o 📈 UTR VALUE se cuentan en ESE dashboard, como una más
#    (sin marca de canal externo). Id NEGATIVO (−id de utr_externo_senales) para que 🗑 y ✏️ Cerrar sepan de
#    qué tabla son. Un solo mensaje por partido: si el partido ya salió por un motor propio, FT Intelligence no
#    dijo el externo, y acá tampoco se cuenta. ──

def _tokens_nombre(n: Optional[str]) -> set:
    from shared.tennis_names import normalize_name
    return {t for t in normalize_name(n or "").replace("-", " ").split() if len(t) >= 3}


def _mismo_par(x1: set, y1: set, x2: set, y2: set) -> bool:
    """Los dos jugadores comparten un apellido (≥ 3 letras) con los del otro par, en cualquier orden
    («Capurro Taborda M» = «Maria Capurro Taborda»)."""
    return bool((x1 & x2 and y1 & y2) or (x1 & y2 and y1 & x2))


def _fecha_dt(v):
    from datetime import datetime, timezone
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00").replace(" ", "T"))
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _propias_enviadas(conn) -> list:
    """(fecha, jugador A, jugador B) de lo que salió a Discord por los motores propios que tienen prioridad
    sobre el externo en FT Intelligence (UTR VALUE, Cuota Mal Puesta, UTR MARKET ANOMALY)."""
    out = []
    for sql in ("SELECT primera_vista_en, jugador_a, jugador_b FROM utr_value_registro WHERE discord_sent=1",
                "SELECT primera_vista_en, jugador_a, jugador_b FROM utr_anomalia_registro WHERE discord_sent=1",
                "SELECT timestamp_signal, player_a, player_b FROM cmp_senales WHERE discord_sent=1"):
        try:
            out += [(_fecha_dt(r[0]), _tokens_nombre(r[1]), _tokens_nombre(r[2])) for r in conn.execute(sql).fetchall()]
        except sqlite3.OperationalError:
            continue
    return out


def externos_como(conn, formato: str, genero: str = "") -> list[dict]:
    """Las alertas del canal (utr_externo_senales) que salieron a Discord con el formato `formato`
    ('UTR_MARKET_ANOMALY' | 'UTR_VALUE'), en filas del dashboard de esa estrategia."""
    from datetime import timedelta
    from production.engines import utr_externo
    try:
        utr_externo._ensure_tabla(conn)
    except sqlite3.OperationalError:
        return []
    where, args = ["discord_sent=1"], []
    if genero:
        where.append(_GENERO_SQL)
        args += [_g(genero), _g(genero)]
    filas = conn.execute("SELECT * FROM utr_externo_senales WHERE " + " AND ".join(where) + " ORDER BY recibido_en DESC",
                         args).fetchall()
    if not filas:
        return []
    propias = _propias_enviadas(conn)
    out = []
    for r in filas:
        f = dict(r)
        fmt = f.get("tipo_senal") or utr_externo.formato_de(f)
        if fmt != formato:
            continue
        t = _fecha_dt(f.get("recibido_en"))
        xa, xb = _tokens_nombre(f.get("jugador")), _tokens_nombre(f.get("rival"))
        if any(pt and t and abs(pt - t) <= timedelta(hours=48) and _mismo_par(xa, xb, pa, pb)
               for pt, pa, pb in propias):
            continue                                   # ya salió por un motor propio: un solo mensaje por partido
        d = f"ΔUTR {f['delta_utr']:+.2f}" if f.get("delta_utr") is not None else None
        if formato == "UTR_MARKET_ANOMALY":
            fase = "en vivo" if (f.get("tipo") or "").upper() == "LIVE" else "pre-partido"
            d = (d + " · " if d else "") + fase
        out.append({"id": -int(f["id"]), "fecha": f["recibido_en"],
                    "partido": f"{f['jugador']} vs {f.get('rival') or '?'}", "seleccion": f["jugador"],
                    "etiqueta": "🚨" if formato == "UTR_MARKET_ANOMALY" else "", "torneo": f.get("torneo"),
                    "cuota": f.get("cuota"), "resultado": _resultado_wl(f.get("result")), "marcador": f.get("resultado_score"),
                    "detalle": d, "lado_a": f["jugador"], "lado_b": f.get("rival"), "origen": "externo"})
    return out


def _mezclar(picks: list[dict], extra: list[dict]) -> list[dict]:
    """Del más nuevo al más viejo, como se muestran."""
    return sorted(picks + extra, key=lambda p: str(p.get("fecha") or ""), reverse=True) if extra else picks


def utr_anomalia(conn, vista: str = "discord", genero: str = "", valor_unidad: float = 1.0,
                 limite: int = 500) -> dict:
    """Un pick = el jugador de UTR superior (A) a la cuota de activación (la
    que disparó la anomalía) o, si no se activó, a la última cuota vista."""
    from production.engines.utr_anomalia import VERSION_MOTOR, _ensure_tabla
    _ensure_tabla(conn)
    where, args = [], []
    if vista == "discord":
        where.append("discord_sent=1")              # 10/10/2026: sin filtro por versión; nunca se borra
    else:
        where.append("version_motor=?")
        args.append(VERSION_MOTOR)
    if vista == "activadas":
        where.append("activada=1")
    if genero:
        where.append(_GENERO_SQL)
        args += [_g(genero), _g(genero)]
    picks = []
    for r in conn.execute(
            "SELECT id, primera_vista_en, torneo, jugador_a, jugador_b, activada, fase_activacion, cuota_activacion, "
            "cuota_ultima, cuota_evaluacion, result, resultado_score, delta_utr, delta_min, delta_max, fuente_utr, "
            "utr_player_id_a, utr_player_id_b, player_a_id, player_b_id FROM utr_anomalia_registro WHERE " + " AND ".join(where) + " ORDER BY primera_vista_en DESC",
            args).fetchall():
        cuota = r["cuota_evaluacion"] or r["cuota_activacion"] or r["cuota_ultima"]
        if r["delta_utr"] is not None and (r["fuente_utr"] or "EXACTO") != "INTERVALO":
            d = f"ΔUTR +{r['delta_utr']:.2f}"
        elif r["delta_min"] is not None:
            d = f"ΔUTR +{r['delta_min']:.2f} … +{r['delta_max']:.2f}"
        else:
            d = None
        if r["activada"]:
            d = (d + " · " if d else "") + ("en vivo" if r["fase_activacion"] == "vivo" else "pre-partido")
        picks.append({"id": r["id"], "fecha": r["primera_vista_en"],
                      "partido": f"{r['jugador_a']} vs {r['jugador_b']}", "seleccion": r["jugador_a"],
                      "etiqueta": "🚨" if r["activada"] else "", "torneo": r["torneo"], "cuota": cuota,
                      "resultado": _resultado_wl(r["result"]), "marcador": r["resultado_score"], "detalle": d,
                      "lado_a": r["jugador_a"], "lado_b": r["jugador_b"], **_urls_utr(conn, r)})
    if vista == "discord":
        picks = _mezclar(picks, externos_como(conn, "UTR_MARKET_ANOMALY", genero))
    picks = sin_ocultas(conn, "utra", picks)
    picks = _con_investigacion(conn, "utra", picks)          # v54: columna 🧠 Investigación
    return {**_armar(picks, valor_unidad, limite), "vista": vista, "vistas": UTRA_VISTAS,
            "version_motor": VERSION_MOTOR}


def utr_externo(conn, vista: str = "discord", genero: str = "", valor_unidad: float = 1.0,
                limite: int = 500) -> dict:
    """📡 UTR EXTERNO (09/10/2026): un pick = el jugador que marcó el canal
    TIRS, a la cuota del mensaje. Solo lo que sonó en Discord."""
    from production.engines.utr_externo import VERSION_MOTOR, _ensure_tabla
    _ensure_tabla(conn)
    where, args = ["1=1"], []
    if vista == "discord":
        where.append("discord_sent=1")
    if genero:
        where.append(_GENERO_SQL)
        args += [_g(genero), _g(genero)]
    picks = []
    for r in conn.execute(
            "SELECT id, recibido_en, torneo, jugador, rival, tipo, lado, cuota, cuota_pre, delta_utr, marcador, "
            "clasificacion_ft, result, resultado_score, motivo_no_discord FROM utr_externo_senales WHERE "
            + " AND ".join(where) + " ORDER BY recibido_en DESC", args).fetchall():
        d = f"ΔUTR {r['delta_utr']:+.2f}" if r["delta_utr"] is not None else ""
        d += (" · " if d else "") + ("en vivo" + (f" {r['marcador']}" if r["marcador"] else "") if r["tipo"] == "LIVE" else "pre-partido")
        if r["cuota_pre"]:
            d += f" · PRE {r['cuota_pre']:.2f}"
        if r["clasificacion_ft"] and r["clasificacion_ft"] != "NINGUNA":
            d += " · cumple " + r["clasificacion_ft"].replace("_", " ")
        if r["motivo_no_discord"]:
            d += " · " + r["motivo_no_discord"]
        picks.append({"id": r["id"], "fecha": r["recibido_en"], "partido": f"{r['jugador']} vs {r['rival'] or '?'}",
                      "seleccion": r["jugador"], "etiqueta": "📡", "torneo": r["torneo"], "cuota": r["cuota"],
                      "resultado": _resultado_wl(r["result"]), "marcador": r["resultado_score"], "detalle": d,
                      "lado_a": r["jugador"], "lado_b": r["rival"]})
    picks = sin_ocultas(conn, "utrx", picks)
    return {**_armar(picks, valor_unidad, limite), "vista": vista,
            "vistas": {"discord": "Enviadas a Discord", "todas": "Todo lo recibido del canal"},
            "version_motor": VERSION_MOTOR}


# ── 🏆 Implicación de Torneo (v45, 13/10/2026, Rubén: "quiero ver exactamente lo que veo en los
#    demás bots: un dashboard con resultados y debajo el historial de picks") ──
IMPLICACION_VISTAS = {"validados": "✅ Picks validados", "experimentales": "🧪 Candidatos (reglas experimentales)",
                      "todos": "Todos (validados + experimentales)"}


def implicacion(conn, vista: str = "", genero: str = "", valor_unidad: float = 1.0, limite: int = 500) -> dict:
    """Solo los PICKS del motor (nunca los partidos investigados). Vista por defecto: los
    validados si hay; si todavía no hay ninguno, los experimentales CON el aviso de que no
    tienen respaldo estadístico. Las pruebas del circuito no cuentan."""
    from production.engines.implicacion import picks as imp
    imp.ensure(conn)
    hay_val = conn.execute("SELECT 1 FROM implicacion_picks WHERE prueba=0 AND nivel='validado' LIMIT 1").fetchone() is not None
    if vista not in IMPLICACION_VISTAS:
        vista = "validados" if hay_val else "experimentales"
    where, args = ["prueba=0"], []
    if vista == "validados":
        where.append("nivel='validado'")
    elif vista == "experimentales":
        where.append("nivel='experimental'")
    if genero:
        where.append(_GENERO_SQL)
        args += [_g(genero), _g(genero)]
    res_map = {"ganado": GANADO, "perdido": PERDIDO, "push": PUSH}
    picks = []
    for r in conn.execute(
            "SELECT id, detectado_en, torneo, seleccion, rival, regla, nivel, cuota, resultado, marcador, set1_marcador, "
            "defensa_sel, defensa_pct, rank_sel, fav_cuota_pre, utr_delta_sel, nivel_implicacion, aviso, dicho_discord FROM implicacion_picks WHERE "
            + " AND ".join(where) + " ORDER BY detectado_en DESC", args).fetchall():
        _icono = {"ALTA": "🔴", "MEDIA": "🟡", "BAJA": "⚪"}.get(r["nivel_implicacion"] or "")
        det = [r["regla"].replace("IMP-", ""),
               f"{_icono} implicación {r['nivel_implicacion']}" if _icono else None,
               # 17/10/2026 (caso Kenin): «defiende» era lo que VENCE esa semana (derivado), no lo que se juega en el partido.
               # Solo se muestra en los picks antiguos R1/R2, rotulado como estimado.
               (f"vencían {r['defensa_sel']} pts (estimado)" + (f" ({r['defensa_pct']:.0f} %)" if r["defensa_pct"] is not None else "")
                if r["defensa_sel"] is not None and r["regla"] in ("IMP-R1", "IMP-R2") else None),
               f"#{r['rank_sel']}" if r["rank_sel"] else None,
               f"⭐ PRE {r['fav_cuota_pre']:.2f}" if r["fav_cuota_pre"] else None,
               f"UTR {r['utr_delta_sel']:+.2f}" if r["utr_delta_sel"] is not None else None,
               f"perdió el 1.er set {r['set1_marcador']}" if r["set1_marcador"] else None,
               ("📣 avisado en Discord" + (" como 🧪 experimental" if r["aviso"] == "experimental" else "")) if r["dicho_discord"] == 1 else None]
        picks.append({"id": r["id"], "fecha": r["detectado_en"], "partido": f"{r['seleccion']} vs {r['rival']}",
                      "seleccion": r["seleccion"], "etiqueta": ("✅" if r["nivel"] == "validado" else "🧪") + (f" {_icono}" if _icono else ""),
                      "torneo": r["torneo"], "cuota": r["cuota"], "resultado": res_map.get(r["resultado"], PENDIENTE),
                      "marcador": r["marcador"], "detalle": " · ".join(x for x in det if x),
                      "lado_a": r["seleccion"], "lado_b": r["rival"]})
    picks = sin_ocultas(conn, "implicacion", picks)
    out = {**_armar(picks, valor_unidad, limite), "vista": vista, "vistas": IMPLICACION_VISTAS, "hay_validados": hay_val,
           "niveles": imp.niveles(conn)}
    if vista != "validados":
        out["aviso"] = ("🧪 CANDIDATOS EXPERIMENTALES: reglas iniciales SIN respaldo estadístico todavía. Se guardan para "
                        "medirlas; no son picks confirmados ni se publican en Discord.")
    elif not hay_val:
        out["aviso"] = "Todavía ninguna regla está validada: no hay picks confirmados. Los candidatos están en la vista 🧪."
    return out


# ── 🔥 UNDER UTR (v54i, 15/10/2026, Rubén: «empezar a generar un histórico limpio») ──
UNDERUTR_VISTAS = {"todas": "Todas las activaciones (histórico completo)", "discord": "📣 Salieron a Discord"}


def under_utr(conn, vista: str = "todas", genero: str = "", valor_unidad: float = 1.0, limite: int = 500) -> dict:
    """Un pick = UNDER a la línea y la cuota CONGELADAS al activar (pre-partido), 1 unidad. Ganado / perdido / nulo
    (retiro sin decidir: push). Fuente: under_utr_registro (permanente). Por defecto TODO lo que activó; la vista 📣
    deja solo lo que salió a Discord."""
    from production.engines import under_utr as uu
    filtros = {"genero": _g(genero) if genero else "", "solo_discord": vista == "discord"}
    picks = []
    for f in uu.historial(conn, filtros):
        r = f.get("resultado")
        res = GANADO if r == uu.GANADO else PERDIDO if r == uu.PERDIDO else PUSH if r == uu.NULO else PENDIENTE
        final = f.get("resultado_score")
        detalle = [f"UTR {f['utr_a']:.2f} vs {f['utr_b']:.2f} · ΔUTR {f['delta_utr']:.2f}",
                   f"al detectar: {f.get('estado_al_detectar') or 's/d'} · {f.get('marcador_al_detectar') or '0-0'}"
                   + (f" · verificado por {f['verificacion_fuente']}" if f.get("verificacion_fuente") else ""),
                   " · ".join(x for x in (f.get("fase"), f.get("circuito"), f.get("casa_activacion")) if x)]
        if not f.get("formato_confirmado"):
            detalle.append("⚠️ formato del mercado sin confirmar")
        est = uu.texto_discord_estado(f)
        if est:
            detalle.append(est)
        picks.append({"id": f["id"], "fecha": f.get("activada_en"), "partido": f"{f['jugador_a']} vs {f['jugador_b']}",
                      "seleccion": f"UNDER {f['linea_activacion']:g}", "etiqueta": "",
                      "torneo": f.get("torneo"), "cuota": f.get("cuota_activacion"), "resultado": res,
                      "marcador": f"{final} · {f['total_juegos_final']} juegos" if final and f.get("total_juegos_final") is not None else "",
                      "detalle": " · ".join(x for x in detalle if x),
                      "lado_a": f.get("jugador_a"), "lado_b": f.get("jugador_b"), "marcador_crudo": final or ""})
    picks = sin_ocultas(conn, "underutr", picks)
    return {**_armar(picks, valor_unidad, limite), "vista": vista if vista in UNDERUTR_VISTAS else "todas",
            "vistas": UNDERUTR_VISTAS}


def valor_unidad_de(conn, request) -> float:
    """La unidad de la cuenta (la misma de Picks). 1 si no hay."""
    try:
        from production.api.auth import usuario_actual
        from production.api.routes_picks import _leer_valor_unidad
        u = usuario_actual(request) if request is not None else None
        if u and u.get("id"):
            return float(_leer_valor_unidad(conn, u["id"]) or 1.0)
    except Exception:
        pass
    return 1.0


# ── 🗑 Reinicio de historial (08/10/2026, Rubén) ────────────────────────────
# Borra TODO el historial de la estrategia (la pestaña arranca de cero). Solo
# admin, con confirmación explícita desde la pantalla. Antes de borrar se
# guarda un respaldo JSON de las filas en `estrategia_respaldos` por si hay
# que volver atrás. No toca reglas, umbrales ni configuración.
REINICIABLES = {
    "maestros": ("partidos_maestros",),
    "cmp": ("cmp_senales", "cmp_l10_detalle"),
    "utrv": ("utr_value_registro",),
    "utra": ("utr_anomalia_registro",),
    "utrx": ("utr_externo_senales",),
}


REINICIO_DESHABILITADO = ("10/10/2026 (Rubén): las alertas enviadas a Discord nunca se borran de sus vistas. "
                          "El reinicio de historial quedó deshabilitado; el historial se pagina de a 10.")


def reiniciar(conn, clave: str, por: str) -> dict:
    raise PermissionError(REINICIO_DESHABILITADO)


def _reiniciar_deshabilitado(conn, clave: str, por: str) -> dict:   # pragma: no cover — se conserva por si vuelve
    import json as _json
    from datetime import datetime, timezone
    from production.api.deps import ejecutar_con_reintento
    tablas = REINICIABLES.get(clave)
    if not tablas:
        raise ValueError(f"No se puede reiniciar '{clave}'")
    conn.execute("CREATE TABLE IF NOT EXISTS estrategia_respaldos (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                 "estrategia TEXT NOT NULL, tabla TEXT NOT NULL, filas INTEGER NOT NULL, datos_json TEXT, "
                 "hecho_por TEXT, hecho_en TEXT NOT NULL)")
    existentes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    ahora = datetime.now(timezone.utc).isoformat()
    borradas = {}

    def _w():
        for t in tablas:
            if t not in existentes:
                continue
            filas = [dict(r) for r in conn.execute(f"SELECT * FROM {t}").fetchall()]
            conn.execute("INSERT INTO estrategia_respaldos (estrategia, tabla, filas, datos_json, hecho_por, hecho_en) "
                         "VALUES (?,?,?,?,?,?)", (clave, t, len(filas), _json.dumps(filas, ensure_ascii=False, default=str),
                                                   por, ahora))
            conn.execute(f"DELETE FROM {t}")
            borradas[t] = len(filas)
        conn.commit()
    ejecutar_con_reintento(_w, conn=conn, con_lock=True)
    return {"ok": True, "estrategia": clave, "borradas": borradas, "respaldo_en": ahora}


# ══════════════════════════════════════════════════════════════════════
#  ✏️ CERRAR A MANO (13/10/2026, Rubén: "cerrar apuestas, como en historial de picks")
# ══════════════════════════════════════════════════════════════════════
# Para las filas que el cierre automático no resolvió (el partido no llegó a
# match_log, nombre que no cruza, circuito que el bot no sigue) o que quedaron
# con un resultado mal cargado. Solo admin. Escribe el resultado en la MISMA
# tabla y con la MISMA fórmula que el cierre automático de cada estrategia
# (P/L a la cuota de evaluación de esa estrategia), y deja constancia en
# dashboard_cierres_manuales (quién, cuándo, qué había antes). Nunca borra filas.

CERRABLES = ("maestros", "cmp", "utrv", "utra", "utrx", "under", "recordador", "implicacion", "mov",
             "underutr")    # v54i: 🔥 UNDER UTR


def _ensure_cierres(conn) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS dashboard_cierres_manuales (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                 "estrategia TEXT NOT NULL, fila_id INTEGER NOT NULL, ganador TEXT, marcador TEXT, antes_json TEXT, "
                 "hecho_por TEXT, hecho_en TEXT NOT NULL)")


def _lado(ganador: str, a: Optional[str], b: Optional[str]) -> bool:
    """True si ganó `a`, False si ganó `b`. ValueError si el nombre no es ninguno de los dos."""
    from shared.tennis_names import normalize_name
    g = normalize_name(ganador or "")
    if g and g == normalize_name(a or ""):
        return True
    if g and g == normalize_name(b or ""):
        return False
    raise ValueError(f"El ganador tiene que ser uno de los dos jugadores: {a} o {b}.")


def cerrar_manual(conn, clave: str, fila_id: int, ganador: str, marcador: Optional[str], por: str) -> dict:
    import json as _json
    from datetime import datetime, timezone
    from production.api.deps import ejecutar_con_reintento
    from shared.retiros import es_retiro_por_score
    if clave not in CERRABLES:
        raise ValueError(f"'{clave}' no se cierra a mano desde el dashboard.")
    if clave in ("utrv", "utra") and int(fila_id) < 0:
        clave, fila_id = "utrx", -int(fila_id)          # v50d: alerta del canal mostrada en UTR VALUE / MARKET ANOMALY
    marcador = (marcador or "").strip() or None
    ahora = datetime.now(timezone.utc)
    conn.row_factory = sqlite3.Row
    sqls: list[tuple[str, tuple]] = []
    if clave == "maestros":
        from production.engines.partidos_maestros import _ensure_tabla
        _ensure_tabla(conn)
        f = conn.execute("SELECT * FROM partidos_maestros WHERE fixture_id=? LIMIT 1", (fila_id,)).fetchone()
        if not f:
            raise LookupError("No existe ese partido.")
        gano_a = _lado(ganador, f["favorito_nombre"], f["jugador2"] if f["favorito_nombre"] == f["jugador1"] else f["jugador1"])
        rival = f["jugador2"] if f["favorito_nombre"] == f["jugador1"] else f["jugador1"]
        sqls.append(("UPDATE partidos_maestros SET ganador_final=?, resultado_score=?, resuelto_en=? WHERE fixture_id=?",
                     (f["favorito_nombre"] if gano_a else rival, marcador, ahora.isoformat(), fila_id)))
    elif clave == "cmp":
        from production.engines.cuota_mal_puesta import _ensure_tabla
        _ensure_tabla(conn)
        f = conn.execute("SELECT * FROM cmp_senales WHERE id=?", (fila_id,)).fetchone()
        if not f:
            raise LookupError("No existe esa señal.")
        gano_a = _lado(ganador, f["player_a"], f["player_b"])
        pl = round((f["odds_a"] - 1.0) if gano_a else -1.0, 4) if f["odds_a"] else None
        sqls.append(("UPDATE cmp_senales SET ganador_final=?, resultado_score=?, result=?, retiro=?, profit_loss=?, resuelto_en=? "
                     "WHERE id=?", (f["player_a"] if gano_a else f["player_b"], marcador, "W" if gano_a else "L",
                                    int(es_retiro_por_score(marcador)), pl, ahora.isoformat(), fila_id)))
    elif clave in ("utrv", "utra"):
        mod = __import__("production.engines." + ("utr_value" if clave == "utrv" else "utr_anomalia"),
                         fromlist=["_ensure_tabla", "_fila_cierre", "_SQL_CIERRE"])
        mod._ensure_tabla(conn)
        tabla = "utr_value_registro" if clave == "utrv" else "utr_anomalia_registro"
        f = conn.execute(f"SELECT * FROM {tabla} WHERE id=?", (fila_id,)).fetchone()
        if not f:
            raise LookupError("No existe ese registro.")
        gano_a = _lado(ganador, f["jugador_a"], f["jugador_b"])
        # misma fórmula que el cierre automático; sin la condición "resuelto_en IS NULL" para poder corregir
        sqls.append((mod._SQL_CIERRE.replace(" AND resuelto_en IS NULL", ""), mod._fila_cierre(f, gano_a, marcador, ahora)))
    elif clave == "utrx":
        from production.engines import utr_externo
        utr_externo._ensure_tabla(conn)
        f = conn.execute("SELECT * FROM utr_externo_senales WHERE id=?", (fila_id,)).fetchone()
        if not f:
            raise LookupError("No existe esa alerta.")
        gano_a = _lado(ganador, f["jugador"], f["rival"])
        sqls.append(("UPDATE utr_externo_senales SET result=?, resultado_score=?, profit_loss=?, resuelto_en=? WHERE id=?",
                     utr_externo._cerrar(conn, f, gano_a, marcador, ahora)))
    elif clave == "under":
        from production.api.routes_public import _games_del_marcador
        from production.engines.under_vivo_registro import _ensure_tabla, _resultado_under
        _ensure_tabla(conn)
        f = conn.execute("SELECT * FROM under_vivo_registro WHERE id=?", (fila_id,)).fetchone()
        if not f:
            raise LookupError("No existe esa alerta.")
        gano_a = _lado(ganador, f["home"], f["away"])
        if not marcador:
            raise ValueError("Para el Under hace falta el marcador (con él se cuentan los games).")
        games = _games_del_marcador(marcador)
        sqls.append(("UPDATE under_vivo_registro SET ganador_final=?, resultado_score=?, games_totales=?, resultado_under=?, "
                     "resuelto_en=? WHERE id=?", (f["home"] if gano_a else f["away"], marcador, games,
                                                  _resultado_under(games, f["under_total"]), ahora.isoformat(), fila_id)))
    elif clave == "underutr":
        # v54i: con el marcador final se cuentan los juegos y se resuelve con la MISMA regla que el cierre
        # automático (production/engines/under_utr.resolver). El ganador del modal no decide nada acá.
        from production.engines import under_utr as uu
        uu._ensure_tabla(conn)
        f = conn.execute("SELECT * FROM under_utr_registro WHERE id=?", (fila_id,)).fetchone()
        if not f:
            raise LookupError("No existe esa activación.")
        if not marcador:
            raise ValueError("Para el UNDER hace falta el marcador final (con él se cuentan los juegos).")
        base = dict(f, resultado=None)
        u = uu.resolver(base, marcador, "retired" if es_retiro_por_score(marcador) else "", f"manual:{por}", ahora)
        if not u:
            raise ValueError("Ese marcador no es un partido terminado (ni un retiro): no alcanza para resolver el UNDER.")
        sqls.append((f"UPDATE under_utr_registro SET {', '.join(k + '=?' for k in u)} WHERE id=?",
                     tuple(u.values()) + (fila_id,)))
    elif clave == "recordador":
        from production.engines import recordador as rec_mod
        from production.engines.recordador_ia import _brier, _logloss
        rec_mod.ensure_tablas(conn)
        f = conn.execute("SELECT * FROM recordador_investigaciones WHERE id=?", (fila_id,)).fetchone()
        if not f:
            raise LookupError("No existe esa predicción.")
        gano_a = _lado(ganador, f["jugador"], f["rival"])
        p = f["p_base_jugador"]
        pa = f["p_ajustada_jugador"] if "p_ajustada_jugador" in f.keys() else None
        cuenta = int(p is not None)
        valores = {"ganador": f["jugador"] if gano_a else f["rival"], "gano_jugador": int(gano_a), "marcador": marcador,
                   "retiro": int(es_retiro_por_score(marcador)), "w_o": 0, "fuente": f"manual:{por}",
                   "fecha_resultado": ahora.isoformat(), "p_usada": p,
                   "brier_base": _brier(p, gano_a) if cuenta else None, "logloss_base": _logloss(p, gano_a) if cuenta else None,
                   "brier_ajustada": _brier(pa, gano_a) if (cuenta and pa is not None) else None, "cuenta": cuenta,
                   "cerrado_en": ahora.isoformat()}
        if conn.execute("SELECT 1 FROM recordador_resultados WHERE investigacion_id=?", (fila_id,)).fetchone():
            sqls.append((f"UPDATE recordador_resultados SET {', '.join(k + '=?' for k in valores)} WHERE investigacion_id=?",
                         tuple(valores.values()) + (fila_id,)))
        else:
            valores.update(investigacion_id=fila_id, recordatorio_id=f["recordatorio_id"], partido_clave=f["partido_clave"])
            sqls.append((f"INSERT INTO recordador_resultados ({', '.join(valores)}) VALUES ({', '.join('?' * len(valores))})",
                         tuple(valores.values())))
    elif clave == "mov":
        f = conn.execute("SELECT * FROM valor_historial WHERE id=? AND tipo LIKE 'movimiento%'", (fila_id,)).fetchone()
        if not f:
            raise LookupError("No existe ese movimiento.")
        gano_a = _lado(ganador, f["home"], f["away"])
        sqls.append(("UPDATE valor_historial SET ganador_final=?, resultado_score=? WHERE id=?",
                     (f["home"] if gano_a else f["away"], marcador, fila_id)))
    elif clave == "implicacion":
        from production.engines.implicacion import picks as imp_picks
        imp_picks.ensure(conn)
        f = conn.execute("SELECT * FROM implicacion_picks WHERE id=? AND prueba=0", (fila_id,)).fetchone()
        if not f:
            raise LookupError("No existe ese pick.")
        gano_a = _lado(ganador, f["seleccion"], f["rival"])
        sqls.append(("UPDATE implicacion_picks SET resultado=?, marcador=?, resuelto_en=?, resultado_fuente=? WHERE id=?",
                     (GANADO if gano_a else PERDIDO, marcador, ahora.isoformat(), f"manual:{por}", fila_id)))
    antes = {k: f[k] for k in f.keys() if any(x in k for x in ("result", "ganador", "score", "marcador", "resuelto", "profit", "under"))}
    _ensure_cierres(conn)

    def _w():
        for sql, args in sqls:
            conn.execute(sql, args)
        conn.execute("INSERT INTO dashboard_cierres_manuales (estrategia, fila_id, ganador, marcador, antes_json, hecho_por, hecho_en) "
                     "VALUES (?,?,?,?,?,?,?)", (clave, int(fila_id), ganador, marcador, _json.dumps(antes, default=str, ensure_ascii=False),
                                               por, ahora.isoformat()))
        conn.commit()
    ejecutar_con_reintento(_w, conn=conn, con_lock=True)
    return {"ok": True, "estrategia": clave, "id": int(fila_id), "ganador": ganador, "marcador": marcador}
