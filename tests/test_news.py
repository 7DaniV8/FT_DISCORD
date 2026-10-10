"""
tests/test_news.py — 📰 FT NEWS INTELLIGENCE en Discord (08/10/2026): las
señales NEWS_VALOR (🔥) salen en el acto con el texto y la voz de FT_NEWS;
v11: las tres (🔥 🔎 📰) nacen encendidas; Admin las gobierna. Sin red.
"""
from __future__ import annotations

import asyncio
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
from ft_discord.anunciador import Anunciador  # noqa: E402
from ft_discord.fuente import Fuente  # noqa: E402
from ft_discord.textos import texto_canal, texto_voz  # noqa: E402

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
TEXTO = ("🔥 FULLTENIS · POSIBLE VALOR POR NOTICIAS\nChallenger Lima · · 2026-10-09T11:00\n"
         "**Juan Perez** (UTR 12.90) vs **Luca Rossi** (UTR 12.40) · cuota Juan Perez 1.40 / Luca Rossi 2.90\n"
         "Noticia: *\"molestias en la muñeca derecha\"* — declaración — 2026-10-07 — tennistourtalk.com — Nivel B\n"
         "Lectura: Por UTR X debería ser FAVORITO; el mercado lo tiene FAVORITO; la noticia es NEGATIVA para X. "
         "El mercado no parece haberla descontado.\nLo que no sabemos: gravedad real y estado físico actual; esto no es una recomendación.")


def senal(i, tipo):
    return {"id": i, "tipo": tipo, "creado_en": AHORA.isoformat(), "resumen": f"🔥 FT NEWS: Juan Perez vs Luca Rossi",
            "jugador1": "Juan Perez", "jugador2": "Luca Rossi", "torneo": "Challenger Lima",
            "fecha_partido": (AHORA + timedelta(hours=15)).isoformat(), "hora_conocida": 1,
            "datos": {"texto_discord": TEXTO, "texto_voz": "Juan Perez contra Luca Rossi. molestias en la muñeca derecha",
                      "jugador": "Juan Perez", "rival": "Luca Rossi", "news_id": 7}}


SENALES: list = []
CONFIG: dict = {"estado": 404}


def ft_intel(req):
    if req.url.path == "/discord-config":
        if CONFIG["estado"] != 200:
            return httpx.Response(404, json={"detail": "x"})
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


print("1 · Textos")
t = texto_canal(senal(1, "NEWS_VALOR"))
check("primera línea en negrita, el cuerpo de FT_NEWS tal cual y el pie", t.startswith("**🔥 FULLTENIS · POSIBLE VALOR POR NOTICIAS**")
      and "Nivel B" in t and "no es una recomendación" in t and "<t:" in t, t)
check("sin porcentajes ni 'probabilidad' en el texto", "%" not in t and "probabilidad" not in t.lower())
v = texto_voz(senal(1, "NEWS_VALOR"), "UTC", AHORA)
check("voz: 'Atención FullTennis. Noticia.' + la frase de FT_NEWS", v == "Atención FullTennis. Noticia. Juan Perez contra Luca Rossi. molestias en la muñeca derecha", v)
check("v11: de fábrica suenan las tres de FT NEWS (texto y voz)",
      {"NEWS_VALOR", "NEWS_INTERESANTE", "NEWS_NOTICIA"} <= config.TIPOS_TEXTO
      and {"NEWS_VALOR", "NEWS_INTERESANTE", "NEWS_NOTICIA"} <= config.TIPOS_VOZ)


async def escenario():
    fuente = Fuente("https://ft-intel.test", "secreto", transport=httpx.MockTransport(ft_intel))
    a = Anunciador(fuente, publicar, encolar, "UTC", config.TIPOS_TEXTO, config.TIPOS_VOZ, 30, reloj=lambda: AHORA)
    await a.arrancar(0)
    print("\n2 · Sin configuración remota (v11): 🔥 y 🔎 salen en el acto, texto y voz")
    SENALES.append(senal(1, "NEWS_VALOR"))
    SENALES.append(senal(2, "NEWS_INTERESANTE"))
    r = await a.ciclo()
    check("🔥 y 🔎 publicadas y dichas de fábrica", r["texto"] == 2 and r["voz"] == 2 and len(publicados) == 2
          and dichos[-1].startswith("Atención FullTennis. Noticia."), (r, dichos))
    print("\n3 · Admin enciende 🔎 y cambia el encabezado de 🔥")
    CONFIG.update(estado=200, cuerpo={"version": 1, "pausa": False, "tipos": {
        "NEWS_VALOR": {"texto": True, "voz": True, "encabezado": "🔥 FULLTENIS · NOTICIA CON VALOR", "voz_inicio": None},
        "NEWS_INTERESANTE": {"texto": True, "voz": False, "encabezado": None, "voz_inicio": None}}})
    SENALES.append(senal(3, "NEWS_VALOR"))
    SENALES.append(senal(4, "NEWS_INTERESANTE"))
    n_t, n_v = len(publicados), len(dichos)
    r = await a.ciclo()
    check("las dos salen por texto; solo 🔥 por voz", len(publicados) - n_t == 2 and len(dichos) - n_v == 1, (r, len(publicados) - n_t))
    check("el encabezado de Admin reemplaza la primera línea", any(p.startswith("**🔥 FULLTENIS · NOTICIA CON VALOR**") for p in publicados[n_t:]), publicados[n_t:])
    print("\n4 · v11: una 🔥 EXPERIMENTAL no pierde la etiqueta por el encabezado personalizado")
    exp = senal(5, "NEWS_VALOR")
    exp["datos"] = {**exp["datos"], "experimental": True,
                    "texto_discord": "🔥 FULLTENNIS · POSIBLE VALOR EXPERIMENTAL\n🎾 Luca Rossi vs Juan Perez\n🟢 JUGADOR FAVORECIDO: LUCA ROSSI\n💰 CUOTA: 2.90\n⚠️ Posible oportunidad sin validar estadísticamente."}
    SENALES.append(exp)
    n_t = len(publicados)
    await a.ciclo()
    check("la primera línea sigue diciendo EXPERIMENTAL", publicados[n_t].startswith("**🔥 FULLTENNIS · POSIBLE VALOR EXPERIMENTAL**")
          and "JUGADOR FAVORECIDO: LUCA ROSSI" in publicados[n_t], publicados[n_t:])


asyncio.run(escenario())
print(f"\n{_ok} OK" + (f", {len(_fallos)} FALLAS: {_fallos}" if _fallos else ""))
sys.exit(1 if _fallos else 0)
