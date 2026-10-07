#!/usr/bin/env python3
"""
tests/test_cuota_mal_puesta.py — 🚨 Cuota Mal Puesta en Discord (07/10/2026).

  1. Viene por defecto en los tipos de texto y de voz.
  2. Se anuncia cuando el partido EMPIEZA: el texto del canal es el de
     RankingFTR (encabezado en negrita) + un pie "Empezó el partido · señal
     detectada hace X" (la cuota del mensaje es la de ese momento), sin
     agregar un "favorito" que no corresponde.
  3. La voz dice qué partido empezó y después la frase de RankingFTR.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
for k in ("FT_DISCORD_TIPOS_TEXTO", "FT_DISCORD_TIPOS_VOZ"):
    os.environ.pop(k, None)

from ft_discord import config  # noqa: E402
from ft_discord.textos import texto_canal, texto_voz  # noqa: E402

ok = fallas = 0


def check(nombre, cond, det=""):
    global ok, fallas
    ok, fallas = (ok + 1, fallas) if cond else (ok, fallas + 1)
    print(f"  {'OK   ' if cond else 'FALLA'} {nombre}" + (f" -- {det}" if det else ""))


TXT = ("💎 FULLTENIS · CUOTA MUY MAL PUESTA\n🎾 Pedro Dos vs ⭐ __**Carlos Uno**__\n"
       "💰 Cuota mercado: 5.00\n🚨 CUOTA MAL PUESTA: +21.5 PP\n💎 PREMIUM")
VOZ = "Cuota muy mal puesta. Carlos Uno. Diferencia de veintidós puntos. Premium."
S = {"id": 7, "tipo": "CUOTA_MAL_PUESTA", "jugador1": "Carlos Uno", "jugador2": "Pedro Dos",
     "torneo": "M25 X", "fecha_partido": "2026-10-07T22:00:00+00:00", "resumen": "💎 ...",
     "creado_en": "2026-10-07T20:00:00+00:00",
     "datos": {"nivel": "PREMIUM", "candidato": "Carlos Uno", "cuota": 5.0,
               "detectado_en": "2026-10-07T17:00:00+00:00", "inicio_real": "2026-10-07T22:01:00+00:00",
               "en_vivo": True, "texto_discord": TXT, "texto_voz": VOZ},
     "foto": {"cuota_mal_puesta": {"player_a": "Carlos Uno", "player_b": "Pedro Dos"}}}

print("\n1. Tipos")
check("texto por defecto", "CUOTA_MAL_PUESTA" in config.TIPOS_TEXTO)
check("voz por defecto", "CUOTA_MAL_PUESTA" in config.TIPOS_VOZ)
print("\n2. Texto")
t = texto_canal(S)
check("encabezado en negrita", t.split("\n")[0] == "**💎 FULLTENIS · CUOTA MUY MAL PUESTA**", t)
check("el cuerpo de RankingFTR tal cual", t.split("\n")[1:len(TXT.split("\n"))] == TXT.split("\n")[1:], t)
check("pie: empezó el partido + cuándo se detectó (hora de Discord)",
      t.split("\n")[-1].startswith("▶️ **Empezó el partido** · señal detectada <t:1791392400:R>"), t)
check("no agrega 'Favorito:'", "Favorito:" not in t)
print("\n3. Voz")
check("dice qué empezó y después la frase tal cual",
      texto_voz(S, "America/Bogota") == "Empezó Carlos Uno contra Pedro Dos. " + VOZ,
      texto_voz(S, "America/Bogota"))

print("\n4. UTR VALUE: mismo trato")
U = {**S, "tipo": "UTR_VALUE", "jugador1": "Alto Uno", "jugador2": "Bajo Dos",
     "datos": {**S["datos"], "texto_discord": "💎 FULLTENIS · UTR VALUE\n🎾 ⭐ __**Alto Uno**__ vs Bajo Dos",
               "texto_voz": "UTR Value. Alto Uno. Paga dos punto cero cinco."}}
check("UTR_VALUE en texto y voz por defecto",
      "UTR_VALUE" in config.TIPOS_TEXTO and "UTR_VALUE" in config.TIPOS_VOZ)
tu = texto_canal(U)
check("texto UTR VALUE: encabezado en negrita + pie de inicio",
      tu.startswith("**💎 FULLTENIS · UTR VALUE**") and "Empezó el partido" in tu, tu)
check("voz UTR VALUE: qué empezó + frase tal cual",
      texto_voz(U, "UTC") == "Empezó Alto Uno contra Bajo Dos. UTR Value. Alto Uno. Paga dos punto cero cinco.",
      texto_voz(U, "UTC"))

print("\n5. UTR MARKET ANOMALY: en vivo en el acto; pre al empezar")
TXA = ("🚨 FULLTENIS · UTR MARKET ANOMALY\n🎾 Biro M vs Beviz L\n⭐ UTR SUPERIOR: Beviz L\n"
       "📊 UTR: 10.58 vs 10.27\n📈 ΔUTR: +0.31\n💰 Cuota PRE: 2.45\n🚨 Cuota de activación: 4.20\n"
       "📈 Movimiento: 2.45 → 4.20\n🔴 En vivo\n\n⚠️ JUGADOR UTR SUPERIOR A CUOTA EXTREMA")
A = {**S, "tipo": "UTR_MARKET_ANOMALY", "jugador1": "Beviz L", "jugador2": "Biro M",
     "datos": {"nivel": "UTR_MARKET_ANOMALY", "cuota": 4.2, "cuota_pre": 2.45, "fase_activacion": "vivo",
               "detectado_en": "2026-10-07T17:00:00+00:00", "activacion_en": "2026-10-07T17:00:00+00:00",
               "texto_discord": TXA, "texto_voz": "UTR Market Anomaly. Beviz L. Paga cuatro punto veinte."}}
check("UTR_MARKET_ANOMALY en texto y voz por defecto",
      "UTR_MARKET_ANOMALY" in config.TIPOS_TEXTO and "UTR_MARKET_ANOMALY" in config.TIPOS_VOZ)
ta = texto_canal(A)
check("texto en vivo: encabezado en negrita, cuerpo tal cual, pie 'En vivo' (no 'Empezó')",
      ta.startswith("**🚨 FULLTENIS · UTR MARKET ANOMALY**") and "⚠️ JUGADOR UTR SUPERIOR A CUOTA EXTREMA" in ta
      and ta.split("\n")[-1].startswith("🔴 **En vivo** · cuota detectada <t:1791392400:R>")
      and "Empezó el partido" not in ta, ta)
check("voz en vivo: 'En vivo, Beviz contra Biro.' + frase",
      texto_voz(A, "UTC") == "En vivo, Beviz contra Biro. UTR Market Anomaly. Beviz L. Paga cuatro punto veinte.",
      texto_voz(A, "UTC"))
AP = {**A, "datos": {**A["datos"], "fase_activacion": "pre"}}
check("pre-partido: pie de 'Empezó el partido' y voz 'Empezó …'",
      "Empezó el partido" in texto_canal(AP) and texto_voz(AP, "UTC").startswith("Empezó Beviz contra Biro."),
      texto_canal(AP))

print()
if fallas:
    print(f"{ok} OK, {fallas} FALLAS")
    sys.exit(1)
print(f"{ok} comprobaciones OK.")
