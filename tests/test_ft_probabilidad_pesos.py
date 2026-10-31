"""Pesos editables de FT PROBABILIDAD (30/10/2026): /ft-intel/probabilidad/config los entrega validados."""
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp()
os.environ.setdefault("ADMIN_TOKEN", "test_token_admin_123456")
os.environ.setdefault("FTR_INTERNAL_SECRET", "secreto_interno_de_prueba_123")
os.environ.setdefault("RESEARCH_TOKEN", "research_token_de_prueba_1234")
os.environ["FTR_DB_PATH"] = os.path.join(_TMP, "prueba.db")
for k in ("FT_PROB_PESO_TENNIS_DATA", "FT_PROB_PESO_CUOTA", "FT_PROB_PESO_UTR"):
    os.environ.pop(k, None)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402
from production.api import config_estrategias as ce  # noqa: E402
from production.api.app import app  # noqa: E402

FALLOS = []


def check(n, cond):
    print(("  OK    " if cond else "  FALLA ") + n)
    if not cond:
        FALLOS.append(n)


c = TestClient(app, headers={"X-FTR-Internal-Secret": os.environ["FTR_INTERNAL_SECRET"]})
U = "/ft-intel/probabilidad/config"

d = c.get(U).json()
check("de fábrica 60/30/10 válidos", d["pesos_validos"] and d["pesos"] == {"TENNIS_DATA": 0.6, "CUOTA": 0.3, "UTR": 0.1})
for k in ce.CLAVES_PESOS_FT_PROB:
    check(f"{k} está en el Admin", k in ce.REGISTRO and ce.REGISTRO[k]["categoria"] == "FT PROBABILIDAD")


def poner(a, b, u):
    ce.set_valor("FT_PROB_PESO_TENNIS_DATA", str(a))
    ce.set_valor("FT_PROB_PESO_CUOTA", str(b))
    ce.set_valor("FT_PROB_PESO_UTR", str(u))


def rechaza(clave, valor):
    try:
        ce.set_valor(clave, valor)
    except ValueError:
        return True
    return False


poner(50, 20, 30)
d = c.get(U).json()
check("50/20/30 se entregan como fracciones", d["pesos_validos"] and d["pesos"] == {"TENNIS_DATA": 0.5, "CUOTA": 0.2, "UTR": 0.3})
poner(70, 29, 1)
d = c.get(U).json()
check("70/29/1 (el caso real) suma 100", d["pesos_validos"] and abs(sum(d["pesos"].values()) - 1) < 1e-9
      and abs(d["pesos"]["UTR"] - 0.01) < 1e-9)
poner(70, 30, 15)
d = c.get(U).json()
check("70/30/15 (no suma 100) se reparte en proporción", d["pesos_validos"] and abs(sum(d["pesos"].values()) - 1) < 1e-9
      and abs(d["pesos"]["TENNIS_DATA"] - 70 / 115) < 1e-9)
for clave in ce.CLAVES_PESOS_FT_PROB:
    for malo in ("-1", "101", "abc", "nan", "inf"):
        check(f"el Admin rechaza {clave}={malo!r}", rechaza(clave, malo))
poner(0, 0, 0)
d = c.get(U).json()
check("0/0/0: sin pesos y con motivo", d["pesos"] is None and not d["pesos_validos"] and d["pesos_motivo"])
poner(0, 100, 0)
check("0/100/0 es válido", c.get(U).json()["pesos"] == {"TENNIS_DATA": 0.0, "CUOTA": 1.0, "UTR": 0.0})
poner("33,5", "33.5", "33")
check("decimales con coma o punto", c.get(U).json()["pesos_validos"])
ce._guardar("FT_PROB_PESO_CUOTA", "xx", "prueba")   # basura guardada por otra vía
d = c.get(U).json()
check("valor basura: pesos=None con motivo (el servicio conserva los últimos)", d["pesos"] is None and not d["pesos_validos"])
check("el interruptor sigue funcionando", "activo" in c.get(U).json())

print("\nFALLAS:", FALLOS or "ninguna")
sys.exit(1 if FALLOS else 0)
