#!/usr/bin/env python3
"""tests/test_parley.py — 🎾 FT PARLEY en Discord (01/11/2026): texto tal cual del servicio, sin voz, tipos registrados."""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ft_discord import config  # noqa: E402
from ft_discord.textos import texto_canal  # noqa: E402

_fallos, _ok = [], 0


def check(n, cond, det=""):
    global _ok
    if cond:
        _ok += 1
        print(f"  OK    {n}")
    else:
        _fallos.append(n)
        print(f"  FALLA {n}" + (f": {det}" if det else ""))


TXT = ("🎾 FULLTENIS · KALSHI\n**🚀 PARLEY SOÑADOR**\n🏆 Cuota combinada objetivo: 30.00+ (esta: **31.40**)\n\n📋 Selecciones\n"
       "**1. A vs B** · ATP · 02/11 10:00\n   Selección: **A gana**\n📊 Probabilidad conjunta: **4.1%**\n⚠️ Riesgo: ALTO\n"
       "🧪 **Combinación teórica para seguimiento**")
for tipo in ("PARLEY_KALSHI", "PARLEY_CASAS", "PARLEY_ATP_OVER", "PARLEY_DOBLE"):
    s = {"id": 1, "tipo": tipo, "jugador1": None, "jugador2": None, "datos": {"texto_discord": TXT}, "foto": {}}
    t = texto_canal(s)
    check(f"{tipo}: el texto del servicio sale tal cual, solo el encabezado en negrita",
          t.startswith("**🎾 FULLTENIS · KALSHI**") and "📋 Selecciones" in t and "teórica para seguimiento" in t and len(t) <= 1900, t[:80])
    check(f"{tipo}: se anuncia por texto y NO por voz", tipo in config.TIPOS_TEXTO and tipo not in config.TIPOS_VOZ)
print(f"\n{_ok} OK, {len(_fallos)} fallas")
sys.exit(1 if _fallos else 0)
