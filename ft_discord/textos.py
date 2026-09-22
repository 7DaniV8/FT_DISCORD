"""
ft_discord/textos.py — Cómo se dice cada señal: en el canal de texto y
en voz alta (21/09/2026).

TEXTO. Las horas van como marca de tiempo de Discord (<t:...:F>): cada
persona la ve en SU zona horaria, sin configurar nada.

VOZ. El sintetizador lee mejor las palabras que los números: "a las nueve
y treinta de la noche", "sesenta y ocho por ciento". La hora se dice en la
zona configurada (FT_DISCORD_ZONA_HORARIA). Frases cortas: una señal no
debería pasar de unos 15 segundos hablada.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

EMOJI = {"REVISION_CUOTA": "📊", "PARTIDO_NUEVO": "🎾", "CARGA_EXTREMA": "🔋", "MTO_RECIENTE": "🩹", "CUOTA_LEJOS": "📊",
         "RECORDATORIO": "⏳", "CAMBIO_HORA": "🕐"}
TITULO = {"REVISION_CUOTA": "REVISIÓN DE CUOTA", "REVISION_VOZ": "AVISO DE VOZ", "PARTIDO_NUEVO": "NUEVO PARTIDO", "CARGA_EXTREMA": "CARGA EXTREMA", "MTO_RECIENTE": "MTO RECIENTE",
          "CUOTA_LEJOS": "CUOTA LEJOS DEL FTR", "RECORDATORIO": "PRÓXIMO PARTIDO",
          "CAMBIO_HORA": "CAMBIO DE HORA"}

# ── números en palabras ──────────────────────────────────────────────
_UNIDADES = ["cero", "uno", "dos", "tres", "cuatro", "cinco", "seis", "siete", "ocho", "nueve",
             "diez", "once", "doce", "trece", "catorce", "quince", "dieciséis", "diecisiete",
             "dieciocho", "diecinueve", "veinte", "veintiuno", "veintidós", "veintitrés",
             "veinticuatro", "veinticinco", "veintiséis", "veintisiete", "veintiocho",
             "veintinueve"]
_DECENAS = {30: "treinta", 40: "cuarenta", 50: "cincuenta", 60: "sesenta", 70: "setenta",
            80: "ochenta", 90: "noventa"}
_CENTENAS = {100: "ciento", 200: "doscientos", 300: "trescientos", 400: "cuatrocientos",
             500: "quinientos", 600: "seiscientos", 700: "setecientos", 800: "ochocientos",
             900: "novecientos"}


def numero(n: int, femenino: bool = False, apocope: bool = False) -> str:
    """0 a 999 en palabras. apocope: 'un partido', 'veintiún partidos';
    femenino: 'una hora', 'veintiuna'."""
    n = int(n)
    if not 0 <= n <= 999:
        return str(n)
    if n >= 100:
        if n == 100:
            return "cien"
        c, resto = (n // 100) * 100, n % 100
        return _CENTENAS[c] + ("" if resto == 0 else " " + numero(resto, femenino, apocope))
    if n < 30:
        palabra = _UNIDADES[n]
    else:
        d, u = (n // 10) * 10, n % 10
        palabra = _DECENAS[d] + ("" if u == 0 else " y " + _UNIDADES[u])
    if palabra.endswith("uno") and (femenino or apocope):
        base = palabra[:-3]
        # Solo "veintiún" lleva tilde: "un partido", "treinta y un partidos".
        palabra = base + ("una" if femenino else ("ún" if base == "veinti" else "un"))
    return palabra


def porcentaje(p: Optional[float]) -> str:
    return "sin dato" if p is None else f"{numero(round(p * 100))} por ciento"


# ── horas en palabras ────────────────────────────────────────────────
def _periodo(h: int) -> str:
    if h < 6:
        return "de la madrugada"
    if h < 12:
        return "de la mañana"
    if h == 12:
        return "del mediodía"
    if h < 20:
        return "de la tarde"
    return "de la noche"


def hora_hablada(t: datetime) -> str:
    """21:30 -> 'a las nueve y treinta de la noche'; 13:05 -> 'a la una y
    cinco de la tarde'."""
    h12 = t.hour % 12 or 12
    articulo = "a la" if h12 == 1 else "a las"
    minutos = "en punto" if t.minute == 0 else "y " + numero(t.minute)
    return f"{articulo} {numero(h12, femenino=True)} {minutos} {_periodo(t.hour)}"


_DIAS = ["el lunes", "el martes", "el miércoles", "el jueves", "el viernes", "el sábado",
         "el domingo"]


def dia_hablado(fecha_local, hoy_local) -> str:
    dif = (fecha_local - hoy_local).days
    return "hoy" if dif == 0 else "mañana" if dif == 1 else _DIAS[fecha_local.weekday()]


def _utc(fecha: str) -> datetime:
    t = datetime.fromisoformat(str(fecha).replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def cuando_hablado(senal: dict, zona: str, ahora: Optional[datetime] = None) -> str:
    """'hoy a las nueve y treinta de la noche' o 'hoy, sin hora confirmada'."""
    tz = ZoneInfo(zona)
    ahora = (ahora or datetime.now(timezone.utc)).astimezone(tz)
    if senal.get("hora_conocida"):
        t = _utc(senal["fecha_partido"]).astimezone(tz)
        return f"{dia_hablado(t.date(), ahora.date())} {hora_hablada(t)}"
    d = date.fromisoformat(str(senal["fecha_partido"])[:10])
    return f"{dia_hablado(d, ahora.date())}, sin hora confirmada"


def hora_local_hablada(senal: dict, zona: str) -> str:
    """Solo la hora ('a las nueve y treinta de la noche'), o '' si no se sabe."""
    if not senal.get("hora_conocida"):
        return ""
    return hora_hablada(_utc(senal["fecha_partido"]).astimezone(ZoneInfo(zona)))


def frase_probabilidades(d: dict) -> str:
    """'El FTR da favorito a A con sesenta y ocho por ciento; el Elo, setenta
    y cuatro.' prob_ftr y prob_elo son del favorito del FTR: si el Elo no
    está de acuerdo (menos de 50 %), se dice que prefiere al rival."""
    if d.get("prob_ftr") is None or not d.get("favorito_ftr"):
        return ""
    frase = f"El FTR da favorito a {d['favorito_ftr']} con {porcentaje(d['prob_ftr'])}"
    pe = d.get("prob_elo")
    if pe is not None:
        frase += (f"; el Elo, {porcentaje(pe)}" if pe >= 0.5
                  else f"; el Elo prefiere al rival, con {porcentaje(1 - pe)}")
    return frase + "."


def cuando_texto(senal: dict) -> str:
    if senal.get("hora_conocida"):
        ts = int(_utc(senal["fecha_partido"]).timestamp())
        return f"<t:{ts}:F> (<t:{ts}:R>)"
    return f"{str(senal['fecha_partido'])[:10]} · hora sin confirmar"


# ── texto para el canal ──────────────────────────────────────────────
def cuota_hablada(c: Optional[float]) -> str:
    """1.95 -> 'uno punto noventa y cinco'; 2.05 -> 'dos punto cero cinco'."""
    if not c:
        return "sin cuota"
    entero, _, dec = f"{c:.2f}".partition(".")
    dec = dec.rstrip("0")
    if not dec:
        return numero(int(entero))
    return f"{numero(int(entero))} punto " + ("cero " + numero(int(dec)) if dec.startswith("0")
                                              else numero(int(dec)))


def _ddmm(fecha: str) -> str:
    return f"{fecha[8:10]}/{fecha[5:7]}"


def _sin_repetidos(eventos: list) -> list:
    """Defensa: el mismo evento repetido en los datos se muestra una vez."""
    vistos, salida = set(), []
    for e in eventos or []:
        k = (e.get("tipo"), e.get("fecha"), (e.get("rival") or "").lower(), e.get("score"))
        if k not in vistos:
            vistos.add(k)
            salida.append(e)
    return salida


def _titulo_alerta(minutos: int) -> str:
    return ("ESTÁ POR EMPEZAR" if minutos <= 0 else "EMPIEZA EN 1 MINUTO" if minutos == 1
            else f"EMPIEZA EN {minutos} MINUTOS")


def _lineas_evento(nv: str, eventos: list) -> list:
    lineas = []
    for e in _sin_repetidos(eventos):
        rival = f"contra {e['rival']}" if e.get("rival") else ""
        extra = ", ".join(x for x in (rival, e.get("score")) if x)
        if e.get("tipo") == "MTO_GANO":
            lineas.append(f"🩹 {nv} pidió MTO el {_ddmm(e['fecha'])} y ganó igual" + (f" ({extra})" if extra else ""))
        else:
            lineas.append(f"🔄 {nv} vuelve tras retirarse el {_ddmm(e['fecha'])}" + (f" ({extra})" if extra else ""))
    return lineas


def texto_revision(s: dict, titulo: Optional[str] = None) -> str:
    """La ficha consolidada del motor de vigilancia: el evento, la
    investigación, la pregunta por la cuota del rival, los datos y la
    conclusión."""
    d = s.get("datos") or {}
    v, r = d.get("vigilado") or {}, d.get("rival") or {}
    nv, nr = v.get("nombre", "?"), r.get("nombre", "?")
    inv = d.get("investigacion")
    lineas = [f"🧠 {EMOJI['REVISION_CUOTA']} **FT INTELLIGENCE · {titulo or 'REVISIÓN DE CUOTA'}**",
              f"**{s['jugador1']}** vs **{s['jugador2']}** · {s.get('torneo') or ''} · {cuando_texto(s)}",
              *_lineas_evento(nv, d.get("eventos") or []),
              "🔎 Causa: " + ((inv or {}).get("resumen") or "sin confirmar")
              + ("" if inv else " (investigador pendiente)"),
              f"**¿Por qué {nr} está a {r.get('cuota')}?**",
              f"{nr}: mercado {porc(r.get('mercado'))} · FTR {porc(r.get('ftr'))} · Elo {porc(r.get('elo'))}"]
    cv, cr = v.get("carga") or {}, r.get("carga") or {}
    if cv:
        lineas.append(f"Carga ({cv.get('ventana_dias')} días): {nv} {cv.get('partidos')} · "
                      f"{nr} {cr.get('partidos', 's/d')}")
    ov, orr = v.get("oposicion") or {}, r.get("oposicion") or {}
    if ov or orr:
        lineas.append(f"Rivales recientes (percentil FTR): {nv} {ov.get('percentil', 's/d')} · "
                      f"{nr} {orr.get('percentil', 's/d')}")
    for otro in d.get("tambien_vigilados") or []:
        lineas += ["👀 También vigilado: " + linea.split(" ", 1)[1]
                   for linea in _lineas_evento(otro.get("nombre", "?"), otro.get("eventos") or [])]
    lineas += _linea_seguimiento(d)
    concl = d.get("conclusion") or {}
    etiqueta = {"EN_LINEA": "cuota en línea", "EXPLICADA": "explicada",
                "SIN_EXPLICACION": "sin explicación suficiente"}.get(concl.get("tipo"), "")
    lineas.append(f"**Conclusión{' (' + etiqueta + ')' if etiqueta else ''}:** {concl.get('texto', '')}")
    lineas.append(f"_Informativo, no es una recomendación · {s.get('version_regla')} · señal #{s['id']}_")
    return "\n".join(lineas)


def _frases_evento(d: dict, zona: str, ahora: Optional[datetime]) -> list:
    """Lo que le pasó al vigilado, dicho: "pidió atención médica ayer y ganó igual"."""
    hoy = (ahora or datetime.now(timezone.utc)).astimezone(ZoneInfo(zona)).date()
    frases = []
    eventos = _sin_repetidos(d.get("eventos") or [])
    for tipo, uno, varios in (("MTO_GANO", "pidió atención médica {hace} y ganó igual",
                               "pidió atención médica {n} veces en los últimos días y ganó igual"),
                              ("RETIRO", "vuelve tras retirarse {hace}", "vuelve tras retirarse")):
        de_tipo = [e for e in eventos if e.get("tipo") == tipo]
        if not de_tipo:
            continue
        dias = (hoy - date.fromisoformat(max(e["fecha"] for e in de_tipo)[:10])).days
        hace = "hoy" if dias <= 0 else "ayer" if dias == 1 else f"hace {numero(dias, apocope=True)} días"
        frases.append(uno.format(hace=hace) if len(de_tipo) == 1 else varios.format(n=numero(len(de_tipo))))
    return frases


def _voz_cuerpo(d: dict, zona: str, ahora: Optional[datetime]) -> str:
    """El evento del vigilado, lo que paga el rival y la conclusión."""
    v, r = d.get("vigilado") or {}, d.get("rival") or {}
    nv, nr = v.get("nombre", ""), r.get("nombre", "")
    frases = _frases_evento(d, zona, ahora)
    return ((f"{nv} {' y '.join(frases)}. " if frases else "")
            + f"{nr} paga {cuota_hablada(r.get('cuota'))}. "
            + ((d.get("conclusion") or {}).get("voz") or ""))


def voz_revision(s: dict, zona: str, ahora: Optional[datetime] = None) -> str:
    frase = (f"Revisión de cuota: {s['jugador1']} contra {s['jugador2']}, "
             f"{cuando_hablado(s, zona, ahora)}. " + _voz_cuerpo(s.get("datos") or {}, zona, ahora))
    return "Atención FullTennis. " + frase.replace("MTO", "tiempo médico")


def _es_candidato(d: dict) -> bool:
    return (d.get("decision") or {}).get("clasificacion") == "CANDIDATO"


def _linea_seguimiento(d: dict) -> list:
    """Qué hizo el jugador después del evento, según nuestros propios datos."""
    frase = (d.get("seguimiento") or {}).get("frase")
    return [f"🔄 Posterior: {frase}"] if frase else []


def texto_candidato(s: dict) -> str:
    """La alerta de un CANDIDATO (la segunda puerta de FT Intelligence): el
    lado con valor, su cuota, mercado contra FTR y Elo, el evento del
    vigilado, la salud y las piezas que respaldan la discrepancia."""
    d = s.get("datos") or {}
    dec, v = d.get("decision") or {}, d.get("vigilado") or {}
    inv = d.get("investigacion") or {}
    salud_txt = (inv.get("estado_actual") or {}).get("resumen") or (inv.get("causa") or {}).get("que_paso")
    lineas = [f"🔥 **FT INTELLIGENCE · CANDIDATO · {_titulo_alerta(d.get('minutos', 10))}**",
              f"**{s['jugador1']}** vs **{s['jugador2']}** · {s.get('torneo') or ''} · {cuando_texto(s)}",
              f"**{dec.get('jugador')} @{dec.get('cuota')}.** El mercado le asigna {porc(dec.get('mercado'))}, "
              f"mientras el FTR lo sitúa en {porc(dec.get('ftr'))} (Elo {porc(dec.get('elo'))}).",
              *_lineas_evento(v.get("nombre", "?"), d.get("eventos") or []),
              "🔎 Salud: " + (salud_txt or "sin confirmar (investigador pendiente)"),
              *_linea_seguimiento(d),
              "A favor: " + "; ".join(p.get("texto", "") for p in dec.get("a_favor") or []) + "."]
    for otro in d.get("tambien_vigilados") or []:
        lineas += ["👀 También vigilado: " + linea.split(" ", 1)[1]
                   for linea in _lineas_evento(otro.get("nombre", "?"), otro.get("eventos") or [])]
    lineas.append(f"_Candidato del motor, a validar con el backtest: no es una recomendación · "
                  f"{dec.get('version', '')} · señal #{s['id']}_")
    return "\n".join(lineas)


def voz_candidato(s: dict, zona: str, ahora: Optional[datetime] = None) -> str:
    d = s.get("datos") or {}
    dec, v = d.get("decision") or {}, d.get("vigilado") or {}
    m = d.get("minutos", 10)
    inicio = (f"Está por empezar {s['jugador1']} contra {s['jugador2']}." if m <= 0 else
              f"En un minuto empieza {s['jugador1']} contra {s['jugador2']}." if m == 1 else
              f"En {numero(m, apocope=True)} minutos empieza {s['jugador1']} contra {s['jugador2']}.")
    frases = _frases_evento(d, zona, ahora)
    apoyos = [p.get("corto", "") for p in dec.get("a_favor") or []]
    texto = (f"{inicio} Candidato: {dec.get('jugador')} paga {cuota_hablada(dec.get('cuota'))}. "
             f"El mercado le da {porcentaje(dec.get('mercado'))}; el FTR, {porcentaje(dec.get('ftr'))}. "
             + (f"{v.get('nombre')} {' y '.join(frases)}. " if frases else "")
             + (f"A favor: {', '.join(apoyos[:-1]) + ' y ' + apoyos[-1] if len(apoyos) > 1 else apoyos[0]}."
                if apoyos else ""))
    return ("Atención FullTennis. " + texto).replace("MTO", "tiempo médico")


def voz_aviso(s: dict, zona: str, ahora: Optional[datetime] = None) -> str:
    """El aviso de voz, 10 minutos antes: primero lo urgente (quién juega y
    cuándo), después el evento, lo que paga el rival y la conclusión."""
    d = s.get("datos") or {}
    m = d.get("minutos", 10)
    j1, j2 = s["jugador1"], s["jugador2"]
    inicio = (f"Está por empezar {j1} contra {j2}." if m <= 0 else
              f"En un minuto empieza {j1} contra {j2}." if m == 1 else
              f"En {numero(m, apocope=True)} minutos empieza {j1} contra {j2}.")
    otros = [o.get("nombre") for o in d.get("tambien_vigilados") or [] if o.get("nombre")]
    extra = f" También está vigilado {' y '.join(otros)}." if otros else ""
    return ("Atención FullTennis. " + inicio + " " + _voz_cuerpo(d, zona, ahora)
            + extra).replace("MTO", "tiempo médico")


def texto_canal(s: dict) -> str:
    tipo, d = s["tipo"], s.get("datos") or {}
    if tipo == "REVISION_CUOTA":
        return texto_revision(s)
    if tipo == "REVISION_VOZ":                   # la alerta, 10 minutos antes (solo CANDIDATO)
        return (texto_candidato(s) if _es_candidato(d)
                else texto_revision(s, _titulo_alerta(d.get("minutos", 10))))
    cab = (f"🧠 {EMOJI.get(tipo, '')} **FT INTELLIGENCE · {TITULO.get(tipo, tipo)}**\n"
           f"**{s['jugador1']}** vs **{s['jugador2']}** · {s.get('torneo') or ''} · "
           f"{cuando_texto(s)}")
    cuerpo = [s.get("resumen") or ""]
    if tipo == "PARTIDO_NUEVO":
        cuerpo = []                              # la cabecera ya dice todo lo esencial
        if d.get("prob_ftr") is not None:
            cuerpo.append(f"FTR: {d.get('favorito_ftr')} {porc(d.get('prob_ftr'))} · "
                          f"Elo {porc(d.get('prob_elo'))} (para {d.get('favorito_ftr')}) · "
                          f"cuotas de apertura {d.get('cuota_apertura_1')} / {d.get('cuota_apertura_2')}")
    elif tipo == "CARGA_EXTREMA":
        c = d.get("cargado", {})
        extra = f"{c.get('sets', 0)} sets y {c.get('games', 0)} games en {c.get('ventana_dias')} días"
        if c.get("minutos_conocidos"):
            extra += (f" · al menos {c['minutos_conocidos'] // 60} h {c['minutos_conocidos'] % 60} min "
                      f"en cancha ({c.get('partidos_con_minutos')} partidos con hora)")
        cuerpo.append(extra)
        if c.get("cuota_apertura"):
            cuerpo.append(f"Cuota de apertura de {c.get('nombre')}: {c['cuota_apertura']}"
                          + (" · favorito del FTR" if c.get("favorito_ftr") else ""))
    elif tipo == "CUOTA_LEJOS":
        cuerpo.append(f"Cuota de apertura de {d.get('favorito_ftr')}: "
                      f"{d.get('cuota_apertura_favorito')} · FTR {porc(d.get('prob_ftr'))} · "
                      f"Elo {porc(d.get('prob_elo'))} · mercado {porc(d.get('prob_mercado'))}")
        for aviso in (d.get("explicacion") or {}).get("avisos", []):
            cuerpo.append(f"⚠️ {aviso}")
    pie = f"_Informativo, no es una recomendación · {s.get('version_regla')} · señal #{s['id']}_"
    return "\n".join([cab, *[c for c in cuerpo if c], pie])


def porc(p: Optional[float]) -> str:
    return "s/d" if p is None else f"{p:.0%}"


# ── texto para decir en voz alta ─────────────────────────────────────
def texto_voz(s: dict, zona: str, ahora: Optional[datetime] = None,
              con_probabilidades: bool = True) -> str:
    tipo, d = s["tipo"], s.get("datos") or {}
    if tipo == "REVISION_CUOTA":
        return voz_revision(s, zona, ahora)
    if tipo == "REVISION_VOZ":
        return voz_candidato(s, zona, ahora) if _es_candidato(d) else voz_aviso(s, zona, ahora)
    j1, j2 = s["jugador1"], s["jugador2"]
    cuando = cuando_hablado(s, zona, ahora)
    if tipo == "PARTIDO_NUEVO":
        frase = f"Partido nuevo: {j1} contra {j2}, {cuando}."
        if con_probabilidades and frase_probabilidades(d):
            frase += " " + frase_probabilidades(d)
    elif tipo == "CARGA_EXTREMA":
        c, r = d.get("cargado", {}), d.get("rival", {})
        n, m = c.get("partidos", 0), r.get("partidos", 0)
        frase = (f"Carga extrema. {c.get('nombre')} llega con {numero(n, apocope=True)} "
                 f"{'partido' if n == 1 else 'partidos'} en los últimos "
                 f"{numero(c.get('ventana_dias', 8), apocope=True)} días; {r.get('nombre')}, "
                 f"con {numero(m)}. Juegan {cuando}.")
    elif tipo == "MTO_RECIENTE":
        dias = d.get("dias", 0)
        hace = ("hoy" if dias == 0 else "ayer" if dias == 1
                else f"hace {numero(dias, apocope=True)} días")
        rival = j2 if d.get("lado") == 1 else j1
        frase = (f"{d.get('jugador')} pidió atención médica {hace}. Juega contra {rival} {cuando}.")
    elif tipo == "CUOTA_LEJOS":
        e = d.get("explicacion") or {}
        porque = (f"Lo explicaría esto: {e['razones'][0]}." if e.get("razones")
                  else "Nada en nuestros datos lo explica.")
        frase = (f"Cuota fuera de lo normal en {j1} contra {j2}, {cuando}. La casa le da a "
                 f"{d.get('favorito_ftr')} {porcentaje(d.get('prob_mercado'))}; el FTR, "
                 f"{porcentaje(d.get('prob_ftr'))}. {porque}")
    elif tipo == "RECORDATORIO":
        mins = d.get("minutos", 15)
        a_las = hora_local_hablada(s, zona)
        frase = (f"Falta un minuto para {j1} contra {j2}" if mins == 1 else
                 f"Faltan {numero(mins, apocope=True)} minutos para {j1} contra {j2}")
        frase += f", {a_las}." if a_las else "."
    elif tipo == "CAMBIO_HORA":
        frase = f"Cambio de horario. {j1} contra {j2} ahora se juega {cuando}."
    else:
        frase = s.get("resumen") or ""
    # "MTO" se deletrearía letra por letra; dicho, se entiende mejor así.
    return "Atención FullTennis. " + frase.replace("MTO", "tiempo médico")


# ── ráfagas: muchas señales del mismo tipo en la misma vuelta ────────
_MAX_LINEAS_GRUPO = 15


def texto_canal_grupo(tipo: str, senales: list) -> str:
    """Una sola publicación para una ráfaga (FullTenis publica el calendario
    en tandas). Discord corta los mensajes en 2.000 caracteres."""
    n = len(senales)
    titulo = {"PARTIDO_NUEVO": f"{n} PARTIDOS NUEVOS",
              "RECORDATORIO": f"EN {senales[0].get('datos', {}).get('minutos', 15)} MINUTOS EMPIEZAN {n} PARTIDOS"
              }.get(tipo, f"{n} SEÑALES · {TITULO.get(tipo, tipo)}")
    lineas = [f"🧠 {EMOJI.get(tipo, '')} **FT INTELLIGENCE · {titulo}**"]
    for s in sorted(senales, key=lambda x: str(x.get("fecha_partido")))[:_MAX_LINEAS_GRUPO]:
        hora = (f"<t:{int(_utc(s['fecha_partido']).timestamp())}:t>" if s.get("hora_conocida")
                else "hora sin confirmar")
        lineas.append(f"• **{s['jugador1']}** vs **{s['jugador2']}** · {s.get('torneo') or ''} · {hora}")
    if n > _MAX_LINEAS_GRUPO:
        lineas.append(f"…y {n - _MAX_LINEAS_GRUPO} más")
    lineas.append("_Informativo, no es una recomendación_")
    return "\n".join(lineas)


def texto_voz_grupo(tipo: str, senales: list, zona: str, ahora: Optional[datetime] = None) -> str:
    n = len(senales)
    primero = sorted(senales, key=lambda x: str(x.get("fecha_partido")))[0]
    if tipo == "RECORDATORIO":
        mins = primero.get("datos", {}).get("minutos", 15)
        frase = (f"En {numero(mins, apocope=True)} minutos empiezan {numero(n, apocope=True)} partidos."
                 f" Los detalles, en el canal de texto.")
    else:
        frase = (f"{numero(n, apocope=True).capitalize()} partidos nuevos en el calendario. "
                 f"El primero: {primero['jugador1']} contra {primero['jugador2']}, "
                 f"{cuando_hablado(primero, zona, ahora)}.")
    return f"Atención FullTennis. {frase}"


TEXTO_PRUEBA_VOZ = ("Atención FullTennis. Esta es una prueba de voz. "
                    "Si me escuchas, la voz funciona.")


TEXTO_RECONEXION = ("Atención FullTennis. Volví a conectarme al canal. "
                    "La voz sigue funcionando.")


def texto_prueba_canal(canal_voz: str, dave: str) -> str:
    return (f"🔧 **Prueba de FT Discord** · voz en **{canal_voz}** · cifrado DAVE: {dave}\n"
            f"Si en ese canal se escucha la frase de prueba, la voz funciona.")
