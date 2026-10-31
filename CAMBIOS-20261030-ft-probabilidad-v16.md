# 56 · FT PROBABILIDAD V16 integrada al paquete (30/10/2026)

- Único cambio en RankingFTR: interruptor `FT_PROBABILIDAD_ACTIVO` (Admin → Estrategias → FT PROBABILIDAD, apagado por defecto) en `production/api/config_estrategias.py` y endpoint de solo lectura `GET /ft-intel/probabilidad/config` en `production/api/routes_ft_intel.py`. Solo líneas añadidas; ninguna estrategia cambia.
- El motor vive en la carpeta hermana `FT_PROBABILIDAD/` (servicio aparte en Railway, volumen propio en `/data`, cero escrituras en las bases de RankingFTR). Ver `FT_PROBABILIDAD/README_RAILWAY.md`.
- Base de RankingFTR: el ZIP más reciente (balance por día, mini calculadora de arbitraje en Balance).
- Pendiente conocido: `routes_arbitraje.py`, `tabs/arbitraje.html` y `tests/test_arbitraje.py` descritos en el punto 55 no venían en el ZIP; la pestaña 🧮 Arbitraje no existe todavía.
- Pendiente: confirmar con un `/ft-intel/fixtures` real que `torneo` contenga ATP/Challenger (filtro de FT_PROBABILIDAD).
