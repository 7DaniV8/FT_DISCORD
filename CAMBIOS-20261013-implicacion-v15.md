# FT Discord v15 · 🏆 Implicación — formato definitivo (13/10/2026)

Solo `texto_implicacion` / `voz_implicacion` (ft_discord/textos.py). Los demás tipos no cambian.
- El texto de RankingFTR se publica tal cual. Ya **no** se agrega encabezado «EXPERIMENTAL» arriba ni el pie con
  la regla (IMP-R3) y el n.º de señal.
- Experimental: si el texto no trae la marca, se agrega al final `-# 🧪 Señal experimental · En estudio` (una sola vez).
- Prueba: pie discreto `-# 🧪 Prueba del circuito · no es una señal`.
- Voz: «Atención FullTennis. …»; un experimental termina con «Señal experimental, en estudio.» (nunca duplicado).

Archivos: ft_discord/textos.py, tests/test_implicacion_e2e.py.
