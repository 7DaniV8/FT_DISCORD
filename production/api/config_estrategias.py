"""
production/api/config_estrategias.py — Configuración de estrategias,
editable desde el Admin (01/08/2026, a pedido de Maria: pestaña
"Estrategias" para no tener que tocar variables de entorno en Railway
cada vez que quiere ajustar un umbral).

DISEÑO: catálogo CERRADO (REGISTRO más abajo) -- el panel solo puede
editar claves que algún código del proyecto realmente lee. Guardar
cualquier string suelto sin que nada lo consuma sería un ajuste
fantasma: se ve guardado en el panel, pero no cambia nada. Agregar un
parámetro nuevo acá adentro es agregar una entrada al diccionario +
hacer que el código relevante llame a obtener_bool/obtener_float en vez
de os.getenv directo -- no hace falta tocar el panel ni los endpoints,
leen el registro solos.

PRIORIDAD DE LECTURA: 1) lo guardado en la base (panel), 2) variable de
entorno (por si ya había algo seteado en Railway antes de que existiera
este mecanismo), 3) default del registro. Así ningún despliegue viejo
se rompe el día que se agrega esto -- el comportamiento no cambia hasta
que alguien lo toca desde el panel.
"""
from __future__ import annotations

import logging
import os
import threading as _threading
import time as _time
from datetime import datetime, timezone

log = logging.getLogger(__name__)

_TABLA_LISTA = False

# Catálogo de parámetros conocidos. tipo: 'bool' | 'float' | 'int'.
REGISTRO: dict[str, dict] = {
    # FT PROBABILIDAD V16 (30/10/2026). ÚNICO interruptor del servicio externo.
    # No existe ninguna tarea en RankingFTR que dependa de esta clave;
    # la lee FT_PROBABILIDAD por /ft-intel/probabilidad/config cada 60 segundos.
    "FT_PROBABILIDAD_ACTIVO": {
        "tipo": "bool", "default": "0", "categoria": "FT PROBABILIDAD",
        "etiqueta": "FT PROBABILIDAD — encender motor independiente",
        "descripcion": (
            "APAGADO por defecto. Encendido: el servicio FT_PROBABILIDAD de Railway "
            "puede consultar los fixtures/UTR en modo lectura y procesar Tennis Data con "
            "presupuesto y caché propios. Apagado: detiene nuevos trabajos y consultas "
            "externas (máximo 60 segundos para propagarse). RankingFTR no hace cálculos "
            "nuevos ni cambia sus estrategias. Los pesos de cada componente se editan en "
            "los tres pesos de abajo (de fábrica 60% Tennis Data, 30% cuotas, 10% UTR). "
            "Resultado experimental, no calibrado."
        ),
    },
    # Pesos del cálculo combinado (30/10/2026, Daniel: «que en el ranking le podamos cambiar el peso a las
    # variables desde el Admin»). Tres campos, en % (de fábrica 60/30/10). NO hace falta que sumen 100: si no
    # suman 100 se reparten EN PROPORCIÓN (70/30/15 -> 61,0/26,1/13,0) y la pestaña muestra los pesos reales
    # en uso. FT_PROBABILIDAD solo adopta unos pesos nuevos cuando los ve iguales en dos lecturas seguidas
    # (≈1-2 min), así editar los tres campos uno a uno no deja pesos a medias.
    "FT_PROB_PESO_TENNIS_DATA": {
        "tipo": "float", "default": "60", "categoria": "FT PROBABILIDAD",
        "etiqueta": "Peso de Tennis Data (%)",
        "descripcion": (
            "Cuánto pesa la estimación de las estadísticas de Tennis Data en el cálculo combinado (0 a 100). "
            "Los tres pesos (Tennis Data, Cuota, UTR) se reparten en proporción: lo ideal es que sumen 100, "
            "pero si no suman 100 se ajustan solos. El servicio toma el cambio en 1-2 minutos. De fábrica: 60. "
            "OJO: el aprendizaje histórico se entrena con 60/30/10; con otros pesos, un modelo ya validado no se usa."
        ),
    },
    "FT_PROB_PESO_CUOTA": {
        "tipo": "float", "default": "30", "categoria": "FT PROBABILIDAD",
        "etiqueta": "Peso de la cuota sin margen (%)",
        "descripcion": (
            "Cuánto pesa la probabilidad implícita de la cuota (sin margen) en el cálculo combinado (0 a 100). "
            "Se reparte en proporción con los otros dos pesos. De fábrica: 30."
        ),
    },
    "FT_PROB_PESO_UTR": {
        "tipo": "float", "default": "10", "categoria": "FT PROBABILIDAD",
        "etiqueta": "Peso del UTR (%)",
        "descripcion": (
            "Cuánto pesa la estimación que sale de la diferencia de UTR en el cálculo combinado (0 a 100). "
            "Se reparte en proporción con los otros dos pesos. De fábrica: 10."
        ),
    },
    # 🎲 Aviso a Discord de FT PROBABILIDAD (30/10/2026). El encendido, el texto y la voz de cada aviso están en
    # Admin → Discord (tipos «FT Probabilidad · candidato detectado / al empezar»); aquí, SOLO la regla.
    "FT_PROB_CAND_PROB_MIN": {
        "tipo": "float", "default": "60", "categoria": "FT PROBABILIDAD",
        "etiqueta": "Candidato: probabilidad mínima (%)",
        "descripcion": (
            "Un jugador es CANDIDATO cuando la probabilidad del modelo es MAYOR que este número (50 a 99) y su cuota actual "
            "es mayor que la cuota mínima. De fábrica: 60. Es una marca de observación: el modelo aún no está calibrado."
        ),
    },
    "FT_PROB_CAND_CUOTA_MIN": {
        "tipo": "float", "default": "1.70", "categoria": "FT PROBABILIDAD",
        "etiqueta": "Candidato: cuota mínima",
        "descripcion": (
            "La cuota ACTUAL del jugador (la de ahora, no la de cuando se calculó) debe ser MAYOR que este número "
            "(1.01 a 20). De fábrica: 1.70."
        ),
    },
    "FT_PROB_CAND_MIN_ANTES": {
        "tipo": "int", "default": "10", "categoria": "FT PROBABILIDAD",
        "etiqueta": "Candidato: minutos antes para el segundo aviso",
        "descripcion": (
            "El primer aviso sale al detectar el candidato; el segundo, cuando faltan estos minutos o menos para el "
            "inicio (1 a 180), y solo si con la cuota de ese momento todavía cumple. De fábrica: 10."
        ),
    },

    # ═════════════════════════════════════════════════════════════════
    #  CLAVES PROPIAS POR ESTRATEGIA (20/08/2026)
    # ═════════════════════════════════════════════════════════════════
    #
    # PEDIDO DE RUBÉN: "una sección por estrategia, con todos sus
    # números, que no compartan ningún número, que si quiero subir solo
    # un número en una estrategia, que solo modifique esa estrategia".
    #
    # POR QUÉ HACÍA FALTA, con un caso real. El postSet 1 de M. Petkovic
    # vs C. Giraldi (20/08/2026) no sonó y costó una tarde entera
    # entender por qué. Parte del problema fue que los umbrales de Elo y
    # FTR que gobiernan Partidos en Vivo NO estaban en su sección: se
    # leían de FTR_ELO_UMBRAL_*, que el panel muestra bajo "Valor
    # Pre-Partido". Mirar la sección correcta daba la respuesta
    # equivocada.
    #
    # CADA CLAVE NUEVA ARRANCA CON EL VALOR QUE HOY TIENE LA CLAVE
    # COMPARTIDA -- ver MIGRACIONES_COPIA al final del archivo. Sin eso,
    # separar claves cambiaría en silencio lo que se apuesta: una clave
    # nueva saldría con su default y nadie se enteraría. Los defaults de
    # acá abajo son solo la red por si la migración no encuentra origen.

    # ── Partidos en Vivo (postSet 1) ─────────────────────────────────
    "VIVO_ELO_MIN": {
        "tipo": "float", "default": "0.65", "categoria": "Partidos en Vivo (postSet 1)",
        "etiqueta": "Prob. Elo mínima del favorito",
        "descripcion": (
            "El favorito pre-partido que perdió el Set 1 necesita AL MENOS esta Prob. Elo "
            "para que la alerta suene. Hasta el 20/08/2026 este número se leía de la "
            "sección Valor Pre-Partido (FTR_ELO_UMBRAL_ELO) y no se veía acá -- por eso no "
            "se entendía qué umbral regía en vivo. Ahora es propio: cambiarlo NO toca "
            "Valor Pre-Partido."
        ),
    },
    "VIVO_FTR_MIN": {
        "tipo": "float", "default": "0.55", "categoria": "Partidos en Vivo (postSet 1)",
        "etiqueta": "Prob. FTR mínima del favorito",
        "descripcion": (
            "El favorito pre-partido que perdió el Set 1 necesita AL MENOS esta Prob. FTR "
            "para que la alerta suene. Se exige junto con la Prob. Elo mínima: las dos, no "
            "una u otra. "
            "OJO (medición del 20/08/2026): la columna Prob. FTR tiene AUC 0,455 -- o sea "
            "que no separa ganadores de perdedores mejor que el azar -- y para el favorito "
            "nunca cae por debajo de 0,50, así que cualquier valor menor a 0,50 acá no "
            "descarta ni un partido: el corte queda inerte y el que filtra de verdad es el "
            "de Elo. Por encima de 0,50 sí corta. No subirlo sin auditar la columna primero: "
            "con ese AUC, cortar más fuerte no implica cortar mejor. "
            "25/08/2026, a pedido de Maria: junto con VIVO_FTR_MAX (acá abajo) arman una "
            "BANDA -- el favorito necesita Prob. FTR por encima de este piso Y por debajo "
            "del techo. Antes esto era solo un piso, sin techo."
        ),
    },
    "VIVO_FTR_MAX": {
        "tipo": "float", "default": "0.75", "categoria": "Partidos en Vivo (postSet 1)",
        "etiqueta": "Prob. FTR máxima del favorito",
        "descripcion": (
            "25/08/2026, a pedido de Maria: mismo pedido que ya hizo para Valor Pre-Partido "
            "(\"que FTR sea menor a 75 y mayor a 50, editable\"), extendido acá. Techo de la "
            "banda de Prob. FTR -- junto con VIVO_FTR_MIN (arriba) arman el rango completo. "
            "Un favorito con Prob. FTR por encima de este número (diferencia demasiado "
            "grande entre los dos jugadores) NO cuenta como la alerta que esta estrategia "
            "busca. Se usa en el detector real, en Ventaja leve (Grupo 1) y en la pestaña "
            "'EN VIVO — DIAGNÓSTICO' del panel LIVE -- los tres leen el mismo número, así "
            "que no pueden desincronizarse."
        ),
    },
    "VIVO_LEVE_GRUPO1_ACTIVO": {
        "tipo": "bool", "default": "0", "categoria": "Partidos en Vivo (postSet 1)",
        "etiqueta": "Ventaja leve: Grupo 1 activo (⚠ duplica con 'Pasan el filtro' si se prende)",
        "descripcion": (
            "⚠️ DEFAULT CAMBIADO A APAGADO EL 30/08/2026, a pedido de Maria: 'un partido que "
            "pertenece a Pasan el filtro no puede pertenecer a Ventaja leve'. "
            "El Grupo 1 usa EXACTAMENTE los mismos umbrales que 'Pasan el filtro' "
            "(VIVO_FTR_MIN/MAX, VIVO_ELO_MIN) -- no es una señal parecida, es LA MISMA señal, "
            "sin exigir que además haya cuota en vivo disponible en ese instante. Por eso "
            "cualquier intento de arreglar el orden de llegada entre las dos (quién detecta "
            "primero) queda incompleto: si 'Pasan el filtro' tarda en confirmar (cuota no "
            "disponible todavía) pero Grupo 1 sí califica en ese momento, Grupo 1 guarda su "
            "fila y quedan DOS filas del mismo partido -- pasó de verdad (caso real: Gabriele "
            "Piraino vs Dusan Obradovic, 30/08/2026, mismo FTR 58,3% y Elo 79,1% en las dos "
            "filas). Apagado, Ventaja leve se queda solo con el Grupo 2, que exige matchup "
            "calculado -- una condición que 'Pasan el filtro' NO mira, así que ahí sí son dos "
            "señales genuinamente distintas y el solapamiento no puede pasar. "
            "28/08/2026, a pedido de Ruben (motivo original de este interruptor, antes de "
            "apagarlo por default): con FTR_VIVO_MATCHUP_MIN en 0 el matchup no se exige en "
            "'Pasan el filtro', y entonces ese corte y el Grupo 1 de Ventaja leve piden "
            "EXACTAMENTE lo mismo. La alternativa a mantenerlo apagado es subir "
            "FTR_VIVO_MATCHUP_MIN (que vuelve a separar los dos cortes) -- pero eso también "
            "le agrega el requisito de estilo a 'Pasan el filtro', cambiando qué suena ahí. "
            "PRENDER ESTO A PROPÓSITO trae de vuelta la duplicación explicada arriba -- no es "
            "un efecto secundario raro, es la consecuencia directa y esperada."
        ),
    },
    "VIVO_MUESTRA_EXIGIR": {
        "tipo": "bool", "default": "1", "categoria": "Partidos en Vivo (postSet 1)",
        "etiqueta": "Exigir muestra suficiente en los dos jugadores",
        "descripcion": (
            "Prendido (default): los DOS jugadores necesitan la muestra mínima de partidos "
            "EN LA SUPERFICIE del partido para que la alerta suene (columna "
            "muestra_suficiente del fixture). Apagarlo deja pasar cruces con poca historia "
            "-- típico en ITF, donde muchos jugadores no llegan a la muestra. La cantidad "
            "de partidos se configura en Valor Pre-Partido, porque es quien la calcula al "
            "reportar el fixture."
        ),
    },
    "VIVO_DESTINO_NO_PASA": {
        "tipo": "str", "default": "ninguno", "categoria": "Partidos en Vivo (postSet 1)",
        "etiqueta": "A dónde llega si NO pasa",
        "opciones": [
            {"valor": "ninguno", "etiqueta": "Ninguno -- no se guarda (como hoy)"},
            {"valor": "sugerencias", "etiqueta": "💡 Sugerencias"},
            {"valor": "descartes", "etiqueta": "🗑️ Descartes"},
        ],
        "descripcion": (
            "20/08/2026, a pedido de Maria. HOY (default 'ninguno'): un partido que no pasa "
            "no se guarda en ningún lado, solo queda el log y lo que muestra 'LIVE 🔴' → "
            "EN VIVO — DIAGNÓSTICO (que se recalcula, no es un guardado). 'sugerencias': "
            "cada partido que no suena queda una fila en 💡 Sugerencias -- OJO, en partidos "
            "en vivo esto puede ser MUCHO volumen (decenas por día). 'descartes': va a la "
            "pestaña 🗑️ Descartes en cambio -- mismo volumen, pero separado de Sugerencias."
        ),
    },
    # 24/08/2026, a pedido de Maria: "guardar cuando un ventaja leve
    # pierda el primer set, en sugerencias, pero que no me notifique".
    # Deliberadamente SIN opción "ninguno" a diferencia de
    # VIVO_DESTINO_NO_PASA de arriba: el pedido fue guardar, no un
    # comportamiento por default de no guardar nada -- si algún día se
    # quiere apagar del todo, es una decisión que amerita volver a
    # tocar este archivo, no un desplegable que puede quedar mal
    # puesto sin querer. Tampoco hay opción de notificar: eso no pasa
    # por acá, está fijo en el código (ver _detectar_ventaja_leve_
    # pierde_set1 en routes_public.py) -- este desplegable solo decide
    # DÓNDE se guarda, nunca si avisa.
    "VIVO_LEVE_DESTINO": {
        "tipo": "str", "default": "sugerencias", "categoria": "Partidos en Vivo (postSet 1)",
        "etiqueta": "Ventaja leve pierde Set 1: a dónde se guarda",
        "opciones": [
            {"valor": "sugerencias", "etiqueta": "💡 Sugerencias (default)"},
            {"valor": "descartes", "etiqueta": "🗑️ Descartes"},
        ],
        "descripcion": (
            "Cuando un partido que está en 'Ventaja leve' (grupo 1 o 2, ver el texto largo "
            "de esta categoría) pierde el Set 1, se guarda una fila acá -- NUNCA se notifica, "
            "sin importar el destino elegido. Es puro registro para medir el patrón antes de "
            "decidir si algún día amerita una alerta real. Se guardan TODOS los que califiquen "
            "para Ventaja leve, incluso los que además disparan la alerta fuerte de arriba -- "
            "las dos cosas son independientes."
        ),
    },

    # ── Valor Pre-Partido ────────────────────────────────────────────
    "VPRE_ELO_MIN": {
        "tipo": "float", "default": "0.65", "categoria": "Valor Pre-Partido",
        "etiqueta": "Prob. Elo mínima",
        "descripcion": "El favorito necesita más de esta Prob. Elo para que la alerta cuente como válida.",
    },
    "VPRE_FTR_MIN": {
        "tipo": "float", "default": "0.55", "categoria": "Valor Pre-Partido",
        "etiqueta": "Prob. FTR mínima",
        "descripcion": (
            "Lo mismo, con el modelo FTR. 25/08/2026, a pedido de Maria: junto con "
            "VPRE_FTR_MAX (acá abajo) arman una BANDA -- el favorito necesita Prob. FTR "
            "por encima de este piso Y por debajo del techo para que la alerta cuente. "
            "Antes esto era solo un piso, sin techo."
        ),
    },
    "VPRE_FTR_MAX": {
        "tipo": "float", "default": "0.75", "categoria": "Valor Pre-Partido",
        "etiqueta": "Prob. FTR máxima",
        "descripcion": (
            "25/08/2026, a pedido de Maria: \"que sea menor a 75 y mayor a 50\". Techo de la "
            "banda de Prob. FTR -- junto con VPRE_FTR_MIN (arriba) arman el rango completo. "
            "Un favorito con Prob. FTR por encima de este número (una diferencia demasiado "
            "grande entre los dos jugadores) NO cuenta como valor -- mismo criterio que ya "
            "usaba la banda de cuota (VPRE_CUOTA_MIN/VPRE_CUOTA_MAX): valor es una zona, no "
            "\"cuanto más, mejor\"."
        ),
    },
    "VPRE_MATCHUP_MIN": {
        "tipo": "float", "default": "0.50", "categoria": "Valor Pre-Partido",
        "etiqueta": "Prob. matchup mínima",
        "descripcion": (
            "El matchup de estilo del favorito tiene que llegar a este valor. Por debajo "
            "-- descontando la banda neutral de abajo -- el estilo del rival bloquea la "
            "alerta. Antes se leía de FTR_ALERTA_MATCHUP_MIN, compartido con Valor FTR y "
            "Movimiento Rentable; ahora es propio."
        ),
    },
    "VPRE_MATCHUP_BANDA": {
        "tipo": "float", "default": "1.5", "categoria": "Valor Pre-Partido",
        "etiqueta": "Banda neutral del matchup (puntos porcentuales)",
        "descripcion": "Dentro de esta banda alrededor del mínimo, el matchup no opina -- deciden los otros filtros.",
    },
    "VPRE_MATCHUP_ESPERA_MIN": {
        "tipo": "int", "default": "25", "categoria": "Valor Pre-Partido",
        "etiqueta": "Minutos de espera del matchup antes de resolver igual",
        "descripcion": (
            "Cuánto se retiene una alerta que ya pasó FTR y Elo pero todavía no tiene el "
            "matchup calculado (el worker tarda ~20s, a veces más si hay cola). Pasado "
            "este tiempo se resuelve con la política de abajo."
        ),
    },
    "VPRE_MATCHUP_SIN_DATO": {
        "tipo": "str", "default": "alertar", "categoria": "Valor Pre-Partido",
        "etiqueta": "Sin matchup calculable: alertar | descartar",
        "descripcion": (
            "Qué hacer cuando el matchup NO se puede calcular (falta perfil de estilo de "
            "alguno de los dos) o venció la espera. 'alertar' (default): suena con los dos "
            "filtros que sí se pudieron aplicar."
        ),
    },
    "VPRE_CUOTA_MIN": {
        "tipo": "float", "default": "1.70", "categoria": "Valor Pre-Partido",
        "etiqueta": "Cuota mínima de la banda",
        "descripcion": "Piso de la banda de cuota PRE-PARTIDO para alertar. Solo de esta estrategia.",
    },
    "VPRE_CUOTA_MAX": {
        "tipo": "float", "default": "2.20", "categoria": "Valor Pre-Partido",
        "etiqueta": "Cuota máxima de la banda",
        "descripcion": "Techo de la banda de cuota pre-partido. Solo de esta estrategia.",
    },
    "VPRE_MUESTRA_FTR": {
        "tipo": "int", "default": "30", "categoria": "Valor Pre-Partido",
        "etiqueta": "Partidos mínimos, motor FTR (por superficie)",
        "descripcion": (
            "Partidos EN LA SUPERFICIE que necesita cada jugador en el rating FTR para que "
            "el motor calcule probabilidad. Es el número que decide muestra_suficiente, "
            "que a su vez usa la alerta postSet 1 de Partidos en Vivo."
        ),
    },
    "VPRE_MUESTRA_ELO": {
        "tipo": "int", "default": "30", "categoria": "Valor Pre-Partido",
        "etiqueta": "Partidos mínimos, motor Elo (research Elo, por superficie)",
        "descripcion": "Lo mismo, para el rating de research Elo.",
    },
    "VPRE_DESTINO_NO_PASA": {
        "tipo": "str", "default": "ninguno", "categoria": "Valor Pre-Partido",
        "etiqueta": "A dónde llega si NO pasa",
        "opciones": [
            {"valor": "ninguno", "etiqueta": "Ninguno -- no se guarda (como hoy)"},
            {"valor": "sugerencias", "etiqueta": "💡 Sugerencias"},
            {"valor": "descartes", "etiqueta": "🗑️ Descartes"},
        ],
        "descripcion": (
            "20/08/2026, a pedido de Maria. CORRECCIÓN IMPORTANTE sobre lo que decía acá "
            "antes: el motor que de verdad gobierna esta pestaña es 'FTR + Elo' (el motor "
            "viejo solo-Elo está apagado desde el 31/07/2026, ver MOTOR VIEJO APAGADO en "
            "el código) -- y desde el 27/07/2026 esta estrategia YA NO genera Descartes: "
            "si no pasa, HOY (default 'ninguno') no se guarda en ningún lado, ni la "
            "resolución del matchup pendiente ('bloqueada') ni el filtro FTR/Elo/muestra "
            "de la apertura. 'sugerencias'/'descartes': a partir de ahora, si lo elegís, "
            "sí queda una fila guardada en esa pestaña. Si NO pasa el filtro determinístico "
            "de FTR/Elo/muestra en el momento de la apertura, tampoco se re-evalúa más "
            "tarde (la regla es 'solo la apertura decide') -- ese fixture ya no vuelve a "
            "pasar por acá."
        ),
    },

    # ── Valor FTR ────────────────────────────────────────────────────
    "VFTR_ELO_MIN": {
        "tipo": "float", "default": "0.65", "categoria": "Valor FTR",
        "etiqueta": "Prob. Elo mínima",
        "descripcion": "Umbral propio de Valor FTR. Antes compartido con Movimiento Rentable vía cumple_filtros_alerta().",
    },
    "VFTR_FTR_MIN": {
        "tipo": "float", "default": "0.55", "categoria": "Valor FTR",
        "etiqueta": "Prob. FTR mínima",
        "descripcion": "Umbral propio de Valor FTR.",
    },
    "VFTR_FTR_MAX": {
        "tipo": "float", "default": "1.0", "categoria": "Valor FTR",
        "etiqueta": "Prob. FTR maxima del recomendado",
        "descripcion": (
            "28/08/2026, a pedido de Ruben: que en todas las estrategias se pueda poner el rango del valor FTR, no solo el piso. Techo de la banda -- junto con VFTR_FTR_MIN arman el rango completo. Un favorito con Prob. FTR POR ENCIMA de este numero (diferencia demasiado grande entre los dos jugadores) deja de contar, igual que uno por debajo del piso. Mismo criterio que VPRE_FTR_MAX y VIVO_FTR_MAX, que ya lo tenian. DEFAULT 1.0 = el techo no corta nada, o sea el comportamiento de antes de este cambio: hay que bajarlo a mano para que empiece a filtrar."
        ),
    },
    "VFTR_MATCHUP_MIN": {
        "tipo": "float", "default": "0.50", "categoria": "Valor FTR",
        "etiqueta": "Prob. matchup mínima",
        "descripcion": "Por debajo de este valor menos la banda neutral, el matchup de estilo bloquea la alerta.",
    },
    "VFTR_MATCHUP_BANDA": {
        "tipo": "float", "default": "1.5", "categoria": "Valor FTR",
        "etiqueta": "Banda neutral del matchup (puntos porcentuales)",
        "descripcion": "Dentro de esta banda alrededor del mínimo, el matchup no opina.",
    },
    "VFTR_MATCHUP_GATE": {
        "tipo": "str", "default": "disponible", "categoria": "Valor FTR",
        "etiqueta": "Política del matchup: disponible | estricto",
        "descripcion": (
            "'disponible' (default): si el diagnóstico de estilo todavía no está calculado, "
            "la alerta pasa igual. 'estricto': sin diagnóstico no hay alerta -- silencia "
            "muchas alertas porque el cálculo en vivo puede tardar ~19s."
        ),
    },
    "VFTR_CUOTA_MIN": {
        "tipo": "float", "default": "1.70", "categoria": "Valor FTR",
        "etiqueta": "Cuota mínima de la banda",
        "descripcion": "Piso de la banda de cuota de Valor FTR. Propio: mover esto no toca las otras estrategias.",
    },
    "VFTR_CUOTA_MAX": {
        "tipo": "float", "default": "2.20", "categoria": "Valor FTR",
        "etiqueta": "Cuota máxima de la banda",
        "descripcion": "Techo de la banda de cuota de Valor FTR.",
    },
    "VFTR_SUPERFICIE_DESCONOCIDA": {
        "tipo": "str", "default": "general", "categoria": "Valor FTR",
        "etiqueta": "Sin superficie conocida: general | descartar",
        "descripcion": (
            "Qué hacer cuando no se sabe en qué superficie se juega (pasa sobre todo en "
            "ITF chicos). 'general': se evalúa con el rating general. 'descartar': sin "
            "superficie no se manda la alerta."
        ),
    },
    "VFTR_DESTINO_NO_PASA": {
        "tipo": "str", "default": "propia_pestana", "categoria": "Valor FTR",
        "etiqueta": "A dónde llega si NO pasa",
        "opciones": [
            {"valor": "propia_pestana", "etiqueta": "Solo su pestaña -- no se copia a ningún lado (como hoy)"},
            {"valor": "sugerencias", "etiqueta": "💡 Sugerencias"},
            {"valor": "descartes", "etiqueta": "🗑️ Descartes"},
        ],
        "descripcion": (
            "20/08/2026, a pedido de Maria. HOY (default 'propia_pestana'): TODO lo que "
            "manda el forwarder ya se guarda en la pestaña 'Valor FTR' pase o no pase los "
            "cuatro filtros (regla de Maria del 21/07: 'absolutamente todas deben sonar en "
            "Admin') -- este desplegable decide si ADEMÁS se copia a otro lado cuando NO "
            "pasa. 'sugerencias'/'descartes': además de quedar en Valor FTR, se copia a esa "
            "pestaña."
        ),
    },

    # ── Favorito en Vivo ─────────────────────────────────────────────
    "FAV_ELO_MIN": {
        "tipo": "float", "default": "0.65", "categoria": "Favorito en Vivo",
        "etiqueta": "Prob. Elo mínima",
        "descripcion": (
            "Umbral propio de Favorito en Vivo. Hasta el 20/08/2026 esta estrategia usaba "
            "en silencio los umbrales de Valor FTR (FTR_ALERTA_*) y no aparecía ningún "
            "número en su sección del panel."
        ),
    },
    "FAV_FTR_MIN": {
        "tipo": "float", "default": "0.55", "categoria": "Favorito en Vivo",
        "etiqueta": "Prob. FTR mínima",
        "descripcion": "Umbral propio de Favorito en Vivo.",
    },
    "FAV_FTR_MAX": {
        "tipo": "float", "default": "1.0", "categoria": "Favorito en Vivo",
        "etiqueta": "Prob. FTR maxima del favorito",
        "descripcion": (
            "28/08/2026, a pedido de Ruben: que en todas las estrategias se pueda poner el rango del valor FTR, no solo el piso. Techo de la banda -- junto con FAV_FTR_MIN arman el rango completo. Un favorito con Prob. FTR POR ENCIMA de este numero (diferencia demasiado grande entre los dos jugadores) deja de contar, igual que uno por debajo del piso. Mismo criterio que VPRE_FTR_MAX y VIVO_FTR_MAX, que ya lo tenian. DEFAULT 1.0 = el techo no corta nada, o sea el comportamiento de antes de este cambio: hay que bajarlo a mano para que empiece a filtrar."
        ),
    },
    "FAV_MATCHUP_MIN": {
        "tipo": "float", "default": "0.50", "categoria": "Favorito en Vivo",
        "etiqueta": "Prob. matchup mínima",
        "descripcion": "Umbral propio de Favorito en Vivo.",
    },

    # ── Mensajes: MTO (Reenvío) ──────────────────────────────────────
    "MSG_CAB_mto": {
        "tipo": "texto", "default": "", "categoria": "Mensajes — encabezados",
        "etiqueta": "MTO (Reenvío) — encabezado",
        "descripcion": (
            "Encabezado que Reenvio_MTO pone ARRIBA del aviso de MTO reenviado "
            "(20/08/2026, pedido de Rubén: 'que también se pueda editar el mensaje de "
            "MTO'). Vacío = se reenvía el texto del canal origen tal cual, como hasta "
            "ahora. El cuerpo del aviso (jugadores, marcador, cuotas) viene del canal "
            "origen y no se puede editar desde acá."
        ),
    },
    "MSG_FRASE_mto": {
        "tipo": "texto", "default": "", "categoria": "Mensajes — frases",
        "etiqueta": "MTO (Reenvío) — frase al pie",
        "descripcion": "Frase que se agrega al final del aviso de MTO. Vacía = no se agrega nada.",
    },
    "FTR_VIVO_EXIGIR_CUOTA": {
        "tipo": "bool", "default": "1", "categoria": "Partidos en Vivo (postSet 1)",
        "etiqueta": "Exigir cuota en vivo confirmada",
        "descripcion": (
            "Prendido (default): el favorito que perdió el Set 1 necesita una cuota "
            "en vivo de Pinnacle por encima del piso de abajo para que suene la alerta. "
            "En Challengers/ITF, donde Pinnacle no siempre cotiza en vivo, esto puede "
            "silenciar partidos que sí cumplen las demás condiciones (FTR/Elo/perdió el "
            "set) -- caso real: Mika Petkovic vs Nikolai Barsukov, 01/08/2026. Apagalo "
            "para que suene igual cuando no haya cuota disponible."
        ),
    },
    # ── MATCHUP EN LA ALERTA POST SET 1 (13/08/2026, pedido de Rubén:
    # "que todos los postset cumplan con el 50% del MatchUp para poder
    # sonar"). Hasta ahora esta estrategia no miraba el matchup: cortaba
    # por muestra, las dos probabilidades, que el favorito hubiera
    # perdido el Set 1, y la cuota en vivo.
    "FTR_VIVO_MATCHUP_MIN": {
        "tipo": "float", "default": "0.50", "categoria": "Partidos en Vivo (postSet 1)",
        "etiqueta": "Prob. matchup mínima del favorito",
        "descripcion": (
            "El matchup de estilo del favorito -- el que perdió el Set 1 y es el que se "
            "juega -- tiene que llegar a este valor para que la alerta suene. En 0 se apaga "
            "el filtro y la estrategia vuelve a como estaba antes del 13/08/2026."
        ),
    },
    # Pestaña "MatchUp" del panel LIVE (15/08/2026, pedido de
    # Rubén). Es un corte de VISTA, no de alerta: no cambia a quién le
    # suena nada, solo qué partidos se listan en esa pestaña. Por eso
    # convive con FTR_VIVO_MATCHUP_MIN sin pisarlo.
    "FTR_VIVO_MATCHUP_TAB_MIN": {
        "tipo": "float", "default": "0.53", "categoria": "Panel LIVE (solo la vista, no alerta)",
        "etiqueta": "MatchUp: corte de la pestaña del panel LIVE",
        "descripcion": (
            "Matchup mínimo, en la superficie del partido, para que un partido en vivo "
            "aparezca en la pestaña MatchUp del panel LIVE. El rótulo de la pestaña sigue "
            "a este valor: en 0.53 dice \"MatchUp +53\". Se mide sobre el jugador "
            "con ventaja de estilo, no sobre el favorito de los ratings: el matchup es "
            "simétrico y 55% para uno es 45% para el otro. Es el ÚNICO filtro de esa "
            "pestaña -- no se exige muestra, ni FTR, ni Elo. En 0 lista todos los "
            "partidos que tengan matchup calculado, que sirve para ver la distribución "
            "real antes de elegir el corte."
        ),
    },
    "FTR_VIVO_MATCHUP_SIN_DATO": {
        "tipo": "str", "default": "descartar", "categoria": "Partidos en Vivo (postSet 1)",
        "etiqueta": "Sin matchup calculado: alertar | descartar",
        "descripcion": (
            "Qué hacer cuando el worker de estilo todavía no calculó ese partido. "
            "'descartar' (default) es literal: si no se puede verificar el 50%, no suena. "
            "'alertar' deja pasar la alerta cuando el dato no está, y aplica el filtro solo "
            "cuando sí está. OJO: con la cola del worker larga, 'descartar' puede silenciar "
            "muchas alertas -- el log escribe una línea por cada una, buscar "
            "'[Vivo] ... sin matchup' para medir cuántas son antes de decidir."
        ),
    },
    "FTR_VIVO_CUOTA_MIN": {
        "tipo": "float", "default": "1.70", "categoria": "Partidos en Vivo (postSet 1)",
        "etiqueta": "Cuota en vivo mínima del favorito",
        "descripcion": (
            "Cuota EN VIVO (ya perdido el Set 1) por encima de la cual se considera que "
            "el mercado castigó lo suficiente al favorito como para que valga la pena. "
            "No es la banda pre-partido -- es el precio de ahora."
        ),
    },
    "FTR_VIVO_CUOTA_MAX": {
        "tipo": "float", "default": "2.20", "categoria": "Partidos en Vivo (postSet 1)",
        "etiqueta": "Cuota en vivo máxima del favorito",
        "descripcion": (
            "Techo de la cuota EN VIVO (05/08/2026, a pedido de Maria: la banda pasa a ser "
            "1.70-2.20). Antes solo había piso: un favorito que perdió el Set 1 y pasó a pagar "
            "3.50 entraba igual, cuando a ese precio el mercado ya no lo considera favorito. "
            "Poner 0 (o un número muy alto) deja la banda sin techo, como estaba antes."
        ),
    },
    "FTR_VIVO_CUOTA_VENTANA_MIN": {
        "tipo": "float", "default": "20.0", "categoria": "Partidos en Vivo (postSet 1)",
        "etiqueta": "Ventana para buscar cuota en vivo (minutos)",
        "descripcion": "Cuánto para atrás se busca una cuota en vivo válida antes de darla por no disponible.",
    },
    # VENTANA DEL SET 1 (07/08/2026, reportado por Rubén: "varias han
    # sonado cuando ya van en el punto 6 del set 2"). Ver
    # _ventana_set1_abierta() en routes_public.py.
    "FTR_VIVO_SET1_VENTANA_MIN": {
        "tipo": "float", "default": "10.0", "categoria": "Partidos en Vivo (postSet 1)",
        "etiqueta": "Ventana desde el cierre del Set 1 (minutos)",
        "descripcion": (
            "Cuánto tiempo después de ver cerrar el Set 1 se sigue pudiendo alertar. Aplica "
            "solo cuando presenciamos el cierre; si el partido llegó con el Set 1 ya "
            "terminado, decide el marcador del set en curso (parámetro de abajo). Subirlo da "
            "más margen para que aparezca cuota en vivo, pero deja sonar alertas más tardías."
        ),
    },
    "FTR_VIVO_SET1_MAX_GAMES": {
        "tipo": "int", "default": "3", "categoria": "Partidos en Vivo (postSet 1)",
        "etiqueta": "Máximo de games jugados en el set en curso",
        "descripcion": (
            "Si el set en curso ya lleva más games que esto, no se alerta -- el partido "
            "avanzó demasiado como para que 'acaba de perder el Set 1' signifique algo. Se "
            "lee de `set_actual` (formato '3-2'); si el bot no lo manda legible y el partido "
            "llegó con el Set 1 ya cerrado, la alerta no sale."
        ),
    },

    # Valor Pre-Partido (01/08/2026) -- antes UMBRAL_PROBABILIDAD_FTR/ELO
    # eran números fijos en ftr_elo_engine.py, ni siquiera leían una
    # variable de entorno. Ver evaluar_ftr_elo().
    "FTR_ELO_UMBRAL_FTR": {
        "tipo": "float", "default": "0.55", "categoria": "Obsoletos — ya no deciden nada",
        "etiqueta": "Prob. FTR mínima",
        "descripcion": "El favorito necesita más de esta probabilidad según el modelo FTR para que la alerta cuente como válida.",
    },
    "FTR_ELO_UMBRAL_ELO": {
        "tipo": "float", "default": "0.65", "categoria": "Obsoletos — ya no deciden nada",
        "etiqueta": "Prob. Elo mínima",
        "descripcion": "Lo mismo que arriba, pero con el modelo Elo (research Elo).",
    },
    # MATCHUP PENDIENTE (07/08/2026, decisión de Rubén: "lo más
    # importante es que pase los tres filtros, no importa que no suene
    # tan rápido"). Ver el bloque GATE DE MATCHUP en fixture_reportar.
    "FTR_MATCHUP_ESPERA_MIN": {
        "tipo": "int", "default": "25", "categoria": "Obsoletos — ya no deciden nada",
        "etiqueta": "Minutos de espera del matchup antes de resolver igual",
        "descripcion": (
            "Cuánto se retiene una alerta de Valor Pre-Partido que ya pasó FTR y Elo pero "
            "todavía no tiene el matchup de estilo calculado (el worker tarda ~20s, a veces "
            "más si hay cola). Pasado este tiempo se resuelve con la política de abajo, para "
            "que una alerta no quede colgada para siempre si el cálculo falló."
        ),
    },
    "FTR_MATCHUP_SIN_DATO": {
        "tipo": "str", "default": "alertar", "categoria": "Obsoletos — ya no deciden nada",
        "etiqueta": "Sin matchup calculable: alertar | descartar",
        "descripcion": (
            "Qué hacer cuando el matchup NO se puede calcular (falta perfil de estilo de "
            "alguno de los dos, típico en ITF/Challenger) o cuando venció la espera de "
            "arriba. 'alertar' (default): suena con los dos filtros que sí se pudieron "
            "aplicar. 'descartar': sin los tres filtros no hay alerta -- más estricto, pero "
            "en superficies con poca cobertura de estilo apaga muchas alertas."
        ),
    },

    # Movimiento Rentable / Valor FTR (compartidos) -- cumple_filtros_alerta()
    # en valor_engine.py, los 4 filtros que usan las dos estrategias.
    # 01/08/2026: antes se leían UNA vez al importar el módulo (con
    # os.getenv), así que un cambio en Railway no se aplicaba sin
    # reiniciar el proceso -- ahora se consultan en cada evaluación.
    "FTR_ALERTA_ELO_MIN": {
        "tipo": "float", "default": "0.65", "categoria": "Obsoletos — ya no deciden nada",
        "etiqueta": "Prob. Elo mínima",
        "descripcion": "Compartido entre Movimiento Rentable y Valor FTR -- los dos usan cumple_filtros_alerta().",
    },
    "FTR_ALERTA_FTR_MIN": {
        "tipo": "float", "default": "0.55", "categoria": "Obsoletos — ya no deciden nada",
        "etiqueta": "Prob. FTR mínima",
        "descripcion": "Compartido entre Movimiento Rentable y Valor FTR.",
    },
    "FTR_ALERTA_MATCHUP_MIN": {
        "tipo": "float", "default": "0.50", "categoria": "Obsoletos — ya no deciden nada",
        "etiqueta": "Prob. matchup mínima (referencia)",
        "descripcion": "Por debajo de este valor menos la banda neutral, el matchup de estilo bloquea la alerta.",
    },
    "FTR_ALERTA_MATCHUP_BANDA": {
        "tipo": "float", "default": "1.5", "categoria": "Obsoletos — ya no deciden nada",
        "etiqueta": "Banda neutral del matchup (puntos porcentuales)",
        "descripcion": "Dentro de esta banda alrededor del 50%, el matchup no opina -- deciden los otros filtros.",
    },
    "FTR_ALERTA_CUOTA_MIN": {
        "tipo": "float", "default": "1.70", "categoria": "Obsoletos — ya no deciden nada",
        "etiqueta": "Cuota mínima de la banda",
        "descripcion": "Piso de la banda de cuota aceptada para alertar.",
    },
    "FTR_ALERTA_CUOTA_MAX": {
        "tipo": "float", "default": "2.20", "categoria": "Obsoletos — ya no deciden nada",
        "etiqueta": "Cuota máxima de la banda",
        "descripcion": (
            "Techo de la banda de cuota aceptada para alertar. 05/08/2026: bajado de 2.50 a "
            "2.20 a pedido de Maria. Lo comparten las tres estrategias pre-partido (Valor "
            "Pre-Partido, Movimiento Rentable y Valor FTR), así que cambiarlo acá las mueve "
            "a las tres a la vez."
        ),
    },

    # Movimiento Rentable -- sus propios umbrales de movimiento de
    # mercado (F1-F3, F5), antes fijos en movimiento_engine.py.
    "MOV_APERTURA_MIN": {
        "tipo": "float", "default": "1.85", "categoria": "Obsoletos — ya no deciden nada",
        "etiqueta": "Cuota de apertura mínima (F1)",
        "descripcion": "Sin techo a propósito (18/07/2026) -- lo que importa es dónde termina la cuota, no dónde arrancó.",
    },
    "MOV_ACTUAL_MIN": {
        "tipo": "float", "default": "1.75", "categoria": "Obsoletos — ya no deciden nada",
        "etiqueta": "Cuota actual mínima (F2)",
        "descripcion": "Rango de cuota actual todavía considerado rentable después del movimiento.",
    },
    "MOV_ACTUAL_MAX": {
        "tipo": "float", "default": "2.50", "categoria": "Obsoletos — ya no deciden nada",
        "etiqueta": "Cuota actual máxima (F2)",
        "descripcion": "Techo del rango de cuota actual.",
    },
    "MOV_CAIDA_PCT_MIN": {
        "tipo": "float", "default": "8.0", "categoria": "Obsoletos — ya no deciden nada",
        "etiqueta": "Caída porcentual mínima, % (F3)",
        "descripcion": "Piso de caída relativa de la cuota para que el movimiento cuente como significativo.",
    },
    "MOV_DELTA_PP_MIN": {
        "tipo": "float", "default": "1.8", "categoria": "Obsoletos — ya no deciden nada",
        "etiqueta": "Caída mínima en puntos porcentuales de probabilidad implícita (F4)",
        "descripcion": "Complementa a F3 -- exige un piso absoluto además del relativo, para que una apertura muy alta no pase con una caída chica en términos absolutos.",
    },
    "MOV_PROB_FULLTENIS_MIN": {
        "tipo": "float", "default": "65.0", "categoria": "Obsoletos — ya no deciden nada",
        "etiqueta": "Prob. FullTenis mínima, % (F5)",
        "descripcion": "El modelo propio (Elo, en escala 0-100) tiene que confirmar por encima de esto para que el movimiento pase a Picks.",
    },

    # Partidos en Vivo -- qué estrategias remontada suenan en absoluto
    # (01/08/2026, a pedido de Maria: "las otras 5 [estrategias del bot]
    # ni siquiera debería sonar, vamos a sacarlo"). Antes esto decidía
    # SOLO qué se ve en la pestaña y qué llega a Exclusivo -- las otras
    # 5 seguían sonando en Admin igual. Ahora el mismo valor también
    # corta el sonido en Admin -- una sola lista, un solo lugar.
    "FTR_VIVO_ESTRATEGIAS": {
        "tipo": "str", "default": "favorito_pre_partido", "categoria": "Partidos en Vivo (postSet 1)",
        "etiqueta": "Estrategias que suenan (separadas por coma, o * para todas)",
        "descripcion": (
            "Nombres de estrategia tal cual los manda el bot -- favorito_pre_partido, "
            "favorito_remonta, favorito_vulnerable, fear_remonta, parejo_remonta, "
            "bp_remonta, bp_parejo. Lo que NO esté en esta lista se guarda igual "
            "(para historial/diagnóstico) pero no suena en ningún lado -- ni Admin, "
            "ni la pestaña, ni Exclusivo."
        ),
    },

    # Muestra mínima -- partidos que necesita CADA jugador para que el
    # motor calcule algo en vez de negarse (01/08/2026, bajado de 40 a
    # 30 a pedido de Maria, caso real: Kirkin vs Paldanius -- Paldanius
    # tenía 34 partidos en dura y el sistema no calculaba nada por
    # quedar 6 por debajo del piso viejo. 30 coincide con
    # CAP_PARTIDOS_SUPERFICIE en shared/indice_fiabilidad.py -- el
    # propio índice de confianza ya trata 30 partidos como "maduro"
    # para ese factor, así que el piso de la alerta deja de ser más
    # estricto que el propio sistema de confianza).
    "MUESTRA_MINIMA_FTR": {
        "tipo": "int", "default": "30", "categoria": "Obsoletos — ya no deciden nada",
        "etiqueta": "Partidos mínimos, motor FTR (por superficie)",
        "descripcion": (
            "Cada jugador necesita esta cantidad de partidos EN LA SUPERFICIE del "
            "partido en el rating de producción (FTR) para que el motor calcule una "
            "probabilidad -- si a cualquiera de los dos le falta, no se calcula nada "
            "(ni a favor ni en contra). Compartido por las tres estrategias -- "
            "Movimiento Rentable y Valor FTR lo usan vía _probs_filtro_alerta()."
        ),
    },
    "MUESTRA_MINIMA_ELO": {
        "tipo": "int", "default": "30", "categoria": "Obsoletos — ya no deciden nada",
        "etiqueta": "Partidos mínimos, motor Elo (research Elo, por superficie)",
        "descripcion": "Lo mismo que arriba, pero para el rating de research Elo -- solo lo usa Valor Pre-Partido (FTR+Elo).",
    },
    "MUESTRA_MINIMA_MOVIMIENTO_VIEJO": {
        "tipo": "int", "default": "30", "categoria": "Obsoletos — ya no deciden nada",
        "etiqueta": "Partidos mínimos -- motor viejo (sin uso hoy)",
        "descripcion": (
            "Vive en production/engines/valor_engine.py::evaluar_valor_elo() -- el motor "
            "de Valor Pre-Partido basado solo en research Elo, apagado desde el 31/07/2026 "
            "(se fusionó con FTR+Elo). No decide nada en producción hoy; se deja acá "
            "editable por consistencia, no porque haga falta tocarlo."
        ),
    },

    # ═════════════════════════════════════════════════════════════════
    #  03/08/2026 -- las estrategias que faltaban
    # ═════════════════════════════════════════════════════════════════
    # Hasta hoy el panel cubría Valor Pre-Partido, Movimiento Rentable,
    # Valor FTR y Partidos en Vivo. Under, Favorito, Racha, Doble
    # Jornada y MTO no tenían NADA editable: sus umbrales eran
    # constantes en el código (algunas ni siquiera en este repo, ver
    # nota de UNDER/FAVORITO abajo). Todo lo de acá para abajo es de
    # esa vuelta.

    # ── Topics de Telegram (01/09/2026) ──────────────────────────────
    # El NÚMERO de topic de cada estrategia se carga en Admin →
    # Telegram → Matriz de envío, celda por celda (vive en ftr_ruteo,
    # no acá: es un dato por cruce categoría×canal, no un parámetro
    # global). Lo único global es qué hacer cuando el topic falla, y
    # eso es esta clave.
    "TOPIC_REINTENTAR_EN_GENERAL": {
        "tipo": "bool", "default": "1", "categoria": "Topics de Telegram",
        "etiqueta": "Reintentar en General si el topic no sirve",
        "descripcion": (
            "Cuando Telegram rechaza el mensaje por el topic ('message thread not found' "
            "si se borró, cambió de número o es de otro canal; 'TOPIC_CLOSED' si está "
            "cerrado), la alerta se manda igual al topic General. Encendido: una alerta en "
            "el lugar equivocado se ve, una que no llega no se ve nunca. Apagado: la alerta "
            "se pierde y solo queda en el log. En los dos casos el motivo se anota en "
            "'Último envío' del destino, así que el panel muestra el canal como caído aunque "
            "el mensaje haya salido por General. NO se reintenta ante otros errores "
            "('chat not found', 'bot was kicked'): ahí el reintento fallaría igual."
        ),
    },

    # ── Movimiento Rentable: lo que quedaba fijo ─────────────────────
    # AVISO DE REGRESO TRAS RETIRO (07/08/2026, a pedido de Rubén: "me
    # gustaría que se me notifique cuando [juegue] el siguiente partido
    # de ese jugador retirado"). Ver _avisar_si_vuelve_tras_retiro().
    "FTR_RETIRO_AVISAR": {
        "tipo": "bool", "default": "1", "categoria": "Avisos de contexto",
        "etiqueta": "Avisar cuando un jugador vuelve tras retirarse",
        "descripcion": (
            "Manda un aviso al canal de Admin la primera vez que se ve un fixture de un "
            "jugador que se retiró de su partido anterior. Es contexto, no una alerta de "
            "valor: no entra a Picks ni a Sugerencias y no toca ninguna estrategia."
        ),
    },
    "FTR_RETIRO_VENTANA_DIAS": {
        "tipo": "int", "default": "21", "categoria": "Avisos de contexto",
        "etiqueta": "Ventana del retiro (días)",
        "descripcion": (
            "Un retiro más viejo que esto ya no genera aviso -- no dice nada del estado "
            "físico de hoy. El aviso sale UNA sola vez por retiro, en el primer fixture que "
            "se ve de ese jugador dentro de la ventana."
        ),
    },

    "FTR_RETIRO_AVISO_MINUTOS_ANTES": {
        "tipo": "int", "default": "30", "categoria": "Avisos de contexto",
        "etiqueta": "Avisar cuántos minutos antes del partido",
        "descripcion": (
            "El aviso se guarda apenas se detecta el fixture -- el partido aparece en la "
            "pestaña Regreso de Retiro en ese momento -- pero el mensaje de Telegram espera "
            "hasta que falte este tiempo para el saque (12/08/2026, a pedido de Rubén). "
            "Antes salía en cuanto el bot veía el fixture, que puede ser un día antes, cuando "
            "todavía no hay nada que hacer con el dato. En 0 vuelve a avisar de inmediato. "
            "Los fixtures que llegan sin hora (solo el día, típico del ciclo ITF) no esperan: "
            "no hay contra qué medir."
        ),
    },

    # ── SOÑADORAS (12/08/2026) ────────────────────────────────────────
    # Nacieron como variables de entorno y se mudaron acá el 13/08 a
    # pedido de Rubén ("que el matchup de todas las estrategias sea
    # modificable desde el Admin"). Tenía razón: un umbral que exige
    # redesplegar para moverse no se ajusta nunca, y estos son
    # exactamente los que hay que calibrar mirando resultados.
    "FTR_SON_UMBRAL_ELO": {
        "tipo": "float", "default": "0.55", "categoria": "Soñadoras",
        "etiqueta": "Prob. Elo mínima",
        "descripcion": ("Primer corte de la pestaña Soñadoras. Los partidos del día que no "
                        "llegan no aparecen en la lista."),
    },
    "FTR_SON_UMBRAL_FTR": {
        "tipo": "float", "default": "0.55", "categoria": "Soñadoras",
        "etiqueta": "Prob. FTR mínima",
        "descripcion": "Segundo corte. Se aplica junto con el de Elo: hay que cumplir los dos.",
    },
    "FTR_SON_UMBRAL_MATCHUP": {
        "tipo": "float", "default": "0.50", "categoria": "Soñadoras",
        "etiqueta": "Prob. matchup mínima",
        "descripcion": ("Decide si un partido va ARRIBA o AL FONDO de la lista, y NO si "
                        "aparece: los que no llegan igual se muestran, atenuados y al final. "
                        "Los que todavía no tienen el matchup calculado quedan en el medio, "
                        "marcados como 'calculando' -- el worker de estilo puede tardar. "
                        "Subirlo achica el bloque de arriba sin esconder nada."),
    },
    "FTR_SON_CACHE_SEG": {
        "tipo": "int", "default": "60", "categoria": "Soñadoras",
        "etiqueta": "Segundos de caché de la pestaña",
        "descripcion": ("Cuánto se reusa el cálculo antes de rehacerlo. Son partidos del día "
                        "siguiente, así que no cambian de un minuto al otro. Bajarlo mucho "
                        "hace que cada usuario dispare su propio barrido: el 12/08 eso mismo "
                        "tumbó el panel LIVE."),
    },
    "FTR_VIVO_UMBRAL_LEVE": {
        "tipo": "float", "default": "0.51", "categoria": "Panel LIVE (solo la vista, no alerta)",
        "etiqueta": "GRUPO 2 de 'Ventaja leve' -- piso de Prob. FTR",
        "descripcion": ("GRUPO 2 de 'Ventaja leve'. Este número NO tiene nada que ver con el "
                        "Grupo 1: el Grupo 1 usa los umbrales de Admin → Estrategias → "
                        "Partidos en Vivo (postSet 1) y no se edita acá. Los tres pisos de "
                        "Grupo 2 (FTR, Elo y Matchup) se exigen JUNTOS, con Y: el favorito "
                        "entra por Grupo 2 solo si su Prob. FTR llega a este piso Y su Prob. "
                        "Elo llega al de abajo Y su Matchup llega al de más abajo. "
                        "CORRECCIÓN 21/08/2026: hasta hoy este texto decía 'alcanza con que "
                        "UNA de las tres' -- era la redacción de una versión anterior del "
                        "20/08 que quedó sin actualizar cuando la regla pasó a dos grupos ese "
                        "mismo día. El código siempre exigió las tres juntas (ver _g2 en "
                        "routes_public.py); el que estaba mal era el texto. Corte de VISTA, no "
                        "de alerta: mover esto no cambia ningún mensaje, solo qué partidos se "
                        "agrupan en esa pestaña."),
    },
    "FTR_VIVO_UMBRAL_LEVE_ELO": {
        "tipo": "float", "default": "0.51", "categoria": "Panel LIVE (solo la vista, no alerta)",
        "etiqueta": "GRUPO 2 de 'Ventaja leve' -- piso de Prob. Elo",
        "descripcion": ("GRUPO 2 de 'Ventaja leve'. Segundo de los tres pisos que se exigen "
                        "JUNTOS (FTR Y Elo Y Matchup) -- subir este solo alcanza para sacar "
                        "partidos del Grupo 2, porque basta que falle uno para quedar afuera. "
                        "No afecta al Grupo 1, que no mira estos números. CORRECCIÓN "
                        "21/08/2026: decía 'alcanza con que UNO de los tres' y era falso, ver "
                        "la nota completa en el piso de FTR de arriba. Corte de VISTA, no de "
                        "alerta."),
    },
    "FTR_VIVO_UMBRAL_LEVE_MUP": {
        "tipo": "float", "default": "0.51", "categoria": "Panel LIVE (solo la vista, no alerta)",
        "etiqueta": "GRUPO 2 de 'Ventaja leve' -- piso de Matchup",
        "descripcion": ("GRUPO 2 de 'Ventaja leve'. Tercero de los tres pisos que se exigen "
                        "JUNTOS. OJO con el matchup sin calcular: un partido cuyo matchup el "
                        "worker de estilo todavía no resolvió NO entra por Grupo 2 (no se le "
                        "adivina el valor), pero SÍ puede entrar por Grupo 1, que no mira "
                        "matchup -- de hecho ése es el caso que el Grupo 1 existe para cubrir. "
                        "CORRECCIÓN 21/08/2026: decía que era 'uno de los tres caminos' y era "
                        "falso, ver la nota completa en el piso de FTR de arriba. Corte de "
                        "VISTA, no de alerta."),
    },

    # APARTADOS DEL PANEL LIVE (v54i, 15/10/2026, Rubén: «que solo quede el apartado de Live, que los otros se
    # borren; en caso de querer alguno, que se pueda crear desde el Admin»). «🔴 En vivo» queda siempre. Los otros
    # cuatro se apagan de fábrica y se vuelven a mostrar desde acá. Son SOLO VISTAS sobre la misma lista:
    # apagarlos no toca ningún motor ni alerta (Ventaja Leve, Pasan Filtro, postSet 1… siguen igual).
    "LIVE_APARTADO_PASAN": {
        "tipo": "bool", "default": "0", "categoria": "Panel LIVE (solo la vista, no alerta)",
        "etiqueta": "Mostrar el apartado «⭐ Pasan el filtro» en LIVE",
        "descripcion": ("Apagado: el panel LIVE muestra solo «🔴 En vivo». Prendido: vuelve el botón con los "
                        "partidos que pasan muestra + FTR + Elo + MatchUp. Es solo una vista: no cambia "
                        "ninguna alerta."),
    },
    "LIVE_APARTADO_LEVE": {
        "tipo": "bool", "default": "0", "categoria": "Panel LIVE (solo la vista, no alerta)",
        "etiqueta": "Mostrar el apartado «⚖️ Ventaja leve» en LIVE",
        "descripcion": ("Apagado: no se ve el botón. La estrategia Ventaja Leve (VL01–VL05) sigue sonando "
                        "igual en Telegram y Discord: esto es solo la vista del panel."),
    },
    "LIVE_APARTADO_MATCHUP": {
        "tipo": "bool", "default": "0", "categoria": "Panel LIVE (solo la vista, no alerta)",
        "etiqueta": "Mostrar el apartado «🎯 MatchUp» en LIVE",
        "descripcion": ("Apagado: no se ve el botón. Prendido: lista los partidos con matchup por encima del "
                        "corte «MatchUp: corte de la pestaña del panel LIVE». Solo vista."),
    },
    "LIVE_APARTADO_TERCER": {
        "tipo": "bool", "default": "0", "categoria": "Panel LIVE (solo la vista, no alerta)",
        "etiqueta": "Mostrar el apartado «🥎 Tercer Set» en LIVE",
        "descripcion": ("Apagado: no se ve el botón. Prendido: favorito que ganó el Set 1 y perdió el Set 2, "
                        "con FTR + Elo + MatchUp. Solo vista."),
    },

    # ── CONEXIÓN EN VIVO (bot) ───────────────────────────────────────
    # Estos tres NO los usa RankingFTR: los lee el BOT
    # (BOT_FTR_APITENNIS) cada pocos minutos desde /config/bot. Viven
    # acá para que se puedan tocar desde el panel sin redesplegar el bot
    # -- 08/08/2026, a pedido de Rubén tras un corte de feed que dejó
    # los partidos en vivo congelados durante más de diez minutos.
    "BOT_SILENCIO_CON_PARTIDOS_SEG": {
        "tipo": "int", "default": "45", "categoria": "Conexión en vivo (bot)",
        "etiqueta": "Segundos sin datos para reconectar (habiendo partidos)",
        "descripcion": (
            "El bot recibe los marcadores en vivo por una conexión permanente. A veces esa "
            "conexión queda abierta pero deja de traer datos: nadie avisa nada y los partidos "
            "se congelan. Este número es cuántos segundos se esperan, CUANDO SE SABE QUE HAY "
            "PARTIDOS JUGÁNDOSE, antes de cortar y reconectar. Más bajo = se recupera antes "
            "pero reconecta más seguido. No conviene subirlo mucho: la alerta del Set 1 solo "
            "vale unos minutos, así que un corte largo se come la oportunidad entera."
        ),
    },
    "BOT_SILENCIO_SIN_SABER_SEG": {
        "tipo": "int", "default": "300", "categoria": "Conexión en vivo (bot)",
        "etiqueta": "Segundos sin datos para reconectar (sin partidos o sin saber)",
        "descripcion": (
            "Lo mismo que arriba, pero para cuando NO hay partidos en vivo (de madrugada, por "
            "ejemplo) o cuando no se pudo averiguar si los hay. Ahí el silencio es normal y no "
            "hay apuro, así que se espera bastante más antes de reconectar por las dudas. "
            "Este es el valor que se usa por defecto cuando hay cualquier duda."
        ),
    },
    "BOT_PROCESANDO_AVISOS_MIN": {
        "tipo": "int", "default": "3", "categoria": "Conexión en vivo (bot)",
        "etiqueta": "Veces seguidas sin ver un partido antes de darlo por terminado",
        "descripcion": (
            "El bot avisa cuando un partido desaparece de su lista de partidos en vivo. Esto "
            "dice cuántos avisos SEGUIDOS hacen falta antes de marcarlo 'procesando' y dejar "
            "de esperar datos suyos. Cualquier dato de marcador que llegue reinicia la cuenta. "
            "EN 1 SE COMPORTA COMO ANTES del 23/08/2026: se daba por terminado al primer "
            "aviso, y eso estaba matando partidos vivos -- de 40 rachas que la otra fuente "
            "reportó, 26 eran de partidos que nosotros ya habíamos dado por terminados, así "
            "que no llegaba ni un dato más y no se detectaba nada. "
            "Subirlo hace que un partido realmente terminado tarde unos minutos más en salir "
            "del panel LIVE (cada aviso llega cada ~60s). Bajarlo acelera la limpieza y "
            "vuelve a arriesgar lo de arriba."
        ),
    },
    "BOT_CHEQUEO_LIVE_SEG": {
        "tipo": "int", "default": "60", "categoria": "Conexión en vivo (bot)",
        "etiqueta": "Cada cuántos segundos preguntar si hay partidos en vivo",
        "descripcion": (
            "Para elegir cuál de los dos tiempos de arriba aplicar, el bot pregunta a la API "
            "si hay partidos jugándose ahora. Esa pregunta cuesta cuota, así que solo se hace "
            "cuando ya hay silencio sospechoso, y como mucho una vez cada tantos segundos. "
            "Subirlo gasta menos cuota; bajarlo detecta los cortes un poco antes."
        ),
    },

    "FTR_SUPERFICIE_DESCONOCIDA": {
        "tipo": "str", "default": "general",
        "categoria": "Obsoletos — ya no deciden nada",
        "etiqueta": "Sin superficie conocida: general | descartar",
        "descripcion": (
            "Qué hacer cuando no se puede saber en qué superficie se juega (pasa sobre todo "
            "en ITF chicos, donde ni el bot ni el nombre del torneo la traen). 'general' "
            "(default): se evalúa con el rating general, que mezcla las tres superficies. "
            "'descartar': sin superficie no se manda la alerta. Hasta el 04/08/2026 estos "
            "partidos se evaluaban como si fueran CANCHA DURA, que es lo que hizo salir la "
            "alerta de Jakupovic vs Malygina (era tierra)."
        ),
    },
    "MOV_CONFIANZAS": {
        "tipo": "str", "default": "alta,muy_alta", "categoria": "Obsoletos — ya no deciden nada",
        "etiqueta": "Niveles de confianza que confirman (F6)",
        "descripcion": (
            "Separados por coma. F6 exige que la confianza del modelo propio esté en esta "
            "lista. Los niveles que existen son: muy_alta, alta, media, baja. Agregar 'media' "
            "hace pasar más movimientos (y más ruido); dejar solo 'muy_alta' lo vuelve muy "
            "estricto. Antes era un conjunto fijo en movimiento_engine.py."
        ),
    },
    "MOV_SUGERENCIA_CAIDA_MIN": {
        "tipo": "float", "default": "20.0", "categoria": "Obsoletos — ya no deciden nada",
        "etiqueta": "Caída mínima para mandar a Sugerencias, %",
        "descripcion": (
            "OBSOLETO desde el 20/08/2026: Sugerencias ya no usa un piso de caída. El "
            "criterio pasó a ser Prob. Elo/FTR/matchup mínimas + caída MÁXIMA, todas a la "
            "vez (ver los cuatro campos siguientes). Este valor ya no se lee en ningún "
            "lado; se deja declarado para no romper el histórico de configuración guardado "
            "en la base. El motivo del cambio: la rama de caída alta rindió -46,3% de ROI "
            "(3-8) sobre 146 descartes resueltos."
        ),
    },
    "MOV_SUGERENCIA_PROB_ELO_MIN": {
        "tipo": "float", "default": "0.50", "categoria": "Movimiento Rentable — canal Sugerencias",
        "etiqueta": "Prob. Elo mínima para mandar a Sugerencias",
        "descripcion": (
            "20/08/2026, escala 0-1. Se exige JUNTO con Prob. FTR, Prob. matchup y la "
            "caída MÁXIMA -- las cuatro a la vez. Es el filtro que manda: con Elo >= 0,50 "
            "y caída < 12% el histórico dio 17-4 (81,0%, +54,7% de ROI, n=21). Rescata "
            "alertas con buen Elo que F6 tumbó por nivel de confianza, o cumple_filtros_"
            "alerta por FTR/matchup. Poner 0 lo desactiva."
        ),
    },
    "MOV_SUGERENCIA_ACTIVA": {
        "tipo": "float", "default": "0", "categoria": "Movimiento Rentable — canal Sugerencias",
        "etiqueta": "Sugerencias activas (1 = si, 0 = no)",
        "descripcion": (
            "20/08/2026, APAGADO por defecto. Al pasar Movimiento Rentable a la regla "
            "unica del cruce a favorito, la poblacion que llega a Sugerencias cambio de "
            "significado: antes era 'cumplio el mercado pero el modelo no confirmo', ahora "
            "es 'cruzo a favorito pero alguna de las tres probabilidades no llego al 50%'. "
            "O sea, EXACTAMENTE el grupo de control del backtest: 39 casos, 16-23, 41,0% "
            "de acierto, -24,9% de ROI. Encenderlo es mandar a Telegram el grupo que se "
            "midio perdiendo. Las filas se siguen guardando en Descartes igual, que es "
            "donde tiene que estar el control para poder compararlo en unas semanas. "
            "Ponerlo en 1 reactiva el canal con los cuatro umbrales de abajo."
        ),
    },
    "MOV_FLIP_CUOTA_MIN": {
        "tipo": "float", "default": "1.75", "categoria": "Movimiento Rentable",
        "etiqueta": "1. Cuota mínima en el instante del cruce",
        "descripcion": (
            "Piso de la cuota en el instante del cruce. "
            "OJO: la condicion de flip ya impone un techo implicito -- si es favorito, su "
            "cuota esta por debajo de ~1,90 -- asi que la banda real es 1,75-1,90, no "
            "1,75-2,50. Medido: min 1,59, mediana 1,83, max 1,93."
        ),
    },
    "MOV_FLIP_ELO_MIN": {
        "tipo": "float", "default": "0.50", "categoria": "Movimiento Rentable",
        "etiqueta": "2. Prob. Elo CRUDA mínima del que cruza",
        "descripcion": (
            "Usa el Elo CRUDO (expected_score sobre los ratings "
            "de research Elo), NO el ajustado por contexto: AUC 0,676 contra 0,519. Es la "
            "unica de las tres condiciones que en los datos hace trabajo real -- separo "
            "+13,4% de -24,9%. Estricto (>), asi que 0,500 exacto NO pasa."
        ),
    },
    "MOV_FLIP_FTR_MIN": {
        "tipo": "float", "default": "0.50", "categoria": "Movimiento Rentable",
        "etiqueta": "3. Prob. FTR mínima del que cruza",
        "descripcion": (
            "AVISO: a 0,50 esto es casi un no-op. En las 146 "
            "alertas medidas Prob. FTR nunca bajo de 0,50 (minimo observado 0,5005), asi "
            "que deja pasar practicamente todo. Y su AUC es 0,455, por debajo del azar, "
            "asi que SUBIRLO probablemente empeore en vez de mejorar -- 'Prob. FTR >= "
            "0,55' rindio -21,3% con IC95 [-40%, -2%]. Sin cobertura (None) pasa."
        ),
    },
    "MOV_FLIP_FTR_MAX": {
        "tipo": "float", "default": "1.0", "categoria": "Movimiento Rentable",
        "etiqueta": "Prob. FTR maxima del favorito",
        "descripcion": (
            "28/08/2026, a pedido de Ruben: que en todas las estrategias se pueda poner el rango del valor FTR, no solo el piso. Techo de la banda -- junto con MOV_FLIP_FTR_MIN arman el rango completo. Un favorito con Prob. FTR POR ENCIMA de este numero (diferencia demasiado grande entre los dos jugadores) deja de contar, igual que uno por debajo del piso. Mismo criterio que VPRE_FTR_MAX y VIVO_FTR_MAX, que ya lo tenian. DEFAULT 1.0 = el techo no corta nada, o sea el comportamiento de antes de este cambio: hay que bajarlo a mano para que empiece a filtrar."
        ),
    },
    "MOV_FLIP_MUP_MIN": {
        "tipo": "float", "default": "0.50", "categoria": "Movimiento Rentable",
        "etiqueta": "4. Prob. matchup mínima del que cruza",
        "descripcion": (
            "Matchup sin cobertura (None) PASA, mismo criterio "
            "que la politica 'disponible' de FTR_MATCHUP_GATE: un filtro que descarta en "
            "silencio por falta de datos es peor que uno que se abstiene. En modo flip "
            "esta probabilidad SI decide, asi que si no esta persistida se calcula al "
            "vuelo -- son ~6 flips/dia, no miles, y el costo se paga poquisimas veces."
        ),
    },
    "MOV_DESTINO_NO_PASA": {
        "tipo": "str", "default": "sugerencias", "categoria": "Movimiento Rentable",
        "etiqueta": "A dónde llega si cruza y FALLA",
        "opciones": [
            {"valor": "sugerencias", "etiqueta": "💡 Sugerencias (como hoy)"},
            {"valor": "descartes", "etiqueta": "🗑️ Descartes"},
            {"valor": "ninguno", "etiqueta": "Ninguno -- no se guarda aparte"},
        ],
        "descripcion": (
            "20/08/2026, a pedido de Maria. Aplica SOLO cuando el tapado cruza a favorito "
            "pero no pasa los cuatro números de arriba -- HOY (default 'sugerencias') va a "
            "💡 Sugerencias con el motivo. 'descartes': va a 🗑️ Descartes en cambio. "
            "'ninguno': no se guarda nada aparte. Si NO cruza a favorito, la fila queda "
            "solo en la tabla movimientos_rentables (su pestaña) siempre, sin pasar por "
            "este desplegable -- eso no es un descarte, es que la condición ni se evaluó."
        ),
    },
    "UNDER_CIRCUITOS_SILENCIADOS": {
        "tipo": "str", "default": "utr", "categoria": "Under en Vivo",
        "etiqueta": "Circuitos de Under que NO salen a Picks (solo se guardan)",
        "descripcion": (
            "20/08/2026. Lista separada por comas de circuitos normalizados (world_tennis, "
            "utr, wta, atp, itf, challenger, otros). Los Under de esos circuitos que pasen "
            "el umbral de UTR se GUARDAN en la pestana Sugerencias para diagnostico, pero "
            "NO se mandan a Picks ni suenan en Telegram. El resto de los circuitos sigue "
            "funcionando exactamente igual que antes. VACIO desactiva el desvio y todo "
            "vuelve al comportamiento anterior. Default 'utr' por el balance del circuito, "
            "pero OJO: son solo 17 apuestas resueltas (7-10, ROI -24,6%), y el IC95 de ese "
            "ROI va de -66% a +23% -- cruza el cero de lado a lado. No es evidencia de que "
            "UTR pierda; es falta de datos. Por eso se guardan en vez de descartarse: para "
            "poder decidir con ~200-300 casos en vez de con 17. Depende de que el forwarder "
            "mande `circuito` -- si llega vacio, el Under NO se silencia."
        ),
    },
    "UNDER_DESTINO_NO_PASA": {
        "tipo": "str", "default": "sugerencias", "categoria": "Under en Vivo",
        "etiqueta": "A dónde llega si el circuito está silenciado",
        "opciones": [
            {"valor": "sugerencias", "etiqueta": "💡 Sugerencias (como hoy)"},
            {"valor": "descartes", "etiqueta": "🗑️ Descartes"},
            {"valor": "ninguno", "etiqueta": "Ninguno -- no se guarda"},
        ],
        "descripcion": (
            "20/08/2026, a pedido de Maria. Aplica a los Under que NO se mandan a Picks "
            "pero se guardan igual para medir -- circuitos silenciados (el campo de "
            "arriba) Y, desde el 25/08/2026, partidos femeninos (ver UNDER_SILENCIAR_"
            "FEMENINO más abajo). HOY (default 'sugerencias') se guardan en 💡 "
            "Sugerencias para poder medirlos. 'descartes': 🗑️ Descartes en cambio. "
            "'ninguno': no se guarda nada -- se pierde la muestra que se está juntando "
            "para decidir con más de 17 casos. Si el ΔUTR no entra en la banda, no se "
            "guarda nunca, sea cual sea esta elección -- ese corte pasa en Reenvio_MTO, "
            "antes de llegar acá."
        ),
    },
    "UNDER_SILENCIAR_FEMENINO": {
        "tipo": "bool", "default": "1", "categoria": "Under en Vivo",
        "etiqueta": "Partidos femeninos: guardar sin enviar a Picks",
        "descripcion": (
            "25/08/2026, a pedido de Maria: \"que los partidos singles femeninos no se "
            "envíen pero sí se guarden en sugerencias\". Prendido (default), un Under de "
            "un partido con genero='F' se guarda igual (Admin + el destino de "
            "UNDER_DESTINO_NO_PASA, arriba) pero NO se manda a Picks ni suena en "
            "Telegram -- mismo mecanismo que un circuito silenciado, mismo desplegable de "
            "destino. Apagado, vuelve a enviarse como cualquier otro Under. "
            "OJO CON \"SINGLES\": el mensaje de este canal no trae ningún dato de dobles/"
            "singles (a diferencia de Jugador en Racha, que sí lo tiene) -- esto filtra "
            "por género nomás. Si algún dobles femenino llega a colarse por acá, no hay "
            "forma de distinguirlo del lado de este código con lo que manda hoy el "
            "forwarder."
        ),
    },
    # ── Zonas de observación de Under (23/09/2026, a pedido de Rubén
    # Darío) -- ver production/engines/under_vivo_registro.py. CONGELADAS
    # para medir fuera de muestra: no se optimizan durante la prueba.
    "UNDER_OBS_CUOTA_MIN": {
        "tipo": "float", "default": "1.85", "categoria": "Under en Vivo",
        "etiqueta": "Observación — cuota UNDER desde (inclusive)",
        "descripcion": (
            "23/09/2026. Un Under que cumple la estrategia con la cuota UNDER dentro de "
            "[desde, hasta] (inclusive) NO se envía: se guarda como UNDER_OBSERVACION_CUOTA "
            "y se resuelve igual, para medir la franja fuera de muestra. CONGELADO durante "
            "la prueba."
        ),
    },
    "UNDER_OBS_CUOTA_MAX": {
        "tipo": "float", "default": "1.89", "categoria": "Under en Vivo",
        "etiqueta": "Observación — cuota UNDER hasta (inclusive)",
        "descripcion": "Ver 'desde', arriba. CONGELADO durante la prueba.",
    },
    "UNDER_EXIGIR_MASCULINO": {
        "tipo": "bool", "default": "1", "categoria": "Under en Vivo",
        "etiqueta": "Exigir género masculino confirmado",
        "descripcion": (
            "24/09/2026. Prendido (default): si el aviso llega sin género (el torneo no dice "
            "Men/Women/ATP/WTA, p. ej. copas por equipos), NO se envía: se guarda como "
            "DESCARTE_GENERO_DESCONOCIDO y se resuelve igual. Billie Jean King Cup se "
            "reconoce como femenino y Davis Cup como masculino."
        ),
    },
    "UNDER_VIVO_PUBLICAR": {
        "tipo": "bool", "default": "0", "categoria": "Under en Vivo",
        "etiqueta": "Publicar los Under del canal de Telegram (Picks, Telegram y Discord)",
        "descripcion": (
            "18/10/2026, Rubén: «saquemos las Under que vienen de Telegram». Apagado (default): los Under del "
            "canal 🐢UTR se siguen recibiendo, filtrando y guardando (Alertas Under en Vivo y registro, con su "
            "resultado) para medirlos, pero NO suenan en Admin ni salen a Picks, Telegram o Discord. Los Under "
            "propios son los del 🔥 UNDER UTR. Prendido = como antes."
        ),
    },
    "UNDER_OBSERVAR_DOBLES": {
        "tipo": "bool", "default": "1", "categoria": "Under en Vivo",
        "etiqueta": "Dobles: guardar sin enviar (observación)",
        "descripcion": (
            "23/09/2026. Prendido (default): un Under de dobles (nombre con '/' o "
            "'doubles'/'dobles'/'mixed' en el torneo) se guarda como "
            "UNDER_OBSERVACION_DOBLES y NO se envía. OJO: match_log es solo singles, así "
            "que los dobles quedan guardados pero hoy no se resuelven solos."
        ),
    },
    "MOV_SUGERENCIA_PROB_ELO_FUENTE": {
        "tipo": "str", "default": "ajustada", "categoria": "Movimiento Rentable — canal Sugerencias",
        "etiqueta": "Qué Prob. Elo mira Sugerencias: ajustada | crudo",
        "descripcion": (
            "20/08/2026. Hay DOS numeros distintos que hasta hoy compartian el nombre "
            "'Prob. Elo': el CRUDO (expected_score sobre los ratings de research Elo) y el "
            "AJUSTADO (prob_fulltenis, que le suma correcciones por superficie, descanso, "
            "edad y altura). Sobre las 146 alertas medidas el crudo dio AUC 0,676 y el "
            "ajustado 0,519 -- indistinguible del azar. El default se queda en 'ajustada' "
            "porque el 17-4 del historico se midio con esa: cambiar la variable y evaluar "
            "el cambio al mismo tiempo no permite atribuir el resultado a nada. 'crudo' es "
            "la candidata a probar en septiembre, con un mes limpio. Si se pide 'crudo' y "
            "el dato falta, se usa el ajustado (no se deja pasar todo)."
        ),
    },
    "MOV_SUGERENCIA_CAIDA_MAX": {
        "tipo": "float", "default": "12.0", "categoria": "Movimiento Rentable — canal Sugerencias",
        "etiqueta": "Caída MÁXIMA para mandar a Sugerencias, %",
        "descripcion": (
            "20/08/2026. TECHO, no piso -- se lee junto a las tres Prob. mínimas de abajo "
            "y todas se exigen a la vez. Medido sobre 146 descartes resueltos: con "
            "Prob. Elo >= 0,50, las 21 alertas por DEBAJO del 12% de caída fueron 17-4 "
            "(81,0% de acierto, +54,7% de ROI) y las 11 por encima fueron 4-7 (36,4%, "
            "-21,2%). Fisher exacto p=0,0198. El mismo corte sobre las 146 sin filtrar "
            "por Elo da -7,9% debajo del 12% contra -18,6% encima. Las caídas grandes "
            "ocurren en cuotas altas y jugadores flojos. Poner 0 lo desactiva."
        ),
    },
    "MOV_SUGERENCIA_PROB_FTR_MIN": {
        "tipo": "float", "default": "0.50", "categoria": "Movimiento Rentable — canal Sugerencias",
        "etiqueta": "Prob. FTR mínima para mandar a Sugerencias",
        "descripcion": (
            "20/08/2026, escala 0-1. OJO: en las 146 alertas medidas el mínimo observado "
            "de Prob. FTR fue 0,5005, o sea que un umbral de 0,50 lo cumple el 100% y no "
            "filtra nada. Se deja como campo propio para poder subirlo sin tocar el de "
            "Valor FTR. Poner 0 lo desactiva."
        ),
    },
    "MOV_SUGERENCIA_PROB_FTR_MAX": {
        "tipo": "float", "default": "1.0", "categoria": "Movimiento Rentable — canal Sugerencias",
        "etiqueta": "Prob. FTR maxima del favorito",
        "descripcion": (
            "28/08/2026, a pedido de Ruben: que en todas las estrategias se pueda poner el rango del valor FTR, no solo el piso. Techo de la banda -- junto con MOV_SUGERENCIA_PROB_FTR_MIN arman el rango completo. Un favorito con Prob. FTR POR ENCIMA de este numero (diferencia demasiado grande entre los dos jugadores) deja de contar, igual que uno por debajo del piso. Mismo criterio que VPRE_FTR_MAX y VIVO_FTR_MAX, que ya lo tenian. DEFAULT 1.0 = el techo no corta nada, o sea el comportamiento de antes de este cambio: hay que bajarlo a mano para que empiece a filtrar."
        ),
    },
    "MOV_SUGERENCIA_PROB_MATCHUP_MIN": {
        "tipo": "float", "default": "0.50", "categoria": "Movimiento Rentable — canal Sugerencias",
        "etiqueta": "Prob. matchup mínima para mandar a Sugerencias",
        "descripcion": (
            "20/08/2026, escala 0-1. Si el matchup no está calculado la alerta pasa igual "
            "(mismo criterio que 'disponible' en FTR_MATCHUP_GATE): sin cobertura no se "
            "castiga. Poner 0 lo desactiva."
        ),
    },
    "FTR_MATCHUP_GATE": {
        "tipo": "str", "default": "disponible",
        "categoria": "Obsoletos — ya no deciden nada",
        "etiqueta": "Política del matchup de estilo: disponible | estricto",
        "descripcion": (
            "'disponible' (default): si el diagnóstico de estilo todavía no está calculado, "
            "la alerta pasa igual -- el matchup solo opina cuando ya está listo. 'estricto': "
            "sin diagnóstico de estilo no hay alerta. Ojo con 'estricto': el cálculo en vivo "
            "puede tardar hasta 19s y no se dispara desde acá, así que en la práctica "
            "silenciaría muchas alertas."
        ),
    },

    # ── Under en Vivo / Favorito en Vivo ─────────────────────────────
    # IMPORTANTE: estos umbrales se aplican en DOS lugares. El filtro
    # de verdad vive en Reenvio_MTO (forwarder.py, parse_live_value_
    # message) -- lo que no pasa ahí nunca llega a este repo. Desde el
    # 03/08 el forwarder LEE estos valores de acá (GET
    # /config/estrategias/forwarder, refrescado cada 5 min), así que
    # tocarlos en el panel sí los cambia de punta a punta; y RankingFTR
    # los vuelve a chequear antes de hacer sonar la alerta, como red de
    # seguridad por si el forwarder está con la copia vieja.
    "UNDER_UTR_MIN": {
        "tipo": "float", "default": "1.0", "categoria": "Under en Vivo",
        "etiqueta": "Diferencia de UTR mínima",
        "descripcion": "Piso de ΔUTR entre los dos jugadores para que la línea de total se considere.",
    },
    "UNDER_UTR_MAX": {
        "tipo": "float", "default": "4.0", "categoria": "Under en Vivo",
        "etiqueta": "Diferencia de UTR máxima",
        "descripcion": (
            "Techo de ΔUTR. Por encima de esto la diferencia entre los jugadores es tan "
            "grande que el partido deja de ser parejo y el under pierde sentido."
        ),
    },
    "FAVORITO_UTR_MIN": {
        "tipo": "float", "default": "1.0", "categoria": "Favorito en Vivo",
        "etiqueta": "Diferencia de UTR mínima",
        "descripcion": "Piso de ΔUTR a favor del jugador destacado.",
    },
    "FAVORITO_UTR_MAX": {
        "tipo": "float", "default": "4.0", "categoria": "Favorito en Vivo",
        "etiqueta": "Diferencia de UTR máxima",
        "descripcion": "Techo de ΔUTR (01/08/2026: antes no tenía techo, solo el piso).",
    },
    "FAVORITO_CUOTA_MIN": {
        "tipo": "float", "default": "1.75", "categoria": "Favorito en Vivo",
        "etiqueta": "Cuota mínima del favorito",
        "descripcion": "La cuota (Odds del mensaje) tiene que ser MAYOR a esto -- estrictamente mayor, no mayor o igual.",
    },
    "FAV_DESTINO_NO_PASA": {
        "tipo": "str", "default": "ninguno", "categoria": "Favorito en Vivo",
        "etiqueta": "A dónde llega si falla Elo/FTR/matchup",
        "opciones": [
            {"valor": "ninguno", "etiqueta": "Ninguno -- no se guarda (como hoy)"},
            {"valor": "sugerencias", "etiqueta": "💡 Sugerencias"},
            {"valor": "descartes", "etiqueta": "🗑️ Descartes"},
        ],
        "descripcion": (
            "20/08/2026, a pedido de Maria. Aplica cuando pasó ΔUTR y cuota pero NO pasa "
            "Elo/FTR/matchup -- HOY (default 'ninguno') solo suena en el canal Admin con "
            "el aviso 'sin confirmar', sin guardarse en ninguna pestaña. 'sugerencias': "
            "además queda en 💡 Sugerencias. 'descartes': además queda en 🗑️ Descartes. Si "
            "falla el primer filtro (ΔUTR o cuota), no se guarda nunca, sea cual sea esta "
            "elección -- ese corte pasa en Reenvio_MTO, antes de llegar acá."
        ),
    },

    # ── Jugador en Racha ─────────────────────────────────────────────
    # ── FUENTE API-TENNIS (22/08/2026, a pedido de Rubén: "que las
    #    rachas del API me lleguen al canal de ADMINS" y "que se pueda
    #    decidir si se envía ganando, parejo o perdiendo de las dos
    #    fuentes") ───────────────────────────────────────────────────
    # Claves PROPIAS y no compartidas con las RACHA_* de la fuente de
    # Telegram, a propósito: son dos detecciones distintas (una parsea un
    # mensaje de un canal ajeno, la otra compara fotos del marcador en
    # vivo) y van a querer umbrales distintos. Compartir la clave
    # obligaría a moverlas juntas justo cuando se las quiere comparar.
    "RACHAAPI_ACTIVA": {
        "tipo": "bool", "default": "1", "categoria": "Jugador en Racha",
        "grupo": "🔌 1) Interruptores maestros, EN CASCADA (si el de arriba está apagado, nada de lo de abajo importa)",
        "etiqueta": "🔌 EL interruptor de TODO 'Jugador en Racha' (más arriba que el de abajo)",
        "descripcion": (
            "⚠️ DESCRIPCIÓN CORREGIDA EL 29/08/2026 -- la anterior decía 'avisa solo al canal "
            "Admin, nunca a los grupos' y ESO YA NO ES CIERTO, quedó escrita de una versión "
            "vieja del código. Hoy este apagado corta TODO 'Jugador en Racha' de raíz, antes de "
            "que se llegue a mirar ninguna casilla de más abajo (Tabla Maestra, lista fija, "
            "ranking, TODAS). El mensaje sale por 'notificar_categoria(\"racha_detallada\", ...)', "
            "que reparte según la fila 'Racha detallada' de Admin → Permisos → Matriz de envío "
            "-- puede ir al canal Admin, a los grupos, a los dos, o a ninguno si esa fila no "
            "tiene ningún canal tildado. Esta casilla no elige el canal, solo si sale o no sale. "
            "APAGADO: las rachas se siguen detectando y guardando en la pestaña 'Jugador en "
            "Racha' → 'Racha detallada' (y en el Historial), pero no suena en NINGÚN lado. Es la "
            "PRIMERA casilla a mirar si el canal se quedó mudo de golpe -- antes que umbral, "
            "mínimo de casos, o cuál de los tres modos está activo."
        ),
    },
    # ── EL RANKING DECIDE QUÉ SUENA (29/08/2026, a pedido de Rubén:
    #    "solo tiene que sonar las siete primeras") ──────────────────
    # Antes el aviso lo decidían nueve casillas puestas a mano (estado,
    # género, seis de superficie, racha exacta, y que el quiebre cerrara
    # set o partido). Cada una era una hipótesis sobre qué funciona, sin
    # medir. Ahora la decisión es una sola: ¿la racha cae en alguno de
    # los patrones que la pestaña "Mejores grupos" tiene arriba?
    #
    # No se pierde ningún criterio: los patrones se arman sobre las
    # MISMAS dimensiones que esas casillas filtraban. Lo que cambia es
    # quién elige la combinación -- ya no una persona, sino el piso de
    # acierto medido sobre la propia muestra del proyecto.
    "RACHAAPI_GRUPOS_ACTIVO": {
        "tipo": "bool", "default": "1", "categoria": "Jugador en Racha",
        "grupo": "🔌 1) Interruptores maestros, EN CASCADA (si el de arriba está apagado, nada de lo de abajo importa)",
        "etiqueta": "🔌 2do interruptor: avisar SOLO si la racha cae en uno de los mejores patrones",
        "descripcion": (
            "El SEGUNDO escalón de la cascada, no el primero -- el primero es la casilla de "
            "arriba (RACHAAPI_ACTIVA), que corta TODO antes de que se llegue acá. "
            "Con ÉSTA apagada, no importa que Tabla Maestra esté prendida más abajo: el código "
            "ni la mira, y vuelve derecho al MODO CLÁSICO de antes del 28/08 (nueve casillas de "
            "estado/género/superficie, hoy ocultas del panel porque no se usan -- se pueden "
            "traer de vuelta desde CLAVES_OCULTAS en el código si algún día hicieran falta). "
            "Esto fue justo lo que pasó el 29/08/2026: la Tabla Maestra estaba prendida y no "
            "sonaba nada porque ÉSTA estaba apagada -- casi una hora buscando el problema en el "
            "lugar equivocado. Si algo dejó de sonar de golpe, mirá primero RACHAAPI_ACTIVA y "
            "después ÉSTA, antes que ninguna otra casilla de acá abajo. "
            "PRENDIDO (default desde el 29/08/2026, y lo que se usa hoy): una racha suena "
            "únicamente si encaja en alguno de los patrones de la Tabla Maestra. Es el mismo "
            "cruce que hace la caja 'En vivo ahora' de la pestaña 'Jugador en Racha' → 'Mejores "
            "grupos', así que lo que suena en Telegram es exactamente lo que ahí se ve."
        ),
    },
    "RACHAAPI_GRUPOS_TOP": {
        # 28/08/2026: default bajado de 7 a 6 y despues a 5, a pedido de
        # Maria, tras revisar el ranking de patrones a mano (descartando
        # VISITA y PAR/IMPAR como condiciones propias, y despues sacando
        # un 6to patron que resulto solapado en mas de la mitad de su
        # muestra con otro ya elegido -- ver notas de esa sesion). Si mas
        # adelante se agrega otro patron limpio, subir este numero.
        "tipo": "int", "default": "5", "categoria": "Jugador en Racha",
        "grupo": "🔄 2c) MODO 3 de 3 — Ranking dinámico (se usa solo si los dos modos de arriba están apagados)",
        "etiqueta": "Cuántos patrones del ranking hacen sonar",
        "descripcion": (
            "Los N primeros de 'Mejores grupos'. 5 es el default actual (bajado de 7 el "
            "28/08/2026 tras descartar dos patrones redundantes), así que con "
            "el default lo que suena y lo que se ve son la misma lista. "
            "SUBIRLO agrega patrones peores (la lista está ordenada de mejor a peor), así que "
            "suma volumen a costa de acierto. BAJARLO a 3 o 4 deja solo los de arriba. "
            "OJO: los patrones más específicos son los de arriba y también los de menos "
            "muestra; el de más volumen suele ser el más genérico y estar último."
        ),
    },
    "RACHAAPI_GRUPOS_MUESTRA_MINIMA": {
        "tipo": "int", "default": "8", "categoria": "Jugador en Racha",
        "grupo": "🔄 2c) MODO 3 de 3 — Ranking dinámico (se usa solo si los dos modos de arriba están apagados)",
        "etiqueta": "Casos mínimos para que un patrón pueda hacer sonar",
        "descripcion": (
            "Cuántas rachas medidas necesita una combinación para entrar al ranking. 8 es el "
            "mismo piso que usa la pestaña, así que con el default las dos listas coinciden "
            "exactamente -- si acá se sube, van a diferir y la pestaña va a mostrar patrones "
            "que no suenan. "
            "Con 8 casos el margen de error es de ±27 puntos: un patrón que mide 78% puede ser "
            "un 50% real. Desde 30 el número empieza a ser sólido y desde 100 es firme (ver la "
            "columna Fiabilidad de la pestaña). Subir esto a 30 hace sonar menos y mejor."
        ),
    },
    "RACHAAPI_GRUPOS_TIPO": {
        "tipo": "str", "default": "BREAK", "categoria": "Jugador en Racha",
        "grupo": "🔄 2c) MODO 3 de 3 — Ranking dinámico (se usa solo si los dos modos de arriba están apagados)",
        "etiqueta": "Qué tipo de apuesta compite por los puestos",
        "opciones": [
            {"valor": "BREAK", "etiqueta": "Solo BREAK — resta buscando el quiebre (por defecto)"},
            {"valor": "HOLD", "etiqueta": "Solo HOLD — saca buscando sostener"},
            {"valor": "todos", "etiqueta": "Los dos — con los puestos de arriba PARA CADA UNO"},
        ],
        "descripcion": (
            "El default es BREAK (28/08/2026, Rubén: 'solo dejemos patrones de break'). Con "
            "eso, lo único que se apuesta son quiebres: los HOLD se siguen midiendo y "
            "guardando, pero no suenan. "
            "'Los dos' NO mezcla las dos listas en una: le da los puestos de arriba a CADA "
            "tipo por separado, o sea 7 de BREAK y 7 de HOLD si el tope está en 7. Es a "
            "propósito. Sostener el saque es intrínsecamente más probable que quebrarlo -- y "
            "acá el que saca viene de ganar 4 juegos seguidos -- así que los patrones de HOLD "
            "tienen un acierto estructuralmente más alto. Ordenarlos juntos les daría los "
            "siete lugares a los HOLD y no quedaría ni un quiebre a la vista: el problema de "
            "hoy dado vuelta. Cada tipo compite contra los de su clase, que es la única "
            "comparación que quiere decir algo. "
            "OJO: pasarlo a 'los dos' DUPLICA el volumen del canal, y los avisos de HOLD van "
            "a mostrar porcentajes más altos que los de BREAK sin que eso signifique que sean "
            "mejor apuesta -- son dos cosas distintas y hay que compararlas por separado. "
            "La pestaña lee esta misma clave, así que lo que se ve y lo que suena son siempre "
            "la misma lista."
        ),
    },
    # 28/08/2026, a pedido de Maria: "establezcamos esos cinco" (los 5
    # patrones que quedaron sin solape entre sí tras revisar el ranking
    # dinámico a mano). El ranking dinámico había dejado colar un
    # patrón ("R4 BREAK + va perdiendo", 19 casos) que resultó ser casi
    # el mismo conjunto que otro ya elegido (18 de esos 19 partidos
    # coincidían con "R4 BREAK + Set 2 + va perdiendo") -- el dedup del
    # ranking dinámico solo descarta grupos con el MISMO conjunto EXACTO
    # de partidos, no detecta que uno sea subconjunto del otro.
    # ══ TABLA MAESTRA (29/08/2026) ═══════════════════════════════════
    #
    # Rubén, tras su análisis del Excel del 29/08: "quiero que usemos
    # esta tabla de referencias, que si alguna está por encima de 60%
    # suena, si está por debajo de 60% no suena. Las rachas siguen
    # actualizando la tabla para que suba y baje el porcentaje".
    #
    # Son 50 señales sin solapes (ver production/engines/tabla_maestra.py).
    # Reemplazan a PATRONES_FIJOS_BREAK como fuente del aviso.
    "RACHA_TMBREAK_AVISA": {
        "tipo": "bool", "default": "1", "categoria": "Jugador en Racha",
        "grupo": "🎯 Tabla Maestra de BREAK — Favorito en Racha",
        "etiqueta": "Mandar a Telegram los avisos '🔥 RACHA BREAK — PASA TABLA MAESTRA' (06/09/2026)",
        "descripcion": (
            "APAGADO: esos mensajes dejan de salir al canal. La tabla SIGUE midiendo y "
            "acumulando casos igual -- no se pierde ni un break, las 20 reglas se siguen "
            "llenando y la pestaña se sigue viendo; lo único que se corta es el envío "
            "(punto 10 del pedido original: 'Telegram y almacenamiento son cosas "
            "distintas'). PRENDIDO (default): salen como hasta ahora. "
            "POR QUÉ EXISTE (a pedido de Rubén, 06/09/2026): hasta hoy no había forma de "
            "apagarlos. RACHA_TMBREAK_ACTIVO (acá abajo) parece esa llave y NO lo es -- "
            "solo decide si después corre el sistema viejo, el envío de este mensaje "
            "quedaba afuera de esa condición y salía siempre. Y bajar la categoría en la "
            "Matriz de envío tampoco servía: los dos mensajes (éste y Racha Detallada) "
            "salen por la MISMA categoría 'racha_detallada', así que apagarla ahí "
            "silenciaba los dos. "
            "OJO CON LA COMBINACIÓN: si además RACHA_TMBREAK_ACTIVO está PRENDIDO, para "
            "los BREAK de favorito no va a sonar NADA -- ni éste (apagado acá) ni Racha "
            "Detallada (bloqueada por esa otra casilla). Para quedarte solo con Racha "
            "Detallada hay que apagar ésta Y dejar RACHA_TMBREAK_ACTIVO apagado."
        ),
    },
    "RACHA_TMBREAK_ACTIVO": {
        "tipo": "bool", "default": "1", "categoria": "Jugador en Racha",
        "grupo": "🎯 Tabla Maestra de BREAK — Favorito en Racha",
        "etiqueta": "La Tabla Maestra de BREAK reemplaza al sistema viejo (04/09/2026)",
        "descripcion": (
            "PRENDIDO (default, es lo que pidió Rubén, confirmado por Maria: 'Tabla "
            "Maestra de BREAK va a reemplazar a Racha Detallada'): para TODO BREAK de "
            "un favorito prepartido en racha (el universo de las 20 reglas), el "
            "sistema viejo de acá abajo -- Tabla Maestra por reglas, la de 185 fijas, "
            "lista fija, modo clásico -- deja de decidir. No importa si la Tabla "
            "Maestra de BREAK terminó avisando (una de las 20 reglas estaba ACTIVA) o "
            "si se quedó callada (ninguna regla le correspondía, o la que le tocaba "
            "estaba INACTIVA/MADURANDO): en los dos casos, el sistema viejo tampoco "
            "avisa para ese caso puntual -- es un reemplazo, no una segunda vuelta. "
            "APAGADO: vuelve al comportamiento de antes de esta fecha, con los dos "
            "sistemas avisando cada uno por su cuenta para el mismo BREAK -- útil solo "
            "si hace falta comparar los dos en algún momento, no para uso normal. "
            "IMPORTANTE: HOLD nunca lo tocó ni lo toca esta llave -- la Tabla Maestra "
            "de BREAK no evalúa HOLD, así que esos avisos siguen saliendo del sistema "
            "viejo sin cambios, prendida o apagada esta casilla."
        ),
    },
    "RACHAAPI_TABLA_ACTIVA": {
        "tipo": "bool", "default": "1", "categoria": "Jugador en Racha",
        "grupo": "🏆 2) Tabla Maestra — el modo que se usa hoy",
        "etiqueta": "Usar la TABLA MAESTRA de 50 señales (29/08/2026)",
        "descripcion": (
            "⚠️ NO HACE NADA SOLA: necesita RACHAAPI_GRUPOS_ACTIVO también PRENDIDO (es el "
            "interruptor maestro, ver su descripción). Con GRUPOS_ACTIVO apagado, esta "
            "casilla puede estar en 1 y el canal igual se queda mudo -- pasó el 29/08/2026, "
            "costó un buen rato encontrarlo. Si prendiste ésta y no suena nada, mirá esa "
            "casilla ANTES que cualquier otra cosa (umbral, mínimo de casos, etc). "
            "PRENDIDO: el aviso sale de las 50 señales del análisis del 29/08/2026, "
            "medidas contra la tabla en vivo. Una señal suena si su acierto llega al "
            "umbral de abajo, y DEJA DE SONAR sola si baja — y vuelve a sonar sola si "
            "sube de nuevo. No hay reactivación manual, a diferencia del corte "
            "prospectivo de la lista fija. "
            "Por defecto suenan todas las que pasen el umbral, sin tope de cantidad. "
            "TIENE PRIORIDAD sobre 'Usar la LISTA FIJA': si las dos están prendidas, "
            "manda ésta y los 10 patrones fijos quedan sin efecto. "
            "APAGADO, vuelve a mandar la lista fija (o el ranking dinámico si esa "
            "también está apagada)."
        ),
    },
    "RACHAAPI_TABLA_TOP_N": {
        # DEFAULT 0 = SIN TOPE (29/08/2026, corrección de Rubén: "no
        # deben sonar las más altas, deben sonar las que estén por
        # encima del 60% nomás").
        #
        # El tope contradecía la propia regla. Si el criterio es "pasa
        # el 60%", que una señal sea la 3ra o la 15va no cambia que lo
        # pase: un tope silenciaba señales que cumplían, y encima de
        # forma inestable, porque el puesto se mueve solo cuando otra
        # señal sube o baja aunque ésta no haya cambiado en nada.
        #
        # Se deja la clave, no se borra: sirve como freno de mano si
        # algún día el volumen del canal se dispara. Pero el default es
        # 0 y la regla efectiva es solo umbral + mínimo de casos.
        "tipo": "int", "default": "0", "categoria": "Jugador en Racha",
        "grupo": "🏆 2) Tabla Maestra — el modo que se usa hoy",
        "etiqueta": "Tope de señales que hacen sonar (0 = sin tope, es el default)",
        "descripcion": (
            "0 (default): NO hay tope. Suenan TODAS las que pasen el umbral y el mínimo "
            "de casos, sin importar el puesto. Es la regla que pidió Rubén el 29/08: "
            "'deben sonar las que estén por encima del 60% nomás'. Con el Excel de ese "
            "día eso serían 18 señales. "
            "Poner un número > 0 es un FRENO DE MANO por si el volumen del canal se "
            "vuelve inmanejable, no parte del criterio. Si se usa, el tope se cuenta "
            "solo entre las que ya pasaron umbral y mínimo — nunca sobre las 50, "
            "porque arriba de esa lista quedan señales de 1 o 2 casos al 100% que no "
            "suenan igual y se comerían los lugares. "
            "OJO: con tope, qué suena depende de cómo se mueven las OTRAS señales. Una "
            "señal que no cambió en nada puede dejar de sonar porque otra la pasó. Sin "
            "tope eso no ocurre: cada señal depende solo de su propio número."
        ),
    },
    "RACHAAPI_TABLA_UMBRAL": {
        "tipo": "str", "default": "60", "categoria": "Jugador en Racha",
        "grupo": "🏆 2) Tabla Maestra — el modo que se usa hoy",
        "etiqueta": "Acierto mínimo para que una señal suene (%)",
        "descripcion": (
            "Se mide sobre el ACIERTO CRUDO, no sobre el piso de Wilson. Es una decisión "
            "explícita de Rubén tomada con el dato a la vista: con piso de Wilson ≥60% no "
            "sonaría NINGUNA de las 50 (la mejor llega a 54,8%, y el propio Excel lo "
            "dice en su celda de resumen: SUENAN = 0). Con acierto crudo pasan 18. "
            "OJO con el efecto de las muestras chicas: las 18 que pasan tienen mediana de "
            "11,5 casos. Una señal en 6-4 (60,0%) se apaga con UNA derrota; una en 14-4 "
            "(77,8%) necesita seis seguidas. Las de muestra chica van a prenderse y "
            "apagarse casi partido por partido. Si eso molesta, la palanca es subir el "
            "mínimo de casos de abajo, no mover este número."
        ),
    },
    "RACHAAPI_TABLA_MINIMO": {
        "tipo": "int", "default": "10", "categoria": "Jugador en Racha",
        "grupo": "🏆 2) Tabla Maestra — el modo que se usa hoy",
        "etiqueta": "Casos mínimos para que una señal de la tabla pueda sonar",
        "descripcion": (
            "10 es el mismo piso que usó el Excel para entrar al ranking, así que con el "
            "default la tabla en pantalla y la del análisis son comparables. "
            "Con 10 casos el margen de error es enorme: una señal que mide 70% puede ser "
            "un 40% real. Subirlo es la forma correcta de bajar el ruido sin tocar el "
            "umbral — con 14 quedan 4 señales de las 18, y son las de muestra más "
            "grande, que son también las que menos oscilan."
        ),
    },
    "RACHAAPI_GRUPOS_FIJOS": {
        "tipo": "bool", "default": "1", "categoria": "Jugador en Racha",
        "grupo": "📌 2b) MODO 2 de 3 — Lista fija (se usa solo si la Tabla Maestra de arriba está apagada)",
        "etiqueta": "Usar la LISTA FIJA de 4 patrones en vez del ranking automático",
        "descripcion": (
            "PRENDIDO (default desde el 28/08/2026): el aviso sale de patrones elegidos a "
            "mano y congelados en el código (PATRONES_FIJOS_BREAK, en racha_api.py), no de "
            "una búsqueda que se rehace sola con cada partido nuevo. Quedan 4 (un 5to, "
            "'R4 + Hard + sets empatados', se sacó el 28/08/2026: la condición de sets "
            "empatados no se podía expresar con lo que este archivo mide en vivo -- ver el "
            "comentario en PATRONES_FIJOS_BREAK). Los 4 son: "
            "1) R4 + Set 2 + va perdiendo, 2) R9, 3) R8 + superficie General, "
            "4) R4 + Hombre + Set 3. "
            "El ACIERTO de cada uno se sigue recalculando contra la tabla en vivo cada vez "
            "(la muestra crece con cada partido nuevo, igual que antes) -- lo que NO se "
            "recalcula es CUÁLES SON los patrones: eso quedó decidido a mano y no cambia "
            "solo. Si aparece un patrón mejor, hay que agregarlo a mano al código, revisando "
            "primero que no se solape con los que ya están y que la condición se pueda medir "
            "de verdad con las dimensiones disponibles (ver PATRONES_FIJOS_BREAK). "
            "APAGADO, vuelve a mandar RACHAAPI_GRUPOS_TOP/MUESTRA_MINIMA/TIPO de arriba: el "
            "ranking dinámico que busca entre todas las combinaciones posibles cada vez. "
            "La pestaña 'Mejores grupos' muestra ESTA lista fija mientras esto esté prendido "
            "(no el ranking dinámico) -- lo que se ve en pantalla es siempre lo mismo que "
            "decide el aviso."
        ),
    },
    # ══ CORTE PROSPECTIVO (29/08/2026) ═══════════════════════════════
    #
    # Rubén: "en caso de que alguna haga bajar ese porcentaje de
    # victoria por debajo del 60% no se debe seguir enviando".
    #
    # DOS CORRECCIONES sobre el pedido literal, las dos medidas antes de
    # implementarlas:
    #
    # 1) EL CONTADOR ARRANCA EN CERO, no con el histórico. Los diez
    #    patrones se eligieron mirando esas mismas filas, así que el
    #    histórico les da un colchón que no mide calidad. Con él,
    #    "R4 + va perdiendo" aguanta 6 derrotas seguidas antes de bajar
    #    del 60% y "R8" se apaga con una sola.
    #
    # 2) SE MIRA EL TECHO DEL INTERVALO, no el acierto pelado. Un patrón
    #    que de verdad acierta 65% toca por debajo del 60% en algún
    #    momento de sus primeras 80 señales el 54% de las veces por puro
    #    azar; apagarlo ahí mataría a la mitad de los patrones sanos.
    #    Con el techo ese falso corte baja al 3%.
    "RACHAAPI_CORTE_ID": {
        "tipo": "int", "default": "0", "categoria": "Jugador en Racha",
        "grupo": "📌 2b) MODO 2 de 3 — Lista fija (se usa solo si la Tabla Maestra de arriba está apagada)",
        "etiqueta": "Corte prospectivo: id de racha_api desde el que se mide",
        "descripcion": (
            "El id más alto que tenía racha_api en el momento de congelar los patrones. "
            "Solo las filas POSTERIORES cuentan para decidir si un patrón sigue avisando; "
            "las anteriores se siguen mostrando en la pestaña como referencia histórica. "
            "EN 0 (default) no hay medida prospectiva y los diez patrones quedan en RODAJE, "
            "o sea que todo funciona igual que antes de este cambio -- un despliegue no "
            "cambia en silencio qué suena. Se sella corriendo "
            "scripts/sellar_corte_prospectivo.py en la consola de Railway, UNA sola vez. "
            "Volver a sellarlo más adelante borra la medida acumulada y empieza de cero."
        ),
    },
    "RACHAAPI_CORTE_MINIMO": {
        "tipo": "int", "default": "20", "categoria": "Jugador en Racha",
        "grupo": "📌 2b) MODO 2 de 3 — Lista fija (se usa solo si la Tabla Maestra de arriba está apagada)",
        "etiqueta": "Corte prospectivo: señales mínimas antes de poder apagar un patrón",
        "descripcion": (
            "Mientras un patrón tenga MENOS de estas señales prospectivas queda en RODAJE: "
            "avisa y se mide, pero no se puede apagar. Sin este mínimo, tres derrotas "
            "seguidas al principio -- que en 10 señales pasan todo el tiempo -- matarían "
            "un patrón sano antes de haberlo mirado. Con 20 y el criterio del techo, la "
            "probabilidad de apagar por error un patrón que de verdad vale 65% es del 3%."
        ),
    },
    "RACHAAPI_CORTE_UMBRAL": {
        "tipo": "float", "default": "60", "categoria": "Jugador en Racha",
        "grupo": "📌 2b) MODO 2 de 3 — Lista fija (se usa solo si la Tabla Maestra de arriba está apagada)",
        "etiqueta": "Corte prospectivo: acierto mínimo exigido (%)",
        "descripcion": (
            "Un patrón se APAGA cuando ya tiene el mínimo de señales prospectivas Y el "
            "TECHO de su intervalo de Wilson al 95% queda por debajo de este número -- "
            "o sea, cuando ni el escenario optimista alcanza el umbral. No se apaga por "
            "que el acierto medido baje: eso pasa por azar todo el tiempo. "
            "OJO: esto mira SOLO el acierto, porque racha_api no guarda la cuota del "
            "juego siguiente (ninguna fuente del proyecto la publica). Un patrón que "
            "acierta 65% pero al que siempre le pagan 1,30 pierde plata y este corte no "
            "lo va a detectar. Esa parte hay que seguirla a mano en el registro."
        ),
    },
    "RACHAAPI_CORTE_REACTIVADOS": {
        "tipo": "texto", "default": "", "categoria": "Jugador en Racha",
        "grupo": "📌 2b) MODO 2 de 3 — Lista fija (se usa solo si la Tabla Maestra de arriba está apagada)",
        "etiqueta": "Corte prospectivo: patrones reactivados a mano",
        "descripcion": (
            "Nombres de patrones apagados que se quieren volver a prender, separados por "
            "comas y escritos igual que en la pestaña (ej: 'R8 BREAK, R9 BREAK'). "
            "NO HAY REENCENDIDO AUTOMÁTICO a propósito: si un patrón volviera solo en "
            "cuanto su techo rebota por encima del umbral, tendríamos el mismo problema "
            "de oscilación que el corte por acierto pelado, nada más que por el otro "
            "lado. Reactivar es una decisión, y queda escrita acá."
        ),
    },
    "RACHAAPI_SOLO_CAMBIO_LADO": {
        "tipo": "bool", "default": "1", "categoria": "Jugador en Racha",
        "grupo": "⚙️ 3) Comunes",
        "etiqueta": "Racha detallada: avisar SOLO en juegos que caen en cambio de lado",
        "descripcion": (
            "24/08/2026, a pedido de Maria. Prendido (default), el aviso SOLO suena cuando el "
            "juego que se acaba de cerrar deja el total de juegos del set en un número IMPAR "
            "(1, 3, 5, 7, 9, 11...) -- el momento exacto en que arranca la pausa del cambio de "
            "lado, de ~90 segundos. Ejemplo: si el marcador queda 2-1 (van 3 juegos) avisa; si "
            "queda 2-2 (van 4) no avisa. TAMBIÉN avisa si el marcador del set queda en 0-0 -- "
            "eso significa que el juego que completó la racha fue el que CERRÓ el set anterior "
            "(caso Hassan, 24/08/2026: racha completada justo en el juego que cerraba el set, "
            "el marcador ya mostraba el 0-0 del set nuevo y no sonó porque 0 es par). Cerrar un "
            "set siempre trae pausa, cuente lo que cuente la paridad. No importa en qué racha "
            "estés -- es una condición aparte, no la reemplaza. Apagado, vuelve a avisar sin "
            "mirar esto (como era antes de hoy)."
        ),
    },
    # ── ESTADOS DE LA RACHA DETALLADA: TRES CASILLAS ─────────────────
    # 23/08/2026, a pedido de Rubén: "que sean mejor tres casillas".
    # Antes era RACHAAPI_ESTADOS, un texto libre separado por comas. Un
    # valor mal tipeado ('ganado') no coincidía con nada y esas rachas
    # dejaban de sonar EN SILENCIO -- se veía igual que si la estrategia
    # estuviera rota, que fue exactamente la conclusión a la que llegó
    # Rubén. Con casillas ese error no se puede cometer.
    #
    # Son claves PROPIAS de la racha detallada. La otra fuente sigue con
    # RACHA_ESTADOS, aparte: nunca compartieron valor, solo el default.
    #
    # El default de 'ganando' es 0 y el de los otros dos es 1, para no
    # cambiarle el comportamiento a nadie al desplegar: es exactamente
    # lo que hacía el default viejo 'perdiendo,parejo'. Quien ya lo tenía
    # editado, ver _migrar_estados_racha_api() más abajo.
    "RACHAAPI_AV_PERDIENDO": {
        "tipo": "bool", "default": "1", "categoria": "Jugador en Racha",
        "grupo": "⚠️ Racha detallada — RESPALDO MANUAL (INACTIVO mientras el ranking esté prendido arriba)",
        "etiqueta": "Racha detallada: avisar cuando va PERDIENDO",
        "descripcion": (
            "⚠️ MODO CLÁSICO — INACTIVA mientras 'Avisar SOLO si la racha cae en uno de los mejores patrones' esté prendido (sección 🏆 Racha detallada — decide el ranking de patrones, más arriba). Solo entra en juego si ese interruptor está APAGADO. El jugador en racha va abajo en el marcador (primero se miran los sets y, si "
            "están iguales, los juegos del set en curso). Es el caso de la remontada: viene "
            "de atrás y se llevó los últimos juegos al hilo."
        ),
    },
    "RACHAAPI_AV_PAREJO": {
        "tipo": "bool", "default": "1", "categoria": "Jugador en Racha",
        "grupo": "⚠️ Racha detallada — RESPALDO MANUAL (INACTIVO mientras el ranking esté prendido arriba)",
        "etiqueta": "Racha detallada: avisar cuando va PAREJO",
        "descripcion": (
            "⚠️ MODO CLÁSICO — INACTIVA mientras 'Avisar SOLO si la racha cae en uno de los mejores patrones' esté prendido (sección 🏆 Racha detallada — decide el ranking de patrones, más arriba). Solo entra en juego si ese interruptor está APAGADO. Sets y juegos empatados. Ojo: en este estado no se puede saber de qué lado del "
            "marcador está el jugador, así que la columna LOCAL/VISITA del Excel queda vacía "
            "en esas filas."
        ),
    },
    "RACHAAPI_AV_GANANDO": {
        "tipo": "bool", "default": "0", "categoria": "Jugador en Racha",
        "grupo": "⚠️ Racha detallada — RESPALDO MANUAL (INACTIVO mientras el ranking esté prendido arriba)",
        "etiqueta": "Racha detallada: avisar cuando va GANANDO",
        "descripcion": (
            "⚠️ MODO CLÁSICO — INACTIVA mientras 'Avisar SOLO si la racha cae en uno de los mejores patrones' esté prendido (sección 🏆 Racha detallada — decide el ranking de patrones, más arriba). Solo entra en juego si ese interruptor está APAGADO. El jugador en racha va arriba en el marcador. VIENE APAGADO de fábrica y ése fue "
            "el motivo de que estas rachas no sonaran (23/08/2026, Rubén: 'no me está "
            "notificando cuando va ganando'). El criterio original era que la racha del que "
            "ya va ganando aporta menos información que la del que remonta -- pero es un "
            "supuesto, no algo medido. Prenderlo es la forma de juntar muestra y comprobarlo."
        ),
    },
    # ── A QUIÉN AVISAR: GÉNERO (23/08/2026, pedido de Rubén; unificado
    #    24/08/2026 a pedido de Maria) ─────────────────────────────────
    # Hasta hoy eran 4 casillas: SOSTIENE hombres/mujeres + QUIEBRE
    # hombres/mujeres. Nunca hizo falta controlar el género distinto
    # según el tipo de apuesta -- se unifican en 2, una por sexo, válidas
    # para los dos tipos de aviso. Se combinan con RACHAAPI_AV_SIN_GENERO
    # (si el género no llegó, cae ahí en vez de en estas dos).
    "RACHAAPI_AV_HOMBRES": {
        "tipo": "bool", "default": "1", "categoria": "Jugador en Racha",
        "grupo": "⚠️ Racha detallada — RESPALDO MANUAL (INACTIVO mientras el ranking esté prendido arriba)",
        "etiqueta": "Avisar: partidos de hombres (SOSTIENE y QUIEBRE que cierra)",
        "descripcion": (
            "⚠️ MODO CLÁSICO — INACTIVA mientras 'Avisar SOLO si la racha cae en uno de los mejores patrones' esté prendido (sección 🏆 Racha detallada — decide el ranking de patrones, más arriba). Solo entra en juego si ese interruptor está APAGADO. Vale para los dos tipos de aviso de un partido masculino: SOSTIENE (el próximo "
            "juego lo saca el de la racha) y QUIEBRE que cierra set/partido (ver "
            "RACHAAPI_RACHA_MINIMA -- solo cuenta en la racha mínima). 24/08/2026: antes eran "
            "2 casillas separadas por tipo de apuesta; se unificaron porque nunca hizo falta "
            "prender una y apagar la otra."
        ),
    },
    "RACHAAPI_AV_MUJERES": {
        "tipo": "bool", "default": "1", "categoria": "Jugador en Racha",
        "grupo": "⚠️ Racha detallada — RESPALDO MANUAL (INACTIVO mientras el ranking esté prendido arriba)",
        "etiqueta": "Avisar: partidos de mujeres (SOSTIENE y QUIEBRE que cierra)",
        "descripcion": (
            "⚠️ MODO CLÁSICO — INACTIVA mientras 'Avisar SOLO si la racha cae en uno de los mejores patrones' esté prendido (sección 🏆 Racha detallada — decide el ranking de patrones, más arriba). Solo entra en juego si ese interruptor está APAGADO. Lo mismo que RACHAAPI_AV_HOMBRES pero en partidos femeninos. Separada a propósito "
            "para poder prender un sexo y apagar el otro sin tocar código -- por ejemplo, para "
            "que hoy solo suenen los hombres, apagá esta sola y dejá la de hombres prendida."
        ),
    },
    "RACHAAPI_AV_SIN_GENERO": {
        "tipo": "bool", "default": "0", "categoria": "Jugador en Racha",
        "grupo": "⚠️ Racha detallada — RESPALDO MANUAL (INACTIVO mientras el ranking esté prendido arriba)",
        "etiqueta": "Avisar: SOSTIENE o QUIEBRE aunque no se sepa el género",
        "descripcion": (
            "⚠️ MODO CLÁSICO — INACTIVA mientras 'Avisar SOLO si la racha cae en uno de los mejores patrones' esté prendido (sección 🏆 Racha detallada — decide el ranking de patrones, más arriba). Solo entra en juego si ese interruptor está APAGADO. Las casillas de arriba piden saber si es masculino o femenino. Cuando el género "
            "no llegó, ese aviso no cae en ninguna de las dos y no suena. Prendé esto para "
            "recibirlos igual. Viene apagado porque hubo un bug viejo en el proyecto por "
            "suponer masculino cuando el dato faltaba, y acá el aviso diría el sexo equivocado."
        ),
    },
    # ── EN QUÉ SUPERFICIE AVISA, POR TIPO DE APUESTA (24/08/2026, a
    #    pedido de Maria: "quiero elegir que suenen las sostenidas solo
    #    en dura y que suene el quiebre cuando es tierra, pero que lo
    #    pueda editar, sumar superficies y sumar en qué puntos") ───────
    # SOSTIENE y QUIEBRE son apuestas distintas -- no tienen por qué
    # compartir las mismas superficies habilitadas, de ahí 3 casillas
    # para cada una en vez de una lista compartida. Se combinan con O:
    # alcanza con que la superficie del partido esté marcada para ESE
    # tipo de apuesta. "Sumar superficies" es prender más casillas de
    # las que vienen por default; "sumar en qué puntos" es hacer lo
    # mismo del otro lado (SOSTIENE/QUIEBRE) -- las 6 son independientes,
    # se puede prender cualquier combinación.
    "RACHAAPI_AV_HOLD_TIERRA": {
        "tipo": "bool", "default": "0", "categoria": "Jugador en Racha",
        "grupo": "⚠️ Racha detallada — RESPALDO MANUAL (INACTIVO mientras el ranking esté prendido arriba)",
        "etiqueta": "Avisar SOSTIENE en Tierra",
        "descripcion": "El próximo juego lo saca el de la racha (SOSTIENE) y la cancha es tierra.",
    },
    "RACHAAPI_AV_HOLD_DURA": {
        "tipo": "bool", "default": "1", "categoria": "Jugador en Racha",
        "grupo": "⚠️ Racha detallada — RESPALDO MANUAL (INACTIVO mientras el ranking esté prendido arriba)",
        "etiqueta": "Avisar SOSTIENE en Dura",
        "descripcion": "El próximo juego lo saca el de la racha (SOSTIENE) y la cancha es dura.",
    },
    "RACHAAPI_AV_HOLD_CESPED": {
        "tipo": "bool", "default": "0", "categoria": "Jugador en Racha",
        "grupo": "⚠️ Racha detallada — RESPALDO MANUAL (INACTIVO mientras el ranking esté prendido arriba)",
        "etiqueta": "Avisar SOSTIENE en Césped",
        "descripcion": "El próximo juego lo saca el de la racha (SOSTIENE) y la cancha es césped.",
    },
    "RACHAAPI_AV_BREAK_TIERRA": {
        "tipo": "bool", "default": "1", "categoria": "Jugador en Racha",
        "grupo": "⚠️ Racha detallada — RESPALDO MANUAL (INACTIVO mientras el ranking esté prendido arriba)",
        "etiqueta": "Avisar QUIEBRE en Tierra",
        "descripcion": (
            "⚠️ MODO CLÁSICO — INACTIVA mientras 'Avisar SOLO si la racha cae en uno de los mejores patrones' esté prendido (sección 🏆 Racha detallada — decide el ranking de patrones, más arriba). Solo entra en juego si ese interruptor está APAGADO. ⚠️ MODO CLÁSICO — INACTIVA mientras 'Avisar SOLO si la racha cae en uno de los mejores patrones' esté prendido (sección 🏆 Racha detallada — decide el ranking de patrones, más arriba). Solo entra en juego si ese interruptor está APAGADO. ⚠️ MODO CLÁSICO — INACTIVA mientras 'Avisar SOLO si la racha cae en uno de los mejores patrones' esté prendido (sección 🏆 Racha detallada — decide el ranking de patrones, más arriba). Solo entra en juego si ese interruptor está APAGADO. ⚠️ MODO CLÁSICO — INACTIVA mientras 'Avisar SOLO si la racha cae en uno de los mejores patrones' esté prendido (sección 🏆 Racha detallada — decide el ranking de patrones, más arriba). Solo entra en juego si ese interruptor está APAGADO. El próximo juego lo resta el de la racha (QUIEBRE que define, ver "
            "RACHAAPI_RACHA_MINIMA -- solo cuenta en la racha mínima) y la cancha es tierra."
        ),
    },
    "RACHAAPI_AV_BREAK_DURA": {
        "tipo": "bool", "default": "0", "categoria": "Jugador en Racha",
        "grupo": "⚠️ Racha detallada — RESPALDO MANUAL (INACTIVO mientras el ranking esté prendido arriba)",
        "etiqueta": "Avisar QUIEBRE en Dura",
        "descripcion": (
            "⚠️ MODO CLÁSICO — INACTIVA mientras 'Avisar SOLO si la racha cae en uno de los mejores patrones' esté prendido (sección 🏆 Racha detallada — decide el ranking de patrones, más arriba). Solo entra en juego si ese interruptor está APAGADO. El próximo juego lo resta el de la racha (QUIEBRE que define) y la cancha es dura."
        ),
    },
    "RACHAAPI_AV_BREAK_CESPED": {
        "tipo": "bool", "default": "0", "categoria": "Jugador en Racha",
        "grupo": "⚠️ Racha detallada — RESPALDO MANUAL (INACTIVO mientras el ranking esté prendido arriba)",
        "etiqueta": "Avisar QUIEBRE en Césped",
        "descripcion": (
            "⚠️ MODO CLÁSICO — INACTIVA mientras 'Avisar SOLO si la racha cae en uno de los mejores patrones' esté prendido (sección 🏆 Racha detallada — decide el ranking de patrones, más arriba). Solo entra en juego si ese interruptor está APAGADO. El próximo juego lo resta el de la racha (QUIEBRE que define) y la cancha es "
            "césped."
        ),
    },
    "RACHAAPI_AV_SUP_DESCONOCIDA": {
        "tipo": "bool", "default": "0", "categoria": "Jugador en Racha",
        "grupo": "⚠️ Racha detallada — RESPALDO MANUAL (INACTIVO mientras el ranking esté prendido arriba)",
        "etiqueta": "Avisar aunque no se sepa la superficie ('general')",
        "descripcion": (
            "⚠️ MODO CLÁSICO — INACTIVA mientras 'Avisar SOLO si la racha cae en uno de los mejores patrones' esté prendido (sección 🏆 Racha detallada — decide el ranking de patrones, más arriba). Solo entra en juego si ese interruptor está APAGADO. Cuando el sistema no logra determinar la cancha real de un torneo, la superficie "
            "queda en 'general' (ver _superficie_de en racha_api.py). Ninguna de las 6 casillas "
            "de arriba cuenta ese caso -- tienen que decir Tierra/Dura/Césped, específicamente. "
            "Prendé esto si igual querés recibir esos avisos aunque no se sepa la cancha. Viene "
            "apagado a propósito: el pedido era avisar con la superficie confirmada, así que "
            "'no sé la cancha' no debería colar por default."
        ),
    },
    "RACHAAPI_ESTADOS": {
        "tipo": "str", "default": "perdiendo,parejo", "categoria": "Jugador en Racha",
        "etiqueta": "(reemplazada) Racha detallada: estados, campo viejo",
        "descripcion": (
            "Campo de texto que el 23/08/2026 se reemplazó por las tres casillas "
            "'Racha detallada: avisar cuando va ...'. Ya no decide nada. Se conserva "
            "porque es el origen de la migración: si alguien lo tenía editado, su valor "
            "se copió a las casillas la primera vez que arrancó la versión nueva."
        ),
    },
    "MSG_RACHAAPI": {
        "tipo": "texto", "default": "", "categoria": "Jugador en Racha",
        "etiqueta": "(reemplazada) Racha detallada: mensaje, nombre viejo",
        "descripcion": (
            "Renombrada a RACHAAPI_MENSAJE el 24/08/2026 porque el prefijo MSG_ la hacía "
            "invisible en el panel. Se conserva solo como respaldo: si tiene algo guardado, "
            "se sigue usando mientras RACHAAPI_MENSAJE esté vacía."
        ),
    },
    "RACHAAPI_DEDUP": {
        "tipo": "str", "default": "partido", "categoria": "Jugador en Racha",
        "grupo": "⚙️ 3) Comunes",
        "etiqueta": "Racha detallada: cada cuánto puede repetir un mismo partido",
        "opciones": [
            {"valor": "partido", "etiqueta": "Una sola vez por partido (por defecto)"},
            {"valor": "por_racha", "etiqueta": "Una vez por cada largo de racha (4, 5, 6...)"},
        ],
        "descripcion": (
            "'Una sola vez por partido': el primer aviso que califique y nada más, aunque la "
            "racha siga subiendo. Es el default y mantiene el canal tranquilo. "
            "'Una vez por cada largo': suena en la 4, en la 5, en la 6... sin repetir el mismo "
            "número. Cada escalón es una apuesta distinta -- con otro HOLD/BREAK y otro momento "
            "-- así que se gana información, pero el volumen se multiplica por tres o cuatro "
            "(en la muestra del 23/08 había ~3 rachas por partido). "
            "OJO con el default: la primera que califica no es la mejor. Un partido puede "
            "sonar en la racha 4 como SOSTIENE y dejar sin avisar un QUIEBRE QUE CIERRA EL "
            "PARTIDO dos juegos después."
        ),
    },
    # NO se llama MSG_* a propósito (24/08/2026). El panel aparta toda
    # clave con ese prefijo para armar la pestaña "Mensajes", que está
    # hecha para pares encabezado+frase; una plantilla entera no encaja
    # ahí y quedaba filtrada de su categoría Y sin pintar en Mensajes --
    # o sea editable en el código pero inalcanzable en pantalla. Con este
    # nombre sale como un parámetro de texto normal en "Jugador en
    # Racha", al lado del resto de la configuración de la estrategia.
    "RACHAAPI_MENSAJE": {
        "tipo": "texto", "es_mensaje": True, "default": (
            "{cab_b}\n{jugador_b} vs {rival}\n{torneo} \u00b7 {superficie}\n\n"
            "\u27a4 {entrar_b}\n\U0001F525 {racha} juegos seguidos \u00b7 {estado_b}\n"
            # 06/09/2026, a pedido de Rubén: el marcador vuelve al
            # default. TIENE QUE ESTAR ACÁ TAMBIÉN, no solo en
            # _PLANTILLA_RACHA_DEFECTO (racha_api.py): obtener_texto()
            # devuelve SIEMPRE el default de este archivo primero, así
            # que la plantilla del código nunca se llega a usar (ver la
            # nota del 29/08/2026 acá abajo -- ya pasó una vez y el
            # canal mandó la versión vieja durante semanas).
            "\U0001F4CD {score}\n\n"
            "{respaldo}\n{frase}"
        ),
        "categoria": "Jugador en Racha",
        "etiqueta": "Racha detallada: TEXTO DEL MENSAJE (editable)",
        "descripcion": (
            "⚠️ CORREGIDO EL 29/08/2026 -- este default estaba DESACTUALIZADO desde antes de "
            "esa fecha: era una plantilla vieja y más sencilla ('Marcador: {marcador} (sets "
            "{sets})...') que NUNCA es la que Rubén y María armaron ('🎾 RACHA DETALLADA', "
            "negrita, {respaldo} con el patrón, {frase} con el acierto). El bug real: "
            "`racha_api.py` tiene SU PROPIA plantilla de respaldo (`_PLANTILLA_RACHA_DEFECTO`, "
            "la rica) para cuando no hay nada guardado -- pero `obtener_texto()` siempre "
            "devuelve el default DE ACÁ primero, así que esa plantilla de respaldo del código "
            "nunca se llegaba a usar; quedó como código muerto. Mientras nadie guardara un "
            "valor a mano en este campo desde Admin → Mensajes, el canal mandaba la versión "
            "vieja sin patrón ni acierto -- que es justo lo que le llegó a María el "
            "29/08/2026 con la racha de Dmitry Popko, y por eso no coincidía con lo que "
            "habían configurado. Ahora el default de acá es el mismo texto que "
            "`_PLANTILLA_RACHA_DEFECTO`, para que las dos plantillas nunca vuelvan a "
            "divergir. Se probó una versión fusionada con la plantilla vieja (cambio de "
            "lado, tipo, marcador/sets/último juego/próximo explícitos) el mismo día, pero "
            "María la descartó -- queda solo esta, la simple, a propósito. "
            "El mensaje completo, editable. Lo que va entre llaves se reemplaza por el dato; "
            "el resto se manda tal cual. Los renglones que queden vacíos se borran solos. "
"SE COMBINA CON 'Mensajes de las alertas' -> 'Racha detallada': lo que se escriba "
            "ahí entra como {cab} (encabezado) y {frase}. Los dos lugares no compiten: "
            "Mensajes decide QUÉ DICEN esas dos líneas, esta plantilla decide DÓNDE van y qué "
            "otros datos salen. Si sólo querés cambiar el título, editalo en Mensajes y no "
            "toques esto. "
            "MARCADORES: {cab}, {frase}, {pausa} (editable en RACHAAPI_PAUSA_SI / RACHAAPI_PAUSA_NO, "
            "aquí abajo), {tipo} (SOSTIENE u QUIEBRE con "
            "el sexo), {jugador}, {rival}, {racha}, {estado} (ganando/parejo/perdiendo), "
            "{sexo}, {marcador}, {sets}, {superficie}, {ultimo_juego} (HOLD o BREAK), "
            "06/09/2026: {score} (\"Sets 1-1 · Juegos 4-2\", el marcador actual del partido), "
            "{sets_r} y {marcador_r} (los dos por separado). Los tres van ORIENTADOS al "
            "jugador en racha (él-rival); {marcador} y {sets} a secas siguen siendo "
            "home-visita como llegan del feed, así que para el mensaje conviene usar los "
            "orientados: un 5-1 sin orientar puede ser 1-5 y no hay cómo saberlo leyendo. "
            "{proximo}, {cierra} (aviso de que el quiebre define set o partido), {torneo}. "
            "29/08/2026, marcadores nuevos: {entrar} (a qué apostar, con los nombres puestos "
            "-- ver RACHAAPI_ENTRAR_BREAK / _HOLD), {respaldo} (el bloque del patrón que hizo "
            "sonar el aviso, ver RACHAAPI_RESPALDO_TEXTO), y sueltos {grupo}, {patron}, "
            "{puesto}, {acierto}, {muestras}, {ganados}, {perdidos}, {fiabilidad}. "
            "Los sueltos quedan VACÍOS cuando decide el modo clásico, así que si se usan "
            "fuera de {respaldo} conviene ponerlos solos en su renglón: el filtro de líneas "
            "en blanco borra el renglón entero, pero no un texto fijo que los acompañe. "
            "Si se escribe un marcador que no existe, el aviso SE MANDA IGUAL pero sin "
            "reemplazar nada, y el error queda en los logs -- se prefirió eso a que una alerta "
            "se pierda por un typo."
        ),
    },
    # Mismo criterio que RACHAAPI_MENSAJE de arriba (24/08/2026, a pedido
    # de Maria: "que lo pueda elegir y editar cuando esté creando el
    # mensaje desde el admin"). Antes {pausa} era una de dos frases fijas
    # escritas en racha_api.py -- no había forma de tocarlas sin
    # desplegar código. Ahora son estas dos claves; racha_api.py las lee
    # con obtener_texto() en vez de traerlas escritas a mano.
    "RACHAAPI_PAUSA_SI": {
        "tipo": "texto", "default": "⏸️ CAMBIO DE LADO -- tenes ~90 segundos",
        "categoria": "Jugador en Racha", "es_mensaje": True,
        "etiqueta": "Racha detallada: texto de {pausa} CON cambio de lado",
        "descripcion": (
            "Lo que reemplaza a {pausa} en el mensaje cuando el aviso cae justo en un cambio "
            "de lado (1º, 3º, 5º... game de cada set). Texto libre."
        ),
    },
    "RACHAAPI_PAUSA_NO": {
        "tipo": "texto", "default": "⚡ SIN pausa -- entra ya, ~25 segundos",
        "categoria": "Jugador en Racha", "es_mensaje": True,
        "etiqueta": "Racha detallada: texto de {pausa} SIN cambio de lado",
        "descripcion": (
            "Lo que reemplaza a {pausa} en el mensaje cuando NO hay cambio de lado. Texto libre."
        ),
    },
    # ── El resto de las frases fijas del mensaje (24/08/2026, a pedido
    # de Maria: "no solo esos campos, en general todo el mensaje") ─────
    # Antes cada una estaba escrita a mano adentro de racha_api.py. Se
    # separan acá con el mismo criterio que RACHAAPI_PAUSA_SI/NO.
    "RACHAAPI_ESTADO_GANANDO": {
        "tipo": "texto", "default": "▲ ganando", "categoria": "Jugador en Racha",
        "es_mensaje": True, "etiqueta": "Racha detallada: {estado} cuando va GANANDO",
        "descripcion": "Reemplaza a {estado} cuando el jugador en racha va arriba en el marcador.",
    },
    "RACHAAPI_ESTADO_PAREJO": {
        "tipo": "texto", "default": "◆ parejo", "categoria": "Jugador en Racha",
        "es_mensaje": True, "etiqueta": "Racha detallada: {estado} cuando va PAREJO",
        "descripcion": "Reemplaza a {estado} cuando el set está empatado.",
    },
    "RACHAAPI_ESTADO_PERDIENDO": {
        "tipo": "texto", "default": "▼ perdiendo", "categoria": "Jugador en Racha",
        "es_mensaje": True, "etiqueta": "Racha detallada: {estado} cuando va PERDIENDO",
        "descripcion": "Reemplaza a {estado} cuando el jugador en racha va abajo en el marcador.",
    },
    "RACHAAPI_SEXO_M": {
        "tipo": "texto", "default": "👨 Hombres", "categoria": "Jugador en Racha",
        "es_mensaje": True, "etiqueta": "Racha detallada: {sexo} cuando es masculino",
        "descripcion": "Reemplaza a {sexo}. Se omite la línea entera si el dato no llegó.",
    },
    "RACHAAPI_SEXO_F": {
        "tipo": "texto", "default": "👩 Mujeres", "categoria": "Jugador en Racha",
        "es_mensaje": True, "etiqueta": "Racha detallada: {sexo} cuando es femenino",
        "descripcion": "Reemplaza a {sexo}. Se omite la línea entera si el dato no llegó.",
    },
    "RACHAAPI_TIPO_SOSTIENE_M": {
        "tipo": "texto", "default": "📌 SOSTIENE · Hombres", "categoria": "Jugador en Racha",
        "es_mensaje": True, "etiqueta": "Racha detallada: {tipo} — SOSTIENE, masculino",
        "descripcion": "Reemplaza a {tipo} cuando el aviso es por SOSTIENE en partido de hombres.",
    },
    "RACHAAPI_TIPO_SOSTIENE_F": {
        "tipo": "texto", "default": "📌 SOSTIENE · Mujeres", "categoria": "Jugador en Racha",
        "es_mensaje": True, "etiqueta": "Racha detallada: {tipo} — SOSTIENE, femenino",
        "descripcion": "Reemplaza a {tipo} cuando el aviso es por SOSTIENE en partido de mujeres.",
    },
    "RACHAAPI_TIPO_SOSTIENE_SIN_GENERO": {
        "tipo": "texto", "default": "📌 SOSTIENE · género desconocido", "categoria": "Jugador en Racha",
        "es_mensaje": True, "etiqueta": "Racha detallada: {tipo} — SOSTIENE, sin género",
        "descripcion": "Reemplaza a {tipo} cuando es SOSTIENE y el género no llegó en el feed.",
    },
    "RACHAAPI_TIPO_QUIEBRE_CIERRA": {
        "tipo": "texto", "default": "📌 QUIEBRE QUE CIERRA EL {define}", "categoria": "Jugador en Racha",
        "es_mensaje": True, "etiqueta": "Racha detallada: {tipo} — QUIEBRE que define set/partido",
        "descripcion": (
            "Reemplaza a {tipo} cuando el próximo quiebre cierra el set o el partido. Acepta "
            "los mismos marcadores que el resto ({jugador}, {rival}, {sexo}, {racha}, "
            "{marcador}...) más su propio {define}, que sale como SET o PARTIDO. Nada se agrega "
            "solo -- si querés el nombre, el género o el SET/PARTIDO en el texto, van los tres "
            "a mano con sus marcadores (24/08/2026: antes el género se pegaba solo al final, "
            "duplicando el del encabezado)."
        ),
    },
    # ── A QUÉ ENTRAR (29/08/2026, a pedido de Rubén: "el mensaje va a
    #    decir cuál es el juego siguiente, el partido y a qué entrar") ─
    # {proximo} ya decía HOLD o BREAK, pero en la jerga del sistema y
    # sin nombrar a nadie: había que traducir "QUIEBRE" a "apostar a que
    # el que saca pierde su juego" y encima acordarse de quién saca.
    # Estas dos lo dicen con los nombres puestos.
    "RACHAAPI_ENTRAR_BREAK": {
        "tipo": "texto", "default": "BREAK de {jugador}",
        "categoria": "Jugador en Racha", "es_mensaje": True,
        "etiqueta": "Racha detallada: {entrar} — cuando el próximo es QUIEBRE",
        "descripcion": (
            "⚠️ DEFAULT CAMBIADO EL 29/08/2026, a pedido de Maria: era la frase larga "
            "('que {rival} PIERDA su saque (break de {jugador})'), ahora es la corta que se "
            "usa de verdad en el mensaje aprobado. "
            "Reemplaza a {entrar} cuando el próximo juego lo saca el RIVAL y la apuesta es al "
            "quiebre. Acepta los mismos marcadores que el resto ({jugador}, {rival}, {racha}, "
            "{marcador}, {torneo}...)."
        ),
    },
    "RACHAAPI_ENTRAR_HOLD": {
        "tipo": "texto", "default": "HOLD de {jugador}",
        "categoria": "Jugador en Racha", "es_mensaje": True,
        "etiqueta": "Racha detallada: {entrar} — cuando el próximo es SOSTENIDA",
        "descripcion": (
            "⚠️ DEFAULT CAMBIADO EL 29/08/2026, mismo motivo y misma fecha que "
            "RACHAAPI_ENTRAR_BREAK: era 'que {jugador} GANE su saque (sostenida)', ahora es la "
            "versión corta, simétrica con la de BREAK. "
            "Reemplaza a {entrar} cuando el próximo juego lo saca el jugador en racha. Mismos "
            "marcadores que el de arriba."
        ),
    },
    # El respaldo va como UN bloque y no como marcadores sueltos a
    # propósito: en modo clásico no hay patrón, y una plantilla escrita
    # con los sueltos dejaría "Patrón #: " colgando -- el filtro de
    # renglones vacíos no borra una línea que tiene texto fijo. Con
    # {respaldo} el bloque entero aparece completo o no aparece.
    "RACHAAPI_RESPALDO_TEXTO": {
        # 29/08/2026: sacado el "#{puesto}" del default. Con la tabla
        # maestra el puesto NO decide nada -- la señal suena por su
        # propio acierto, no por su posición -- y encima el número se
        # mueve solo cuando OTRA señal sube o baja. Poner "#47" al lado
        # de una señal sugiere un ranking que ya no existe. En su lugar
        # va {margen}, que dice cuánto le sobra sobre el umbral: eso sí
        # es información del caso concreto.
        # 29/08/2026, estilo "opción C": negrita Unicode solo en los DOS
        # números que deciden -- el acierto y el margen sobre el umbral.
        # El resto en redonda a propósito: si se resalta todo, no se
        # resalta nada.
        #
        # "minimo" sin tilde NO es un typo: la í no tiene glifo en
        # negrita matemática. Acá va en redonda igual, pero se deja sin
        # tilde para que quien edite el texto y le ponga negrita no se
        # encuentre con "𝐦í𝐧𝐢𝐦𝐨". Ver _neg() en racha_api.py.
        # 29/08/2026: sacada la línea "{margen_b} sobre el minimo de
        # {umbral}%" del default, a pedido de Rubén. El marcador
        # {margen} SIGUE EXISTIENDO y se puede volver a poner desde acá.
        #
        # Vale la pena saber qué se pierde: el margen era lo único del
        # mensaje que distinguía una señal holgada de una al filo. Con
        # el texto actual, "60,0% en 10 casos" y "78,6% en 22 casos" se
        # leen parecido, pero la primera se apaga con la próxima derrota
        # y la segunda aguanta seis. La fiabilidad ayuda pero mide otra
        # cosa (el tamaño de la muestra, no la distancia al umbral).
        "tipo": "texto", "es_mensaje": True, "default": (
            "📊 {grupo}\n"
            "{acierto_b}% en {muestras} casos ({ganados}-{perdidos}) · "
            "fiab. {fiabilidad}"
        ),
        "categoria": "Jugador en Racha",
        "etiqueta": "Racha detallada: {respaldo} — el patrón que hizo sonar el aviso",
        "descripcion": (
            "El bloque que explica POR QUÉ suena este aviso. Solo aparece cuando decide el "
            "ranking (ver RACHAAPI_GRUPOS_ACTIVO); en modo clásico queda vacío y sus "
            "renglones se borran solos. "
            "MARCADORES PROPIOS: {grupo} (el nombre corto, ej. 'Femenino + Racha 4 + Set 2'), "
            "{patron} (la frase larga), {acierto}, {muestras}, {ganados}, {perdidos}, "
            "{fiabilidad}. "
            "NEGRITA: cada marcador tiene su gemelo terminado en _b, ya convertido a negrita "
            "Unicode ({jugador_b}, {acierto_b}, {margen_b}, {entrar_b}...). Se usa Unicode y "
            "no <b> porque así se ve en negrita incluso al copiar o reenviar fuera de "
            "Telegram. OJO: las vocales con tilde y la ñ no tienen glifo en negrita y quedan "
            "en redonda -- por eso el texto por defecto escribe 'minimo' y 'ENTRA' sin acento. "
            "En los nombres de jugador no se puede evitar y se acepta. "
            "CON LA TABLA MAESTRA hay tres más: {umbral} (el mínimo configurado, ej. 60), "
            "{margen} (cuánto le sobra sobre ese mínimo, ej. '+12,7 pp' — es el dato que dice "
            "si la señal está holgada o al filo) y {acierto_excel} (lo que medía el análisis "
            "del 29/08, para comparar contra lo que mide hoy). "
            "{puesto} SIGUE EXISTIENDO pero ya no significa nada: con la tabla maestra no hay "
            "ranking que decida, cada señal suena por su propio acierto, y el número se mueve "
            "solo cuando OTRA señal sube o baja. Dos avisos de la misma señal pueden mostrar "
            "puestos distintos sin que haya cambiado nada de ella. Por eso se sacó del texto "
            "por defecto. "
            "Además acepta los generales ({jugador}, {rival}, {torneo}...). "
            "Los siete de {grupo} y compañía también se pueden usar sueltos en la plantilla "
            "grande, pero entonces hay que hacerse cargo de los renglones que quedan cuando "
            "no hay patrón."
        ),
    },
    "RACHAAPI_PROXIMO_HOLD": {
        "tipo": "texto", "default": "🟢 SOSTENIDA (HOLD) -- saca el de la racha",
        "categoria": "Jugador en Racha", "es_mensaje": True,
        "etiqueta": "Racha detallada: {proximo} — próximo es SOSTENIDA",
        "descripcion": "Reemplaza a {proximo} cuando el próximo juego lo saca el jugador en racha.",
    },
    "RACHAAPI_PROXIMO_BREAK": {
        "tipo": "texto", "default": "🔴 QUIEBRE (BREAK) -- resta el de la racha",
        "categoria": "Jugador en Racha", "es_mensaje": True,
        "etiqueta": "Racha detallada: {proximo} — próximo es QUIEBRE",
        "descripcion": (
            "Reemplaza a {proximo} cuando el próximo juego lo resta el jugador en racha (lo saca "
            "el rival). Si ese quiebre además cierra set o partido, se le agrega abajo el texto "
            "de RACHAAPI_CIERRA_TEXTO en un renglón aparte."
        ),
    },
    "RACHAAPI_PROXIMO_SIN_DATO": {
        "tipo": "texto", "default": "sin dato de saque", "categoria": "Jugador en Racha",
        "es_mensaje": True, "etiqueta": "Racha detallada: {proximo} — sin dato de saque",
        "descripcion": "Reemplaza a {proximo} cuando el feed no trajo quién saca el próximo juego.",
    },
    "RACHAAPI_CIERRA_TEXTO": {
        "tipo": "texto", "default": "⚠️ ESTE QUIEBRE CIERRA EL {define}", "categoria": "Jugador en Racha",
        "es_mensaje": True, "etiqueta": "Racha detallada: {cierra} — aviso de quiebre que define",
        "descripcion": (
            "Reemplaza a {cierra}, y también se agrega dentro de {proximo} cuando corresponde "
            "(ver RACHAAPI_PROXIMO_BREAK). Vacío cuando el próximo juego no define nada. Lleva "
            "su propio marcador {define} (SET o PARTIDO)."
        ),
    },
    "RACHAAPI_RACHA_MINIMA": {
        "tipo": "int", "default": "4", "categoria": "Jugador en Racha",
        "grupo": "⚙️ 3) Comunes",
        "etiqueta": "Racha detallada: juegos seguidos mínimos para avisar",
        "descripcion": (
            "Cuántos juegos al hilo hacen falta para que suene. 4 es el número con el que se "
            "venía midiendo en el Excel. OJO: esto NO cambia a partir de cuántos se GUARDA -- "
            "la tabla registra desde 4 siempre (RACHA_MINIMA en racha_api.py), porque el "
            "objetivo de esa tabla es juntar muestra y subir el piso de guardado dejaría un "
            "hueco imposible de reconstruir después. Esto solo decide desde cuántos se avisa. "
            "24/08/2026, a pedido de Maria: el aviso suena SOLO en este número exacto, no en "
            "este número para arriba -- si la racha sigue (5, 6, 7...) ya no vuelve a avisar. "
            "29/08/2026: con RACHAAPI_GRUPOS_ACTIVO prendido esto NO se lee. El largo de la "
            "racha lo decide el patrón (un patrón de R7 hace sonar la racha de 7), que es "
            "justamente lo que este campo no podía hacer."
        ),
    },
    # INTERRUPTOR MAESTRO DE LA RACHA SENCILLA (28/08/2026, a pedido de
    # Rubén: "ya no se van a enviar rachas sencillas, ahora solo se van a
    # enviar las rachas de la nueva tabla").
    #
    # DEFAULT 0 -- APAGADA. Es el único default de este archivo que
    # cambia el comportamiento al desplegar, y es a propósito: el pedido
    # es justamente que deje de sonar. Se deja como interruptor en vez de
    # borrar el envío para que se pueda volver atrás desde Admin en un
    # clic, sin deploy, si aparece algún motivo para reactivarla.
    #
    # NO ALCANZABA con ESTRATEGIA_ACTIVA_RACHA, el interruptor de la
    # Matriz: ése corta notificar_categoria() -- los GRUPOS -- pero
    # /racha/registrar además llama a notificar_valor(), que va al canal
    # de Admin sin pasar por la Matriz. Con solo el de la Matriz, las
    # rachas sencillas seguirían llegando al canal de Admin y el pedido
    # quedaría a medias. Éste corta las dos.
    #
    # LO QUE NO APAGA, a propósito: la fila se sigue guardando en
    # racha_alertas y en el Historial FullTennis. La pestaña "📣 Racha"
    # sigue funcionando igual y el export a Excel también. Apagar el
    # aviso no es lo mismo que dejar de registrar -- y dejar de registrar
    # sería perder la única muestra que permite comparar las dos fuentes.
    "RACHA_ACTIVA": {
        "tipo": "bool", "default": "0", "categoria": "Racha sencilla (canal externo — no es Jugador en Racha)",
        "etiqueta": "Racha sencilla: enviar avisos",
        "descripcion": (
            "APAGADO desde el 28/08/2026, a pedido de Rubén: los avisos ahora los manda la "
            "Racha detallada (la nuestra), no ésta. Apagado, las rachas del canal externo se "
            "siguen guardando en la pestaña '📣 Racha' y en el Historial FullTennis, y el "
            "export a Excel las sigue trayendo -- lo único que no pasa es el envío: ni al "
            "canal de Admin ni a los grupos de la Matriz. Prendido, vuelve a sonar como "
            "antes, respetando los estados y el dedup de abajo. Ojo: esto NO afecta a la "
            "Racha detallada, que tiene su propio interruptor (RACHAAPI_ACTIVA)."
        ),
    },
    "RACHA_ESTADOS": {
        "tipo": "str", "default": "perdiendo,parejo", "categoria": "Racha sencilla (canal externo — no es Jugador en Racha)",
        "etiqueta": "Racha: estados que hacen sonar la alerta",
        "descripcion": (
            "Separados por coma. Los estados posibles son: perdiendo, parejo, ganando. El "
            "default deja afuera 'ganando' a propósito (si ya va ganando, el mercado ya lo "
            "sabe). Las rachas que no están en esta lista se guardan igual en la pestaña, "
            "pero no suenan en ningún lado."
        ),
    },
    "RACHA_DEDUP_HORAS": {
        "tipo": "int", "default": "3", "categoria": "Racha sencilla (canal externo — no es Jugador en Racha)",
        "etiqueta": "Horas sin repetir el mismo enfrentamiento",
        "descripcion": (
            "Una racha del mismo jugador contra el mismo rival suena UNA vez dentro de esta "
            "ventana, aunque la tabla siga sumando filas a medida que la racha crece."
        ),
    },
    # RACHA_RETENCION_HORAS (21/08/2026, a pedido de Maria: "editable
    # desde el admin, que no pese tanto, pero que cuando quiera hacer
    # un análisis tenga datos suficientes"). Hasta ahora esta tabla se
    # borraba con FTR_RETENCION_RACHA_HORAS (env var fija, default 12h,
    # ver valor_cleanup_task.py) -- el mismo criterio operativo que
    # valor_en_vivo/mto_alertas/etc ("el partido termina en horas, no
    # hace falta más"). Pero acá la tabla tiene un segundo uso además
    # de la pestaña en vivo: el export a Excel para análisis histórico
    # (ver /racha/exportar) -- y para ESE uso, 12h no alcanza, "todo el
    # historial" terminaba siendo casi lo mismo que lo que ya se veía
    # en pantalla. Clave propia, no la comparte con ninguna otra tabla
    # de retención -- así se puede estirar SOLO esta sin tocar el
    # criterio de 12h del resto de las alertas "en vivo".
    #
    # Default 720h (30 días): cada fila de racha_alertas es liviana
    # (texto corto, sin historial de precios como pinnodds), así que
    # incluso guardando meses el peso en disco es marginal -- el límite
    # real no es el tamaño, es tener algo razonable para no dejarlo en
    # "para siempre" (0) sin que Maria lo haya pedido explícitamente.
    # 30 días, a las rachas que se ven por sesión (~74 avisos en una
    # tarde, según capturas reales), ya da una muestra bastante más
    # grande que el mínimo de 60-80 partidos que usa para validación
    # estadística en otras estrategias. 0 = no borrar nunca.
    #
    # valor_cleanup_task.py lee esta clave en CADA vuelta del loop de
    # limpieza (no solo al arrancar el proceso) -- ver
    # valor_cleanup_task._retencion_actual() -- así que cambiarla acá
    # vale de inmediato, sin reiniciar el servicio.
    "RACHA_RETENCION_HORAS": {
        "tipo": "int", "default": "720", "categoria": "Racha sencilla (canal externo — no es Jugador en Racha)",
        "etiqueta": "Horas de retención del historial (0 = no borrar nunca)",
        "descripcion": (
            "Cuánto tiempo se guardan las filas de Jugador en Racha antes de que la "
            "limpieza automática las borre. Afecta DOS cosas: lo que se ve en la pestaña "
            "en vivo y lo que trae el botón 'Exportar a Excel' (todo lo que sigue en la "
            "tabla en ese momento). Default 720h = 30 días -- las filas son livianas, así "
            "que subir esto no debería notarse en el rendimiento; bajarlo hace que el "
            "export para análisis tenga menos historial disponible. 0 = no borrar nunca."
        ),
    },
    "RACHA_DESTINO_NO_PASA": {
        "tipo": "str", "default": "propia_pestana", "categoria": "Racha sencilla (canal externo — no es Jugador en Racha)",
        "etiqueta": "A dónde llega si el estado NO está en la lista",
        "opciones": [
            {"valor": "propia_pestana", "etiqueta": "Solo su pestaña -- no se copia a ningún lado (como hoy)"},
            {"valor": "sugerencias", "etiqueta": "💡 Sugerencias"},
            {"valor": "descartes", "etiqueta": "🗑️ Descartes"},
        ],
        "descripcion": (
            "20/08/2026, a pedido de Maria. HOY (default 'propia_pestana'): la fila ya se "
            "guarda siempre en la pestaña '🔥 Jugador en Racha' (esté o no el estado en la "
            "lista de arriba) -- este desplegable decide si ADEMÁS se copia a otro lado "
            "cuando NO suena. 'sugerencias'/'descartes': además se copia ahí."
        ),
    },

    # ── POST SET — Valor histórico (01/09/2026, a pedido de Maria) ───
    # El bloque que hasta hoy solo se veía en la pestaña POST SET del
    # panel de research (ver _resumen_over/_resumen_ambas_lineas en
    # research/api/routes_research.py) y ahora se manda a Telegram,
    # armado en production/engines/post_set_mensaje.py. Todo lo que
    # tiene un número vive acá -- nada fijo en el código de ese módulo,
    # a diferencia de las grillas del panel (_UMBRALES_POSTSET_*, que
    # SÍ son fijas a propósito, ver esas constantes: son la grilla de
    # una tabla de auditoría, no el corte de una alerta).
    #
    # Seis líneas fijas por caso -- la CANTIDAD no es configurable
    # (cambiar de 6 a otro número es un cambio de layout del mensaje,
    # no un ajuste de umbral), pero CADA línea sí lo es, una por una.
    **{
        f"POSTSET_LEVE_LINEA_{_i+1}": {
            "tipo": "float", "default": str(_v), "categoria": "POST SET — Valor histórico",
            "etiqueta": f"Ventaja Leve — línea UNDER {_i+1} de 6",
            "descripcion": (
                "Línea de Under que se muestra en el bloque de Valor histórico de "
                "'PostSet — Ventaja Leve', sobre el total de juegos del PARTIDO completo. "
                "Las seis se muestran siempre, en este orden, de menor a mayor."
            ),
        }
        for _i, _v in enumerate((21.5, 22.5, 23.5, 24.5, 25.5, 26.5))
    },
    **{
        f"POSTSET_PASA_LINEA_{_i+1}": {
            "tipo": "float", "default": str(_v), "categoria": "POST SET — Valor histórico",
            "etiqueta": f"Pasan los filtros — línea OVER {_i+1} de 6",
            "descripcion": (
                "Línea de Over que se muestra en el bloque de Valor histórico de "
                "'PostSet — Pasan los filtros', sobre el total de juegos del PARTIDO "
                "completo. Las seis se muestran siempre, en este orden, de menor a mayor."
            ),
        }
        for _i, _v in enumerate((20.5, 21.5, 22.5, 23.5, 24.5, 25.5))
    },
    "POSTSET_REMONTADA_ELO_MIN": {
        "tipo": "float", "default": "0.78", "categoria": "POST SET — Valor histórico",
        "etiqueta": "Potencial de Remontada — Elo mínimo del favorito",
        "descripcion": (
            "La línea 🔥 Potencial de Remontada solo aparece en 'Pasan los filtros' cuando "
            "el favorito tiene Prob. Elo por ENCIMA de este número, Prob. FTR dentro de la "
            "banda de abajo, Y el partido es de hombres. En mujeres nunca aparece. Si falla "
            "cualquiera de las tres condiciones, la línea no se manda -- el resto del bloque "
            "(las seis líneas de Over) sí se manda igual."
        ),
    },
    "POSTSET_REMONTADA_FTR_MIN": {
        "tipo": "float", "default": "0.60", "categoria": "POST SET — Valor histórico",
        "etiqueta": "Potencial de Remontada — Prob. FTR mínima del favorito",
        "descripcion": "Piso de la banda de Prob. FTR para que aparezca la línea 🔥 (ver Elo mínimo).",
    },
    "POSTSET_REMONTADA_FTR_MAX": {
        "tipo": "float", "default": "0.75", "categoria": "POST SET — Valor histórico",
        "etiqueta": "Potencial de Remontada — Prob. FTR máxima del favorito",
        "descripcion": "Techo de la banda de Prob. FTR para que aparezca la línea 🔥 (ver Elo mínimo).",
    },
    "POSTSET_MUESTRA_MINIMA": {
        "tipo": "int", "default": "1", "categoria": "POST SET — Valor histórico",
        "etiqueta": "Muestra mínima para mandar el bloque",
        "descripcion": (
            "Si el historial de esa estrategia, filtrado por el género del partido, tiene "
            "MENOS partidos resueltos que este número, el bloque de Valor histórico no se "
            "manda a Telegram (la alerta de siempre, si corresponde, se manda igual). No "
            "aplica a la línea 🔥 de Potencial de Remontada, que tiene su propio criterio: "
            "si la muestra filtrada por Elo/FTR/género da 0 partidos, esa línea puntual no "
            "se agrega; con 1 o más se agrega igual aunque sea chica -- el número "
            "'📊 Muestra: N partidos' que la acompaña ya avisa qué tan chica es."
        ),
    },
    "POSTSET_REMONTADA_SOBRE_MUESTRA_FILTRADA": {
        "tipo": "bool", "default": "1", "categoria": "POST SET — Valor histórico",
        "etiqueta": "Potencial de Remontada — calcular sobre la muestra filtrada",
        "descripcion": (
            "01/09/2026, a pedido de Maria. Encendido (default): el % de 🔥 Potencial de "
            "Remontada se calcula SOLO sobre los partidos de la muestra que además cumplen "
            "Elo/FTR (los que se parecen de verdad al partido de la alerta) -- más preciso, "
            "pero la muestra puede quedar en 4 o 5 partidos. Apagado: se calcula sobre TODA "
            "la muestra de 'Pasan los filtros', sin mirar su Elo/FTR individual."
        ),
    },
    "POSTSET_ELO_FTR_ACTIVO": {
        "tipo": "bool", "default": "1", "categoria": "POST SET — Valor histórico",
        "etiqueta": "Capa Elo + FTR — activada",
        "descripcion": (
            "04/09/2026, a pedido de Rubén. Encendido (default): cuando un partido cumple el "
            "cruce de Elo y FTR configurado para su género + marcador de Set 1 (ver la sección "
            "'POST SET — Filtro Elo + FTR'), los porcentajes se calculan SOLO sobre los "
            "históricos de ese mismo marcador que también cumplen ese cruce, y el mensaje "
            "agrega '🔥 Elo + FTR Activo'. Apagado: todos los partidos usan el histórico "
            "general del marcador, como antes de que existiera esta capa. En los dos casos el "
            "partido sale igual: esta capa NUNCA bloquea un diagnóstico, solo cambia qué "
            "muestra histórica se usa para calcularlo."
        ),
    },
    "POSTSET_ELO_FTR_MUESTRA_MINIMA": {
        "tipo": "int", "default": "1", "categoria": "POST SET — Valor histórico",
        "etiqueta": "Capa Elo + FTR — muestra mínima del histórico específico",
        "descripcion": (
            "Cuántos partidos tiene que tener como mínimo el histórico específico del cruce "
            "para que se lo use. Si el partido cumple Elo/FTR pero todavía no hay tantos "
            "históricos que también lo cumplan, se cae al histórico general del marcador y NO "
            "se muestra la etiqueta -- el diagnóstico sale igual. En 1 (default) alcanza con "
            "un solo partido previo; subirlo hace la capa más exigente a medida que crezca la "
            "muestra. Nunca puede ser menor a 1."
        ),
    },
    "POSTSET_CAPAS_UMBRAL": {
        "tipo": "float", "default": "65", "categoria": "POST SET — Valor histórico",
        "etiqueta": "3 capas — umbral para que una capa cuente (%)",
        "descripcion": (
            "04/09/2026, punto 31 del pedido de Rubén: el 65% no queda escrito en ninguna "
            "parte del código, sale de acá. Una capa (Marcador, Elo o FTR) respalda una "
            "línea solo si su porcentaje histórico llega a este valor; por debajo, esa capa "
            "no cuenta. Si ninguna de las tres llega (0/3) la línea NO se muestra; con una, "
            "dos o tres se muestra, y con las tres se marca 🔥. Cambiarlo acá cambia el "
            "mensaje entero, sin deploy."
        ),
    },
    "POSTSET_CAPAS_MUESTRA_MINIMA": {
        "tipo": "int", "default": "0", "categoria": "POST SET — Valor histórico",
        "etiqueta": "3 capas — muestra mínima para que una capa cuente",
        "descripcion": (
            "Cuántos partidos históricos tiene que tener una capa para que su porcentaje "
            "cuente. En 0 (default) no bloquea nada -- punto 24 del pedido: por ahora no se "
            "bloquea ninguna línea por tamaño de muestra, pero el parámetro queda listo para "
            "cuando haya más partidos. Una capa sin ningún partido no cuenta nunca, sea cual "
            "sea este valor: su porcentaje es sin dato, que no es lo mismo que 0%."
        ),
    },
    "MSG_CAB_post_set_leve": {
        "tipo": "texto", "default": "🎾 POST SET \u2014 VENTAJA LEVE", "categoria": "POST SET — Valor histórico",
        "etiqueta": "Encabezado — PostSet Ventaja Leve",
        "descripcion": "Primera línea del mensaje de Ventaja Leve. Se manda en negrita (el emoji queda como esté acá).",
    },
    "MSG_CAB_post_set_pasa": {
        "tipo": "texto", "default": "🎾 POST SET \u2014 PASAN EL FILTRO", "categoria": "POST SET — Valor histórico",
        "etiqueta": "Encabezado — PostSet Pasan los filtros",
        "descripcion": "Primera línea del mensaje de Pasan los filtros. Se manda en negrita (el emoji queda como esté acá).",
    },
    "MSG_SUBCAB_post_set_leve": {
        "tipo": "texto", "default": "📉 Valor hist\u00f3rico UNDER \u2014 Ventaja Leve",
        "categoria": "POST SET — Valor histórico",
        "etiqueta": "Subtítulo del bloque — Ventaja Leve",
        "descripcion": "Título que encabeza las seis líneas de Under. Se manda en negrita (el emoji queda como esté acá).",
    },
    "MSG_SUBCAB_post_set_pasa": {
        "tipo": "texto", "default": "📈 Valor hist\u00f3rico OVER \u2014 Favorito pierde Set 1",
        "categoria": "POST SET — Valor histórico",
        "etiqueta": "Subtítulo del bloque — Pasan los filtros",
        "descripcion": "Título que encabeza las seis líneas de Over. Se manda en negrita (el emoji queda como esté acá).",
    },

    # ── Partidos Maestros (01/09/2026, a pedido de Maria) ────────────
    # Estrategia pre-partido nueva: cuota del favorito en una banda
    # relativamente cerrada (favoritos moderados, no aplastantes) Y los
    # dos motores de rating de acuerdo en que el favoritismo es real.
    # Se evalúa en /fixture/reportar -- ver
    # production/engines/partidos_maestros.py -- y UNA SOLA VEZ por
    # fixture: la primera vez que las tres condiciones se cumplen
    # juntas, con la cuota de APERTURA (fixtures_ftr_elo.cuota_favorito,
    # que el sistema ya congela sola y nunca pisa en los re-reportes) y
    # el FTR/Elo de ESE momento -- no se vuelve a evaluar después.
    "PARTIDOS_MAESTROS_CUOTA_MIN": {
        "tipo": "float", "default": "1.25", "categoria": "Partidos Maestros",
        "etiqueta": "Cuota del favorito — mínima",
        "descripcion": (
            "Piso de la banda de cuota de apertura del favorito. Junto con la máxima, "
            "define el rango de favoritos que interesa (ni aplastantes ni parejos)."
        ),
    },
    "PARTIDOS_MAESTROS_CUOTA_MAX": {
        "tipo": "float", "default": "1.37", "categoria": "Partidos Maestros",
        "etiqueta": "Cuota del favorito — máxima",
        "descripcion": "Techo de la banda de cuota de apertura del favorito (ver la mínima).",
    },
    "PARTIDOS_MAESTROS_ELO_MIN": {
        "tipo": "float", "default": "0.80", "categoria": "Partidos Maestros",
        "etiqueta": "Prob. Elo mínima del favorito",
        "descripcion": "El favorito necesita Prob. Elo MAYOR O IGUAL a este número para calificar (desde el 23/09/2026; antes era estrictamente mayor).",
    },
    "PARTIDOS_MAESTROS_FTR_MIN": {
        "tipo": "float", "default": "0.60", "categoria": "Partidos Maestros",
        "etiqueta": "Prob. FTR mínima del favorito",
        "descripcion": "Piso de la banda de Prob. FTR del favorito (junto con la máxima).",
    },
    "PARTIDOS_MAESTROS_FTR_MAX": {
        "tipo": "float", "default": "0.80", "categoria": "Partidos Maestros",
        "etiqueta": "Prob. FTR máxima del favorito",
        "descripcion": "Techo de la banda de Prob. FTR del favorito (ver la mínima).",
    },
    "PARTIDOS_MAESTROS_MUESTRA_EXIGIR": {
        "tipo": "bool", "default": "1", "categoria": "Partidos Maestros",
        "etiqueta": "Exigir muestra suficiente",
        "descripcion": (
            "Encendido (default): solo califican fixtures con muestra_suficiente=1 en "
            "fixtures_ftr_elo -- mismo corte de calidad que ya usa Partidos en Vivo "
            "(VIVO_MUESTRA_EXIGIR). Apagado: se evalúan igual los fixtures con muestra chica."
        ),
    },
    # 🔥 MAESTRO TOP (23/09/2026). NO es otra estrategia ni otra alerta:
    # dentro de los que YA calificaron arriba, los que además pasan estos
    # dos pisos reciben la insignia TOP en la misma fila y el mismo
    # mensaje. REGLAS CONGELADAS para medir fuera de muestra -- ver el
    # docstring de production/engines/partidos_maestros.py.
    "PARTIDOS_MAESTROS_TOP_ELO_MIN": {
        "tipo": "float", "default": "0.85", "categoria": "Partidos Maestros",
        "etiqueta": "🔥 TOP — Prob. Elo mínima",
        "descripcion": (
            "Insignia 🔥 MAESTRO TOP: un Partido Maestro con Prob. Elo mayor o igual a esto "
            "(y FTR mayor o igual al de abajo). Sigue exigiendo todo lo de arriba (cuota, "
            "techo de FTR, muestra). CONGELADO desde el 23/09/2026: cambiarlo invalida la "
            "comparación Maestros vs TOP."
        ),
    },
    "PARTIDOS_MAESTROS_TOP_FTR_MIN": {
        "tipo": "float", "default": "0.62", "categoria": "Partidos Maestros",
        "etiqueta": "🔥 TOP — Prob. FTR mínima",
        "descripcion": "Ver el de Elo, arriba. CONGELADO desde el 23/09/2026.",
    },
    "MSG_CAB_partidos_maestros": {
        "tipo": "texto", "default": "⭐ PARTIDO MAESTRO", "categoria": "Partidos Maestros",
        "etiqueta": "Encabezado del mensaje (⭐ Partido Maestro)",
        "descripcion": "Primera línea del mensaje a Telegram. Se manda en negrita (el emoji queda como esté acá).",
    },
    "MSG_CAB_partidos_maestros_top": {
        "tipo": "texto", "default": "🔥 MAESTRO TOP", "categoria": "Partidos Maestros",
        "etiqueta": "Encabezado del mensaje (🔥 Maestro TOP)",
        "descripcion": (
            "Primera línea cuando el partido tiene la insignia TOP. Es el MISMO mensaje, "
            "solo cambia esta línea -- un TOP no manda un segundo aviso."
        ),
    },

    # ── Cuota Mal Puesta V1 (07/10/2026, a pedido de Rubén Darío) ────
    # Motor pre-partido NUEVO: probabilidad FullTenis (Elo + FTR + L10 de
    # los DOS jugadores) contra 1/cuota. Ver
    # production/engines/cuota_mal_puesta.py. TODOS sus números viven acá
    # (punto 25 del pedido: "idealmente una única configuración"); el
    # motor los lee juntos en cada evaluación y guarda la foto entera en
    # cmp_senales.config_json, así cada señal sabe con qué parámetros
    # entró. Defaults = los "parámetros conservadores" del pedido: son
    # PUNTO DE PARTIDA, no están medidos -- se ajustan con resultados.
    **{
        _k: {"tipo": _t, "default": _d, "categoria": "Cuota Mal Puesta", "grupo": _g,
             "etiqueta": _e, "descripcion": _desc}
        for _k, _t, _d, _g, _e, _desc in (
            ("CMP_ACTIVO", "bool", "1", "General", "Motor encendido",
             "Apagado, el motor no evalúa ni guarda nada. No toca a ninguna otra estrategia."),
            ("CMP_GUARDAR_DESCARTES", "bool", "1", "General",
             "Guardar también los ❌ (< umbral de estudio) como control",
             "Una fila por partido, SIN detalle L10, para tener grupo de control en el "
             "diagnóstico. Apagado: los descartes no se guardan."),
            ("CMP_MUESTRA_EXIGIR", "bool", "1", "General", "Exigir muestra suficiente FTR/Elo",
             "Solo evalúa partidos con muestra_suficiente=1 en fixtures_ftr_elo (mismo corte "
             "que Valor Pre-Partido)."),
            ("CMP_UMBRAL_ESTUDIO", "float", "10", "Clasificación (pp)", "🟡 EN ESTUDIO desde (pp)",
             "Desde esta diferencia FT − mercado la señal se guarda completa (con sus 20 "
             "partidos L10). No va a Discord."),
            ("CMP_UMBRAL_CMP", "float", "15", "Clasificación (pp)", "🔥 CUOTA MAL PUESTA desde (pp)",
             "Desde acá puede ir a Discord, si además pasa los controles de abajo."),
            ("CMP_UMBRAL_PREMIUM", "float", "20", "Clasificación (pp)",
             "💎 CUOTA MUY MAL PUESTA desde (pp)",
             "Candidato principal. Es 💎 PREMIUM en Discord solo si además L10 confirma."),
            ("CMP_PESO_ELO", "float", "0.5", "Capa 1 — Elo + FTR", "Peso de Elo en FT base",
             "FT_PROB_BASE = promedio ponderado de Prob. Elo y Prob. FTR del jugador."),
            ("CMP_PESO_FTR", "float", "0.5", "Capa 1 — Elo + FTR", "Peso de FTR en FT base",
             "Ver el de Elo."),
            ("CMP_BASE_MODO", "str", "promedio", "Capa 1 — Elo + FTR", "Cómo combinar Elo y FTR",
             "'promedio' (ponderado con los pesos de arriba) o 'minimo' (el menor de los dos, "
             "más conservador). Nunca se usa el más optimista."),
            ("CMP_PENALIZACION_DESACUERDO_PP", "float", "3", "Capa 1 — Elo + FTR",
             "Penalización si Elo y FTR no coinciden (pp)",
             "Si cada motor ve superior a un jugador distinto, se restan estos puntos a la "
             "probabilidad FT del candidato. Además no va a Discord (ver controles)."),
            ("CMP_L10_PARTIDOS", "int", "10", "Capa 2 — L10", "Partidos del L10",
             "Cuántos partidos recientes (anteriores a la señal) se miran de CADA jugador."),
            ("CMP_L10_MIN_PARTIDOS", "int", "6", "Capa 2 — L10", "Mínimo de partidos para L10",
             "Si alguno de los dos tiene menos, el ajuste L10 es 0 y la señal no puede ir a "
             "Discord ('datos suficientes', punto 16)."),
            ("CMP_L10_TRAMO1_HASTA", "int", "3", "Capa 2 — L10", "Tramo 1: partidos 1 a …",
             "Los más recientes. Por defecto 1-3."),
            ("CMP_L10_TRAMO2_HASTA", "int", "6", "Capa 2 — L10", "Tramo 2: hasta el partido …",
             "Por defecto 4-6. El resto (7-10) es el tramo 3."),
            ("CMP_L10_PESO_TRAMO1", "float", "0.5", "Capa 2 — L10", "Peso tramo 1 (1-3)", "Punto 7."),
            ("CMP_L10_PESO_TRAMO2", "float", "0.3", "Capa 2 — L10", "Peso tramo 2 (4-6)", "Punto 7."),
            ("CMP_L10_PESO_TRAMO3", "float", "0.2", "Capa 2 — L10", "Peso tramo 3 (7-10)", "Punto 7."),
            ("CMP_L10_PESO_OPOSICION", "float", "0.4", "Capa 2 — L10",
             "L10_SCORE: peso calidad de oposición",
             "Percentil del rating (ANTES del partido) de cada rival."),
            ("CMP_L10_PESO_RENDIMIENTO", "float", "0.4", "Capa 2 — L10",
             "L10_SCORE: peso rendimiento ajustado por oposición",
             "Resultado contra lo esperable: ganarle a uno mejor suma mucho, perder con uno "
             "inferior resta mucho."),
            ("CMP_L10_PESO_COMPETITIVIDAD", "float", "0.2", "Capa 2 — L10",
             "L10_SCORE: peso competitividad (% games)", "Punto 6."),
            ("CMP_L10_EXPECTATIVA", "str", "promedio", "Capa 2 — L10",
             "Probabilidad esperada de cada partido L10",
             "'promedio' = FTR de ese momento + Elo de ese momento (si el partido está en el "
             "historial de research Elo; si no, solo FTR). 'ftr' = solo FTR."),
            ("CMP_L10_RIVAL_SUPERIOR_PROB", "float", "0.40", "Capa 2 — L10",
             "Rival 'superior' si la prob. esperada era menor a …",
             "Solo para el corte por tipo de rival (fuertes / parecidos / inferiores)."),
            ("CMP_L10_RIVAL_INFERIOR_PROB", "float", "0.60", "Capa 2 — L10",
             "Rival 'inferior' si la prob. esperada era mayor a …", "Ver el de arriba."),
            ("CMP_L10_PERCENTIL_DIAS", "int", "180", "Capa 2 — L10",
             "Días de match_log para el percentil de oposición",
             "Distribución de ratings contra la que se mide a cada rival (cacheada 6 h)."),
            ("CMP_L10_PP_POR_PUNTO", "float", "0.125", "Capa 2 — L10",
             "Ajuste: pp por cada punto de L10_DIFF",
             "0.125 → ventaja L10 de +24 ≈ +3 pp (el ejemplo del pedido)."),
            ("CMP_L10_AJUSTE_MAX_PP", "float", "5", "Capa 2 — L10", "Ajuste L10 máximo (± pp)",
             "Techo del efecto L10, para que 10 partidos no destruyan la historia Elo/FTR."),
            ("CMP_L10_CONTRADICE_FUERTE", "float", "-15", "Capa 2 — L10",
             "⚠️ L10 CONTRADICE si L10_DIFF ≤ …",
             "Contradicción fuerte: se guarda como 🟡 EN ESTUDIO y no va a Discord."),
            ("CMP_L10_CONFIRMA_MIN", "float", "10", "Capa 2 — L10", "L10 CONFIRMA si L10_DIFF ≥ …",
             "Requisito de 💎 PREMIUM."),
            ("CMP_DISCORD_ACTIVO", "bool", "1", "Discord", "Publicar para Discord",
             "PRENDIDO desde el primer día (07/10/2026, Rubén Darío: 'sí queremos mandar las "
             "mejores desde el primer día; lo que no queremos es llenar Discord'). CUÁNDO: "
             "Discord avisa cuando el partido EMPIEZA (FT Intelligence lo confirma con el feed "
             "en vivo); Telegram, apenas se detecta (interruptor aparte: 'Cuota Mal Puesta — "
             "mandar a Telegram' y su fila en la Matriz de envío). Solo salen "
             "🔥/💎 que pasan los controles; 🟡 y ❌ nunca. Apagado: todo se guarda y cada señal "
             "queda en su grupo 🔥/💎 con el motivo 'Discord cerrado — habría salido como …', "
             "pero nada se marca para FT Intelligence. Al volver a prenderlo solo se publican "
             "las señales NUEVAS; las ya guardadas no se mandan tarde."),
            ("CMP_DISCORD_EXIGIR_ALINEADOS", "bool", "1", "Discord", "Exigir Elo y FTR alineados", ""),
            ("CMP_DISCORD_EXIGIR_L10", "bool", "1", "Discord", "Exigir L10 con datos suficientes", ""),
            ("CMP_PREMIUM_EXIGIR_L10_CONFIRMA", "bool", "0", "Discord",
             "💎 PREMIUM exige además que L10 confirme",
             "Apagado (07/10/2026 tarde, Rubén: 💎 = FT ≥ 65 % y ventaja ≥ 15 pp). Prendido: "
             "además tiene que confirmar L10."),
            ("CMP_CUOTA_MIN", "float", "1.01", "Discord", "Cuota del candidato — mínima (técnica)",
             "Red técnica contra cuotas corruptas, no un filtro de estrategia."),
            ("CMP_CUOTA_MAX", "float", "1000", "Discord",
             "Cuota del candidato — techo TÉCNICO (0 = sin techo)",
             "07/10/2026, Rubén Darío: la cuota alta NO es motivo de descarte. Una cuota 5, 8, 12 "
             "o más con una anomalía enorme de Elo + FTR + L10 se estudia y puede salir. Este "
             "techo existe solo para frenar un dato absurdo/corrupto; la protección real es la "
             "coherencia de las dos cuotas (overround), Elo/FTR, L10 y el PP. Aun fuera de rango, "
             "la señal se guarda y clasifica igual: solo no es publicable."),
            ("CMP_OVERROUND_MIN", "float", "0.98", "Discord", "Overround mínimo (1/c1 + 1/c2)",
             "Fuera de la banda = cuota inconsistente: no va a Discord."),
            ("CMP_OVERROUND_MAX", "float", "1.15", "Discord", "Overround máximo", ""),
            # Puerta de salida (07/10/2026 tarde, Rubén: "varias señales de hoy
            # fueron falsos positivos"). Reemplaza a CMP_DISCORD_PROB_MIN (55 %).
            ("CMP_DISCORD_FT_MIN", "float", "0.60", "Discord",
             "Probabilidad FullTenis mínima para Discord",
             "REGLA (Rubén, 07/10/2026). Va JUNTO con la ventaja mínima: las dos. Debajo, la "
             "señal se guarda y clasifica igual pero queda como ESTUDIO (no Discord ni Telegram)."),
            ("CMP_DISCORD_VENTAJA_MIN_PP", "float", "10", "Discord",
             "Ventaja mínima contra el mercado para Discord (pp)",
             "Probabilidad FullTenis − 1/cuota, en puntos. REGLA (Rubén, 07/10/2026), junto con la "
             "probabilidad mínima. Ej.: FT 61 % / mercado 49 % = +12 → sale; FT 58 % / 38 % = "
             "+20 → solo estudio (no llega a 60 %)."),
            ("CMP_FUERTE_FT_MIN", "float", "0.65", "Discord",
             "💎 FUERTE / PREMIUM: probabilidad FullTenis mínima",
             "Con la ventaja de abajo, la señal se marca FUERTE (columna es_fuerte, siempre) y, si "
             "sale, como 💎 PREMIUM, para medir ese grupo aparte."),
            ("CMP_FUERTE_VENTAJA_MIN_PP", "float", "15", "Discord",
             "💎 FUERTE / PREMIUM: ventaja mínima (pp)", ""),
            ("CMP_DEDUP_HORAS", "int", "48", "Discord", "Ventana de duplicado por par (horas)",
             "El mismo par de jugadores con otro fixture_id dentro de esta ventana no genera "
             "otra señal."),
            # ── V2 (08/10/2026, Rubén): OPOSICIÓN + CARGA + MTO < 24 h. Topes de
            # ARRANQUE, no definitivos: la Fase 7 (apagada) los recalibrará con muestra.
            # V3 (17/10/2026, Daniel/Rubén): la OPOSICIÓN se mide SOLO con el ranking UTR.
            ("CMP_OPOSICION_FUENTE", "str", "utr", "V2 — 🧠 Oposición", "Nivel de los rivales para la OPOSICIÓN",
             "'utr' (V3, 17/10/2026) = UTR del padrón (perfiles usables, exacto) del jugador y de cada rival: "
             "percentil del rival entre los UTR del mismo género y probabilidad esperada por ΔUTR. Partidos con "
             "un rival sin UTR no cuentan; sin UTR del jugador no hay oposición (ajuste 0). 'ftr' = como V2 "
             "(rating FTR/Elo de ese momento)."),
            ("CMP_OPOSICION_UTR_ESCALA", "float", "1.0", "V2 — 🧠 Oposición",
             "ΔUTR → probabilidad esperada (escala logística)",
             "p = 1 / (1 + e^(−ΔUTR / escala)). 1.0 → +1 UTR ≈ 73 %, +2 ≈ 88 %. Valor de ARRANQUE, no calibrado."),
            ("CMP_OPOSICION_MAX_PP", "float", "3", "V2 — 🧠 Oposición", "Ajuste OPOSICIÓN máximo (± pp)",
             "V2 inicial ±3. OPOSICIÓN es el L10 de siempre (mismo cálculo y detalle 10+10) con tope "
             "nuevo. CMP_L10_AJUSTE_MAX_PP queda solo como techo técnico del L10 crudo."),
            ("CMP_OPOSICION_PP_POR_PUNTO", "float", "0.125", "V2 — 🧠 Oposición",
             "Ajuste: pp por cada punto de diferencia de OPOSICIÓN", "0.125 → +24 ≈ +3 pp."),
            ("CMP_OPOSICION_BANDA_MUERTA", "float", "2", "V2 — 🧠 Oposición",
             "Diferencia mínima para ajustar (puntos)", "Por debajo, 'prácticamente no hay diferencia' → 0 pp."),
            ("CMP_CARGA_MAX_PP", "float", "4", "V2 — 🏃 Carga", "Ajuste CARGA máximo (± pp)",
             "V2 inicial ±4. Índice 0–100 comparativo (production/engines/cmp_carga.py); duración "
             "NUNCA se estima (queda NULL)."),
            ("CMP_CARGA_PP_POR_PUNTO", "float", "0.1", "V2 — 🏃 Carga",
             "Ajuste: pp por cada punto de diferencia de carga", "0.1 → 40 puntos ≈ +4 pp."),
            ("CMP_CARGA_BANDA_MUERTA", "float", "10", "V2 — 🏃 Carga",
             "Diferencia mínima para ajustar (puntos)", "Cargas 'similares' → 0 pp."),
            ("CMP_CARGA_CONSIDERABLE", "float", "45", "V2 — 🏃 Carga",
             "PUERTA 1: índice desde el que la carga es CONSIDERABLE",
             "V3 (10/10/2026, Rubén): sin carga considerable en alguno de los dos, 0 pp aunque uno haya "
             "jugado más. PROVISORIO: fijar con la distribución histórica (Cuota Mal Puesta → 📊 Distribución de carga)."),
            ("CMP_CARGA_ALTA", "float", "65", "V2 — 🏃 Carga", "Índice desde el que la carga es ALTA", "Provisorio."),
            ("CMP_CARGA_EXTREMA", "float", "80", "V2 — 🏃 Carga", "Índice desde el que la carga es EXTREMA", "Provisorio."),
            # V3.1 (10/10/2026, Rubén, caso Rakhimova vs Kenin: 2 partidos / 46 games / 4 días de descanso → NORMAL, 0 pp):
            # el índice SOLO no alcanza para CONSIDERABLE; hace falta además acumulación REAL (alguna de estas).
            ("CMP_CARGA_ACUM_PARTIDOS_7D", "int", "4", "V2 — 🏃 Carga",
             "Acumulación real: partidos en 7 días desde los que la carga puede ser CONSIDERABLE",
             "PUERTA ABSOLUTA (V3.1): sin acumulación real (esta, la de 3 días, la de games, doble jornada o poco descanso) "
             "el índice no convierte una semana normal en fatiga. Provisorio."),
            ("CMP_CARGA_ACUM_PARTIDOS_3D", "int", "3", "V2 — 🏃 Carga",
             "Acumulación real: partidos en 3 días", "Provisorio (3 en 3 días = una ronda por día + algo más)."),
            ("CMP_CARGA_ACUM_GAMES_7D", "int", "110", "V2 — 🏃 Carga",
             "Acumulación real: games en 7 días", "Provisorio: 46 games en 2 partidos (Rakhimova) o 60 en 2 (Boisson) son normales."),
            ("CMP_CARGA_ACUM_DESCANSO_MAX", "float", "1", "V2 — 🏃 Carga",
             "Acumulación real: poco descanso (días desde el último partido ≤ esto) con al menos 3 partidos en 7 días",
             "Provisorio. La doble jornada (dos partidos el mismo día) siempre cuenta como acumulación real."),
            ("CMP_INACTIVIDAD_DIAS", "float", "7", "V2 — 🏃 Carga",
             "Días sin competir desde los que se marca INACTIVIDAD (con cobertura)",
             "Descansado ≠ con ritmo: por ahora 0 pp y se guarda dias_desde_ultimo_partido para estudiar 7/10/14/21+."),
            ("CMP_CARGA_MIN_HIST", "int", "5", "V2 — 🏃 Carga",
             "Partidos mínimos en 90 días para confiar en la carga",
             "Con menos, la carga del jugador es None y no se ajusta: menos partidos NO significa "
             "más fresco (puede ser falta de cobertura)."),
            ("CMP_MTO_PP", "float", "3", "V2 — 🩹 MTO", "Ajuste por MTO < 24 h (± pp)",
             "V2 inicial ±3: nuestro jugador con MTO −3, rival +3, los dos 0."),
            ("CMP_MTO_HORAS", "float", "24", "V2 — 🩹 MTO", "Ventana del MTO (horas)",
             "edad_mto = evaluación − MTO; influye solo si 0 ≤ edad ≤ estas horas. Nunca información futura."),
            ("CMP_MTO_CRUCE_RIVAL_EXIGIR", "bool", "1", "V2 — 🩹 MTO",
             "'Apellido + inicial' solo cuenta si el rival del aviso también coincide",
             "mto_alertas no trae player_id. Con esto puesto, un aviso 'Tobon M' sin rival que lo "
             "confirme queda DUDOSO (se guarda, no ajusta)."),
            ("CMP_FACTORES_POSITIVOS_MIN", "int", "1", "Discord",
             "✅ Confirmación: factores favorables mínimos (de 3) para Discord",
             "Regla TEMPORAL (08/10/2026) mientras se afina Elo/FTR: además de FT ≥ 60 % y EDGE ≥ 10, "
             "al menos este número de factores a FAVOR del candidato entre 🧠 Oposición > 0, 🏃 Carga > 0 y "
             "🩹 MTO del rival (+). Tener el dato no alcanza; un MTO propio no cuenta. 0 = sin esta regla. "
             "No cambia el modelo: todo se guarda con factores_positivos (0–3) para comparar en el Excel. "
             "Desde el 19/10/2026 solo se usa con «Regla de publicación» = factores (la de antes)."),
            ("CMP_REGLA_PUBLICACION", "str", "oposicion", "Discord",
             "🔥 Regla de publicación para Discord",
             "19/10/2026 (Rubén y Daniel): «OPOSICIÓN será el único factor de activación». oposicion = FT ≥ 60 % + "
             "EDGE ≥ 10 pp + 🔥 OPOSICIÓN FAVORABLE CONFIRMADA; CARGA y MTO se calculan y guardan igual pero ya no "
             "activan. Lo que cumple FT y EDGE sin OPOSICIÓN confirmada se guarda con NO_OPOSICION_CONFIRMADA. "
             "factores = la regla anterior (≥ 1 factor a favor de 3). Versionada: cada señal guarda regla_publicacion; "
             "el histórico anterior no se recalcula. Valores: «oposicion» (desde 19/10/2026) o «factores» (la de antes)."),
            ("CMP_OPO_CONF_NIVEL_MARGEN", "float", "0", "Discord",
             "🔥 OPOSICIÓN confirmada: «rivales de mayor nivel» — margen sobre los del rival (percentil)",
             "El nivel medio de los rivales recientes del candidato (percentil 0–100, ponderado por recencia) tiene que "
             "SUPERAR al de los rivales recientes de su rival en más que esto. 0 = basta con que sea mayor."),
            ("CMP_OPO_CONF_RENDIMIENTO_MIN", "float", "50", "Discord",
             "🔥 OPOSICIÓN confirmada: «rinde bien o por encima de lo esperado» — rendimiento mínimo",
             "Rendimiento medio del candidato contra esos rivales: 50 + 50·(resultado − probabilidad esperada), "
             "ponderado por recencia. 50 = gana lo que se esperaba; más de 50 = por encima de lo esperado."),
            ("CMP_APRENDIZAJE_ACTIVO", "bool", "0", "V2 — Fase 7 (apagada)",
             "Autoaprendizaje de pesos ENCENDIDO",
             "APAGADO (Rubén, 08/10/2026): primero acumular resultados limpios. Encendido, solo "
             "PROPONE una versión nueva en cmp_modelo_versiones cuando hay muestra; nunca reescribe "
             "predicciones viejas."),
            ("CMP_APRENDIZAJE_MIN_N", "int", "300", "V2 — Fase 7 (apagada)",
             "Muestra mínima (señales cerradas de la versión vigente)", ""),
            ("CMP_APRENDIZAJE_CAMBIO_MAX_PP", "float", "1", "V2 — Fase 7 (apagada)",
             "Cambio máximo de cada tope por recalibración (pp)", "Cambios graduales, nunca de golpe."),
        )
    },
    "MENSAJES_CORTOS": {
        "tipo": "bool", "default": "1", "categoria": "Mensajes de Discord",
        "etiqueta": "✂️ Mensajes cortos en Discord",
        "descripcion": (
            "19/10/2026, Rubén: «que todos los mensajes que llegan a Discord sean más cortos». Prendido: Cuota Mal "
            "Puesta, UTR VALUE, Movimiento Rentable, Ventaja Leve y Pasan Filtro salen con el formato corto (jugador "
            "recomendado, cuota, probabilidad y un ¿por qué? en palabras simples, hora de Colombia). Telegram, la "
            "señal y lo guardado no cambian. Apagado: los mensajes de antes."
        ),
    },
    "MSG_CAB_cmp": {
        "tipo": "texto", "default": "🔥 FULLTENIS · CUOTA MAL PUESTA", "categoria": "Cuota Mal Puesta",
        "grupo": "Mensaje", "etiqueta": "Encabezado — 🔥 Cuota mal puesta",
        "descripcion": "Primera línea del mensaje de Discord (15-19.9 pp).",
    },
    "MSG_CAB_cmp_muy": {
        "tipo": "texto", "default": "💎 FULLTENIS · CUOTA MUY MAL PUESTA",
        "categoria": "Cuota Mal Puesta", "grupo": "Mensaje",
        "etiqueta": "Encabezado — 💎 Cuota muy mal puesta",
        "descripcion": "Primera línea del mensaje de Discord (≥ umbral premium).",
    },
    "CMP_FRASE_PREMIUM": {
        "tipo": "texto", "categoria": "Cuota Mal Puesta", "grupo": "Mensaje",
        "default": ("FullTenis considera al jugador claramente superior al precio que está "
                    "ofreciendo el mercado. El contexto reciente también confirma la ventaja."),
        "etiqueta": "Frase final — 💎 PREMIUM", "descripcion": "Vacía = sin frase.",
    },
    "CMP_FRASE_CMP": {
        "tipo": "texto", "categoria": "Cuota Mal Puesta", "grupo": "Mensaje",
        "default": ("FullTenis le da al jugador más probabilidad de la que paga el mercado. "
                    "El contexto reciente no la contradice."),
        "etiqueta": "Frase final — 🔥", "descripcion": "Vacía = sin frase.",
    },

    # ── UTR VALUE (07/10/2026, a pedido de Rubén Darío) ──────────────
    # Motor pre-partido: jugador CLARAMENTE SUPERIOR POR UTR al que el
    # mercado paga demasiado. Ver production/engines/utr_value.py. Las dos
    # puertas mínimas (ΔUTR ≥ 0.75 y cuota ≥ 1.70) son REGLA OBLIGATORIA del
    # pedido para Discord; la base de datos guarda todo igual. El resto son
    # filtros de calidad con valores de arranque, no medidos todavía, y TODOS
    # son de UTR o de cuota: Elo/FTR/MatchUp no confirman ni descartan nada
    # (corrección de Rubén, 07/10/2026; se sacaron UTRV_CONTRADICCION_ELO_FTR,
    # UTRV_FILTRO_MATCHUP y UTRV_MATCHUP_ESPERA_MIN). Más tarde, el mismo día:
    # la Prob. UTR es MÉTRICA DE ESTUDIO, no filtro -- se sacaron
    # UTRV_DISCORD_PROB_MIN y UTRV_VALUE_MIN_PP; superficie queda apagada.
    # UTR EXTERNO (09/10/2026, Rubén): las alertas LIVE VALUE / PREMATCH
    # VALUE del canal de Telegram DOC | TIRS, en nuestro formato y a Discord,
    # mientras se completa el padrón UTR. Ver production/engines/utr_externo.py.
    **{
        _k: {"tipo": _t, "default": _d, "categoria": "UTR EXTERNO", "grupo": _g,
             "etiqueta": _e, "descripcion": _desc}
        for _k, _t, _d, _g, _e, _desc in (
            ("UTRX_ACTIVO", "bool", "1", "General", "Recibir las alertas del canal",
             "Apagado: el reenviador manda y acá se ignora (no se guarda nada)."),
            ("UTRX_DISCORD", "bool", "1", "Avisos", "Publicar para Discord",
             "Apagado: se guardan para estudio pero no suenan."),
            ("UTRX_LIVE", "bool", "1", "Avisos", "Suenan las LIVE VALUE (en vivo)", "En el acto, con marcador."),
            ("UTRX_PREMATCH", "bool", "1", "Avisos", "Suenan las PREMATCH VALUE",
             "Si el partido está en nuestras fuentes con hora, minutos antes; si no, en el acto."),
            ("UTRX_SOLO_SI_CUMPLE", "bool", "0", "Avisos", "Solo las que cumplen nuestras reglas",
             "0 = suena todo lo del canal (pedido de Rubén, 09/10/2026). 1 = solo lo que cumpliría UTR VALUE "
             "(ΔUTR ≥ UTRV_DELTA_MIN y cuota ≥ UTRV_CUOTA_MIN) o UTR MARKET ANOMALY (ΔUTR ≥ UTRA_DELTA_MIN y "
             "cuota ≥ UTRA_CUOTA_MIN; en vivo solo a 0-0). El mensaje es el MISMO que el de la estrategia "
             "(LIVE → UTR MARKET ANOMALY; PREMATCH → UTR VALUE si cumple, si no UTR MARKET ANOMALY)."),
            ("UTRX_DEDUP_HORAS", "int", "6", "Avisos", "Ventana de un mismo partido (horas)",
             "El canal repite el mismo partido con cada cuota nueva: suena una vez por ventana; lo demás "
             "se guarda con motivo REPETIDA."),
        )
    },

    # UTR POWER -- UTR REFRESH MANAGER (08/10/2026, Rubén; ver
    # production/engines/utr_power.py). Credenciales SOLO en Railway
    # (UTR_EMAIL / UTR_PASSWORD), nunca acá.
    **{
        _k: {"tipo": _t, "default": _d, "categoria": "UTR POWER", "grupo": _g,
             "etiqueta": _e, "descripcion": _desc}
        for _k, _t, _d, _g, _e, _desc in (
            ("UTRP_ACTIVO", "bool", "0", "General", "Refresco UTR Power encendido",
             "Trae el UTR exacto (con decimales) de los perfiles con UTR ID usando la cuenta UTR Power "
             "del proyecto (UTR_EMAIL / UTR_PASSWORD en Railway). No cambia identidades: un perfil 'por "
             "confirmar' sigue por confirmar aunque llegue el número."),
            ("UTRP_PRESUPUESTO_DIARIO", "int", "700", "Límites", "Consultas por día (techo propio)",
             "Techo de seguridad nuestro, por día UTC. No es una suposición sobre el límite real de UTR: "
             "si UTR responde 429 antes, se para igual."),
            ("UTRP_MAX_POR_VUELTA", "int", "120", "Límites", "Consultas por vuelta",
             "Para repartir las consultas a lo largo del día en vez de hacerlas todas juntas."),
            ("UTRP_PAUSA_SEG", "float", "1.25", "Límites", "Segundos entre consultas", ""),
            ("UTRP_COOLDOWN_429_MIN", "int", "360", "Límites", "Pausa después de un HTTP 429 (min)",
             "Con un 429 la vuelta se para en el acto y no se vuelve a consultar hasta que pase este tiempo "
             "(o el Retry-After de UTR, si es mayor). Sin rotar IP ni cuentas."),
            ("UTRP_VUELTA_MIN", "int", "10", "Frecuencia", "Minutos entre vueltas", ""),
            # 09/10/2026 (Rubén, caso Beviz vs Fruhvirtova "sin dato en el padrón").
            ("UTRP_BUSQUEDA_ACTIVA", "bool", "1", "Jugadores nuevos",
             "Buscar por nombre a los jugadores nuevos del circuito",
             "En cada vuelta, los jugadores de los próximos partidos del circuito UTR (OddsPapi) que no están en el "
             "padrón se buscan por nombre en UTR con la misma cuenta y presupuesto, y se agregan al padrón: un solo "
             "candidato con el nombre igual → MATCH (ALTA); apellido + inicial → MATCH_VARIANTE (por confirmar); "
             "varios → REVISAR; ninguno → se reintenta a los 7 días."),
            ("UTRP_BUSQUEDAS_POR_VUELTA", "int", "15", "Jugadores nuevos", "Búsquedas por nombre por vuelta",
             "Cada búsqueda cuenta como una consulta del presupuesto diario."),
            ("UTRP_FRESCO_HORAS", "int", "24", "Frecuencia", "Un UTR se considera fresco por (horas)",
             "Lo fresco no se vuelve a pedir. Mantenimiento: lo más viejo primero."),
            ("UTRP_FRESCO_HORAS_PROXIMOS", "int", "12", "Frecuencia",
             "Jugadores con partido en 48 h: refrescar si tiene más de (horas)",
             "Prioridad 1: partido en las próximas 24 h; 2: en 24–48 h."),
        )
    },
    # ⏰ RECORDADOR (08/10/2026, Rubén: "se debe decir en el canal de Discord
    # minutos antes o cuando ya estén en juego"). Ver production/engines/recordador.py.
    **{
        _k: {"tipo": _t, "default": _d, "categoria": "Recordador", "grupo": _g,
             "etiqueta": _e, "descripcion": _desc}
        for _k, _t, _d, _g, _e, _desc in (
            ("RECORDADOR_MINUTOS_ANTES", "int", "5", "Cuándo avisar", "Primer aviso: estos minutos antes de la hora programada vigente",
             "v43 (11/10/2026, Rubén): DOS avisos por partido. El primero a estos minutos de la hora programada (solo si "
             "el fixture trae hora); el segundo al detectar el inicio real (marcador en vivo / circuito UTR). "
             "Sin hora fiable, solo el de inicio."),
            ("RECORDADOR_TARDE_MAX_MIN", "int", "30", "Cuándo avisar",
             "No dar el primer aviso si pasaron más de (minutos) de la hora sin verlo en juego",
             "Para no decir tarde un partido que se canceló o no se ve en vivo: se pasa al siguiente."),
            ("RECORDADOR_DISCORD_ACTIVO", "bool", "1", "Salida", "Publicar en Discord",
             "FT Intelligence lo lee de /ft-intel/recordador y lo dice en el canal (texto y voz). "
             "Telegram va por la Matriz de envío (categoría ⏰ Recordador)."),
            ("RECORDADOR_IA_ACTIVO", "bool", "1", "🧠 Investigación", "Investigar a los dos jugadores y guardar la probabilidad BASE",
             "v43: con el próximo partido conocido, FullTennis investiga a los dos (FTR, Elo, UTR, carga, oposición, MTO y "
             "retiros), interpreta la observación del usuario y guarda TODO versionado antes del partido; después cierra "
             "el resultado y mide Brier / log loss / calibración. Nunca inventa penalizaciones por molestias."),
            ("RECORDADOR_IA_DISCORD", "bool", "1", "🧠 Investigación", "Avisos de Discord con el formato nuevo (🩹 RECORDATORIO IA + 🔴 EN VIVO)",
             "1 = (activado por Rubén el 11/10/2026 para toda la comunidad) los dos avisos salen con el formato "
             "aprobado: RECORDADOR_IA (5 min antes de la hora programada) y RECORDADOR_VIVO (inicio real), cada uno "
             "con su fila en Admin → Discord. 0 = modo prueba: los avisos salen como siempre (⏰ RECORDADOR) y la "
             "investigación queda solo en la pestaña."),
            ("RECORDADOR_OBS_PP", "float", "3", "🧠 Investigación", "Ajuste por la observación del usuario (pp en contra del jugador observado)",
             "v43c (Rubén: 'o el mensaje del usuario'). Solo observaciones físicas (cojera, caída, fisio, cansancio, "
             "saque alterado…); 'nada' o 'lenguaje corporal' no mueven. Se guarda aparte de la BASE y la Precisión "
             "mide las dos: si la ajustada no acierta más, se baja a 0."),
            ("RECORDADOR_DASH_PROB_MIN", "float", "60", "🧠 Investigación", "Dashboard: probabilidad mínima del favorito (%)",
             "v43d (Rubén): el dashboard de la pestaña suma las alertas dichas en Discord cuyo favorito FullTennis llega a este %."),
            ("RECORDADOR_INVESTIGAR_HORAS", "int", "240", "🧠 Investigación", "Preparar la investigación estas horas antes del partido",
             "Así el aviso de inicio sale en el acto (no se recalcula al empezar). En el primer aviso se reconfirma "
             "por si cambió la cuota o la hora; si cambió, queda una versión nueva (la anterior no se toca)."),
        )
    },
    # 🏆 IMPLICACIÓN DE TORNEO (12/10/2026, Fase 0). Interruptores en caliente: se
    # releen en cada vuelta (caché de 10 s), sin redeploy. Ver production/engines/implicacion/.
    # v44c (subida controlada): los tres vienen APAGADOS de fábrica. Se prenden juntos con
    # «✅ Activar Fase 0» en la pestaña, que exige el ranking ATP y WTA verificados.
    **{
        _k: {"tipo": _t, "default": _d, "categoria": "Implicación de Torneo", "grupo": _g,
             "etiqueta": _e, "descripcion": _desc}
        for _k, _t, _d, _g, _e, _desc in (
            ("IMPLICACION_ACTIVO", "bool", "0", "Interruptores", "Módulo encendido (registro, set 1, cierre)",
             "Apagado, la tarea no lee ni escribe NADA en la vuelta siguiente (≤ 30 s, sin redeploy). Lo ya guardado "
             "queda. No toca ningún otro motor. Para apagarlo además en el arranque: variable IMPLICACION_ENABLED=0."),
            ("IMPLICACION_RANKING_ACTIVO", "bool", "0", "Interruptores", "Captura semanal del ranking oficial",
             "Apagado, no se llama a la API de ranking (SportsAPI365). Aunque esté prendido, cada tour solo se captura si su "
             "última «Verificar ranking (prueba real)» dio OK (o REVISAR aprobada a mano)."),
            ("DEFENSA_SNAPSHOT_ACTIVO", "bool", "0", "Interruptores", "Snapshot semanal de Defensa de Puntos",
             "Apagado, la tarea de defensa no calcula (se relee en cada chequeo horario)."),
            ("IMPLICACION_CUOTA_VENTANA_SEG", "int", "300", "Cuota al perder el set 1",
             "Ventana para verificar la cuota (segundos después del cierre del set 1)",
             "La cuota es el primer precio en vivo POSTERIOR al cierre dentro de esta ventana. Si además el cierre fue "
             "presenciado → 'verificada' (entra al ROI principal). Si no se puede probar → 'no_verificada' (ROI aparte)."),
            ("IMPLICACION_VUELTA_SEG", "int", "20", "Cuota al perder el set 1", "Cada cuántos segundos corre la vuelta",
             "Mínimo 10. La cuota se busca en el historial de precios (14 días), así que una vuelta más lenta no pierde el "
             "precio; lo que caduca es la fila del partido en vivo (3–8 h)."),
            # 🏆 MOTOR DE PICKS v1 (13/10/2026). Ver production/engines/implicacion/picks.py.
            ("IMP_PICKS_ACTIVO", "bool", "1", "Picks (reglas v1)", "Detectar picks",
             "Con el módulo encendido, evalúa cada partido en el que el favorito PRE pierde el 1.er set y guarda los picks "
             "(experimentales mientras la regla no esté validada)."),
            ("IMP_DEFENSA_PTS_MIN", "int", "20", "Picks (reglas v1)", "Implicación: puntos mínimos que defiende",
             "El que perdió el set tiene que defender al menos estos puntos esta semana."),
            ("IMP_DEFENSA_PCT_MIN", "float", "10", "Picks (reglas v1)", "Implicación: % mínimo de sus puntos",
             "…y que esos puntos sean al menos este % de sus puntos del ranking oficial (lo que representan para SU ranking)."),
            ("IMP_CUOTA_MIN", "float", "1.50", "Picks (reglas v1)", "Cuota en vivo mínima", "Cuota en vivo verificada del pick, mínimo."),
            ("IMP_CUOTA_MAX", "float", "5.00", "Picks (reglas v1)", "Cuota en vivo máxima", "Cuota en vivo verificada del pick, máximo."),
            ("IMP_CUOTA_FRESCA_SEG", "int", "120", "Picks (reglas v1)", "Cuota LIVE al enviar: antigüedad máxima (s)",
             "Antes de enviar a Discord se toma el último precio en vivo de Pinnacle de estos segundos (cuota ACTUAL). Sin precio "
             "fresco se espera; pasados IMP_DETECCION_MAX_MIN minutos no se envía. Fuera de IMP_CUOTA_MIN–MAX tampoco."),
            ("IMP_UTR_DELTA_MIN", "float", "0.25", "Picks (reglas v1)", "R2: UTR superior por al menos",
             "IMP-R2 = R1 + UTR exacto del jugador por encima del rival en al menos esta diferencia."),
            ("IMP_DETECCION_MAX_MIN", "int", "20", "Picks (reglas v1)", "Minutos máximos tras el cierre del set",
             "Más tarde que esto no se crea el pick: la cuota ya no estaría disponible."),
            ("IMP_VALIDACION_N_MIN", "int", "50", "Picks (reglas v1)", "Validación: picks mínimos con resultado",
             "Criterio 1 de 5 para poder pasar una regla de experimental a validada (ver la pestaña de administración)."),
            ("IMP_DISCORD_ACTIVO", "bool", "0", "Picks (reglas v1)", "Publicar picks VALIDADOS en Discord",
             "Apagado de fábrica. Aunque esté prendido, los experimentales NUNCA se publican: solo los de reglas validadas."),
            # 🏆 v47 (13/10/2026, Rubén: "que empiece a sonar en Discord ... las señales experimentales deben enviarse
            # claramente identificadas como 🧪 EXPERIMENTAL — EN ESTUDIO ... mediante una opción independiente").
            ("IMP_DISCORD_EXPERIMENTAL", "bool", "0", "Picks (reglas v1)", "Avisar también los EXPERIMENTALES (🧪 EN ESTUDIO)",
             "Opción independiente: los picks de las reglas de IMP_DISCORD_EXPERIMENTAL_REGLAS salen a Discord marcados "
             "«🧪 EXPERIMENTAL — EN ESTUDIO», sin cambiar su estado (siguen experimentales). Necesita IMP_DISCORD_ACTIVO = 1."),
            ("IMP_DISCORD_EXPERIMENTAL_REGLAS", "str", "IMP-R3", "Picks (reglas v1)", "Reglas experimentales que se avisan",
             "Separadas por coma. De fábrica solo IMP-R3 (implicación alta)."),
            ("IMP_DISCORD_AUTO_ACTIVAR", "bool", "1", "Picks (reglas v1)", "Encender solo tras la prueba del circuito",
             "Cuando la 🧪 prueba del circuito vuelve confirmada por FT Discord (texto en el canal Y voz), se encienden "
             "IMP_DISCORD_ACTIVO e IMP_DISCORD_EXPERIMENTAL automáticamente. No toca a ningún otro bot."),
            # 🏆 v46 (13/10/2026): NIVEL DE IMPLICACIÓN. Ver production/engines/implicacion/importancia.py.
            # Es una clasificación descriptiva (no una probabilidad); el estudio de candidatos dice si sirve.
            ("IMP_NIVEL_ALTA_DEF_PCT", "float", "25", "Nivel de implicación (v46)", "🔴 ALTA: % de sus puntos que defiende",
             "Defiende al menos este % de sus puntos del ranking oficial."),
            ("IMP_NIVEL_MEDIA_DEF_PCT", "float", "10", "Nivel de implicación (v46)", "🟡 MEDIA: % de sus puntos que defiende", "Defiende al menos este % de sus puntos (y menos que el de ALTA)."),
            ("IMP_NIVEL_ALTA_POS_MIN", "int", "10", "Nivel de implicación (v46)", "🔴 ALTA: puestos que perdería si pierde (mínimo)",
             "Perdería al menos max(este número, IMP_NIVEL_ALTA_POS_REL % de su ranking) puestos. Proyección estática."),
            ("IMP_NIVEL_ALTA_POS_REL", "float", "10", "Nivel de implicación (v46)", "🔴 ALTA: puestos que perdería (% de su ranking)", "Ej.: 10 → un #200 tiene que poder caer 20 puestos o más."),
            ("IMP_NIVEL_MEDIA_POS_MIN", "int", "3", "Nivel de implicación (v46)", "🟡 MEDIA: puestos que perdería (mínimo)", "Perdería al menos max(este número, IMP_NIVEL_MEDIA_POS_REL % de su ranking) puestos."),
            ("IMP_NIVEL_MEDIA_POS_REL", "float", "3", "Nivel de implicación (v46)", "🟡 MEDIA: puestos que perdería (% de su ranking)", "Ej.: 3 → un #200 tiene que poder caer 6 puestos o más."),
            ("IMP_TOPS", "str", "10,20,50,100,200", "Nivel de implicación (v46)", "Objetivos Top N",
             "Cortes del ranking que se miran (corte = puntos del #N del ranking oficial publicado)."),
            ("IMP_FINALS_MESES", "str", "8,9,10,11", "Nivel de implicación (v46)", "Race a las Finals: meses en que se mira",
             "Solo en estos meses se considera la Race (corte = puntos del #IMP_FINALS_PLAZAS de la Race publicada)."),
            ("IMP_FINALS_PLAZAS", "int", "8", "Nivel de implicación (v46)", "Race a las Finals: plazas", "Plazas de las Finals (8 en ATP y WTA). El corte es el #N de la Race publicada."),
            ("IMP_FINALS_MARGEN", "int", "4", "Nivel de implicación (v46)", "Race a las Finals: margen de puestos", "Se mira a quien esté dentro de las plazas + este margen."),
            # v56 (17/10/2026, Rubén): clasificación por CONSECUENCIAS con datos comprobados (verificacion.py).
            ("IMP_CLASIFICACION", "str", "v2", "Nivel de implicación (v46)", "Clasificación que deciden picks y Discord",
             "v2 (por defecto desde el 17/10/2026, Rubén) = por consecuencias, solo con datos OFICIALES/COMPROBADOS: solo la "
             "ALTA comprobada hace pick (IMP-R3); R1/R2 (por % que defiende) no; lo estimado queda rotulado y lo SIN CONFIRMAR "
             "no hace pick. v1 = la antigua (por % que defiende y puestos)."),
            ("IMP_DEFENSA_CON_MARGEN", "bool", "1", "Nivel de implicación (v46)",
             "v2: aceptar consecuencias que se cumplen AUN con el margen de error de la defensa",
             "19/10/2026 (Rubén: «quiero que funcione y empiece a alertar»). La defensa casi nunca queda COMPROBADA (exige el "
             "desglose oficial completo; los resultados son derivados). Prendido: con ranking y ronda comprobados y la regla de "
             "conteo verificada, una consecuencia (Top N, caída de puestos) cuenta como hecho si se cumple aun corriendo los "
             "puntos proyectados en contra tanto como difiere nuestro total del oficial. Si el error podría cambiar la "
             "conclusión, no cuenta. Apagado: solo con defensa COMPROBADA (como antes)."),
        )
    },
    **{
        _k: {"tipo": _t, "default": _d, "categoria": "UTR VALUE", "grupo": _g,
             "etiqueta": _e, "descripcion": _desc, **({"opciones": _o} if _o else {})}
        for _k, _t, _d, _g, _e, _desc, _o in (
            ("UTRV_ACTIVO", "bool", "1", "General", "Motor encendido",
             "Apagado, no evalúa ni guarda nada. No toca a ninguna otra estrategia.", None),
            ("UTRV_INTERVALOS", "bool", "1", "General",
             "Usar el UTR oculto (11.xx) por intervalos",
             "11.xx = entre 11.00 y 11.99, sin inventar decimales. Si el peor caso ya llega a "
             "+0.75 se evalúa como siempre; si ni el mejor caso llega, se descarta "
             "(UTR_INTERVALO_NO_ALCANZA); si los decimales cambian la decisión, o la identidad "
             "está por confirmar, va a ⚠️ revisión manual por Telegram (solo con cuota ≥ 1.70). "
             "Las reglas (+0.75 y 1.70) no cambian. Apagado: un UTR oculto es SIN_UTR, como antes.",
             None),
            ("UTRV_GUARDAR_SIN_UTR", "bool", "1", "General",
             "Guardar también los partidos SIN UTR (cobertura)",
             "Una fila mínima por partido al que le falta UTR usable de alguno de los dos, con el "
             "motivo. Sirve para medir cobertura. Si después se sube UTR y el partido todavía no "
             "empezó, se reevalúa completo.", None),
            ("UTRV_TABLA_PROB", "texto",
             "0.10:0.748, 0.20:0.781, 0.25:0.800, 0.30:0.807, 0.40:0.837, 0.50:0.862, "
             "0.75:0.883, 1.00:0.868, 1.25:0.941",
             "Probabilidad UTR", "Tabla de probabilidad por ΔUTR (umbral:probabilidad)",
             "El histórico inicial de Rubén (ΔUTR o más → % que ganó el UTR superior). NO son "
             "probabilidades definitivas (pueden tener look-ahead): se usan para calcular UTR "
             "VALUE como MÉTRICA DE ESTUDIO -- nunca aprueban ni descartan una señal -- mientras "
             "la pestaña compara cada umbral contra los partidos nuevos. Se toma la fila con el "
             "mayor umbral que el ΔUTR alcanza; debajo del primero no hay estimación.", None),
            ("UTRV_MENSAJE_PROB", "bool", "0", "Probabilidad UTR",
             "Mostrar Prob. UTR y UTR VALUE en el mensaje",
             "Apagado de arranque: la tabla no está validada y no se quiere presentar su % como "
             "probabilidad real. Se guarda y se ve en la pestaña igual.", None),
            ("UTRV_DELTA_MIN", "float", "0.75", "Puertas mínimas (obligatorias)",
             "ΔUTR mínimo para ser candidato",
             "REGLA OBLIGATORIA (Rubén, 07/10/2026). Debajo: SOLO_ESTUDIO_DELTA_BAJO.", None),
            ("UTRV_CUOTA_MIN", "float", "1.70", "Puertas mínimas (obligatorias)",
             "Cuota mínima del jugador UTR superior",
             "REGLA OBLIGATORIA (Rubén, 07/10/2026). Debajo: SOLO_ESTUDIO_CUOTA_BAJA. NO hay "
             "cuota máxima. Si la cuota sube a este valor antes de empezar, el partido pasa a "
             "candidato en ese momento, con esa cuota.", None),
            ("UTR_VERIFICAR_ALERTAS", "bool", "1", "Controles técnicos del dato",
             "Alertar SOLO con el UTR verificado de hoy (UTR VALUE, UTR MARKET ANOMALY, UNDER UTR)",
             "24/10/2026 (Rubén: «no puede pasar que en UTR tenga un ranking y el nuestro otro; las alertas siempre "
             "deberían ser verídicas»). Antes de publicar, el UTR de los DOS jugadores tiene que ser exacto (un "
             "«N.xx» oculto no vale), de un perfil con identidad confirmada y traído por UTR Power (o la captura) "
             "hace ≤ UTR_VERIFICADO_HORAS. La alerta usa y muestra ESOS números. Si falta, no sale: UTR VALUE espera "
             "hasta el inicio por si Power lo trae; si no, queda «UTR_NO_VERIFICADO_A_TIEMPO».", None),
            ("UTR_VERIFICADO_HORAS", "int", "24", "Controles técnicos del dato",
             "UTR verificado: antigüedad máxima (horas)",
             "Power refresca cada 12 h a los jugadores con partido en 48 h (UTRP_FRESCO_HORAS_PROXIMOS).", None),
            ("UTRV_DELTA_SOSPECHOSO", "float", "3.0", "Controles técnicos del dato",
             "ΔUTR inverosímil → revisión manual",
             "23/10/2026 (Lambert «8.xx» vs Grechkina «0.xx»: «al menos +7.01» con Lambert a 10.00, y la Grechkina "
             "real tiene 10.71). Una diferencia de UTR así de grande en un partido donde el superior paga ≥ la cuota "
             "mínima es casi siempre un perfil equivocado o sin rating: no sale sola, va a la revisión manual "
             "(Telegram) para que una persona mire los dos perfiles. 0 = apagado.", None),
            ("UTRV_DISCORD_CONFIANZA", "str", "ALTA", "Controles técnicos del dato",
             "Confianza mínima de la identidad UTR (los dos)",
             "ALTA = solo MATCH (nombre idéntico, perfil pro). MEDIA = también MATCH_VARIANTE. "
             "Si no la cumple: DESCARTADO_CALIDAD_UTR.",
             [{"valor": "ALTA", "etiqueta": "ALTA (solo MATCH)"},
              {"valor": "MEDIA", "etiqueta": "MEDIA (también MATCH_VARIANTE)"}]),
            ("UTRV_UTR_MAX_DIAS", "int", "30", "Controles técnicos del dato",
             "Antigüedad máxima del UTR capturado (días)",
             "Si el UTR de alguno se capturó hace más que esto: DESCARTADO_CALIDAD_UTR. 0 = no "
             "mirar la fecha.", None),
            ("UTRV_FILTRO_SUPERFICIE", "bool", "0", "Opcionales (apagados)",
             "Descartar si el UTR de la superficie contradice",
             "APAGADO (Rubén, 07/10/2026: las dos puertas son la única regla deportiva). "
             "Prendido: si los dos tienen UTR de la superficie y ahí el rival es mejor, "
             "DESCARTADO_CONTRADICCION_SUPERFICIE. El UTR de superficie se guarda siempre.", None),
            ("UTRV_DERIVA_MAX_PCT", "float", "0", "Opcionales (apagados)",
             "Movimiento de mercado: subida máxima desde la apertura (%)",
             "0 = apagado (de arranque solo se guarda el movimiento). Con un valor, si la cuota "
             "del UTR superior subió más que eso desde la apertura: DESCARTADO_MOVIMIENTO_MERCADO.",
             None),
            ("UTRV_OVERROUND_MIN", "float", "0.98", "Controles técnicos del dato",
             "Overround mínimo (coherencia de las dos cuotas)", "Técnico, no estratégico.", None),
            ("UTRV_OVERROUND_MAX", "float", "1.15", "Controles técnicos del dato", "Overround máximo",
             "Técnico, no estratégico.", None),
            ("UTRV_CUOTA_MAX_TECNICA", "float", "1000", "Controles técnicos del dato",
             "Techo TÉCNICO de cuota (0 = sin techo)",
             "Solo contra datos corruptos. NO es una cuota máxima de estrategia.", None),
            ("UTRV_DISCORD_ACTIVO", "bool", "1", "Avisos", "Publicar para Discord",
             "Discord avisa cuando el partido EMPIEZA (FT Intelligence); Telegram al aprobarse "
             "(interruptor aparte: 'UTR VALUE — mandar a Telegram' + su fila en la Matriz de "
             "envío). Apagado: la aprobación queda como APROBADO_DISCORD_CERRADO.", None),
            ("UTRV_DEDUP_HORAS", "int", "48", "Avisos", "Ventana de duplicado por par (horas)",
             "El mismo partido con otro fixture_id no se guarda ni se avisa dos veces.", None),
            # Fuente OddsPapi (07/10/2026): partidos del circuito UTR, que
            # SportsAPI365/PinnOdds no traen. Ver production/engines/oddspapi_utr.py.
            ("UTRV_ODDSPAPI_ACTIVO", "bool", "1", "Fuente OddsPapi (circuito UTR)",
             "Traer los partidos del circuito UTR desde OddsPapi",
             "Partidos UTR Pro Tennis Tour con la cuota de la casa contratada, SOLO para UTR VALUE "
             "(no entran a las demás estrategias). Necesita la variable ODDSPAPI_KEY en Railway. "
             "Apagado: no se hace ninguna petición.", None),
            ("UTRV_ODDSPAPI_CASA", "str", "fonbet", "Fuente OddsPapi (circuito UTR)",
             "Casa de la cuota (slug de OddsPapi)",
             "La contratada en el plan de OddsPapi (desde el 17/10/2026: fonbet; antes 1xbet). Queda en la fila "
             "(fuente 'oddspapi/<casa>') y el mensaje dice de qué casa es la cuota.", None),
            ("UTRV_ODDSPAPI_TOPE_DIARIO", "int", "1000", "Fuente OddsPapi (circuito UTR)",
             "Tope de peticiones por día (UTC)",
             "Plan de 100.000 por mes (17/10/2026): 1.000/día para UTR VALUE y 2.000/día aparte para el UNDER UTR "
             "con Fonbet (UNDERUTR_FONBET_TOPE_DIARIO). Al llegar, se para hasta el día siguiente. "
             "Los resultados solo usan el último 15 % si sobra.", None),
            ("UTRV_ODDSPAPI_FIXTURES_MIN", "int", "60", "Fuente OddsPapi (circuito UTR)",
             "Lista de partidos: cada N minutos", "1 petición trae todo el tenis de ayer a +2 días.",
             None),
            ("UTRV_ODDSPAPI_CUOTAS_MIN", "int", "30", "Fuente OddsPapi (circuito UTR)",
             "Cuotas: cada N minutos",
             "1 petición cada 5 torneos UTR con partidos por jugarse; solo si los hay.", None),
            ("UTRV_ODDSPAPI_HORAS_ADELANTE", "int", "24", "Fuente OddsPapi (circuito UTR)",
             "Cuotas: partidos que empiezan en las próximas N horas", "", None),
            ("UTRV_ODDSPAPI_INICIO_MIN", "int", "5", "Fuente OddsPapi (circuito UTR)",
             "Señales aprobadas: mirar si empezó cada N minutos",
             "Discord avisa cuando el partido EMPIEZA; estos partidos no están en el feed en vivo de "
             "FullTenis, así que se pregunta a OddsPapi, desde 10 min antes de la hora y hasta 4 h "
             "después. 1 petición por señal aprobada en cada vuelta.", None),
            ("UTRV_CIERRE_VENTANA_MIN", "int", "90", "Avisos",
             "Cuota de cierre: anotar en los últimos N minutos",
             "La última cuota pre-partido se anota solo cerca del inicio, para no escribir cada "
             "movimiento del día de todos los partidos guardados.", None),
        )
    },
    "MSG_CAB_utr_value": {
        "tipo": "texto", "default": "💎 FULLTENIS · UTR VALUE", "categoria": "UTR VALUE",
        "grupo": "Mensaje", "etiqueta": "Encabezado del mensaje",
        "descripcion": "Primera línea en Telegram (en negrita) y en Discord.",
    },
    "UTRV_FRASE": {
        "tipo": "texto", "categoria": "UTR VALUE", "grupo": "Mensaje",
        "default": ("UTR lo ve claramente superior y el mercado lo está pagando demasiado alto."),
        "etiqueta": "Frase final", "descripcion": "Vacía = sin frase.",
    },

    # ── UTR MARKET ANOMALY (07/10/2026, a pedido de Rubén Darío) ─────
    # Señal SEPARADA de UTR VALUE (no la toca): UTR superior por poco y
    # el mercado lo paga a una cuota extrema. Pre-partido y EN VIVO, con
    # cada cuota nueva. Ver production/engines/utr_anomalia.py.
    **{
        _k: {"tipo": _t, "default": _d, "categoria": "UTR MARKET ANOMALY", "grupo": _g,
             "etiqueta": _e, "descripcion": _desc, **({"opciones": _o} if _o else {})}
        for _k, _t, _d, _g, _e, _desc, _o in (
            ("UTRA_ACTIVO", "bool", "1", "General", "Motor encendido",
             "Apagado, no mira cuotas ni guarda nada. No toca a UTR VALUE ni a ninguna otra "
             "estrategia.", None),
            ("UTRA_INTERVALOS", "bool", "1", "General",
             "Usar el UTR oculto (11.xx) por intervalos",
             "Misma idea que en UTR VALUE, contra +0.25: si el peor caso llega, se sigue; si ni el "
             "mejor caso llega, no se sigue; si los decimales deciden, el disparo (cuota ≥ 4.00, con "
             "las mismas protecciones de marcador en vivo) va a ⚠️ revisión manual. En vivo, la "
             "confirmación vale hasta 10 min después del cruce.", None),
            ("UTRA_VIVO", "bool", "1", "General", "Mirar también las cuotas EN VIVO",
             "Prendido: cada cuota en vivo de Pinnacle (/pinnodds/price-drop) se revisa. Apagado: "
             "solo pre-partido (ni estudio ni avisos en vivo).", None),
            ("UTRA_DELTA_MIN", "float", "0.00", "Puertas (obligatorias)",
             "ΔUTR mínimo del jugador UTR superior",
             "REGLA (Rubén, 07/10/2026). Debajo no se guarda nada. Desde acá se guarda TODO partido "
             "para estudio, con cualquier cuota.", None),
            ("UTRA_CUOTA_MIN", "float", "3.00", "Puertas (obligatorias)",
             "Cuota mínima para DISPARAR",
             "REGLA (Rubén, 07/10/2026). Sin cuota máxima. Se revisa con CADA cuota nueva: si la "
             "cuota sube a este valor (pre-partido o en vivo) dispara en ese momento, UNA sola vez "
             "por partido. Debajo queda para estudio (bandas 2.00-3.99).", None),
            ("UTRA_DISCORD_CONFIANZA", "str", "ALTA", "Controles técnicos del dato",
             "Confianza mínima de la identidad UTR (los dos)",
             "ALTA = solo MATCH (perfil pro). MEDIA = también MATCH_VARIANTE. Si no: "
             "DESCARTADO_CALIDAD_UTR.",
             [{"valor": "ALTA", "etiqueta": "ALTA (solo MATCH)"},
              {"valor": "MEDIA", "etiqueta": "MEDIA (también MATCH_VARIANTE)"}]),
            ("UTRA_UTR_MAX_DIAS", "int", "30", "Controles técnicos del dato",
             "Antigüedad máxima del UTR capturado (días)", "0 = no mirar la fecha.", None),
            ("UTRA_OVERROUND_MIN", "float", "0.98", "Controles técnicos del dato",
             "Overround mínimo (coherencia de las dos cuotas)",
             "Técnico, no estratégico. Si no: DESCARTADO_CUOTAS_INCOHERENTES.", None),
            ("UTRA_OVERROUND_MAX", "float", "1.15", "Controles técnicos del dato", "Overround máximo",
             "Técnico, no estratégico.", None),
            ("UTRA_CUOTA_MAX_TECNICA", "float", "1000", "Controles técnicos del dato",
             "Techo TÉCNICO de cuota (0 = sin techo)",
             "Solo contra datos corruptos. NO es una cuota máxima de estrategia.", None),
            ("UTRA_DISCORD_ACTIVO", "bool", "1", "Avisos", "Publicar para Discord",
             "En vivo: Discord en el acto. Pre-partido: cuando el partido EMPIEZA (igual que UTR "
             "VALUE). Apagado: queda como APROBADO_DISCORD_CERRADO.", None),
            ("UTRA_DISCORD_VIVO", "bool", "1", "Avisos", "Discord también para las disparadas EN VIVO",
             "Apagado: las en vivo se guardan y se estudian igual (y van a Telegram), pero no a "
             "Discord. Útil si en vivo sale demasiado volumen.", None),
            ("UTRA_MENSAJE_CORTO", "bool", "1", "Avisos", "Discord: mensaje corto «🚨 FULLTENIS · CUOTA EXTREMA»",
             "18/10/2026 (Rubén). Prendido: un solo mensaje corto (jugador detectado, nivel, cuota actual, último "
             "enfrentamiento y los datos de salud/regreso VERIFICADOS de FT INTELLIGENCE) y ya no se adjunta el bloque "
             "largo de contexto. Apagado: el mensaje anterior + el bloque «FT INTELLIGENCE · CONTEXTO DEL PARTIDO».",
             None),
            ("HORARIOS_SEGUIR_CAMBIOS", "bool", "1", "Avisos", "🕒 Seguir los cambios de horario de los partidos",
             "19/10/2026 (Rubén: «la programación se retrasa y ya no empieza en 10 minutos sino en 3 horas»). Prendido: "
             "cuando el bot, OddsPapi, SportsAPI365 o Fonbet traen una hora nueva (≥5 min de diferencia) para un partido, "
             "esa hora pasa a TODAS sus señales (CMP, UTR VALUE, UTR MARKET ANOMALY, Under UTR…) y queda el historial; "
             "FT Intelligence la lee en /ft-intel/horarios antes de avisar. Solo cambia la hora, nunca la señal.",
             None),
            ("UTRA_DEDUP_HORAS", "int", "48", "Avisos", "Ventana de un mismo partido (horas)",
             "El mismo par de jugadores dentro de esta ventana es el mismo partido: una sola fila y "
             "un solo aviso.", None),
            ("UTRA_ESCRITURA_MIN", "int", "5", "Avisos",
             "Evolución de cuota: guardar cada N minutos",
             "La cuota máxima, la última y cada cruce de umbral se guardan siempre en el acto; el "
             "resto de los movimientos, como mucho cada N minutos por partido (el detalle tick a "
             "tick de Pinnacle ya queda en pinnodds_price_history).", None),
            # EN VIVO solo sin games jugados (Rubén, 07/10/2026): que Discord no
            # confunda una cuota alta por perder games con una anomalía real.
            ("UTRA_VIVO_SOLO_SIN_GAMES", "bool", "1", "En vivo: protección por marcador",
             "En vivo, avisar SOLO si todavía no hay games jugados",
             "REGLA (Rubén, 07/10/2026). En vivo, la cuota pudo llegar a 4.00 por el desarrollo del "
             "partido (perdió games/puntos): eso no es una anomalía de mercado. Solo suena si el "
             "marcador está 0-0 sin games jugados (partido recién abierto). Con games jugados, o "
             "sin poder confirmar el marcador, se guarda TODO para estudio "
             "(ESTUDIO_VIVO_MARCADOR_AVANZADO / _TARDE / _SIN_MARCADOR) sin Discord ni Telegram. "
             "Pre-partido no cambia.", None),
            ("UTRA_VIVO_SOLO_SIN_PUNTOS", "bool", "1", "En vivo: protección por marcador",
             "En vivo, avisar SOLO si todavía no se jugó ningún PUNTO",
             "17/10/2026 (Daniel): un aviso de Watson vs Jones salió con 0-0 en games pero el primer punto ya "
             "jugado. Encendido: 15-0, 0-15, etc. también cuenta como marcador avanzado (estudio, sin Discord). "
             "Usa los puntos del live-tracker del bot (partidos_en_vivo); una fuente sin puntos sigue con la "
             "regla de games.", None),
            ("UTRA_VIVO_PUNTOS_MAX_SEG", "int", "45", "En vivo: protección por marcador",
             "«Sin puntos» fresco: actualizado hace como mucho N segundos",
             "Los puntos cambian cada pocos segundos: un «0-0 sin puntos» más viejo que esto no se cree "
             "(queda esperando el marcador y, si no llega a tiempo, para estudio).", None),
            ("UTRA_VIVO_MARCADOR_MAX_SEG", "int", "180", "En vivo: protección por marcador",
             "Marcador fresco: actualizado hace como mucho N segundos",
             "Un '0-0' más viejo que esto no se cree (puede haber games que todavía no llegaron). "
             "Pinnacle: marcador del live-tracker del bot (partidos_en_vivo). Circuito UTR: el de "
             "OddsPapi.", None),
            ("UTRA_VIVO_ESPERA_MARCADOR_MIN", "int", "3", "En vivo: protección por marcador",
             "Sin marcador todavía: esperar hasta N minutos",
             "Si la cuota cruza 4.00 en vivo y el marcador todavía no se conoce, se espera (sin "
             "avisar) a que llegue. Si llega 0-0 dentro de este lapso y la cuota sigue ≥ 4.00, "
             "suena; si llega con games, o no llega, queda para estudio.", None),
            ("UTRA_VIVO_MAX_MIN_DESDE_INICIO", "int", "15", "En vivo: protección por marcador",
             "No creer un '0-0' si el partido empezó hace más de N minutos",
             "Un primer game casi nunca dura tanto: un 0-0 a esa altura es un marcador atrasado.",
             None),
            # Cuotas EN VIVO del circuito UTR (07/10/2026): Pinnacle no lo trae;
            # OddsPapi sí, con un plan con en vivo. Ver production/engines/oddspapi_vivo.py.
            ("UTRA_ODDSPAPI_VIVO", "str", "0", "Fuente OddsPapi en vivo (circuito UTR)",
             "Cuotas en vivo del circuito UTR desde OddsPapi",
             "0 = apagado (el plan actual no trae en vivo). 'ws' = WebSocket: OddsPapi empuja cada "
             "cambio, no gasta peticiones (necesita un plan con WebSocket; clave en ODDSPAPI_WS_KEY "
             "u ODDSPAPI_KEY). 'rest' = preguntar cada N minutos, con su propio tope diario. Solo "
             "alimenta a UTR MARKET ANOMALY; UTR VALUE sigue siendo pre-partido.",
             [{"valor": "0", "etiqueta": "Apagado"}, {"valor": "ws", "etiqueta": "WebSocket (recomendado)"},
              {"valor": "rest", "etiqueta": "Sondeo REST"}]),
            ("UTRA_ODDSPAPI_WS_URL", "str", "wss://v5.oddspapi.io/ws", "Fuente OddsPapi en vivo (circuito UTR)",
             "Dirección del WebSocket", "La de la documentación de OddsPapi (07/10/2026).", None),
            ("UTRA_ODDSPAPI_VIVO_REST_MIN", "int", "2", "Fuente OddsPapi en vivo (circuito UTR)",
             "Modo 'rest': preguntar cada N minutos",
             "1 petición cada 5 torneos UTR que se estén jugando. Cada 2 min con 1 torneo en juego "
             "todo el día son ~720 por día.", None),
            ("UTRA_ODDSPAPI_VIVO_TOPE_DIARIO", "int", "25", "Fuente OddsPapi en vivo (circuito UTR)",
             "Modo 'rest': tope de peticiones por día (aparte del pre-partido)",
             "Contador propio: el en vivo no se come el cupo del pre-partido (UTRV_ODDSPAPI_TOPE_DIARIO). "
             "OJO con el plan de 5.000/mes: el pre-partido ya usa ~100-160 por día; 25 más ≈ 750 por "
             "mes. Subirlo solo con un plan de más peticiones (o usar el modo 'ws', que no gasta).", None),
            ("UTRA_ODDSPAPI_VIVO_SOLO_CANDIDATOS", "bool", "1", "Fuente OddsPapi en vivo (circuito UTR)",
             "Modo 'rest': preguntar solo por los partidos que pueden disparar",
             "Prendido: solo los partidos en juego con UTR de los dos, ΔUTR ≥ 0.25 y sin disparar "
             "todavía (los demás no pueden disparar nunca). Ahorra casi todas las peticiones. "
             "Apagado: todos los partidos UTR en juego.", None),
            ("UTRA_ODDSPAPI_MERCADO", "str", "121", "Fuente OddsPapi en vivo (circuito UTR)",
             "Mercado Winner (id de OddsPapi)",
             "121 en la API v4. En el WebSocket todavía no se vio con datos reales: el panel muestra "
             "los mercados que llegan para confirmarlo.", None),
            ("UTRA_ODDSPAPI_SALIDAS", "str", "121,122", "Fuente OddsPapi en vivo (circuito UTR)",
             "Salidas del Winner: jugador 1, jugador 2", "Como en la API v4.", None),
        )
    },
    "MSG_CAB_utr_market_anomaly": {
        "tipo": "texto", "default": "🚨 FULLTENIS · UTR MARKET ANOMALY", "categoria": "UTR MARKET ANOMALY",
        "grupo": "Mensaje", "etiqueta": "Encabezado del mensaje",
        "descripcion": "Primera línea en Telegram (en negrita) y en Discord.",
    },
    "UTRA_FRASE": {
        "tipo": "texto", "categoria": "UTR MARKET ANOMALY", "grupo": "Mensaje",
        "default": "⚠️ JUGADOR UTR SUPERIOR A CUOTA EXTREMA",
        "etiqueta": "Frase final", "descripcion": "Vacía = sin frase.",
    },

    # ── 🔥 UNDER UTR (v54i, 15/10/2026, Rubén: «nueva estrategia de UNDER utilizando nuestro ranking UTR
    # actualizado… construirla desde cero y empezar a generar un histórico limpio»). Corrección de la misma noche:
    # «la señal sale únicamente ANTES DE EMPEZAR EL PARTIDO… una vez jugado el primer punto, ese partido queda
    # definitivamente cerrado». Valores de fábrica = el pedido. Ver production/engines/under_utr.py.
    **{
        _k: {"tipo": _t, "default": _d, "categoria": "UNDER UTR", "grupo": _g,
             "etiqueta": _e, "descripcion": _desc, **({"opciones": _o} if _o else {})}
        for _k, _t, _d, _g, _e, _desc, _o in (
            ("UNDERUTR_ACTIVO", "bool", "1", "General", "Motor encendido",
             "Apagado, no sigue partidos ni guarda nada. No toca a ninguna otra estrategia (tampoco al ▼ Under en Vivo).",
             None),
            ("UNDERUTR_DELTA_MIN", "float", "1.70", "Regla (pedido de Rubén, 15/10/2026)",
             "ΔUTR mínimo = |UTR A − UTR B|",
             "Con nuestro padrón UTR (UTR Power exacto si está). Desde acá el partido POR JUGARSE queda pendiente de "
             "que abra el mercado UNDER. Elo, FTR y MatchUp no intervienen.", None),
            ("UNDERUTR_LINEA_MIN", "float", "16.5", "Regla (pedido de Rubén, 15/10/2026)",
             "Línea UNDER mínima (línea principal de la casa)",
             "Se evalúa la línea PRINCIPAL que abre la casa ANTES del primer punto (las alternativas se guardan para "
             "estudio, no activan).", None),
            ("UNDERUTR_CUOTA_MIN", "float", "1.70", "Regla (pedido de Rubén, 15/10/2026)",
             "Cuota mínima del UNDER", "Sin cuota máxima. Línea y cuota quedan CONGELADAS al activar.", None),
            ("UNDERUTR_EXCLUIR_PALABRAS", "str", "utr,pro tennis tour,ptt", "Regla (pedido de Rubén, 15/10/2026)",
             "Circuito UTR Pro: palabras del torneo que lo marcan (excluido)",
             "Palabra entera, separadas por coma. Esos partidos no se siguen ni se guardan. Los dobles también quedan "
             "fuera (regla fija del pedido).", None),
            ("UNDERUTR_STATUS_PRE", "str",
             "notstarted,not started,not_started,scheduled,upcoming,ns,pre,prematch,fixture",
             "Solo antes del primer punto",
             "Status del proveedor que significan «no empezó»",
             "La señal sale SOLO con uno de estos status (en minúsculas), sin juegos en el marcador y sin puntos en "
             "curso. Un status de partido en juego (también InPlay 0-0), terminado, suspendido o cancelado = "
             "empezado. Un status que no está acá = desconocido: NO activa (la pestaña muestra los status que llegan "
             "para completar la lista).", None),
            ("UNDERUTR_PUBLICAR_MAX_SEG", "int", "180", "Solo antes del primer punto",
             "Ventana para publicar en Discord después de activar (segundos)",
             "Pasado este tiempo sin publicarse, la activación ya no sale (el partido pudo empezar sin que lo "
             "viéramos). Se guarda igual, con el motivo.", None),
            ("UNDERUTR_VERIF_MAX_SEG", "int", "90", "Solo antes del primer punto",
             "Verificación del estado: antigüedad máxima (segundos)",
             "Antes de activar, el bot consulta el estado FRESCO del partido (socket o /event/get). Si la verificación "
             "es más vieja que esto, se pide otra.", None),
            ("UNDERUTR_ESPERAR_HORA_REAL", "bool", "1", "Solo antes del primer punto",
             "Esperar la hora REAL del partido (retrasos)",
             "19/10/2026 (Dedura vs Rengifo: programado 10:00, empezó 13:21). Si alguna fuente sabe que el partido es "
             "más tarde (la hora guardada, que sigue los cambios de programación, la del bot o la de Fonbet), la señal "
             "no se activa hasta UNDERUTR_VENTANA_MIN minutos antes de esa hora, con la línea y la cuota de ESE "
             "momento. Si la hora ya pasó o no se conoce, se activa como siempre (puede empezar en cualquier momento).",
             None),
            ("UNDERUTR_VENTANA_MIN", "int", "5", "Solo antes del primer punto",
             "Minutos antes de la hora real en que se puede activar",
             "Con «Esperar la hora REAL» prendido: cuántos minutos antes del inicio conocido se permite activar.", None),
            ("UNDERUTR_LIBERAR_RETENIDAS", "bool", "1", "Solo antes del primer punto",
             "Publicar activaciones retenidas cuando se prende el interruptor",
             "Una activación que quedó SIN Discord solo porque «Formato del mercado confirmado» o «Discord» estaban "
             "apagados, se publica si después se prende el interruptor y el partido todavía no empezó: verificación "
             "fresca del estado y la línea y la cuota de ese momento tienen que seguir cumpliendo la regla (se publica "
             "con esas, y la activación original queda guardada). Solo las de las últimas 24 h.", None),
            ("UNDERUTR_DISCORD_ACTIVO", "bool", "1", "Discord",
             "Mandar las activaciones a Discord",
             "En el acto, por FT Intelligence → FT Discord (tipo UNDER_UTR en Admin → Discord). Si el partido empieza "
             "antes de publicarse, ya no se publica. Apagado: se guardan igual.", None),
            ("UNDERUTR_FORMATO_CONFIRMADO", "bool", "0", "Mercado pre-partido (SportsAPI365)",
             "Formato del mercado confirmado con datos reales",
             "Mientras esté apagado, una activación cuyo Under se tomó de od1/od2 (sin claves Under/Over explícitas) se "
             "GUARDA pero NO sale a Discord. Prenderlo después de correr probar_over_under.py en el bot y comprobar la "
             "línea, el lado del Under, que sea total de juegos y que llegue antes del primer punto.", None),
            ("UNDERUTR_CONTROL_ACTIVO", "bool", "1", "Mercado pre-partido (SportsAPI365)",
             "📋 Control: mostrar TODOS los partidos (también los que no pasan el ΔUTR)",
             "16/10/2026, Rubén: «saber que sí funciona». La pestaña muestra cada partido por jugarse con sus UTR, su "
             "ΔUTR y, desde 5 min antes de empezar, la línea y la cuota leídas. Solo control: nunca activa ni entra al "
             "histórico. Apagado: no se guarda (el bot igual puede pedir el mercado; se apaga allá con "
             "UNDER_UTR_PRE_CONTROL=0).", None),
            ("UNDERUTR_MERCADOS", "str", "over under", "Mercado pre-partido (SportsAPI365)",
             "Nombre del mercado de total de juegos del partido",
             "Nombre exacto (sin mayúsculas), separados por coma. La pestaña muestra los mercados que llegan.", None),
            ("UNDERUTR_LADO_UNDER", "str", "od2", "Mercado pre-partido (SportsAPI365)",
             "Si el mercado solo trae od1/od2: cuál es el Under",
             "Por convención Over primero y Under después (como el «O and U: 1.80 | 1.90» del canal). SIN CONFIRMAR "
             "con datos de SportsAPI365.",
             [{"valor": "od2", "etiqueta": "od2 (Over | Under)"}, {"valor": "od1", "etiqueta": "od1 (Under | Over)"}]),
            ("UNDERUTR_CLAVES_LINEA", "str", "line,handicap,hd,total,points,value,name", "Mercado pre-partido (SportsAPI365)",
             "Claves donde puede venir la línea de juegos (en orden)",
             "Se toma la primera que traiga un número. Una línea fuera de 5–70 no es de juegos (p. ej. 2.5 = sets) y "
             "se ignora.", None),
            ("UNDERUTR_CASAS", "str", "Pinnacle,Bet365", "Mercado pre-partido (SportsAPI365)",
             "Casas en orden de preferencia",
             "La línea que cuenta es la de la primera casa de la lista que la traiga; si ninguna, cualquier otra.", None),
            # 17/10/2026 (v54l, Daniel y Rubén): «todo lo relacionado a UTR y Under pase por Fonbet». Fonbet
            # (OddsPapi) es la casa PRINCIPAL; las del bot (SportsAPI365) quedan de respaldo. Ver fonbet_under.py.
            ("UNDERUTR_FONBET_ACTIVO", "bool", "1", "Fonbet (OddsPapi)",
             "Leer la línea UNDER de Fonbet (OddsPapi)",
             "Desde N minutos antes de la hora programada y hasta que empieza, se pide el total de juegos de Fonbet "
             "para los partidos del UNDER. Usa la misma ODDSPAPI_KEY. Apagado: solo las casas del bot.", None),
            ("UNDERUTR_FONBET_PRIMERO", "bool", "1", "Fonbet (OddsPapi)",
             "Fonbet primero; las casas del bot de respaldo",
             "Si Fonbet trae una línea válida, es la que cuenta. Si no, la de la primera casa de «Casas en orden de "
             "preferencia». Apagado: Fonbet entra como una casa más según esa lista.", None),
            ("UNDERUTR_FONBET_CASA", "str", "fonbet", "Fonbet (OddsPapi)", "Slug de la casa en OddsPapi",
             "Tal como figura en el plan de OddsPapi (Bookmakers Access).", None),
            ("UNDERUTR_FONBET_VENTANA_MIN", "int", "5", "Fonbet (OddsPapi)",
             "Desde cuántos minutos antes de la hora programada se pide",
             "Igual que el bot (UNDER_UTR_PRE_VENTANA_MIN). Hasta que el partido empieza de verdad.", None),
            ("UNDERUTR_FONBET_CADA_SEG", "int", "60", "Fonbet (OddsPapi)", "Cada cuántos segundos (mínimo 30)",
             "Cada vuelta: 1 petición cada 5 torneos con partidos en la ventana; la lista de partidos, cada 20 min.",
             None),
            ("UNDERUTR_FONBET_CONTROL", "bool", "1", "Fonbet (OddsPapi)",
             "También para la tabla 📋 Control (todos los partidos)",
             "Muestra la línea de Fonbet también en los partidos que no pasan el ΔUTR. Gasta más peticiones; "
             "apagado, solo los pendientes del UNDER.", None),
            ("UNDERUTR_FONBET_DESCUBRIR", "bool", "1", "Fonbet (OddsPapi)",
             "Usar también los partidos de Fonbet que no llegan por SportsAPI365",
             "18/10/2026 (Rubén): la lista de partidos de OddsPapi se compara con la nuestra; los que faltan entran al "
             "UNDER UTR con la misma regla. Para ellos, «todavía no empezó» se comprueba con la misma lectura de Fonbet "
             "(statusId 0, sin hora real de inicio, antes de la hora programada) + nuestro seguimiento en vivo.", None),
            ("UNDERUTR_FONBET_DESCUBRIR_CADA_MIN", "int", "10", "Fonbet (OddsPapi)",
             "Cada cuántos minutos se compara la lista de Fonbet",
             "La lista se pide como mucho cada 20 min (1 petición); esto solo decide cada cuánto se compara.", None),
            ("UNDERUTR_FONBET_TOPE_DIARIO", "int", "2000", "Fonbet (OddsPapi)", "Tope de peticiones por día (UTC)",
             "Contador propio (no se come el de UTR VALUE). Al llegar, el UNDER sigue con las casas del bot.", None),
            ("UNDERUTR_FONBET_MAX_EDAD_SEG", "int", "150", "Fonbet (OddsPapi)",
             "No usar una lectura de Fonbet más vieja que N segundos", "", None),
            ("UNDERUTR_FONBET_PATRON_MERCADO", "str", r"total|over ?/ ?under|totals", "Fonbet (OddsPapi)",
             "Patrón del nombre del mercado de total de juegos (expresión regular)",
             "Se compara con el nombre del mercado (catálogo de OddsPapi) en minúsculas. La pestaña muestra los "
             "mercados que llegan (`mercados_vistos`) para ajustarlo.", None),
            ("UNDERUTR_FONBET_EXCLUIR", "str", "set,1st,2nd,3rd,4th,5th,home,away,player,competitor,tiebreak,sets",
             "Fonbet (OddsPapi)", "Palabras que descartan un mercado (separadas por coma)",
             "Para quedarse con el total de juegos DEL PARTIDO: fuera los de un set, de un jugador o de sets.", None),
            ("UNDERUTR_CUOTA_MAX_EDAD_SEG", "int", "0", "Mercado pre-partido (SportsAPI365)",
             "No usar una cuota más vieja que N segundos (0 = sin control)",
             "Según la hora que trae la propia casa (addTime). De fábrica sin control: en pre-partido una cuota que "
             "no se movió en una hora sigue vigente, y todavía no se vio qué significa addTime en este endpoint.", None),
            ("UNDERUTR_ESCRITURA_SEG", "int", "30", "Mercado pre-partido (SportsAPI365)",
             "Espaciado de escrituras de un partido pendiente (segundos)",
             "El primer mercado, una línea nueva, la activación, el inicio y el resultado se escriben en el acto.", None),
        )
    },
    "MSG_CAB_under_utr": {
        "tipo": "texto", "default": "🔥 FULLTENNIS · UNDER UTR", "categoria": "UNDER UTR",
        "grupo": "Mensaje", "etiqueta": "Encabezado del mensaje",
        "descripcion": "Primera línea del mensaje de Discord.",
    },

    # ── Encender / apagar estrategias enteras ────────────────────────
    # Un interruptor por categoría de la Matriz. Apagado, esa categoría
    # deja de mandarse a CUALQUIER canal (se sigue guardando y viendo
    # en el panel) -- se aplica en notificar_categoria(), el punto único
    # por el que pasa todo lo que sale a Telegram.
    **{
        f"ESTRATEGIA_ACTIVA_{_cat.upper()}": {
            "tipo": "bool", "default": "1", "categoria": "Encender / apagar estrategias",
            "etiqueta": f"{_nombre} — mandar a Telegram",
            "descripcion": (
                f"Apagado, '{_cat}' se sigue guardando y mostrando en el panel, pero no se "
                "manda a ningún canal. Es un corte más fuerte que destildar la Matriz: no "
                "depende de qué canal esté marcado. El canal de Admin tampoco lo recibe."
            ),
        }
        for _cat, _nombre in (
            ("valor_ftr", "Valor FTR"),
            ("pre", "Valor Pre-Partido (pre)"),
            ("ftr_elo", "Valor Pre-Partido (FTR+Elo)"),
            ("movimiento", "Movimiento Rentable"),
            ("vivo", "Partidos en Vivo"),
            ("under", "Under en Vivo"),
            ("favorito", "Favorito en Vivo"),
            ("doble_jornada", "Doble Jornada"),
            # 30/08/2026, BUG REAL encontrado por Maria: acá solo estaba
            # ("racha", ...) -- la categoría de "Racha sencilla" (el
            # sistema VIEJO, hoy apagado por default, ver
            # RACHA_ACTIVA). La que de verdad manda los avisos de
            # "Jugador en Racha" hoy es la categoría "racha_detallada"
            # (Tabla Maestra), y esa NUNCA tuvo su propio interruptor
            # acá -- estrategia_activa() trata una categoría sin
            # interruptor registrado como "siempre encendida" (falla
            # abierto), así que esto no estaba bloqueando nada
            # activamente, pero la casilla "Jugador en Racha -- mandar
            # a Telegram" que se veía en Admin apagaba la del sistema
            # viejo y dejaba intacto el nuevo -- exactamente al revés
            # de lo que el nombre sugiere. Se renombra la vieja para
            # que quede claro cuál es cuál, y se agrega la que faltaba.
            ("racha", "Racha sencilla (canal externo, hoy apagada -- no es Jugador en Racha)"),
            ("racha_detallada", "Jugador en Racha (Racha detallada / Tabla Maestra)"),
            # 09/08/2026 -- ver CATEGORIAS_ALERTA en telegram_destinos.py.
            # OJO: este interruptor NO corta el aviso al canal de Admin,
            # a diferencia de las demás categorías. El aviso de retiro
            # sale por dos vías (notificar_valor al admin +
            # notificar_categoria a la matriz) justamente para no
            # perderse mientras la matriz esté sin tildar, y esta llave
            # solo gobierna la segunda.
            ("retiro", "Vuelve tras Retirarse"),
            ("manual", "Crear Pick (manual)"),
            ("sugerencias", "Sugerencias"),
            ("avisos", "Botón de aviso rápido"),
            # 01/09/2026 -- ver CATEGORIAS_ALERTA en telegram_destinos.py.
            # Categoría PROPIA, separada de 'vivo' y de 'sugerencias': es
            # un mensaje distinto (el bloque de Valor histórico), con su
            # propio topic, que puede apagarse sin tocar la alerta normal
            # de "Partidos en Vivo" ni el guardado en Sugerencias.
            ("post_set", "PostSet — Valor histórico"),
            ("partidos_maestros", "Partidos Maestros"),
            # 07/10/2026 -- ver CATEGORIAS_ALERTA en telegram_destinos.py.
            ("cuota_mal_puesta", "Cuota Mal Puesta"),
            ("utr_value", "UTR VALUE"),
            ("utr_market_anomaly", "UTR MARKET ANOMALY"),
        )
    },

    # ── Mensajes ─────────────────────────────────────────────────────
    # El encabezado y la frase de cada estrategia, tal cual salen a
    # Telegram. El resto del mensaje (jugadores, cuotas, métricas) se
    # calcula y no se puede escribir a mano -- son los datos de la
    # alerta.
    #
    # El encabezado se convierte a NEGRITA UNICODE al mandarse (𝐀𝐁𝐂):
    # se escribe normal acá. Las letras con tilde y la ñ no tienen
    # glifo en negrita y quedan sin convertir, por eso los defaults
    # evitan tildes en los encabezados.
    **{
        f"MSG_CAB_{_t}": {
            "tipo": "texto", "default": _cab, "categoria": "Mensajes — encabezados",
            "etiqueta": f"Encabezado — {_nombre}",
            "descripcion": "Primera línea del mensaje. Se manda en negrita; los emojis quedan como estén acá.",
        }
        for _t, _nombre, _cab in (
            ("valor_ftr", "Valor FTR", "🚨 ALERTA DE VALOR | RANKING FTR"),
            ("pre", "Valor Pre-Partido (pre)", "🚨 ALERTA DE VALOR | PRE-PARTIDO"),
            ("vivo", "Partidos en Vivo", "🔴 ALERTA EN VIVO | POST SET 1"),
            ("movimiento", "Movimiento Rentable (confirmado)", "📉 MOVIMIENTO RENTABLE | CONFIRMADO POR ELO"),
            ("movimiento_sin_confirmar", "Movimiento Rentable (a Sugerencias, sin confirmar)",
             "🚨 MOVIMIENTO MUY FUERTE | SIN CONFIRMAR POR ELO"),
            ("under", "Under en Vivo", "📊 ALERTA EN VIVO | UNDER"),
            ("favorito", "Favorito en Vivo", "⭐ ALERTA EN VIVO | FAVORITO"),
            ("ftr_elo", "Valor Pre-Partido (FTR+Elo)", "🚨 ALERTA DE VALOR | VALOR PRE-PARTIDO"),
            ("manual", "Crear Pick (manual)", "🎾 FULLTENNIS PICK"),
            ("racha", "Jugador en Racha", "🔥 JUGADOR EN RACHA"),
            ("doble_jornada", "Doble Jornada", "📅 DOBLE JORNADA"),
            # 09/08/2026, a pedido de Rubén ("que el mensaje de retiro
            # se pueda editar desde el Admin"). Estaba escrito a mano en
            # routes_public.py, era el único aviso que no se podía tocar
            # sin desplegar.
            ("retiro", "Vuelve tras Retirarse", "🩹 VUELVE TRAS RETIRARSE"),
            # 24/08/2026, a pedido de Rubén ("que salga como en Mensajes
            # de alerta"). Entra a las mismas dos listas que el resto
            # para aparecer en esa pestaña con su botón "Ver y editar",
            # en vez de ser un campo suelto en otra sección.
            ("racha_detallada", "Racha detallada", "🎾 RACHA DETALLADA"),
        )
    },
    **{
        f"MSG_FRASE_{_t}": {
            "tipo": "texto", "default": _frase, "categoria": "Mensajes — frases",
            "etiqueta": f"Frase — {_nombre}",
            "descripcion": "La línea que explica qué detectó el modelo. Va debajo del torneo, sin negrita. Vacío = no se manda esa línea.",
        }
        for _t, _nombre, _frase in (
            ("valor_ftr", "Valor FTR", "⚠️ Error de cuota detectado por el modelo."),
            ("pre", "Valor Pre-Partido (pre)", "⚠️ Cuota por encima de la probabilidad real del modelo."),
            ("vivo", "Partidos en Vivo", "⚠️ Valor detectado con el partido en juego."),
            ("movimiento", "Movimiento Rentable (confirmado)", "⚠️ El mercado se movio a favor y el modelo lo respalda."),
            ("movimiento_sin_confirmar", "Movimiento Rentable (a Sugerencias, sin confirmar)",
             "⚠️ El mercado se movió fuerte, pero el modelo NO lo respalda."),
            ("under", "Under en Vivo", "⚠️ Partido parejo: la linea de juegos tiene valor."),
            ("favorito", "Favorito en Vivo", "⚠️ Favorito real por debajo de su precio justo."),
            ("ftr_elo", "Valor Pre-Partido (FTR+Elo)", "⚠️ FTR y Elo coinciden: diferencia real entre los jugadores."),
            ("manual", "Crear Pick (manual)", ""),
            ("racha", "Jugador en Racha",
             "🎯 Venía perdiendo y tomó el control del partido -- posible oportunidad "
             "antes de que el mercado reaccione del todo."),
            # 09/08/2026 -- ver el encabezado de arriba. Ojo: en este
            # aviso la frase va al FINAL del mensaje, no debajo del
            # torneo como en las alertas de valor. Es la advertencia de
            # contexto, y ahí es donde tiene sentido leerla.
            ("retiro", "Vuelve tras Retirarse",
             "⚠️ Aviso de contexto, no es una alerta de valor -- conviene mirar el "
             "estado físico antes de tomar cualquier señal de este partido."),
            ("racha_detallada", "Racha detallada",
             "🔥 Viene ganando juegos al hilo -- posible ventana antes de que la cuota se acomode."),
        )
    },
    "MSG_PREFIJO_SUGERENCIA": {
        "tipo": "texto", "default": "💡 ", "categoria": "Mensajes — frases",
        "etiqueta": "Prefijo de las Sugerencias",
        "descripcion": "Lo que se le pega adelante a todo lo que sale por la categoría Sugerencias. El espacio final cuenta.",
    },
    "TORNEOS_AVISAR_SIN_SUPERFICIE": {
        "tipo": "bool", "default": "0", "categoria": "Torneos",
        "etiqueta": "Avisar a Admin cuando aparece un torneo sin superficie",
        "descripcion": (
            "APAGADO por default (04/08/2026): al principio la mayoría de los torneos van a "
            "estar sin cargar, así que el aviso sería una avalancha justo cuando menos sirve. "
            "Conviene encenderlo DESPUÉS de cargar los torneos activos desde Admin → Torneos, "
            "para enterarse solo de los nuevos. Avisa una sola vez por torneo, no por alerta; "
            "cuando se carga la superficie deja de avisar. Con esto apagado los torneos sin "
            "resolver igual se registran y se ven en la lista, solo que en silencio."
        ),
    },
    "RESULTADOS_LOTE": {
        "tipo": "int", "default": "5", "categoria": "Resultados de picks",
        "etiqueta": "Cerrar de a cuántos para avisar",
        "descripcion": (
            "Cada vez que se juntan estos cierres, se manda UNA lista con todos ellos "
            "-- por separado para Picks Pre-Partido y para Picks en Vivo, cada grupo con su "
            "propia cuenta. "
            "diciendo cuáles se ganaron y cuáles se perdieron. Uno por uno serían 20 o 30 "
            "mensajes en un día movido. Solo entran los picks que se mandaron de verdad al "
            "grupo; Under en Vivo queda afuera porque su resultado no se deduce del marcador."
        ),
    },
    "MSG_CAB_resultados_pre": {
        "tipo": "texto", "default": "📊 RESULTADOS | PICKS PRE-PARTIDO",
        "categoria": "Mensajes — encabezados",
        "etiqueta": "Encabezado — Resultados de Picks Pre-Partido",
        "descripcion": "Primera línea de la lista de resultados pre-partido. Se manda en negrita.",
    },
    "MSG_CAB_resultados_vivo": {
        "tipo": "texto", "default": "📊 RESULTADOS | PICKS EN VIVO",
        "categoria": "Mensajes — encabezados",
        "etiqueta": "Encabezado — Resultados de Picks en Vivo",
        "descripcion": "Primera línea de la lista de resultados en vivo. Se manda en negrita.",
    },
    "MSG_AVISO_BOTON": {
        "tipo": "texto", "default": "🚨URGENTE AL DISCORD🚨", "categoria": "Mensajes — frases",
        "etiqueta": "Botón de aviso rápido",
        "descripcion": (
            "El mensaje del botón flotante del panel. Sale igual por Telegram, por push al "
            "celular y en la notificación dentro de la página. Debajo se agrega solo "
            "\"Avisado por <quien apretó>\"."
        ),
    },
}


def _ensure_tabla(conn) -> None:
    global _TABLA_LISTA
    if _TABLA_LISTA:
        return
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ftr_config_estrategias (
            clave           TEXT PRIMARY KEY,
            valor           TEXT NOT NULL,
            actualizado_en  TEXT NOT NULL,
            actualizado_por TEXT
        )
    """)
    conn.commit()
    _TABLA_LISTA = True


# ═════════════════════════════════════════════════════════════════════
#  CACHÉ DE LECTURA (16/08/2026)
# ═════════════════════════════════════════════════════════════════════
#
# EL PROBLEMA: obtener() abría una CONEXIÓN NUEVA a SQLite por cada
# clave leída -- get_conn() hace connect + 3 PRAGMAs (uno de ellos,
# journal_mode, toma un lock breve) + SELECT + close, para traer una
# fila de una tabla de veinte. Y las lecturas no vienen de a una:
#
#   fixture/reportar  ->  3 propias
#                         + 4 de evaluar_ftr_elo()
#                         + 6 de cumple_filtros_alerta()   = 13 conexiones
#   panel LIVE        ->  10 al entrar + 1 POR PARTIDO dentro del loop
#
# O sea que en el camino más caliente del servicio -- el que mide
# 90-105ms y corre ~200 veces por minuto -- una parte del tiempo se iba
# en abrir conexiones para leer constantes que no cambian en semanas.
#
# LA SOLUCIÓN es la que este mismo proyecto ya usa para la matriz de
# permisos (production/api/permisos.py, "EL PROBLEMA A RESOLVER"):
# caché en memoria con TTL corto. Mismo razonamiento, mismo TTL.
#
# Se cachea la TABLA ENTERA en una sola query, no clave por clave: leer
# veinte filas cuesta lo mismo que leer una, y así trece lecturas
# seguidas pagan una conexión en vez de trece.
#
# POR QUÉ EL TTL Y NO SOLO LA INVALIDACIÓN: puede haber más de un
# worker. El que recibe el POST del panel invalida su caché en el acto
# (ver _guardar); el que no lo recibió se entera al vencer la suya. 10
# segundos es la demora máxima en propagarse un cambio de umbral.
#
# NO ROMPE la semántica documentada en SONADORAS.md ("se leen EN CADA
# CONSULTA, no al importar el módulo"): lo que esa nota descarta es
# congelar el valor hasta el próximo deploy. Diez segundos no es eso.
_TTL_CACHE_SEGUNDOS = 10
_cache_valores: dict[str, str] = {}
_cache_vence: float = 0.0
_cache_lock = _threading.Lock()


def invalidar_cache() -> None:
    """Tira la caché al piso -- la llama _guardar() al escribir, para que
    el worker que recibió el cambio lo vea en la consulta siguiente sin
    esperar el TTL."""
    global _cache_vence
    with _cache_lock:
        _cache_vence = 0.0


def _valores_guardados() -> dict[str, str]:
    """La tabla entera, cacheada. Si la base falla se devuelve {} y cada
    clave cae a env/default -- igual que antes, un problema de base no
    puede dejar al servicio sin poder leer un umbral."""
    global _cache_valores, _cache_vence
    ahora = _time.monotonic()
    with _cache_lock:
        if ahora < _cache_vence:
            return _cache_valores
    try:
        from production.api.deps import get_conn
        conn = get_conn()
        try:
            _ensure_tabla(conn)
            filas = conn.execute(
                "SELECT clave, valor FROM ftr_config_estrategias").fetchall()
        finally:
            conn.close()
        nuevos = {f["clave"]: f["valor"] for f in filas}
    except Exception as e:
        log.warning(f"[ConfigEstrategias] no se pudo leer la configuración de la base "
                    f"({e}) -- se usa env/default")
        return {}
    with _cache_lock:
        _cache_valores = nuevos
        _cache_vence = _time.monotonic() + _TTL_CACHE_SEGUNDOS
        return _cache_valores


def obtener(clave: str) -> str:
    """Valor vigente de `clave`, siempre como string -- ver
    obtener_bool()/obtener_float() para el tipo ya convertido. Una
    clave que no está en REGISTRO devuelve "" (no rompe el caller,
    pero queda logueado -- es un error de programación, no algo que
    pueda pasar desde el panel).

    PRIORIDAD (sin cambios): 1) lo guardado en la base, 2) variable de
    entorno, 3) default del registro."""
    if clave not in REGISTRO:
        log.warning(f"[ConfigEstrategias] clave desconocida: {clave!r} -- revisar REGISTRO")
        return ""
    guardados = _valores_guardados()
    if clave in guardados:
        return guardados[clave]
    # str() SIEMPRE (21/08/2026, incidente real: Admin → Estrategias
    # devolvía 500 y la pantalla entera quedaba en "Error 500", sin
    # tabla de umbrales y sin tabla de ruteo).
    #
    # Cinco claves de Movimiento Rentable (MOV_SUGERENCIA_ACTIVA y las
    # cuatro MOV_FLIP_*) se habían escrito con el default como número
    # Python (`"default": 0.0`) en vez de string (`"0"`), que es lo que
    # usa el resto del registro. Mientras esas claves tuvieran fila en
    # la base o variable de entorno no se notaba -- se devolvía el
    # string guardado. Sin fila y sin env, este return entregaba un
    # float, y el primer obtener_bool() sobre él reventaba con
    # AttributeError: 'float' object has no attribute 'strip'. Como
    # interruptores_vigentes() corre dentro del GET del panel, el error
    # se llevaba puesta la respuesta completa: ningún umbral se podía
    # ver ni editar, incluidos los del matchup de postSet 1.
    #
    # Los cinco defaults ya se corrigieron a string arriba. Esto es la
    # barrera: la firma dice `-> str` y ahora se cumple pase lo que
    # pase, así un default mal tipado en el futuro sale como un número
    # raro en un campo y no como una pantalla caída.
    return str(os.getenv(clave, REGISTRO[clave]["default"]))


def obtener_bool(clave: str) -> bool:
    return obtener(clave).strip().lower() not in ("0", "false", "no", "")


def obtener_float(clave: str) -> float:
    try:
        return float(obtener(clave))
    except (TypeError, ValueError):
        return float(REGISTRO.get(clave, {}).get("default", 0) or 0)


def obtener_int(clave: str) -> int:
    try:
        return int(float(obtener(clave)))
    except (TypeError, ValueError):
        return int(float(REGISTRO.get(clave, {}).get("default", 0) or 0))


def obtener_texto(clave: str) -> str:
    """
    Un texto editable (encabezados, frases, el mensaje del botón).

    A diferencia de obtener(), si lo guardado quedó vacío devuelve el
    DEFAULT del registro: un encabezado en blanco no es una preferencia,
    es un mensaje roto. Las frases sí pueden quedar vacías a propósito
    (ver MSG_FRASE_manual) -- para esas el default también es "", así
    que este fallback no cambia nada.
    """
    valor = obtener(clave)
    if valor is None or not str(valor).strip():
        return REGISTRO.get(clave, {}).get("default", "")
    return valor


def obtener_lista(clave: str) -> list[str]:
    """
    Un parámetro de tipo lista separada por coma -- RACHA_ESTADOS,
    MOV_CONFIANZAS, FTR_VIVO_ESTRATEGIAS. Devuelve los elementos en
    minúscula y sin espacios, sin vacíos.
    """
    return [p.strip().lower() for p in (obtener(clave) or "").split(",") if p.strip()]


def estrategia_activa(categoria: str) -> bool:
    """
    ¿La categoría de alerta `categoria` (la misma de la Matriz de envío:
    'under', 'racha', 'movimiento'...) está encendida?

    Una categoría sin interruptor en el registro se considera ENCENDIDA:
    el default de todo el sistema es que las cosas se manden, y una
    categoría nueva no puede quedar muda por olvidarse de agregar acá su
    parámetro.
    """
    clave = f"ESTRATEGIA_ACTIVA_{(categoria or '').upper()}"
    if clave not in REGISTRO:
        return True
    return obtener_bool(clave)


# ═════════════════════════════════════════════════════════════════════
#  MOVER UN DEFAULT SIN ENTRAR A ADMIN (17/08/2026)
# ═════════════════════════════════════════════════════════════════════
#
# EL PROBLEMA, que ya costó tiempo dos veces: lo guardado en
# ftr_config_estrategias le gana al default del código. Cambiar el
# default y redesplegar NO mueve una clave que alguna vez se registró
# -- por eso el panel LIVE siguió diciendo "MatchUp +55" cuando el
# código decía 0.53, y por eso los umbrales de Soñadoras del 17/08 no
# se iban a mover solos. La única salida era acordarse de tocarlos a
# mano en Admin, y "lo que depende de acordarse, se olvida" (regla de
# Maria del 27/07).
#
# CÓMO FUNCIONA. Cada entrada dice: clave, el valor ANTERIOR del
# default, el valor NUEVO, y la fecha. Al arrancar, para cada entrada:
#
#   · si la fila guardada dice EXACTAMENTE el valor anterior, se
#     reemplaza por el nuevo -- esa fila nunca se editó a mano, solo
#     tiene registrado el default de su momento;
#   · si dice CUALQUIER OTRA COSA, no se toca -- alguien la calibró a
#     propósito y un deploy no tiene derecho a pisarle esa decisión;
#   · si no hay fila, no pasa nada: el default del código ya manda.
#
# Esa asimetría es todo el diseño. Lo que se está corrigiendo no es "el
# valor guardado", es el hecho de que un default viejo quedara
# fosilizado en la base sin que nadie lo eligiera.
#
# CÓMO SE USA. Al cambiar un default en REGISTRO, se agrega acá una
# línea con el valor que tenía antes. Aplicada una vez, la entrada
# queda inofensiva (ya no va a encontrar ninguna fila con el valor
# viejo), así que se puede dejar como registro histórico.
MIGRACIONES_DEFAULT: list[tuple[str, str, str, str]] = [
    # (clave, valor anterior, valor nuevo, fecha del cambio)
    #
    # Soñadoras (17/08/2026): los dos cortes de la pestaña. Se bajó el de
    # Elo y se subió el de matchup al punto donde el estilo deja de
    # favorecer al rival.
    ("FTR_SON_UMBRAL_ELO", "0.65", "0.55", "17/08/2026"),
    ("FTR_SON_UMBRAL_MATCHUP", "0.485", "0.50", "17/08/2026"),
    # Pestaña "MatchUp" del panel LIVE (16/08/2026): la primera versión
    # registró 0.55 en la base antes de que el default quedara en 0.53,
    # y desde entonces el botón mostraba "+55" sin que nadie lo hubiera
    # elegido. Es el caso que motivó todo este mecanismo.
    ("FTR_VIVO_MATCHUP_TAB_MIN", "0.55", "0.53", "17/08/2026"),
    # Partidos Maestros (23/09/2026, reglas congeladas). Solo se mueven
    # las filas que tenían el default anterior; si alguien las había
    # editado, NO se tocan -- revisar Admin después del deploy (la
    # consulta de verificación está en CAMBIOS-20260923-maestros-under.md).
    ("PARTIDOS_MAESTROS_CUOTA_MAX", "1.50", "1.37", "23/09/2026"),
    ("PARTIDOS_MAESTROS_CUOTA_MAX", "1.5", "1.37", "23/09/2026"),
    ("PARTIDOS_MAESTROS_ELO_MIN", "0.72", "0.80", "23/09/2026"),
    ("PARTIDOS_MAESTROS_FTR_MIN", "0.50", "0.60", "23/09/2026"),
    ("PARTIDOS_MAESTROS_FTR_MIN", "0.5", "0.60", "23/09/2026"),
    ("PARTIDOS_MAESTROS_FTR_MAX", "0.75", "0.80", "23/09/2026"),
    ("MSG_CAB_partidos_maestros", "🎓 PARTIDOS MAESTROS", "⭐ PARTIDO MAESTRO", "23/09/2026"),
    # UTR Market Anomaly (08/10/2026 noche, Rubén: "que suene a partir de una
    # diferencia de 0.00"): la puerta de ΔUTR deja de cortar; queda la cuota
    # ≥ UTRA_CUOTA_MIN y los controles del dato.
    ("UTRA_DELTA_MIN", "0.25", "0.00", "08/10/2026"),
    # 09/10/2026 (Rubén): cuota mínima PROVISIONAL 3.00 (caso Stevens 2.60 → 3.50
    # sin jugarse un punto). Se siguen guardando todos los cruces para decidir
    # el corte con datos propios.
    ("UTRA_CUOTA_MIN", "4.00", "3.00", "09/10/2026"),
    ("UTRA_CUOTA_MIN", "4.0", "3.00", "09/10/2026"),
]


def aplicar_migraciones_default(conn=None) -> list[str]:
    """
    Corre MIGRACIONES_DEFAULT. Devuelve la lista de claves movidas --
    vacía en el arranque siguiente, porque ya no queda ninguna fila con
    el valor viejo.

    Se llama UNA VEZ al arrancar (production/api/app.py). No puede
    tumbar el arranque: cualquier fallo se loguea y se sigue, porque un
    umbral desactualizado es un problema menor comparado con un
    servicio que no levanta.
    """
    propio = conn is None
    movidas: list[str] = []
    try:
        if propio:
            from production.api.deps import get_conn
            conn = get_conn()
        _ensure_tabla(conn)
        for clave, viejo, nuevo, fecha in MIGRACIONES_DEFAULT:
            if clave not in REGISTRO:
                log.warning(f"[ConfigEstrategias] migración de una clave que ya no existe: {clave!r}")
                continue
            fila = conn.execute(
                "SELECT valor FROM ftr_config_estrategias WHERE clave=?", (clave,)).fetchone()
            if fila is None or str(fila["valor"]).strip() != viejo:
                continue        # nunca se guardó, o alguien la calibró: no se toca
            _guardar(clave, nuevo, por=f"migración {fecha}")
            movidas.append(clave)
            log.info(f"[ConfigEstrategias] {clave}: {viejo} -> {nuevo} "
                     f"(default movido el {fecha}; la fila tenía el default anterior, "
                     f"nadie la había editado)")
    except Exception as e:
        log.warning(f"[ConfigEstrategias] no se pudieron aplicar las migraciones de default: {e}")
    finally:
        if propio and conn is not None:
            conn.close()
    if movidas:
        invalidar_cache()
    return movidas



# ═════════════════════════════════════════════════════════════════════
#  SEPARAR UNA CLAVE COMPARTIDA SIN CAMBIAR EL COMPORTAMIENTO (20/08/2026)
# ═════════════════════════════════════════════════════════════════════
#
# EL RIESGO QUE CUBRE. Cuando una clave que servía a tres estrategias se
# parte en tres claves propias, las nuevas arrancan con el default del
# código. Si el usuario tenía la compartida calibrada (ejemplo real: la
# cuota en vivo estaba en 1,40 y el default del código decía 1,70), las
# tres estrategias cambian de comportamiento en el mismo deploy y NADA
# lo avisa. Es el fallo más silencioso posible: no hay error, no hay
# log, solo se empieza a apostar otra cosa.
#
# QUÉ HACE. Para cada par (origen, destino): si el destino todavía NO
# tiene fila propia, se le escribe el VALOR VIGENTE del origen -- el
# que devuelve obtener(), o sea base > variable de entorno > default.
# Copiar el valor vigente y no solo la fila guardada es a propósito:
# así también se preserva un valor que venía de una variable de entorno
# de Railway, que es como estaban varios antes de que existiera el panel.
#
# Si el destino ya tiene fila, no se toca NUNCA: el usuario ya lo
# calibró después de la separación y un deploy no tiene derecho a
# pisarlo. Por eso la copia es idempotente y se puede dejar corriendo
# para siempre.
MIGRACIONES_COPIA: list[tuple[str, str, str]] = [
    # (clave origen — la compartida de antes, clave destino — la propia, fecha)

    # Partidos en Vivo leía los umbrales de Valor Pre-Partido. Es lo que
    # hizo que el caso Petkovic vs Giraldi (20/08/2026) fuera tan difícil
    # de diagnosticar: el número que decidía no estaba en la sección.
    ("FTR_ELO_UMBRAL_ELO", "VIVO_ELO_MIN", "20/08/2026"),
    ("FTR_ELO_UMBRAL_FTR", "VIVO_FTR_MIN", "20/08/2026"),

    # Valor Pre-Partido: sus dos umbrales propios + lo que compartía.
    ("FTR_ELO_UMBRAL_ELO", "VPRE_ELO_MIN", "20/08/2026"),
    ("FTR_ELO_UMBRAL_FTR", "VPRE_FTR_MIN", "20/08/2026"),
    ("FTR_ALERTA_MATCHUP_MIN", "VPRE_MATCHUP_MIN", "20/08/2026"),
    ("FTR_ALERTA_MATCHUP_BANDA", "VPRE_MATCHUP_BANDA", "20/08/2026"),
    ("FTR_MATCHUP_ESPERA_MIN", "VPRE_MATCHUP_ESPERA_MIN", "20/08/2026"),
    ("FTR_MATCHUP_SIN_DATO", "VPRE_MATCHUP_SIN_DATO", "20/08/2026"),
    ("FTR_ALERTA_CUOTA_MIN", "VPRE_CUOTA_MIN", "20/08/2026"),
    ("FTR_ALERTA_CUOTA_MAX", "VPRE_CUOTA_MAX", "20/08/2026"),
    ("MUESTRA_MINIMA_FTR", "VPRE_MUESTRA_FTR", "20/08/2026"),
    ("MUESTRA_MINIMA_ELO", "VPRE_MUESTRA_ELO", "20/08/2026"),

    # Valor FTR: era la mitad de cumple_filtros_alerta().
    ("FTR_ALERTA_ELO_MIN", "VFTR_ELO_MIN", "20/08/2026"),
    ("FTR_ALERTA_FTR_MIN", "VFTR_FTR_MIN", "20/08/2026"),
    ("FTR_ALERTA_MATCHUP_MIN", "VFTR_MATCHUP_MIN", "20/08/2026"),
    ("FTR_ALERTA_MATCHUP_BANDA", "VFTR_MATCHUP_BANDA", "20/08/2026"),
    ("FTR_MATCHUP_GATE", "VFTR_MATCHUP_GATE", "20/08/2026"),
    ("FTR_ALERTA_CUOTA_MIN", "VFTR_CUOTA_MIN", "20/08/2026"),
    ("FTR_ALERTA_CUOTA_MAX", "VFTR_CUOTA_MAX", "20/08/2026"),
    ("FTR_SUPERFICIE_DESCONOCIDA", "VFTR_SUPERFICIE_DESCONOCIDA", "20/08/2026"),


    # Favorito en Vivo usaba los de Valor FTR sin decirlo en ningún lado.
    ("FTR_ALERTA_ELO_MIN", "FAV_ELO_MIN", "20/08/2026"),
    ("FTR_ALERTA_FTR_MIN", "FAV_FTR_MIN", "20/08/2026"),
    ("FTR_ALERTA_MATCHUP_MIN", "FAV_MATCHUP_MIN", "20/08/2026"),
]


def aplicar_migraciones_copia(conn=None) -> list[str]:
    """
    Corre MIGRACIONES_COPIA. Devuelve las claves que se rellenaron.

    Se llama UNA VEZ al arrancar, después de aplicar_migraciones_default
    (production/api/app.py) -- ese orden importa: primero se corrige un
    default fosilizado en el origen, después se copia el valor bueno al
    destino. Al revés se copiaría el valor viejo.

    Como aplicar_migraciones_default, no puede tumbar el arranque.
    """
    propio = conn is None
    copiadas: list[str] = []
    try:
        if propio:
            from production.api.deps import get_conn
            conn = get_conn()
        _ensure_tabla(conn)
        for origen, destino, fecha in MIGRACIONES_COPIA:
            if destino not in REGISTRO:
                log.warning(f"[ConfigEstrategias] copia hacia una clave que no existe: {destino!r}")
                continue
            fila = conn.execute(
                "SELECT valor FROM ftr_config_estrategias WHERE clave=?", (destino,)).fetchone()
            if fila is not None:
                continue        # ya tiene valor propio: es una decisión del usuario
            valor = obtener(origen)
            if valor is None:
                valor = REGISTRO.get(origen, {}).get("default")
            if valor is None:
                continue
            _guardar(destino, str(valor), por=f"separación de claves {fecha}")
            copiadas.append(destino)
            log.info(f"[ConfigEstrategias] {destino} = {valor!r} (copiado de {origen}, "
                     f"separación del {fecha}) -- el comportamiento NO cambia")
    except Exception as e:
        log.warning(f"[ConfigEstrategias] no se pudieron aplicar las copias de claves: {e}")
    finally:
        if propio and conn is not None:
            conn.close()
    if copiadas:
        invalidar_cache()
    return copiadas


# ═════════════════════════════════════════════════════════════════════
#  CÓMO SE ORDENA Y QUÉ SE ESCONDE EN EL PANEL (20/08/2026)
# ═════════════════════════════════════════════════════════════════════
#
# Antes el panel mostraba las categorías en el orden en que estaban
# escritas en REGISTRO, que es el orden en que se fueron agregando: sin
# ningún criterio. Ahora manda esta lista -- una sección por estrategia,
# las que más se tocan arriba. Una categoría que no esté acá sale al
# final, así que agregar una clave nueva nunca la hace desaparecer.

# ═════════════════════════════════════════════════════════════════════
#  QUÉ HACE CADA ESTRATEGIA, ARRIBA DE SUS NÚMEROS (20/08/2026)
# ═════════════════════════════════════════════════════════════════════
#
# Pedido de Rubén: "que yo sepa qué características tiene cada
# estrategia". Separar las claves resolvió la mitad del problema -- cada
# número tiene dueño -- pero seguía faltando la otra mitad: qué mira la
# estrategia, EN QUÉ ORDEN, y qué corta primero. Sin eso, saber que un
# umbral es propio no dice si moverlo va a servir de algo.
#
# El orden de los cortes de cada ficha es el REAL, el del código. Si
# mañana se reordena un filtro, hay que reordenar la ficha: una ficha
# que miente es peor que no tener ficha.
FICHAS_CATEGORIAS: dict[str, str] = {
    "Partidos en Vivo (postSet 1)":
        "El favorito PRE-PARTIDO pierde el Set 1 y el mercado lo castiga. Cortes, en "
        "orden: (1) los dos jugadores con muestra suficiente en la superficie; "
        "(2) Prob. FTR y Prob. Elo del fixture por encima de los mínimos de abajo; "
        "(3) el favorito PERDIÓ el Set 1; (4) la ventana sigue abierta -- minutos desde "
        "el cierre si vimos cerrar el set, o games del set en curso si el partido llegó "
        "con el Set 1 hecho; (5) matchup de estilo del favorito; (6) cuota EN VIVO dentro "
        "de la banda. La cuota se busca hacia atrás dentro de su propia ventana: si el "
        "mercado está suspendido (pasa en el descanso entre sets) se lee el último precio "
        "conocido, que puede ser del set anterior.\n\n"
        "MUESTRA MÍNIMA (20/08/2026, a pedido de Maria: 've el número acá'). El corte (1) "
        "de arriba NO se decide con un número propio de esta sección -- se lee de la "
        "columna muestra_suficiente que YA quedó escrita en el fixture cuando Valor "
        "Pre-Partido lo reportó, con SUS partidos mínimos: {muestra_ftr} en FTR y "
        "{muestra_elo} en Elo (Admin → Estrategias → Valor Pre-Partido, 'Partidos mínimos, "
        "motor FTR/Elo'). Por eso no hay un campo editable acá: un número propio de esta "
        "sección se vería guardado pero no cambiaría nada -- el fixture ya llegó con el "
        "veredicto tomado. Para subir o bajar la muestra, se edita en Valor Pre-Partido; "
        "el cambio rige para los próximos fixtures que se reporten (no reevalúa los que ya "
        "están en la base). (Ver el cuadro 'A dónde llega' arriba de la tabla para el "
        "detalle de dónde se guarda y se manda cada resultado.)\n\n"
        "EL MATCHUP VA ESCRITO EN LA ALERTA (20/08/2026, a pedido de Rubén: 'no entiendo "
        "por qué sonó este postSet'). Cada alerta que suena lleva en su texto el matchup "
        "EXACTO con el que se decidió, orientado al favorito, con el umbral contra el que "
        "se comparó y la superficie: 'Matchup 58.4% (≥50%) en Dura'. Si el corte está en "
        "0 dice '(corte apagado)', y si el worker de estilo no había terminado dice "
        "'Matchup sin dato (pendiente|sin_dato)' -- en ese caso quien decidió fue el "
        "desplegable 'Sin matchup calculado' de acá abajo, no el 50%. Se ve en la columna "
        "'Contexto' de la pestaña 'Partidos en Vivo' del panel y en el mensaje de "
        "Telegram.\n\n"
        "OJO AL COMPARARLO CON EL MODAL: el modal del panel (IA Match Analyzer, "
        "'Ventaja táctica por estilo') RECALCULA el matchup en vivo por superficie contra "
        "/estilo/comparar, mientras que el gate lee el snapshot que dejó el worker en "
        "style_fixture_history al momento de la alerta. Si los dos números no coinciden, "
        "no es un bug del gate: es que el snapshot es más viejo que el recálculo (el "
        "perfil de estilo de alguno de los dos cambió desde entonces, o el snapshot vino "
        "del mismo cruce en otra fecha). El número que decidió es el de la alerta.",
    "Valor Pre-Partido":
        "Antes de que empiece el partido: el favorito según FTR y Elo cotiza dentro de una "
        "banda de cuota que deja margen. Cortes: (1) muestra mínima en la superficie para "
        "los dos motores; (2) Prob. FTR y Prob. Elo; (3) cuota de mercado del favorito "
        "dentro de la banda; (4) matchup de estilo, con espera si el worker todavía no lo "
        "calculó. Es la estrategia que CALCULA la muestra que después usan las otras.",
    "Valor FTR":
        "Alerta que llega desde el forwarder y se valida contra los modelos. Los cuatro "
        "filtros corren juntos en cumple_filtros_alerta(): Prob. Elo, Prob. FTR, matchup "
        "de estilo (con banda neutral) y banda de cuota. Desde el 20/08/2026 usa umbrales "
        "propios: mover uno acá ya no toca a las demás estrategias.",
    "Movimiento Rentable":
        "REGLA ÚNICA desde el 20/08/2026: el tapado en apertura CRUZA a favorito, y se "
        "mira el precio de ese primer cruce. Los cuatro números de abajo son toda la "
        "estrategia -- no hay más filtros. Respaldo estadístico DÉBIL (n=21, p=0,18): "
        "hipótesis pre-registrada, no ventaja demostrada. Stake plano y bajo hasta ~200 "
        "casos.",
    "Movimiento Rentable — canal Sugerencias":
        "Segunda puerta, para el canal Sugerencias solamente. HOY ESTÁ APAGADA "
        "(interruptor de abajo en 0) y es a propósito: desde el cambio del 20/08/2026 la "
        "población que llega acá es la que CRUZÓ a favorito pero no llegó al 50% en alguna "
        "de las tres probabilidades -- o sea el grupo de control del backtest, que midió "
        "39 casos con -24,9% de ROI. Encenderlo es mandar a apostar el grupo que se midió "
        "perdiendo. Los cuatro umbrales quedan calibrables por si más adelante se quiere "
        "un segundo canal; mientras el interruptor esté en 0, ninguno de ellos se lee.",
    "Favorito en Vivo":
        "Favorito en vivo por diferencia de UTR. Cortes, en DOS etapas: (1) del lado de "
        "Reenvio_MTO y de nuevo acá como red de seguridad -- diferencia de UTR dentro de "
        "la banda Y cuota mínima del favorito (Admin → Estrategias → 'Favorito en Vivo', "
        "campos UTR/cuota); (2) recién si pasa eso, los tres umbrales de modelo de abajo "
        "(Elo, FTR, matchup) -- que hasta el 20/08/2026 se leían de Valor FTR y no "
        "aparecían en esta sección.",
    "Under en Vivo":
        "Under de games por diferencia de UTR entre los dos jugadores. La lista de "
        "circuitos silenciados manda los de esos circuitos a Sugerencias sin notificar, "
        "en vez de descartarlos: son pocos casos y conviene medirlos antes de decidir.",
    "Jugador en Racha":
        "Un jugador encadena games seguidos. Se filtra por el estado del partido y se "
        "deduplica por enfrentamiento durante unas horas.",
    "UTR VALUE":
        "Pre-partido. Siempre desde el jugador con UTR SUPERIOR. Se guarda TODO partido con UTR "
        "de los dos (cualquier cuota, cualquier diferencia) con su foto: UTR general y por "
        "superficie con fecha de captura y el mercado. Funciona SOLO con UTR + cuota: Elo, FTR "
        "y MatchUp no confirman ni descartan nada. CANDIDATO = ΔUTR ≥ "
        "{UTRV_DELTA_MIN} y cuota ≥ {UTRV_CUOTA_MIN} (las dos; sin cuota máxima). A Discord solo "
        "llega el candidato que además pasa los controles técnicos del dato (identidad y "
        "antigüedad del UTR, cuotas coherentes, no duplicado). La Prob. UTR y el UTR VALUE en "
        "pp se calculan y guardan como métrica de estudio: no aprueban ni descartan nada. "
        "Cada partido guarda POR QUÉ no salió. "
        "Telegram avisa al aprobarse; Discord, cuando el partido empieza.",
    "UTR MARKET ANOMALY":
        "Señal SEPARADA de UTR VALUE. Siempre desde el jugador con UTR SUPERIOR. DISPARA con "
        "ΔUTR ≥ {UTRA_DELTA_MIN} y cuota ≥ {UTRA_CUOTA_MIN} (sin cuota máxima). Solo UTR + precio: "
        "Elo, FTR y MatchUp no intervienen; la Prob. UTR y el UTR VALUE en pp se guardan para "
        "estudio, no son puertas. Se revisa CADA cuota nueva, pre-partido y en vivo: si la cuota "
        "sube hasta el corte, dispara en ese momento, una sola vez por partido, y la evolución se "
        "sigue guardando. EN VIVO solo suena si todavía NO hay games jugados (0-0, partido recién "
        "abierto): una cuota que llegó a 4.00 por perder games queda para estudio, sin Discord. Todo partido con ΔUTR ≥ {UTRA_DELTA_MIN} queda guardado aunque no "
        "llegue al corte (estudio por bandas de cuota y de ΔUTR). Si también cumple UTR VALUE, "
        "sale un solo mensaje: el de UTR VALUE. Controles técnicos: identidad y antigüedad del "
        "UTR y cuotas coherentes. Telegram al disparar; Discord en vivo en el acto, pre-partido "
        "cuando el partido empieza.",
    "UNDER UTR":
        "🔥 v54i (15/10/2026, Rubén). SOLO PRE-PARTIDO. Señal únicamente si cumple TODO antes del primer punto: "
        "ΔUTR = |UTR A − UTR B| ≥ {UNDERUTR_DELTA_MIN} con nuestro padrón UTR, línea UNDER ≥ {UNDERUTR_LINEA_MIN} y "
        "cuota del UNDER ≥ {UNDERUTR_CUOTA_MIN}, singles y fuera del circuito UTR Pro. Sin Elo, FTR ni MatchUp. El "
        "bot sigue los partidos POR JUGARSE (SportsAPI365) y, a los que tienen el ΔUTR, les revisa el mercado solo desde "
        "5 min antes de la hora programada hasta que empiecen (cada 60 s); "
        "antes de activar, verifica el estado REAL del partido (no la hora programada). Línea y cuota quedan "
        "congeladas. Con el primer punto, el partido queda cerrado para siempre: si no dio señal, se guarda el motivo "
        "(no abrió el mercado, nunca línea, nunca cuota, no se pudo verificar). Discord en el acto.",
    "Recordador":
        "⏰ Recordador: cualquier usuario pide un jugador (se guarda una sola vez) y se avisa su PRÓXIMO "
        "partido UNA vez, cuando faltan {RECORDADOR_MINUTOS_ANTES} minutos o cuando ya está en juego (lo que "
        "pase primero): Discord (FT Intelligence), Telegram (Matriz de envío) y celular de quienes lo pidieron.",
    "UTR EXTERNO":
        "PUENTE PROVISORIO (09/10/2026): las alertas LIVE VALUE / PREMATCH VALUE del canal de Telegram "
        "DOC | TIRS llegan por el reenviador (Reenvio_MTO → POST /utr-externo/registrar), se guardan "
        "todas y se publican en Discord con EL MISMO MENSAJE de nuestras estrategias (LIVE → 🚨 UTR "
        "MARKET ANOMALY; PREMATCH → 📈 UTR VALUE si cumple sus reglas, si no UTR MARKET ANOMALY), vía "
        "FT Intelligence (/ft-intel/utr-externo). Los UTR son del canal, no del padrón. En la base queda "
        "qué cumpliría con nuestras reglas. No cambia ningún motor propio; si el mismo partido ya salió "
        "por UTR VALUE, Cuota Mal Puesta o MARKET ANOMALY, el externo no se repite.",
    "UTR POWER":
        "UTR REFRESH MANAGER: trae el UTR exacto de UTR Power (cuenta del proyecto) y lo guarda en "
        "el padrón con su fuente y fecha. Orden: "
        "partido en 24 h → 48 h → UTR ocultos nunca traídos → el resto → mantenimiento. Se para en "
        "el acto con un 429 (pausa) o con 401/403 (AUTH_REQUIRED). No toca identidades ni reglas.",
    "Cuota Mal Puesta":
        "Pre-partido. FullTenis calcula su probabilidad SIN mirar la cuota: (1) Elo + FTR "
        "del jugador (promedio ponderado, nunca el más optimista; penalizado si los dos motores "
        "no coinciden); (2) L10 de LOS DOS jugadores -- últimos partidos anteriores a la señal, "
        "con el nivel de cada rival en ese momento, ponderados por recencia -- que mueve la "
        "probabilidad como máximo ±{CMP_L10_AJUSTE_MAX_PP} pp (V3: nivel de los rivales por UTR). Recién ahí "
        "se resta la probabilidad del MERCADO SIN MARGEN (las dos cuotas, sin el margen de la casa): "
        "esa diferencia en puntos es la CUOTA MAL PUESTA (clasificación para estudio: 🟡 desde "
        "{CMP_UMBRAL_ESTUDIO} pp, 🔥 desde {CMP_UMBRAL_CMP}, 💎 desde {CMP_UMBRAL_PREMIUM}). A "
        "Discord sale SOLO con probabilidad FullTenis ≥ {CMP_DISCORD_FT_MIN_PCT} % Y ventaja ≥ "
        "{CMP_DISCORD_VENTAJA_MIN_PP} pp (las dos), además de Elo/FTR alineados, L10 suficiente y "
        "sin contradecir y cuotas coherentes; lo demás queda como ESTUDIO. 💎 FUERTE/PREMIUM: "
        "FT ≥ {CMP_FUERTE_FT_MIN_PCT} % y ventaja ≥ {CMP_FUERTE_VENTAJA_MIN_PP} pp. Telegram avisa apenas "
        "se detecta; Discord, cuando el partido empieza. Todo queda guardado con "
        "su foto exacta y los 20 partidos L10 que la produjeron.",
    "Soñadoras":
        "Pestaña de los partidos de mañana con la combinada. Sus tres umbrales son SOLO "
        "de la pestaña: no mandan ninguna alerta a Telegram.",
    "Panel LIVE (solo la vista, no alerta)":
        "Cortes de presentación del panel LIVE. No deciden ninguna alerta: mover esto "
        "cambia qué ves, nunca qué se apuesta.\n\n"
        "DÓNDE SE VEN ESTOS FILTROS, PARTIDO POR PARTIDO (20/08/2026, a pedido de Maria: "
        "'que se vean los campos'). NO es en esta pantalla de Estrategias -- acá solo se "
        "AJUSTAN los números. Los partidos, con cada filtro aplicado, están en la pestaña "
        "'LIVE 🔴' → sección 'EN VIVO — DIAGNÓSTICO' del panel principal (no del Admin). "
        "Ahí:\n"
        "• Prob. FTR, Prob. Elo y Matchup de CADA partido en vivo: columnas propias en la "
        "tabla, con la cuota en vivo al lado -- todos los partidos, suenen o no.\n"
        "• 'Pasan el filtro' (botón ⭐, arriba de la tabla): los partidos con muestra "
        "suficiente + Prob. FTR entre {vivo_ftr_min} y {vivo_ftr_max} + Prob. Elo ≥ "
        "{vivo_elo_min} -- los MISMOS umbrales de Admin → Estrategias → Partidos en Vivo "
        "(postSet 1), no un número aparte. Si el favorito además pasa el matchup y la "
        "cuota en vivo, la fila queda '✅ Suena'; si le falta la cuota o el matchup, queda "
        "'🟢 Pasa FTR+Elo' nomás.\n"
        "• 'Ventaja leve' (botón ⚖️, REDEFINIDO 20/08/2026 a pedido de Maria): dos grupos, "
        "entra con CUALQUIERA de los dos. Grupo 1: FTR (entre {vivo_ftr_min} y "
        "{vivo_ftr_max}) y Elo por encima de los umbrales de arriba, SIN exigir matchup -- "
        "esto reemplaza a la vieja pestaña 'Frena el MatchUp' (sacada el 20/08/2026 por "
        "redundante: cubría menos casos que este grupo, que además incluye los partidos "
        "con matchup todavía sin calcular). Grupo 2: FTR ≥ {leve_ftr}, Elo ≥ {leve_elo} Y "
        "Matchup ≥ {leve_mup} -- los TRES a la vez, con sus propios pisos editables acá "
        "abajo. Cada fila dice por cuál de los dos grupos (o los dos) entró, con una "
        "etiqueta G1/G2/G1+G2.\n"
        "• CUÁL DE LOS NÚMEROS DE ABAJO ES DE CADA GRUPO (21/08/2026, a pedido de Rubén: "
        "'no se distingue entre los parámetros del grupo 1 y grupo 2'). Los TRES pisos de "
        "'Ventaja leve' que aparecen en la tabla de abajo son del GRUPO 2 y de nadie más "
        "-- por eso ahora se llaman así. El GRUPO 1 NO TIENE NINGÚN PARÁMETRO EDITABLE EN "
        "ESTA SECCIÓN: usa {vivo_ftr_min}/{vivo_ftr_max}/{vivo_elo_min}, que son "
        "VIVO_FTR_MIN, VIVO_FTR_MAX y VIVO_ELO_MIN, y se editan en Admin → Estrategias → "
        "Partidos en Vivo (postSet 1). Es a propósito: son los mismos umbrales que deciden "
        "si una alerta suena, así que un número propio acá los podría desincronizar. "
        "Consecuencia práctica: mover los pisos de abajo NUNCA cambia quién entra por "
        "Grupo 1, y mover los de Partidos en Vivo cambia el Grupo 1 de esta pestaña además "
        "de las alertas.\n"
        "• El único otro número de abajo, 'MatchUp: corte de la pestaña del panel LIVE' "
        "(FTR_VIVO_MATCHUP_TAB_MIN), no es de ninguno de los dos grupos: gobierna la "
        "pestaña MatchUp, que es transversal y no toca 'Ventaja leve'.\n"
        "La columna 'Por qué' de esa tabla dice, para cada partido, el PRIMER corte que no "
        "pasó -- ahí se ve exactamente por qué uno que se ve verde no terminó sonando.",
    "Obsoletos — ya no deciden nada":
        "Números que quedaron fuera del recorrido. Se muestran para poder auditar de dónde "
        "salió el valor de cada clave nueva. Editarlos no cambia nada.",
    "FT PROBABILIDAD": (
        "Servicio separado de RankingFTR. Lee fixtures y UTR mediante interfaces internas "
        "con límite de frecuencia; obtiene estadísticas históricas de Tennis Data, "
        "calcula una estimación experimental (pesos de fábrica 60/30/10, editables aquí; "
        "tienen que sumar 100) y guarda su aprendizaje en almacenamiento propio. "
        "No emite picks ni alertas. El interruptor es el único control de activación del servicio."
    ),
}

ORDEN_CATEGORIAS: list[str] = [
    "FT PROBABILIDAD",  # servicio separado, apagado por defecto
    "Partidos en Vivo (postSet 1)",
    "Panel LIVE (solo la vista, no alerta)",
    "Valor Pre-Partido",
    "Valor FTR",
    "Movimiento Rentable",
    # "Movimiento Rentable — canal Sugerencias" salió del panel el
    # 21/08/2026 (sus claves están en OCULTAS). Se deja el renglón
    # comentado para que se vea dónde iba si alguna vez vuelve.
    "Favorito en Vivo",
    "Under en Vivo",
    "Jugador en Racha",
    "Racha sencilla (canal externo — no es Jugador en Racha)",
    "Soñadoras",
    # 07/10/2026 -- motor nuevo, ver production/engines/cuota_mal_puesta.py.
    "Cuota Mal Puesta",
    "UTR VALUE",
    "UTR MARKET ANOMALY",
    "UNDER UTR",              # v54i (15/10/2026)
    "UTR EXTERNO",
    "UTR POWER",
    "Recordador",
    "Avisos de contexto",
    "Encender / apagar estrategias",
    "Conexión en vivo (bot)",
    "Torneos",
    "Resultados de picks",
    "Mensajes — encabezados",
    "Mensajes — frases",
    "Obsoletos — ya no deciden nada",
]

# ═════════════════════════════════════════════════════════════════════
#  RUTEO DE ALERTAS -- tabla resumen, un vistazo por estrategia (20/08/2026)
# ═════════════════════════════════════════════════════════════════════
#
# Pedido de Maria: "que cada estrategia me diga a dónde se envía y en
# dónde se guarda", con el mismo formato que la Matriz de envío
# (Admin → Permisos). La Matriz dice A QUÉ CANAL llega cada categoría
# ya decidida; esto es un paso ANTES -- dice, para cada estrategia, qué
# pasa según pase o no pase sus propios filtros (arriba en cada ficha).
# Las dos tablas se complementan, no se pisan.
#
# Texto CORTO a propósito -- el detalle completo (ventanas de gracia,
# el Gate de IA, veredictos revisables vs permanentes) vive en la ficha
# de cada categoría, arriba de sus números. Si el código cambia y esto
# queda desactualizado, es peor que no tener la tabla -- mantenerla al
# día es tan importante como la ficha misma.
RUTEO_ESTRATEGIAS: list[dict] = [
    {
        "estrategia": "Partidos en Vivo (postSet 1)",
        "si_pasa": "Canal Admin + Matriz de envío (Telegram) y pestaña 'Partidos en Vivo' del panel",
        "si_no_pasa": "Nada, por defecto (configurable: 'A dónde llega' → Sugerencias/Descartes) -- también queda el log y la pestaña '🔴 LIVE' → EN VIVO — DIAGNÓSTICO, que se recalcula sola, no es un guardado",
    },
    {
        "estrategia": "Valor Pre-Partido",
        "si_pasa": "Canal Admin (Telegram, inmediato) + pestaña '🗂 Historial de Picks'",
        "si_no_pasa": "Nada, por defecto (configurable: 'A dónde llega' → Sugerencias/Descartes) -- no pasó FTR/Elo/muestra/cuota al abrir, o el matchup lo bloqueó",
    },
    {
        "estrategia": "Valor FTR",
        "si_pasa": "Canal Admin (Telegram) + pestaña 'Valor FTR' (siempre) + '🗂 Historial de Picks' si además sale solo",
        "si_no_pasa": "Se queda en 'Valor FTR', por defecto (configurable: 'A dónde llega' → además copiarla a Sugerencias/Descartes)",
    },
    {
        "estrategia": "Movimiento Rentable",
        # 21/08/2026: pasó de salir por la categoría 'sugerencias' a
        # salir por su fila propia de la Matriz de envío. La fila queda
        # igual en la pestaña Sugerencias del panel para revisarla; lo
        # que cambió es por dónde se manda el mensaje de Telegram.
        "si_pasa": ("Canal Admin (Telegram) + Matriz de envío, fila 'Movimiento Rentable' "
                    "(hoy: solo Grupo Exclusivo) + pestaña '💡 Sugerencias' del panel. "
                    "Es un AVISO, no un pick: va como texto, sin tarjeta y sin entrar al "
                    "Historial de Picks -- mismo trato que postSet 1"),
        "si_no_pasa": "Si cruzó a favorito: pestaña '💡 Sugerencias' por defecto (configurable: 'A dónde llega' → Descartes/nada). Si no cruzó: solo la pestaña 'Movimiento Rentable', eso no se puede reconfigurar (no llegó a evaluarse)",
    },
    {
        "estrategia": "Favorito en Vivo",
        "si_pasa": "Matriz de envío (Telegram) + '🗂 Historial de Picks'",
        "si_no_pasa": "Si falla ΔUTR/cuota: no se guarda (se corta en Reenvio_MTO, antes de acá). Si falla Elo/FTR/matchup: canal Admin con aviso 'sin confirmar' y nada más, por defecto (configurable: 'A dónde llega' → Sugerencias/Descartes)",
    },
    {
        "estrategia": "Under en Vivo",
        "si_pasa": "Matriz de envío (Telegram) + '🗂 Historial de Picks'",
        "si_no_pasa": "Si falla ΔUTR: no se guarda (se corta en Reenvio_MTO). Si el circuito está silenciado: pestaña '💡 Sugerencias' por defecto (configurable: 'A dónde llega' → Descartes/nada)",
    },
    {
        "estrategia": "Jugador en Racha",
        "si_pasa": "Matriz de envío (Telegram, categoría 'racha')",
        "si_no_pasa": "Se queda en su propia pestaña '🔥 Jugador en Racha', por defecto (configurable: 'A dónde llega' → además copiarla a Sugerencias/Descartes)",
    },
    {
        "estrategia": "Soñadoras",
        "si_pasa": "Solo la pestaña '🌙 Soñadoras' -- nunca sale de ahí (ni Telegram, ni Sugerencias, ni Picks)",
        "si_no_pasa": "Solo la pestaña '🌙 Soñadoras', igual que si pasa (no tiene desplegable -- no hay otro destino posible)",
    },
    {
        # 07/10/2026 -- production/engines/cuota_mal_puesta.py.
        "estrategia": "Cuota Mal Puesta",
        "si_pasa": ("🔥/💎 que pasan los controles: Telegram APENAS SE DETECTA (Matriz de "
                    "envío, fila 'Cuota Mal Puesta') + Discord CUANDO EL PARTIDO EMPIEZA (FT "
                    "Intelligence lee /ft-intel/cuota-mal-puesta y espera el feed en vivo; texto "
                    "+ voz) + pestaña 'Cuota Mal Puesta'."),
        "si_no_pasa": ("🟡 desde el umbral de estudio: cmp_senales completa con sus 20 partidos "
                       "L10 (cmp_l10_detalle), pestaña 'Cuota Mal Puesta'. ❌ debajo: una fila "
                       "de control sin detalle (CMP_GUARDAR_DESCARTES)."),
    },
    {
        # 07/10/2026 -- production/engines/utr_value.py.
        "estrategia": "UTR VALUE",
        "si_pasa": ("Candidato (ΔUTR ≥ 0.75 y cuota ≥ 1.70) que pasa los filtros de calidad: "
                    "Telegram al aprobarse (Matriz, fila 'UTR VALUE') + Discord cuando el partido "
                    "empieza (FT Intelligence, /ft-intel/utr-value) + pestaña 'UTR VALUE'."),
        "si_no_pasa": ("Todo partido con UTR de los dos queda en utr_value_registro con su "
                       "motivo (SOLO_ESTUDIO_*, DESCARTADO_*), resultado y P/L, para estudiarlo."),
    },
    {
        # 07/10/2026 -- production/engines/utr_anomalia.py.
        "estrategia": "UTR MARKET ANOMALY",
        "si_pasa": ("ΔUTR ≥ 0.25 y cuota ≥ 4.00 que pasa los controles técnicos: Telegram al "
                    "disparar (Matriz, fila 'UTR MARKET ANOMALY') + Discord (en vivo en el acto; "
                    "pre-partido al empezar; FT Intelligence, /ft-intel/utr-anomalia) + pestaña "
                    "'UTR VALUE', bloque 'UTR MARKET ANOMALY'. Si también cumple UTR VALUE, solo "
                    "sale UTR VALUE."),
        "si_no_pasa": ("Todo partido con ΔUTR ≥ 0.25 queda en utr_anomalia_registro (ESTUDIO) con "
                       "la evolución de su cuota y el primer cruce de cada umbral, resultado y P/L."),
    },
]

# CATEGORÍAS CON UN INTERRUPTOR PROPIO, APAGADO POR DEFAULT (20/08/2026,
# a pedido de Maria: "por qué sigue saliendo esto" -- viendo Movimiento
# Rentable — canal Sugerencias con sus seis números editables, sin
# ningún aviso fuerte de que hoy no hacen nada porque el interruptor
# está en 0).
#
# Los números se SIGUEN mostrando editables a propósito (no se
# esconden): la idea original era dejarlos calibrados para el día que
# se prenda el canal, sin tener que volver a esta pantalla. Lo que
# faltaba era el aviso -- FICHAS_CATEGORIAS ya decía "HOY ESTÁ APAGADA"
# en el texto corrido, pero mezclado con el resto del párrafo no se
# nota lo suficiente. Este dict hace que el panel pinte un cartel
# aparte, imposible de no ver, arriba de la tabla de esa categoría.
#
# Formato: categoria -> clave bool que la gobierna. Agregar una
# categoría nueva acá es la única conexión que hace falta -- el panel
# arma el cartel solo a partir de esto.
CATEGORIA_INTERRUPTOR: dict[str, str] = {
    # 21/08/2026: "Movimiento Rentable — canal Sugerencias" salió del
    # panel (sus claves pasaron a OCULTAS), así que su interruptor ya no
    # tiene sección que gobernar. Si vuelve, se descomenta.
    # "Movimiento Rentable — canal Sugerencias": "MOV_SUGERENCIA_ACTIVA",
    # 29/08/2026: RACHA_ACTIVA está apagada por default desde el 28/08
    # (a pedido de Rubén, ver su comentario en la clave). El cartel
    # "sección apagada" avisa de un vistazo que esta categoría entera no
    # está sonando, sin tener que abrir la primera fila para leerlo.
    "Racha sencilla (canal externo — no es Jugador en Racha)": "RACHA_ACTIVA",
    # 29/08/2026: mismo cartel para "Jugador en Racha", atado a
    # RACHAAPI_ACTIVA -- es el interruptor MÁS arriba de la cascada (ver
    # su descripción). OJO: esto NO cubre que RACHAAPI_GRUPOS_ACTIVO esté
    # apagado con RACHAAPI_ACTIVA prendido -- ese es un escalón más dentro
    # de la cascada, no "la sección entera apagada", y el cartel de acá
    # solo admite UN interruptor por categoría. Ese caso lo explica la
    # descripción de RACHAAPI_GRUPOS_ACTIVO, no este cartel.
    "Jugador en Racha": "RACHAAPI_ACTIVA",
}

# ═════════════════════════════════════════════════════════════════════
#  NÚMEROS QUE GOBIERNAN UNA SECCIÓN PERO SE EDITAN EN OTRA (21/08/2026)
# ═════════════════════════════════════════════════════════════════════
#
# Pedido de Rubén, mirando la sección "Panel LIVE": "¿por qué no sale el
# grupo 1?". No salía porque el Grupo 1 no tiene ningún parámetro propio
# ahí -- usa VIVO_FTR_MIN/VIVO_ELO_MIN, que viven en "Partidos en Vivo
# (postSet 1)" a propósito, para no poder desincronizarse de los
# umbrales que deciden las alertas.
#
# La ficha de la categoría YA lo explicaba, pero la ficha quedó plegada
# en la reorganización del panel del 21/08 -- o sea que la respuesta
# estaba a un clic de distancia que nadie tiene motivo para dar. Una
# ausencia no se puede explicar en una tabla: donde no hay fila, no hay
# dónde escribir el motivo. Por eso esto es un cuadro APARTE, siempre
# visible, y no otro párrafo de la ficha.
#
# Formato: categoria -> lista de referencias. `claves` son las que se
# muestran con su VALOR VIGENTE (así el cuadro no miente si alguien las
# mueve) y `destino` es la categoría a la que salta el botón del panel.
#
# REGLA AL AGREGAR: esto es para números que DECIDEN algo de esta
# sección y se editan en otra. No es para "temas relacionados" -- si se
# llena de referencias sueltas deja de leerse, que es exactamente el
# problema que vino a resolver.
REFERENCIAS_CATEGORIAS: dict[str, list[dict]] = {
    "Panel LIVE (solo la vista, no alerta)": [
        {
            "titulo": "Grupo 1 de 'Ventaja leve' y 'Pasan el filtro' no se editan acá",
            "detalle": (
                "Los tres pisos de abajo son del GRUPO 2 solamente. El Grupo 1 (FTR y Elo "
                "por encima de lo establecido, sin exigir matchup) y la sección 'Pasan el "
                "filtro' usan los umbrales de las alertas, no números propios de la vista. "
                "Es a propósito: un número aparte acá podría desincronizarse de lo que "
                "realmente decide si una alerta suena, y la pestaña LIVE mostraría un verde "
                "que no se corresponde con nada."
            ),
            "destino": "Partidos en Vivo (postSet 1)",
            "claves": ["VIVO_FTR_MIN", "VIVO_ELO_MIN"],
            # Rótulo con el que se muestran en ESTA sección. El nombre
            # propio de la clave ("Prob. FTR mínima") describe su papel
            # en las alertas; acá hace falta decir qué papel cumple en
            # la vista, que es otro.
            "rotulo": "Grupo 1 de 'Ventaja leve'",
            # Qué papel cumple el número EN ESTA SECCIÓN. Su descripción
            # propia habla de cuándo suena una alerta, que es su papel en
            # la sección de origen y no acá -- leída en Panel LIVE sola,
            # confunde: parece que la pestaña manda alertas.
            "nota": ("En esta pestaña no manda ninguna alerta: es el piso del Grupo 1. "
                     "Un favorito que llega a los dos (FTR y Elo) cae en 'Ventaja leve' "
                     "si el matchup lo frenó, o en 'Pasan el filtro' si no."),
        },
    ],
    "Partidos en Vivo (postSet 1)": [
        {
            "titulo": "La muestra mínima se edita en Valor Pre-Partido",
            "detalle": (
                "El corte de muestra de esta estrategia no se decide con un número propio: "
                "se lee de la columna muestra_suficiente que YA quedó escrita en el fixture "
                "cuando Valor Pre-Partido lo reportó, con SUS partidos mínimos. Cambiarlos "
                "rige para los próximos fixtures que se reporten; no reevalúa los que ya "
                "están en la base."
            ),
            "destino": "Valor Pre-Partido",
            "claves": ["VPRE_MUESTRA_FTR", "VPRE_MUESTRA_ELO"],
            "rotulo": "Corte de muestra (corte 1)",
            "nota": ("Acá no se vuelve a medir: esta estrategia solo LEE la columna "
                     "muestra_suficiente que este número ya dejó escrita en el fixture."),
        },
    ],
}


def referencias_con_valores_vivos() -> dict[str, list[dict]]:
    """REFERENCIAS_CATEGORIAS con el valor VIGENTE de cada clave
    resuelto contra la base (21/08/2026). El panel pinta un cuadro por
    referencia, arriba de la tabla de parámetros, con un botón que salta
    a la sección donde ese número SÍ se edita.

    Se resuelve por llamada y no al importar: si mañana alguien mueve
    VIVO_FTR_MIN, el cuadro de 'Panel LIVE' lo refleja en el próximo GET.
    Una clave que no esté en REGISTRO se saltea en silencio en vez de
    romper la pestaña entera -- este cuadro es ayuda, no un dato crítico.
    """
    salida: dict[str, list[dict]] = {}
    # 21/08/2026, a raíz de "¿por qué la leyenda dice esa monda?": la
    # descripción de VIVO_FTR_MIN empezaba con "Igual que el de arriba".
    # En su sección de origen era cierto -- venía justo debajo de
    # VIVO_ELO_MIN. Mostrada en Panel LIVE, "el de arriba" pasó a ser
    # otra fila y la frase quedó apuntando a cualquier lado.
    #
    # Una descripción que se muestra en dos lugares NO puede ubicar nada
    # por posición. Esto avisa en los logs si alguien vuelve a referenciar
    # por posición una clave que viaja fuera de su sección. No corta el
    # panel: es un texto de ayuda, romper la pestaña sería peor que la
    # frase mal escrita.
    _POSICIONALES = ("el de arriba", "los de arriba", "el de abajo", "los de abajo",
                     "de arriba", "de abajo", "de más abajo", "acá abajo", "acá arriba",
                     "el anterior", "el siguiente", "más arriba")
    for cat, refs in REFERENCIAS_CATEGORIAS.items():
        items = []
        for ref in refs:
            claves = []
            for clave in ref.get("claves", []):
                if clave not in REGISTRO:
                    log.warning(f"[ConfigEstrategias] referencia a clave desconocida: {clave!r}")
                    continue
                # Viaja lo MISMO que un parámetro normal (etiqueta, tipo,
                # descripción, valor) porque el panel las pinta como una
                # fila más de la tabla, solo que sin control de edición
                # (21/08/2026, a pedido de Rubén: "que el grupo 1 también
                # se vea, pero que los números solo se puedan editar en la
                # otra sección"). Si acá viajara menos, la fila de solo
                # lectura se vería distinta de las editables por una razón
                # que no es la que importa.
                claves.append({
                    "clave": clave,
                    "etiqueta": REGISTRO[clave]["etiqueta"],
                    "descripcion": REGISTRO[clave]["descripcion"],
                    "tipo": REGISTRO[clave]["tipo"],
                    "valor": obtener(clave),
                })
                _desc = REGISTRO[clave]["descripcion"].lower()
                _malas = [f for f in _POSICIONALES if f in _desc]
                if _malas:
                    log.warning(
                        f"[ConfigEstrategias] {clave} se muestra en {cat!r} además de en su "
                        f"sección, pero su descripción ubica cosas por posición ({_malas}). "
                        f"Reescribirla nombrando el parámetro en vez de su lugar en la tabla."
                    )
            items.append({**ref, "claves": claves})
        salida[cat] = items
    return salida

# CLAVES QUE EL PANEL YA NO MUESTRA (20/08/2026).
#
# NO SE BORRAN. Dos motivos: (1) algún código todavía las lee, o las
# leyó hasta hace poco, y borrarlas del registro rompería esa lectura;
# (2) su valor guardado es el ORIGEN de las copias de arriba -- si
# desaparecen, una base que todavía no migró se queda sin de dónde
# copiar. Solo dejan de aparecer en Admin, que era el pedido: que cada
# número que se ve tenga dueño y sirva para algo.
#
# Las dos familias:
#   · las compartidas que se separaron hoy (FTR_ALERTA_*, etc.);
#   · los filtros F1-F6 de Movimiento Rentable, que dejaron de decidir
#     el 20/08 cuando la estrategia pasó a ser solo la regla del cruce.
CLAVES_OCULTAS: set[str] = {
    # Compartidas separadas hoy -- ahora cada estrategia tiene la suya
    "FTR_ALERTA_ELO_MIN", "FTR_ALERTA_FTR_MIN", "FTR_ALERTA_MATCHUP_MIN",
    "FTR_ALERTA_MATCHUP_BANDA", "FTR_ALERTA_CUOTA_MIN", "FTR_ALERTA_CUOTA_MAX",
    "FTR_MATCHUP_GATE", "FTR_SUPERFICIE_DESCONOCIDA",
    "FTR_ELO_UMBRAL_FTR", "FTR_ELO_UMBRAL_ELO",
    "FTR_MATCHUP_ESPERA_MIN", "FTR_MATCHUP_SIN_DATO",
    "MUESTRA_MINIMA_FTR", "MUESTRA_MINIMA_ELO",
    # Movimiento Rentable: F1-F6 y compañía, fuera del recorrido desde el
    # 20/08/2026. Quedan MOV_FLIP_* y MOV_SUGERENCIA_ACTIVA.
    "MOV_APERTURA_MIN", "MOV_ACTUAL_MIN", "MOV_ACTUAL_MAX",
    "MOV_CAIDA_PCT_MIN", "MOV_DELTA_PP_MIN", "MOV_PROB_FULLTENIS_MIN",
    "MOV_CONFIANZAS", "MOV_SUGERENCIA_CAIDA_MIN",
    "MUESTRA_MINIMA_MOVIMIENTO_VIEJO",
    # ── Canal Sugerencias de Movimiento Rentable (21/08/2026) ─────────
    # A pedido de Rubén: borrar esa sección del panel. Se ocultan, NO se
    # borran del REGISTRO, por dos motivos concretos:
    #
    # 1. La rama que las lee sigue en pie en routes_public.py, apagada
    #    por MOV_SUGERENCIA_ACTIVA=0. Sacarlas del REGISTRO haría que
    #    _cfg_float("MOV_SUGERENCIA_ACTIVA") reviente con KeyError la
    #    próxima vez que entre un price-drop -- o sea, romper el
    #    endpoint del bot para limpiar una pantalla.
    # 2. Son la calibración de un grupo de control medido (39 casos,
    #    -24,9% de ROI). Ese trabajo se pierde si se borra la fila.
    #
    # Siguen visibles en "Obsoletos" para poder auditarlas. Para
    # borrarlas de verdad hay que sacar antes la rama de sugerencias
    # automáticas de _evaluar_movimiento_rentable.
    # RACHAAPI_ESTADOS (23/08/2026): reemplazada por las tres casillas
    # RACHAAPI_AV_*. Se OCULTA en vez de borrarse porque es el origen de
    # migrar_estados_racha_api() -- si se borrara del REGISTRO, quien la
    # tuviera editada perdería su configuración al desplegar.
    # MSG_RACHAAPI: nombre viejo de RACHAAPI_MENSAJE, oculto pero vivo
    # para que un valor guardado antes del renombre se siga respetando.
    "MSG_RACHAAPI",
    "RACHAAPI_ESTADOS",
    "MOV_SUGERENCIA_ACTIVA", "MOV_SUGERENCIA_PROB_ELO_MIN",
    "MOV_SUGERENCIA_PROB_ELO_FUENTE", "MOV_SUGERENCIA_CAIDA_MAX",
    "MOV_SUGERENCIA_PROB_FTR_MIN", "MOV_SUGERENCIA_PROB_MATCHUP_MIN",
    # ── JUGADOR EN RACHA: SOLO SE USA TABLA MAESTRA (29/08/2026) ──────
    # A pedido de Maria: "quitemos lo que no usamos, dejemos claro solo
    # lo que vamos a dejar". 'Jugador en Racha' tenía TRES modos
    # compitiendo por el mismo interruptor (RACHAAPI_GRUPOS_ACTIVO) --
    # Tabla Maestra, Lista fija y Ranking dinámico -- y solo puede
    # ganar uno a la vez (ver el if/elif/elif de _quizas_avisar en
    # racha_api.py). El que se usa hoy es Tabla Maestra
    # (RACHAAPI_TABLA_ACTIVA=1 por default, tiene prioridad sobre los
    # otros dos). Los parámetros de los otros dos modos NO deciden nada
    # mientras eso siga así -- se ocultan para que la pantalla muestre
    # solo lo que de verdad hace algo.
    #
    # Se ocultan, NO se borran, por el mismo motivo de siempre: el
    # código que los lee sigue en pie (calcular_ranking_grupos,
    # grupos_fijos, sellar-corte...) y romper eso para limpiar una
    # pantalla es peor que el desorden. Si algún día se vuelve a
    # necesitar Lista fija o Ranking dinámico -- por ejemplo para
    # comparar contra la Tabla Maestra -- se sacan de acá y aparecen
    # de nuevo tal cual estaban, sin tocar código.
    #
    # Lista fija (5): el modo con el corte prospectivo manual.
    "RACHAAPI_GRUPOS_FIJOS", "RACHAAPI_CORTE_ID", "RACHAAPI_CORTE_MINIMO",
    "RACHAAPI_CORTE_UMBRAL", "RACHAAPI_CORTE_REACTIVADOS",
    # Ranking dinámico (3): el modo más viejo, de antes del 28/08.
    "RACHAAPI_GRUPOS_TOP", "RACHAAPI_GRUPOS_MUESTRA_MINIMA", "RACHAAPI_GRUPOS_TIPO",
    # Respaldo manual (13): las 9 casillas AV_* de estado/género/
    # superficie del MODO CLÁSICO -- solo se leen si RACHAAPI_GRUPOS_ACTIVO
    # está APAGADO, que es justo lo que NO tiene que pasar para que
    # suene la Tabla Maestra. Mientras eso se cumpla, estas 13 casillas
    # no tienen ningún efecto.
    "RACHAAPI_AV_PERDIENDO", "RACHAAPI_AV_PAREJO", "RACHAAPI_AV_GANANDO",
    "RACHAAPI_AV_HOMBRES", "RACHAAPI_AV_MUJERES", "RACHAAPI_AV_SIN_GENERO",
    "RACHAAPI_AV_HOLD_TIERRA", "RACHAAPI_AV_HOLD_DURA", "RACHAAPI_AV_HOLD_CESPED",
    "RACHAAPI_AV_BREAK_TIERRA", "RACHAAPI_AV_BREAK_DURA", "RACHAAPI_AV_BREAK_CESPED",
    "RACHAAPI_AV_SUP_DESCONOCIDA",
}

CLAVES_PESOS_FT_PROB = ("FT_PROB_PESO_TENNIS_DATA", "FT_PROB_PESO_CUOTA", "FT_PROB_PESO_UTR")
# Regla de «candidato» del aviso a Discord: clave -> (mínimo, máximo) permitidos.
CLAVES_CANDIDATO_FT_PROB = {"FT_PROB_CAND_PROB_MIN": (50, 99), "FT_PROB_CAND_CUOTA_MIN": (1.01, 20),
                            "FT_PROB_CAND_MIN_ANTES": (1, 180)}


def normalizar_pesos_ft_prob(td, cuota, utr):
    """(pesos en fracción | None, motivo | None). Acepta números o textos; reparte en proporción (suma 1)."""
    import math
    v = []
    for x in (td, cuota, utr):
        try:
            f = float(str(x).strip().replace(",", "."))
        except (TypeError, ValueError):
            return None, f"Peso no numérico: {x!r}"
        if not math.isfinite(f) or f < 0 or f > 100:
            return None, "Cada peso debe estar entre 0 y 100"
        v.append(f)
    total = sum(v)
    if total <= 0:
        return None, "Los tres pesos están en 0: al menos uno debe ser mayor que 0"
    return {"TENNIS_DATA": v[0] / total, "CUOTA": v[1] / total, "UTR": v[2] / total}, None


def set_valor(clave: str, valor: str, por: str = "") -> None:
    if clave not in REGISTRO:
        raise ValueError(f"Clave desconocida: {clave}")
    tipo = REGISTRO[clave]["tipo"]
    if tipo == "bool" and valor.strip().lower() not in ("0", "1", "true", "false", "si", "no"):
        raise ValueError(f"'{clave}' es booleano -- valor inválido: {valor!r}")
    if clave in CLAVES_PESOS_FT_PROB:
        valor = str(valor).strip().replace(",", ".")   # «33,5» = 33.5
        try:
            v = float(valor)
        except (TypeError, ValueError):
            raise ValueError(f"'{clave}' es numérico -- valor inválido: {valor!r}")
        if not (v == v and abs(v) != float("inf")) or v < 0 or v > 100:
            raise ValueError("El peso debe ser un número entre 0 y 100")
    if clave in CLAVES_CANDIDATO_FT_PROB:
        valor = str(valor).strip().replace(",", ".")
        try:
            v = float(valor)
        except (TypeError, ValueError):
            raise ValueError(f"'{clave}' es numérico -- valor inválido: {valor!r}")
        lo, hi = CLAVES_CANDIDATO_FT_PROB[clave]
        if not (v == v) or v < lo or v > hi:
            raise ValueError(f"'{clave}' debe estar entre {lo} y {hi}")
        if clave == "FT_PROB_CAND_MIN_ANTES" and v != int(v):
            raise ValueError("Los minutos deben ser un número entero")
    if tipo in ("float", "int"):
        try:
            float(valor)
        except (TypeError, ValueError):
            raise ValueError(f"'{clave}' es numérico -- valor inválido: {valor!r}")
    # 'str' y 'texto' no se validan: son libres a propósito. La
    # diferencia entre los dos es solo cómo los pinta el panel (una
    # línea vs. un cuadro de varias) y que 'texto' NO se recorta -- en
    # MSG_PREFIJO_SUGERENCIA el espacio del final es parte del valor.
    # 'texto' se guarda tal cual (sin .strip()); el resto se recorta.
    _guardar(clave, valor if tipo == "texto" else valor.strip(), por)


def _guardar(clave: str, valor: str, por: str) -> None:
    from production.api.deps import get_conn, PROD_DB_WRITE_LOCK
    conn = get_conn()
    try:
        _ensure_tabla(conn)
        with PROD_DB_WRITE_LOCK:
            conn.execute("""
                INSERT INTO ftr_config_estrategias (clave, valor, actualizado_en, actualizado_por)
                VALUES (?,?,?,?)
                ON CONFLICT(clave) DO UPDATE SET
                    valor=excluded.valor, actualizado_en=excluded.actualizado_en,
                    actualizado_por=excluded.actualizado_por
            """, (clave, valor, datetime.now(timezone.utc).isoformat(), por))
            conn.commit()
    finally:
        conn.close()
    # El worker que recibió el cambio lo ve en la consulta siguiente, sin
    # esperar el TTL (ver la nota de la caché arriba).
    invalidar_cache()
    log.info(f"[ConfigEstrategias] {por or '(?)'} puso {clave}={valor!r}")


def fichas_con_valores_vivos() -> dict[str, str]:
    """Copia de FICHAS_CATEGORIAS con los placeholders resueltos contra
    el valor VIGENTE de la base (20/08/2026, a pedido de Maria: 've el
    número acá'). Cubre:
      - 'Partidos en Vivo (postSet 1)': {muestra_ftr}/{muestra_elo},
        de VPRE_MUESTRA_FTR/ELO (ver la nota dentro de la ficha sobre
        por qué ese número no es editable en esta sección).
      - 'Panel LIVE (solo la vista, no alerta)': {vivo_ftr_min}/
        {vivo_ftr_max}/{vivo_elo_min} (de VIVO_FTR_MIN/MAX/ELO_MIN,
        Partidos en Vivo) y
        {leve_ftr}/{leve_elo}/{leve_mup} (de FTR_VIVO_UMBRAL_LEVE/
        _ELO/_MUP, esta misma sección) -- los números que gobiernan
        'Pasan el filtro' y los dos grupos de 'Ventaja leve' en la
        pestaña LIVE 🔴.
    Si mañana se editan esos parámetros, las fichas lo reflejan en el
    próximo GET, sin tocar código -- por eso NO se resuelve una sola
    vez al importar el módulo."""
    fichas = dict(FICHAS_CATEGORIAS)
    clave = "Partidos en Vivo (postSet 1)"
    if clave in fichas:
        fichas[clave] = fichas[clave].format(
            muestra_ftr=obtener_int("VPRE_MUESTRA_FTR"),
            muestra_elo=obtener_int("VPRE_MUESTRA_ELO"),
        )
    clave_live = "Panel LIVE (solo la vista, no alerta)"
    if clave_live in fichas:
        fichas[clave_live] = fichas[clave_live].format(
            vivo_ftr_min=f"{obtener_float('VIVO_FTR_MIN') * 100:.0f}%",
            vivo_ftr_max=f"{obtener_float('VIVO_FTR_MAX') * 100:.0f}%",
            vivo_elo_min=f"{obtener_float('VIVO_ELO_MIN') * 100:.0f}%",
            leve_ftr=f"{obtener_float('FTR_VIVO_UMBRAL_LEVE') * 100:.0f}%",
            leve_elo=f"{obtener_float('FTR_VIVO_UMBRAL_LEVE_ELO') * 100:.0f}%",
            leve_mup=f"{obtener_float('FTR_VIVO_UMBRAL_LEVE_MUP') * 100:.0f}%",
        )
    # Cuota Mal Puesta (07/10/2026): los umbrales vigentes en la ficha.
    clave_utrv = "UTR VALUE"
    if clave_utrv in fichas:
        fichas[clave_utrv] = fichas[clave_utrv].format(**{
            k: f"{obtener_float(k):g}" for k in ("UTRV_DELTA_MIN", "UTRV_CUOTA_MIN")})
    clave_utra = "UTR MARKET ANOMALY"
    if clave_utra in fichas:
        fichas[clave_utra] = fichas[clave_utra].format(**{
            k: f"{obtener_float(k):.2f}" for k in ("UTRA_DELTA_MIN", "UTRA_CUOTA_MIN")})
    if "UNDER UTR" in fichas:                      # v54i (15/10/2026)
        fichas["UNDER UTR"] = fichas["UNDER UTR"].format(
            UNDERUTR_DELTA_MIN=f"{obtener_float('UNDERUTR_DELTA_MIN'):.2f}",
            UNDERUTR_LINEA_MIN=f"{obtener_float('UNDERUTR_LINEA_MIN'):g}",
            UNDERUTR_CUOTA_MIN=f"{obtener_float('UNDERUTR_CUOTA_MIN'):.2f}")
    clave_cmp = "Cuota Mal Puesta"
    if clave_cmp in fichas:
        fichas[clave_cmp] = fichas[clave_cmp].format(**{
            k: f"{obtener_float(k):g}" for k in (
                "CMP_L10_AJUSTE_MAX_PP", "CMP_UMBRAL_ESTUDIO", "CMP_UMBRAL_CMP",
                "CMP_UMBRAL_PREMIUM")},
            CMP_DISCORD_FT_MIN_PCT=f"{obtener_float('CMP_DISCORD_FT_MIN') * 100:g}",
            CMP_FUERTE_FT_MIN_PCT=f"{obtener_float('CMP_FUERTE_FT_MIN') * 100:g}",
            CMP_DISCORD_VENTAJA_MIN_PP=f"{obtener_float('CMP_DISCORD_VENTAJA_MIN_PP'):g}",
            CMP_FUERTE_VENTAJA_MIN_PP=f"{obtener_float('CMP_FUERTE_VENTAJA_MIN_PP'):g}")
    return fichas


def interruptores_vigentes() -> dict[str, bool]:
    """{categoria: prendido?} para las categorías de CATEGORIA_INTERRUPTOR
    (20/08/2026) -- el panel usa esto para pintar el cartel de
    'sección apagada' arriba de la tabla correspondiente. Leído por
    consulta, igual que el resto de obtener_*() -- si Maria prende el
    interruptor, el cartel desaparece en el próximo GET sin tocar
    código."""
    return {cat: obtener_bool(clave) for cat, clave in CATEGORIA_INTERRUPTOR.items()}


def listar() -> list[dict]:
    """Todo lo que necesita el panel: catálogo + valor vigente de cada
    parámetro, agrupado por categoría."""
    salida = []
    for clave, meta in REGISTRO.items():
        valor = obtener(clave)
        salida.append({
            "clave": clave, "tipo": meta["tipo"], "categoria": meta["categoria"],
            "etiqueta": meta["etiqueta"], "descripcion": meta["descripcion"],
            "default": meta["default"], "valor": valor,
            "es_default": valor == meta["default"],
            # Desplegable con opciones fijas (20/08/2026, a pedido de
            # Maria: los campos "str" de destino se veían como texto
            # libre y no como algo elegible). None si el parámetro no
            # tiene un set cerrado de valores -- el panel sigue
            # mostrando el input de texto de siempre en ese caso.
            "opciones": meta.get("opciones"),
            # 20/08/2026: el panel las esconde, pero siguen viajando en la
            # respuesta -- así el que quiera auditar qué quedó guardado en
            # una clave vieja lo puede ver sin entrar a la base.
            "oculto": clave in CLAVES_OCULTAS,
            # Un texto que se edita en el modal de "Mensajes de las
            # alertas" (24/08/2026) aunque no tenga prefijo MSG_ -- por
            # ejemplo RACHAAPI_PAUSA_SI, que es parte del mensaje de
            # Racha detallada pero necesita vivir junto al resto de la
            # config de "Jugador en Racha" en el REGISTRO. El panel usa
            # esta bandera en vez de hardcodear cada nombre de clave.
            "es_mensaje": meta.get("es_mensaje", False),
            # Subtítulo para agrupar filas dentro de una misma categoría
            # (24/08/2026, a pedido de Maria: "ya existen tantos
            # recuadros que me pierdo"). Puramente visual -- el panel
            # inserta un renglón de subtítulo cada vez que cambia.
            "grupo": meta.get("grupo", ""),
            # La estrategia dueña de este número. El panel arma una
            # sección por cada una, en este orden.
            "orden_categoria": (ORDEN_CATEGORIAS.index(meta["categoria"])
                                if meta["categoria"] in ORDEN_CATEGORIAS
                                else len(ORDEN_CATEGORIAS)),
        })
    # Orden estable: por categoría según ORDEN_CATEGORIAS, y dentro de
    # cada una, el orden en que están escritas en REGISTRO (que agrupa
    # las de cada estrategia juntas).
    salida.sort(key=lambda p: p["orden_categoria"])
    return salida


# ═════════════════════════════════════════════════════════════════════
#  MIGRACIÓN: RACHAAPI_ESTADOS (texto) -> tres casillas (23/08/2026)
# ═════════════════════════════════════════════════════════════════════
_MIGRACION_ESTADOS_HECHA = False

_MAPA_ESTADOS_RACHA_API = {
    "perdiendo": "RACHAAPI_AV_PERDIENDO",
    "parejo": "RACHAAPI_AV_PAREJO",
    "ganando": "RACHAAPI_AV_GANANDO",
}


def migrar_estados_racha_api() -> None:
    """Pasa el valor guardado del campo de texto viejo a las casillas.

    La clave RACHAAPI_ESTADOS era un texto tipo "perdiendo,parejo". Si
    alguien la EDITÓ alguna vez, ese valor es una decisión suya y no se
    puede perder al cambiar la forma del control -- se traduce a las tres
    casillas.

    Solo se toca si hay fila guardada de la clave vieja Y ninguna de las
    nuevas. Si ya se tocaron las casillas, mandan ellas: lo último que
    hizo el usuario gana. Corre una vez por proceso.

    Un valor viejo mal tipeado ('ganado') simplemente no prende ninguna
    casilla, que es lo mismo que hacía antes -- no se intenta adivinar a
    cuál se refería.
    """
    global _MIGRACION_ESTADOS_HECHA
    if _MIGRACION_ESTADOS_HECHA:
        return
    _MIGRACION_ESTADOS_HECHA = True
    try:
        guardados = _valores_guardados()
        if "RACHAAPI_ESTADOS" not in guardados:
            return                                   # nunca se editó
        if any(k in guardados for k in _MAPA_ESTADOS_RACHA_API.values()):
            return                                   # ya se usan las casillas
        pedidos = {e.strip().lower()
                   for e in guardados["RACHAAPI_ESTADOS"].split(",") if e.strip()}
        for estado, clave in _MAPA_ESTADOS_RACHA_API.items():
            _guardar(clave, "1" if estado in pedidos else "0", "migracion-23/08/2026")
        # _guardar() ya invalida la caché en cada escritura.
        log.info(f"[ConfigEstrategias] RACHAAPI_ESTADOS={guardados['RACHAAPI_ESTADOS']!r} "
                 f"migrado a las tres casillas")
    except Exception as e:
        log.warning(f"[ConfigEstrategias] no se pudo migrar RACHAAPI_ESTADOS: {e}")


def estados_racha_api() -> set[str]:
    """Los estados que hacen sonar la racha detallada, según las casillas."""
    migrar_estados_racha_api()
    return {estado for estado, clave in _MAPA_ESTADOS_RACHA_API.items()
            if obtener_bool(clave)}
