"""
tests/test_news_admin.py — v16 (13/10/2026, Rubén: "las pruebas técnicas solo a administradores";
"no quiero que aparezca como enviada una alerta que FT Discord no haya confirmado").

  - una señal con datos.solo_admin (la 🧪 PRUEBA de FT NEWS) sale SOLO por el canal de
    administradores, sin voz y sin encabezado personalizado;
  - sin canal de administradores (DISCORD_CANAL_ADMIN_ID) NO sale por ningún lado;
  - las tres de FT NEWS devuelven ACUSE a FT Intelligence con el canal (comunidad | admin)
    o el motivo si no se publicaron;
  - los demás tipos no cambian. Sin red.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DISCORD_TOKEN", "x")
os.environ.setdefault("FT_INTEL_URL", "https://ft-intel.test")
os.environ.setdefault("FTR_INTERNAL_SECRET", "secreto")

from ft_discord import config  # noqa: E402
from ft_discord.anunciador import ACUSE_TIPOS, Anunciador  # noqa: E402
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


AHORA = datetime(2026, 10, 13, 16, 0, tzinfo=timezone.utc)


def senal(i, tipo, texto, solo_admin=False):
    return {"id": i, "tipo": tipo, "creado_en": AHORA.isoformat(), "resumen": "📰 FT NEWS",
            "jugador1": "Juan Perez", "jugador2": None, "torneo": None,
            "fecha_partido": (AHORA + timedelta(hours=5)).isoformat(), "hora_conocida": 1,
            "datos": {"texto_discord": texto, "texto_voz": "Juan Perez. molestias", "news_id": i,
                      "solo_admin": solo_admin, "prueba": solo_admin}}


PRUEBA = "🧪 PRUEBA DE FT NEWS — NO ES UNA OPORTUNIDAD\nMensaje de comprobación pedido por ruben."
REAL = "📰 FULLTENNIS · NOTICIA IMPORTANTE\n🎾 Juan Perez"
SENALES: list = []
ACUSES: list = []
CONFIG = {"version": 1, "pausa": False, "tipos": {
    "NEWS_NOTICIA": {"texto": True, "voz": True, "encabezado": "📰 FULLTENNIS · ÚLTIMA HORA", "voz_inicio": None},
    "NEWS_VALOR": {"texto": False, "voz": False, "encabezado": None, "voz_inicio": None}}}


def ft_intel(req):
    if req.url.path == "/discord-config":
        return httpx.Response(200, json=CONFIG)
    if req.url.path == "/senales/acuse":
        ACUSES.extend(json.loads(req.content)["acuses"])
        return httpx.Response(200, json={"ok": True})
    desde = int(req.url.params["desde_id"])
    return httpx.Response(200, json={"senales": [s for s in SENALES if s["id"] > desde],
                                     "ultimo_id": max([s["id"] for s in SENALES] or [0])})


async def escenario():
    print("1 · Con canal de administradores")
    comunidad, admin, voces = [], [], []

    async def pub(t):
        comunidad.append(t)

    async def pub_admin(t):
        admin.append(t)
    fuente = Fuente("https://ft-intel.test", "secreto", transport=httpx.MockTransport(ft_intel))
    a = Anunciador(fuente, pub, lambda v: voces.append(v) or True, "UTC", config.TIPOS_TEXTO, config.TIPOS_VOZ, 30,
                   reloj=lambda: AHORA, publicar_admin=pub_admin)
    await a.arrancar(0)
    SENALES.extend([senal(1, "NEWS_NOTICIA", PRUEBA, solo_admin=True), senal(2, "NEWS_NOTICIA", REAL),
                    senal(3, "NEWS_VALOR", "🔥 FULLTENNIS · POSIBLE VALOR EXPERIMENTAL\n...")])
    r = await a.ciclo()
    check("la PRUEBA sale SOLO por el canal de administradores, con su primera línea (el encabezado de Admin no la pisa)",
          len(admin) == 1 and admin[0].startswith("**🧪 PRUEBA DE FT NEWS — NO ES UNA OPORTUNIDAD**")
          and not any("PRUEBA" in t for t in comunidad), (admin, comunidad))
    check("la PRUEBA no se dice por voz (el canal de voz es el de la comunidad): solo suena la 📰 real",
          len(voces) == 1 and "Prueba" not in voces[0], voces)
    check("la 📰 real sale en la comunidad (con el encabezado de Admin), texto y voz",
          len(comunidad) == 1 and comunidad[0].startswith("**📰 FULLTENNIS · ÚLTIMA HORA**"), comunidad)
    ac = {x["senal_id"]: x for x in ACUSES}
    check("acuse de las tres de FT NEWS con el canal o el motivo",
          ac[1]["canal"] == "admin" and ac[1]["texto"] is True and ac[1]["voz"] is False
          and ac[2]["canal"] == "comunidad" and ac[2]["texto"] is True and ac[2]["voz"] is True
          and ac[3]["texto"] is False and "apagado" in ac[3]["motivo"], ac)
    check("contadores: 1 por admin, 1 por la comunidad", r.get("admin") == 1 and r["texto"] == 1, r)
    await fuente.cerrar()

    print("\n2 · SIN canal de administradores (falta DISCORD_CANAL_ADMIN_ID)")
    SENALES.clear()
    ACUSES.clear()
    comunidad2, voces2 = [], []

    async def pub2(t):
        comunidad2.append(t)
    fuente = Fuente("https://ft-intel.test", "secreto", transport=httpx.MockTransport(ft_intel))
    b = Anunciador(fuente, pub2, lambda v: voces2.append(v) or True, "UTC", config.TIPOS_TEXTO, config.TIPOS_VOZ, 30,
                   reloj=lambda: AHORA)
    await b.arrancar(0)
    SENALES.append(senal(10, "NEWS_NOTICIA", PRUEBA, solo_admin=True))
    await b.ciclo()
    check("la PRUEBA no sale por ningún lado (nunca cae en la comunidad)", comunidad2 == [] and voces2 == [], (comunidad2, voces2))
    check("y el acuse lo dice", ACUSES and ACUSES[0]["texto"] is False and "DISCORD_CANAL_ADMIN_ID" in ACUSES[0]["motivo"], ACUSES)
    await fuente.cerrar()

    print("\n3 · Los demás tipos no cambian")
    check("acuse para Implicación, las tres de FT NEWS y (v17) el Under",
          set(ACUSE_TIPOS) == {"IMPLICACION", "NEWS_VALOR", "NEWS_INTERESANTE", "NEWS_NOTICIA", "UNDER_PICK", "MOVIMIENTO_RENTABLE"})
    check("sin DISCORD_CANAL_ADMIN_ID el canal de admin queda en 0", config.CANAL_ADMIN_ID == 0)


asyncio.run(escenario())
print(f"\n{_ok} OK" + (f", {len(_fallos)} FALLAS: {_fallos}" if _fallos else ""))
sys.exit(1 if _fallos else 0)
