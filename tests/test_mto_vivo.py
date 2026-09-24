#!/usr/bin/env python3
"""tests/test_mto_vivo.py — 🩹 Tiempo médico en vivo (24/09/2026): la frase exacta."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
for k in ("DISCORD_TOKEN", "DISCORD_CANAL_TEXTO_ID", "FT_INTEL_URL", "FTR_INTERNAL_SECRET"):
    os.environ.setdefault(k, "1")

from ft_discord import config  # noqa: E402
from ft_discord.textos import nombre_para_voz, texto_canal, texto_voz  # noqa: E402

fallos = []


def check(nombre, cond, det=""):
    print(("  OK    " if cond else "  FALLA ") + nombre + ("" if cond else f": {det}"))
    if not cond:
        fallos.append(nombre)


s = {"tipo": "MTO_VIVO", "id": 1, "jugador1": "Kovacs L", "jugador2": "Sziklai E",
     "datos": {"jugador": "Kovacs L", "rival": "Sziklai E", "torneo": "ITF W35"}}
v = texto_voz(s, "UTC")
check("la frase pedida (sin cuota)", v == "Atención FullTennis. Tiempo médico solicitado por Kovacs.", v)
s2 = {**s, "datos": {**s["datos"], "cuota_rival": "2.97"}}
v2 = texto_voz(s2, "UTC")
check("con cuota: la frase exacta del pedido",
      v2 == "Atención FullTennis. Tiempo médico solicitado por Kovacs. Rival con cuota 2.97.", v2)
check("el texto también muestra la cuota", "Cuota del rival: **2.97**" in texto_canal(s2))
s3 = {**s, "datos": {**s2["datos"], "tiene_video": True}}
v3 = texto_voz(s3, "UTC")
check("con video lo dice al final",
      v3 == "Atención FullTennis. Tiempo médico solicitado por Kovacs. Rival con cuota 2.97. "
            "Partido con video.", v3)
check("y el texto lo muestra", "📺 **Con video**" in texto_canal(s3))
check("sin video no dice nada de video", "video" not in v2)

# Velocidad rechazada por la voz (Chirp 3 HD): reintenta sin velocidad.
import httpx  # noqa: E402
from ft_discord.tts import TTSGoogle  # noqa: E402
pedidos = []


def google(req):
    import json as _j
    cuerpo = _j.loads(req.content)
    pedidos.append(cuerpo["audioConfig"])
    if "speakingRate" in cuerpo["audioConfig"]:
        return httpx.Response(400, json={"error": {"message": "speaking rate not supported"}})
    return httpx.Response(200, json={"audioContent": "T2dnUw=="})


t = TTSGoogle("CLAVE", "es-US", "es-US-Chirp3-HD-Charon", 1.4, transport=httpx.MockTransport(google))
check("si rechaza la velocidad, igual genera el audio", t.sintetizar("hola") is not None)
check("y no vuelve a pedir velocidad", t.sintetizar("hola") is not None
      and "speakingRate" not in pedidos[-1] and len(pedidos) == 3, str(pedidos))
c = texto_canal(s)
check("el texto dice quién y contra quién",
      "TIEMPO MÉDICO" in c and "Kovacs L" in c and "Sziklai E" in c, c)
check("nombre completo queda igual", nombre_para_voz("Carlos Alcaraz") == "Carlos Alcaraz")
check("varias iniciales sueltas se quitan", nombre_para_voz("Kovacs-Sebestyen L. M") == "Kovacs-Sebestyen")
check("MTO_VIVO va por texto y por voz por defecto",
      "MTO_VIVO" in config.TIPOS_TEXTO and "MTO_VIVO" in config.TIPOS_VOZ)
print()
if fallos:
    print(f"FALLARON {len(fallos)}: {fallos}")
    sys.exit(1)
print("Pasaron todos.")
