"""Historial/dashboard de candidatos de FT PROBABILIDAD enviados a Discord (30/10/2026)."""
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone

_TMP = tempfile.mkdtemp()
os.environ.setdefault("ADMIN_TOKEN", "test_token_admin_123456")
os.environ.setdefault("FTR_INTERNAL_SECRET", "secreto_interno_de_prueba_123")
os.environ.setdefault("RESEARCH_TOKEN", "research_token_de_prueba_1234")
os.environ["FTR_DB_PATH"] = os.path.join(_TMP, "prueba.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FALLOS = []


def check(n, cond):
    print(("  OK    " if cond else "  FALLA ") + n)
    if not cond:
        FALLOS.append(n)


from production.engines import dashboard_estrategia as de  # noqa: E402
from production.engines import discord_config, ft_prob_registro as reg  # noqa: E402
from shared.tennis_names import normalize_name  # noqa: E402

AHORA = datetime(2026, 10, 30, 20, 0, tzinfo=timezone.utc)

conn = sqlite3.connect(":memory:")
conn.row_factory = sqlite3.Row
conn.executescript("""
CREATE TABLE players (player_id INTEGER PRIMARY KEY, nombre_canonico TEXT, nombre_norm TEXT, genero TEXT);
CREATE TABLE match_log (match_id INTEGER PRIMARY KEY AUTOINCREMENT, player_id INT, opponent_id INT, fecha TEXT,
    superficie TEXT, score TEXT, sets_ganados INT, sets_perdidos INT);
""")
for i, (n, g) in enumerate([("Billy Harris", "M"), ("Jose Pereira", "M"), ("Ana Lopez", "F"), ("Eva Roca", "F")], 1):
    conn.execute("INSERT INTO players VALUES (?,?,?,?)", (i, n, normalize_name(n), g))
conn.commit()
reg._TABLA = False


def cand(jug="Billy Harris", riv="Jose Pereira", id_ftr="501", etapa="DETECTADO", cuota=1.82, prob=.642, min_ini=120):
    return {"id_ftr": id_ftr, "jugador": jug, "rival": riv, "jugador1": jug, "jugador2": riv, "torneo": "Seoul",
            "superficie": "Dura", "inicio_utc": (AHORA - timedelta(minutes=min_ini)).isoformat(),
            "etapa": etapa, "prob": prob, "cuota": cuota}


# 1) Se registra una vez por partido y jugador, aunque lo vea cada minuto
check("registra 1", reg.registrar(conn, [cand()], AHORA) == 1)
check("no repite", reg.registrar(conn, [cand(), cand(etapa="INICIO", cuota=1.9)], AHORA) == 0)
f = conn.execute("SELECT * FROM ft_prob_candidatos_registro").fetchall()
check("una sola fila", len(f) == 1)
check("conserva la cuota de la primera vez", f[0]["cuota"] == 1.82 and f[0]["etapa"] == "DETECTADO")
check("discord_sent=1 con Admin por defecto", f[0]["discord_sent"] == 1 and f[0]["enviado_en"] is not None)
check("género tomado de players", f[0]["genero"] == "M")

# 2) Con Discord apagado en Admin NO cuenta como enviado; al encenderlo sí
discord_config.guardar(conn, "PROB_CANDIDATO", False, False, None, None, "t")
reg.registrar(conn, [cand(jug="Jose Pereira", riv="Billy Harris", id_ftr="502", cuota=2.1, prob=.61)], AHORA)
g = conn.execute("SELECT discord_sent FROM ft_prob_candidatos_registro WHERE id_ftr='502'").fetchone()
check("apagado en Admin: no cuenta como enviado", g["discord_sent"] == 0)
check("el dashboard no lo muestra", de.ft_probabilidad(conn)["kpis"]["total"] == 1)
discord_config.guardar(conn, "PROB_CANDIDATO", True, True, None, None, "t")
reg.registrar(conn, [cand(jug="Jose Pereira", riv="Billy Harris", id_ftr="502", cuota=2.05, prob=.62)], AHORA)
g = conn.execute("SELECT discord_sent, cuota FROM ft_prob_candidatos_registro WHERE id_ftr='502'").fetchone()
check("al encenderlo pasa a enviado con la cuota de ese envío", g["discord_sent"] == 1 and g["cuota"] == 2.05)
conn.execute("DELETE FROM ft_prob_candidatos_registro WHERE id_ftr='502'")
conn.commit()

# 3) Cierre: no antes de 30 min del inicio; sin jugador único no se adivina
check("no cierra si no pasaron 30 min", reg.cerrar_pendientes(conn, AHORA - timedelta(minutes=150), forzar=True)["cerradas"] == 0)
conn.execute("INSERT INTO match_log (player_id, opponent_id, fecha, superficie, score, sets_ganados, sets_perdidos) "
             "VALUES (1,2,?, 'general','6-4 6-3',2,0)", ((AHORA - timedelta(minutes=120)).isoformat(),))
conn.commit()
check("cierra W", reg.cerrar_pendientes(conn, AHORA, forzar=True)["cerradas"] >= 1)
r = conn.execute("SELECT result, resultado_score FROM ft_prob_candidatos_registro WHERE id_ftr='501'").fetchone()
check("W con marcador", r["result"] == "W" and r["resultado_score"] == "6-4 6-3")

# 4) Derrota y retiro
reg.registrar(conn, [cand(jug="Ana Lopez", riv="Eva Roca", id_ftr="601", cuota=2.0, prob=.65),
                     cand(jug="Eva Roca", riv="Ana Lopez", id_ftr="602", cuota=1.9, prob=.62)], AHORA)
conn.execute("INSERT INTO match_log (player_id, opponent_id, fecha, superficie, score, sets_ganados, sets_perdidos) "
             "VALUES (4,3,?, 'general','6-2 6-1',2,0)", ((AHORA - timedelta(minutes=120)).isoformat(),))
conn.commit()
reg.cerrar_pendientes(conn, AHORA, forzar=True)
res = {r["id_ftr"]: r["result"] for r in conn.execute("SELECT id_ftr, result FROM ft_prob_candidatos_registro")}
check("derrota L", res.get("601") == "L")
check("la otra pareja es otro partido (Eva gana ese match_log: 602 W)", res.get("602") == "W")

# 5) Dashboard: mismas tarjetas que UTR VALUE
d = de.ft_probabilidad(conn)
k = d["kpis"]
check("KPIs: 3 liquidadas", k["total"] == 3 and k["ganadas"] == 2 and k["perdidas"] == 1)
check("unidades = 0.82 + 0.90 - 1", abs(k["unidades"] - 0.72) < 0.011)
check("historial con selección, cuota y marcador",
      all(h["seleccion"] and h["cuota"] and "resultado" in h for h in d["historial"]))
check("filtro por género F", de.ft_probabilidad(conn, genero="F")["kpis"]["total"] == 2)
check("filtro por género M", de.ft_probabilidad(conn, genero="M")["kpis"]["total"] == 1)

# 6) Ocultar fila (admin)
de.ocultar(conn, "ftprob", d["historial"][0]["id"], "admin")
check("ocultar quita la fila", de.ft_probabilidad(conn)["kpis"]["total"] == 2)

print("\nFALLAS:", FALLOS if FALLOS else "ninguna")
sys.exit(1 if FALLOS else 0)
