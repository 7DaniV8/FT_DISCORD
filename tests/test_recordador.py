#!/usr/bin/env python3
"""
tests/test_recordador.py — ⏰ RECORDADOR en Discord (08/10/2026).

  1. Viene por defecto en los tipos de texto y de voz.
  2. Texto: el de RankingFTR tal cual + pie; sin "Favorito:" inventado.
  3. Voz: la frase de RankingFTR, con los nombres como se dicen.
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


TXT = ("⏰ **RECORDADOR · JUEGA EN ~9 MIN**\n🎾 **Carlos Alcaraz** vs Kovacs L\n"
       "🏆 ATP 500 · 🕐 08/10 14:00 hora Miami · 18:00 UTC")
VOZ = "Recordatorio FullTennis. Carlos Alcaraz juega en nueve minutos contra Kovacs L."
S = {"id": 11, "tipo": "RECORDADOR", "jugador1": "Carlos Alcaraz", "jugador2": "Kovacs L",
     "torneo": "ATP 500", "fecha_partido": "2026-10-08T18:00:00+00:00", "resumen": "⏰ ...",
     "creado_en": "2026-10-08T17:51:00+00:00",
     "datos": {"jugador": "Carlos Alcaraz", "rival": "Kovacs L", "momento": "ANTES", "minutos": 9,
               "en_vivo": False, "texto_discord": TXT, "texto_voz": VOZ},
     "foto": {"recordador": {"jugador": "Carlos Alcaraz"}}}

print("\n1. Tipos")
check("texto por defecto", "RECORDADOR" in config.TIPOS_TEXTO)
check("voz por defecto", "RECORDADOR" in config.TIPOS_VOZ)
print("\n2. Texto")
t = texto_canal(S)
check("el texto de RankingFTR tal cual", t.startswith(TXT), t)
check("pie con el número de señal", t.split("\n")[-1] == "_Recordatorio pedido en FullTennis · señal #11_", t)
check("no agrega 'Favorito:'", "Favorito:" not in t)
print("\n3. Voz")
v = texto_voz(S, "America/Bogota")
check("la frase, con 'Kovacs L' dicho 'Kovacs'",
      v == "Recordatorio FullTennis. Carlos Alcaraz juega en nueve minutos contra Kovacs.", v)
S2 = {**S, "datos": {**S["datos"], "momento": "EN_JUEGO",
                     "texto_voz": "Recordatorio FullTennis. Carlos Alcaraz ya está jugando contra Kovacs L."}}
check("en juego", texto_voz(S2, "America/Bogota") ==
      "Recordatorio FullTennis. Carlos Alcaraz ya está jugando contra Kovacs.")

print(f"\n{ok} OK, {fallas} fallas")
sys.exit(1 if fallas else 0)
