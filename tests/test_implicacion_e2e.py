#!/usr/bin/env python3
"""
tests/test_implicacion_e2e.py — 🏆 IMPLICACIÓN DE TORNEO de punta a punta, con el código
REAL de los tres servicios (13/10/2026, Rubén: "Primero debemos probar las reglas y el
circuito completo. Los candidatos experimentales deben mantenerse separados de los picks
validados y no enviarse a la comunidad como señales confirmadas").

    RANKINGFTR_REPO=/ruta/RankingFTR FT_INTEL_REPO=/ruta/FT_INTELLIGENCE \\
        python tests/test_implicacion_e2e.py
Sin esas dos variables, se saltea.

  RankingFTR v45    motor de picks REAL sobre implicacion_partidos:
     │              🧪 experimental (IMP-R1 recién nacida) · 🧪 PRUEBA de Admin ·
     │              ✅ validado (IMP-R1 validada) con IMP_DISCORD_ACTIVO 0 y después 1
     ▼  router real /ft-intel/implicacion y /ft-intel/discord-config (TestClient, secreto interno)
  FT Intelligence v21   implicacion.sincronizar → señales IMPLICACION en el acto
     ▼  /senales y /discord-config reales (ASGI)
  FT Discord v13    Anunciador real: lo que el bot ESCRIBE y DICE
     ▲  FT Intelligence reportar_dichas → POST /ft-intel/implicacion/dicho → RankingFTR marca dicho
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

RFTR = os.getenv("RANKINGFTR_REPO", "")
FTI = os.getenv("FT_INTEL_REPO", "")
if not (RFTR and (Path(RFTR) / "production" / "engines" / "implicacion" / "picks.py").exists()
        and FTI and (Path(FTI) / "ft_intelligence" / "implicacion.py").exists()):
    print("Saltado: definir RANKINGFTR_REPO (≥ v45) y FT_INTEL_REPO (≥ v21).")
    sys.exit(0)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.append(RFTR)
sys.path.append(FTI)
_TMP = Path(tempfile.mkdtemp())
DB_FTR = _TMP / "ftr_utr.db"
os.environ.update({
    "FTR_DB_PATH": str(DB_FTR), "DB_PATH": str(DB_FTR), "ADMIN_TOKEN": "t", "RESEARCH_TOKEN": "r",
    "FTR_INTERNAL_SECRET": "secreto", "RAPIDAPI_KEY": "x",
    "FULLTENIS_ELO_DB_PATH": str(_TMP / "no_existe_elo.sqlite"),
    "FT_INTEL_DB_PATH": str(_TMP / "ft_intelligence.db"), "FTR_SERVICE_URL": "http://rankingftr.test",
})
for k in ("FT_DISCORD_TIPOS_TEXTO", "FT_DISCORD_TIPOS_VOZ"):
    os.environ.pop(k, None)

ok = fallas = 0


def check(nombre, cond, det=""):
    global ok, fallas
    ok, fallas = (ok + 1, fallas) if cond else (ok, fallas + 1)
    print(f"  {'OK   ' if cond else 'FALLA'} {nombre}" + (f" -- {det}" if det else ""))


# ═════════════════════════════════════════════════════════════════════
#  1. RankingFTR: el motor real decide
# ═════════════════════════════════════════════════════════════════════
print("\n1 · RankingFTR v45: motor de picks real")
from production.database.schema import SCHEMA  # noqa: E402

_c = sqlite3.connect(DB_FTR)
_c.execute("PRAGMA journal_mode=WAL")
_c.executescript(SCHEMA)
_c.commit()
_c.close()

import production.api.deps as deps  # noqa: E402


def _get_conn():
    c = sqlite3.connect(DB_FTR, timeout=20)
    c.row_factory = sqlite3.Row
    return c


deps.get_conn = _get_conn
from production.api.config_estrategias import set_valor  # noqa: E402
from production.engines import discord_config as dcfg  # noqa: E402
from production.engines.implicacion import esquema, picks  # noqa: E402

conn = _get_conn()
esquema.ensure_una_vez(conn)
picks.ensure(conn)
AHORA = datetime.now(timezone.utc).replace(microsecond=0)


def partido(i, **kw):
    f = {"clave": f"e2e{i}", "tour": "ATP", "genero": "M", "circuito": "atp", "torneo": "ATP Basel", "jugador1": f"Favorito {i}",
         "jugador2": f"Rival {i}", "inicio_programado": (AHORA - timedelta(minutes=50)).isoformat(), "registrado_en": AHORA.isoformat(),
         "fuente_registro": "fixture", "version_registro": "t", "set1_perdedor": 1, "set1_fuente": "en_vivo",
         "set1_cerrado_en": (AHORA - timedelta(minutes=1)).isoformat(), "set1_marcador": "4-6", "fav_pre_estado": "verificada",
         "fav_pre": 1, "fav_pre_cuota": 1.30, "fav_pre_grupo": "fuerte", "cuota_set1_estado": "verificada", "cuota_perdedor_set1": 2.60,
         "defensa1": 100, "puntos1": 700, "rank1": 70, "utr1": 13.5, "utr2": 13.0, "utr_delta": 0.1}
    f.update(kw)
    cur = conn.execute(f"INSERT INTO implicacion_partidos ({', '.join(f)}) VALUES ({', '.join('?' * len(f))})", tuple(f.values()))
    conn.commit()
    return cur.lastrowid


partido(1)                                             # IMP-R1 todavía experimental
r = picks.detectar(conn, AHORA)
check("el pick con la regla recién nacida es 🧪 EXPERIMENTAL y no publicable", r["experimentales"] == 1 and r["publicables"] == 0, r)
prueba = picks.crear_prueba(conn, "ruben@fulltennis")
# Validación simulada (los 5 criterios se prueban en RankingFTR, tests/test_implicacion_picks.py):
conn.execute("UPDATE implicacion_reglas SET nivel='validado', cambiado_por='e2e' WHERE regla='IMP-R1'")
conn.commit()
partido(2)                                             # validado, pero Discord apagado
r = picks.detectar(conn, AHORA)
check("regla validada con IMP_DISCORD_ACTIVO = 0 (fábrica): ✅ validado pero NO publicable", r["validados"] == 1 and r["publicables"] == 0, r)
set_valor("IMP_DISCORD_ACTIVO", "1", "e2e")
partido(3, torneo="ATP Viena", jugador1="Taylor Fritz", jugador2="Arthur Fils")
r = picks.detectar(conn, AHORA)
check("con IMP_DISCORD_ACTIVO = 1: el ✅ validado es publicable", r["validados"] == 1 and r["publicables"] == 1, r)
td = dcfg.para_discord(conn)["tipos"]
check("Admin → Discord: IMPLICACION con texto y voz de fábrica", td["IMPLICACION"]["texto"] and td["IMPLICACION"]["voz"])
dcfg.guardar(conn, "IMPLICACION", None, None, "🏆 FULLTENNIS · IMPLICACIÓN DE TORNEO", None, "e2e")

# ═════════════════════════════════════════════════════════════════════
#  2. FT Intelligence (contra el router REAL de RankingFTR)
# ═════════════════════════════════════════════════════════════════════
print("\n2 · FT Intelligence v21: solo lo publicable, en el acto")
import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from production.api.app import app as ftr_app  # noqa: E402

tc = TestClient(ftr_app, base_url="https://rankingftr.test")


class _Puente(httpx.BaseTransport):
    def handle_request(self, request: httpx.Request) -> httpx.Response:
        q_ = request.url.query.decode()
        r_ = tc.request(request.method, request.url.path + (f"?{q_}" if q_ else ""),
                        headers={k: v for k, v in request.headers.items()
                                 if k.lower().startswith("x-") or k.lower() == "content-type"},
                        content=request.content or None)
        return httpx.Response(r_.status_code, content=r_.content, headers={"content-type": r_.headers.get("content-type", "")})


from ft_intelligence import discord_config as fti_dcfg  # noqa: E402
from ft_intelligence import implicacion as fti_imp  # noqa: E402
from ft_intelligence.cliente_http import ClienteHTTP  # noqa: E402
from ft_intelligence.db import conectar  # noqa: E402

cli = ClienteHTTP("http://rankingftr.test", {"X-FTR-Internal-Secret": "secreto"}, "ftr", transport=_Puente(), pausa=0)
fti = conectar()
check("la Matriz de Admin → Discord llega a FT Intelligence", fti_dcfg.sincronizar(fti, cli)["ok"] is True)
r1 = fti_imp.sincronizar(fti, cli, datetime.now(timezone.utc))
sen = [dict(s) for s in fti.execute("SELECT tipo, clave, jugador1 FROM senales WHERE tipo='IMPLICACION'").fetchall()]
check("dos señales IMPLICACION: la 🧪 PRUEBA y el ✅ validado publicable; ni el experimental ni el validado con Discord apagado",
      r1["avisados"] == 2 and sorted(s["jugador1"] for s in sen) == ["Jugador Prueba", "Taylor Fritz"], (r1, sen))
r2 = fti_imp.sincronizar(fti, cli, datetime.now(timezone.utc))
check("segunda vuelta: nada se repite", r2["avisados"] == 0 and r2["nuevos"] == 0, r2)

# ═════════════════════════════════════════════════════════════════════
#  3. FT Discord: el anunciador REAL contra el /senales REAL
# ═════════════════════════════════════════════════════════════════════
print("\n3 · FT Discord v13: lo que el bot escribe y dice")
from ft_discord import config as dconf  # noqa: E402
from ft_discord.anunciador import Anunciador  # noqa: E402
from ft_discord.fuente import Fuente  # noqa: E402
from ft_intelligence.app import app as fti_app  # noqa: E402

publicados, dichos = [], []


async def publicar(t):
    publicados.append(t)


def encolar(t):
    dichos.append(t)
    return True


async def correr():
    f = Fuente("http://ft-intel", "secreto", transport=httpx.ASGITransport(app=fti_app))
    a = Anunciador(f, publicar, encolar, "America/Bogota", dconf.TIPOS_TEXTO, dconf.TIPOS_VOZ, 30,
                   reloj=lambda: datetime.now(timezone.utc))
    await a.arrancar(0)
    c1 = await a.ciclo()
    c2 = await a.ciclo()
    await f.cerrar()
    return a, c1, c2

a, c1, c2 = asyncio.run(correr())
check("IMPLICACION está entre los tipos de FT Discord (texto y voz)", "IMPLICACION" in dconf.TIPOS_TEXTO and "IMPLICACION" in dconf.TIPOS_VOZ)
check("primer ciclo: 2 textos y 2 voces", c1["texto"] == 2 and c1["voz"] == 2, c1)
val = [t for t in publicados if "Taylor Fritz" in t]
pru = [t for t in publicados if "Jugador Prueba" in t]
check("✅ el validado llega con el formato pedido (encabezado de Admin en la 1.ª línea), sin 'apuesta' ni 'rentable'",
      len(val) == 1 and val[0].startswith("**🏆 FULLTENNIS · IMPLICACIÓN DE TORNEO**") and "⭐ Favorito PRE: Taylor Fritz (1.30)" in val[0]
      and "🔴 Taylor Fritz pierde el primer set 4-6." in val[0] and "💰 Cuota LIVE: 2.60" in val[0] and "🏆 Defiende 100 puntos" in val[0]
      and "🔥 FAVORITO PRE EN VIGILANCIA DE REMONTADA" in val[0] and "_🔴 En vivo · IMP-R1 · señal #" in val[0].split("\n")[-1]
      and "apuesta" not in val[0].lower() and "rentable" not in val[0].lower(), val)
check("🧪 la PRUEBA no pierde su marca aunque Admin tenga encabezado: 1.ª línea «PRUEBA DEL CIRCUITO» y pie «no es una señal»",
      len(pru) == 1 and pru[0].startswith("🧪 **PRUEBA DEL CIRCUITO**") and "no es una señal" in pru[0].split("\n")[-1], pru)
check("voz: 'Atención FullTennis. Implicación de torneo. Taylor Fritz…' y la prueba empieza por 'Prueba del circuito'",
      any(v.startswith("Atención FullTennis. Implicación de torneo. Taylor Fritz") for v in dichos)
      and any(v.startswith("Atención FullTennis. Prueba del circuito.") for v in dichos), dichos)
check("segundo ciclo del bot: no repite nada", c2["texto"] == 0 and c2["voz"] == 0, c2)
for t in publicados:
    print("\n   " + t.replace("\n", "\n   "))

# ═════════════════════════════════════════════════════════════════════
#  4. La confirmación vuelve a RankingFTR
# ═════════════════════════════════════════════════════════════════════
print("\n4 · Confirmación en RankingFTR")
rep = fti_imp.reportar_dichas(fti, cli, datetime.now(timezone.utc))
marcas = {r_["seleccion"]: r_["dicho_discord"] for r_ in conn.execute("SELECT seleccion, dicho_discord FROM implicacion_picks").fetchall()}
check("FT Intelligence reporta los 2 dichos y RankingFTR los marca dicho_discord=1; el experimental y el validado sin Discord quedan sin marca",
      rep["reportadas"] == 2 and marcas["Taylor Fritz"] == 1 and marcas["Jugador Prueba"] == 1
      and not marcas["Favorito 1"] and not marcas["Favorito 2"], (rep, marcas))
check("…una sola vez", fti_imp.reportar_dichas(fti, cli, datetime.now(timezone.utc))["reportadas"] == 0)
set_valor("IMP_DISCORD_ACTIVO", "0", "e2e")
check("apagar IMP_DISCORD_ACTIVO corta el feed en el acto (solo pruebas)",
      all(f["prueba"] == 1 for f in tc.get("/ft-intel/implicacion", headers={"X-FTR-Internal-Secret": "secreto"}).json()["implicacion"]))

print(f"\n{ok} OK, {fallas} fallas")
sys.exit(1 if fallas else 0)
