# CAMBIOS — 08/10/2026 — 📰 FT NEWS INTELLIGENCE (FT Discord v9)

Tres tipos nuevos: `NEWS_VALOR` (🔥 posible valor por noticias; texto y voz
de fábrica, en el acto), `NEWS_INTERESANTE` (🔎) y `NEWS_NOTICIA` (📰),
estos dos apagados de fábrica y encendibles desde Admin → Discord. El
texto viene armado por FT_NEWS (plantilla fija, sin probabilidades); acá
primera línea en negrita y pie con la hora del partido. Voz: "Atención
FullTennis. Noticia." + la frase de FT_NEWS.

- Editado: `ft_discord/config.py` (`_TODOS`, `_VOZ`), `ft_discord/textos.py`
  (`texto_news`, voz, títulos), `tests/test_bot.py` (defaults), `tests/correr_todo.sh`.
- Nuevo: `tests/test_news.py`.
- OJO: si `FT_DISCORD_TIPOS_TEXTO` / `FT_DISCORD_TIPOS_VOZ` están puestas en
  Railway, agregarles `NEWS_VALOR`.
