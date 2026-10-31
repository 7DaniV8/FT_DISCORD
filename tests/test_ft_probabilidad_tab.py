"""
Pestaña 🎲 FT PROBABILIDAD (30/10/2026): solo lectura, solo admin de fábrica, con tiempos cortos y sin filtrar el secreto.
Sin red: httpx.get se reemplaza por una función falsa.
"""
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp()
os.environ.setdefault("ADMIN_TOKEN", "test_token_admin_123456")
os.environ.setdefault("FTR_INTERNAL_SECRET", "secreto_interno_de_prueba_123")
os.environ.setdefault("RESEARCH_TOKEN", "research_token_de_prueba_1234")
os.environ["FTR_DB_PATH"] = os.path.join(_TMP, "prueba.db")
os.environ.pop("FT_PROBABILIDAD_URL", None)
RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from production.api import permisos  # noqa: E402
from production.api.app import app  # noqa: E402
from research.api import routes_ft_probabilidad as rfp  # noqa: E402

FALLOS = []


def check(nombre, cond):
    print(("  OK    " if cond else "  FALLA ") + nombre)
    if not cond:
        FALLOS.append(nombre)


class Resp:
    def __init__(self, code=200, data=None, text=""):
        self.status_code, self._d, self.text = code, data, text

    def json(self):
        if self._d is None:
            raise ValueError("no json")
        return self._d


LLAMADAS = []
SIGUIENTE = {"r": None, "exc": None}


def falso_get(url, params=None, headers=None, timeout=None, follow_redirects=None, trust_env=None):
    LLAMADAS.append({"url": url, "params": params, "headers": headers, "timeout": timeout, "redir": follow_redirects})
    if SIGUIENTE["exc"]:
        raise SIGUIENTE["exc"]
    return SIGUIENTE["r"]


rfp.httpx.get = falso_get
c0 = TestClient(app)   # sin ninguna credencial
c = TestClient(app, headers={"X-FTR-Internal-Secret": os.environ["FTR_INTERNAL_SECRET"]})  # pasa el portón de la app
T = {"token": os.environ["RESEARCH_TOKEN"]}
R = "/research/api/ft-probabilidad/resumen"

print("\nA. Permisos de fábrica")
check("admin la ve de fábrica", "ver_tab_ftprobabilidad" in permisos._TABS_TARDIAS_POR_ROL.get("admin", set()))
check("lectura NO la ve de fábrica", "ver_tab_ftprobabilidad" not in permisos._TABS_TARDIAS_POR_ROL.get("lectura", set()))
check("sin grupo: PERMISOS_TAB propio", permisos.PERMISOS_TAB.get("ftprobabilidad") == "ver_tab_ftprobabilidad")
check("la ruta GET pide ver_tab_ftprobabilidad", permisos.permiso_requerido("GET", R) == "ver_tab_ftprobabilidad")
check("tiene_permiso: admin sí, lectura no", permisos.tiene_permiso("admin", "ver_tab_ftprobabilidad") and not permisos.tiene_permiso("lectura", "ver_tab_ftprobabilidad"))
check("el reporte GET también", permisos.permiso_requerido("GET", "/research/api/ft-probabilidad/reporte/FT_abc") == "ver_tab_ftprobabilidad")

print("\nB. Sin sesión ni token")
r = c0.get(R)
check("rechazada (no 200)", r.status_code in (401, 403))
check("no llamó al servicio", len(LLAMADAS) == 0)

print("\nC. Sin FT_PROBABILIDAD_URL")
r = c.get(R, params=T)
check("200 con ok=false y motivo sin_configurar", r.status_code == 200 and r.json().get("motivo") == "sin_configurar")
check("no hizo ninguna petición de red", len(LLAMADAS) == 0)

print("\nD. URL inválidas se ignoran")
for malo in ("ftp://x:1", "http://u:p@host:8080", "http://host:8080/ruta", "http://host:8080?x=1", "javascript:alert(1)", "http://host ;x"):
    os.environ["FT_PROBABILIDAD_URL"] = malo
    check("rechaza " + malo, rfp._origen() is None)
os.environ["FT_PROBABILIDAD_URL"] = "http://ft-probabilidad.railway.internal:8080/"
check("acepta la URL privada de Railway", rfp._origen() == "http://ft-probabilidad.railway.internal:8080")

print("\nE. Respuesta buena, cabeceras y caché")
SIGUIENTE["r"] = Resp(200, {"servicio": {"habilitado_admin": True, "consultas_tdi_hoy": 24, "limite_diario": 1000},
                            "pesos": {}, "partidos": [{"fixture_id": "FT_abc", "jugadores": ["A", "B"]}]})
r = c.get(R, params=T)
d = r.json()
check("ok=true y trae los partidos", r.status_code == 200 and d.get("ok") is True and len(d["partidos"]) == 1)
check("llamó una vez a /resumen del servicio", len(LLAMADAS) == 1 and LLAMADAS[0]["url"].endswith("/resumen"))
check("manda el secreto solo en la cabecera", LLAMADAS[0]["headers"] == {"X-FT-Status-Secret": os.environ["FTR_INTERNAL_SECRET"]})
check("tiempos cortos", LLAMADAS[0]["timeout"].connect <= 2.0 and LLAMADAS[0]["timeout"].read <= 4.0)
check("no sigue redirecciones", LLAMADAS[0]["redir"] is False)
check("el secreto NO aparece en lo que recibe el navegador", os.environ["FTR_INTERNAL_SECRET"] not in r.text)
c.get(R, params=T)
check("segunda petición dentro de 10 s sale de la copia", len(LLAMADAS) == 1)

print("\nF. Fallos del servicio (caché vaciada antes de cada uno)")
def limpio():
    rfp._cache["datos"], rfp._cache["t"] = None, 0.0
limpio(); SIGUIENTE["exc"] = httpx.ConnectError("boom")
r = c.get(R, params=T)
check("sin conexión -> ok=false, 200", r.status_code == 200 and r.json().get("motivo") == "sin_conexion")
limpio(); SIGUIENTE["exc"] = httpx.ReadTimeout("lento")
check("timeout -> ok=false", c.get(R, params=T).json().get("motivo") == "sin_conexion")
SIGUIENTE["exc"] = None
limpio(); SIGUIENTE["r"] = Resp(401)
check("401 del servicio -> no_autorizado", c.get(R, params=T).json().get("motivo") == "no_autorizado")
limpio(); SIGUIENTE["r"] = Resp(500)
check("500 del servicio -> http_500", c.get(R, params=T).json().get("motivo") == "http_500")
limpio(); SIGUIENTE["r"] = Resp(200, None)
check("no-JSON -> respuesta_invalida", c.get(R, params=T).json().get("motivo") == "respuesta_invalida")
limpio(); SIGUIENTE["r"] = Resp(200, {"partidos": "x"})
check("formato raro -> respuesta_invalida", c.get(R, params=T).json().get("motivo") == "respuesta_invalida")
check("los fallos no se guardan en la copia", rfp._cache["datos"] is None)

print("\nG. Reporte")
P = "/research/api/ft-probabilidad/reporte/"
n = len(LLAMADAS)
check("id con caracteres raros -> 400 sin llamar", c.get(P + "a.b", params=T).status_code == 400 and len(LLAMADAS) == n)
SIGUIENTE["r"] = Resp(200, None, "<html>panel</html>")
r = c.get(P + "FT_abc123", params=T)
check("200 con el HTML", r.status_code == 200 and "panel" in r.text)
check("CSP sin scripts y sin marcos", "default-src 'none'" in r.headers.get("content-security-policy", "")
      and "script" not in r.headers.get("content-security-policy", "") and r.headers.get("x-frame-options") == "DENY")
SIGUIENTE["r"] = Resp(404)
check("404 del servicio -> 404", c.get(P + "FT_nada", params=T).status_code == 404)

print("\nH. Detalle del partido (modal)")
D = "/research/api/ft-probabilidad/partido/"
n = len(LLAMADAS)
check("id raro -> 400 sin llamar", c.get(D + "a.b", params=T).status_code == 400 and len(LLAMADAS) == n)
check("sin credenciales -> no pasa", c0.get(D + "FT_abc123").status_code in (401, 403))
SIGUIENTE["exc"] = None
SIGUIENTE["r"] = Resp(200, {"fixture_id": "FT_abc123", "jugadores": ["A", "B"], "cuotas": {"A": {"cuota": 1.8}}})
r = c.get(D + "FT_abc123", params=T)
check("200 con ok y datos", r.status_code == 200 and r.json()["ok"] and r.json()["cuotas"]["A"]["cuota"] == 1.8)
check("llama a /partido con el secreto", LLAMADAS[-1]["url"].endswith("/partido/FT_abc123")
      and LLAMADAS[-1]["headers"]["X-FT-Status-Secret"] == os.environ["FTR_INTERNAL_SECRET"])
SIGUIENTE["r"] = Resp(404)
j = c.get(D + "FT_nada", params=T).json()
check("404 del servicio -> mensaje suave", j["ok"] is False and j["motivo"] == "no_encontrado")
SIGUIENTE["r"] = Resp(200, None, "no json")
check("no JSON -> respuesta_invalida", c.get(D + "FT_x", params=T).json()["motivo"] == "respuesta_invalida")
SIGUIENTE["r"] = None
SIGUIENTE["exc"] = httpx.ConnectError("x")
check("sin conexión -> sin_conexion", c.get(D + "FT_x", params=T).json()["motivo"] == "sin_conexion")
SIGUIENTE["exc"] = None
html = open(os.path.join(RAIZ, "research/web/templates/tabs/ft_probabilidad.html"), encoding="utf-8").read()
check("la pestaña abre modal, no enlace de reporte directo en la tabla", "data-ftp-id" in html and "ftp-modal" in html)

print("\n==========================================================")
print("FT PROBABILIDAD (pestaña): todo consistente." if not FALLOS else f"FALLAN {len(FALLOS)}: {FALLOS}")
sys.exit(1 if FALLOS else 0)
