# v16 — FT NEWS: canal de administradores y acuse de publicación (13/10/2026)

Pedido de Rubén:
- *"Las pruebas técnicas deben ir únicamente al canal de administradores."*
- *"No quiero que aparezca como enviada una alerta que FT Discord no haya confirmado."*

- **Variable nueva `DISCORD_CANAL_ADMIN_ID`**: el id de un canal de texto solo para
  administradores. Una señal con `datos.solo_admin` (la 🧪 PRUEBA de FT NEWS):
  - sale **solo** por ese canal;
  - **sin voz**, porque el canal de voz es el de la comunidad;
  - **sin encabezado personalizado**.

  **Sin esa variable, no sale por ningún lado**: nunca cae en el canal de la comunidad.
- **Acuse también para las tres de FT NEWS** (`NEWS_VALOR`, `NEWS_INTERESANTE`,
  `NEWS_NOTICIA`). Antes solo lo tenía Implicación. Tras cada una, FT Discord le dice a FT
  Intelligence si se publicó, en qué `canal` (`comunidad` / `admin`) y, si no, por qué (tipo
  apagado, pausa, error de Discord). FT Intelligence (v24) se lo pasa a FT NEWS.
- Las alertas reales de FT NEWS siguen yendo al canal de siempre, con texto y voz, sin cambios.
  Ningún otro tipo cambia.

Pruebas: `tests/test_news_admin.py` (9, nueva). `bash tests/correr_todo.sh`: todo igual que
antes de este cambio. `tests/test_recorrido_completo.py` sigue con **7 fallas que ya estaban**
en el código recibido: son de 🚨 UTR Market Anomaly (RankingFTR), no de FT NEWS, y no se tocaron.

**Pendiente de configurar en Railway:** crear el canal privado de administradores, invitar al
bot con permiso de ver y escribir, y poner `DISCORD_CANAL_ADMIN_ID` en este servicio.
