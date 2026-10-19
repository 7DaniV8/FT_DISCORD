#!/usr/bin/env python3
"""
tests/test_horarios_pie.py — 🕒 v20 (19/10/2026, Rubén): partido que cambió de hora + pie corto de CUOTA EXTREMA.

  1. Cuota Mal Puesta / UTR VALUE / UTR MARKET ANOMALY pre-partido: si FT Intelligence manda la hora original y la
     vigente, el pie dice «🕒 El partido se retrasó: era <hora>, ahora <hora>» (horas en la zona de quien lee).
  2. Sin cambio (o < 5 min) no aparece nada nuevo: el pie de siempre.
  3. El mensaje corto «🚨 FULLTENIS · CUOTA EXTREMA» cierra con «⏳ Comienza en 10 minutos.» (sin «Detectada hace»).
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
for k in ("FT_DISCORD_TIPOS_TEXTO", "FT_DISCORD_TIPOS_VOZ"):
    os.environ.pop(k, None)

from ft_discord.textos import texto_canal, texto_voz  # noqa: E402

ok = fallas = 0


def check(nombre, cond, det=""):
    global ok, fallas
    ok, fallas = (ok + 1, fallas) if cond else (ok, fallas + 1)
    print(f"  {'OK   ' if cond else 'FALLA'} {nombre}" + (f" -- {det}" if det else ""))


VIEJA, NUEVA = "2026-10-19T15:00:00+00:00", "2026-10-19T18:00:00+00:00"
TV = int(datetime(2026, 10, 19, 15, tzinfo=timezone.utc).timestamp())
TN = int(datetime(2026, 10, 19, 18, tzinfo=timezone.utc).timestamp())


def senal(tipo, texto, **datos):
    return {"id": 1, "tipo": tipo, "jugador1": "Ayana Akli", "jugador2": "Kayla Cross", "torneo": "W75 Saguenay",
            "fecha_partido": NUEVA, "resumen": texto.split("\n")[0], "creado_en": "2026-10-19T17:50:00+00:00",
            "datos": {"texto_discord": texto, "texto_voz": "Cuota mal puesta. Akli.", "momento": "ANTES", "minutos": 10,
                      "detectado_en": "2026-10-19T05:00:00+00:00", **datos}}


print("\n1. Cuota Mal Puesta con el partido retrasado 3 h")
CMP = "🔥 FULLTENIS · CUOTA MAL PUESTA\n🎾 ⭐ Ayana Akli vs Kayla Cross\n🏆 W75 Saguenay · 19/10 13:00 · ⚠️ cambió de hora"
t = texto_canal(senal("CUOTA_MAL_PUESTA", CMP, fecha_partido=NUEVA, fecha_partido_original=VIEJA))
check("dice que se retrasó, con la hora vieja y la nueva", f"🕒 El partido se retrasó: era <t:{TV}:t>, ahora <t:{TN}:t>" in t, t)
check("y el pie corto con los minutos (contados con la hora nueva)",
      t.endswith("⏳ Empieza en aproximadamente 10 minutos.") and "Detectada" not in t, t)
check("la línea del cambio va justo antes del pie", t.split("\n")[-2].startswith("🕒 El partido se retrasó"), t)
t2 = texto_canal(senal("UTR_VALUE", "📈 FULLTENIS · UTR VALUE\n🎾 A vs B", fecha_partido="2026-10-19T14:00:00+00:00",
                       fecha_partido_original="2026-10-19T15:00:00+00:00"))
check("adelantado → «se adelantó»", "🕒 El partido se adelantó" in t2, t2)

print("\n2. Sin cambio de hora: igual que siempre")
t3 = texto_canal(senal("CUOTA_MAL_PUESTA", CMP, fecha_partido=NUEVA))
check("sin hora original: sin línea nueva", "🕒" not in t3 and t3.endswith("⏳ Empieza en aproximadamente 10 minutos."), t3)
t4 = texto_canal(senal("CUOTA_MAL_PUESTA", CMP, fecha_partido=NUEVA, fecha_partido_original="2026-10-19T17:57:00+00:00"))
check("3 min de diferencia: no se dice", "🕒" not in t4, t4)

print("\n3. Mensaje corto «🚨 FULLTENIS · CUOTA EXTREMA» (UTR MARKET ANOMALY pre-partido)")
CE = ("🚨 FULLTENIS · CUOTA EXTREMA\n🎾 Santiago Rodriguez Taverna vs ⭐ Mariano Kestelboim\n🏆 Challenger Cali · Tierra\n"
      "⭐ JUGADOR DETECTADO: Mariano Kestelboim\n💰 Cuota actual: 3.74\n🔥 DOS JUGADORES DE NIVEL SIMILAR, PERO UNO PAGA MUCHO MÁS.")
t5 = texto_canal(senal("UTR_MARKET_ANOMALY", CE, fase_activacion="pre"))
check("cierra con «⏳ Empieza en aproximadamente 10 minutos.» (sin línea en blanco antes)",
      t5.split("\n")[-1] == "⏳ Empieza en aproximadamente 10 minutos." and t5.split("\n")[-2].startswith("🔥"), t5)
check("sin «Detectada hace» ni «EMPIEZA EN ~»", "Detectada" not in t5 and "EMPIEZA EN" not in t5, t5)
t6 = texto_canal(senal("UTR_MARKET_ANOMALY", CE, fase_activacion="pre", fecha_partido=NUEVA, fecha_partido_original=VIEJA))
check("con cambio de hora: la línea 🕒 y después el pie", t6.split("\n")[-2].startswith("🕒 El partido se retrasó")
      and t6.endswith("⏳ Empieza en aproximadamente 10 minutos."), t6)
t7 = texto_canal(senal("UTR_MARKET_ANOMALY", CE, fase_activacion="pre", momento="EN_JUEGO", minutos=None))
check("visto empezar primero: «▶️ Ya empezó.»", t7.endswith("▶️ Ya empezó."), t7)
t8 = texto_canal(senal("UTR_MARKET_ANOMALY", CE, fase_activacion="pre", minutos=1))
check("1 minuto en singular", t8.endswith("⏳ Empieza en aproximadamente 1 minuto."), t8)
v = texto_voz(senal("UTR_MARKET_ANOMALY", CE, fase_activacion="pre"), "UTC")
check("la voz sigue igual («En diez minutos, …»)", v.startswith("En diez minutos"), v)

print("\n4. ✂️ El ejemplo de Rubén (19/10): Cuota Mal Puesta corta + pie corto, línea por línea")
EJ = ("🚨 FULLTENIS · CUOTA MUY MAL PUESTA\n🎾 Ayana Akli vs ⭐ Kayla Cross\n🏆 ITF Saguenay · 19/10 · 10:00 AM 🇨🇴\n"
      "💰 JUGADORA RECOMENDADA: Kayla Cross\n🎯 CUOTA: 1.79\n📊 PROBABILIDAD DE GANAR:\nFullTenis: 68.5% · Mercado: 52%\n"
      "🔥 ¿POR QUÉ LA RECOMENDAMOS?\n✅ Nuestros análisis favorecen a Kayla Cross.\n"
      "✅ Mejor rendimiento ante rivales de mayor nivel.\n✅ Su cuota ofrece una oportunidad según nuestros cálculos.\n"
      "💎 SEÑAL PREMIUM")
t9 = texto_canal(senal("CUOTA_MAL_PUESTA", EJ))
check("igual al ejemplo (encabezado en negrita) + «⏳ Empieza en aproximadamente 10 minutos.»",
      t9 == "**" + EJ.split("\n")[0] + "**\n" + "\n".join(EJ.split("\n")[1:]) + "\n⏳ Empieza en aproximadamente 10 minutos.", t9)
t10 = texto_canal(senal("UTR_VALUE", "📈 FULLTENIS · UTR VALUE\n🎾 A vs B", momento="EN_JUEGO", minutos=None))
check("visto empezar: «▶️ Ya empezó.»", t10.endswith("▶️ Ya empezó."), t10)

print(f"\n{ok} OK · {fallas} falla(s)")
sys.exit(1 if fallas else 0)
