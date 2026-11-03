"""
production/engines/ft_prob_candidatos.py — 🎲 Candidatos de FT PROBABILIDAD para Discord (30/10/2026, Daniel:
«probabilidad superior a 60 % y cuota por encima de 1.70», aviso «cuando lo detecte y cuando empiece»).

RankingFTR DECIDE y arma el texto y la frase de voz (igual que Under UTR); FT Intelligence solo los transporta y FT Discord
los publica. Todo aquí es puro (sin red ni base): la ruta /ft-intel/probabilidad/candidatos le pasa los datos.

REGLA (los tres números se editan en Admin → Estrategias → FT PROBABILIDAD):
  candidato = probabilidad del modelo > FT_PROB_CAND_PROB_MIN (%)  Y  cuota ACTUAL > FT_PROB_CAND_CUOTA_MIN.
La cuota que se compara es la de AHORA (fixtures_ftr_elo), no la que tenía el servicio cuando calculó, que puede ser de
hace horas. Si la cuota actual no se puede leer o tiene más de MAX_EDAD_CUOTA_H horas, no hay aviso.

ETAPAS (a lo sumo dos avisos por jugador y partido):
  DETECTADO  el partido cumple la regla y todavía falta más de FT_PROB_CAND_MIN_ANTES minutos.
  INICIO     cumple la regla (con la cuota de ese momento) y faltan FT_PROB_CAND_MIN_ANTES minutos o menos.
Un partido que ya empezó no se avisa. Esto es una marca de observación: el modelo no está calibrado.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from production.engines.ventaja_leve_reglas import numero_en_palabras

MAX_EDAD_CUOTA_H = 3.0
_NOMBRE_COMP = (("TENNIS_DATA", "Tennis Data"), ("CUOTA", "Cuota"), ("UTR", "UTR"))


def _utc(v) -> Optional[datetime]:
    if not v:
        return None
    try:
        t = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _num(v) -> Optional[float]:
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    return n if n == n and abs(n) != float("inf") else None


def _pct(p: float, dec: int = 1) -> str:
    return f"{p * 100:.{dec}f}".replace(".", ",") + " %"


def _decimal_hablado(x: float) -> str:
    ent, frac = f"{x:.2f}".split(".")
    frac_txt = (" ".join(numero_en_palabras(int(c)) for c in frac) if frac.startswith("0")
                else numero_en_palabras(int(frac)))
    return f"{numero_en_palabras(int(ent))} punto {frac_txt}"


def _cuota_fresca(actualizado_en, ahora: datetime) -> bool:
    """True si la cuota se actualizó hace poco, o si no hay hora legible (no se descarta por eso)."""
    t = _utc(actualizado_en)
    return t is None or (ahora - t).total_seconds() <= MAX_EDAD_CUOTA_H * 3600


def evaluar(partidos: list, cuotas_ftr: dict, ahora: datetime, prob_min: float, cuota_min: float,
            min_antes: float) -> dict:
    """-> {'candidatos': [...], 'revisados': n}. `prob_min` en fracción (0.60)."""
    candidatos, revisados = [], 0
    for p in partidos or []:
        nombres = p.get("jugadores") or []
        probs = p.get("probabilidad") or {}
        inicio = _utc(p.get("inicio_utc"))
        fila = cuotas_ftr.get(str(p.get("id_ftr") or ""))
        if len(nombres) < 2 or not probs or inicio is None or fila is None:
            continue
        minutos = (inicio - ahora).total_seconds() / 60
        if minutos <= 0:
            continue                                   # ya empezó (o está por empezar sin margen): no se avisa
        revisados += 1
        if not _cuota_fresca(fila.get("actualizado_en"), ahora):
            continue
        comp = p.get("componentes") or {}
        for i, jugador in enumerate(nombres[:2]):
            prob, odd = _num(probs.get(jugador)), _num(fila.get(f"odd{i + 1}"))
            if prob is None or odd is None or odd <= 1:
                continue
            if not (prob > prob_min and odd > cuota_min):
                continue
            lado = "A" if i == 0 else "B"
            comps = {k: _num((comp.get(k) or {}).get(lado)) for k, _ in _NOMBRE_COMP}
            c = {"fixture_id": p.get("fixture_id"), "id_ftr": str(p.get("id_ftr")),
                 "jugador": jugador, "rival": nombres[1 - i], "jugador1": nombres[0], "jugador2": nombres[1],
                 "torneo": p.get("torneo"), "superficie": p.get("superficie"),
                 "inicio_utc": inicio.isoformat(), "minutos_para_inicio": round(minutos, 1),
                 "prob": prob, "cuota": odd, "ev": prob * odd - 1,
                 "componentes": comps, "etapa": "INICIO" if minutos <= min_antes else "DETECTADO"}
            c.update(armar_textos(c))
            candidatos.append(c)
    return {"candidatos": candidatos, "revisados": revisados}


def armar_textos(c: dict) -> dict:
    """texto_discord (la primera línea es el encabezado, que Admin puede reemplazar) y texto_voz."""
    inicio = _utc(c["inicio_utc"])
    epoch = int(inicio.timestamp())
    inicio_txt = f"<t:{epoch}:R>" if c["etapa"] == "INICIO" else f"<t:{epoch}:f> (<t:{epoch}:R>)"
    minutos = max(1, int(round(c["minutos_para_inicio"])))
    cab = ("🎲 FULLTENNIS · FT PROBABILIDAD · EMPIEZA EN %d MIN" % minutos) if c["etapa"] == "INICIO" \
        else "🎲 FULLTENNIS · FT PROBABILIDAD · CANDIDATO"
    comps = c.get("componentes") or {}
    mercado = comps.get("CUOTA")
    lineas = [
        cab,
        f"🎾 {c['jugador1']} vs {c['jugador2']}" + (f" · {c['torneo']}" if c.get("torneo") else "")
        + f" · empieza {inicio_txt}",
        f"✅ Candidato: **{c['jugador']}**",
        f"📈 Probabilidad del modelo: {_pct(c['prob'])}"
        + (f"  (mercado sin margen: {_pct(mercado)})" if mercado is not None else ""),
        f"💰 Cuota: {c['cuota']:.2f}  ·  valor esperado " + f"{c['ev'] * 100:+.1f}".replace(".", ",") + " %",
    ]
    partes = [f"{nombre} {_pct(comps[k], 0)}" for k, nombre in _NOMBRE_COMP if comps.get(k) is not None]
    if partes:
        lineas.append("📊 " + " · ".join(partes))
    lineas.append("⚠️ Experimental, sin calibrar. Informativo, no es una recomendación.")
    prob_voz = numero_en_palabras(int(round(c["prob"] * 100)))
    if c["etapa"] == "INICIO":
        voz = (f"En {numero_en_palabras(minutos)} {'minuto' if minutos == 1 else 'minutos'}, "
               f"{c['jugador1']} contra {c['jugador2']}. FT Probabilidad: {c['jugador']}, {prob_voz} por ciento. "
               f"Cuota {_decimal_hablado(c['cuota'])}. Modelo experimental.")
    else:
        voz = (f"FT Probabilidad detecta un candidato. {c['jugador']}, {prob_voz} por ciento. "
               f"Cuota {_decimal_hablado(c['cuota'])}. {c['jugador1']} contra {c['jugador2']}. Modelo experimental.")
    return {"texto_discord": "\n".join(lineas), "texto_voz": "Atención FullTennis. " + voz}
