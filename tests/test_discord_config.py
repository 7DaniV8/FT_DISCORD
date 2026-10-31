"""
tests/test_discord_config.py — 📣 Admin → Discord (08/10/2026, Rubén: "activar,
desactivar y modificar todo lo que llega a Discord"). Base DESECHABLE:

    /opt/venv/bin/python /app/tests/test_discord_config.py
"""
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
_tmp = tempfile.mkdtemp()
_DB = os.path.join(_tmp, "dc.db")
os.environ["FTR_DB_PATH"] = _DB
os.environ["DB_PATH"] = _DB
os.environ.setdefault("ADMIN_TOKEN", "prueba")
os.environ.setdefault("RESEARCH_TOKEN", "prueba")
os.environ["FTR_INTERNAL_SECRET"] = "secreto-interno-prueba"
os.environ.setdefault("RAPIDAPI_KEY", "x")
os.environ["FULLTENIS_ELO_DB_PATH"] = os.path.join(_tmp, "no_existe_elo.sqlite")

fallos = []


def check(nombre, cond, detalle=""):
    if cond:
        print(f"  OK   {nombre}")
    else:
        print(f"  FALLA {nombre} — {detalle}")
        fallos.append(nombre)


from production.database.schema import SCHEMA  # noqa: E402
_c = sqlite3.connect(_DB)
_c.executescript(SCHEMA)
_c.commit()
_c.close()
import production.api.deps as deps  # noqa: E402


def _get_conn():
    c = sqlite3.connect(_DB)
    c.row_factory = sqlite3.Row
    return c


deps.get_conn = _get_conn
import production.api.routes_public as rp  # noqa: E402
rp.get_conn = _get_conn
from production.engines import discord_config as dc  # noqa: E402

conn = _get_conn()
print("=== 1 · Catálogo y defaults ===")
l = dc.listar(conn)
tipos = {t["tipo"]: t for t in l["tipos"]}
check("23 tipos, todos los que FT Discord sabe anunciar (08/10: + 3 de FT NEWS; 11/10: + RECORDADOR_IA y RECORDADOR_VIVO; 13/10: + IMPLICACION; 15/10: + MOVIMIENTO_RENTABLE; v54i: + UNDER_UTR; 30/10: + 2 de FT PROBABILIDAD)", len(tipos) == 23 and "PROB_CANDIDATO" in tipos and "PROB_CANDIDATO_INICIO" in tipos and "UNDER_UTR" in tipos and "MOVIMIENTO_RENTABLE" in tipos and "IMPLICACION" in tipos and "NEWS_VALOR" in tipos and "CUOTA_MAL_PUESTA" in tipos and "MTO_VIVO" in tipos)
check("defaults = lo que hoy suena: CMP texto+voz; APRENDIZAJE solo texto; MAESTRO_AVISO apagado",
      tipos["CUOTA_MAL_PUESTA"]["texto"] == 1 and tipos["CUOTA_MAL_PUESTA"]["voz"] == 1
      and tipos["APRENDIZAJE"]["voz"] == 0 and tipos["MAESTRO_AVISO"]["texto"] == 0)
check("sin pausa de fábrica", l["pausa"] == 0)
check("09/10 23:13: las tres de FT NEWS encendidas de fábrica (texto y voz), encabezado FULLTENNIS",
      all(tipos[t]["texto"] == 1 and tipos[t]["voz"] == 1 for t in ("NEWS_VALOR", "NEWS_INTERESANTE", "NEWS_NOTICIA"))
      and tipos["NEWS_VALOR"]["encabezado_default"].startswith("🔥 FULLTENNIS") and tipos["NEWS_VALOR"]["encabezado"] == "")
f = dc.para_discord(conn)
check("el feed trae TODOS los tipos con valor efectivo, sin nada guardado todavía",
      len(f["tipos"]) == 23 and f["tipos"]["RECORDADOR"] == {"texto": True, "voz": True, "encabezado": None, "voz_inicio": None}
      and f["pausa"] is False)

print("\n=== 2 · Guardar, restablecer, pausar ===")
l = dc.guardar(conn, "CUOTA_MAL_PUESTA", None, False, "🔥 FULLTENIS PREMIUM · CUOTA MAL PUESTA", "Atención FullTennis", "dani@gmail.com")
t = {x["tipo"]: x for x in l["tipos"]}["CUOTA_MAL_PUESTA"]
check("apagar la voz sin tocar el texto; encabezado y frase guardados; quién y cuándo",
      t["texto"] == 1 and t["voz"] == 0 and t["encabezado"].startswith("🔥 FULLTENIS PREMIUM") and t["voz_inicio"] == "Atención FullTennis"
      and t["actualizado_por"] == "dani@gmail.com" and t["personalizado"], t)
l = dc.guardar(conn, "CUOTA_MAL_PUESTA", False, None, None, None, "dani@gmail.com")
t = {x["tipo"]: x for x in l["tipos"]}["CUOTA_MAL_PUESTA"]
check("apagar el texto conserva lo demás", t["texto"] == 0 and t["voz"] == 0 and t["voz_inicio"] == "Atención FullTennis")
l = dc.guardar(conn, "CUOTA_MAL_PUESTA", True, True, "", "", "dani@gmail.com")
t = {x["tipo"]: x for x in l["tipos"]}["CUOTA_MAL_PUESTA"]
check("vaciar encabezado/frase = volver a 'como viene'", t["encabezado"] == "" and t["voz_inicio"] == "" and t["texto"] == 1)
l = dc.guardar(conn, "MAESTRO_AVISO", True, True, None, None, "dani@gmail.com")
check("encender un tipo hoy apagado (Maestro aviso)", {x["tipo"]: x for x in l["tipos"]}["MAESTRO_AVISO"]["texto"] == 1)
l = dc.restablecer(conn, "MAESTRO_AVISO")
check("restablecer vuelve a fábrica", {x["tipo"]: x for x in l["tipos"]}["MAESTRO_AVISO"]["texto"] == 0
      and not {x["tipo"]: x for x in l["tipos"]}["MAESTRO_AVISO"]["personalizado"])
for em, vi in (("x" * 121, None), (None, "y" * 201)):
    try:
        dc.guardar(conn, "RECORDADOR", None, None, em, vi, "x")
        check("rechaza textos demasiado largos", False)
    except ValueError:
        check("rechaza textos demasiado largos", True)
try:
    dc.guardar(conn, "NO_EXISTE", True, True, None, None, "x")
    check("tipo desconocido → error", False)
except ValueError:
    check("tipo desconocido → error", True)
l = dc.pausar(conn, True, "dani@gmail.com")
check("pausa general", l["pausa"] == 1 and l["pausa_por"] == "dani@gmail.com" and dc.para_discord(conn)["pausa"] is True)
l = dc.pausar(conn, False, "dani@gmail.com")
check("reanudar", l["pausa"] == 0 and dc.para_discord(conn)["pausa"] is False)

print("\n=== 3 · API ===")
from production.api.auth import crear_usuario  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from production.api.app import app  # noqa: E402
crear_usuario(conn, "admin.dc.ftr@gmail.com", "Clave-Larga-123!", nombre="Admin Prueba", rol="admin")
crear_usuario(conn, "lector.dc.ftr@gmail.com", "Clave-Larga-123!", nombre="Lector Prueba", rol="lectura")
conn.commit()


def cliente(u):
    c = TestClient(app, base_url="https://testserver")
    assert c.post("/auth/login", json={"usuario": u, "password": "Clave-Larga-123!"}).status_code == 200
    return c


ca, cl = cliente("admin.dc.ftr@gmail.com"), cliente("lector.dc.ftr@gmail.com")
r = ca.get("/admin/discord/config")
check("GET /admin/discord/config (admin)", r.status_code == 200 and len(r.json()["tipos"]) == 23, r.text[:120])
check("lectura no puede (403)", cl.get("/admin/discord/config").status_code == 403
      and cl.post("/admin/discord/config", json={"tipo": "RECORDADOR", "voz": False}).status_code == 403)
r = ca.post("/admin/discord/config", json={"tipo": "RECORDADOR", "voz": False, "encabezado": "⏰ RECORDADOR FULLTENIS"})
check("POST guarda y devuelve la lista", r.status_code == 200 and {t["tipo"]: t for t in r.json()["tipos"]}["RECORDADOR"]["voz"] == 0)
check("POST pausa", ca.post("/admin/discord/config", json={"pausa": True}).json()["pausa"] == 1)
check("POST tipo inválido → 400", ca.post("/admin/discord/config", json={"tipo": "ZZZ", "voz": True}).status_code == 400)
ff = TestClient(app, base_url="https://testserver").get("/ft-intel/discord-config", headers={"X-FTR-Internal-Secret": "secreto-interno-prueba"})
check("GET /ft-intel/discord-config con el secreto: pausa y RECORDADOR voz=false, encabezado",
      ff.status_code == 200 and ff.json()["pausa"] is True and ff.json()["tipos"]["RECORDADOR"]["voz"] is False
      and ff.json()["tipos"]["RECORDADOR"]["encabezado"] == "⏰ RECORDADOR FULLTENIS", ff.text[:200])
check("sin secreto → 401/403", TestClient(app, base_url="https://testserver").get("/ft-intel/discord-config").status_code in (401, 403))
h = ca.get("/admin/").text if ca.get("/admin/").status_code == 200 else ca.get("/admin").text
check("el panel de Admin tiene la pestaña Discord", 'id="t-discord"' in h and 'id="vista-discord"' in h and "dc-pausa" in h)

print()
print("TODO OK" if not fallos else f"FALLARON {len(fallos)}: {fallos}")
sys.exit(1 if fallos else 0)
