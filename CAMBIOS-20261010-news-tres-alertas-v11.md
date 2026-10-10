# CAMBIOS — 10/10/2026 — FT_DISCORD v11: las tres alertas de FT NEWS encendidas

Pedido de Rubén (09/10/2026 23:13): "activemos las tres alertas de FT NEWS en
Discord". Va con FT_NEWS v6 (formatos cortos, favorecido en MAYÚSCULAS con su
cuota, sin enlaces) y RankingFTR v38.

## Qué cambia

- `ft_discord/config.py`: `NEWS_VALOR` (🔥 validada y EXPERIMENTAL),
  `NEWS_INTERESANTE` (🔎) y `NEWS_NOTICIA` (📰) en `FT_DISCORD_TIPOS_TEXTO` y
  `FT_DISCORD_TIPOS_VOZ` por defecto. Si esas variables están puestas a mano en
  Railway, agregarles los tres (o borrarlas). Con configuración remota (Admin →
  Discord → grupo FT NEWS) manda Admin, como siempre.
- `ft_discord/anunciador.py`: una 🔥 cuya primera línea dice EXPERIMENTAL no
  pierde la etiqueta aunque Admin tenga un encabezado personalizado para
  `NEWS_VALOR` (Rubén: "mantener la etiqueta EXPERIMENTAL").
- El texto y la voz siguen viniendo armados por FT_NEWS; FT Discord solo pone la
  primera línea en negrita y el pie "🕐 partido … · investigación de noticias; no
  es una recomendación".

## Pruebas

`tests/test_news.py` (8 OK: las tres de fábrica; experimental conserva la
etiqueta), `tests/test_bot.py` (defaults). Suite TODO OK. El recorrido completo
FT NEWS → FT Intelligence → FT Discord con el anunciador real está en FT_NEWS
(`herramientas/recorrido_discord.py`, `EJEMPLOS-ALERTAS-20261010.md`).

Nota: `tests/test_recorrido_completo.py` (Cuota Mal Puesta con FT Intelligence
real) ya fallaba con FT Intelligence ≥ v16 (revalidación FOTO 2) antes de este
cambio; no es de FT NEWS. Queda para revisar aparte.

## Archivos tocados (v11 sobre v10)

- `ft_discord/config.py`, `ft_discord/anunciador.py`, `tests/test_news.py`,
  `tests/test_bot.py`, este archivo
