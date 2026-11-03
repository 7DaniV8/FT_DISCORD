"""
production/api/permisos.py — Qué puede hacer cada rol (29/07/2026).

POR QUÉ ESTE ARCHIVO EXISTE
───────────────────────────
Hasta hoy los roles ('admin' y 'lectura') solo se usaban en dos lugares:
`requiere_admin` protegía el panel de usuarios, y nada más. Todo el resto
del sistema pedía únicamente "estar logueado". El resultado era que un
usuario con rol 'lectura' -- el rol que reciben todas las cuentas del
auto-registro -- podía, llamando a la API directo desde la consola del
navegador:

    * borrar alertas del historial      (DELETE /valor/historial/{id})
    * cerrar y editar alertas ajenas    (POST  .../cerrar, .../editar-under)
    * ENVIAR PICKS AL GRUPO DE TELEGRAM (POST  /valor/enviar-picks)
    * gastar plata en análisis con IA   (GET   /research/api/analisis-ia)
    * editar la bio de los jugadores    (POST  /admin/player-bio)

El nombre 'lectura' describía una intención que el código no cumplía.

CÓMO SE ORGANIZA
────────────────
Un permiso es un string. Un rol es un conjunto de permisos. Una regla
asocia (método HTTP, patrón de ruta) con el permiso que hace falta
(REGLAS).

DÓNDE VIVE CADA COSA
────────────────────
  * QUÉ PERMISOS EXISTEN y QUÉ RUTA PIDE CUÁL -> acá, en el código. Son
    afirmaciones sobre cómo está construido el sistema; cambiarlas desde
    una pantalla no tendría sentido, porque una ruta nueva la agrega
    quien programa, no quien administra.
  * QUÉ PERMISOS TIENE CADA ROL -> en la base (tabla ftr_permisos_rol),
    editable desde el panel. Es una decisión de operación, y cambia sin
    necesidad de un deploy.

Las constantes PERMISOS_INICIALES de más abajo son solo el punto de
partida: con qué arranca el sistema la primera vez y a qué vuelve el
botón "Restaurar valores iniciales".

LA DECISIÓN IMPORTANTE: NEGAR POR DEFECTO
─────────────────────────────────────────
Cualquier request que MODIFIQUE algo (POST/PUT/PATCH/DELETE) y que no
coincida con ninguna regla, requiere ser admin. Es deliberado y tiene un
costo: cuando alguien agregue un endpoint nuevo de escritura, los usuarios
'lectura' van a recibir 403 hasta que se agregue su regla acá.

Ese costo se paga con gusto. La alternativa -- permitir lo no listado --
significa que olvidarse de una línea abre un agujero en silencio, y nadie
se entera hasta que pasa algo. Con negar por defecto, olvidarse de una
línea produce un 403 visible que alguien reporta en el día. Un sistema
seguro falla ruidosamente hacia el lado cerrado.

Las rutas del bot (X-FTR-Internal-Secret) no pasan por acá: se validan
antes, con un mecanismo aparte.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from datetime import datetime, timezone
from typing import Optional

# ── Catálogo de permisos ─────────────────────────────────────────────
# Se separan por lo que la acción PROVOCA, no por la pantalla en la que
# está el botón. "Enviar a Telegram" es su propio permiso porque manda un
# mensaje a gente real y no se puede deshacer; "gestionar alertas" toca la
# base pero se arregla; "ver" no provoca nada.
VER_RANKINGS = "ver_rankings"          # Ranking FTR, Ranking Elo, Comparador
VER_VALOR = "ver_valor"                # alertas de valor y señales en vivo
VER_EVENTOS = "ver_eventos"            # MTO, GAMES24HR
VER_HISTORIAL = "ver_historial"        # Historial de Alertas (auditoría)
VER_PICKS_PRE = "ver_picks_pre"        # dashboard de Picks Pre-partido
VER_PICKS_VIVO = "ver_picks_vivo"      # dashboard de Picks en Vivo
CONFIGURAR_UNIDAD = "configurar_unidad"  # fijar SU PROPIO valor de unidad
ANALISIS_IA = "analisis_ia"            # llamar al modelo -- cuesta plata por uso
GESTIONAR_ALERTAS = "gestionar_alertas"  # cerrar, descartar, editar, borrar
ENVIAR_TELEGRAM = "enviar_telegram"    # mandar picks/análisis al grupo
EDITAR_DATOS = "editar_datos"          # bio de jugadores, alias de torneos
ADMINISTRAR = "administrar"            # usuarios, dashboard, destinos, panel
# Chat público (31/07/2026, a pedido de Maria: "espacio para enviar
# mensajes... chat público para todos los usuarios del panel"). Permiso
# propio, no atado a ningún grupo de datos existente -- leer/escribir en
# el chat no es "ver valor" ni "ver rankings", es su propia cosa. Entra
# igual al sistema configurable (no un agujero aparte en _SIN_PERMISO)
# para que quede consistente con todo lo demás y Maria lo pueda sacar
# desde el panel si alguna vez quiere, aunque arranca otorgado a los dos
# roles que existen hoy -- ver TODOS_LOS_PERMISOS_GRUPO y
# PERMISOS_INICIALES["lectura"] más abajo.
USAR_CHAT = "usar_chat"

TODOS_LOS_PERMISOS_GRUPO = (
    VER_RANKINGS, VER_VALOR, VER_EVENTOS, VER_HISTORIAL,
    VER_PICKS_PRE, VER_PICKS_VIVO, CONFIGURAR_UNIDAD, ANALISIS_IA,
    GESTIONAR_ALERTAS, ENVIAR_TELEGRAM, EDITAR_DATOS, ADMINISTRAR, USAR_CHAT,
)

# Texto para mostrar en el panel. Que el admin lea "Enviar picks a
# Telegram" en vez de "enviar_telegram" es la diferencia entre entender
# lo que está otorgando y tildar casillas a ciegas.
ETIQUETAS = {
    VER_RANKINGS: "Ver rankings y comparador",
    VER_VALOR: "Ver alertas de valor y señales en vivo",
    VER_EVENTOS: "Ver MTO y GAMES24HR",
    VER_HISTORIAL: "Ver el historial de alertas",
    VER_PICKS_PRE: "Ver el dashboard de Picks Pre-partido",
    VER_PICKS_VIVO: "Ver el dashboard de Picks en Vivo",
    CONFIGURAR_UNIDAD: "Configurar su propio valor de unidad",
    ANALISIS_IA: "Pedir análisis con IA (tiene costo por uso)",
    GESTIONAR_ALERTAS: "Cerrar, editar y borrar alertas",
    ENVIAR_TELEGRAM: "Enviar picks y análisis a Telegram",
    EDITAR_DATOS: "Editar datos de jugadores y torneos",
    ADMINISTRAR: "Administrar usuarios y ver el dashboard",
    # usar_chat (17/08/2026): FALTABA. Nació el 31/07 y nunca se le puso
    # etiqueta, así que en el panel de Admin la casilla se veía con la
    # clave cruda ("usar_chat") en vez de un nombre legible. El resto de
    # los permisos por pestaña la reciben solos, del bucle
    # `ETIQUETAS[_p] = NOMBRE_TAB.get(...)` más abajo -- este no, porque
    # dejó de ser una pestaña el 01/08 (pasó a ser el botón flotante) y
    # salió de NOMBRE_TAB, pero el PERMISO se quedó. Lo detectaba
    # test_permisos.py ("todo permiso tiene etiqueta"), ahogado entre
    # las otras 25 fallas.
    USAR_CHAT: "Usar el chat de consultas",
}


ETIQUETAS_ROL = {
    "admin": "Administradores",
    "lectura": "Usuarios",
}

# Roles que vienen del código, no de una fila creada a mano (01/08/2026,
# a pedido de Maria: "que se pudieran crear más roles"). Estos dos nunca
# se pueden borrar ni renombrar desde el panel -- admin es irrenunciable
# (ver PERMISOS_IRRENUNCIABLES) y lectura es el rol por defecto de
# cualquier cuenta nueva/solicitud sin especificar otra cosa.
ROLES_PROTEGIDOS = frozenset({"admin", "lectura"})

# Cuáles son ACCIONES (hacen algo) y cuáles son VISTAS (solo miran). El
# panel las agrupa así porque son decisiones de naturaleza distinta: dar
# una vista es cuestión de qué le interesa a la persona; dar una acción es
# cuestión de en quién confiás.
ACCIONES = frozenset({ANALISIS_IA, GESTIONAR_ALERTAS, ENVIAR_TELEGRAM,
                      EDITAR_DATOS, ADMINISTRAR})
# CONFIGURAR_UNIDAD escribe en la base, pero solo la preferencia PROPIA de
# quien hace el request (el usuario_id sale de la sesión, no del cuerpo).
# No se agrupa con las acciones porque no afecta a nadie más, y meterla ahí
# sugeriría que darla es arriesgado.

# Nombre visible de cada tab, igual que se lee en /research. Sin esto el
# panel mostraría "rankingproduccion" y el admin tendría que adivinar a
# qué pantalla corresponde.
NOMBRE_TAB = {
    "rankingproduccion": "🏆 Ranking FTR",
    "ranking": "Ranking Elo",
    "compare": "Comparador A vs B",
    "mto": "🚑 MTO",
    "games24hr": "GAMES24HR",
    # Historial FullTennis (28/08/2026): archivo permanente de TODO lo
    # que el sistema detecta, suene o no. Lee, no genera alertas.
    "historialft": "\U0001F4DA Historial FullTennis",
    "valorftr": "Valor FTR",
    # 08/10/2026 (Rubén): "Picks en Vivo" pasa a llamarse "Under en Vivo" (son los
    # UNDER enviados); la pestaña de alertas del canal queda como "Alertas Under en Vivo".
    "undervivo": "Alertas Under en Vivo",
    "favoritovivo": "Favorito en Vivo",
    # Valor en Vivo (revivida 01/08/2026): pipeline viejo retirado el
    # 20/07 reemplazado por el mix de las 6+ estrategias "remontada"
    # del bot -- ver POST /live-signal/remontada en routes_public.py.
    "valorvivo": "🔁 Valor en Vivo",
    "racha": "🔥 Jugador en Racha",
    "validator": "Valor Pre-Partido",
    "movimientorentable": "Movimiento Rentable",
    "historialalertas": "Historial de Alertas",
    "pickspre": "Picks Pre-partido",
    "picksvivo": "Under en Vivo",
    "defensapuntos": "🛡 Defensa de Puntos",
    "retiros": "⭐ Regreso de Retiro",
    "envivodiag": "🔴 LIVE",
    "sonadoras": "🌙 Soñadoras",
    # Sugerencias (antes "Descartes" puertas adentro -- 31/07/2026, a
    # pedido de Maria: "renombralo por Sugerencias"). El tipo interno
    # sigue siendo 'descartes' (tabla descartes_ia, endpoint
    # /descartes/ia) para no romper nada que ya lo referencia -- solo
    # cambia el nombre visible, mismo criterio que ya se usó con
    # ftr_elo -> "Valor Pre-Partido". Nunca se había registrado acá,
    # por eso la pestaña no existía en ningún lado pese a que
    # descartes_ia se sigue llenando en cada corrida del bot.
    "descartes": "💡 Sugerencias",
    # Descartes (20/08/2026) -- distinta pestaña, distinta tabla
    # (descartes_generales). Ver la nota en config_estrategias.py sobre
    # por qué no se reusó descartes_ia/"Sugerencias".
    "descartes2": "🗑️ Descartes",
    "prepartidomatchup": "🧮 Prepartido - MatchUp",
    # ANÁLISIS PRE PARTIDO (10/09/2026, a pedido de Daniel): registro
    # permanente de TODOS los pre-partidos con ELO/FTR/MUP y cuota
    # congelados. Solo admin, igual que el resto del historial.
    "analisisprepartido": "📋 Análisis Pre Partido",
    # BOT BUSCADOR (28/09/2026): pestaña de entrada a un servicio APARTE.
    "buscador": "🔎 BOT BUSCADOR",
    # POST SET (26/08/2026, a pedido de Maria): % de acierto de Over Set 2
    # para los partidos de Ventaja leve / Pasan el filtro que perdieron el
    # Set 1 -- mismos datos que ya se mandan a Sugerencias.
    "postset": "🎾 POST SET",
    # Partidos Maestros (01/09/2026, a pedido de Maria): favoritos
    # moderados donde FTR y Elo coinciden -- ver
    # production/engines/partidos_maestros.py.
    "partidosmaestros": "🎓 Partidos Maestros",
    # Bajón Mental (01/09/2026, a pedido de Maria/Rubén): puntos de
    # set/partido perdidos + quiebre inmediato -- ver bajon_mental.py
    # del lado del bot y la tabla bajon_mental acá.
    "bajonmental": "🧠 Bajón Mental",
    # Cuota Mal Puesta V1 (07/10/2026): diagnóstico del motor nuevo --
    # ver production/engines/cuota_mal_puesta.py.
    "cuotamalpuesta": "🚨 Cuota Mal Puesta",
    # UTR VALUE (07/10/2026): ver production/engines/utr_value.py.
    "utrvalue": "📈 UTR VALUE",
    # UTR (08/10/2026, Rubén: "que el UTR sea una nueva tab con su propio
    # ranking"): ranking UTR de todos los jugadores que tenemos + (solo
    # admin) la gestión del padrón que antes vivía dentro de UTR VALUE.
    "utr": "🎯 Ranking UTR",
    # 🛠 Padrón UTR (08/10/2026): la gestión del padrón que vivía debajo del ranking.
    # Solo admin de fábrica, sin grupo (como BOT BUSCADOR): ver el ranking no la abre.
    "utrpadron": "🛠 Padrón UTR",
    # 📰 FT NEWS INTELLIGENCE (08/10/2026, Fase 0): nace vacía y solo admin, sin
    # grupo (como el buscador y el padrón): estado del servicio FT_NEWS y la
    # calibración de bandas. Ver production/engines/news_calibracion.py.
    "news": "📰 FT NEWS",
    # Recordador (08/10/2026, Rubén): cualquier usuario escribe un jugador y
    # el sistema avisa su próximo partido -- ver production/engines/recordador.py.
    "recordador": "⏰ Recordador",
    # 🏆 Implicación de Torneo (12/10/2026, Fase 0): solo admin de fábrica, sin
    # grupo (como el Padrón UTR). Ver production/engines/implicacion/.
    "implicacion": "🏆 Implicación de Torneo",
    # 🔥 UNDER UTR (v54i, 15/10/2026, Rubén): sin grupo, como Implicación. Ver production/engines/under_utr.py.
    "underutr": "🔥 Under UTR",
    # 💰 BALANCE DE APUESTAS (28/10/2026, Rubén): el balance PERSONAL de cada usuario (sus apuestas, su capital). Sin
    # grupo. Ver production/engines/balance_apuestas.py y research/api/routes_balance.py.
    "balance": "💰 Balance de apuestas",
    # 🎲 FT PROBABILIDAD (30/10/2026): lista de partidos que analiza el servicio aparte FT_PROBABILIDAD, con su
    # probabilidad experimental. Sin grupo, solo admin de fábrica (como el Padrón UTR). Ver research/api/routes_ft_probabilidad.py.
    "ftprobabilidad": "🎲 FT PROBABILIDAD",
    # "chat" salió de NOMBRE_TAB el 01/08/2026: dejó de ser una pestaña y
    # pasó a ser el botón flotante de aviso. El PERMISO (USAR_CHAT) se
    # queda -- sigue controlando quién puede usar el botón -- pero si la
    # clave siguiera acá aparecería como pestaña en el panel de orden y en
    # el de permisos, ofreciendo algo que ya no existe.
}


# ═════════════════════════════════════════════════════════════════════
#  VALORES INICIALES  (la matriz viva se edita desde el panel)
# ═════════════════════════════════════════════════════════════════════
#
# Esto NO es la configuración vigente: es con qué arranca el sistema la
# primera vez, y a qué se vuelve si alguien aprieta "Restaurar valores
# iniciales" en el panel. Lo que rige en cada momento sale de la tabla
# ftr_permisos_rol (ver `permisos_de`).
#
# El criterio del reparto inicial: un usuario CONSUME el producto (mira
# los rankings, las alertas y las señales en vivo) y no ejecuta ninguna
# acción. Un admin además OPERA el sistema.
#
# La línea se dibujó donde la acción tiene consecuencias que salen del
# navegador: plata (IA), mensajes a gente (Telegram) o datos que otros
# van a leer (historial, bios).
PERMISOS_INICIALES: dict[str, set[str]] = {
    # Se declara con los de GRUPO porque es como se lee ("un admin
    # puede todo"); _expandir_a_tabs() lo traduce a permisos finos más
    # abajo, apenas el catálogo por pestaña está definido.
    "admin": set(TODOS_LOS_PERMISOS_GRUPO),
    "lectura": {
        VER_RANKINGS,
        VER_VALOR,
        VER_EVENTOS,
        # Los dos dashboards de picks y su propia unidad: es exactamente
        # para lo que se pidieron estas pantallas, así que arrancan
        # otorgados. Se pueden quitar desde el panel como cualquier otro.
        VER_PICKS_PRE,
        VER_PICKS_VIVO,
        CONFIGURAR_UNIDAD,
        USAR_CHAT,
    },
}

# ── Pestañas nacidas DESPUÉS de TABS_AL_MIGRAR ───────────────────────
#
# BUG DE ORDEN (17/08/2026 -- estos tres bloques ESCRIBÍAN directo en
# PERMISOS_INICIALES acá arriba, y eso rompía la expansión de más abajo).
#
# Qué pasaba: PERMISOS_INICIALES se declara en el idioma de GRUPO
# ("admin ve ver_rankings"). Estos bucles le metían pestañas FINAS
# (ver_tab_defensapuntos, ver_tab_retiros, ver_tab_envivodiag). Después,
# en la línea ~468, corría _expandir_a_tabs(), cuya guarda dice: "si el
# grupo ya tiene alguna pestaña propia mencionada, esta lista ya está
# migrada -- no la re-expando". La guarda está bien para una fila
# GUARDADA (una pestaña mencionada = alguien la editó a mano, y
# re-expandir le devolvería accesos que le quitaron). Pero acá se
# aplicaba a los valores DE FÁBRICA, que estos bucles acababan de
# pre-sembrar con pestañas sueltas. Resultado: al llegar a ver_rankings
# encontraba ver_tab_defensapuntos, concluía "ya migrado", descartaba el
# grupo y NO agregaba las demás pestañas.
#
# Once pestañas quedaban invisibles para cualquier rol sin fila propia
# (rol nuevo, deploy limpio, o el respaldo de _leer_de_base cuando la
# base falla): compare, favoritovivo, movimientorentable, racha, ranking,
# rankingproduccion, sonadoras, undervivo, validator, valorftr, valorvivo.
#
# ARREGLO: acá solo se DECIDE a quién le corresponde cada una -- la
# decisión necesita leer el idioma de grupo, así que tiene que quedar
# antes de la expansión. La ESCRITURA se difiere hasta después, ver
# "_aplicar_tabs_tardias()" junto a _expandir_a_tabs().
_TABS_TARDIAS: dict[str, str] = {
    # pestaña -> grupo que decide quién la hereda por default
    "defensapuntos": VER_RANKINGS,
    # "Vuelven de un Retiro" (07/08/2026).
    "retiros": VER_RANKINGS,
    # "En Vivo -- Diagnóstico" (07/08/2026).
    "envivodiag": VER_VALOR,
    # Descartes (20/08/2026) -- mismo grupo que Sugerencias/Historial.
    "descartes2": VER_HISTORIAL,
    # POST SET (26/08/2026) -- mismo grupo que envivodiag/valorvivo.
    "postset": VER_VALOR,
    # Partidos Maestros (01/09/2026) -- mismos umbrales que deciden una
    # alerta de valor, mismo grupo que Sugerencias/valorvivo.
    "partidosmaestros": VER_VALOR,
    "bajonmental": VER_VALOR,
    "cuotamalpuesta": VER_VALOR,  # 07/10/2026
    "utrvalue": VER_VALOR,        # 07/10/2026
    # 08/10/2026: ranking (como Ranking FTR / Elo) y Recordador -- los dos
    # son para todos los usuarios, como el resto de los rankings.
    "utr": VER_RANKINGS,
    "recordador": VER_RANKINGS,
}
# {rol: {ver_tab_x, ...}} -- se aplica después de expandir.
_TABS_TARDIAS_POR_ROL: dict[str, set] = {}
for _rol in PERMISOS_INICIALES:
    for _tab, _grupo in _TABS_TARDIAS.items():
        if _grupo in PERMISOS_INICIALES[_rol] or _rol == "admin":
            _TABS_TARDIAS_POR_ROL.setdefault(_rol, set()).add("ver_tab_" + _tab)
# BOT BUSCADOR (28/09/2026): de fábrica SOLO admin (piloto). No va en
# _TABS_TARDIAS porque no pertenece a ningún grupo de vista: darla a otro rol
# desde el panel no le abre ninguna otra ruta. Ver PERMISOS_TAB más abajo.
_TABS_TARDIAS_POR_ROL.setdefault("admin", set()).add("ver_tab_buscador")
# 🛠 Padrón UTR (08/10/2026): de fábrica SOLO admin, sin grupo (igual que el buscador).
_TABS_TARDIAS_POR_ROL.setdefault("admin", set()).add("ver_tab_utrpadron")
# 🎲 FT PROBABILIDAD (30/10/2026): de fábrica SOLO admin, sin grupo (igual que el Padrón UTR).
_TABS_TARDIAS_POR_ROL.setdefault("admin", set()).add("ver_tab_ftprobabilidad")
# 🏆 Implicación de Torneo (12/10/2026): sin grupo. v47 (13/10/2026, Rubén: "activar los avisos de
# Implicación de Torneo para toda la comunidad autorizada, no solamente administradores"): de fábrica
# para los roles que ven las alertas de valor (como Cuota Mal Puesta) y admin. La pestaña muestra el
# dashboard, el historial y las estadísticas; la investigación sigue siendo solo admin (_imp_solo_admin).
# Un rol cuya matriz guardada ya opinó sobre esta pestaña queda como está: se cambia en Admin → Permisos.
for _rol in PERMISOS_INICIALES:
    if _rol == "admin" or VER_VALOR in PERMISOS_INICIALES[_rol]:
        _TABS_TARDIAS_POR_ROL.setdefault(_rol, set()).add("ver_tab_implicacion")
# 🔥 UNDER UTR (v54i, 15/10/2026): sin grupo; de fábrica igual que Implicación (admin + roles que ven las
# alertas de valor). El diagnóstico del mercado y la exportación son solo admin (ver routes_research).
for _rol in PERMISOS_INICIALES:
    if _rol == "admin" or VER_VALOR in PERMISOS_INICIALES[_rol]:
        _TABS_TARDIAS_POR_ROL.setdefault(_rol, set()).add("ver_tab_underutr")
# 💰 BALANCE DE APUESTAS (28/10/2026): sin grupo. De fábrica para TODOS los roles: cada usuario ve y guarda SOLO su
# propio balance (el usuario sale de la sesión, nunca del pedido). Se quita por rol en Admin → Permisos.
for _rol in PERMISOS_INICIALES:
    _TABS_TARDIAS_POR_ROL.setdefault(_rol, set()).add("ver_tab_balance")
# 📰 FT NEWS (08/10/2026): sin grupo. De fábrica para TODOS los roles (noche:
# "los usuarios ven el dashboard y el historial; solo los administradores ven
# todo el panel"): la pestaña se dibuja reducida para quien no administra.
for _rol in PERMISOS_INICIALES:
    _TABS_TARDIAS_POR_ROL.setdefault(_rol, set()).add("ver_tab_news")

# ADMINISTRAR no se le puede quitar al rol 'admin'. Sin esta garantía, un
# admin puede desmarcar la casilla de "administrar" para su propio rol,
# guardar, y quedar sin forma de volver a entrar al panel para revertirlo
# -- el sistema queda sin administración posible y hay que arreglarlo a
# mano en la base. El panel muestra esa casilla fija y el servidor la
# vuelve a agregar aunque llegue un request que la omita.
# ── Permisos agregados DESPUÉS de que ya hubiera filas guardadas ──────
#
# Bug real (30/07/2026): a Dani, rol 'lectura', no le aparecía el campo
# "Valor de tu unidad". No era la pantalla: era que _leer_de_base()
# REEMPLAZA los valores iniciales por lo que diga la fila guardada. La
# fila de 'lectura' se había guardado antes del 29/07, cuando
# configurar_unidad y los dos ver_picks_* todavía no existían, así que
# no los mencionaba -- y "no mencionado" se leía como "denegado".
#
# El problema de fondo: una fila guardada no distingue "esto se quitó a
# propósito" de "esto todavía no existía cuando guardé". Para los
# permisos de esta lista se resuelve a favor del valor inicial: si la
# fila no los nombra, se toma lo que dice PERMISOS_INICIALES en vez de
# asumir denegado. Una vez que alguien guarda desde el panel, la fila ya
# los nombra (guardar_permisos escribe el conjunto completo) y esta
# lista deja de aplicar para ese rol.
#
# Al agregar un permiso nuevo al catálogo, sumarlo acá. Sacar los viejos
# no es urgente pero conviene: una vez que todos los roles guardaron al
# menos una vez, la entrada no hace nada.
PERMISOS_AGREGADOS_TARDE: frozenset = frozenset({
    VER_PICKS_PRE,          # 29/07/2026
    VER_PICKS_VIVO,         # 29/07/2026
    CONFIGURAR_UNIDAD,      # 29/07/2026
    # Defensa de Puntos: pestaña creada DESPUÉS de la migración por
    # pestañas del 31/07, así que sí es un permiso genuinamente nuevo y
    # tiene que pasar por este mecanismo. Va como literal porque
    # _perm_tab() todavía no está definida a esta altura del archivo;
    # equivale a _perm_tab("defensapuntos").
    "ver_tab_defensapuntos",    # 31/07/2026
    # Sugerencias (antes "Descartes"): mismo motivo que defensapuntos
    # arriba -- pestaña que nunca tuvo permiso propio hasta hoy.
    "ver_tab_descartes",        # 31/07/2026
    "usar_chat",  # 31/07/2026 (ver_tab_chat retirado el 01/08)
    # Valor en Vivo (01/08/2026): pestaña revivida, permiso nuevo --
    # mismo motivo que defensapuntos/descartes arriba.
    "ver_tab_valorvivo",        # 01/08/2026
    # Soñadoras (11/08/2026): FALTABA. Nació después de TABS_AL_MIGRAR
    # igual que las de arriba, pero nunca se agregó a esta lista, así
    # que ningún rol con fila ya guardada la veía -- ni 'admin'. Si hoy
    # aparece en el panel es porque alguien tildó la casilla a mano.
    # Encontrado el 17/08/2026 midiendo contra una fila simulada.
    "ver_tab_sonadoras",        # 11/08/2026
    # Prepartido - MatchUp (16/08/2026): pestaña nueva, mismo motivo
    # que las de arriba -- sin esto, ninguna fila ya guardada la ve
    # (ni siquiera 'admin', hasta que alguien vuelva a guardar sus
    # permisos desde el panel).
    "ver_tab_prepartidomatchup",  # 16/08/2026
    # BOT BUSCADOR (28/09/2026): nuevo; así las filas ya guardadas toman
    # el valor de fábrica (solo admin) en vez de leerlo como denegado.
    "ver_tab_buscador",           # 28/09/2026
    # Análisis Pre Partido (10/09/2026): mismo motivo que las de arriba
    # -- sin esto, ningún rol con fila YA GUARDADA la ve, ni siquiera
    # 'admin', hasta que alguien vuelva a guardar sus permisos.
    "ver_tab_analisisprepartido",  # 10/09/2026
    # POST SET (26/08/2026): pestaña nueva, mismo motivo que las de
    # arriba -- sin esto, ningún rol con fila ya guardada la ve.
    "ver_tab_postset",            # 26/08/2026
    # Partidos Maestros (01/09/2026): pestaña nueva, mismo motivo que
    # las de arriba -- sin esto, ningún rol con fila ya guardada la ve.
    "ver_tab_partidosmaestros",   # 01/09/2026
    "ver_tab_bajonmental",        # 01/09/2026
    "ver_tab_cuotamalpuesta",     # 07/10/2026
    "ver_tab_utrvalue",           # 07/10/2026
    "ver_tab_utr",                # 08/10/2026
    "ver_tab_recordador",         # 08/10/2026
    "ver_tab_utrpadron",          # 08/10/2026 (solo admin de fábrica)
    "ver_tab_news",               # 08/10/2026 (solo admin de fábrica)
    "ver_tab_implicacion",        # 12/10/2026 (solo admin de fábrica)
    "ver_tab_underutr",           # 15/10/2026 (v54i)
    "ver_tab_balance",            # 28/10/2026 (💰 Balance de apuestas)
    "ver_tab_ftprobabilidad",     # 30/10/2026 (🎲 FT PROBABILIDAD, solo admin de fábrica)
})


PERMISOS_IRRENUNCIABLES: dict[str, set[str]] = {
    "admin": {ADMINISTRAR},
}


# ── Tabs de /research ────────────────────────────────────────────────
# data-tab (en research.html) -> permiso necesario. Un tab sin permiso NO
# SE RENDERIZA: el HTML del panel ni siquiera se le manda al navegador,
# así que no hay nada que "des-ocultar" desde las herramientas de
# desarrollo. Aun así, el que protege es el gating de los endpoints de
# abajo -- esconder el tab es prolijidad, no una barrera.
# ── Permiso de GRUPO por tab ─────────────────────────────────────────
# Este mapa es el que gobierna las RUTAS, no la interfaz. Existe porque
# los endpoints están compartidos entre pestañas: /valor alimenta a Valor
# FTR, Under en Vivo, Favorito en Vivo, Racha, Valor Pre-Partido y
# Movimiento Rentable a la vez. No hay forma de gatear /valor "solo para
# Under en Vivo" -- es el mismo endpoint.
GRUPO_POR_TAB: dict[str, str] = {
    "rankingproduccion": VER_RANKINGS,
    "ranking": VER_RANKINGS,
    "compare": VER_RANKINGS,
    "mto": VER_EVENTOS,
    "games24hr": VER_EVENTOS,
    # VER_EVENTOS y no VER_VALOR: cruza todas las estrategias, asi que
    # atarla al permiso de una sola no tendria sentido.
    "historialft": VER_EVENTOS,
    "valorftr": VER_VALOR,
    "undervivo": VER_VALOR,
    "favoritovivo": VER_VALOR,
    "valorvivo": VER_VALOR,
    "racha": VER_VALOR,
    "validator": VER_VALOR,
    "movimientorentable": VER_VALOR,
    "historialalertas": VER_HISTORIAL,
    # Sugerencias (antes "Descartes" puertas adentro, 31/07/2026): va
    # con el mismo grupo que Historial de Alertas -- Maria: "funciona
    # de la misma manera que Historial de Alertas".
    "descartes": VER_HISTORIAL,
    # Descartes (20/08/2026, a pedido de Maria: bandeja NUEVA, separada
    # de Sugerencias, para "todo lo que no pasó a Picks o Sugerencias"
    # -- ver DESTINOS_NO_PASA_VALIDOS en routes_public.py). Mismo grupo
    # que Sugerencias/Historial: los tres muestran resultados de
    # estrategias ya evaluadas, no alertas en curso.
    "descartes2": VER_HISTORIAL,
    # Prepartido - MatchUp (16/08/2026, a pedido de Maria: "por ahora
    # solo admin, pero dejalo listo para en un futuro mostrárselo a
    # usuarios"). Va con VER_HISTORIAL, NO con VER_VALOR -- ese grupo
    # ya está en el set inicial de 'lectura' (ver PERMISOS_INICIALES
    # más abajo), así que ponerlo ahí lo dejaría visible para lectura
    # desde el primer día por herencia de grupo, sin que nadie lo haya
    # decidido. VER_HISTORIAL NO está en el set inicial de 'lectura'
    # (mismo motivo que historialalertas/descartes arriba: parten
    # apagados). El día que Maria quiera abrirlo, es tildar
    # "ver_tab_prepartidomatchup" para el rol que corresponda en el
    # panel de Admin -- sin deploy.
    "prepartidomatchup": VER_HISTORIAL,
    # Análisis Pre Partido (10/09/2026): VER_HISTORIAL, el mismo que
    # respalda el endpoint /analisis-pre-partido. Parte apagado para
    # 'lectura' igual que los demás historiales; para abrirlo alcanza
    # con tildar "ver_tab_analisisprepartido" en el panel de Admin, sin
    # deploy.
    "analisisprepartido": VER_HISTORIAL,
    "pickspre": VER_PICKS_PRE,
    "picksvivo": VER_PICKS_VIVO,
    # Defensa de Puntos (31/07/2026): es una vista de ranking
    # -- puntos que caen de la ventana de 52 semanas -- no una
    # alerta de valor. Va con el grupo de rankings.
    "defensapuntos": VER_RANKINGS,
    # Vuelven de un Retiro (07/08/2026, a pedido de Rubén). Es contexto
    # sobre el estado del jugador -- quién viene de abandonar y cuándo
    # vuelve a jugar -- no una alerta de valor: no evalúa
    # probabilidades ni recomienda nada. Por eso va con el grupo de
    # rankings y no con el de alertas.
    "retiros": VER_RANKINGS,
    # En Vivo -- Diagnóstico (07/08/2026, a pedido de Rubén: "varios
    # partidos en vivo que no suenan y no sé por qué"). Muestra el
    # estado de las alertas en vivo, así que va con el grupo de alertas
    # de valor, no con rankings.
    "envivodiag": VER_VALOR,
    # Soñadoras (12/08/2026, pedido de Rubén). Los partidos de
    # mañana que pasan los tres filtros, para armar una combinada.
    # Van con el grupo de valor: son los mismos umbrales que
    # deciden una alerta.
    "sonadoras": VER_VALOR,
    # POST SET (26/08/2026): mide alertas de Valor en Vivo (Ventaja
    # leve / Pasan el filtro), así que va con el mismo grupo que
    # envivodiag y valorvivo.
    "postset": VER_VALOR,
    # Partidos Maestros (01/09/2026): favoritos moderados con FTR y Elo
    # de acuerdo -- misma familia de umbrales de valor que las demás.
    "partidosmaestros": VER_VALOR,
    "bajonmental": VER_VALOR,
    "cuotamalpuesta": VER_VALOR,  # 07/10/2026
    "utrvalue": VER_VALOR,        # 07/10/2026
    # UTR (08/10/2026): es un ranking, va con el grupo de rankings.
    "utr": VER_RANKINGS,
    # Recordador (08/10/2026): no es una alerta de valor; cualquier usuario
    # que vea los rankings puede pedir que le avisen un partido.
    "recordador": VER_RANKINGS,
}


# ── UN PERMISO POR PESTAÑA ───────────────────────────────────────────
# 31/07/2026, a pedido de Rubén: "las tabs están por grupos... entonces
# que cada tab fuera un check".
#
# El problema concreto: hasta acá, "Ver alertas de valor y señales en
# vivo" era UNA casilla que prendía o apagaba SEIS pestañas de una
# (Valor FTR, Under en Vivo, Favorito en Vivo, Jugador en Racha, Valor
# Pre-Partido, Movimiento Rentable). No había forma de darle a alguien
# Under en Vivo sin darle también Movimiento Rentable.
#
# Cómo se resuelve, y por qué así:
#
#   · Cada pestaña tiene ahora su propio permiso: `ver_tab_<tab>`. Eso es
#     lo único que se edita en el panel y lo único que decide si la
#     pestaña se dibuja.
#   · Los permisos de GRUPO (ver_valor, ver_rankings...) NO desaparecen,
#     pero dejan de editarse a mano: se DERIVAN. Un rol tiene `ver_valor`
#     si tiene al menos una de las seis pestañas de ese grupo.
#
# Por qué no se borran los de grupo directamente: son los que gatean las
# RUTAS, y las rutas están compartidas (ver GRUPO_POR_TAB arriba). Si un
# usuario tiene Under en Vivo, necesita poder llamar a GET /valor -- y ese
# mismo endpoint lo usa Movimiento Rentable. Prometer en el panel un
# aislamiento por endpoint que no existe sería peor que no ofrecerlo:
# esto separa lo que SE VE, que es lo que se pidió, y es honesto sobre
# el resto.
def _perm_tab(tab: str) -> str:
    return "ver_tab_" + tab


PERMISOS_TAB: dict[str, str] = {t: _perm_tab(t) for t in GRUPO_POR_TAB}
# BOT BUSCADOR (28/09/2026): pestaña SIN grupo a propósito. derivar_grupos()
# le da a un rol el permiso de grupo de cualquier pestaña que tenga; como el
# buscador no tiene grupo, concederlo no arrastra acceso a nada más.
PERMISOS_TAB["buscador"] = _perm_tab("buscador")
# 🛠 Padrón UTR (08/10/2026): también SIN grupo; las rutas /admin/utr/* siguen pidiendo ADMINISTRAR.
PERMISOS_TAB["utrpadron"] = _perm_tab("utrpadron")
# 📰 FT NEWS (08/10/2026): también SIN grupo; /api/news/* pide ver_tab_news.
PERMISOS_TAB["news"] = _perm_tab("news")
# 🏆 Implicación de Torneo (12/10/2026): SIN grupo; /api/implicacion/* (GET) pide ver_tab_implicacion.
PERMISOS_TAB["implicacion"] = _perm_tab("implicacion")
# 🔥 UNDER UTR (v54i, 15/10/2026): SIN grupo; /research/api/under-utr/* (GET) pide ver_tab_underutr.
PERMISOS_TAB["underutr"] = _perm_tab("underutr")
# 💰 BALANCE DE APUESTAS (28/10/2026): SIN grupo; /research/balance y /research/api/balance/* piden ver_tab_balance.
PERMISOS_TAB["balance"] = _perm_tab("balance")
# 🎲 FT PROBABILIDAD (30/10/2026): SIN grupo; /research/api/ft-probabilidad/* (GET) pide ver_tab_ftprobabilidad.
PERMISOS_TAB["ftprobabilidad"] = _perm_tab("ftprobabilidad")

# Las pestañas que YA EXISTÍAN cuando se migró a permisos por pestaña
# (31/07/2026). Solo éstas quedan fuera del mecanismo de "permisos
# agregados tarde".
#
# La distinción importa. Para una pestaña de esta lista, una fila vieja
# SÍ opinó -- lo dijo con su permiso de grupo -- y resolverla desde
# PERMISOS_INICIALES devolvería accesos que se habían quitado a mano.
# Para una pestaña creada DESPUÉS no hay tal opinión: es un permiso
# nuevo como cualquier otro, y si se lo excluye del mecanismo no lo ve
# NADIE cuyo rol se haya guardado alguna vez. Sin este corte por fecha,
# toda pestaña futura nace invisible.
TABS_AL_MIGRAR: frozenset = frozenset({
    "rankingproduccion", "ranking", "compare", "mto", "games24hr",
    "valorftr", "undervivo", "favoritovivo", "racha", "validator",
    "movimientorentable", "historialalertas", "pickspre", "picksvivo",
})
PERMISOS_TAB_MIGRADOS: frozenset = frozenset(
    _perm_tab(t) for t in TABS_AL_MIGRAR)

# El que usan puede_ver_tab() y las plantillas: fino, uno por pestaña.
PERMISO_POR_TAB: dict[str, str] = dict(PERMISOS_TAB)

# Grupo -> pestañas que lo componen. Se usa para derivar el permiso de
# grupo y para migrar las filas viejas.
TABS_POR_GRUPO: dict[str, list[str]] = {}
for _t, _g in GRUPO_POR_TAB.items():
    TABS_POR_GRUPO.setdefault(_g, []).append(_t)

for _t, _p in PERMISOS_TAB.items():
    ETIQUETAS[_p] = NOMBRE_TAB.get(_t, _t)

# Los de grupo quedan al final: el orden importa poco salvo para que el
# panel los pueda ignorar de un corte limpio.
TODOS_LOS_PERMISOS = tuple(PERMISOS_TAB.values()) + TODOS_LOS_PERMISOS_GRUPO

# Lo que el panel ofrece tildar. Los permisos de vista de GRUPO quedan
# afuera a propósito: son derivados, y mostrarlos junto a los finos sería
# ofrecer dos controles para la misma cosa, con uno pisando al otro.
VISTAS_DE_GRUPO = frozenset({VER_RANKINGS, VER_VALOR, VER_EVENTOS,
                             VER_HISTORIAL, VER_PICKS_PRE, VER_PICKS_VIVO})
EDITABLES = tuple(p for p in TODOS_LOS_PERMISOS if p not in VISTAS_DE_GRUPO)


def derivar_grupos(permisos: set[str]) -> set[str]:
    """Agrega los permisos de grupo que se desprenden de las pestañas
    otorgadas. Es lo que hace que el gating de rutas siga funcionando sin
    que nadie tenga que tildar `ver_valor` a mano."""
    salida = set(permisos)
    for grupo, tabs in TABS_POR_GRUPO.items():
        if any(PERMISOS_TAB[t] in salida for t in tabs):
            salida.add(grupo)
    return salida


def _expandir_a_tabs(permisos) -> set[str]:
    """
    Traduce permisos de vista de GRUPO a los permisos finos de cada una de
    sus pestañas, y saca el de grupo. Solo actúa sobre los grupos que no
    tengan ya alguna pestaña propia mencionada.

    Es la migración: una fila guardada antes de este cambio dice
    `ver_valor` y quiere decir "las seis pestañas de valor". Traducirla
    así deja a todo el mundo exactamente con lo que tenía. Y la condición
    de "solo si no hay ninguna pestaña del grupo" evita que una fila ya
    migrada se re-expanda y le devuelva pestañas que le quitaron a mano.
    """
    salida = set(permisos)
    for grupo, tabs in TABS_POR_GRUPO.items():
        if grupo in salida:
            salida.discard(grupo)
            if not any(PERMISOS_TAB[t] in salida for t in tabs):
                salida |= {PERMISOS_TAB[t] for t in tabs}
    return salida


# Los valores iniciales se declararon arriba en términos de grupo (que es
# como se leen: "un usuario ve las alertas de valor"). Se traducen acá,
# una sola vez al importar, para que el resto del módulo trabaje siempre
# con permisos finos.
#
# La versión EN GRUPO se conserva porque el mecanismo de
# PERMISOS_AGREGADOS_TARDE razona en esos términos: una fila vieja que no
# menciona `ver_picks_pre` no lo hace porque ese permiso no existía
# cuando se guardó, y para resolverlo hay que preguntarle a los iniciales
# en el mismo idioma en el que está escrita la fila. Ver _leer_de_base.
PERMISOS_INICIALES_GRUPO = {rol: set(ps)
                            for rol, ps in PERMISOS_INICIALES.items()}
PERMISOS_INICIALES = {rol: _expandir_a_tabs(ps)
                      for rol, ps in PERMISOS_INICIALES.items()}

# Recién ACÁ se escriben las pestañas nacidas después de TABS_AL_MIGRAR
# -- ver el bloque "_TABS_TARDIAS" arriba para el bug de orden que esto
# corrige (17/08/2026). Después de la expansión, agregarlas es
# inofensivo: la guarda de _expandir_a_tabs() ya hizo su trabajo y no
# hay nada más que pueda confundirlas con "esta lista ya está migrada".
#
# Van a los DOS diccionarios a propósito. _leer_de_base() cruza
# `nunca_vistos & iniciales` contra la unión de ambos, y para una fila
# guardada ANTES de que existiera la pestaña ese cruce es lo único que
# se la puede devolver -- si faltara en los dos, la pestaña nueva
# quedaría invisible para todo rol ya guardado.
for _rol, _tabs in _TABS_TARDIAS_POR_ROL.items():
    PERMISOS_INICIALES[_rol] |= _tabs
    PERMISOS_INICIALES_GRUPO[_rol] |= _tabs


# ── Reglas por ruta ──────────────────────────────────────────────────
# (métodos, patrón, permiso). El patrón se ancla al principio de la ruta.
# El ORDEN IMPORTA: gana la primera que coincide, así que lo específico
# va antes que lo general.
_M_ESCRITURA = ("POST", "PUT", "PATCH", "DELETE")
_M_TODOS = ("GET", "POST", "PUT", "PATCH", "DELETE")

REGLAS: list[tuple[tuple[str, ...], str, str]] = [
    # --- Acciones que salen del sistema: Telegram ---
    (("POST",), r"/valor/enviar-picks", ENVIAR_TELEGRAM),
    # Crear Pick (31/07/2026): manda a Telegram igual que enviar-picks,
    # mismo permiso.
    (("POST",), r"/valor/crear-pick", ENVIAR_TELEGRAM),
    # Chat público (31/07/2026): el GET no necesita regla (no está en
    # _M_ESCRITURA, "GET no listado" ya se permite) -- pero el POST SÍ
    # necesita esto, si no cae en ADMINISTRAR por default ("negar por
    # defecto" para escrituras no listadas, ver permiso_requerido()).
    (("POST",), r"/api/chat/mensajes", USAR_CHAT),
    (("POST",), r"/descartes/ia/[^/]+/enviar", ENVIAR_TELEGRAM),
    (("POST",), r"/valor/[^/]+/[^/]+/enviar-analisis", ENVIAR_TELEGRAM),
    (("POST",), r"/admin/telegram/probar", ENVIAR_TELEGRAM),

    # --- Análisis con IA: cuesta por llamada ---
    (("GET", "POST"), r"/research/api/analisis-ia", ANALISIS_IA),
    # --- BOT BUSCADOR (28/09/2026): el token solo a quien ve la pestaña ---
    (("GET",), r"/api/buscador/token$", "ver_tab_buscador"),
    (("POST",), r"/descartes/ia/[^/]+/guardar-analisis", ANALISIS_IA),

    # --- Gestión de alertas y análisis ---
    (_M_ESCRITURA, r"/valor/historial", GESTIONAR_ALERTAS),
    (_M_ESCRITURA, r"/valor-ftr/[^/]+/cerrar", GESTIONAR_ALERTAS),
    (_M_ESCRITURA, r"/descartes/ia", GESTIONAR_ALERTAS),
    # /valor/enviar-sugerencia (31/07/2026): a propósito NO va con
    # ENVIAR_TELEGRAM -- esta acción no manda nada a Telegram, solo
    # archiva en descartes_ia (Sugerencias). Va antes que un eventual
    # catch-all de /valor/... para no heredar un permiso que no le
    # corresponde.
    (("POST",), r"/valor/enviar-sugerencia", GESTIONAR_ALERTAS),
    (_M_ESCRITURA, r"/analisis-partidos", GESTIONAR_ALERTAS),

    # --- Edición de datos de referencia ---
    (("POST",), r"/admin/player-bio", EDITAR_DATOS),
    (("POST",), r"/admin/condiciones", EDITAR_DATOS),

    # --- Dashboard de picks ---
    # El PUT de la unidad va primero: es más específico que la lectura y
    # tiene su propio permiso, porque es una preferencia personal y no
    # debería exigir los mismos privilegios que ver los dashboards.
    (("GET", "PUT"), r"/picks-unidad", CONFIGURAR_UNIDAD),
    (("GET",), r"/picks/pre", VER_PICKS_PRE),
    (("GET",), r"/picks/vivo", VER_PICKS_VIVO),

    # --- Lectura del historial (respalda el tab admin-only) ---
    (("GET",), r"/valor/historial", VER_HISTORIAL),
    (("GET",), r"/descartes/ia", VER_HISTORIAL),
    (("GET",), r"/analisis-partidos", VER_HISTORIAL),
    # Análisis Pre Partido (10/09/2026): registro permanente de todos
    # los pre-partidos. Solo admin, igual que el resto del historial.
    (("GET",), r"/analisis-pre-partido", VER_HISTORIAL),
    # Registro de Under en Vivo por estado (23/09/2026): incluye las
    # zonas de observación, que no se mandan a usuarios. Solo historial.
    (("GET",), r"/under-vivo/registro", VER_HISTORIAL),
    # Prepartido - MatchUp (16/08/2026, a pedido de Maria: "por ahora
    # solo admin, pero dejalo listo para en un futuro mostrárselo a
    # usuarios"). A propósito NO va con ADMINISTRAR (ese permiso es
    # irrenunciable para 'admin' pero no tiene sentido dárselo a otro
    # rol -- traería consigo administrar usuarios). Va con su propio
    # permiso fino (ver_tab_prepartidomatchup, el mismo que dibuja o
    # esconde el botón en research.html): hoy solo 'admin' lo tiene por
    # default (ver PERMISOS_INICIALES), y el día que se quiera abrir a
    # 'lectura' es tildar la casilla en el panel de Admin -- sin tocar
    # esta regla.
    (("GET",), r"/api/prepartido-matchup", "ver_tab_prepartidomatchup"),
    # Partidos Maestros (01/09/2026) -- mismo patrón que Prepartido -
    # MatchUp arriba: permiso fino propio, mismo que dibuja o esconde
    # el botón en research.html.
    (("GET",), r"/api/partidos-maestros", "ver_tab_partidosmaestros"),
    (("GET",), r"/api/bajon-mental", "ver_tab_bajonmental"),
    (("GET",), r"/api/cuota-mal-puesta", "ver_tab_cuotamalpuesta"),
    (("GET",), r"/api/utr-value", "ver_tab_utrvalue"),
    # UTR MARKET ANOMALY (07/10/2026): bloque dentro de la pestaña UTR VALUE.
    (("GET",), r"/api/utr-anomalia", "ver_tab_utrvalue"),
    # 📰 FT NEWS (08/10/2026): calibración y estado, de quien ve la pestaña (solo admin de fábrica).
    (("GET",), r"/api/news/", "ver_tab_news"),
    # 🏆 Implicación de Torneo (12/10/2026): lectura de quien ve la pestaña; los POST caen en admin.
    (("GET",), r"/api/implicacion/", "ver_tab_implicacion"),
    # 🔥 UNDER UTR (v54i, 15/10/2026): lectura de quien ve la pestaña; los POST caen en admin.
    (("GET",), r"/research/api/under-utr", "ver_tab_underutr"),
    (("GET",), r"/research/api/dashboard/under-utr", "ver_tab_underutr"),
    # 💰 Balance de apuestas (28/10/2026): leer y GUARDAR el balance propio es de quien ve la pestaña (todos los
    # usuarios de fábrica). Sin esta línea el PUT caería en «negar por defecto» (solo admin).
    (("GET", "PUT"), r"/research/api/balance", "ver_tab_balance"),
    (("GET",), r"/research/balance", "ver_tab_balance"),
    # 🎲 FT PROBABILIDAD (30/10/2026): lectura de quien ve la pestaña (solo admin de fábrica).
    (("GET",), r"/research/api/ft-probabilidad", "ver_tab_ftprobabilidad"),
    # ⏰ Recordador (08/10/2026): escribir un jugador o quitarlo es de quien
    # ve la pestaña (todos los usuarios), no de ADMINISTRAR. Sin esta línea
    # el POST caería en "negar por defecto" (solo admin).
    (("POST", "DELETE"), r"/research/api/recordador", "ver_tab_recordador"),

    # --- Lectura de eventos ---
    (("GET",), r"/mto/", VER_EVENTOS),
    (("GET",), r"/games24hr/", VER_EVENTOS),

    # --- Lectura de valor y señales en vivo ---
    (("GET",), r"/valor", VER_VALOR),
    (("GET",), r"/valor-ftr", VER_VALOR),
    (("GET",), r"/live-", VER_VALOR),
    # Export de Jugador en Racha (21/08/2026, a pedido de Maria: "solo
    # lo puedan ver los administradores ese botón") -- va ANTES que el
    # catch-all /racha/ de abajo (VER_VALOR, que tiene todo el rol
    # 'lectura'): las reglas se evalúan en orden y gana la primera que
    # matchea, así que si esta línea quedara después, /racha/exportar
    # heredaría VER_VALOR por el prefijo compartido y el botón dejaría
    # de ser admin-only. Mismo criterio que Prepartido - MatchUp: no va
    # con ADMINISTRAR porque ese permiso es irrenunciable para 'admin'
    # y no tiene sentido asociarlo a otro rol (traería consigo
    # administrar usuarios) -- pero acá SÍ corresponde ADMINISTRAR
    # porque es exactamente lo que Maria pidió ("solo administradores"),
    # a diferencia de MatchUp que tiene su propio permiso fino pensado
    # para abrirse a futuro.
    (("GET",), r"/racha/exportar", ADMINISTRAR),
    (("GET",), r"/racha/", VER_VALOR),
    # Export de Racha detallada (24/08/2026, a pedido de Maria: "por qué
    # el excel solo me exporta 200" -- reusaba _rachaApiData, que en
    # pantalla tiene tope). Mismo criterio que el export de arriba:
    # ADMINISTRAR y ANTES del catch-all r"/racha-api/" de abajo, tal
    # cual decía el comentario que dejamos ahí el 21/08 anticipando este
    # endpoint.
    (("GET",), r"/racha-api/exportar", ADMINISTRAR),
    # Export de Historial FT (28/08/2026, mismo criterio que los de
    # arriba: la tabla en pantalla tiene tope de 300/2000, el export
    # tiene que traer TODO sin limite -- solo admin).
    (("GET",), r"/historial-fulltennis/exportar", ADMINISTRAR),
    # Racha propia, fuente API-Tennis (21/08/2026). Regla PROPIA y no
    # heredada: el catch-all de arriba es r"/racha/" con barra, y esta
    # ruta es "/racha-api/..." -- el guion no matchea la barra, así que
    # sin esta línea el endpoint quedaría fuera de toda regla. Se le da
    # VER_VALOR, el mismo que la pestaña 'racha' donde se muestra
    # (PERMISO_POR_TAB), para que quien ya puede ver la pestaña pueda
    # ver las dos tablas y no una sola.
    #
    # No lleva ADMINISTRAR como el export de arriba porque esto no es un
    # export: es la vista, el equivalente de /racha/historial.
    (("GET",), r"/racha-api/", VER_VALOR),
    (("GET",), r"/fixture/valor", VER_VALOR),
    (("GET",), r"/fixture/ftr", VER_VALOR),
    (("GET",), r"/pinnodds/", VER_VALOR),
    (("GET",), r"/partido", VER_VALOR),
    (("GET",), r"/research/api/en-vivo", VER_VALOR),
    (("GET",), r"/research/api/partido-en-vivo", VER_VALOR),

    # --- Lectura de rankings y comparación ---
    (("GET",), r"/rankings", VER_RANKINGS),
    (("GET",), r"/research/api/rankings", VER_RANKINGS),
    (("GET",), r"/research/api/compare", VER_RANKINGS),
    (("GET",), r"/research/api/history", VER_RANKINGS),
    (("GET",), r"/research/api/buscar-jugador", VER_RANKINGS),
    (("GET",), r"/research/api/leaderboard", VER_RANKINGS),
    (("GET",), r"/research/api/recientes", VER_RANKINGS),
    (("GET",), r"/research/api/ftr-", VER_RANKINGS),
    (("GET",), r"/ftr/interpretar-delta", VER_RANKINGS),
    (("GET",), r"/estilo/", VER_RANKINGS),
    (("GET",), r"/condiciones/", VER_RANKINGS),
    (("GET",), r"/player/", VER_RANKINGS),

    # --- Administración ---
    (_M_TODOS, r"/admin/usuarios", ADMINISTRAR),
    (_M_TODOS, r"/admin/solicitudes", ADMINISTRAR),
    (_M_TODOS, r"/admin/dashboard", ADMINISTRAR),
    (_M_TODOS, r"/admin/telegram", ADMINISTRAR),
    (_M_TODOS, r"/admin/perf", ADMINISTRAR),
    # Cajón de sastre para /admin/, ÚLTIMO a propósito: las reglas de
    # arriba y las de EDITAR_DATOS (/admin/player-bio, /admin/condiciones)
    # están antes y por lo tanto ganan.
    #
    # Existe porque la primera versión de este archivo listaba los /admin/
    # uno por uno y el test encontró que GET /admin/permisos quedaba
    # abierto para el rol 'lectura' -- sencillamente porque me olvidé de
    # la línea. Los GET no listados se permiten (hay muchas rutas de
    # lectura inofensivas), así que sin este cajón cada endpoint
    # administrativo nuevo nace abierto. Con él, nace cerrado.
    (_M_TODOS, r"/admin/", ADMINISTRAR),
]

# Se compilan una vez al importar, no en cada request.
_REGLAS_COMPILADAS = [(ms, re.compile("^" + pat), p) for ms, pat, p in REGLAS]

# Rutas sin permiso asociado: la propia sesión y la navegación básica.
# No incluye ninguna ruta de datos.
_SIN_PERMISO = re.compile(
    r"^(/auth/|/entrar|/static/|/research/static/|/favicon\.ico|/health|/healthz|/ws$)")


def permiso_requerido(metodo: str, ruta: str) -> Optional[str]:
    """
    Qué permiso hace falta para (método, ruta). None = no hace falta ninguno.

    Devuelve ADMINISTRAR para cualquier escritura no listada -- ver
    "NEGAR POR DEFECTO" arriba.
    """
    metodo = (metodo or "GET").upper()
    if metodo == "OPTIONS" or _SIN_PERMISO.match(ruta):
        return None
    for metodos, patron, permiso in _REGLAS_COMPILADAS:
        if metodo in metodos and patron.match(ruta):
            return permiso
    if metodo in _M_ESCRITURA:
        return ADMINISTRAR
    return None      # GET no listado: navegación de páginas, /, etc.


# ═════════════════════════════════════════════════════════════════════
#  PERSISTENCIA: la matriz viva sale de la base, no de este archivo
# ═════════════════════════════════════════════════════════════════════
#
# EL PROBLEMA A RESOLVER: `permisos_de` se llama en CADA request desde el
# middleware. Una consulta a SQLite por request para leer una tabla de dos
# filas sería absurdo -- y peor: en este proyecto las escrituras se
# serializan con PROD_DB_WRITE_LOCK, así que agregar tráfico de lectura al
# camino caliente empeora la latencia que se estuvo peleando toda la
# semana (ver CAMBIOS_PERF.md).
#
# Solución: caché en memoria con TTL corto. El TTL existe porque puede
# haber más de un worker: el que NO recibió el cambio se enteraría solo al
# vencer su caché. 10 segundos es la demora máxima en propagarse un cambio
# de permisos, y es un precio razonable por no tocar la base en cada
# request. El worker que recibe el POST invalida su caché al instante.

_TTL_CACHE_SEGUNDOS = 10
_cache: dict[str, set[str]] = {}
_cache_vence = 0.0
_cache_lock = threading.Lock()
_TABLA_LISTA = False


def _ensure_tabla(conn) -> None:
    global _TABLA_LISTA
    if _TABLA_LISTA:
        return
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ftr_permisos_rol (
            rol            TEXT PRIMARY KEY,
            permisos       TEXT NOT NULL,      -- JSON: lista de permisos
            actualizado_en TEXT,
            actualizado_por TEXT
        )
    """)
    # `conocidos` (30/07/2026): el catálogo completo tal como existía en
    # el momento de guardar esta fila. Es lo que permite distinguir "este
    # permiso se quitó a propósito" de "este permiso todavía no existía
    # cuando se guardó". Sin esa distinción, un permiso nuevo o queda
    # denegado para siempre en silencio, o revive cada vez que alguien
    # intenta quitarlo. Filas viejas lo tienen NULL: ver _leer_de_base.
    try:
        conn.execute("ALTER TABLE ftr_permisos_rol ADD COLUMN conocidos TEXT")
    except Exception:
        pass        # ya existe
    # etiqueta (01/08/2026, roles creados desde el panel) -- nombre para
    # mostrar de un rol custom (ej. "moderador" -> "Moderadores"). Los
    # dos roles del código (admin/lectura) NO usan esta columna, siguen
    # con ETIQUETAS_ROL -- esta es solo para los que se crean a mano.
    try:
        conn.execute("ALTER TABLE ftr_permisos_rol ADD COLUMN etiqueta TEXT")
    except Exception:
        pass        # ya existe
    conn.commit()
    _TABLA_LISTA = True


def _leer_de_base() -> dict[str, set[str]]:
    """
    Lee la matriz guardada. Un rol SIN fila usa sus valores iniciales:
    así el primer arranque funciona sin que nadie configure nada, y un rol
    nuevo agregado en el código no nace sin permisos.

    Si la base falla, se devuelven los valores iniciales en vez de
    propagar la excepción. Es deliberado: un error de base acá dejaría el
    middleware sin poder decidir y todo el sistema devolvería 500. Volver
    a los iniciales mantiene la app en pie con permisos conservadores.
    """
    matriz = {rol: set(ps) for rol, ps in PERMISOS_INICIALES.items()}
    try:
        # El import va DENTRO del try, no arriba. Estaba afuera y el test
        # lo encontró: si `deps` falla al importarse, la excepción se
        # escapaba del respaldo y el middleware devolvía 500 en todas las
        # rutas. El docstring prometía degradar a los valores iniciales y
        # no lo cumplía para ese caso.
        from production.api.deps import get_conn

        conn = get_conn()
        try:
            _ensure_tabla(conn)
            for fila in conn.execute(
                    "SELECT rol, permisos, conocidos FROM ftr_permisos_rol"):
                try:
                    guardados = json.loads(fila["permisos"])
                except (TypeError, ValueError):
                    continue        # fila corrupta: se ignora y queda el inicial
                try:
                    conocidos = set(json.loads(fila["conocidos"] or "null") or [])
                except (TypeError, ValueError):
                    conocidos = set()
                # Se filtra contra el catálogo: si un permiso se elimina
                # del código, las filas viejas que lo mencionen no
                # resucitan un permiso que ya no existe.
                guardados = {p for p in guardados if p in TODOS_LOS_PERMISOS}
                # Los permisos que no existían cuando se guardó esta fila
                # no pueden leerse como "denegados": la fila nunca opinó
                # sobre ellos. Para esos, vale el valor inicial. Ver
                # PERMISOS_AGREGADOS_TARDE para el porqué.
                # Qué permisos NO pudo haber opinado esta fila:
                #   · con `conocidos` cargado, exactamente los que no
                #     estaban en el catálogo al guardar. Si el permiso SÍ
                #     estaba y no figura en `permisos`, se lo quitaron a
                #     propósito y así queda.
                #   · sin `conocidos` (fila anterior al 30/07), no hay
                #     forma de saberlo: se cae a la lista declarada de
                #     permisos agregados tarde.
                if conocidos:
                    nunca_vistos = set(TODOS_LOS_PERMISOS) - conocidos
                else:
                    nunca_vistos = set(PERMISOS_AGREGADOS_TARDE) - guardados
                # Los permisos por pestaña (31/07/2026) quedan FUERA de
                # este mecanismo a propósito. Para una fila guardada
                # antes del cambio son, técnicamente, "nunca vistos" --
                # pero resolverlos con PERMISOS_INICIALES sería un error:
                # a un rol al que se le habían QUITADO las alertas de
                # valor se las devolvería, porque el inicial las trae. La
                # respuesta fiel a esa fila la da _expandir_a_tabs(), que
                # traduce el `ver_valor` que la fila SÍ dice.
                nunca_vistos -= PERMISOS_TAB_MIGRADOS
                revividos: set = set()
                if nunca_vistos:
                    # Los iniciales, en los DOS idiomas: `nunca_vistos`
                    # puede traer permisos de grupo (ej. ver_picks_pre) y
                    # permisos por pestaña nacidos después del 31/07 (ej.
                    # ver_tab_sonadoras), así que hay que mirar las dos
                    # versiones de los iniciales.
                    iniciales = (PERMISOS_INICIALES.get(fila["rol"], set())
                                 | PERMISOS_INICIALES_GRUPO.get(fila["rol"], set()))
                    revividos = nunca_vistos & iniciales
                # MISMO BUG DE ORDEN que el de los valores iniciales
                # (17/08/2026 -- ver el bloque "_TABS_TARDIAS" arriba).
                # Antes, los revividos se sumaban a `guardados` ACÁ, y
                # recién después corría _expandir_a_tabs(). Si un
                # revivido era una pestaña fina de un grupo que la fila
                # menciona en idioma viejo (ej. la fila dice
                # `ver_rankings` y se revive `ver_tab_defensapuntos`), la
                # guarda de _expandir_a_tabs leía esa pestaña como "esta
                # fila ya está migrada" y NO expandía el grupo: el rol
                # perdía rankingproduccion, ranking, compare y retiros.
                # Los dos conjuntos se expanden POR SEPARADO y recién
                # ahí se unen. Expandir los revividos aparte es necesario
                # -- entre ellos puede haber permisos de grupo (ver
                # arriba), que sin expandir no dibujarían ninguna
                # pestaña -- y es seguro, porque salen de un conjunto
                # limpio donde la guarda no tiene nada que malinterpretar.
                guardados = _expandir_a_tabs(guardados)
                if revividos:
                    guardados |= _expandir_a_tabs(revividos)
                matriz[fila["rol"]] = guardados
        finally:
            conn.close()
    except Exception as e:      # noqa: BLE001
        logging.getLogger(__name__).error(
            f"[Permisos] no se pudo leer la matriz, se usan los valores "
            f"iniciales: {e}")
        return {rol: derivar_grupos(set(ps))
                for rol, ps in PERMISOS_INICIALES.items()}

    # Los irrenunciables se aplican SIEMPRE, incluso sobre lo guardado.
    for rol, obligatorios in PERMISOS_IRRENUNCIABLES.items():
        matriz.setdefault(rol, set()).update(obligatorios)
    # Y al final se derivan los permisos de grupo desde las pestañas. Va
    # ACÁ, en la matriz ya armada, y no en cada rama de arriba: así vale
    # igual para un rol con fila guardada, uno sin fila (valores
    # iniciales) y el camino de degradación por error de base.
    return {rol: derivar_grupos(ps) for rol, ps in matriz.items()}


def matriz_actual(forzar: bool = False) -> dict[str, set[str]]:
    """La matriz vigente, con caché."""
    global _cache, _cache_vence
    ahora = time.monotonic()
    with _cache_lock:
        if forzar or not _cache or ahora >= _cache_vence:
            _cache = _leer_de_base()
            _cache_vence = ahora + _TTL_CACHE_SEGUNDOS
        return _cache


def invalidar_cache() -> None:
    """Fuerza la relectura en la próxima consulta. La llama el endpoint
    que guarda, para que el cambio se vea de inmediato en este worker."""
    global _cache_vence
    with _cache_lock:
        _cache_vence = 0.0


def roles_conocidos() -> dict[str, str]:
    """
    Todos los roles que existen hoy -> etiqueta para mostrar. Es la
    unión de los dos roles del código (admin/lectura, vía ETIQUETAS_ROL)
    con cualquier rol creado desde el panel (una fila real en
    ftr_permisos_rol -- crear_rol() es lo único que las inserta).

    Esto es lo que reemplaza a "PERMISOS_INICIALES" como fuente de
    verdad de "qué roles existen" en guardar_permisos/
    restaurar_iniciales/resumen_para_panel: antes esos tres validaban
    contra PERMISOS_INICIALES, que es un dict fijo del código con
    exactamente dos claves -- por eso no se podía crear un rol nuevo,
    aunque la tabla y el resto del motor de permisos ya soportaban
    cualquier nombre de rol sin problema (ver _leer_de_base, que arma
    la matriz a partir de las FILAS de la base, no de este dict).
    """
    from production.api.deps import get_conn

    etiquetas = dict(ETIQUETAS_ROL)
    conn = get_conn()
    try:
        _ensure_tabla(conn)
        for fila in conn.execute("SELECT rol, etiqueta FROM ftr_permisos_rol"):
            if fila["rol"] not in etiquetas:  # no pisar admin/lectura si por lo que sea tienen fila
                etiquetas[fila["rol"]] = fila["etiqueta"] or fila["rol"]
    finally:
        conn.close()
    return etiquetas


def crear_rol(nombre: str, etiqueta: str = "", copiar_de: str = "lectura", por: str = "") -> dict:
    """
    Crea un rol nuevo (01/08/2026, a pedido de Maria: "que se pudieran
    crear más roles y que si creo un rol me permita otorgarle
    permisos"). Arranca con los permisos de `copiar_de` (default
    "lectura") en vez de vacío -- un rol recién creado sin ninguna
    vista dejaría la app en blanco para la primera cuenta que se le
    asigne, y nadie se daría cuenta hasta que esa persona entrara y no
    viera nada. Se pueden destildar desde el panel apenas se crea.

    `nombre` es la clave interna (la que viaja en la cookie de sesión y
    en las URLs de la API) -- se normaliza a minúsculas/guiones bajos
    para que sea segura como tal, no como el admin la tipeó.
    """
    from production.api.deps import PROD_DB_WRITE_LOCK, get_conn

    nombre = re.sub(r"[^a-z0-9_]+", "_", (nombre or "").strip().lower()).strip("_")
    if not nombre:
        raise ValueError("El nombre del rol no puede estar vacío (usá letras, números o guión bajo)")
    if len(nombre) > 40:
        raise ValueError("El nombre del rol es demasiado largo (máximo 40 caracteres)")
    etiqueta = (etiqueta or "").strip() or nombre.replace("_", " ").title()

    conn = get_conn()
    try:
        _ensure_tabla(conn)
        if nombre in roles_conocidos():
            raise ValueError(f"Ya existe un rol '{nombre}'")

        # Mismo criterio que resumen_para_panel(): de la matriz derivada
        # del rol origen, solo los permisos FINOS (EDITABLES) -- los de
        # grupo son derivados, no se copian ni se guardan sueltos.
        finales = set(matriz_actual().get(copiar_de, set())) & set(EDITABLES)

        with PROD_DB_WRITE_LOCK:
            conn.execute("""
                INSERT INTO ftr_permisos_rol (rol, permisos, actualizado_en, actualizado_por, conocidos, etiqueta)
                VALUES (?,?,?,?,?,?)
            """, (nombre, json.dumps(sorted(finales)),
                  datetime.now(timezone.utc).isoformat(), por,
                  json.dumps(sorted(TODOS_LOS_PERMISOS)), etiqueta))
            conn.commit()
    finally:
        conn.close()

    invalidar_cache()
    logging.getLogger(__name__).info(f"[Permisos] {por} creó el rol '{nombre}' (copiado de '{copiar_de}')")
    return {"rol": nombre, "etiqueta": etiqueta, "permisos": sorted(finales)}


def borrar_rol(nombre: str, por: str = "") -> None:
    """
    Borra un rol creado desde el panel. admin/lectura son
    ROLES_PROTEGIDOS -- no se pueden borrar nunca, ni con este endpoint
    ni de ningún otro modo (son la base del sistema: sin "lectura" las
    solicitudes/altas nuevas no tendrían default, y admin es
    irrenunciable por diseño).

    Rechaza el borrado si todavía hay cuentas con este rol asignado --
    borrar la fila las dejaría con un rol sin ningún permiso (ver
    permisos_de: "un rol desconocido no tiene ninguno"), sin ningún
    aviso de por qué la app les quedó en blanco. Hay que reasignarlas
    primero (panel de Usuarios).
    """
    from production.api.deps import PROD_DB_WRITE_LOCK, get_conn

    nombre = (nombre or "").strip().lower()
    if nombre in ROLES_PROTEGIDOS:
        raise ValueError(f"El rol '{nombre}' es parte del sistema y no se puede borrar")
    if nombre not in roles_conocidos():
        raise ValueError(f"Rol desconocido: {nombre}")

    conn = get_conn()
    try:
        en_uso = conn.execute(
            "SELECT COUNT(*) AS n FROM ftr_usuarios WHERE rol=?", (nombre,)
        ).fetchone()["n"]
        if en_uso:
            raise ValueError(
                f"Hay {en_uso} cuenta(s) con el rol '{nombre}' -- reasignalas antes de borrarlo")
        with PROD_DB_WRITE_LOCK:
            conn.execute("DELETE FROM ftr_permisos_rol WHERE rol=?", (nombre,))
            conn.commit()
    finally:
        conn.close()
    invalidar_cache()
    logging.getLogger(__name__).info(f"[Permisos] {por} borró el rol '{nombre}'")


def guardar_permisos(rol: str, permisos, por: str = "") -> set[str]:
    """
    Guarda los permisos de un rol y devuelve el conjunto que quedó.

    Valida contra el catálogo (nada de inventar permisos por POST) y
    aplica los irrenunciables. Lanza ValueError si el rol no existe.
    """
    from production.api.deps import PROD_DB_WRITE_LOCK, get_conn

    rol = (rol or "").strip()
    if rol not in roles_conocidos():
        raise ValueError(f"Rol desconocido: {rol}")

    pedidos = {str(p) for p in (permisos or [])}
    desconocidos = pedidos - set(TODOS_LOS_PERMISOS)
    if desconocidos:
        raise ValueError(f"Permisos inexistentes: {sorted(desconocidos)}")

    finales = pedidos | PERMISOS_IRRENUNCIABLES.get(rol, set())
    # Se guardan SOLO permisos finos. Si llegara alguno de grupo -- de un
    # panel viejo cacheado, o de un curl a mano -- se traduce a sus
    # pestañas en vez de rechazarlo. Guardar los dos tipos mezclados
    # dejaría la fila ambigua: `ver_valor` presente y `ver_tab_undervivo`
    # ausente no querría decir nada claro.
    finales = _expandir_a_tabs(finales)

    conn = get_conn()
    try:
        _ensure_tabla(conn)
        with PROD_DB_WRITE_LOCK:
            conn.execute("""
                INSERT INTO ftr_permisos_rol (rol, permisos, actualizado_en, actualizado_por, conocidos)
                VALUES (?,?,?,?,?)
                ON CONFLICT(rol) DO UPDATE SET
                    permisos=excluded.permisos,
                    actualizado_en=excluded.actualizado_en,
                    actualizado_por=excluded.actualizado_por,
                    conocidos=excluded.conocidos
            """, (rol, json.dumps(sorted(finales)),
                  datetime.now(timezone.utc).isoformat(), por,
                  # El catálogo completo de hoy: a partir de este guardado,
                  # lo que falte en `permisos` es una quita deliberada.
                  json.dumps(sorted(TODOS_LOS_PERMISOS))))
            conn.commit()
    finally:
        conn.close()

    invalidar_cache()
    logging.getLogger(__name__).info(
        f"[Permisos] {por} cambió el rol '{rol}' a: {sorted(finales)}")
    return finales


def restaurar_iniciales(rol: str, por: str = "") -> set[str]:
    """
    Vuelve un rol a los valores del código, borrando su fila.

    Solo aplica a admin/lectura (ROLES_PROTEGIDOS) -- son los únicos
    que tienen un valor "del código" al que volver (PERMISOS_INICIALES).
    Un rol creado desde el panel no tiene ese respaldo: borrar su única
    fila lo dejaría sin ningún permiso Y sin etiqueta (roles_conocidos()
    lee de esa misma fila) -- efectivamente lo borraría entero, que no
    es lo que pide el botón "Restaurar valores iniciales". Para un rol
    custom, la acción correspondiente es Borrar rol (y crearlo de nuevo
    si hace falta) -- ver borrar_rol().
    """
    from production.api.deps import PROD_DB_WRITE_LOCK, get_conn

    if rol not in PERMISOS_INICIALES:
        if rol in roles_conocidos():
            raise ValueError(
                "Los roles creados desde el panel no tienen valores de fábrica a los que "
                "volver -- para empezar de nuevo, borrá el rol y crealo otra vez.")
        raise ValueError(f"Rol desconocido: {rol}")
    conn = get_conn()
    try:
        _ensure_tabla(conn)
        with PROD_DB_WRITE_LOCK:
            conn.execute("DELETE FROM ftr_permisos_rol WHERE rol=?", (rol,))
            conn.commit()
    finally:
        conn.close()
    invalidar_cache()
    logging.getLogger(__name__).info(
        f"[Permisos] {por} restauró los valores iniciales del rol '{rol}'")
    return set(PERMISOS_INICIALES[rol]) | PERMISOS_IRRENUNCIABLES.get(rol, set())


def version_matriz() -> str:
    """
    Huella corta de la matriz vigente. El frontend la compara contra la
    que tenía: si cambió, la página se recarga sola. Sin esto, a quien le
    revocan un permiso le seguirían apareciendo los tabs viejos hasta que
    recargara a mano, y cada click le daría un 403 inexplicable.
    """
    m = matriz_actual()
    crudo = ";".join(f"{rol}:{','.join(sorted(ps))}" for rol, ps in sorted(m.items()))
    return hashlib.sha256(crudo.encode()).hexdigest()[:12]


def permisos_de(rol: str) -> set[str]:
    """Permisos de un rol. Un rol desconocido no tiene ninguno: si algún
    día aparece un rol nuevo sin definir, la falla es hacia el lado
    cerrado, no hacia el abierto."""
    return set(matriz_actual().get(rol or "", set()))


def tiene_permiso(rol: str, permiso: Optional[str]) -> bool:
    if permiso is None:
        return True
    return permiso in permisos_de(rol)


def puede_ver_tab(rol: str, tab: str) -> bool:
    """Un tab sin permiso declarado se muestra: los tabs son la lista de
    arriba, y uno nuevo no debería quedar invisible sin explicación."""
    return tiene_permiso(rol, PERMISO_POR_TAB.get(tab))


def tabs_de(rol: str) -> list[str]:
    return [t for t in PERMISO_POR_TAB if puede_ver_tab(rol, t)]


def contexto_de_plantilla(rol: str) -> dict:
    """Lo que necesitan las plantillas Jinja para dibujar solo lo permitido."""
    permisos = permisos_de(rol)
    return {
        "rol": rol,
        "permisos": permisos,
        "puede": lambda p: p in permisos,      # {% if puede('ver_valor') %}
        "tabs_permitidos": tabs_de(rol),
        "es_admin": rol == "admin",
    }


def resumen_para_panel() -> dict:
    """Todo lo que el panel necesita para dibujar y editar la matriz."""
    matriz = matriz_actual()
    etiquetas = roles_conocidos()
    # Etiqueta del grupo al que pertenece cada pestaña. El panel las
    # agrupa visualmente con esto: son 14 pestañas, y una lista plana de
    # 14 casillas sin encabezados es peor de leer que las 6 de antes.
    etiqueta_grupo = {t: ETIQUETAS.get(g, g) for t, g in GRUPO_POR_TAB.items()}
    perm_a_tab = {p: t for t, p in PERMISOS_TAB.items()}
    return {
        "permisos": [
            {
                "clave": p,
                "etiqueta": ETIQUETAS.get(p, p),
                # Ya no hace falta listar "qué tabs destraba": ahora cada
                # casilla ES una tab. Se deja vacío para no romper el
                # panel viejo si quedó cacheado.
                "tabs": [],
                # `configurar_unidad` no es una pestaña ni una acción: es
                # una preferencia propia. Se le da su propio encabezado en
                # vez de dejarlo caer en un "Otros" que no explica nada.
                "grupo": etiqueta_grupo.get(perm_a_tab.get(p),
                                            "Preferencias personales"),
                "es_accion": p in ACCIONES,
            }
            # EDITABLES, no TODOS_LOS_PERMISOS: los permisos de vista de
            # grupo son derivados y no se ofrecen para tildar.
            for p in EDITABLES
        ],
        "roles": {
            rol: {
                # Se filtra a lo editable antes de mandarlo: si el panel
                # recibiera los derivados los mandaría de vuelta al
                # guardar, y la fila quedaría con permisos de grupo otra
                # vez. Van y vienen solo permisos finos.
                "permisos": sorted(set(matriz.get(rol, set())) & set(EDITABLES)),
                "fijos": sorted(PERMISOS_IRRENUNCIABLES.get(rol, set())),
                "iniciales": sorted(set(PERMISOS_INICIALES.get(rol, set()))
                                    & set(EDITABLES)),
                "etiqueta": etiquetas.get(rol, rol),
                # El panel usa esto para saber si mostrar "Restaurar
                # valores iniciales" (solo protegidos) o "Borrar rol"
                # (solo NO protegidos) -- ver restaurar_iniciales/
                # borrar_rol para el porqué de la distinción.
                "protegido": rol in ROLES_PROTEGIDOS,
            }
            for rol in etiquetas
        },
        "version": version_matriz(),
    }
