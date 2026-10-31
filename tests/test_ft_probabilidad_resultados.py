"""/ft-intel/probabilidad/resultados (30/10/2026): partidos terminados con ganador para etiquetar predicciones."""
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

con = sqlite3.connect(DB)
con.executescript("""
CREATE TABLE players (player_id INTEGER PRIMARY KEY, nombre_canonico TEXT, nombre_norm TEXT, genero TEXT);
CREATE TABLE match_log (match_id INTEGER PRIMARY KEY AUTOINCREMENT, player_id INT, opponent_id INT, fecha TEXT,
  nivel_torneo TEXT, superficie TEXT, score TEXT, games_ganados INT, games_perdidos INT,
  sets_ganados INT, sets_perdidos INT, torneo TEXT);
""")
for i, n in enumerate(["Billy Harris", "Jose Pereira", "Ana Ret", "Beto Ret", "Cai Wo", "Dan Wo", "Eli Ok", "Fer Ok"], 1):
    con.execute("INSERT INTO players VALUES (?,?,?,?)", (i, n, n.lower(), "M"))
hoy = datetime.now(timezone.utc).date().isoformat()
viejo = (datetime.now(timezone.utc) - timedelta(days=30)).date().isoformat()


def partido(gan, per, score, sg, sp, fecha=hoy):
    # cuatro filas por partido: dos caras x (superficie real, 'general')
    for sup in ("dura", "general"):
        con.execute("INSERT INTO match_log (player_id,opponent_id,fecha,nivel_torneo,superficie,score,games_ganados,"
                    "games_perdidos,sets_ganados,sets_perdidos,torneo) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (gan, per, fecha, "challenger", sup, score, 12, 7, sg, sp, "ATP Challenger X"))
        con.execute("INSERT INTO match_log (player_id,opponent_id,fecha,nivel_torneo,superficie,score,games_ganados,"
                    "games_perdidos,sets_ganados,sets_perdidos,torneo) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (per, gan, fecha, "challenger", sup, score, 7, 12, sp, sg, "ATP Challenger X"))


partido(1, 2, "6-3 6-4", 2, 0)             # válido
partido(3, 4, "6-3 4-1 ret.", 1, 0)        # retiro
partido(5, 6, "w/o", 1, 0)                 # walkover
partido(7, 8, "6-3 6-4", 2, 0, fecha=viejo)  # fuera de la ventana
con.commit()
con.close()

from fastapi.testclient import TestClient  # noqa: E402
from production.api.app import app  # noqa: E402

FALLOS = []


def check(n, cond):
    print(("  OK    " if cond else "  FALLA ") + n)
    if not cond:
        FALLOS.append(n)


c = TestClient(app, headers={"X-FTR-Internal-Secret": os.environ["FTR_INTERNAL_SECRET"]})
r = c.get("/ft-intel/probabilidad/resultados?dias=5")
check("responde 200", r.status_code == 200)
d = r.json()
res = d["resultados"]
check("un solo partido válido (sin duplicados por cara/superficie)", len(res) == 1)
check("ganador y perdedor correctos", res and res[0]["ganador"] == "Billy Harris" and res[0]["perdedor"] == "Jose Pereira")
check("el retiro queda fuera", all(x["ganador"] != "Ana Ret" for x in res))
check("el walkover queda fuera", all(x["ganador"] != "Cai Wo" for x in res))
check("fuera de la ventana no entra", all(x["ganador"] != "Eli Ok" for x in res))
check("se informa cuántos se excluyeron", d["excluidos"]["retiro_o_incompleto"] >= 2)
check("sin secreto, no autorizado", TestClient(app).get("/ft-intel/probabilidad/resultados").status_code in (401, 403))
r2 = c.get("/ft-intel/probabilidad/resultados?dias=60")
check("con ventana de 14 días máximo, rechaza 60", r2.status_code == 422)

print("\nFALLAS:", FALLOS if FALLOS else "ninguna")
sys.exit(1 if FALLOS else 0)
