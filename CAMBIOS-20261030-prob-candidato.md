# 🎲 FT PROBABILIDAD en Discord (30/10/2026)

Dos tipos nuevos de señal, con texto y voz que arma RankingFTR (se publican tal cual; el encabezado en negrita):
- `PROB_CANDIDATO`: el partido cumple la regla (probabilidad > mínimo y cuota actual > mínimo, del Admin). De fábrica: texto, sin voz.
- `PROB_CANDIDATO_INICIO`: minutos antes del inicio, solo si con la cuota de ese momento todavía cumple. De fábrica: texto y voz.

Se gobiernan en Admin → Discord (encender/apagar texto y voz, encabezado, frase con la que arranca la voz). Sin configuración remota
valen los defaults de `config.py`. Si en Railway quedó cargada una lista propia en `FT_DISCORD_TIPOS_TEXTO` / `FT_DISCORD_TIPOS_VOZ`,
hay que añadirle los dos tipos (o borrar la variable).
- `ft_discord/textos.py`: títulos y ramas de texto/voz. `ft_discord/config.py`: tipos por defecto.
- Pruebas: `tests/test_prob_candidato.py` (14), `tests/test_bot.py` (tipos por defecto), `tests/correr_todo.sh`.
