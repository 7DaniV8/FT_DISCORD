#!/usr/bin/env python3
"""
tests/test_recordador_ia_e2e.py — 🩹 RECORDATORIOS IA de punta a punta, con el
código REAL de los tres servicios (11/10/2026, Rubén: "antes de dejarlo
funcionando definitivamente, realiza una prueba real de los dos avisos y
confirma que llegan correctamente a Discord").

    RANKINGFTR_REPO=/ruta/RankingFTR FT_INTEL_REPO=/ruta/FT_INTELLIGENCE \\
        python tests/test_recordador_ia_e2e.py
Sin esas dos variables, se saltea.

  RankingFTR v43b   Admin → "🧪 Probar los dos avisos en Discord" (prueba_discord)
     │              recordador_avisos: 🩹 ANTES (RECORDADOR_IA) + 🔴 EN_JUEGO (RECORDADOR_VIVO)
     ▼  router real /ft-intel/recordador y /ft-intel/discord-config (TestClient, secreto interno)
  FT Intelligence v20   recordador.sincronizar → señales con el tipo tal cual
     ▼  /senales y /discord-config reales (ASGI)
  FT Discord v12    Anunciador real: lo que el bot ESCRIBE y DICE
     ▲  FT Intelligence reportar_dichas → POST /ft-intel/recordador/dicho → RankingFTR marca 📣 Discord ✔
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

RFTR = os.getenv("RANKINGFTR_REPO", "")
FTI = os.getenv("FT_INTEL_REPO", "")
if not (RFTR and (Path(RFTR) / "production" / "engines" / "recordador_ia.py").exists()
        and FTI and (Path(FTI) / "ft_intelligence" / "recordador.py").exists()):
    print("Saltado: definir RANKINGFTR_REPO (≥ v43b) y FT_INTEL_REPO (≥ v20).")
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
#  1. RankingFTR: Admin manda la prueba (y un aviso viejo, anterior a v43)
# ═════════════════════════════════════════════════════════════════════
print("\n1 · RankingFTR v43b: la prueba de Admin y la configuración de fábrica")
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
from production.engines import discord_config as dcfg  # noqa: E402
from production.engines import recordador as rec  # noqa: E402
from production.engines import recordador_ia as ria  # noqa: E402

conn = _get_conn()
rec.ensure_tablas(conn)
rec._asegurar_nota(conn)
AHORA = datetime.now(timezone.utc).replace(microsecond=0)
cfg = rec.leer_config()
check("de fábrica: RECORDADOR_IA_ACTIVO=1, RECORDADOR_IA_DISCORD=1, 5 minutos antes",
      cfg["ia"] is True and cfg["ia_discord"] is True and cfg["minutos_antes"] == 5, cfg)
td = dcfg.para_discord(conn)["tipos"]
check("Admin → Discord: ⏰ RECORDADOR, 🩹 RECORDADOR_IA y 🔴 RECORDADOR_VIVO con texto y voz",
      all(td[t]["texto"] and td[t]["voz"] for t in ("RECORDADOR", "RECORDADOR_IA", "RECORDADOR_VIVO")))
# un aviso VIEJO (anterior a v43: sin tipo) ya avisado antes de la actualización
import json  # noqa: E402
conn.execute("INSERT INTO recordador_avisos (recordatorio_id, nombre, partido_clave, partido_json, avisado_en, momento, texto_discord, "
             "texto_voz, discord) VALUES (1, 'Viejo Uno', 'viejo uno|rival|2026-10-11', ?, ?, 'ANTES', "
             "'⏰ **RECORDADOR · JUEGA EN ~10 MIN**\n🎾 **Viejo Uno** vs Rival', 'Recordatorio FullTennis. Viejo Uno juega en diez minutos contra Rival.', 1)",
             (json.dumps({"jugador": "Viejo Uno", "rival": "Rival", "inicio": AHORA.isoformat(), "minutos": 10}), AHORA.isoformat()))
conn.commit()
p = ria.prueba_discord(conn, "ruben@fulltennis", AHORA)
check("Admin → 🧪 Probar: dos avisos (🩹 ANTES + 🔴 EN_JUEGO) con el formato aprobado, marcados como prueba",
      len(p["avisos"]) == 2 and p["texto_ia"].startswith("🩹 FULLTENNIS · RECORDATORIO IA") and "PRUEBA" in p["texto_ia"]
      and p["texto_vivo"].startswith("🔴 FULLTENNIS · PARTIDO EN VIVO") and "55%" in p["texto_vivo"], p)

# ═════════════════════════════════════════════════════════════════════
#  2. FT Intelligence (contra el router REAL de RankingFTR)
# ═════════════════════════════════════════════════════════════════════
print("\n2 · FT Intelligence v20: señales con el tipo tal cual, sin repetir")
import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from production.api.app import app as ftr_app  # noqa: E402

tc = TestClient(ftr_app, base_url="https://rankingftr.test")


class _Puente(httpx.BaseTransport):
    def handle_request(self, request: httpx.Request) -> httpx.Response:
        q_ = request.url.query.decode()
        r = tc.request(request.method, request.url.path + (f"?{q_}" if q_ else ""),
                       headers={k: v for k, v in request.headers.items()
                                if k.lower().startswith("x-") or k.lower() == "content-type"},
                       content=request.content or None)
        return httpx.Response(r.status_code, content=r.content, headers={"content-type": r.headers.get("content-type", "")})


from ft_intelligence import discord_config as fti_dcfg  # noqa: E402
from ft_intelligence import recordador as fti_rec  # noqa: E402
from ft_intelligence.cliente_http import ClienteHTTP  # noqa: E402
from ft_intelligence.db import conectar  # noqa: E402

cli = ClienteHTTP("http://rankingftr.test", {"X-FTR-Internal-Secret": "secreto"}, "ftr", transport=_Puente(), pausa=0)
fti = conectar()
check("la Matriz de Admin → Discord llega a FT Intelligence", fti_dcfg.sincronizar(fti, cli)["ok"] is True)
r1 = fti_rec.sincronizar(fti, cli, AHORA)
sen = {s["clave"]: dict(s) for s in fti.execute("SELECT tipo, clave, datos_json FROM senales").fetchall()}
check("tres señales: el aviso viejo como ⏰ RECORDADOR, la prueba como 🩹 RECORDADOR_IA y 🔴 RECORDADOR_VIVO",
      r1["avisados"] == 3 and sorted(s["tipo"] for s in sen.values()) == ["RECORDADOR", "RECORDADOR_IA", "RECORDADOR_VIVO"], r1)
r2 = fti_rec.sincronizar(fti, cli, AHORA)
check("segunda vuelta: nada se repite (ni lo viejo ni lo nuevo)", r2["avisados"] == 0 and r2["nuevos"] == 0, r2)

# ═════════════════════════════════════════════════════════════════════
#  3. FT Discord: el anunciador REAL contra el /senales REAL
# ═════════════════════════════════════════════════════════════════════
print("\n3 · FT Discord v12: lo que el bot escribe y dice")
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
    a = Anunciador(f, publicar, encolar, "America/Bogota", dconf.TIPOS_TEXTO, dconf.TIPOS_VOZ, 30, reloj=lambda: AHORA)
    await a.arrancar(0)
    r1 = await a.ciclo()
    r2 = await a.ciclo()
    await f.cerrar()
    return a, r1, r2

a, c1, c2 = asyncio.run(correr())
check("el bot recibe la Matriz de Admin (los tres tipos encendidos, texto y voz)",
      a.config_remota and all(a.config_remota["tipos"][t]["texto"] and a.config_remota["tipos"][t]["voz"]
                              for t in ("RECORDADOR", "RECORDADOR_IA", "RECORDADOR_VIVO")))
check("primer ciclo: 3 textos publicados y 3 voces encoladas (viejo + 🩹 + 🔴)", c1["texto"] == 3 and c1["voz"] == 3, c1)
ia = [t for t in publicados if t.startswith("🩹 FULLTENNIS · RECORDATORIO IA")]
vivo = [t for t in publicados if t.startswith("🔴 FULLTENNIS · PARTIDO EN VIVO")]
viejo = [t for t in publicados if t.startswith("⏰")]
check("🩹 RECORDATORIO IA llega a Discord con observación, PROBABILIDAD FULLTENNIS (con qué se calculó) 55 % / 45 % y conclusión, más el pie",
      len(ia) == 1 and "👀 Observación:" in ia[0] and "📊 PROBABILIDAD FULLTENNIS (Elo + FTR · con la observación)" in ia[0] and "Jugador de Prueba: 55%" in ia[0]
      and "🧠 Conclusión:" in ia[0] and "_Seguimiento FullTennis · señal #" in ia[0].split("\n")[-1], ia)
check("🔴 PARTIDO EN VIVO llega corto, con 55% vs 45% y el resumen de la observación",
      len(vivo) == 1 and "📊 FULLTENNIS: 55% vs 45%" in vivo[0] and "🩹 En seguimiento: Jugador de Prueba, cojera en pierna derecha." in vivo[0]
      and len(vivo[0]) < len(ia[0]), vivo)
check("el aviso viejo (anterior a v43) sale con el ⏰ formato de siempre, una sola vez", len(viejo) == 1 and "Viejo Uno" in viejo[0])
check("nada inventado: sin 'Favorito:', sin cuotas, sin 'apostar'",
      all(x not in (ia[0] + vivo[0]).lower() for x in ("favorito:", "cuota", "apost")))
v_ia = [v for v in dichos if v.startswith("Recordatorio FullTennis. Jugador de Prueba")]
v_vivo = [v for v in dichos if v.startswith("FullTennis. Empezó Jugador de Prueba")]
check("voz: el 🩹 dice 'cincuenta y cinco por ciento' y el 🔴 anuncia que empezó",
      len(v_ia) == 1 and "cincuenta y cinco por ciento" in v_ia[0] and len(v_vivo) == 1 and "por ciento" in v_vivo[0], dichos)
check("segundo ciclo del bot: no repite nada", c2["texto"] == 0 and c2["voz"] == 0, c2)
for t in publicados:
    print("\n   " + t.replace("\n", "\n   "))

# ═════════════════════════════════════════════════════════════════════
#  4. La confirmación vuelve a RankingFTR (📣 Discord ✔ en la pestaña)
# ═════════════════════════════════════════════════════════════════════
print("\n4 · Confirmación en RankingFTR")
rep = fti_rec.reportar_dichas(fti, cli, AHORA)
marcas = {r["id"]: r["dicho_discord"] for r in conn.execute("SELECT id, dicho_discord FROM recordador_avisos").fetchall()}
check("FT Intelligence reporta los 3 avisos dichos y RankingFTR los marca dicho_discord=1 (📣 Discord ✔)",
      rep["reportadas"] == 3 and all(marcas[i] == 1 for i in p["avisos"]) and marcas[1] == 1, (rep, marcas))
check("…una sola vez", fti_rec.reportar_dichas(fti, cli, AHORA)["reportadas"] == 0)

print(f"\n{ok} OK, {fallas} fallas")
sys.exit(1 if fallas else 0)
