#!/usr/bin/env python3
"""
tests/test_bot.py — FT Discord sin Discord (21/09/2026).

  1. Español hablado: números, apócope ("un partido" / "con uno"), horas
     ("a la una y cinco de la tarde"), hoy/mañana, zona horaria.
  2. Textos por tipo de señal: el del canal (con la hora de Discord que
     cada uno ve en su zona) y el hablado (sin cifras sueltas).
  3. Anunciador: arranca desde ahora (no repite lo viejo), anuncia cada
     señal una vez, descarta las atrasadas, respeta tipos y horas de
     silencio, y una falla al publicar no frena a las demás.
  4. Voz: el pedido exacto a Google Text-to-Speech; errores sin romper.
  5. Configuración incompleta: dice qué falta.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
for k in ("DISCORD_TOKEN", "DISCORD_CANAL_TEXTO_ID", "FT_INTEL_URL", "FTR_INTERNAL_SECRET"):
    os.environ.pop(k, None)

import httpx  # noqa: E402

from ft_discord import config  # noqa: E402
from ft_discord.anunciador import Anunciador, en_silencio  # noqa: E402
from ft_discord.fuente import ErrorFuente, Fuente  # noqa: E402
from ft_discord.textos import (TEXTO_PRUEBA_VOZ, dia_hablado, hora_hablada,  # noqa: E402
                               numero, texto_canal, texto_canal_grupo, texto_prueba_canal,
                               texto_voz, texto_voz_grupo)
from ft_discord.tts import TTSGoogle  # noqa: E402

_fallos: list[str] = []
_ok = 0


def check(nombre: str, condicion: bool, detalle: str = ""):
    global _ok
    if condicion:
        _ok += 1
        print(f"  OK    {nombre}" + (f" -- {detalle}" if detalle else ""))
    else:
        _fallos.append(nombre)
        print(f"  FALLA {nombre}" + (f": {detalle}" if detalle else ""))


print("\n1. Español hablado")
for n, kw, esperado in [(1, {}, "uno"), (1, {"apocope": True}, "un"),
                        (21, {"apocope": True}, "veintiún"), (31, {"apocope": True}, "treinta y un"),
                        (1, {"femenino": True}, "una"), (16, {}, "dieciséis"),
                        (74, {}, "setenta y cuatro"), (100, {}, "cien"),
                        (101, {"apocope": True}, "ciento un"), (250, {}, "doscientos cincuenta")]:
    check(f"{n} {kw or ''} -> {esperado}", numero(n, **kw) == esperado, numero(n, **kw))
for (h, m), esperado in [((21, 30), "a las nueve y treinta de la noche"),
                         ((13, 5), "a la una y cinco de la tarde"),
                         ((12, 0), "a las doce en punto del mediodía"),
                         ((0, 15), "a las doce y quince de la madrugada")]:
    r = hora_hablada(datetime(2026, 9, 22, h, m))
    check(f"{h:02d}:{m:02d} -> {esperado}", r == esperado, r)
from datetime import date  # noqa: E402

check("hoy / mañana / día de la semana",
      (dia_hablado(date(2026, 9, 22), date(2026, 9, 22)), dia_hablado(date(2026, 9, 23), date(2026, 9, 22)),
       dia_hablado(date(2026, 9, 25), date(2026, 9, 22))) == ("hoy", "mañana", "el viernes"))

print("\n2. Textos por tipo de señal")
AHORA = datetime(2026, 9, 22, 20, 0, tzinfo=timezone.utc)          # 15:00 en Bogotá (UTC-5)
BASE = {"fixture_id": 11, "par_norm": "x", "jugador1": "Jugador Uno", "jugador2": "Jugador Dos",
        "torneo": "M25 A", "version_regla": "v1", "creado_en": AHORA.isoformat(),
        "fecha_partido": "2026-09-23T02:30:00+00:00", "hora_conocida": 1}
CARGA = {**BASE, "id": 7, "tipo": "CARGA_EXTREMA", "resumen": "Jugador Uno llega con 8 partidos.",
         "datos": {"cargado": {"lado": 1, "nombre": "Jugador Uno", "partidos": 8, "sets": 19,
                               "games": 180, "ventana_dias": 8, "minutos_conocidos": 340,
                               "partidos_con_minutos": 3, "cuota_apertura": 1.55,
                               "favorito_ftr": True},
                   "rival": {"lado": 2, "nombre": "Jugador Dos", "partidos": 1, "ventana_dias": 8}}}
voz = texto_voz(CARGA, "America/Bogota", AHORA)
check("voz de carga: hora local en palabras y 'con uno' (sin sustantivo detrás)",
      voz == "Atención FullTennis. Carga extrema. Jugador Uno llega con ocho partidos en los "
             "últimos ocho días; Jugador Dos, con uno. Juegan hoy a las nueve y treinta de la noche.",
      voz)
canal = texto_canal(CARGA)
check("texto de carga: hora de Discord (cada uno la ve en su zona) y detalle",
      "<t:1790130600:F>" in canal and "5 h 40 min" in canal and "1.55" in canal
      and "no es una recomendación" in canal, canal)
MTO = {**BASE, "id": 8, "tipo": "MTO_RECIENTE", "resumen": "Jugador Dos pidió MTO.",
       "datos": {"jugador": "Jugador Dos", "lado": 2, "dias": 2}}
check("voz de MTO: 'atención médica', rival correcto",
      texto_voz(MTO, "America/Bogota", AHORA) == "Atención FullTennis. Jugador Dos pidió atención "
      "médica hace dos días. Juega contra Jugador Uno hoy a las nueve y treinta de la noche.")
CUOTA = {**BASE, "id": 9, "tipo": "CUOTA_LEJOS", "resumen": "La casa confía menos.",
         "datos": {"favorito_ftr": "Jugador Uno", "prob_mercado": 0.689, "prob_ftr": 0.85,
                   "prob_elo": 0.8, "cuota_apertura_favorito": 1.4,
                   "explicacion": {"razones": ["Jugador Uno pidió MTO hace 2 días"],
                                   "avisos": ["el Elo no coincide con el FTR"]}}}
v = texto_voz(CUOTA, "America/Bogota", AHORA)
check("voz de cuota: porcentajes en palabras y 'MTO' dicho como palabras",
      "sesenta y nueve por ciento" in v and "ochenta y cinco por ciento" in v
      and "tiempo médico" in v and "MTO" not in v, v)
check("texto de cuota: los avisos se ven", "⚠️ el Elo no coincide" in texto_canal(CUOTA))
REC = {**BASE, "id": 10, "tipo": "RECORDATORIO", "resumen": "Faltan 1 minutos", "datos": {"minutos": 1}}
check("recordatorio de un minuto en singular, con la hora del partido",
      texto_voz(REC, "UTC", AHORA) == "Atención FullTennis. Falta un minuto para Jugador Uno "
      "contra Jugador Dos, a las dos y treinta de la madrugada.", texto_voz(REC, "UTC", AHORA))
SIN_HORA = {**CARGA, "fecha_partido": "2026-09-22", "hora_conocida": 0}
check("ITF sin hora: se dice sin inventar una hora",
      texto_voz(SIN_HORA, "America/Bogota", AHORA).endswith("Juegan hoy, sin hora confirmada.")
      and "hora sin confirmar" in texto_canal(SIN_HORA))

NUEVO = {**BASE, "id": 12, "tipo": "PARTIDO_NUEVO", "resumen": "Nuevo partido.",
         "datos": {"nivel": "ATP_WTA", "favorito_ftr": "Jugador Uno", "prob_ftr": 0.68,
                   "prob_elo": 0.74, "cuota_apertura_1": 1.55, "cuota_apertura_2": 2.45}}
v = texto_voz(NUEVO, "America/Bogota", AHORA)
check("voz de partido nuevo + hora, con FTR y Elo en palabras",
      v == "Atención FullTennis. Partido nuevo: Jugador Uno contra Jugador Dos, hoy a las nueve y "
           "treinta de la noche. El FTR da favorito a Jugador Uno con sesenta y ocho por ciento; "
           "el Elo, setenta y cuatro por ciento.", v)
check("sin probabilidades si se desactiva", texto_voz(NUEVO, "America/Bogota", AHORA, False)
      == "Atención FullTennis. Partido nuevo: Jugador Uno contra Jugador Dos, hoy a las nueve y "
         "treinta de la noche.")
DISCREPA = {**NUEVO, "datos": {**NUEVO["datos"], "prob_elo": 0.42}}
check("si el Elo no está de acuerdo con el FTR, lo dice (no lee un 42 % engañoso)",
      "el Elo prefiere al rival, con cincuenta y ocho por ciento" in texto_voz(DISCREPA, "UTC", AHORA))
check("texto de partido nuevo: FTR, Elo y cuotas de apertura",
      "FTR: Jugador Uno 68% · Elo 74%" in texto_canal(NUEVO) and "1.55 / 2.45" in texto_canal(NUEVO))
tanda = [{**NUEVO, "id": 20 + k, "jugador1": f"A{k}", "jugador2": f"B{k}",
          "fecha_partido": f"2026-09-23T0{k}:00:00+00:00"} for k in range(1, 6)]
g = texto_canal_grupo("PARTIDO_NUEVO", tanda)
check("ráfaga en texto: UNA publicación con la lista ordenada por hora",
      g.startswith("🧠 🎾 **FT INTELLIGENCE · 5 PARTIDOS NUEVOS**") and g.count("• ") == 5
      and g.index("A1") < g.index("A5"), g.splitlines()[0])
check("ráfaga en voz: UNA frase con el total y el primero",
      texto_voz_grupo("PARTIDO_NUEVO", tanda, "UTC", AHORA) == "Atención FullTennis. Cinco partidos "
      "nuevos en el calendario. El primero: A1 contra B1, mañana a la una en punto de la madrugada.")
recs = [{**REC, "id": 30 + k, "datos": {"minutos": 15}} for k in range(4)]
check("varios recordatorios a la vez: una frase",
      texto_voz_grupo("RECORDATORIO", recs, "UTC", AHORA) == "Atención FullTennis. En quince minutos "
      "empiezan cuatro partidos. Los detalles, en el canal de texto.")
check("textos del modo prueba", "Si me escuchas, la voz funciona" in TEXTO_PRUEBA_VOZ
      and "cifrado DAVE: activo" in texto_prueba_canal("Anuncios", "activo"))

print("\n3. Anunciador")
SENALES = [{**CARGA, "id": 5, "creado_en": "2026-09-22T19:59:00+00:00"},
           {**MTO, "id": 6, "creado_en": "2026-09-22T19:59:30+00:00"}]
pedidos: list[dict] = []


def ft_intel(req: httpx.Request) -> httpx.Response:
    q = {k: v[0] for k, v in parse_qs(req.url.query.decode()).items()}
    pedidos.append(q)
    if req.headers.get("X-FTR-Internal-Secret") != "secreto":
        return httpx.Response(403, json={"detail": "no"})
    desde = int(q["desde_id"])
    return httpx.Response(200, json={"senales": [s for s in SENALES if s["id"] > desde],
                                     "ultimo_id": max(s["id"] for s in SENALES)})


publicados: list[str] = []
dichos: list[str] = []


async def publicar(t: str) -> None:
    if "FALLAR" in t:
        raise RuntimeError("Discord caído")
    publicados.append(t)


def encolar(t: str) -> bool:
    dichos.append(t)
    return True


async def escenario():
    fuente = Fuente("https://ft-intel.test", "secreto", transport=httpx.MockTransport(ft_intel))
    a = Anunciador(fuente, publicar, encolar, "America/Bogota", {"CARGA_EXTREMA", "MTO_RECIENTE"},
                   {"CARGA_EXTREMA"}, 30, reloj=lambda: AHORA)
    cursor = await a.arrancar()
    r0 = await a.ciclo()
    check("arranca desde ahora: las señales que ya existían NO se anuncian",
          cursor == 6 and r0["texto"] == 0 and not publicados, str(r0))
    SENALES.append({**CARGA, "id": 7, "creado_en": "2026-09-22T19:59:50+00:00"})
    SENALES.append({**MTO, "id": 8, "creado_en": "2026-09-22T19:59:55+00:00"})
    r1 = await a.ciclo()
    check("la nueva de carga va por texto y voz; la de MTO solo por texto (según tipos)",
          r1["texto"] == 2 and r1["voz"] == 1 and len(dichos) == 1, str(r1))
    r2 = await a.ciclo()
    check("cada señal se anuncia una sola vez", r2["texto"] == 0 and len(publicados) == 2)
    SENALES.append({**CARGA, "id": 9, "creado_en": "2026-09-22T18:00:00+00:00"})
    r3 = await a.ciclo()
    check("una señal atrasada (2 h) no se anuncia", r3["viejas"] == 1 and len(publicados) == 2)
    SENALES.append({**CARGA, "id": 10, "resumen": "FALLAR", "creado_en": AHORA.isoformat()})
    SENALES.append({**MTO, "id": 11, "creado_en": AHORA.isoformat()})
    r4 = await a.ciclo()
    check("una falla al publicar no frena a la siguiente", r4["errores"] == 1 and r4["texto"] == 1)
    ahora_s = AHORA.isoformat()
    SENALES.extend({**NUEVO, "id": 40 + k, "jugador1": f"A{k}", "creado_en": ahora_s} for k in range(6))
    antes_t, antes_v = len(publicados), len(dichos)
    g = Anunciador(fuente, publicar, encolar, "UTC", {"PARTIDO_NUEVO"}, {"PARTIDO_NUEVO"}, 30,
                   reloj=lambda: AHORA, umbral_agrupar=3)
    await g.arrancar(desde_id=39)
    r6 = await g.ciclo()
    check("6 partidos nuevos en una vuelta: 1 publicación y 1 frase, no 6",
          r6["agrupadas"] == 6 and len(publicados) - antes_t == 1 and len(dichos) - antes_v == 1, str(r6))
    SENALES.extend({**NUEVO, "id": 50 + k, "creado_en": ahora_s} for k in range(2))
    r7 = await g.ciclo()
    check("2 partidos nuevos (menos que el umbral): uno por uno",
          r7["agrupadas"] == 0 and r7["texto"] == 2, str(r7))
    silencio = Anunciador(fuente, publicar, encolar, "America/Bogota", set(), {"CARGA_EXTREMA"},
                          30, silencio_voz="00-07",
                          reloj=lambda: datetime(2026, 9, 22, 8, 0, tzinfo=timezone.utc))  # 03:00 local
    await silencio.arrancar(desde_id=9)
    r5 = await silencio.ciclo()
    check("en horas de silencio, nada por voz", r5["voz"] == 0 and r5["voz_omitida"] == 1, str(r5))
    malo = Fuente("https://ft-intel.test", "otro", transport=httpx.MockTransport(ft_intel))
    try:
        await malo.ultimo_id()
        check("secreto equivocado -> ErrorFuente", False)
    except ErrorFuente:
        check("secreto equivocado -> ErrorFuente", True)
    await fuente.cerrar()

asyncio.run(escenario())
check("rangos de silencio, incluso cruzando la medianoche",
      en_silencio("00-07", 3) and not en_silencio("00-07", 7) and en_silencio("22-07", 23)
      and en_silencio("22-07", 2) and not en_silencio("22-07", 12) and not en_silencio("", 3))

print("\n4. Voz (Google Text-to-Speech)")
visto: dict = {}


def google(req: httpx.Request) -> httpx.Response:
    visto["url"] = str(req.url)
    visto["cabecera"] = req.headers.get("X-Goog-Api-Key")
    visto["cuerpo"] = json.loads(req.content)
    if visto["cuerpo"]["voice"]["name"] == "voz-inexistente":
        return httpx.Response(400, json={"error": {"message": "Voice does not exist"}})
    return httpx.Response(200, json={"audioContent": base64.b64encode(b"OggS-audio").decode()})


t = TTSGoogle("CLAVE", "es-US", "es-US-Neural2-B", 1.0, transport=httpx.MockTransport(google))
ruta = t.sintetizar("Atención FullTennis.")
check("la clave viaja en la cabecera y NUNCA en la dirección (que httpx registra)",
      visto["cabecera"] == "CLAVE" and "CLAVE" not in visto["url"], visto["url"])
check("pide OGG_OPUS con la voz y el idioma configurados, y guarda el audio",
      visto["cuerpo"]["audioConfig"]["audioEncoding"] == "OGG_OPUS"
      and visto["cuerpo"]["voice"] == {"languageCode": "es-US", "name": "es-US-Neural2-B"}
      and ruta is not None and ruta.read_bytes() == b"OggS-audio")
t2 = TTSGoogle("CLAVE", "es-US", "voz-inexistente", 1.0, transport=httpx.MockTransport(google))
check("voz inexistente: devuelve None sin romper (el texto igual sale)", t2.sintetizar("x") is None)

print("\n5. Configuración")
check("sin variables, dice qué falta", len(config.problemas()) == 3, "; ".join(config.problemas()))
# 24/09/2026: + MTO_VIVO (tiempo médico en vivo, texto y voz).
check("por defecto: alerta de 10 minutos, Ventaja Leve, Pasan Filtro y MTO en vivo, por texto y voz",
      config.TIPOS_TEXTO == {"REVISION_VOZ", "VENTAJA_LEVE", "PASAN_FILTRO", "MTO_VIVO"}
      and config.TIPOS_VOZ == {"REVISION_VOZ", "VENTAJA_LEVE", "PASAN_FILTRO", "MTO_VIVO"})

print("\n6. La ficha consolidada del motor de vigilancia")
from ft_discord.textos import cuota_hablada  # noqa: E402
check("las cuotas dichas en voz alta", (cuota_hablada(1.95), cuota_hablada(2.05), cuota_hablada(2.0),
      cuota_hablada(1.5)) == ("uno punto noventa y cinco", "dos punto cero cinco", "dos", "uno punto cinco"))
_ahora = datetime(2026, 9, 22, 18, 0, tzinfo=timezone.utc)
rev = {"id": 7, "tipo": "REVISION_CUOTA", "jugador1": "Carlos Ruiz", "jugador2": "Pedro Soto",
       "torneo": "M25 Sapporo", "fecha_partido": "2026-09-23T15:00:00+00:00", "hora_conocida": 1,
       "version_regla": "revision_cuota.v1", "datos": {
           "eventos": [{"tipo": "MTO_GANO", "fecha": "2026-09-21", "rival": "Mario Gil", "score": "6-3 6-4"}],
           "investigacion": None,
           "vigilado": {"nombre": "Carlos Ruiz", "cuota": 1.87, "carga": {"partidos": 7, "ventana_dias": 8},
                        "oposicion": {"percentil": 12}},
           "rival": {"nombre": "Pedro Soto", "cuota": 1.95, "mercado": 0.49, "ftr": 0.43, "elo": 0.49,
                     "carga": {"partidos": 3, "ventana_dias": 8}, "oposicion": {"percentil": 91}},
           "conclusion": {"tipo": "EXPLICADA", "texto": "La cuota de Pedro Soto podría estar incorporando el MTO.",
                          "voz": "La cuota de Pedro Soto podría estar incorporando el tiempo médico de Carlos Ruiz."}}}
canal = texto_canal(rev)
check("el canal muestra el evento, la causa pendiente y la pregunta por la cuota del rival",
      "REVISIÓN DE CUOTA" in canal and "🩹 Carlos Ruiz pidió MTO el 21/09 y ganó igual (contra Mario Gil, 6-3 6-4)"
      in canal and "Causa: sin confirmar (investigador pendiente)" in canal
      and "**¿Por qué Pedro Soto está a 1.95?**" in canal, canal)
check("...con mercado, FTR, Elo, carga, oposición y la conclusión",
      "mercado 49%" in canal and "FTR 43%" in canal and "Carga (8 días): Carlos Ruiz 7 · Pedro Soto 3" in canal
      and "Carlos Ruiz 12 · Pedro Soto 91" in canal and "**Conclusión (explicada):**" in canal, canal)
voz = texto_voz(rev, "UTC", _ahora)
check("la voz dice el evento, la cuota del rival y la conclusión, sin 'MTO'",
      "Revisión de cuota: Carlos Ruiz contra Pedro Soto" in voz and "Carlos Ruiz pidió atención médica ayer "
      "y ganó igual" in voz and "Pedro Soto paga uno punto noventa y cinco" in voz and "MTO" not in voz, voz)
aviso = {**rev, "tipo": "REVISION_VOZ", "datos": {**rev["datos"], "minutos": 10, "revision_id": 7}}
va = texto_voz(aviso, "UTC", _ahora)
check("el aviso de 10 minutos empieza por lo urgente y sigue con la conclusión",
      va.startswith("Atención FullTennis. En diez minutos empieza Carlos Ruiz contra Pedro Soto. Carlos Ruiz pidió")
      and "Pedro Soto paga uno punto noventa y cinco" in va and "podría estar incorporando" in va
      and "MTO" not in va, va)
ca = texto_canal({**aviso, "datos": {**aviso["datos"], "eventos": aviso["datos"]["eventos"] * 4,
                                     "tambien_vigilados": [{"nombre": "Pedro Soto", "eventos": [
                                         {"tipo": "RETIRO", "fecha": "2026-09-10", "score": "4-1 ret."}]}]}})
check("la alerta escrita es la ficha completa, titulada 'EMPIEZA EN 10 MINUTOS'",
      "FT INTELLIGENCE · EMPIEZA EN 10 MINUTOS" in ca and "¿Por qué Pedro Soto está a 1.95?" in ca
      and "**Conclusión (explicada):**" in ca, ca)
check("un evento repetido en los datos se muestra UNA vez", ca.count("pidió MTO el 21/09") == 1, ca)
check("si el rival también está vigilado, lo dice (texto y voz)",
      "👀 También vigilado: Pedro Soto vuelve tras retirarse el 10/09 (4-1 ret.)" in ca
      and "También está vigilado Pedro Soto." in texto_voz({**aviso, "datos": {**aviso["datos"], "tambien_vigilados": [
          {"nombre": "Pedro Soto", "eventos": []}]}}, "UTC", _ahora), ca)
check("a 1 minuto y a 0 se dice distinto",
      "En un minuto empieza" in texto_voz({**aviso, "datos": {**aviso["datos"], "minutos": 1}}, "UTC", _ahora)
      and "Está por empezar" in texto_voz({**aviso, "datos": {**aviso["datos"], "minutos": 0}}, "UTC", _ahora))
cand = {**aviso, "datos": {**aviso["datos"], "decision": {
    "clasificacion": "CANDIDATO", "version": "decision.v1", "jugador": "Pedro Soto", "cuota": 2.6,
    "mercado": 0.38, "ftr": 0.56, "elo": 0.53,
    "a_favor": [{"texto": "el Elo también ve a Pedro Soto por encima del mercado (53 % contra 38 %)",
                 "corto": "el Elo lo acompaña"},
                {"texto": "Carlos Ruiz llega más cargado (7 partidos en 8 días contra 3)",
                 "corto": "Carlos Ruiz llega más cargado"}], "en_contra": []}}}
cc = texto_canal(cand)
check("CANDIDATO por escrito: el lado con valor, su cuota, mercado contra FTR, y las piezas a favor",
      "🔥 **FT INTELLIGENCE · CANDIDATO · EMPIEZA EN 10 MINUTOS**" in cc
      and "**Pedro Soto @2.6.** El mercado le asigna 38%, mientras el FTR lo sitúa en 56% (Elo 53%)." in cc
      and "A favor: el Elo también ve a Pedro Soto" in cc and "Salud: sin confirmar" in cc
      and "no es una recomendación" in cc, cc)
cand_conf = {**cand, "datos": {**cand["datos"], "confianza": {
    "nivel": "EXPERIMENTAL", "etiqueta": "🔴 EVIDENCIA: EXPERIMENTAL", "casos": 4,
    "texto": "4 casos comparables cerrados", "rendimiento": "1-3 · -1.4 U · ROI -35.0 %",
    "aviso": "Regla todavía con muestra pequeña."}}}
cc2 = texto_canal(cand_conf)
check("la evidencia se muestra aparte del rendimiento, y el caso sigue siendo candidato",
      "CANDIDATO" in cc2 and "🔴 EVIDENCIA: EXPERIMENTAL" in cc2 and "4 casos comparables cerrados" in cc2
      and "1-3 · -1.4 U · ROI -35.0 %" in cc2 and "⚠️ Regla todavía con muestra pequeña." in cc2, cc2)
check("con muestra amplia no hay aviso, y se ve el rendimiento igual",
      (lambda c: "🟢 EVIDENCIA: MUESTRA AMPLIA" in c and "82-55" in c and "⚠️" not in c)(
          texto_canal({**cand, "datos": {**cand["datos"], "confianza": {
              "nivel": "MUESTRA_AMPLIA", "etiqueta": "🟢 EVIDENCIA: MUESTRA AMPLIA", "casos": 137,
              "texto": "137 casos comparables cerrados", "rendimiento": "82-55 · +14.7 U · ROI +10.7 %",
              "aviso": None}}})))
check("y la voz lo dice en una frase",
      "Evidencia todavía escasa" in texto_voz(cand_conf, "UTC", _ahora))
radar = {"id": 9, "tipo": "RADAR_VOZ", "jugador1": "J1", "jugador2": "J2",
         "fecha_partido": "2026-09-23T10:00:00+00:00",
         "datos": {"jugador": "J2", "cuota": 2.45, "triggers": ["SALUD", "OPOSICION"],
                   "refuerzos": ["el rival llega con 3 partidos más en 8 días"],
                   "investigacion": {"estado": "SIN_EXPLICACION_DOCUMENTADA"},
                   "confianza": {"nivel": "EXPERIMENTAL", "etiqueta": "🔴 EVIDENCIA: EXPERIMENTAL",
                                 "texto": "2 casos comparables cerrados", "rendimiento": "1-1 · +0.5 U",
                                 "aviso": "Regla todavía con muestra pequeña."}}}
under = {"id": 5, "tipo": "UNDER_PICK", "jugador1": "Player A", "jugador2": "Player B",
         "datos": {"linea": 18.5, "cuota": 1.78, "contexto": {
             "similares": {"n": 10, "under": 4, "over": 6, "pct": 40, "muestra_chica": True},
             "linea": {"valor": 18.5, "n": 0}, "piedra": {"condicion": "cuota UNDER 1.85-1.89",
                                                          "n": 10, "under": 4, "over": 6, "pct": 40}}}}
vl = {"id": 11, "tipo": "VENTAJA_LEVE", "jugador1": "Jugadora A", "jugador2": "Jugadora B",
      "datos": {"regla": "VL02", "texto_discord":
                "🔥 VENTAJA LEVE — TENDENCIA UNDER\n🎾 Jugadora A vs Jugadora B\n"
                "📉 Favorito leve perdió 1.er set: 2-6\n📊 Elo 58% · FTR 61%\n"
                "📚 Histórico: 79% terminó en 2 sets\n🧪 Muestra: 14 partidos",
                "texto_voz": "Ventaja Leve. Jugadora A perdió el primer set seis-dos. "
                             "Tendencia Under. Histórico, setenta y nueve por ciento."}}
cvl = texto_canal(vl)
check("Ventaja Leve: el texto de RankingFTR tal cual, con el encabezado en negrita",
      cvl.startswith("**🔥 VENTAJA LEVE — TENDENCIA UNDER**") and "🧪 Muestra: 14 partidos" in cvl, cvl)
check("Ventaja Leve: la voz tal cual, corta, sin Elo/FTR/muestra/cuota",
      texto_voz(vl, "UTC", _ahora) == vl["datos"]["texto_voz"])
pfs = {"id": 12, "tipo": "PASAN_FILTRO", "jugador1": "A", "jugador2": "B",
       "datos": {"texto_discord": "🔥 PASAN FILTRO — TENDENCIA OVER\n\n🎾 A vs B",
                 "texto_voz": "Pasan el filtro. A perdió el primer set seis-tres."}}
check("Pasan Filtro: mismo tratamiento que Ventaja Leve",
      texto_canal(pfs).startswith("**🔥 PASAN FILTRO — TENDENCIA OVER**")
      and texto_voz(pfs, "UTC", _ahora) == pfs["datos"]["texto_voz"])
cu = texto_canal(under)
check("el UNDER muestra su contexto, la muestra chica y la piedra",
      "FULLTENNIS — UNDER" in cu and "Línea 18.5 · UNDER @1.78" in cu
      and "4-6 UNDER (40%) · n=10 ⚠️ muestra pequeña" in cu
      and "🪨 Piedra histórica: cuota UNDER 1.85-1.89" in cu, cu)
check("y sin historial no inventa porcentajes",
      "sin historial suficiente" in texto_canal({**under, "datos": {"linea": 18.5, "cuota": 1.78,
                                                                    "contexto": {}}}))
radar["datos"]["decision"] = {"etiqueta": "🔥 VALOR CONFIRMADO"}
radar["datos"]["diagnostico"] = {
    "señalado": "J2", "rival": "J1", "precio": {"mercado_pct": 16, "modelos_pct": [70, 73], "anomalia_pp": 54},
    "salud": {"J1": {"mto_45d": 1}, "J2": {"mto_45d": 0}},
    "carga": {"J1": {"partidos_7d": 4}, "J2": {"partidos_7d": 1}},
    "oposicion": {"J1": {"percentil": 40}, "J2": {"percentil": 78}}}
cr = texto_canal(radar)
check("la señal se entiende en diez segundos: jugador, cuota, nuestros números y el mercado",
      "FULLTENIS · VALOR DETECTADO" in cr and "J2 @2.45" in cr and "~70 %" in cr and "Mercado: 16 %" in cr,
      cr[:200])
check("y dice en una línea cada área, con ⚠️ cuando algo va en contra",
      "⚡ Carga: J1 llega con 3 partidos más" in cr and "🎾 Oposición: viene enfrentando mejores rivales" in cr
      and "🩹 Salud: J1 viene de un problema físico reciente" in cr
      and "🌐 Investigación: no encontramos información pública" in cr, cr)
check("cierra con el veredicto, la evidencia y la aclaración",
      "🔥 VALOR CONFIRMADO POR FULLTENIS" in cr and "🔴 EVIDENCIA: EXPERIMENTAL" in cr
      and "no recomendación de apuesta" in cr)
check("la voz dice el jugador, la cuota y las dos cifras",
      "Valor detectado: J2" in texto_voz(radar, "UTC", _ahora)
      and "70 por ciento" in texto_voz(radar, "UTC", _ahora))
vc = texto_voz(cand, "UTC", _ahora)
check("CANDIDATO por voz: lo urgente, la cuota, mercado contra FTR y los apoyos",
      vc.startswith("Atención FullTennis. En diez minutos empieza Carlos Ruiz contra Pedro Soto. "
                    "Candidato: Pedro Soto paga dos punto seis. El mercado le da treinta y ocho por ciento; "
                    "el FTR, cincuenta y seis por ciento.")
      and "A favor: el Elo lo acompaña y Carlos Ruiz llega más cargado." in vc and "MTO" not in vc, vc)
rev["datos"]["eventos"] = [{"tipo": "RETIRO", "fecha": "2026-09-12"}]
check("un retiro se dice 'vuelve tras retirarse'", "vuelve tras retirarse hace diez días"
      in texto_voz(rev, "UTC", _ahora) and "🔄 Carlos Ruiz vuelve tras retirarse el 12/09" in texto_canal(rev))

print(f"\n{'─' * 60}")
if _fallos:
    print(f"{_ok} OK, {len(_fallos)} FALLAS:")
    for f in _fallos:
        print(f"  - {f}")
    sys.exit(1)
print(f"{_ok} comprobaciones OK.")
