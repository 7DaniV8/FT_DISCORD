#!/usr/bin/env python3
"""
tests/test_implicacion_e2e.py — 🏆 IMPLICACIÓN DE TORNEO de punta a punta, con el código REAL de los
tres servicios (v45 13/10/2026; v14 13/10/2026 03:17, Rubén: "quiero que Implicación de Torneo empiece a
sonar en Discord ... empezando por IMP-R3 ... las señales experimentales deben enviarse claramente
identificadas como 🧪 EXPERIMENTAL — EN ESTUDIO, nunca como picks validados ... mediante una opción
independiente ... una vez comprobado el canal y la voz con una prueba controlada, quede activado
automáticamente para toda la comunidad ... mantener todos los demás bots funcionando sin cambios").

    RANKINGFTR_REPO=/ruta/RankingFTR FT_INTEL_REPO=/ruta/FT_INTELLIGENCE \\
        python tests/test_implicacion_e2e.py
Sin esas dos variables, se saltea.

  1. Instalado (fábrica): Discord de Implicación apagado → nada sale, salvo la PRUEBA de Admin.
  2. La prueba recorre RankingFTR → FT Intelligence → FT Discord (texto + voz); FT Discord ACUSA lo que hizo
     (POST /senales/acuse) → FT Intelligence lo informa (POST /ft-intel/implicacion/dicho) → RankingFTR
     ENCIENDE solo IMP_DISCORD_ACTIVO e IMP_DISCORD_EXPERIMENTAL.
  3. Un IMP-R3 (implicación ALTA) sale como «🧪 EXPERIMENTAL — EN ESTUDIO», con texto y voz, con todos los datos;
     su nivel sigue «experimental». Un R1 de implicación media NO sale (no está en las reglas habilitadas).
  4. Sin duplicados; el acuse vuelve; los demás tipos de Discord no cambian; apagar la opción corta el envío.
"""
from __future__ import annotations

import asyncio
import json
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
    print("Saltado: definir RANKINGFTR_REPO (≥ v47) y FT_INTEL_REPO (≥ v22).")
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
print("\n1 · RankingFTR v47 recién instalado")
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
from production.api.config_estrategias import obtener_bool, set_valor  # noqa: E402
from production.engines import discord_config as dcfg  # noqa: E402
from production.engines.implicacion import candidatos, esquema, picks  # noqa: E402

conn = _get_conn()
esquema.ensure_una_vez(conn)
picks.ensure(conn)
AHORA = datetime.now(timezone.utc).replace(microsecond=0)
OTROS_ANTES = {t: v for t, v in dcfg.para_discord(conn)["tipos"].items() if t != "IMPLICACION"}


def partido(i, **kw):
    f = {"clave": f"e2e{i}", "tour": "ATP", "genero": "M", "circuito": "atp", "torneo": "ATP Basel - R16", "torneo_base": "ATP Basel",
         "ronda": "R16", "nivel": "atp500", "jugador1": f"Favorito {i}", "jugador2": f"Rival {i}",
         "inicio_programado": (AHORA - timedelta(minutes=50)).isoformat(), "registrado_en": AHORA.isoformat(),
         "fuente_registro": "fixture", "version_registro": "t", "set1_perdedor": 1, "set1_fuente": "en_vivo",
         "set1_cerrado_en": (AHORA - timedelta(minutes=1)).isoformat(), "set1_marcador": "4-6", "fav_pre_estado": "verificada",
         "fav_pre": 1, "fav_pre_cuota": 1.30, "fav_pre_grupo": "fuerte", "cuota_set1_estado": "verificada", "cuota_perdedor_set1": 2.60,
         "defensa1": 100, "puntos1": 700, "rank1": 70, "ranking_fecha": AHORA.date().isoformat(), "utr1": 13.5, "utr2": 13.0,
         "utr_delta": 0.5, "prob_elo1": 0.70, "prob_ftr1": 0.62}
    f.update(kw)
    cur = conn.execute(f"INSERT INTO implicacion_partidos ({', '.join(f)}) VALUES ({', '.join('?' * len(f))})", tuple(f.values()))
    conn.commit()
    return cur.lastrowid


c0 = picks.cfg()
check("de fábrica: IMP_DISCORD_ACTIVO = 0, IMP_DISCORD_EXPERIMENTAL = 0 (opción independiente), reglas habilitadas 'IMP-R3', "
      "encendido automático tras la prueba = 1",
      c0["IMP_DISCORD_ACTIVO"] is False and c0["IMP_DISCORD_EXPERIMENTAL"] is False and c0["IMP_DISCORD_EXPERIMENTAL_REGLAS"] == "IMP-R3"
      and c0["IMP_DISCORD_AUTO_ACTIVAR"] is True, c0)
p1 = partido(1, defensa1=300, puntos1=1200, rank1=45)           # 25 % → implicación ALTA → R1 + R3
candidatos.registrar(conn, AHORA)
r = picks.detectar(conn, AHORA)
k1 = dict(conn.execute("SELECT * FROM implicacion_picks WHERE partido_id=?", (p1,)).fetchone())
check("antes de la prueba: el IMP-R3 se detecta (🧪 experimental) pero NO se avisa (Discord de Implicación apagado)",
      k1["regla"] == "IMP-R3" and k1["nivel"] == "experimental" and k1["publicable"] == 0 and "IMP_DISCORD_ACTIVO" in k1["motivo_no_publicable"], k1)
prueba = picks.crear_prueba(conn, "ruben@fulltennis")

# ═════════════════════════════════════════════════════════════════════
print("\n2 · La prueba controlada: canal + voz, acuse y encendido automático")
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
fti_dcfg.sincronizar(fti, cli)
r1 = fti_imp.sincronizar(fti, cli, datetime.now(timezone.utc))
check("con Discord apagado viaja SOLO la prueba", r1["avisados"] == 1
      and [s["jugador1"] for s in fti.execute("SELECT jugador1 FROM senales WHERE tipo='IMPLICACION'")] == ["Jugador Prueba"], r1)

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


_CURSOR = [0]                    # como el bot de verdad: sigue desde la última señal que leyó


def correr(ciclos=1):
    async def _c():
        f = Fuente("http://ft-intel", "secreto", transport=httpx.ASGITransport(app=fti_app))
        a = Anunciador(f, publicar, encolar, "America/Bogota", dconf.TIPOS_TEXTO, dconf.TIPOS_VOZ, 30,
                       reloj=lambda: datetime.now(timezone.utc))
        await a.arrancar(_CURSOR[0])
        out = [await a.ciclo() for _ in range(ciclos)]
        _CURSOR[0] = a.cursor
        await f.cerrar()
        return out
    return asyncio.run(_c())


c1 = correr()[0]
check("FT Discord publica la prueba en el canal y la encola en la voz", c1["texto"] == 1 and c1["voz"] == 1
      and publicados[-1].startswith("🧪 **PRUEBA DEL CIRCUITO**") and dichos[-1].startswith("Atención FullTennis. Prueba del circuito."), c1)
acu = fti.execute("SELECT texto, voz FROM senal_acuses").fetchall()
check("…y le ACUSA a FT Intelligence lo que hizo (texto ✔, voz ✔)", [tuple(x) for x in acu] == [(1, 1)], [tuple(x) for x in acu])
rep = fti_imp.reportar_dichas(fti, cli, datetime.now(timezone.utc))
pr = dict(conn.execute("SELECT dicho_discord, dicho_texto, dicho_voz FROM implicacion_picks WHERE id=?", (prueba["id"],)).fetchone())
check("FT Intelligence lo informa a RankingFTR: la prueba queda dicha con texto y voz", rep["reportadas"] == 1
      and pr == {"dicho_discord": 1, "dicho_texto": 1, "dicho_voz": 1}, (rep, pr))
c2 = picks.cfg()
check("✅ ENCENDIDO AUTOMÁTICO: IMP_DISCORD_ACTIVO = 1 e IMP_DISCORD_EXPERIMENTAL = 1, con constancia de la prueba",
      c2["IMP_DISCORD_ACTIVO"] is True and c2["IMP_DISCORD_EXPERIMENTAL"] is True
      and (picks._discord_estado(conn)["auto_activado"] or {}).get("por", "").startswith(f"auto: prueba #{prueba['id']}"), c2)
check("los demás tipos de Discord no cambiaron (Admin → Discord intacto)",
      {t: v for t, v in dcfg.para_discord(conn)["tipos"].items() if t != "IMPLICACION"} == OTROS_ANTES)

# ═════════════════════════════════════════════════════════════════════
print("\n3 · Una oportunidad real: IMP-R3 sale como 🧪 EXPERIMENTAL — EN ESTUDIO")
p2 = partido(2, jugador1="Taylor Fritz", jugador2="Arthur Fils", torneo="ATP Viena - R16", torneo_base="ATP Viena",
             defensa1=330, puntos1=1300, rank1=40, fav_pre_cuota=1.28, cuota_perdedor_set1=2.75)
p3 = partido(3, jugador1="Jugador Medio", jugador2="Otro", defensa1=150, puntos1=1200, rank1=60)   # 12.5 % → R1, implicación MEDIA
candidatos.registrar(conn, AHORA)
picks.detectar(conn, AHORA)
k2 = dict(conn.execute("SELECT * FROM implicacion_picks WHERE partido_id=?", (p2,)).fetchone())
k3 = dict(conn.execute("SELECT * FROM implicacion_picks WHERE partido_id=?", (p3,)).fetchone())
check("el IMP-R3 queda publicable con aviso 'experimental' y su nivel SIGUE 'experimental' (no se lo valida para publicarlo)",
      k2["regla"] == "IMP-R3" and k2["publicable"] == 1 and k2["aviso"] == "experimental" and k2["nivel"] == "experimental"
      and picks.niveles(conn)["IMP-R3"] == "experimental", k2)
check("un R1 con implicación MEDIA no se avisa: su regla no está habilitada (motivo guardado)",
      k3["publicable"] == 0 and "IMP_DISCORD_EXPERIMENTAL_REGLAS" in k3["motivo_no_publicable"], k3["motivo_no_publicable"])
r2 = fti_imp.sincronizar(fti, cli, datetime.now(timezone.utc))
c3 = correr()[0]
msg = publicados[-1]
print("\n   " + msg.replace("\n", "\n   "))
print("   🔊 " + dichos[-1])
check("FT Discord lo publica Y lo dice por voz (1 texto, 1 voz)", r2["avisados"] == 1 and c3["texto"] == 1 and c3["voz"] == 1, (r2, c3))
check("primera línea «🧪 EXPERIMENTAL — EN ESTUDIO» (aunque Admin tenga encabezado) y pie experimental, nunca como validado",
      msg.startswith("🧪 **EXPERIMENTAL — EN ESTUDIO**") and "no es un pick validado" in msg.split("\n")[0]
      and msg.split("\n")[-1].startswith("_🧪 Experimental · en estudio · IMP-R3") and "En vivo · IMP" not in msg, msg.split("\n")[-1])
check("trae partido y torneo, favorito PRE con su cuota PRE de Pinnacle, cuota LIVE verificada",
      "ATP Viena" in msg and "🎾 **Taylor Fritz** vs Arthur Fils" in msg and "⭐ Favorito PRE: Taylor Fritz · cuota PRE Pinnacle 1.28" in msg
      and "💰 Cuota LIVE verificada: 2.75" in msg)
check("nivel de implicación y puntos que defiende; UTR, Elo y FTR", "🔴 Implicación ALTA · defiende 330 puntos (25 % de sus puntos)" in msg
      and "📊 UTR 13.50 vs 13.00 · Elo 70 % · FTR 62 %" in msg)
check("motivo exacto de la alerta", "📌 Motivo: IMP-R3 — favorito PRE (1.28) perdió el 1.er set 4-6 con implicación ALTA" in msg
      and "defiende 330 pts (25 % de sus puntos)" in msg)
check("voz: 'Atención FullTennis. Señal experimental, en estudio. Implicación de torneo. Taylor Fritz…'",
      dichos[-1].startswith("Atención FullTennis. Señal experimental, en estudio. Implicación de torneo. Taylor Fritz"), dichos[-1])

print("\n4 · Sin duplicados, acuse, resultado y apagado")
check("una segunda vuelta no repite nada (FT Intelligence ni FT Discord)",
      fti_imp.sincronizar(fti, cli, datetime.now(timezone.utc))["avisados"] == 0 and correr()[0]["texto"] == 0)
p2b = partido(22, clave="e2e22-otro-registro", jugador1="Taylor Fritz", jugador2="Arthur Fils", torneo="ATP Viena - R16",
              torneo_base="ATP Viena", defensa1=330, puntos1=1300, rank1=40)
candidatos.registrar(conn, AHORA)
picks.detectar(conn, AHORA)
k2b = conn.execute("SELECT 1 FROM implicacion_picks WHERE partido_id=?", (p2b,)).fetchone()
ev2b = conn.execute("SELECT motivo FROM implicacion_pick_eval WHERE partido_id=?", (p2b,)).fetchone()
check("el mismo partido llegado por otro registro: ni otro pick ni otro aviso (motivo guardado)", k2b is None
      and "ya es pick" in (ev2b[0] if ev2b else ""), ev2b and ev2b[0])
fti_imp.reportar_dichas(fti, cli, datetime.now(timezone.utc))
k2 = dict(conn.execute("SELECT dicho_discord, dicho_texto, dicho_voz FROM implicacion_picks WHERE partido_id=?", (p2,)).fetchone())
check("vuelve el acuse del aviso real: dicho, texto y voz", k2 == {"dicho_discord": 1, "dicho_texto": 1, "dicho_voz": 1}, k2)
conn.execute("UPDATE implicacion_partidos SET cerrado_en=?, ganador=1, remonto=1, marcador_final='4-6 6-3 6-4' WHERE id=?", (AHORA.isoformat(), p2))
conn.commit()
picks.cerrar(conn, AHORA)
k2 = dict(conn.execute("SELECT resultado, marcador FROM implicacion_picks WHERE partido_id=?", (p2,)).fetchone())
from production.engines import dashboard_estrategia as de  # noqa: E402
h = [x for x in de.implicacion(conn, vista="todos")["historial"] if x["seleccion"] == "Taylor Fritz"][0]
check("se guarda el resultado final y queda en el historial (+1.75 u, '📣 avisado en Discord como 🧪 experimental')",
      k2["resultado"] == "ganado" and h["unidades"] == 1.75 and "como 🧪 experimental" in h["detalle"], (k2, h))
set_valor("IMP_DISCORD_EXPERIMENTAL", "0", "e2e")
feed = tc.get("/ft-intel/implicacion", headers={"X-FTR-Internal-Secret": "secreto"}).json()["implicacion"]
check("apagar la opción de experimentales corta el envío en el acto (quedan solo pruebas)", all(f["prueba"] == 1 for f in feed))
check("y apagarla NO toca a los demás bots", {t: v for t, v in dcfg.para_discord(conn)["tipos"].items() if t != "IMPLICACION"} == OTROS_ANTES
      and obtener_bool("IMP_DISCORD_ACTIVO") is True)

print(f"\n{ok} OK, {fallas} fallas")
sys.exit(1 if fallas else 0)
