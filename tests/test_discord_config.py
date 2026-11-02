"""
tests/test_discord_config.py — 📣 Admin → Discord (08/10/2026): la configuración
que decide RankingFTR llega vía FT Intelligence y el bot la aplica antes de
escribir o hablar. Sin red: FT Intelligence es un MockTransport.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DISCORD_TOKEN", "x")
os.environ.setdefault("FT_INTEL_URL", "https://ft-intel.test")
os.environ.setdefault("FTR_INTERNAL_SECRET", "secreto")

from ft_discord.anunciador import Anunciador  # noqa: E402
from ft_discord.fuente import Fuente  # noqa: E402

_ok, _fallos = 0, []


def check(nombre, cond, det=""):
    global _ok
    if cond:
        _ok += 1
        print(f"  OK    {nombre}")
    else:
        _fallos.append(nombre)
        print(f"  FALLA {nombre}" + (f": {det}" if det else ""))


AHORA = datetime(2026, 10, 8, 20, 0, tzinfo=timezone.utc)
SENALES: list = []
CONFIG: dict = {"estado": 404}


def senal(i, tipo):
    return {"id": i, "tipo": tipo, "creado_en": AHORA.isoformat(), "resumen": f"{tipo} #{i}",
            "jugador1": "Ana Uno", "jugador2": "Bea Dos", "torneo": "W15 X", "fecha_partido": AHORA.isoformat(),
            "hora_conocida": 1, "datos": {"texto_discord": "💎 FULLTENIS · CUOTA MUY MAL PUESTA\n🎾 ⭐ __**Ana Uno**__ vs Bea Dos\n🚨 EDGE +20 PP",
                                          "texto_voz": "Cuota muy mal puesta. Ana Uno.", "detectado_en": AHORA.isoformat(),
                                          "jugador": "Ana Uno", "rival": "Bea Dos"}}


def ft_intel(req: httpx.Request) -> httpx.Response:
    if req.url.path == "/discord-config":
        if CONFIG["estado"] != 200:
            return httpx.Response(CONFIG["estado"], json={"detail": "sin configuración"})
        return httpx.Response(200, json=CONFIG["cuerpo"])
    desde = int(req.url.params["desde_id"])
    return httpx.Response(200, json={"senales": [s for s in SENALES if s["id"] > desde],
                                     "ultimo_id": max([s["id"] for s in SENALES] or [0])})


publicados, dichos = [], []


async def publicar(t):
    publicados.append(t)


def encolar(t):
    dichos.append(t)
    return True


async def escenario():
    fuente = Fuente("https://ft-intel.test", "secreto", transport=httpx.MockTransport(ft_intel))
    # Railway: CUOTA_MAL_PUESTA por texto y voz; RECORDADOR solo texto; MTO_VIVO nada.
    a = Anunciador(fuente, publicar, encolar, "UTC", {"CUOTA_MAL_PUESTA", "RECORDADOR"},
                   {"CUOTA_MAL_PUESTA"}, 30, reloj=lambda: AHORA)
    await a.arrancar(0)

    print("1 · Sin configuración remota (404): mandan las variables de Railway")
    SENALES.append(senal(1, "CUOTA_MAL_PUESTA"))
    r = await a.ciclo()
    check("sin /discord-config el bot sigue igual: texto y voz", r["texto"] == 1 and r["voz"] == 1 and a.config_remota is None, r)
    check("el encabezado es el de RankingFTR, sin tocar", publicados[-1].startswith("**💎 FULLTENIS · CUOTA MUY MAL PUESTA**"), publicados[-1])

    print("\n2 · Admin apaga la voz de Cuota Mal Puesta y enciende MTO en vivo")
    CONFIG.update(estado=200, cuerpo={"version": 1, "pausa": False, "tipos": {
        "CUOTA_MAL_PUESTA": {"texto": True, "voz": False, "encabezado": None, "voz_inicio": None},
        "MTO_VIVO": {"texto": True, "voz": True, "encabezado": None, "voz_inicio": None},
        "RECORDADOR": {"texto": False, "voz": False, "encabezado": None, "voz_inicio": None}}})
    SENALES.append(senal(2, "CUOTA_MAL_PUESTA"))
    SENALES.append({**senal(3, "MTO_VIVO"), "datos": {"jugador_mto": "Ana Uno", "rival": "Bea Dos", "texto_discord": "x", "texto_voz": "x"}})
    SENALES.append(senal(4, "RECORDADOR"))
    n_t, n_v = len(publicados), len(dichos)
    r = await a.ciclo()
    check("Cuota Mal Puesta: texto sí, voz no (Admin manda sobre Railway)",
          r["texto"] >= 1 and len(dichos) - n_v == 1, (r, len(dichos) - n_v))
    check("MTO en vivo: encendido desde Admin aunque Railway no lo tenía (texto y voz)",
          len(publicados) - n_t == 2 and any("Tiempo médico" in d for d in dichos[n_v:]), (len(publicados) - n_t, dichos[n_v:]))
    check("Recordador apagado desde Admin: no sale, cuenta como apagada", r["apagadas"] == 1, r)

    print("\n3 · Encabezado y frase de voz personalizados")
    CONFIG["cuerpo"]["tipos"]["CUOTA_MAL_PUESTA"] = {"texto": True, "voz": True, "encabezado": "🔥 FULLTENIS PREMIUM · CUOTA MAL PUESTA",
                                                     "voz_inicio": "Atención FullTennis"}
    SENALES.append(senal(5, "CUOTA_MAL_PUESTA"))
    await a.ciclo()
    check("la primera línea del mensaje es el encabezado de Admin, en negrita",
          publicados[-1].split("\n")[0] == "**🔥 FULLTENIS PREMIUM · CUOTA MAL PUESTA**", publicados[-1].split("\n")[0])
    check("el resto del mensaje queda igual", "🚨 EDGE +20 PP" in publicados[-1])
    check("la voz arranca con la frase de Admin", dichos[-1].startswith("Atención FullTennis. "), dichos[-1])

    print("\n4 · Pausa general")
    CONFIG["cuerpo"]["pausa"] = True
    SENALES.append(senal(6, "CUOTA_MAL_PUESTA"))
    n_t, n_v = len(publicados), len(dichos)
    r = await a.ciclo()
    check("en pausa no sale nada (ni texto ni voz)", len(publicados) == n_t and len(dichos) == n_v and r["apagadas"] == 1, r)
    CONFIG["cuerpo"]["pausa"] = False
    SENALES.append(senal(7, "CUOTA_MAL_PUESTA"))
    r = await a.ciclo()
    check("al reanudar, las nuevas salen; la que llegó en pausa no se repite", len(publicados) == n_t + 1, r)

    print("\n5 · FT Intelligence deja de responder la configuración")
    CONFIG["estado"] = 500
    SENALES.append(senal(8, "RECORDADOR"))
    r = await a.ciclo()
    check("se conserva la última configuración buena (Recordador sigue apagado)", r["apagadas"] == 1 and a.config_remota is not None, r)

    print("\n6 · Un tipo que Admin no conoce sigue con Railway")
    CONFIG.update(estado=200)
    CONFIG["cuerpo"]["tipos"].pop("RECORDADOR")
    SENALES.append(senal(9, "RECORDADOR"))
    n_t = len(publicados)
    r = await a.ciclo()
    check("Recordador no está en la configuración → Railway (solo texto)", len(publicados) == n_t + 1 and r["voz"] == 0, r)


asyncio.run(escenario())
print(f"\n{'─' * 60}")
if _fallos:
    print(f"{_ok} OK, {len(_fallos)} FALLAS: {_fallos}")
    sys.exit(1)
print(f"{_ok} comprobaciones OK.")
