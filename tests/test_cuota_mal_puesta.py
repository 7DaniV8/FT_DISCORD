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

print()
if fallas:
    print(f"{ok} OK, {fallas} FALLAS")
    sys.exit(1)
print(f"{ok} comprobaciones OK.")
