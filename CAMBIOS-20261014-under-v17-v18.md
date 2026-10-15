# FT Discord v17 · 14/10/2026 · ▼ Under en Vivo con voz y acuse

Rubén: «se supone que los Under deben sonar en Discord, pero ninguno ha sonado».

* **Voz.** El Under no tenía frase y se decía solo «Atención FullTennis.». Ahora dice: «Atención FullTennis. Under en
  vivo. Bairo contra Neimanis. Under diecisiete y medio, cuota uno punto setenta y dos.»
* **Texto.** El encabezado pasa a «▼ FULLTENNIS · UNDER EN VIVO» y suma el torneo (el encabezado de Admin → Discord
  sigue mandando).
* **Encendido de fábrica.** `UNDER_PICK` entra en los tipos de texto y voz por defecto. Si en Railway existen
  `FT_DISCORD_TIPOS_TEXTO` / `FT_DISCORD_TIPOS_VOZ`, hay que agregarle `UNDER_PICK` a mano; normalmente manda Admin →
  Discord.
* **Acuse.** El Under devuelve qué pasó (texto publicado / voz encolada / motivo) a FT Intelligence, y de ahí a
  RankingFTR. La pestaña ▼ Under en Vivo lo muestra en cada fila.
* **Tests.** `test_recorrido_completo.py` tenía dos verificaciones viejas que fallaban sin relación con este cambio:
  * la hora del marcador era la del arranque del test, y en una máquina lenta quedaba «marcador viejo»;
  * el texto esperado era «marcador 0-0», pero el formato actual es «🔴 Marcador: 0-0».

## Archivos

Nuevo:
* este archivo

Modificados:
* `ft_discord/textos.py`
* `ft_discord/anunciador.py`
* `ft_discord/config.py`
* `tests/test_bot.py`
* `tests/test_news_admin.py`
* `tests/test_recorrido_completo.py`

Pruebas: `RANKINGFTR_REPO=… FT_INTEL_REPO=… bash tests/correr_todo.sh` → TODO OK.

---

# v18 · 15/10/2026 · 📉 Movimiento Rentable

* Tipo nuevo `MOVIMIENTO_RENTABLE`, tratado igual que Cuota Mal Puesta y UTR VALUE:
  * el texto de RankingFTR con el encabezado en negrita;
  * pie «⏳ EMPIEZA EN ~N MIN» o «▶️ EN JUEGO» con «Detectada hace X»;
  * voz «En nueve minutos, A contra B. Movimiento Rentable a favor de …».
* Encendido de fábrica (texto y voz) y con acuse. Si en Railway existen `FT_DISCORD_TIPOS_TEXTO` /
  `FT_DISCORD_TIPOS_VOZ`, hay que agregarles `MOVIMIENTO_RENTABLE` (y `UNDER_PICK`).

Archivos:
* `ft_discord/textos.py`
* `ft_discord/anunciador.py`
* `ft_discord/config.py`
* `tests/test_bot.py`
* `tests/test_news_admin.py`
