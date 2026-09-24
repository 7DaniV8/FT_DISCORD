#!/usr/bin/env python3
"""tests/test_favorito.py — el favorito, marcado en negrita (24/09/2026)."""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
for k in ("DISCORD_TOKEN", "DISCORD_CANAL_TEXTO_ID", "FT_INTEL_URL", "FTR_INTERNAL_SECRET"):
    os.environ.setdefault(k, "1")

from ft_discord.textos import favorito_de, texto_canal, texto_voz  # noqa: E402

fallos = []


def check(n, c, d=""):
    print(("  OK    " if c else "  FALLA ") + n + ("" if c else f": {d}"))
    if not c:
        fallos.append(n)


M = "⭐ __**{}**__"

print("\n1. Partidos Maestros")
av = {"tipo": "MAESTRO_AVISO", "id": 1, "jugador1": "Ana Uno", "jugador2": "Eva Dos",
      "datos": {"favorito": "Eva Dos", "cuota": 1.31, "es_top": True}}
t = texto_canal(av)
check("aviso: el favorito marcado en la línea de jugadores", f"🎾 Ana Uno vs {M.format('Eva Dos')}" in t, t)
check("aviso: y la línea Favorito en negrita", "⭐ Favorito: __**Eva Dos**__ @1.31" in t, t)
ini = {**av, "tipo": "MAESTRO_INICIO", "datos": {"favorito": "Eva Dos", "es_top": False}}
t = texto_canal(ini)
check("inicio: el favorito marcado", M.format("Eva Dos") in t, t)
check("voz del inicio dice el favorito", texto_voz(ini, "UTC").endswith("Favorito: Eva Dos."),
      texto_voz(ini, "UTC"))
check("voz del aviso dice el favorito", "Favorito: Eva Dos." in texto_voz(av, "UTC"), texto_voz(av, "UTC"))

print("\n2. En general: el favorito del mercado (cuota más baja)")
rad = {"tipo": "RADAR_VOZ", "id": 2, "jugador1": "Eri Shimizu", "jugador2": "Hiromi Abe",
       "foto": {"fixture": {"jugador1": "Eri Shimizu", "jugador2": "Hiromi Abe", "odd1": 3.29, "odd2": 1.311}},
       "datos": {"jugador": "Eri Shimizu", "cuota": 3.29, "diagnostico": {"precio": {}}}}
check("por cuota: Hiromi Abe", favorito_de(rad) == "Hiromi Abe")
t = texto_canal(rad)
check("radar: marcado en la línea de jugadores", f"🎾 Eri Shimizu vs {M.format('Hiromi Abe')}" in t, t)

print("\n3. MTO: por las cuotas en vivo del aviso")
mto = {"tipo": "MTO_VIVO", "id": 3, "jugador1": "Kawaguchi N", "jugador2": "Ishii S",
       "foto": {"mto": {"payload_json": json.dumps({"odds_mto": "2.2", "odds_rival": "1.6"})}},
       "datos": {"jugador": "Kawaguchi N", "rival": "Ishii S", "torneo": "W50"}}
check("MTO: la favorita es la rival (1.6)", favorito_de(mto) == "Ishii S")
t = texto_canal(mto)
check("MTO: marcada en el texto", f"🎾 Kawaguchi N vs {M.format('Ishii S')}" in t, t)
check("MTO sin cuotas legibles: el texto queda igual",
      favorito_de({**mto, "foto": {"mto": {"payload_json": json.dumps({"odds_mto": "x"})}}}) is None)

print("\n4. Ventaja Leve ya marcado desde RankingFTR: no se duplica")
vl = {"tipo": "VENTAJA_LEVE", "id": 4, "jugador1": "A", "jugador2": "B",
      "datos": {"favorito": "Ana Uno",
                "texto_discord": "🔥 VENTAJA LEVE — TENDENCIA UNDER\n🎾 ⭐ __**Ana Uno**__ vs Eva Dos\n📉 x"}}
t = texto_canal(vl)
check("una sola marca", t.count("__**Ana Uno**__") == 1 and "Favorito:" not in t, t)
vl_viejo = {**vl, "datos": {"favorito": "Ana Uno",
                            "texto_discord": "🔥 VENTAJA LEVE — TENDENCIA UNDER\n🎾 Ana Uno vs Eva Dos\n📉 x"}}
check("un aviso viejo (sin marca) también se marca", M.format("Ana Uno") in texto_canal(vl_viejo))

print()
if fallos:
    print(f"FALLARON {len(fallos)}: {fallos}")
    sys.exit(1)
print("Pasaron todos.")
