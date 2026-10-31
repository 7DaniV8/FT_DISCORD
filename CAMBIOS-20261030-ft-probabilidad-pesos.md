# 30/10/2026 · Pesos editables de FT PROBABILIDAD desde el Admin

- `production/api/config_estrategias.py`: 3 campos en Admin → Estrategias → FT PROBABILIDAD:
  `FT_PROB_PESO_TENNIS_DATA` (60), `FT_PROB_PESO_CUOTA` (30), `FT_PROB_PESO_UTR` (10), en %, 0 a 100.
  No hace falta que sumen 100: si no suman, se reparten EN PROPORCIÓN y la pestaña muestra los pesos reales en uso.
  `set_valor` rechaza valores no numéricos o fuera de 0-100 (acepta coma decimal).
- `production/api/routes_ft_intel.py`: `/ft-intel/probabilidad/config` entrega `pesos` (fracciones normalizadas, o `null`
  si no hay pesos válidos, p. ej. los tres en 0), `pesos_pct` (lo escrito en el Admin), `pesos_validos`, `pesos_motivo`.
  Sigue siendo solo lectura (una consulta por 60 s).
- FT_PROBABILIDAD: usa los pesos vigentes. Unos pesos nuevos solo se adoptan cuando se ven IGUALES en dos lecturas
  seguidas (≈1-2 min), así editar los tres campos uno a uno no deja pesos a medias; la primera lectura tras arrancar
  se adopta de inmediato. Si la configuración no es válida conserva los últimos válidos y avisa (`pesos_aviso`).
  Cada informe guarda `pesos_usados`. Con pesos distintos de 60/30/10 el modelo histórico validado NO se usa.
- Pestaña 🎲 FT PROBABILIDAD: muestra los pesos en uso y el aviso; el modal explica el cálculo (fórmula y fila Resultado).
- Pruebas: `tests/test_ft_probabilidad_pesos.py` y 2 pruebas en FT_PROBABILIDAD/tests.


## Resultados para medir el modelo (30/10/2026)
- `production/api/routes_ft_intel.py`: nueva ruta de SOLO LECTURA `GET /ft-intel/probabilidad/resultados?dias=5` con los partidos
  terminados (ganador, perdedor, fecha, marcador) desde `match_log`. Un partido por pareja y día; excluye retiros, walkovers,
  abandonos y marcadores truncados. Mismo secreto interno que el resto de `/ft-intel`.
- FT_PROBABILIDAD consulta esa ruta cada 30 min y etiqueta sus predicciones PRE; no hay que configurar nada nuevo.
- Prueba: `tests/test_ft_probabilidad_resultados.py`.

## Aviso a Discord de los candidatos (30/10/2026)
- Regla editable en Admin → Estrategias → FT PROBABILIDAD: `FT_PROB_CAND_PROB_MIN` (60 %), `FT_PROB_CAND_CUOTA_MIN` (1.70),
  `FT_PROB_CAND_MIN_ANTES` (10 min). Candidato = probabilidad del modelo > mínimo Y cuota ACTUAL (fixtures_ftr_elo, no la de
  cuando se calculó; si tiene más de 3 h no se usa) > mínimo. Valores con rango validado.
- `production/engines/ft_prob_candidatos.py` (nuevo): la regla, las dos etapas (DETECTADO / INICIO) y el texto y la voz.
- `GET /ft-intel/probabilidad/candidatos` (solo lectura, sin estado): consulta el servicio por la red privada y entrega los
  candidatos con su etapa. Apagado o sin servicio: lista vacía con el motivo.
- Admin → Discord: dos tipos nuevos (`PROB_CANDIDATO` texto sí/voz no; `PROB_CANDIDATO_INICIO` texto y voz). Total 23 tipos.
- FT_PROBABILIDAD: `/resumen` expone `id_ftr` (el id del fixture en RankingFTR) para leer la cuota actual.
- Pruebas: `tests/test_ft_prob_candidatos.py`; `tests/test_discord_config.py` ahora espera 23 tipos.

## Dashboard + historial de FT PROBABILIDAD (candidatos enviados a Discord)
- Misma sección que UTR VALUE al final de la pestaña 🎲 FT PROBABILIDAD: tarjetas (total, ganadas, perdidas, % acierto,
  unidades, ROI, yield, cuota promedio, rachas, ganancia) e Historial (Enviado, Partido, Selección, Torneo, Cuota,
  Resultado, Marcador, Unidades).
- `production/engines/ft_prob_registro.py` (NUEVO): tabla `ft_prob_candidatos_registro`. Una fila por partido y jugador,
  con la cuota y la probabilidad del primer envío. Se llena desde GET /ft-intel/probabilidad/candidatos y se cierra
  sola contra match_log (cada 10 min como máximo, 30 min después del inicio; retiro/W.O. = push).
- «Enviado a Discord» = en ese momento Admin → Discord tenía encendido el texto o la voz de ese tipo y la pausa apagada.
  Es una aproximación: FT Discord aún no devuelve acuse para estos tipos.
- `dashboard_estrategia.ft_probabilidad`, ruta `/research/api/dashboard/ft-probabilidad`, clave `ftprob` en
  dashboard_estrategia.js, `data-dashboard="ftprob"` en ft_probabilidad.html, 🗑 ocultar filas (admin).
- Pruebas: tests/test_ft_prob_registro.py (nuevo) y ampliación de tests/test_ft_prob_candidatos.py.
