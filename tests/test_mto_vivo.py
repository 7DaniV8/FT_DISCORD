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
check("la frase pedida", v == "Atención FullTennis. Tiempo médico solicitado por Kovacs.", v)
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
