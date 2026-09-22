#!/usr/bin/env python3
"""
tests/test_contrato_ft_intelligence.py — El bot contra el /senales REAL de
FT Intelligence (21/09/2026).

FT Intelligence genera señales con su propio código sobre una base
temporal, y el bot las lee por el endpoint verdadero (en memoria, con
httpx.ASGITransport) y arma los textos. Si un lado cambia el formato, falla.

  FT_INTEL_REPO=/ruta/a/FT_INTELLIGENCE python tests/test_contrato_ft_intelligence.py
Sin esa variable, se saltea.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = os.getenv("FT_INTEL_REPO", "")
if not REPO or not (Path(REPO) / "ft_intelligence" / "vigilancia.py").exists():
    print("Saltado: definir FT_INTEL_REPO con una copia de FT_INTELLIGENCE (fase 2).")
    sys.exit(0)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.append(REPO)
os.environ.update({"FT_INTEL_DB_PATH": str(Path(tempfile.mkdtemp()) / "fti.db"),
                   "FTR_SERVICE_URL": "https://ftr.test", "FTR_INTERNAL_SECRET": "secreto"})

import httpx  # noqa: E402

from ft_discord.fuente import Fuente  # noqa: E402
from ft_discord.textos import texto_canal, texto_voz  # noqa: E402
from ft_intelligence.app import app  # noqa: E402
from ft_intelligence.db import conectar  # noqa: E402
from ft_intelligence.identidad import resolver_fixtures_pendientes, resolver_mto_pendientes  # noqa: E402
from ft_intelligence.vigilancia import evaluar  # noqa: E402

ok = fallas = 0


def check(nombre, cond, det=""):
    global ok, fallas
    ok, fallas = (ok + 1, fallas) if cond else (ok, fallas + 1)
    print(f"  {'OK   ' if cond else 'FALLA'} {nombre}" + (f" -- {det}" if det else ""))


ahora = datetime.now(timezone.utc).replace(microsecond=0)
inicio = (ahora + timedelta(hours=5)).replace(minute=30, second=0)
conn = conectar()
ayer = ahora.date() - timedelta(days=1)
for pid, n in ((1, "Jugador Uno"), (2, "Jugador Dos"), (3, "Jugador Tres")):
    conn.execute("INSERT INTO players VALUES (?,?,?,?,?)", (pid, n, n.lower(), "M", "x"))
    conn.execute("INSERT INTO ratings (player_id, superficie, rating, partidos_jugados, sincronizado_en) "
                 "VALUES (?, 'general', 11, 40, 'x')", (pid,))
# Uno pidió MTO ayer contra Tres, y ganó igual: queda vigilado.
for mid, a, b, sg, sp in ((1, 1, 3, 2, 1), (2, 3, 1, 1, 2)):
    conn.execute("INSERT INTO ftr_partidos (match_id, player_id, opponent_id, fecha, score, sets_ganados, "
                 "sets_perdidos, sincronizado_en) VALUES (?,?,?,?, '6-4 3-6 6-2', ?,?, 'x')",
                 (mid, a, b, ayer.isoformat() + "T10:00:00+00:00", sg, sp))
conn.execute("INSERT INTO ftr_mto (id, fecha_partido, home, away, torneo, genero, jugador_mto, creado_en, "
             "sincronizado_en) VALUES (1, ?, 'Jugador Uno', 'Jugador Tres', 'M25 Test', 'M', 'Jugador Uno', "
             "?, 'x')", (ayer.isoformat(), ayer.isoformat() + "T11:00:00+00:00"))
# Su próximo partido, con cuota: se revisa.
conn.execute("INSERT INTO ftr_fixtures (fixture_id, par_norm, fecha, torneo, genero, jugador1, jugador2, "
             "odd1, odd2, ftr1, ftr2, prob_ftr, prob_elo, favorito_nombre, primer_visto, ultimo_visto) "
             "VALUES (1, 'p', ?, 'M25 Test', 'M', 'Jugador Uno', 'Jugador Dos', 1.6, 2.4, 12, 10, "
             "0.6, 0.58, 'Jugador Uno', 'x', 'x')", (inicio.isoformat(),))
conn.commit()
resolver_mto_pendientes(conn)
resolver_fixtures_pendientes(conn)
evaluar(conn, ahora)


async def leer():
    f = Fuente("http://ft-intel", "secreto", transport=httpx.ASGITransport(app=app))
    ultimo, senales = await f.ultimo_id(), await f.leer(0)
    await f.cerrar()
    return ultimo, senales

print("\nContrato FT Discord <-> FT Intelligence (/senales real)")
ultimo, senales = asyncio.run(leer())
check("el bot lee la revisión real y el último id", ultimo == 1 and len(senales) == 1
      and senales[0]["tipo"] == "REVISION_CUOTA", str([x["tipo"] for x in senales]))
s = senales[0]
canal = texto_canal(s)
check("la ficha del canal, armada con los datos reales", "REVISIÓN DE CUOTA" in canal
      and "¿Por qué Jugador Dos está a 2.4?" in canal and "investigador pendiente" in canal
      and "<t:" in canal, canal)
voz = texto_voz(s, "UTC", ahora)
check("la conclusión por voz, armada con los datos reales",
      voz.startswith("Atención FullTennis. Revisión de cuota: Jugador Uno contra Jugador Dos")
      and "pidió atención médica ayer y ganó igual" in voz and "Jugador Dos paga dos punto cuatro" in voz
      and "MTO" not in voz, voz)
evaluar(conn, inicio - timedelta(minutes=9))
_, senales = asyncio.run(leer())
aviso = [x for x in senales if x["tipo"] == "REVISION_VOZ"]
va = texto_voz(aviso[0], "UTC", ahora) if aviso else ""
check("a 9 minutos del partido, el aviso de voz real", len(aviso) == 1
      and va.startswith("Atención FullTennis. En nueve minutos empieza Jugador Uno contra Jugador Dos"), va)
print(f"\n{'─' * 60}\n{ok} comprobaciones OK." if not fallas else f"\n{fallas} FALLAS")
sys.exit(1 if fallas else 0)
