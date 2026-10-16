# FT Discord v19 · 15/10/2026 · 🔥 UNDER UTR

Va con RankingFTR v54i y FT Intelligence v30.

* Tipo nuevo `UNDER_UTR`, encendido de fábrica (texto y voz) y gobernado por Admin → Discord.
* Texto: el de RankingFTR **tal cual** (formato del pedido de Rubén), con el encabezado en negrita. No se agrega ⭐
  favorito ni pie de hora: el mensaje ya dice «📍 Marcador: 0-0 · ⏱️ sin empezar».
* Solo pre-partido (RankingFTR v54i). Voz: «Atención FullTennis. Antes de empezar, A contra B. Under UTR. Diferencia de UTR, uno punto ochenta y cinco. Under
  dieciocho y medio, cuota uno punto setenta y seis.»
* Devuelve el ACUSE (texto publicado / voz encolada), como ▼ Under y 📉 Movimiento Rentable.

Railway: si todavía existen `FT_DISCORD_TIPOS_TEXTO` / `FT_DISCORD_TIPOS_VOZ`, agregarles `UNDER_UTR`, o mejor
borrarlas: el valor de fábrica ya lo incluye y manda Admin.

Archivos: `ft_discord/config.py`, `ft_discord/anunciador.py`, `ft_discord/textos.py`, `tests/test_bot.py`,
`tests/test_news_admin.py`, este archivo.

Pruebas: `bash tests/correr_todo.sh` → «TODO OK».
