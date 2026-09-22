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
if not REPO or not (Path(REPO) / "ft_intelligence" / "senales.py").exists():
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
from ft_intelligence.identidad import resolver_fixtures_pendientes  # noqa: E402
from ft_intelligence.senales import evaluar  # noqa: E402

ok = fallas = 0


def check(nombre, cond, det=""):
    global ok, fallas
    ok, fallas = (ok + 1, fallas) if cond else (ok, fallas + 1)
    print(f"  {'OK   ' if cond else 'FALLA'} {nombre}" + (f" -- {det}" if det else ""))


ahora = datetime.now(timezone.utc).replace(microsecond=0)
inicio = (ahora + timedelta(hours=5)).replace(minute=30, second=0)
conn = conectar()
for pid, n in ((1, "Jugador Uno"), (2, "Jugador Dos")):
    conn.execute("INSERT INTO players VALUES (?,?,?,?,?)", (pid, n, n.lower(), "M", "x"))
    conn.execute("INSERT INTO ratings (player_id, superficie, partidos_jugados, sincronizado_en) "
                 "VALUES (?, 'general', 40, 'x')", (pid,))
for k in range(1, 9):
    conn.execute("INSERT INTO ftr_partidos (match_id, player_id, opponent_id, fecha, sincronizado_en) "
                 "VALUES (?, 1, 2, ?, 'x')", (k, (inicio.date() - timedelta(days=k)).isoformat() + "T10:00:00+00:00"))
conn.execute("INSERT INTO ftr_fixtures (fixture_id, par_norm, fecha, torneo, genero, jugador1, jugador2, "
             "odd1, odd2, ftr1, ftr2, prob_ftr, prob_elo, favorito_nombre, primer_visto, ultimo_visto) "
             "VALUES (1, 'p', ?, 'M25 Test', 'M', 'Jugador Uno', 'Jugador Dos', 1.6, 2.4, 12, 10, "
             "0.6, 0.58, 'Jugador Uno', 'x', 'x')", (inicio.isoformat(),))
conn.commit()
resolver_fixtures_pendientes(conn)
evaluar(conn, ahora)


async def leer():
    f = Fuente("http://ft-intel", "secreto", transport=httpx.ASGITransport(app=app))
    ultimo, senales = await f.ultimo_id(), await f.leer(0)
    await f.cerrar()
    return ultimo, senales

print("\nContrato FT Discord <-> FT Intelligence (/senales real)")
ultimo, senales = asyncio.run(leer())
check("el bot lee las señales reales y el último id", ultimo >= 1 and len(senales) == ultimo)
s = next(x for x in senales if x["tipo"] == "CARGA_EXTREMA")
canal = texto_canal(s)
check("texto del canal armado con los datos reales", "CARGA EXTREMA" in canal
      and "Jugador Uno" in canal and "<t:" in canal, canal.splitlines()[0])
voz = texto_voz(s, "UTC", ahora)
check("texto de voz armado con los datos reales", voz.startswith("Atención FullTennis. Carga extrema.")
      and "ocho partidos" in voz, voz)
n = next(x for x in senales if x["tipo"] == "PARTIDO_NUEVO")
vn = texto_voz(n, "UTC", ahora)
check("partido nuevo + hora con los datos reales, FTR y Elo incluidos",
      vn.startswith("Atención FullTennis. Partido nuevo: Jugador Uno contra Jugador Dos")
      and "sesenta por ciento" in vn and "cincuenta y ocho por ciento" in vn, vn)
print(f"\n{'─' * 60}\n{ok} comprobaciones OK." if not fallas else f"\n{fallas} FALLAS")
sys.exit(1 if fallas else 0)
