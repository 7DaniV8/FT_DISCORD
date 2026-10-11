#!/usr/bin/env python3
"""
tests/test_recordador.py — ⏰ RECORDADOR en Discord (08/10/2026).

  1. Viene por defecto en los tipos de texto y de voz.
  2. Texto: el de RankingFTR tal cual + pie; sin "Favorito:" inventado.
  3. Voz: la frase de RankingFTR, con los nombres como se dicen.
  4. v12 (11/10/2026): 🩹 RECORDADOR_IA y 🔴 RECORDADOR_VIVO vienen por defecto
     en texto y voz; el formato aprobado de RankingFTR se publica tal cual (sin
     "Favorito:", sin cuotas, sin "apostar"), con el pie; la voz dice los
     porcentajes en palabras tal como llegan.
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

print("\n4. v12: RECORDADOR_IA y RECORDADOR_VIVO")
check("RECORDADOR_IA y RECORDADOR_VIVO por defecto en texto y voz",
      {"RECORDADOR_IA", "RECORDADOR_VIVO"} <= config.TIPOS_TEXTO and {"RECORDADOR_IA", "RECORDADOR_VIVO"} <= config.TIPOS_VOZ)
TXT_IA = ("🩹 FULLTENNIS · RECORDATORIO IA\n🎾 Carlos Alcaraz vs Kovacs L\n👀 Observación: cojera en pierna derecha.\n"
          "📊 PROBABILIDAD FULLTENNIS BASE\nCarlos Alcaraz: 55%\nKovacs L: 45%\n🧠 Conclusión: observar sus desplazamientos desde el inicio.")
VOZ_IA = "Recordatorio FullTennis. Carlos Alcaraz contra Kovacs L. Probabilidad base: cincuenta y cinco por ciento."
SIA = {**S, "id": 12, "tipo": "RECORDADOR_IA", "datos": {**S["datos"], "tipo": "RECORDADOR_IA", "prediccion_id": 31,
       "probabilidad": {"jugador": 0.55, "rival": 0.45, "base": True, "version": 2}, "texto_discord": TXT_IA, "texto_voz": VOZ_IA}}
t = texto_canal(SIA)
check("IA: el formato aprobado tal cual + pie de seguimiento", t.startswith(TXT_IA) and t.split("\n")[-1] == "_Seguimiento FullTennis · señal #12_", t)
check("IA: nada inventado (sin 'Favorito:', sin cuota, sin 'apostar')", all(x not in t.lower() for x in ("favorito:", "cuota", "apost", "elo", "ftr ")))
check("IA: voz con 'cincuenta y cinco por ciento' y 'Kovacs' dicho corto",
      texto_voz(SIA, "America/Bogota") == "Recordatorio FullTennis. Carlos Alcaraz contra Kovacs. Probabilidad base: cincuenta y cinco por ciento.")
TXT_VIVO = "🔴 FULLTENNIS · PARTIDO EN VIVO\n🎾 Carlos Alcaraz vs Kovacs L\n📊 FULLTENNIS: 55% vs 45% (base)\n🩹 En seguimiento: Carlos Alcaraz, cojera en pierna derecha.\n🧠 Conclusión: observar sus desplazamientos."
SV = {**SIA, "id": 13, "tipo": "RECORDADOR_VIVO", "datos": {**SIA["datos"], "tipo": "RECORDADOR_VIVO", "momento": "EN_JUEGO", "en_vivo": True,
      "texto_discord": TXT_VIVO, "texto_voz": "Empezó Carlos Alcaraz contra Kovacs L. Cincuenta y cinco por ciento base."}}
t = texto_canal(SV)
check("VIVO: corto, tal cual + pie", t.startswith(TXT_VIVO) and t.endswith("_Seguimiento FullTennis · señal #13_") and len(t) < len(texto_canal(SIA)), t)
check("VIVO: voz tal cual", texto_voz(SV, "America/Bogota") == "Empezó Carlos Alcaraz contra Kovacs. Cincuenta y cinco por ciento base.")
check("sin texto de RankingFTR: encabezado mínimo por tipo, sin inventar probabilidad",
      texto_canal({**SV, "datos": {**SV["datos"], "texto_discord": ""}}).startswith("🔴 **FULLTENNIS · PARTIDO EN VIVO**\n🎾 **Carlos Alcaraz** vs Kovacs L")
      and "%" not in texto_canal({**SIA, "datos": {**SIA["datos"], "texto_discord": ""}}))

print(f"\n{ok} OK, {fallas} fallas")
sys.exit(1 if fallas else 0)
