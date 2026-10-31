"""
production/api/app.py — App factory del servicio de producción.

La web (rankings.html, admin.html) son archivos de verdad en web/ —
NO strings de Python embebidos. Esto es lo que cambió respecto a la
versión anterior del proyecto (un solo api/main.py de ~3000 líneas con
HTML/CSS/JS mezclado adentro).
"""
from __future__ import annotations
import asyncio
import json
import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.requests import Request
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi import _rate_limit_exceeded_handler

# FIX LOGS-INVISIBLES (14/07/2026, encontrado a raíz de Maria preguntando
# "no sé si Movimiento Rentable está sirviendo" -- los logs de diagnóstico
# que se agregaron ese mismo día estaban ahí en el código, pero nunca
# llegaban a ningún lado visible): sin ESTA configuración, Python no
# emite nada por debajo de WARNING, y ni siquiera eso va a un lugar
# fijo -- cada módulo usa `logging.getLogger(__name__)` (correcto, es
# la práctica estándar), pero si nadie configura el logger RAÍZ con un
# handler + nivel, esos logs se pierden en el aire. Esto tiene que
# correr ANTES de importar cualquier otro módulo del proyecto, para
# que sus loggers ya queden enganchados desde el arranque.
#
# FTR_LOG_LEVEL (default INFO) -- se puede subir a DEBUG en Railway
# (variable de entorno) para ver también los logs más detallados
# (ej. por qué Movimiento Rentable descartó una evaluación puntual)
# sin tener que volver a desplegar código, y bajarlo de nuevo a INFO
# cuando no se necesite ese nivel de detalle -- DEBUG es bastante más
# verboso (imprime hasta chequeos rutinarios).
logging.basicConfig(
    level=os.getenv("FTR_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

# v44c (12/10/2026): restauración de un respaldo pedida con la variable
# FTR_RESTAURAR_RESPALDO. Tiene que correr ANTES de que cualquier módulo abra la
# base. Sin la variable no hace nada. Ver production/engines/respaldo.py.
try:
    from production.engines.respaldo import restaurar_si_pedido as _restaurar_si_pedido
    _restaurar_si_pedido()
except Exception as _e_rest:  # noqa: BLE001
    logging.getLogger("ftr.respaldo").error(f"[Respaldo] la restauración falló, se arranca con la base actual: {_e_rest}",
                                             exc_info=True)

from production.api.deps import ACTIVE_DB
from production.api.routes_public import router as public_router
from production.api.routes_admin import router as admin_router
from production.api.ws_manager import manager
from shared.rate_limit import limiter

_WEB_DIR = Path(__file__).resolve().parent.parent / "web"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # FIX PERFORMANCE (28/07/2026, a pedido de Maria: "qué pasa si entran
    # 500 personas a la vez"). Este servicio corre un solo proceso
    # Uvicorn (sin --workers -- ver nota en routes_research.py sobre por
    # qué: PROD_DB_WRITE_LOCK es un threading.Lock() de Python, que solo
    # sirve DENTRO de un mismo proceso -- correr varios workers
    # rompería esa protección y volvería a las "database is locked" que
    # se acaban de arreglar, porque cada proceso tendría su propio lock
    # sin coordinación con los demás).
    #
    # Con un solo proceso, TODOS los endpoints calientes -- tanto los
    # que sirven la página pública (rankings, historiales) como los que
    # recibe el bot (price-drop, fixture/reportar, live-signal) -- son
    # `def` sincrónicos que corren en el mismo thread pool compartido de
    # AnyIO/Starlette, que por defecto tiene un tope de 40 threads
    # concurrentes. Con 500 visitantes a la vez (cada uno dispara ~15-20
    # requests entre JS/CSS/datos al cargar la página) más el tráfico
    # normal del bot, ese tope de 40 se queda corto -- no rompe nada
    # (no hay riesgo de "database is locked" de nuevo, eso ya está
    # resuelto), pero todo hace fila y la carga se siente lenta otra
    # vez, aunque cada consulta individual sea rápida.
    #
    # Se subió a 200 con este razonamiento: "el trabajo de estos
    # endpoints es mayormente espera de I/O (disco, red a Telegram/IA)
    # -- durante esa espera Python libera el GIL, así que más threads SÍ
    # ayudan a la concurrencia real, no compiten por CPU".
    #
    # CORRECCIÓN (25/08/2026, incidente 19:19-19:21 UTC): esa premisa no
    # se cumplía. El completado perezoso de FTR/Elo metió trabajo de CPU
    # PURA adentro de los GET del panel (dos búsquedas en el padrón de
    # Elo por fila, hasta 20 filas por request, recorriendo ~13.600
    # jugadores en Python). Para trabajo CPU-bound sobre 0.5-1.0 vCPU,
    # 200 threads no es más concurrencia: es la misma CPU repartida en
    # 200 pedazos, con el GIL rotando entre todos y el costo de cambio
    # de contexto encima. Evidencia: un GET a /static/img/icon-192.png
    # -- ruta exenta del portón, servida desde disco, sin una sola
    # consulta -- tardó 21.332 ms. Con 200 tokens libres, ese request
    # tenía thread disponible al instante; lo que no tenía era CPU.
    #
    # 60 es el número nuevo: sigue siendo bastante más que los 40 de
    # fábrica (que era la queja original de Maria, con 500 visitantes
    # simultáneos), pero acota cuánta CPU puede estar peleándose a la
    # vez. La causa de CPU ya se movió a una tarea de fondo (ver
    # ftr_elo_backfill_task.py más abajo), así que este número ahora
    # gobierna I/O de verdad.
    #
    # Configurable a propósito: si vuelve a hacer falta moverlo, se
    # cambia la variable en Railway y se reinicia, sin tocar código ni
    # esperar un deploy.
    import anyio
    _tokens = int(os.getenv("FTR_THREADPOOL_TOKENS", "60"))
    anyio.to_thread.current_default_thread_limiter().total_tokens = _tokens
    print(f"[Threadpool] límite de threads concurrentes: {_tokens}")

    # El cartel decía "motor oficial: UTR v1" (11/08/2026). Era cierto
    # cuando v1 calculaba el ranking, y dejó de serlo al entrar
    # utr_v2_ajuste -- pero el texto quedó, y sostenía la idea de que v1
    # seguía siendo el motor de rating. No lo es: hoy v1 es el INGESTOR
    # (crea jugadores, parsea scores, llena match_log y mantiene los
    # contadores) y v2 es el que calcula y escribe ratings.rating.
    # Confundir esos dos roles fue parte de por qué el conflicto de
    # escritura tardó diez días en verse.
    from shared.version import ETIQUETA as _VERSION_ETIQUETA
    print(f"FullTenis production — versión {_VERSION_ETIQUETA}")
    print(f"FullTenis production — ingesta: UTR v1 · rating: FTR-Ajuste (utr_v2) "
          f"— DB: {ACTIVE_DB}")

    # FullTenis Research — sincronización en vivo del motor Elo experimental.
    # Corre enteramente adentro de research/ (research/lab/live_sync.py) —
    # esto solo arranca la tarea, no le pasa ningún dato ni la llama desde
    # el manejo de /match-finished. Ver la nota completa en ese archivo.
    from research.lab.live_sync import start_live_sync_task
    task = start_live_sync_task()

    # Backfill incremental — mismo patrón que LiveSync arriba: hilo de
    # fondo que corre adentro de este mismo proceso, cada
    # FTR_INCREMENTAL_BACKFILL_SECONDS (default 600s = 10 min). Ver la
    # nota completa en production/tasks/backfill_task.py. Requiere
    # RAPIDAPI_KEY seteada en este servicio (RankingFTR) además de en
    # el del bot -- si falta, el loop avisa por log en vez de crashear.
    from production.tasks.backfill_task import start_backfill_task
    backfill_tarea_reciente, backfill_tarea_completo, backfill_tarea_intermedio, backfill_tarea_total = start_backfill_task()

    # Diccionario ΔFTR (bins v1) + curva isotónica (v2) — se regeneran
    # solos cada FTR_DICT_REBUILD_SECONDS (default 24h) en vez de
    # depender de que alguien corra los scripts a mano. Ver la nota
    # completa en production/tasks/ftr_dictionary_task.py.
    from production.tasks.ftr_dictionary_task import start_ftr_dictionary_task
    ftr_dictionary_tarea = start_ftr_dictionary_task()

    # Limpieza periódica de alertas viejas de Valor en Vivo / Valor
    # Pre-Partido -- ver la nota completa en production/tasks/valor_cleanup_task.py.
    from production.tasks.valor_cleanup_task import start_valor_cleanup_task
    valor_cleanup_tarea = start_valor_cleanup_task()

    # Volcado en lote de pinnodds_price_history (25/08/2026) -- ver la
    # nota completa en production/api/pinnodds_buffer.py. Evita que una
    # ráfaga de price-drops (típico tras un reinicio: el bot descarga
    # de una todo lo pendiente) haga pelear el candado de escritura una
    # vez por fila -- ahora pelean una vez por lote.
    from production.tasks.pinnodds_flush_task import start_pinnodds_flush_task
    pinnodds_flush_tarea = start_pinnodds_flush_task()

    # Completado de FTR/Elo/Matchup por jugador en Historial y Descartes
    # (25/08/2026). ANTES esto pasaba adentro de los GET del panel y era
    # la causa del incidente de esa noche -- ver la nota completa en
    # production/tasks/ftr_elo_backfill_task.py. Arranca con gracia
    # (FTR_ELO_BACKFILL_GRACIA_S) para no meterse en la ráfaga de
    # reinicio del bot.
    # Red de contención de descartes_ia (07/09/2026): reintenta las filas
    # de Sugerencias/Descartes que la base rechazó por lock. En un día
    # normal no hace nada -- el archivo está vacío. Ver
    # production/api/descartes_buffer.py.
    from production.tasks.descartes_flush_task import start_descartes_flush_task
    descartes_flush_tarea = start_descartes_flush_task()

    # ANALISIS PRE PARTIDO (10/09/2026): vuelca el buffer del registro
    # permanente de pre-partidos. Ver
    # production/api/analisis_pre_partido_buffer.py.
    from production.tasks.analisis_pre_partido_flush_task import (
        start_analisis_pre_partido_flush_task,
    )
    analisis_pp_flush_tarea = start_analisis_pre_partido_flush_task()

    from production.tasks.ftr_elo_backfill_task import start_ftr_elo_backfill_task
    ftr_elo_backfill_tarea = start_ftr_elo_backfill_task()

    # Completado de bio (edad/altura/mano dominante) -- 14/07/2026, a
    # pedido de Maria: "que sea automático, una vez por semana". Antes
    # esto era 100% manual (scripts/backfill_player_bio.py corrido a
    # mano). Ver la nota completa en production/tasks/bio_task.py.
    from production.tasks.bio_task import start_bio_task
    bio_tarea = start_bio_task()

    # Cierre automático de alertas pendientes (Pre-Partido, Vivo,
    # Movimiento Rentable + Elo) -- 15/07/2026, a pedido de Maria: "que
    # cierre todas las alertas, de todas las estrategias, cada 12
    # horas". Ver la nota completa en
    # production/tasks/cierre_alertas_task.py.
    from production.tasks.cierre_alertas_task import start_cierre_alertas_task
    cierre_alertas_tarea = start_cierre_alertas_task()

    # UTR VALUE -- fuente OddsPapi (07/10/2026): partidos del circuito UTR
    # (SportsAPI365/PinnOdds no los traen) con la cuota de la casa
    # contratada, inicio de las señales aprobadas y resultados. Ver
    # production/tasks/oddspapi_utr_task.py.
    from production.tasks.oddspapi_utr_task import start_oddspapi_utr_task
    oddspapi_utr_tarea = start_oddspapi_utr_task()
    # UTR MARKET ANOMALY -- cuotas EN VIVO del circuito UTR por el WebSocket
    # de OddsPapi (07/10/2026). No conecta mientras UTRA_ODDSPAPI_VIVO no sea
    # 'ws' (Admin). Ver production/tasks/oddspapi_vivo_task.py.
    from production.tasks.oddspapi_vivo_task import start_oddspapi_vivo_task
    oddspapi_vivo_tarea = start_oddspapi_vivo_task()

    # ⚠️ UTR · REVISIÓN MANUAL (07/10/2026 noche): botones de Telegram
    # (✅ SÍ / ❌ NO / ⏳ DESPUÉS) y vencimiento de las revisiones cuyo
    # partido empezó. Ver production/tasks/utr_revision_task.py.
    from production.tasks.utr_revision_task import start_utr_revision_task
    utr_revision_tarea = start_utr_revision_task()

    # UTR REFRESH MANAGER -- UTR Power (08/10/2026): UTR exacto con la cuenta
    # Power del proyecto. Apagado hasta
    # UTRP_ACTIVO=1 y con UTR_EMAIL / UTR_PASSWORD en Railway. Ver
    # production/tasks/utr_power_task.py.
    from production.tasks.utr_power_task import start_utr_power_task
    utr_power_tarea = start_utr_power_task()

    # ⏰ RECORDADOR (08/10/2026, Rubén): avisa el próximo partido de los
    # jugadores que pidió cualquier usuario. Solo lee partidos ya conocidos.
    # Ver production/tasks/recordador_task.py. Kill switch RECORDADOR_ENABLED=0.
    from production.tasks.recordador_task import start_recordador_task
    recordador_tarea = start_recordador_task()

    # Retiros (07/08/2026, a pedido de Rubén: avisar cuando un jugador
    # que abandonó vuelve a jugar). Escanea match_log buscando partidos
    # cortados y anota quién abandonó -- SOLO LECTURA, no toca ratings
    # ni excluye nada. El aviso sale después, en /fixture/reportar,
    # cuando ese jugador aparece en un fixture. Ver
    # production/tasks/retiros_task.py.
    from production.tasks.retiros_task import start_retiros_task
    retiros_tarea = start_retiros_task()

    # Recalculo nocturno del ranking FTR con el motor de ajuste
    # (utr_v2_ajuste). Corre una vez al dia (04:00 por defecto), toma los
    # partidos nuevos y reescribe ratings.rating con la salvaguarda de
    # sanidad. Las promesas nuevas entran solas. Ver la nota completa en
    # production/tasks/ftr_ajuste_task_loop.py.
    #   - Se puede desactivar con la env FTR_AJUSTE_ENABLED=0 sin tocar codigo.
    #   - Devuelve None si esta desactivada (por eso el guard en el cancel).
    from production.tasks.ftr_ajuste_task_loop import start_ftr_ajuste_task
    ftr_ajuste_tarea = start_ftr_ajuste_task()

    # Reconstrucción semanal de los perfiles de estilo desde el Match
    # Charting Project (27/07/2026) -- ver production/tasks/estilo_task.py.
    #   - Se desactiva con FTR_ESTILO_ENABLED=0 sin tocar código.
    #   - Devuelve None si está desactivada (por eso el guard en el cancel).
    from production.tasks.estilo_task import start_estilo_task
    estilo_tarea = start_estilo_task()

    # Worker de snapshots/comparaciones de estilo (29/07/2026) -- hilo
    # daemon con cola y dedupe; procesa lo que /fixture/reportar y
    # /estilo/comparar encolan en vez de calcular dentro del request.
    from production.tasks.estilo_worker import start_estilo_worker
    start_estilo_worker()

    # Historial FT de LIVE · MatchUp y LIVE · Tercer Set (29/08/2026, a
    # pedido de Maria: "en el historial FT no se están guardando los
    # partidos de MUP, ni Tercer Set"). Ese guardado vivía DENTRO del
    # cálculo de /api/envivo-diagnostico y solo corría si alguien tenía
    # la pestaña Live abierta. Ver production/tasks/live_historial_task.py.
    #   - Se desactiva con FTR_LIVE_HISTORIAL_ENABLED=0 sin tocar código.
    #   - Devuelve None si está desactivada (por eso el guard en el cancel).
    from production.tasks.live_historial_task import start_live_historial_task
    live_historial_tarea = start_live_historial_task()

    # 🏆 IMPLICACIÓN DE TORNEO, Fase 0 (12/10/2026, Rubén): registro de TODOS
    # los partidos ATP/WTA/Challenger, cuota al perder el set 1, ranking
    # oficial semanal y reglas de puntos verificadas. Módulo independiente:
    # solo lee de los motores existentes. Ver production/tasks/implicacion_task.py.
    #   - Kill switch IMPLICACION_ENABLED=0 (devuelve None).
    from production.tasks.implicacion_task import start_implicacion_task
    implicacion_tarea = start_implicacion_task()

    # 🧠 INVESTIGACIÓN de Partidos Maestros, Cuota Mal Puesta, UTR VALUE y UTR Market
    # Anomaly (v54, 13/10/2026, Rubén): la misma investigación del Recordador para la
    # selección de cada estrategia, guardada antes del partido. Solo lee de los motores.
    # Ver production/tasks/estrategia_investigacion_task.py. Kill switch
    # ESTRATEGIA_INVESTIGACION_ENABLED=0 (devuelve None).
    from production.tasks.estrategia_investigacion_task import start_estrategia_investigacion_task
    estrategia_investigacion_tarea = start_estrategia_investigacion_task()

    # 🔎 ¿POR QUÉ LA CUOTA? (v54c, 14/10/2026, Rubén): estudio de contexto de cada UTR Market
    # Anomaly, APARTE de la detección (nunca la bloquea, modifica ni retrasa). Solo referencia.
    # Ver production/tasks/pqc_task.py. Kill switch PQC_ENABLED=0.
    from production.tasks.pqc_task import start_pqc_task
    pqc_tarea = start_pqc_task()

    # 🛡 Snapshot semanal de Defensa de Puntos (existía desde el 31/07/2026
    # pero NUNCA se conectó aquí; Rubén autorizó integrarlo el 12/10/2026).
    # Lee player_ranking_results y el ranking oficial semanal; escribe solo
    # ranking_defense_snapshot. Kill switch FTR_DEFENSA_ENABLED=0.
    from production.tasks.defensa_snapshot_task import start_defensa_snapshot_task
    defensa_snapshot_tarea = start_defensa_snapshot_task()

    # Migraciones de columnas nuevas, al arrancar (04/08/2026). La de
    # match_log.torneo vivía solo dentro de procesar_partido(), o sea que
    # la columna no existía hasta que se procesara un partido -- y
    # live_sync, que arranca antes y corre cada 5s, la pedía y se caía
    # entera con "no such column: m.torneo". Acá se hace una sola vez,
    # antes de que arranque nada más.
    try:
        from production.api.deps import get_conn
        _c = get_conn()
        try:
            _c.execute("ALTER TABLE match_log ADD COLUMN torneo TEXT")
            _c.commit()
            print("FullTenis production — columna match_log.torneo agregada")
        except Exception:
            pass  # ya existe
        finally:
            _c.close()
    except Exception as _e:
        print(f"FullTenis production — aviso: no se pudo migrar match_log.torneo ({_e})")

    # Auth (29/07/2026): crea el primer administrador si no hay ninguno,
    # a partir de FTR_ADMIN_USUARIO / FTR_ADMIN_PASSWORD. Si esas
    # variables no están, solo avisa por log -- nunca inventa una
    # contraseña por default.
    try:
        from production.api.auth import bootstrap_admin
        from production.api.deps import get_conn as _gc
        _c = _gc()
        try:
            bootstrap_admin(_c)
        finally:
            _c.close()
    except Exception as e:
        logging.getLogger(__name__).error(f"[Auth] bootstrap falló: {e}")

    # Defaults de estrategias movidos desde el código (17/08/2026). Ver
    # MIGRACIONES_DEFAULT en config_estrategias.py: mueve una clave
    # guardada SOLO si todavía tiene el default anterior, para que
    # cambiar un umbral en el código no dependa de que alguien se
    # acuerde de tocarlo también en Admin. Una clave calibrada a mano no
    # se toca. No puede tumbar el arranque -- tiene su propio try.
    try:
        from production.api.config_estrategias import (aplicar_migraciones_default,
                                                       aplicar_migraciones_copia)
        _movidas = aplicar_migraciones_default()
        # 20/08/2026: DESPUÉS de las de default, no antes. Primero se
        # corrige un default fosilizado en la clave vieja, después se
        # copia el valor bueno a la clave nueva de cada estrategia. Al
        # revés se copiaría el valor viejo y el comportamiento cambiaría
        # justo en el deploy que promete no cambiarlo.
        _copiadas = aplicar_migraciones_copia()
        if _copiadas:
            logging.getLogger(__name__).info(
                     f"[Arranque] separación de claves por estrategia: se rellenaron "
                     f"{len(_copiadas)} claves nuevas con el valor que tenía la compartida "
                     f"({', '.join(_copiadas[:6])}{'...' if len(_copiadas) > 6 else ''})")
        if _movidas:
            logging.getLogger(__name__).info(
                f"[ConfigEstrategias] {len(_movidas)} default(s) movido(s): {', '.join(_movidas)}")
    except Exception as e:
        logging.getLogger(__name__).warning(f"[ConfigEstrategias] migraciones de default: {e}")

    # La limpieza de arranque de Valor FTR se dio de baja el 29/07/2026.
    # Borraba toda fila con tipo='valor_ftr' de valor_historial en CADA
    # arranque; ahora Valor FTR vuelve a registrarse ahí, así que seguir
    # llamándola significaría que cada redeploy borra en silencio los
    # picks de Valor FTR enviados desde el anterior. La función sigue
    # existiendo en routes_public.py, devolviendo 0.

    _tareas = {
        "live_sync": task,
        "backfill_reciente": backfill_tarea_reciente,
        "backfill_completo": backfill_tarea_completo,
        "backfill_intermedio": backfill_tarea_intermedio,
        "backfill_total": backfill_tarea_total,
        "ftr_dictionary": ftr_dictionary_tarea,
        "valor_cleanup": valor_cleanup_tarea,
        "pinnodds_flush": pinnodds_flush_tarea,
        "descartes_flush": descartes_flush_tarea,
        # va SÍ o SÍ en esta lista -- ver la nota de arriba sobre
        # pinnodds_flush, que quedó sin supervisar por no estar acá:
        # si la tarea muere, el buffer se llena y nadie se entera.
        "analisis_pp_flush": analisis_pp_flush_tarea,
        "ftr_elo_backfill": ftr_elo_backfill_tarea,
        "bio": bio_tarea,
        "cierre_alertas": cierre_alertas_tarea,
        "oddspapi_utr": oddspapi_utr_tarea,
        "oddspapi_vivo": oddspapi_vivo_tarea,
        "utr_revision": utr_revision_tarea,
        "utr_power": utr_power_tarea,
        "recordador": recordador_tarea,      # None si RECORDADOR_ENABLED=0
        "retiros": retiros_tarea,
        "ftr_ajuste": ftr_ajuste_tarea,      # None si FTR_AJUSTE_ENABLED=0
        "estilo": estilo_tarea,              # None si FTR_ESTILO_ENABLED=0
        "live_historial": live_historial_tarea,  # None si FTR_LIVE_HISTORIAL_ENABLED=0
        "implicacion": implicacion_tarea,          # None si IMPLICACION_ENABLED=0
        "estrategia_investigacion": estrategia_investigacion_tarea,  # None si ESTRATEGIA_INVESTIGACION_ENABLED=0
        "pqc": pqc_tarea,                    # None si PQC_ENABLED=0
        "defensa_snapshot": defensa_snapshot_tarea,  # None si FTR_DEFENSA_ENABLED=0
    }
    # 🩺 Admin → Salud (08/10/2026): el mismo diccionario, registrado ANTES
    # del yield para poder preguntar en vivo si cada tarea sigue corriendo.
    from production.engines import salud as _salud
    _salud.registrar_tareas(_tareas)

    yield

    # FIX APAGADO-ROTO (25/08/2026): antes esto era una lista de
    # `x.cancel()` sueltos, y los cuatro backfill son None cuando
    # FTR_BACKFILL_ENABLED=0 -- o sea que prender el kill switch (el que
    # se agregó justamente después del incidente de sobreconsumo de
    # julio) hacía que el apagado reventara con AttributeError:
    # 'NoneType' object has no attribute 'cancel', y las tareas
    # posteriores de la lista no se cancelaban nunca. Dos de ellas ya
    # tenían su `if ... is not None` a mano; el patrón no escala, se
    # olvida uno y se rompe recién el día que alguien usa el switch.
    #
    # De paso: pinnodds_flush_tarea no estaba en la lista -- quedaba
    # viva en el apagado.
    for _nombre, _t in _tareas.items():
        if _t is None:
            continue  # esa tarea nunca se lanzó (kill switch apagado)
        try:
            _t.cancel()
        except Exception as e:
            logging.getLogger(__name__).warning(
                f"[Apagado] no se pudo cancelar {_nombre}: {e}")
    print("FullTenis production — apagado")


app = FastAPI(title="FullTenis — Motor oficial (UTR v1)", version="5.0", lifespan=lifespan)

# 24/09/2026: httpx loguea en INFO la URL completa de cada request, y la
# de Telegram lleva el TOKEN DEL BOT adentro (api.telegram.org/bot<TOKEN>/
# sendPhoto) -- quedaba impreso en los logs de Railway. En WARNING solo
# se ven los errores, sin la línea con la URL de cada envío exitoso.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# Rate limiting por IP (defensa en profundidad -- ver shared/rate_limit.py).
# Límite global aplicado a TODAS las rutas, incluidas las que se agregan
# más abajo con app.include_router(). Endpoints puntuales que necesiten
# un límite distinto (más estricto en /admin y /research, ver esos
# routers) se sobreescriben con @limiter.limit(...) ahí mismo.
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)


# 🩺 Admin → Salud (08/10/2026): anota quién le habla a RankingFTR (bot de
# partidos, PinnOdds, FT Intelligence, FT Discord) y hace cuánto. Solo mira
# el prefijo de la ruta; un dict en memoria, sin I/O.
@app.middleware("http")
async def _salud_entradas(request, call_next):
    try:
        from production.engines import salud as _salud
        _salud.visto(request.url.path)
    except Exception:                                    # nunca frena un request
        pass
    return await call_next(request)

# ============================================================================
# CABECERAS DE SEGURIDAD (18/08/2026, a pedido de Maria: "quiero agregarle
# seguridad a la página").
#
# El proyecto no mandaba NINGUNA. Auditado: ni CSP, ni X-Frame-Options, ni
# HSTS, ni X-Content-Type-Options, ni Referrer-Policy. La autenticación
# está bien resuelta (PBKDF2 240k, cookies HttpOnly+Secure+SameSite,
# permisos que niegan por defecto, rate limit por IP) -- lo que faltaba es
# la capa que protege al NAVEGADOR del usuario, no al servidor.
#
# Qué previene cada una:
#
#   X-Frame-Options / frame-ancestors -- clickjacking: que alguien cargue
#     el panel en un <iframe> invisible sobre su propia página y le robe
#     los clics a un admin logueado. Sin esto, "Enviar a Picks" se puede
#     disparar desde un sitio de terceros.
#   X-Content-Type-Options: nosniff -- que el navegador adivine el tipo de
#     un archivo subido y lo ejecute como HTML/JS.
#   Referrer-Policy -- que las URLs internas (con ids de alertas, nombres
#     de jugadores) se filtren en el header Referer al hacer clic a un
#     sitio externo.
#   HSTS -- degradación a HTTP. Solo se manda si la request llegó por
#     HTTPS: mandarlo en local (http://localhost) le clava al navegador
#     una regla de "siempre HTTPS" para ese host que después es molesta
#     de sacar.
#   CSP -- la más importante y la más delicada, ver abajo.
#
# SOBRE LA CSP Y `unsafe-inline`
# ──────────────────────────────
# Lo honesto: esta CSP NO bloquea XSS todavía. El panel tiene 172
# handlers `onclick="..."` inline en los templates y varios bloques
# <script> embebidos, así que una CSP estricta rompe la aplicación
# entera. Poner 'unsafe-inline' es lo que la deja funcionando.
#
# Entonces, ¿para qué sirve hoy? Para acotar el DESTINO: `connect-src` y
# `form-action` en 'self' significan que aunque alguien logre inyectar
# script, no puede mandar los datos a un servidor propio. Y `img-src`/
# `style-src` limitan de dónde se carga contenido.
#
# El camino para endurecerla de verdad es sacar los onclick inline y
# reemplazarlos por addEventListener -- trabajo mecánico pero de 172
# lugares, así que se hace aparte. Cuando eso esté, se saca
# 'unsafe-inline' de script-src y ahí sí la CSP para XSS.
#
# CDNs: cdnjs, fonts.googleapis y fonts.gstatic están permitidos porque
# los templates los usan. Si algún día se sirven locales, se sacan de acá
# y la política queda más cerrada.
from production.api.routes_buscador import origen_buscador as _origen_buscador  # noqa: E402
_ORIGEN_BUSCADOR = _origen_buscador()
_CSP = "; ".join([
    "default-src 'self'",
    # 'unsafe-inline' es deuda técnica consciente -- ver el comentario.
    "script-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com",
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdnjs.cloudflare.com",
    "font-src 'self' https://fonts.gstatic.com https://cdnjs.cloudflare.com data:",
    # v53 (13/10/2026): fotos con licencia libre de Wikimedia Commons en la portada de FT NEWS (con autor y licencia).
    "img-src 'self' data: blob: https://upload.wikimedia.org",
    # ws/wss: el panel usa /ws para el stream de rankings.
    # BOT BUSCADOR (28/09/2026): su origen se añade SOLO si la URL pública
    # del buscador es un origen https válido (routes_buscador.
    # validar_origen). Sin ella, la cabecera queda idéntica a la de antes.
    "connect-src 'self' ws: wss:" + (f" {_ORIGEN_BUSCADOR}" if _ORIGEN_BUSCADOR else ""),
    "frame-ancestors 'none'",   # nadie puede embeber el panel
    "form-action 'self'",       # ningún formulario puede postear afuera
    "base-uri 'self'",          # no se puede reescribir la base de las URLs relativas
    "object-src 'none'",        # sin Flash/applets/embeds
])


@app.middleware("http")
async def cabeceras_de_seguridad(request, call_next):
    respuesta = await call_next(request)
    respuesta.headers.setdefault("Content-Security-Policy", _CSP)
    respuesta.headers.setdefault("X-Frame-Options", "DENY")
    respuesta.headers.setdefault("X-Content-Type-Options", "nosniff")
    respuesta.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    respuesta.headers.setdefault("Permissions-Policy",
                                 "geolocation=(), microphone=(), camera=(), payment=()")
    # HSTS SOLO sobre HTTPS real (ver comentario arriba). Railway termina
    # TLS en su proxy y pasa el esquema original en X-Forwarded-Proto, así
    # que se mira ese header y no request.url.scheme, que acá siempre
    # sería "http".
    if request.headers.get("x-forwarded-proto") == "https":
        respuesta.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return respuesta

# ============================================================================
# COMPRESIÓN HTTP (28/07/2026, a raíz de "lo que más quiero es mejorar la
# latencia de cargar"). El proyecto no tenía NINGÚN middleware de
# compresión, así que todas las respuestas salían en crudo.
#
# Por qué importa tanto acá y no en un proyecto cualquiera: las respuestas
# grandes de este servicio son JSON con 3.200 jugadores, o sea la MISMA
# lista de claves ("nombre_canonico", "rating", "partidos_jugados"...)
# repetida 3.200 veces. Eso es el caso ideal para gzip. Medido con el
# payload real:
#
#     /rankings, 3.200 jugadores sin comprimir:  676,2 KB
#     el mismo payload con gzip:                  19,5 KB   (3%)
#
# Son 35 veces menos bytes por la red. Para comparar, TODA la página
# (HTML + CSS + JS) pesa 27,5 KB -- el JSON sin comprimir era 25 veces
# más grande que la página entera.
#
# En tiempo de descarga, sobre 4G flojo: 1.108 ms -> 32 ms.
#
# minimum_size=500: no vale la pena comprimir respuestas chicas, el costo
# de CPU y los headers extra no se pagan. compresslevel=6 es el punto
# habitual: nivel 9 comprime ~1% mejor y cuesta bastante más CPU.
#
# Nota: esto NO afecta al WebSocket /ws (gzip es solo HTTP). El "init" de
# /ws manda ese mismo payload de 3.200 jugadores; los WebSockets tienen su
# propia compresión (permessage-deflate) que la librería `websockets` de
# uvicorn[standard] negocia sola con el navegador. Conviene confirmarlo en
# la pestaña Network de las DevTools mirando el tamaño del frame -- si ahí
# aparecen ~676 KB en vez de ~20 KB, hay que activarla explícitamente.
from starlette.middleware.gzip import GZipMiddleware

# El parámetro `compresslevel` existe desde Starlette 0.37 (que es lo que
# exige fastapi>=0.111, así que debería estar). El try/except es para que,
# si el entorno de Railway resolviera una versión más vieja, el servicio
# arranque igual con el nivel por defecto en vez de morir en el import --
# no vale la pena tumbar el deploy por un parámetro de afinado.
try:
    app.add_middleware(GZipMiddleware, minimum_size=500, compresslevel=6)
except TypeError:
    app.add_middleware(GZipMiddleware, minimum_size=500)

# ============================================================================
# MEDICIÓN POR REQUEST (27/07/2026, a raíz de "la página está muy lenta" y
# la pregunta de Maria: "¿cómo puedo verificar qué procesos son los más
# lentos?"). Railway solo muestra el response time AGREGADO de todo el
# servicio -- no dice QUÉ endpoint es el lento. Este middleware mide cada
# request y deja dos salidas:
#
#   1) Log WARNING por cada request que tarde más de FTR_SLOW_REQUEST_MS
#      (default 1000 ms) -- visible directo en los logs de Railway, con
#      método, ruta, status y duración. Para cazar el culpable en vivo.
#
#   2) Acumulado en memoria por ruta (cantidad, promedio, máximo, cuántas
#      fueron lentas) consultable en GET /admin/perf?token=... -- para ver
#      el ranking de rutas lentas desde el último deploy sin leer logs.
#      En memoria a propósito: es diagnóstico, no historial -- se resetea
#      en cada deploy y no le suma NI UNA escritura a la DB (que es
#      justamente la parte contendida que se está midiendo).
#
# Se agrupa por el TEMPLATE de la ruta (ej. "/valor/historial/{id}/descartar",
# no "/valor/historial/123/descartar") para que las stats no se fragmenten
# por parámetro.
# ============================================================================
import threading as _threading
import time as _time

_SLOW_REQUEST_MS = float(os.getenv("FTR_SLOW_REQUEST_MS", "1000"))
_perf_stats: dict[str, dict] = {}
_perf_lock = _threading.Lock()
_perf_log = logging.getLogger("production.api.perf")

# VENTANA MÓVIL (29/07/2026, a pedido de Rubén tras leer el acumulado y
# creer que el sitio estaba lento AHORA cuando en realidad estaba
# leyendo la tormenta de arranque de hacía una hora): el acumulado de
# arriba nunca olvida -- 2 requests atrapados en el arranque arruinan
# el promedio de una ruta para siempre. Este buffer guarda cada request
# de los últimos _PERF_VENTANA_SEG segundos (deque con purga por edad,
# memoria acotada: a ~10 req/s son ~9000 tuplas chicas máximo) y
# /admin/perf lo resume aparte en "ultimos_15_min" -- ESA es la sección
# que responde "¿está lento AHORA?"; el acumulado responde "¿qué pasó
# desde el deploy?".
from collections import deque as _deque
_PERF_VENTANA_SEG = float(os.getenv("FTR_PERF_VENTANA_SEG", "900"))
_perf_ventana: _deque = _deque()  # tuplas (epoch, clave, ms)


@app.middleware("http")
async def _medir_requests(request: Request, call_next):
    t0 = _time.perf_counter()
    try:
        response = await call_next(request)
        status = response.status_code
    except Exception:
        status = 500
        raise
    finally:
        ms = (_time.perf_counter() - t0) * 1000
        # El template de la ruta recién existe DESPUÉS del routing --
        # por eso se lee acá y no antes de call_next.
        route = request.scope.get("route")
        ruta = getattr(route, "path", None) or request.url.path
        clave = f"{request.method} {ruta}"
        with _perf_lock:
            s = _perf_stats.setdefault(clave, {"n": 0, "total_ms": 0.0, "max_ms": 0.0, "lentas": 0})
            s["n"] += 1
            s["total_ms"] += ms
            if ms > s["max_ms"]:
                s["max_ms"] = ms
            if ms >= _SLOW_REQUEST_MS:
                s["lentas"] += 1
            # Ventana móvil: registrar y purgar lo viejo (ver nota arriba)
            ahora = _time.time()
            _perf_ventana.append((ahora, clave, ms))
            corte = ahora - _PERF_VENTANA_SEG
            while _perf_ventana and _perf_ventana[0][0] < corte:
                _perf_ventana.popleft()
        if ms >= _SLOW_REQUEST_MS:
            _perf_log.warning(f"[Perf] LENTO {clave} -> {status} en {ms:.0f} ms")
    return response


@app.get("/admin/perf")
def admin_perf(token: str = "", limite: int = 30):
    """
    Ranking de rutas más lentas desde el último deploy -- ordenado por
    tiempo TOTAL acumulado (n * promedio), que es lo que de verdad
    responde "quién se está comiendo el servidor": una ruta de 200 ms
    que se llama 5000 veces pesa más que una de 4 s que se llamó dos
    veces. max_ms delata los picos puntuales (ej. un lock de 20 s).
    """
    from production.api.deps import check_admin_token
    check_admin_token(token)
    with _perf_lock:
        filas = [
            {
                "ruta": clave, "requests": s["n"],
                "promedio_ms": round(s["total_ms"] / s["n"], 1),
                "max_ms": round(s["max_ms"], 1),
                "total_seg": round(s["total_ms"] / 1000, 1),
                "lentas_por_encima_umbral": s["lentas"],
            }
            for clave, s in _perf_stats.items()
        ]
    filas.sort(key=lambda f: f["total_seg"], reverse=True)

    # Resumen de la ventana móvil (ver nota en la declaración de
    # _perf_ventana): responde "¿está lento AHORA?" sin que la
    # tormenta de arranque de hace una hora contamine los números.
    with _perf_lock:
        corte = _time.time() - _PERF_VENTANA_SEG
        por_ruta: dict[str, dict] = {}
        for (t, clave, ms) in _perf_ventana:
            if t < corte:
                continue
            v = por_ruta.setdefault(clave, {"n": 0, "total_ms": 0.0, "max_ms": 0.0, "lentas": 0})
            v["n"] += 1
            v["total_ms"] += ms
            if ms > v["max_ms"]:
                v["max_ms"] = ms
            if ms >= _SLOW_REQUEST_MS:
                v["lentas"] += 1
    filas_ventana = [
        {
            "ruta": clave, "requests": v["n"],
            "promedio_ms": round(v["total_ms"] / v["n"], 1),
            "max_ms": round(v["max_ms"], 1),
            "total_seg": round(v["total_ms"] / 1000, 1),
            "lentas_por_encima_umbral": v["lentas"],
        }
        for clave, v in por_ruta.items()
    ]
    filas_ventana.sort(key=lambda f: f["total_seg"], reverse=True)

    # FIX PERDIDA-SILENCIOSA (30/07/2026): contadores de escritura. El
    # incidente del 30/07 perdió 12 filas de fixtures_ftr_elo devolviendo
    # 200 -- sin esta sección, la única evidencia era un log.error suelto
    # entre miles de líneas. `fallos_lock` es el número a mirar: cada
    # unidad es una escritura que se PERDIÓ. Debería quedarse en 0.
    from production.api.deps import (METRICAS_ESCRITURA, LOCK_ESPERA_MAX_S,
                                     FALLOS_POR_ORIGEN)
    escrituras = dict(METRICAS_ESCRITURA)
    # 07/09/2026: quién perdió cada escritura, de mayor a menor. Es lo
    # que convierte "se perdieron 2.604" en "se perdieron 2.604 y 2.100
    # son del flush de PinnOdds", que es lo único que permite elegir el
    # arreglo con criterio en vez de por corazonada.
    try:
        from production.api.routes_public import UPSERTS_SALTEADOS
        escrituras["upserts_fixtures_valor_salteados"] = UPSERTS_SALTEADOS["n"]
    except Exception:
        pass
    # Estado de la red de contención (07/09/2026). `pendientes` es el
    # número que importa: si no vuelve a 0, hay filas esperando en disco.
    try:
        from production.api.descartes_buffer import METRICAS_RESCATE, pendientes_en_disco
        escrituras["descartes_rescate"] = {**METRICAS_RESCATE,
                                           "pendientes": pendientes_en_disco()}
    except Exception:
        pass
    escrituras["fallos_por_origen"] = dict(
        sorted(FALLOS_POR_ORIGEN.items(), key=lambda kv: kv[1], reverse=True)[:20])
    # 09/09/2026: quién RETIENE, que no es quién pierde. fallos_por_origen
    # responde lo segundo (y el experimento del 07/09 con
    # FTR_UPSERT_SOLO_SI_CAMBIA=1 mostró que no alcanza para elegir el
    # arreglo). Ver production/api/instrumentacion_tx.py.
    try:
        from production.api import instrumentacion_tx as _instr_tx
        escrituras["retencion"] = _instr_tx.resumen()
    except Exception:
        pass
    escrituras["nota"] = (
        "fallos_lock = escrituras PERDIDAS (se agotaron los 3 intentos); debería ser 0. "
        "rechazos_503 = requests rechazados rápido por cola llena; > 0 significa "
        "saturación real pero SIN pérdida de datos -- el cliente puede reintentar."
    )
    escrituras["espera_max_lock_s"] = LOCK_ESPERA_MAX_S

    # Backfill de FTR/Elo (25/08/2026). El número a mirar es
    # `filas_completadas`: si se queda quieto mientras las tablas siguen
    # con pendientes, la tarea se trabó y las columnas del panel no se
    # van a llenar solas.
    try:
        from production.tasks.ftr_elo_backfill_task import METRICAS as _MET_FTR_ELO
        ftr_elo = dict(_MET_FTR_ELO)
    except Exception as e:
        ftr_elo = {"error": str(e)}

    return {
        "umbral_lento_ms": _SLOW_REQUEST_MS,
        "ftr_elo_backfill": ftr_elo,
        "nota": "acumulado en memoria desde el último deploy -- se resetea al redeployar",
        "escrituras": escrituras,
        "rutas": filas[:limite],
        "ultimos_15_min": {
            "nota": ("solo requests de los últimos "
                     f"{int(_PERF_VENTANA_SEG // 60)} minutos -- ESTA sección responde "
                     "'¿está lento ahora?'; la de arriba, '¿qué pasó desde el deploy?'"),
            "rutas": filas_ventana[:limite],
        },
    }

app.mount("/static", StaticFiles(directory=_WEB_DIR / "static"), name="static")
templates = Jinja2Templates(directory=_WEB_DIR / "templates")

# Auth: login, gestión de usuarios y dashboard (29/07/2026). Va PRIMERO
# para que /auth/* y /admin/usuarios queden definidos antes que las
# rutas genéricas de admin_router.
from production.api.routes_auth import router as auth_router
app.include_router(auth_router)

# ── PORTÓN DE ACCESO (29/07/2026) ────────────────────────────────────
# Antes de este middleware, /research y sus APIs eran accesibles con el
# token compartido en la URL. Ahora TODO exige sesión: quien no está
# identificado ve la pantalla de entrada y nada más.
#
# QUÉ QUEDA EXENTO Y POR QUÉ (esta lista es la parte delicada -- si algo
# falta acá, se rompe en producción):
#   * /auth/*        -> si el login exigiera login, nadie podría entrar.
#   * la pantalla de entrada y sus estáticos -> hay que poder dibujarla.
#   * endpoints del BOT -> se autentican con la cabecera
#     X-FTR-Internal-Secret, que es un mecanismo aparte y más fuerte que
#     una sesión de navegador. Si les exigiéramos cookie, el bot dejaría
#     de reportar partidos y el sistema se quedaría ciego.
#   * OPTIONS -> el preflight de CORS no lleva credenciales.
#   * /sw.js y /manifest.json (01/08/2026) -> el navegador los pide SIN
#     cookies de sesión: un service worker se descarga fuera del contexto
#     de la página. Sin exención devolvían 401, register() fallaba en
#     silencio y las notificaciones push NUNCA podían suscribirse -- se
#     veía como "no hay ningún dispositivo suscrito" sin más pistas.
#     Ninguno de los dos expone datos: uno es JavaScript estático y el
#     otro, metadatos del sitio.
_RUTAS_LIBRES = ("/auth/", "/entrar", "/static/", "/research/static/", "/favicon.ico",
                 "/health", "/healthz", "/sw.js", "/manifest.json")


@app.middleware("http")
async def _porton_de_acceso(request: Request, call_next):
    from production.api.auth import usuario_actual
    from production.api.deps import FTR_INTERNAL_SECRET, _tokens_iguales

    ruta = request.url.path

    # Credencial de SOLO LECTURA de BOT_BUSCADOR (28/09/2026). Vale
    # únicamente para GET /ft-intel/fixtures y GET /ft-intel/en-vivo. Quien
    # la presente en cualquier otra ruta o método recibe 403 -- incluidas las
    # rutas libres y aunque traiga una sesión. Va PRIMERO por eso. Ningún
    # navegador manda esta cabecera.
    from production.api.deps import CABECERA_LECTURA, lectura_permitida, lectura_valida
    lectura = request.headers.get(CABECERA_LECTURA, "")
    if lectura:
        if lectura_valida(lectura) and lectura_permitida(request.method, ruta):
            return await call_next(request)
        return JSONResponse(status_code=403,
                            content={"detail": "Credencial de lectura no válida para esta ruta"})

    if request.method == "OPTIONS" or any(ruta.startswith(r) for r in _RUTAS_LIBRES):
        return await call_next(request)

    # El bot: su secreto vale como identidad. Se compara en tiempo
    # constante igual que en verify_internal_secret.
    secreto = request.headers.get("X-FTR-Internal-Secret", "")
    if secreto and _tokens_iguales(secreto, FTR_INTERNAL_SECRET):
        return await call_next(request)


    # Identificado: ahora se chequea si su ROL alcanza para esta ruta.
    # El control va acá y no en cada endpoint por una razón concreta: hay
    # 29 rutas de escritura repartidas en 8.000 líneas, y una regla por
    # decorador se olvida. Acá, la regla que falta produce un 403 (negar
    # por defecto), no un agujero. Ver production/api/permisos.py.
    u = usuario_actual(request)
    if u is not None:
        from production.api.permisos import permiso_requerido, tiene_permiso

        necesario = permiso_requerido(request.method, ruta)
        if not tiene_permiso(u.get("rol", ""), necesario):
            logging.getLogger(__name__).warning(
                f"[Permisos] {u.get('usuario')} (rol={u.get('rol')}) "
                f"rechazado en {request.method} {ruta} -- necesita '{necesario}'")
            acepta_html_p = "text/html" in request.headers.get("accept", "")
            if acepta_html_p and request.method == "GET":
                # Navegación a una página que no le corresponde: se lo
                # manda a la que sí, en vez de mostrarle un JSON crudo.
                return RedirectResponse(url="/research", status_code=302)
            return JSONResponse(
                status_code=403,
                content={"detail": "Tu cuenta no tiene permiso para esta acción.",
                         "permiso_necesario": necesario})
        return await call_next(request)
    # Sin identificar. Al navegador le mostramos la pantalla de entrada;
    # a un cliente de API le devolvemos 401 en JSON, que es lo que sabe
    # interpretar (mandarle HTML lo confundiría).
    # Si trae una cookie de una sesión que MURIÓ (la desplazó otro
    # dispositivo, o un admin la cerró), averiguamos el motivo para
    # poder explicarlo en la pantalla de entrada. Solo se consulta en
    # este camino -- el de alguien que ya fue rechazado -- así que no
    # agrega trabajo a los requests normales.
    from production.api.auth import COOKIE_SESION, motivo_de_cierre
    motivo = None
    cookie_muerta = request.cookies.get(COOKIE_SESION, "")
    if cookie_muerta:
        from production.api.deps import get_conn as _gc
        conn = _gc()
        try:
            motivo = motivo_de_cierre(conn, cookie_muerta)
        except Exception:
            motivo = None
        finally:
            conn.close()

    acepta_html = "text/html" in request.headers.get("accept", "")
    if acepta_html and request.method == "GET":
        # Se guarda a dónde quería ir para devolverlo ahí tras el login
        # (entrar.html valida que sea una ruta interna antes de usarla).
        from urllib.parse import quote
        destino = quote(ruta, safe="/")
        url = f"/entrar?destino={destino}"
        if motivo:
            url += f"&motivo={quote(motivo)}"
        return RedirectResponse(url=url, status_code=302)
    return JSONResponse(status_code=401,
                        content={"detail": "No autenticado", "motivo": motivo})


@app.get("/entrar", response_class=HTMLResponse)
def pantalla_entrada(request: Request):
    """Pantalla de entrada: login y, si FTR_REGISTRO_ABIERTO=1, también
    el formulario para pedir acceso."""
    from production.api.auth import (MENSAJES_CIERRE, registro_abierto,
                                     registro_requiere_aprobacion)
    motivo = request.query_params.get("motivo", "")
    return templates.TemplateResponse(
        request=request, name="entrar.html",
        context={
            "registro_abierto": registro_abierto(),
            "requiere_aprobacion": registro_requiere_aprobacion(),
            # Solo se muestra si el motivo es uno de los que conocemos:
            # el valor viene de la URL y podría venir manipulado, así que
            # no se refleja texto arbitrario en la página.
            "mensaje_cierre": MENSAJES_CIERRE.get(motivo, ""),
        })


app.include_router(public_router)
app.include_router(admin_router)

# Configuración del sitio (31/07/2026): por ahora, los links de redes
# sociales del pie de página. Va DESPUÉS de admin_router porque
# routes_sitio importa check_admin_token_o_sesion de routes_admin.
from production.api.routes_sitio import (  # noqa: E402
    router_publico as sitio_publico_router,
    router_admin as sitio_admin_router,
)

# Notificaciones push al celular (01/08/2026).
from production.api.routes_push import router as push_router
app.include_router(push_router)


@app.get("/manifest.json", include_in_schema=False)
async def manifest():
    """
    El manifiesto va en la RAÍZ, igual que el service worker.

    Su `scope` es "/" y un manifiesto servido desde /static tendría un
    alcance limitado a esa carpeta: iOS lo ignoraría y volveríamos al
    problema del atajo que abre Safari.
    """
    from fastapi.responses import FileResponse
    return FileResponse(_WEB_DIR / "static" / "manifest.json",
                        media_type="application/manifest+json")


@app.get("/sw.js", include_in_schema=False)
async def service_worker():
    """
    El service worker DEBE servirse desde la raíz.

    El alcance de un service worker es su carpeta hacia abajo: uno
    servido desde /static/sw.js solo controlaría /static/*, y las
    notificaciones nunca llegarían. Por eso esta ruta explícita en vez
    de dejarlo en el mount de estáticos.
    """
    from fastapi.responses import FileResponse
    ruta = _WEB_DIR / "static" / "sw.js"
    return FileResponse(ruta, media_type="application/javascript",
                        headers={"Service-Worker-Allowed": "/"})
app.include_router(sitio_publico_router)
app.include_router(sitio_admin_router)

# Chat público del panel (31/07/2026, a pedido de Maria).
from production.api.routes_chat import router as chat_router  # noqa: E402
app.include_router(chat_router)

# Dashboard de picks (29/07/2026). Va DESPUÉS de public_router porque
# routes_picks importa _ensure_valor_historial_table de routes_public: el
# orden de include no crea el ciclo (el import es diferido, dentro de la
# función), pero mantenerlo así deja explícito quién depende de quién.
from production.api.routes_picks import router as picks_router  # noqa: E402
app.include_router(picks_router)

# FullTenis Research — Beta interna: mismo deploy, código completamente
# separado (research/ nunca es importado por production/api/routes_*,
# solo al revés — production nunca depende de research).
from research.api.routes_research import router as research_router  # noqa: E402
app.mount(
    "/research/static",
    StaticFiles(directory=Path(__file__).resolve().parent.parent.parent / "research" / "web" / "static"),
    name="research_static",
)
app.include_router(research_router)
# 💰 BALANCE DE APUESTAS (28/10/2026): el balance personal de cada usuario (pestaña 💰). Ver research/api/routes_balance.py.
from research.api.routes_balance import router as balance_router  # noqa: E402
app.include_router(balance_router)
# 🎲 FT PROBABILIDAD (30/10/2026): pestaña de solo lectura sobre el servicio aparte FT_PROBABILIDAD.
# Ver research/api/routes_ft_probabilidad.py (solo se consulta cuando un admin abre la pestaña).
from research.api.routes_ft_probabilidad import router as ft_probabilidad_router  # noqa: E402
app.include_router(ft_probabilidad_router)

# FT Intelligence (21/09/2026): servicio APARTE que lee de FullTenis solo
# por este router -- GET, conexión de solo lectura, paginado, con el
# secreto interno. No agrega tareas de fondo ni escrituras a este proceso.
# Ver production/api/routes_ft_intel.py.
from production.api.routes_ft_intel import router as ft_intel_router  # noqa: E402
app.include_router(ft_intel_router)

# BOT BUSCADOR (28/09/2026): solo la entrada /buscador y el token corto. El
# motor vive en otro servicio; este proceso nunca lo llama. Ver
# production/api/routes_buscador.py.
from production.api.routes_buscador import router as buscador_router  # noqa: E402
app.include_router(buscador_router)

# 📡 UTR EXTERNO (09/10/2026): entrada de las alertas del canal de Telegram
# DOC | TIRS (vía Reenvio_MTO). Ver production/api/routes_utr_externo.py.
from production.api.routes_utr_externo import router as utr_externo_router  # noqa: E402
app.include_router(utr_externo_router)
# 📡 FT NEWS · canales de Telegram (18/10/2026): config para Reenvio_MTO y entrada de sus mensajes como pistas.
# Ver production/api/routes_news_telegram.py y production/engines/news_telegram.py.
from production.api.routes_news_telegram import router as news_telegram_router  # noqa: E402
app.include_router(news_telegram_router)
# 🔥 UNDER UTR (v54i, 15/10/2026): lote en vivo del bot (línea de juegos + marcador exacto).
# Ver production/api/routes_under_utr.py y production/engines/under_utr.py.
from production.api.routes_under_utr import router as under_utr_router  # noqa: E402
app.include_router(under_utr_router)


@app.get("/", response_class=HTMLResponse)
def rankings_page(request: Request):
    # 16/07/2026, a pedido de Maria: "quitar una vista y dejar la
    # otra" -- antes acá vivía rankings.html (el ranking solo, sin el
    # resto de research), separado de /research/ (la página única con
    # todo). Redirige a la página única -- ahora hay UN solo lugar
    # para entrar, no dos.
    return RedirectResponse(url="/research/")


@app.get("/admin/rankings-simple", response_class=HTMLResponse)
def rankings_page_simple(request: Request):
    """
    rankings.html tal cual estaba -- YA NO es la vista pública (ver
    "/" arriba), queda viva SOLO para el iframe de "Tabla de rankings"
    de Admin (production/web/static/js/admin.js::_inyectarLapices()
    depende del id="tbody" de esta página puntual para el editor
    inline con lápiz -- tocar esa estructura rompería la edición de
    jugadores, así que se dejó la página intacta en vez de adaptarla).
    No pensada para uso directo -- no tiene link a esto en ningún
    lado, solo el iframe de admin.html la carga.
    """
    return templates.TemplateResponse(request=request, name="rankings.html")


# ── PANEL DE ADMINISTRACIÓN (29/07/2026) ─────────────────────────────
# La ruta es CONFIGURABLE por env. Ojo con las expectativas: mover el
# panel a una URL no adivinable es "seguridad por oscuridad" -- ayuda
# contra los escaneos automáticos que prueban /admin, /wp-admin y
# similares, pero NO es lo que protege el panel. Lo que lo protege es
# el login con sesión de production/api/auth.py. Esto es una capa
# extra, nunca la principal.
_ADMIN_PATH = os.getenv("FTR_ADMIN_PATH", "/admin")
if not _ADMIN_PATH.startswith("/"):
    _ADMIN_PATH = "/" + _ADMIN_PATH


@app.get(_ADMIN_PATH, response_class=HTMLResponse)
def admin_page(request: Request):
    """
    Panel nuevo: login + dashboard + usuarios. La página se sirve
    SIEMPRE (no hace falta estar logueado para ver el formulario), pero
    no trae ningún dato: todo lo carga por /admin/dashboard y
    /admin/usuarios, que sí exigen sesión. Devolver 404 acá tampoco
    escondería nada -- el atacante igual ve las rutas de la API.
    """
    # SIN CACHÉ (24/08/2026). El JS del panel va EMBEBIDO en panel.html,
    # no en un .js aparte, así que no tiene el `?v=` con el que se
    # invalidan los scripts de research. Resultado: después de cada
    # deploy el navegador seguía sirviendo el HTML viejo y las funciones
    # nuevas "no aparecían" -- pasó con la fila de Racha detallada en
    # Mensajes, y antes con la reorganización de Estrategias. La página
    # no trae datos (todo lo carga por API), así que cachearla no ahorra
    # prácticamente nada y cuesta una sesión de diagnóstico cada vez.
    #
    # Las tres cabeceras juntas porque los navegadores no coinciden en
    # cuál respetan: Cache-Control es la que manda hoy, Pragma cubre
    # proxies viejos y Expires es el fallback de HTTP/1.0.
    resp = templates.TemplateResponse(request=request, name="panel.html")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.get("/admin/legacy", response_class=HTMLResponse)
def admin_page_legacy(request: Request):
    """Panel viejo (edición de jugadores, torneos sin resolver). Se
    conserva porque tiene funciones que el panel nuevo todavía no
    replica."""
    return templates.TemplateResponse(request=request, name="admin.html")


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await manager.connect(ws)
    try:
        conn = sqlite3.connect(ACTIVE_DB)
        conn.execute("PRAGMA busy_timeout = 20000")  # FIX BUSY-TIMEOUT-FALTANTE (18/07/2026)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT p.nombre_canonico, p.genero, p.rank_oficial,
                   p.utr_real, p.edad,
                   r.rating, r.rd, r.partidos_jugados
            FROM players p JOIN ratings r ON p.player_id = r.player_id
            WHERE r.superficie = 'general' ORDER BY r.rating DESC LIMIT 3200
        """).fetchall()
        conn.close()

        # FIX RANKING-FTR-SIN-ALTURA-MANO (18/07/2026, reportado por
        # Maria: "por qué en esta vista no sale la altura y mano, pero
        # sí está en el Ranking Elo"). Causa real: la tabla de Ranking
        # FTR se llena de DOS fuentes -- este WebSocket manda el "init"
        # apenas se abre la página (antes de que el usuario toque
        # nada), y GET /rankings (el que sí tiene Altura/Mano, ver fix
        # anterior) recién se pide cuando el usuario hace clic en
        # Hombres/Mujeres o en una superficie. Como cargarRankingProduccion()
        # solo conecta el WebSocket al entrar (no llama a /rankings de
        # entrada), lo que se ve al abrir la página siempre era el
        # payload de acá -- que nunca tuvo Altura/Mano, solo `edad`
        # (columna vieja de producción, de un Excel semilla, sin
        # relación con player_bio). Se agrega el mismo cruce bulk contra
        # player_bio (research Elo) que ya usa /rankings, para que el
        # "init" traiga los mismos tres campos.
        from production.api.routes_public import _bio_bulk_por_genero, _bio_de
        bio_m = _bio_bulk_por_genero("M")
        bio_f = _bio_bulk_por_genero("F")

        jugadores = []
        for r in rows:
            d = dict(r)
            bio_indice = bio_m if d.get("genero") == "M" else bio_f if d.get("genero") == "F" else {}
            bio = _bio_de(bio_indice, d.get("nombre_canonico", ""), d.get("genero", ""))
            # OJO: no pisar `edad` -- esta consulta YA trae p.edad (columna
            # vieja de producción, de un Excel semilla, y es la que
            # funcionaba bien en la captura que mandó Maria). Solo se usa
            # la edad de player_bio como fallback si esa columna vieja
            # viniera vacía para algún jugador puntual.
            d["altura_cm"] = bio["altura_cm"]
            d["mano_dominante"] = bio["mano_dominante"]
            if d.get("edad") is None:
                d["edad"] = bio["edad"]
            jugadores.append(d)

        await ws.send_text(json.dumps({
            "evento": "init",
            "jugadores": jugadores,
        }, ensure_ascii=False))
        while True:
            await asyncio.sleep(30)
            try:
                await ws.send_text(json.dumps({"evento": "ping"}))
            except Exception:
                break
    except WebSocketDisconnect:
        manager.disconnect(ws)
    except Exception:
        manager.disconnect(ws)
