#!/usr/bin/env python3
"""
tests/test_recorrido_completo.py — El recorrido ENTERO, con el código real de
los tres servicios (07/10/2026, Rubén: "quiero que me confirmes no solamente
que el código está, sino que las señales correctas realmente llegan hasta
Discord y que los casos descartados quedan guardados").

    RANKINGFTR_REPO=/ruta/RankingFTR FT_INTEL_REPO=/ruta/FT_INTELLIGENCE \\
        python tests/test_recorrido_completo.py
Sin esas dos variables, se saltea.

  RankingFTR (motores reales, base temporal con el esquema real)
     │  Cuota Errada · UTR VALUE · UTR MARKET ANOMALY → tablas + Telegram
     ▼  router real /ft-intel/* (TestClient, con el secreto interno)
  FT Intelligence (sincronizar + anunciar al empezar / en el acto)
     ▼  /senales real (ASGI)
  FT Discord (lo que el bot escribe y dice)

Los casos son los de la verificación pedida:
  Cuota Errada   FT 59 % + mercado 35 % → NO · 61/50 (+11) → 🔥 · 61/53 (+8) → NO ·
                 66/48 → 💎 fuerte
  UTR VALUE      +0.80 @1.75 con Elo y FTR EN CONTRA → sí · +0.70 @2.50 → no ·
                 +1.00 @1.60 → no · +0.80 @4.50 → sí, y como además es Cuota
                 Errada y anomalía, UN solo mensaje
  ANOMALY        +0.31 PRE 2.45 → EN VIVO 0-0 @4.20 → sí · 0-3 @4.20 → no (estudio)
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
if not (RFTR and (Path(RFTR) / "production" / "api" / "routes_ft_intel.py").exists()
        and FTI and (Path(FTI) / "ft_intelligence" / "al_empezar.py").exists()):
    print("Saltado: definir RANKINGFTR_REPO y FT_INTEL_REPO.")
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
    # El requisito "L10 con datos suficientes" de Cuota Errada es un control
    # aparte que NO cambió; acá se apaga para aislar la puerta 60 % / +10 pp
    # con jugadores sin historia (la probabilidad FT queda exacta).
    "CMP_DISCORD_EXIGIR_L10": "0",
})

ok = fallas = 0


def check(nombre, cond, det=""):
    global ok, fallas
    ok, fallas = (ok + 1, fallas) if cond else (ok, fallas + 1)
    print(f"  {'OK   ' if cond else 'FALLA'} {nombre}" + (f" -- {det}" if det else ""))


# ═════════════════════════════════════════════════════════════════════
#  1. RankingFTR
# ═════════════════════════════════════════════════════════════════════
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
import production.api.routes_public as rp  # noqa: E402
rp.get_conn = _get_conn
import production.engines.cuota_mal_puesta as cmp  # noqa: E402
import production.engines.utr_anomalia as ua  # noqa: E402
import production.engines.utr_padron as padron  # noqa: E402
import production.engines.utr_value as uv  # noqa: E402
import shared.telegram_valor_notifier as tvn  # noqa: E402

TELEGRAM = []
tvn.notificar_categoria = lambda cat, txt, *a, **k: (TELEGRAM.append((cat, txt.split("\n")[1])) or "texto")

conn = _get_conn()
AHORA = datetime.now(timezone.utc).replace(microsecond=0)
HOY = AHORA.date().isoformat()
PARTIDO = (AHORA + timedelta(hours=3)).isoformat()
_pid = [0]


def jugador(nombre):
    _pid[0] += 1
    conn.execute("INSERT INTO players (player_id, nombre_canonico, nombre_norm, genero, fecha_seed) "
                 "VALUES (?,?,?,?,?)", (_pid[0], nombre, nombre.lower(), "M", "2026-01-01"))
    return _pid[0]


CAB = ("ftr_player_id;nombre_ftr;genero;estado;tipo_coincidencia;nivel_confianza;regla;utr_player_id;"
       "nombre_utr;utr_singles;estado_utr;utr_display;fiabilidad;utr_3_meses;utr_dura;utr_tierra;"
       "utr_hierba;is_pro;nacionalidad;ranking_utr;rank_ftr;utr_real_ftr_solo_comparacion;"
       "fecha_actualizacion;n_candidatos;alternativas;url_perfil;revision_manual;tanda")
UTR = {"Valor Alto": 13.30, "Valor Bajo": 12.50, "Corto Alto": 13.20, "Corto Bajo": 12.50,
       "Barato Alto": 13.50, "Barato Bajo": 12.50, "Doble Alto": 13.30, "Doble Bajo": 12.50,
       "Beviz L": 10.58, "Biro M": 10.27, "Pierde L": 10.58, "Gana M": 10.27}
IDS = {n: jugador(n) for n in UTR}
for n in ("Cmp Uno", "Cmp Dos", "Cmp Tres", "Cmp Cuatro", "Cmp Cinco", "Cmp Seis", "Cmp Siete", "Cmp Ocho"):
    IDS[n] = jugador(n)
conn.commit()
padron.importar(conn, ("﻿" + CAB + "\n" + "\n".join(
    f"{IDS[n]};{n};M;MATCH;AUTOMATICA;ALTA;r;{9000 + IDS[n]};{n};{u};OK;{u};100.0;{u};;;;True;ARG;;;;{HOY};1;;;;t"
    for n, u in UTR.items()) + "\n").encode("utf-8"), archivo="resultados_v2.csv")


def fx(fid, j1, j2, o1, o2, fav, prob):
    """Lo que /fixture/reportar arma para los motores (fixtures_ftr_elo + cuota)."""
    return {"fixture_id": fid, "fecha": PARTIDO, "tour": "atp", "fuente": "pinnacle", "torneo": "Challenger Prueba",
            "superficie": "hard", "genero": "M", "jugador1": j1, "jugador2": j2, "odd1": o1, "odd2": o2,
            "odd1_apertura": o1, "odd2_apertura": o2, "ftr1": 11.0, "ftr2": 10.8, "elo1": 1650, "elo2": 1640,
            "favorito_nombre": fav, "prob_ftr": prob, "prob_elo": prob, "muestra_suficiente": True,
            "player_id1": IDS.get(j1), "player_id2": IDS.get(j2)}


def reportar(f):
    """El mismo orden que fixture_reportar (07/10/2026): UTR VALUE, Cuota
    Errada, anomalía."""
    uv.registrar(conn, f)
    cmp.registrar(conn, f)
    ua.observar(conn, f["fuente"], f["jugador1"], f["jugador2"], f["odd1"], f["odd2"], en_vivo=False,
                genero="M", torneo=f["torneo"], fecha_partido=f["fecha"], fixture_id=f["fixture_id"],
                player_id1=f["player_id1"], player_id2=f["player_id2"])


def m(mk):
    return round(1 / mk, 4), round(1 / (1.04 - mk), 4)


print("\n1 · RankingFTR: los motores reales")
# Cuota Errada (jugadores sin UTR: UTR VALUE / anomalía no aplican)
CMP_CASOS = {101: ("Cmp Uno", "Cmp Dos", 0.59, 0.35), 102: ("Cmp Tres", "Cmp Cuatro", 0.61, 0.50),
             103: ("Cmp Cinco", "Cmp Seis", 0.61, 0.53), 104: ("Cmp Siete", "Cmp Ocho", 0.66, 0.48)}
for fid, (a, b, ft, mk) in CMP_CASOS.items():
    reportar(fx(fid, a, b, *m(mk), a, ft))
# UTR VALUE: +0.80 @1.75 con Elo/FTR a favor del RIVAL (FT le da 30 % al UTR superior)
reportar(fx(201, "Valor Alto", "Valor Bajo", 1.75, 2.15, "Valor Bajo", 0.70))
reportar(fx(202, "Corto Alto", "Corto Bajo", 2.50, 1.60, "Corto Bajo", 0.55))      # +0.70 @2.50
reportar(fx(203, "Barato Alto", "Barato Bajo", 1.60, 2.40, "Barato Alto", 0.62))   # +1.00 @1.60
# +0.80 @4.50 y FullTenis 66 %: es UTR VALUE, Cuota Errada 💎 y anomalía a la vez
reportar(fx(204, "Doble Alto", "Doble Bajo", 4.50, 1.25, "Doble Alto", 0.66))
# UTR MARKET ANOMALY: PRE 2.45 por Pinnacle (sin cuota de UTR VALUE: Δ +0.31)
for j1, j2, ev in (("Biro M", "Beviz L", 301), ("Gana M", "Pierde L", 302)):
    ua.observar(conn, "pinnacle", j1, j2, 1.50, 2.45, en_vivo=False, ahora=AHORA - timedelta(hours=1),
                genero="M", torneo="ITF M25 Prueba", event_id=ev)
M00 = {"games": 0, "sets": 0, "texto": "0-0", "en": AHORA.isoformat(), "inicio": AHORA.isoformat(),
       "fuente": "partidos_en_vivo", "stream": False}
ua.observar(conn, "pinnacle", "Biro M", "Beviz L", 1.17, 4.20, en_vivo=True, genero="M", event_id=301,
            torneo="ITF M25 Prueba", marcador=M00)
ua.observar(conn, "pinnacle", "Gana M", "Pierde L", 1.17, 4.20, en_vivo=True, genero="M", event_id=302,
            torneo="ITF M25 Prueba", marcador={**M00, "games": 3, "texto": "3-0"})

q = lambda sql, *a: conn.execute(sql, a).fetchall()  # noqa: E731
cm = {r["fixture_id"]: dict(r) for r in q("SELECT * FROM cmp_senales")}
check("Cuota Errada FT 59 % / 35 %: guardada como ESTUDIO, no Discord",
      cm[101]["grupo"] == "ESTUDIO" and cm[101]["discord_sent"] == 0, cm[101]["motivo_no_discord"])
check("Cuota Errada FT 61 % / 50 % (+11): 🔥 publicada", cm[102]["grupo"] == "CMP" and cm[102]["discord_sent"] == 1)
check("Cuota Errada FT 61 % / 53 % (+8): guardada (control), no Discord",
      cm[103]["discord_sent"] == 0 and cm[103]["grupo"] in ("ESTUDIO", "DESCARTE"), cm[103]["grupo"])
check("Cuota Errada FT 66 % / 48 %: 💎 PREMIUM publicada y marcada fuerte",
      cm[104]["grupo"] == "PREMIUM" and cm[104]["discord_sent"] == 1 and cm[104]["es_fuerte"] == 1)
uvr = {r["fixture_id"]: dict(r) for r in q("SELECT * FROM utr_value_registro")}
check("UTR VALUE +0.80 @1.75 con Elo y FTR en contra: publicada",
      uvr[201]["motivo"] == "ENVIADO_DISCORD" and uvr[201]["prob_elo_a"] < 0.5 and uvr[201]["prob_ftr_a"] < 0.5,
      (uvr[201]["motivo"], uvr[201]["prob_elo_a"]))
check("UTR VALUE +0.70 @2.50: no (ΔUTR bajo), guardada", uvr[202]["motivo"] == "SOLO_ESTUDIO_DELTA_BAJO"
      and uvr[202]["discord_sent"] == 0)
check("UTR VALUE +1.00 @1.60: no (cuota baja), guardada", uvr[203]["motivo"] == "SOLO_ESTUDIO_CUOTA_BAJA"
      and uvr[203]["discord_sent"] == 0)
check("UTR VALUE +0.80 @4.50: publicada", uvr[204]["motivo"] == "ENVIADO_DISCORD")
an = {r["jugador_a"]: dict(r) for r in q("SELECT * FROM utr_anomalia_registro")}
check("ANOMALY +0.31 PRE 2.45 → EN VIVO 0-0 @4.20: publicada en vivo",
      an["Beviz L"]["motivo"] == "ENVIADO_DISCORD" and an["Beviz L"]["fase_activacion"] == "vivo")
check("ANOMALY +0.31 PRE 2.45 → EN VIVO 0-3 @4.20: estudio, sin Discord",
      an["Pierde L"]["motivo"] == "ESTUDIO_VIVO_MARCADOR_AVANZADO" and an["Pierde L"]["discord_sent"] == 0)
check("el partido +0.80 @4.50: la anomalía se guarda pero no avisa (prioridad UTR VALUE)",
      an["Doble Alto"]["motivo"] == "SUPRIMIDO_POR_UTR_VALUE" and an["Doble Alto"]["discord_sent"] == 0)
tg = [(c, linea) for c, linea in TELEGRAM]
check("Telegram: un aviso por partido publicado, sin duplicar el +0.80 @4.50",
      sorted(c for c, _ in tg) == sorted(["cuota_mal_puesta", "cuota_mal_puesta", "utr_value", "utr_value",
                                          "utr_market_anomaly"]),
      tg)
check("…la Cuota Errada del partido de UTR VALUE no repitió el Telegram (queda anotado)",
      "UTR VALUE" in (cm[204]["telegram_resultado"] or ""), cm[204]["telegram_resultado"])

# ═════════════════════════════════════════════════════════════════════
#  2. FT Intelligence (contra el router REAL de RankingFTR)
# ═════════════════════════════════════════════════════════════════════
print("\n2 · FT Intelligence: sincroniza y anuncia")
import httpx  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from production.api.routes_ft_intel import router  # noqa: E402

_app = FastAPI()
_app.include_router(router)
tc = TestClient(_app)


class _Puente(httpx.BaseTransport):
    def handle_request(self, request: httpx.Request) -> httpx.Response:
        q_ = request.url.query.decode()
        r = tc.request(request.method, request.url.path + (f"?{q_}" if q_ else ""),
                       headers={k: v for k, v in request.headers.items() if k.lower().startswith("x-")})
        return httpx.Response(r.status_code, content=r.content,
                              headers={"content-type": r.headers.get("content-type", "")})


from ft_intelligence import tareas  # noqa: E402
from ft_intelligence.cliente_http import ClienteHTTP  # noqa: E402
from ft_intelligence.db import conectar  # noqa: E402

cli = ClienteHTTP("http://rankingftr.test", {"X-FTR-Internal-Secret": "secreto"}, "ftr",
                  transport=_Puente(), pausa=0)
fti = conectar()
FEEDS = [f for f in tareas.FEEDS if f[0] in ("utr_value", "cuota_mal_puesta", "utr_anomalia")]
check("orden de los feeds: UTR VALUE → Cuota Errada → anomalía",
      [f[0] for f in FEEDS] == ["utr_value", "cuota_mal_puesta", "utr_anomalia"], [f[0] for f in FEEDS])
for _, _, fn in FEEDS:
    fn(fti, cli)
anunc = lambda: [dict(r) for r in fti.execute("SELECT tipo, clave, resumen, datos_json FROM senales")]  # noqa: E731
check("antes de que empiecen: solo la anomalía EN VIVO salió (en el acto)",
      [a["tipo"] for a in anunc()] == ["UTR_MARKET_ANOMALY"], [a["tipo"] for a in anunc()])
# Empiezan los partidos pre-partido (el feed en vivo los ve).
for i, (h, a_) in enumerate((("Cmp Tres", "Cmp Cuatro"), ("Cmp Siete", "Cmp Ocho"), ("Valor Alto", "Valor Bajo"),
                             ("Doble Alto", "Doble Bajo"), ("Cmp Uno", "Cmp Dos"), ("Corto Alto", "Corto Bajo"))):
    fti.execute("INSERT INTO ftr_en_vivo (fid, home, away, inicio, primer_visto_fti, ultimo_visto_fti) "
                "VALUES (?,?,?,?, 'x', 'x')", (f"v{i}", h, a_, AHORA.isoformat()))
fti.commit()
for _, _, fn in FEEDS:
    fn(fti, cli)
tipos = sorted(a["tipo"] for a in anunc())
check("al empezar: 2 Cuota Errada + 2 UTR VALUE + 1 anomalía = 5 anuncios",
      tipos == sorted(["CUOTA_MAL_PUESTA"] * 2 + ["UTR_VALUE"] * 2 + ["UTR_MARKET_ANOMALY"]), tipos)
omit = {r["id"]: r["omitida_motivo"] for r in fti.execute("SELECT id, omitida_motivo FROM cuota_mal_puesta")}
check("la Cuota Errada del +0.80 @4.50 NO se anunció: un solo mensaje (UTR VALUE)",
      any("UTR VALUE" in (v or "") for v in omit.values()), omit)
for _, _, fn in FEEDS:
    fn(fti, cli)
check("una vuelta más no repite nada", len(anunc()) == 5, len(anunc()))

# ═════════════════════════════════════════════════════════════════════
#  3. FT Discord (contra el /senales REAL de FT Intelligence)
# ═════════════════════════════════════════════════════════════════════
print("\n3 · FT Discord: lo que el bot escribe y dice")
from ft_discord import config as dconf  # noqa: E402
from ft_discord.fuente import Fuente  # noqa: E402
from ft_discord.textos import texto_canal, texto_voz  # noqa: E402
from ft_intelligence.app import app as fti_app  # noqa: E402


async def leer():
    f = Fuente("http://ft-intel", "secreto", transport=httpx.ASGITransport(app=fti_app))
    s = await f.leer(0)
    await f.cerrar()
    return s

senales = asyncio.run(leer())
check("el bot lee las 5 señales", len(senales) == 5, len(senales))
check("los 3 tipos están habilitados por texto y por voz",
      {"CUOTA_MAL_PUESTA", "UTR_VALUE", "UTR_MARKET_ANOMALY"} <= dconf.TIPOS_TEXTO
      and {"CUOTA_MAL_PUESTA", "UTR_VALUE", "UTR_MARKET_ANOMALY"} <= dconf.TIPOS_VOZ)
por_jug = {}
for s in senales:
    t, v = texto_canal(s), texto_voz(s, "UTC", AHORA)
    por_jug[(s["tipo"], s["jugador1"])] = (t, v)
    print(f"\n   [{s['tipo']}] {t.splitlines()[0]}  ·  voz: {v[:90]}")
check("🔥 Cuota Errada FT 61 % / 50 % llega a Discord",
      any(k[0] == "CUOTA_MAL_PUESTA" and "Cmp Tres" in por_jug[k][0] and "🔥" in por_jug[k][0] for k in por_jug))
check("💎 Cuota Errada FT 66 % / 48 % llega como CUOTA MUY MAL PUESTA · PREMIUM",
      any(k[0] == "CUOTA_MAL_PUESTA" and "Cmp Siete" in por_jug[k][0] and "MUY MAL PUESTA" in por_jug[k][0]
          and "PREMIUM" in por_jug[k][0] for k in por_jug))
check("📈 UTR VALUE +0.80 @1.75 llega", any(k[0] == "UTR_VALUE" and "Valor Alto" in por_jug[k][0] for k in por_jug))
check("📈 UTR VALUE +0.80 @4.50 llega (y nada más de ese partido)",
      sum(1 for k in por_jug if "Doble Alto" in por_jug[k][0]) == 1
      and any(k[0] == "UTR_VALUE" and "Doble Alto" in por_jug[k][0] for k in por_jug))
check("🚨 ANOMALY 0-0 @4.20 llega 'En vivo' con el marcador",
      any(k[0] == "UTR_MARKET_ANOMALY" and "marcador 0-0" in por_jug[k][0]
          and por_jug[k][1].startswith("En vivo,") for k in por_jug))
check("lo descartado NO llega: 59/35, 61/53, +0.70, +1.00 @1.60, anomalía 0-3",
      not any(n in por_jug[k][0] for k in por_jug for n in ("Cmp Uno", "Cmp Cinco", "Corto Alto",
                                                              "Barato Alto", "Pierde L")))

# ═════════════════════════════════════════════════════════════════════
#  4. Lo descartado queda guardado para estudiar
# ═════════════════════════════════════════════════════════════════════
print("\n4 · Lo descartado queda guardado")
check("Cuota Errada: todos los partidos evaluados quedan guardados; los que no salen, con su motivo",
      all(f in cm for f in (101, 102, 103, 104, 204)) and all(cm[f]["motivo_no_discord"] for f in (101, 103)),
      sorted(cm))
check("UTR VALUE: los 4 partidos guardados (2 solo estudio con motivo)",
      len(uvr) >= 4 and uvr[202]["grupo"] == "ESTUDIO" and uvr[203]["grupo"] == "ESTUDIO")
check("ANOMALY: la de 0-3 guardada con su marcador, cuota PRE y de activación",
      an["Pierde L"]["marcador_activacion"] == "3-0" and an["Pierde L"]["cuota_pre"] == 2.45
      and an["Pierde L"]["cuota_activacion"] == 4.20)
_om = {r["fixture_id"]: r["omitida_motivo"] for r in fti.execute(
    "SELECT fixture_id, omitida_motivo FROM cuota_mal_puesta WHERE omitida_motivo IS NOT NULL")}
check("FT Intelligence: las Cuota Errada de partidos que salieron por UTR VALUE quedan omitidas con su motivo",
      "204" in _om and all("UTR VALUE" in v for v in _om.values()), _om)

print(f"\n{'─' * 60}\n{ok} comprobaciones OK." if not fallas else f"\n{fallas} FALLAS ({ok} OK)")
sys.exit(1 if fallas else 0)
