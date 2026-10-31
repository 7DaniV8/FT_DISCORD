"""Candidatos de FT PROBABILIDAD para Discord (30/10/2026): regla, etapas, textos, ruta y Admin."""
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone

_TMP = tempfile.mkdtemp()
os.environ.setdefault("ADMIN_TOKEN", "test_token_admin_123456")
os.environ.setdefault("FTR_INTERNAL_SECRET", "secreto_interno_de_prueba_123")
os.environ.setdefault("RESEARCH_TOKEN", "research_token_de_prueba_1234")
DB = os.path.join(_TMP, "prueba.db")
os.environ["FTR_DB_PATH"] = DB
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FALLOS = []


def check(n, cond):
    print(("  OK    " if cond else "  FALLA ") + n)
    if not cond:
        FALLOS.append(n)


AHORA = datetime(2026, 10, 30, 20, 0, tzinfo=timezone.utc)
from production.engines import ft_prob_candidatos as fpc  # noqa: E402


def partido(prob_a=.642, inicio_min=120, id_ftr="501"):
    return {"fixture_id": "FT_abc", "id_ftr": id_ftr, "jugadores": ["Billy Harris", "Jose Pereira"],
            "probabilidad": {"Billy Harris": prob_a, "Jose Pereira": 1 - prob_a},
            "inicio_utc": (AHORA + timedelta(minutes=inicio_min)).isoformat(),
            "torneo": "ATP Challenger Seoul", "superficie": "Dura",
            "componentes": {"TENNIS_DATA": {"A": .70, "B": .30}, "UTR": {"A": .61, "B": .39},
                            "CUOTA": {"A": .536, "B": .464}}}


def cuotas(o1=1.82, o2=2.10, act=None):
    return {"501": {"odd1": o1, "odd2": o2, "actualizado_en": (act or AHORA - timedelta(minutes=5)).isoformat()}}


def ev(p, q, minutos=10, **kw):
    return fpc.evaluar([p], q, AHORA, .60, 1.70, minutos)


r = ev(partido(), cuotas())
check("cumple: un candidato (A)", len(r["candidatos"]) == 1 and r["candidatos"][0]["jugador"] == "Billy Harris")
check("falta mucho: etapa DETECTADO", r["candidatos"][0]["etapa"] == "DETECTADO")
check("valor esperado correcto", abs(r["candidatos"][0]["ev"] - (.642 * 1.82 - 1)) < 1e-9)
check("faltan 8 min: etapa INICIO", ev(partido(inicio_min=8), cuotas())["candidatos"][0]["etapa"] == "INICIO")
check("justo 10 min: INICIO", ev(partido(inicio_min=10), cuotas())["candidatos"][0]["etapa"] == "INICIO")
check("prob exactamente 60 % no cumple", ev(partido(prob_a=.60), cuotas())["candidatos"] == [])
check("cuota exactamente 1.70 no cumple", ev(partido(), cuotas(o1=1.70))["candidatos"] == [])
check("cuota baja desde que se calculó: no avisa", ev(partido(), cuotas(o1=1.55))["candidatos"] == [])
check("cuota vieja (>3 h): no avisa", ev(partido(), cuotas(act=AHORA - timedelta(hours=5)))["candidatos"] == [])
check("sin hora de cuota legible: se evalúa igual",
      len(ev(partido(), {"501": {"odd1": 1.82, "odd2": 2.1, "actualizado_en": "basura"}})["candidatos"]) == 1)
check("partido ya empezado: no avisa", ev(partido(inicio_min=-5), cuotas())["candidatos"] == [])
check("sin cuota actual en RankingFTR: no avisa", ev(partido(), {})["candidatos"] == [])
check("sin id de RankingFTR: no avisa", ev(partido(id_ftr=""), cuotas())["candidatos"] == [])
rb = ev(partido(prob_a=.38), cuotas(o1=2.8, o2=1.85))
check("el candidato puede ser el jugador B", len(rb["candidatos"]) == 1 and rb["candidatos"][0]["jugador"] == "Jose Pereira"
      and abs(rb["candidatos"][0]["cuota"] - 1.85) < 1e-9)
check("ambos no pueden cumplir a la vez con probabilidades complementarias",
      len(ev(partido(prob_a=.61), cuotas(o1=1.9, o2=3.0))["candidatos"]) == 1)
check("probabilidad nula: se ignora", ev({**partido(), "probabilidad": None}, cuotas())["candidatos"] == [])

d = ev(partido(), cuotas())["candidatos"][0]
check("texto: encabezado y candidato", d["texto_discord"].startswith("🎲 FULLTENNIS · FT PROBABILIDAD") and "**Billy Harris**" in d["texto_discord"])
check("texto: probabilidad, cuota y valor esperado", "64,2 %" in d["texto_discord"] and "1.82" in d["texto_discord"] and "+16,8 %" in d["texto_discord"])
check("texto: componentes y advertencia", "Tennis Data 70 %" in d["texto_discord"] and "Experimental" in d["texto_discord"])
check("texto: marca de tiempo de Discord", "<t:" in d["texto_discord"])
check("voz detectado", d["texto_voz"].startswith("Atención FullTennis. FT Probabilidad detecta un candidato. Billy Harris, sesenta y cuatro por ciento. Cuota uno punto ochenta y dos."))
i = ev(partido(inicio_min=10), cuotas())["candidatos"][0]
check("voz inicio", i["texto_voz"] == "Atención FullTennis. En diez minutos, Billy Harris contra Jose Pereira. FT Probabilidad: Billy Harris, sesenta y cuatro por ciento. Cuota uno punto ochenta y dos. Modelo experimental.")
check("encabezado de inicio dice los minutos", "EMPIEZA EN 10 MIN" in i["texto_discord"].split("\n")[0])
check("cuota con cero: dos punto cero cinco", fpc._decimal_hablado(2.05) == "dos punto cero cinco")
check("cuota redonda: uno punto setenta y cinco", fpc._decimal_hablado(1.75) == "uno punto setenta y cinco")

# ---- Admin: Discord y regla -------------------------------------------------------------------------------
from production.api import config_estrategias as ce  # noqa: E402
from production.engines import discord_config as dc  # noqa: E402
tipos = {t[0]: t for t in dc.CATALOGO}
check("Discord: tipo detectado (texto sí, voz no)", tipos["PROB_CANDIDATO"][3] == 1 and tipos["PROB_CANDIDATO"][4] == 0)
check("Discord: tipo al empezar (texto y voz)", tipos["PROB_CANDIDATO_INICIO"][3] == 1 and tipos["PROB_CANDIDATO_INICIO"][4] == 1)
for k, malo in (("FT_PROB_CAND_PROB_MIN", "30"), ("FT_PROB_CAND_PROB_MIN", "abc"), ("FT_PROB_CAND_CUOTA_MIN", "0.5"),
                ("FT_PROB_CAND_MIN_ANTES", "0"), ("FT_PROB_CAND_MIN_ANTES", "7.5")):
    try:
        ce.set_valor(k, malo)
        check(f"Admin rechaza {k}={malo}", False)
    except ValueError:
        check(f"Admin rechaza {k}={malo}", True)
ce.set_valor("FT_PROB_CAND_PROB_MIN", "62,5")
check("Admin acepta coma decimal", abs(ce.obtener_float("FT_PROB_CAND_PROB_MIN") - 62.5) < 1e-9)
ce.set_valor("FT_PROB_CAND_PROB_MIN", "60")

# ---- Ruta -------------------------------------------------------------------------------------------------
con = sqlite3.connect(DB)
con.execute("CREATE TABLE fixtures_ftr_elo (fixture_id INTEGER PRIMARY KEY, fecha TEXT, odd1 REAL, odd2 REAL, actualizado_en TEXT)")
con.execute("INSERT INTO fixtures_ftr_elo VALUES (501, '2026-10-30', 1.82, 2.10, ?)", (datetime.now(timezone.utc).isoformat(),))
con.commit()
con.close()
from fastapi.testclient import TestClient  # noqa: E402
from production.api.app import app  # noqa: E402
import research.api.routes_ft_probabilidad as rfp  # noqa: E402

ahora_real = datetime.now(timezone.utc)
p_real = {**partido(inicio_min=120), "inicio_utc": (ahora_real + timedelta(hours=2)).isoformat()}


class R:
    status_code = 200

    def json(self):
        return {"partidos": [p_real]}


rfp._pedir = lambda ruta, params=None: (R(), None)
c = TestClient(app, headers={"X-FTR-Internal-Secret": os.environ["FTR_INTERNAL_SECRET"]})
U = "/ft-intel/probabilidad/candidatos"
j0 = c.get(U).json()
check("apagado: lista vacía con motivo", j0["candidatos"] == [] and j0["motivo"] == "apagado")
ce.set_valor("FT_PROBABILIDAD_ACTIVO", "1")
j = c.get(U).json()
check("encendido: un candidato DETECTADO", len(j["candidatos"]) == 1 and j["candidatos"][0]["etapa"] == "DETECTADO")
check("la ruta informa la regla", j["regla"] == {"prob_min": 0.6, "cuota_min": 1.7, "min_antes": 10})
ce.set_valor("FT_PROB_CAND_CUOTA_MIN", "1.90")
check("subir la cuota mínima en Admin lo saca", c.get(U).json()["candidatos"] == [])
ce.set_valor("FT_PROB_CAND_CUOTA_MIN", "1.70")
rfp._pedir = lambda ruta, params=None: (None, {"ok": False, "motivo": "sin_conexion"})
check("servicio caído: vacío con motivo", c.get(U).json()["motivo"] == "sin_conexion")
check("sin secreto: no autorizado", TestClient(app).get(U).status_code in (401, 403))

# ── Historial de la pestaña (dashboard «solo enviado a Discord») ──
rfp._pedir = lambda ruta, params=None: (R(), None)
c.get(U)
con = sqlite3.connect(DB)
n_reg = con.execute("SELECT COUNT(*), SUM(discord_sent) FROM ft_prob_candidatos_registro").fetchone()
con.close()
check("la ruta registra el candidato enviado (1 fila, discord_sent=1)", n_reg[0] == 1 and n_reg[1] == 1)
c.get(U)
con = sqlite3.connect(DB)
check("consultar de nuevo no duplica", con.execute("SELECT COUNT(*) FROM ft_prob_candidatos_registro").fetchone()[0] == 1)
con.close()
rd = c.get("/research/api/dashboard/ft-probabilidad?token=" + os.environ["RESEARCH_TOKEN"])
check("ruta del dashboard responde con KPIs e historial",
      rd.status_code == 200 and rd.json()["kpis"]["total"] == 1 and rd.json()["kpis"]["pendientes"] == 1
      and rd.json()["historial"][0]["seleccion"] == "Billy Harris")

print("\nFALLAS:", FALLOS if FALLOS else "ninguna")
sys.exit(1 if FALLOS else 0)
