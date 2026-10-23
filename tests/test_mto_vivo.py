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

# 22/10/2026: MTO anteriores (cuántos en 4 semanas y hace cuántos días), en texto y en voz
s4 = {**s, "datos": {**s2["datos"], "mto_previos": "**2 MTO** en las últimas 4 semanas · el último **hace 3 días**",
                     "mto_previos_voz": "Pidió 2 tiempos médicos en las últimas 4 semanas, el último hace 3 días."}}
check("texto: 🕒 2 MTO en 4 semanas · el último hace 3 días",
      "🕒 **2 MTO** en las últimas 4 semanas · el último **hace 3 días**" in texto_canal(s4), texto_canal(s4))
v4 = texto_voz(s4, "UTC")
check("voz: los MTO anteriores NO se leen (solo en el texto)",
      v4 == "Atención FullTennis. Tiempo médico solicitado por Kovacs. Rival con cuota 2.97.", v4)
s5 = {**s, "datos": {**s["datos"], "mto_previos": "Último MTO **hace 194 días**",
                     "mto_previos_voz": "Su último tiempo médico fue hace 194 días."}}
check("solo días (sin fecha)", "🕒 Último MTO **hace 194 días**" in texto_canal(s5)
      and "194" not in texto_voz(s5, "UTC") and "/" not in texto_canal(s5).split("🕒")[1])
check("sin el dato, nada cambia", "🕒" not in texto_canal(s) and "tiempos médicos" not in texto_voz(s, "UTC"))

# 22/10/2026: cuotas pre-partido y la favorita
s6 = {**s, "datos": {**s2["datos"], "genero": "F", "cuota_pre_jugador": 1.65, "cuota_pre_rival": 2.2,
                     "favorito_pre": "jugador"}}
c6 = texto_canal(s6)
check("texto: 📊 Pre-partido, una línea por jugadora (formato de Rubén)",
      "📊 Pre-partido:\n• Kovacs L **1.65**\n• Sziklai E **2.20**" in c6, c6)
check("la estrella ⭐ marca a la favorita PRE-PARTIDO en la línea del partido", "⭐ __**Kovacs L**__" in c6, c6)
v6 = texto_voz(s6, "UTC")
check("voz: las cuotas pre-partido NO se leen (solo en el texto)",
      v6 == "Atención FullTennis. Tiempo médico solicitado por Kovacs. Rival con cuota 2.97.", v6)
s7 = {**s, "datos": {**s["datos"], "genero": "M", "cuota_pre_jugador": 3.5, "cuota_pre_rival": 1.3,
                     "favorito_pre": "rival"}}
check("si el favorito era el rival: la ⭐ lo marca", "⭐ __**Sziklai E**__" in texto_canal(s7), texto_canal(s7))
check("sin cuotas pre-partido, nada cambia", "Pre-partido" not in texto_canal(s) and "Antes del partido" not in texto_voz(s, "UTC"))

# 22/10/2026: el mensaje completo con el formato de Rubén; 🩹 momento SOLO si fue entre puntos
s8 = {**s, "jugador1": "Michiru Furuya", "jugador2": "Junri Namigata",
      "datos": {"jugador": "Michiru Furuya", "rival": "Junri Namigata", "torneo": "UTR Pro W. Saitama", "genero": "F",
                "cuota_pre_jugador": 1.65, "cuota_pre_rival": 2.2, "favorito_pre": "jugador", "cuota_rival": "1.98",
                "momento_mto": "entre_puntos", "momento_mto_texto": "Lo pidió **entre puntos** (game 15-30)",
                "momento_mto_voz": "Lo pidió entre puntos, en medio de un game.",
                "mto_previos": "**1 MTO** en las últimas 4 semanas (**hace 3 días**)",
                "mto_previos_voz": "Pidió 1 tiempo médico en las últimas 4 semanas, hace 3 días."}}
c8 = texto_canal(s8)
check("entre puntos: partido · pre-partido · cuota del rival · 🩹 entre puntos · 🕒 MTO previos", c8 == "\n".join([
    "🩹 **TIEMPO MÉDICO** — solicitado por **Michiru Furuya**",
    "🎾 ⭐ __**Michiru Furuya**__ vs Junri Namigata · UTR Pro W. Saitama",
    "📊 Pre-partido:", "• Michiru Furuya **1.65**", "• Junri Namigata **2.20**",
    "💲 Cuota del rival: **1.98**",
    "🩹 Lo pidió **entre puntos** (game 15-30)",
    "🕒 **1 MTO** en las últimas 4 semanas (**hace 3 días**)"]), c8)
check("voz corta: solo quién lo pidió, la cuota del rival y que fue entre puntos",
      texto_voz(s8, "UTC") == "Atención FullTennis. Tiempo médico solicitado por Michiru Furuya. Rival con cuota 1.98. "
      "Lo pidió entre puntos.", texto_voz(s8, "UTC"))
s10 = {**s8, "datos": {**s8["datos"], "tiene_video": True}}
check("voz corta con video: quién · cuota del rival · entre puntos · video",
      texto_voz(s10, "UTC") == "Atención FullTennis. Tiempo médico solicitado por Michiru Furuya. Rival con cuota 1.98. "
      "Lo pidió entre puntos. Partido con video.", texto_voz(s10, "UTC"))
for clave, txt in (("fin_set", "Lo pidió **al terminar el set**"), ("cambio_lado", "Lo pidió **en el cambio de lado**"),
                   ("entre_games", "Lo pidió **entre games, sin cambio de lado**")):
    s9 = {**s8, "datos": {**s8["datos"], "momento_mto": clave, "momento_mto_texto": txt, "momento_mto_voz": "Lo pidió x."}}
    check(f"{clave}: no se dice nada (ni texto ni voz)", "Lo pidió" not in texto_canal(s9)
          and "Lo pidió" not in texto_voz(s9, "UTC"), texto_canal(s9))

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
