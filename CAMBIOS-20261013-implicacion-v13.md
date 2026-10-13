# FT Discord v13 — 🏆 IMPLICACIÓN DE TORNEO (13/10/2026)

Va con **RankingFTR v45** y **FT Intelligence v21**. Preparado y probado; **no suena nada
hasta que Rubén autorice** (en RankingFTR `IMP_DISCORD_ACTIVO` está en 0 de fábrica).

## Qué cambia

- Tipo nuevo `IMPLICACION` (🏆): favorito PRE de Pinnacle que pierde el primer set y
  defiende puntos en el torneo. Se dice **en el acto** (el partido ya está en juego).
- `ft_discord/textos.py`: `texto_implicacion` / `voz_implicacion`. El texto viene armado de
  RankingFTR y se publica tal cual; acá solo el pie (`_🔴 En vivo · IMP-R1 · señal #N_`).
  La voz arranca con "Atención FullTennis." y dice los nombres como se pronuncian.
- **Pruebas del circuito**: llegan con la primera línea `🧪 PRUEBA DEL CIRCUITO — no es una
  señal`, con el pie `_🧪 Prueba del circuito, no es una señal_` y la voz empieza con "Prueba
  del circuito". `anunciador._personalizar` no deja que un encabezado de Admin pise esa
  línea (igual que la etiqueta EXPERIMENTAL de FT NEWS).
- `ft_discord/config.py`: `IMPLICACION` entra en texto y voz por defecto. Admin → Discord
  (grupo Estrategias) manda sobre las variables de Railway, como siempre.

## Qué NUNCA llega

Los 🧪 **candidatos experimentales** no salen de RankingFTR (el feed solo trae picks de reglas
**validadas** con `IMP_DISCORD_ACTIVO = 1`, más las pruebas). FT Intelligence tiene además un
segundo candado: si algo que no es `validado` ni `prueba` llegara, lo guarda y no lo dice.

## Prueba de punta a punta

`tests/test_implicacion_e2e.py` (con `RANKINGFTR_REPO` ≥ v45 y `FT_INTEL_REPO` ≥ v21) usa el
código real de los tres servicios: motor de picks real → `/ft-intel/implicacion` real →
FT Intelligence → `/senales` real → anunciador real → `/ft-intel/implicacion/dicho`.
Resultado: **16 OK, 0 fallas**. El experimental y el validado con Discord apagado no salen;
la prueba y el validado con Discord prendido salen una vez, con el formato pedido, y vuelven
marcados como dichos a RankingFTR. Apagar `IMP_DISCORD_ACTIVO` corta el feed en el acto.
