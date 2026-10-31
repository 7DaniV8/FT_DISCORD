# 57 · 🎲 Pestaña FT PROBABILIDAD (30/10/2026)

**Pedido:** «¿en dónde veo los partidos que está analizando, la probabilidad que le dio y demás?» → opción A: una pestaña nueva.

- Pestaña **🎲 FT PROBABILIDAD** (`ver_tab_ftprobabilidad`, de fábrica **solo admin**, sin grupo, como el Padrón UTR). Tabla con hora de inicio, torneo, partido, probabilidad de cada jugador, componentes del primer jugador (Tennis Data · UTR · Cuota sin margen), estado y enlace al reporte de cada partido. Arriba: motor encendido/apagado, consultas a Tennis Data de hoy, bloqueo 403/429, aprendizaje y último error. Se actualiza sola cada 30 s mientras la pestaña está abierta.
- **Solo lectura.** `research/api/routes_ft_probabilidad.py` le pregunta a FT_PROBABILIDAD por la red privada de Railway: `GET /research/api/ft-probabilidad/resumen` y `.../reporte/{id}`. Sin escrituras, sin tareas de fondo, sin tablas nuevas.
- **Límites:** solo se llama cuando un admin abre la pestaña; 2 s para conectar y 4 s en total; copia de 10 s (como mucho una pregunta cada 10 s al servicio); si el servicio no responde o falta la variable, la pestaña lo dice y nada más cambia. El secreto (`FTR_INTERNAL_SECRET`) solo viaja en una cabecera hacia el servicio, nunca al navegador.
- El reporte se sirve con CSP `default-src 'none'` (es HTML sin scripts) y sin permitir marcos.
- **Variable nueva en RankingFTR:** `FT_PROBABILIDAD_URL` (p. ej. `http://NOMBRE-DEL-SERVICIO.railway.internal:8080`). Sin ella, la pestaña muestra «Falta la variable FT_PROBABILIDAD_URL».
- Diferencia con el BOT BUSCADOR: allí RankingFTR nunca llama al otro servicio; aquí sí, por pedido, con los límites de arriba.

Archivos: research/api/routes_ft_probabilidad.py (nuevo), research/web/templates/tabs/ft_probabilidad.html (nuevo), tests/test_ft_probabilidad_tab.py (nuevo), production/api/app.py, production/api/permisos.py, research/web/templates/research.html.
Para quitarla: esos tres nuevos, el include en app.py, la pestaña en research.html y las 5 líneas de permisos.py.
