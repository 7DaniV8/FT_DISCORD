# CAMBIOS — 09/10/2026 — Mensajes en el formato de Rubén y aviso minutos antes (RankingFTR v32 · FT_INTELLIGENCE v15 · FT_DISCORD v10)

Pedido de Rubén (09/10/2026, 00:21): "quiero que los mensajes se vean así y
recuerda que siempre deben sonar minutos antes de que empiece el juego".
Orden de subida: FT_DISCORD → FT_INTELLIGENCE → RankingFTR (cualquier orden
funciona; así los pies nuevos ya están cuando lleguen los datos nuevos).

## Minutos antes (FT_INTELLIGENCE v15, `al_empezar.py`)

Cuota Mal Puesta, UTR VALUE y UTR MARKET ANOMALY pre-partido se anuncian
**`FT_INTEL_AVISO_MINUTOS_ANTES` (10) minutos antes de la hora programada**
del partido, sin esperar al feed en vivo. Si el feed lo ve empezar antes
(adelantado), se anuncia al verlo. Si la hora pasa sin verse en vivo
(retrasado/cancelado), sigue mandando el feed, como hasta ahora: por reloj
nunca se dice "ya empezó". Un partido se anuncia una sola vez. La señal lleva
`momento` (ANTES / EN_JUEGO) y `minutos`. Anomaly en vivo sigue en el acto.
Variable nueva (opcional): `FT_INTEL_AVISO_MINUTOS_ANTES=10` (0 = solo al empezar).

## Pies y voz (FT_DISCORD v10, `textos.py`)

- Minutos antes: `⏳ **EMPIEZA EN ~9 MIN** · Detectada hace 3 h`; voz "En nueve
  minutos, Fakih contra Pace. …".
- Al empezar: `▶️ **EN JUEGO** · Detectada hace 3 h`; voz "Empezó …".
- Anomaly en vivo: `🔴 **En vivo** · cuota detectada hace 2 min` (igual).

## Mensajes (RankingFTR v32)

**🔥 Cuota Mal Puesta (V2.2)**
```
🔥 FULLTENIS · CUOTA MAL PUESTA
🎾 ⭐ Kate Fakih vs Francesca Pace
🏆 W15 Monastir · 10/10 14:30
👉 Pick: Kate Fakih @ 1.95
📈 FullTenis 63% · Mercado 51.3%
🚨 Ventaja: +11.7 pp
🧠 Por qué: Base 61% + carga favorable (+2 pp)
✅ Factor que activó la señal: CARGA
📊 Elo y FTR coinciden en el favoritismo.
```
"Por qué" lista los factores ≠ 0 con signo y palabra (favorable / en contra /
MTO del rival / MTO propio); "Factor que activó" nombra los que están a favor
(OPOSICIÓN + CARGA + MTO). 💎 PREMIUM se agrega al final cuando corresponde.

**📈 UTR Value**
```
📈 FULLTENIS · UTR VALUE

🎾 ⭐ Kate Fakih vs Francesca Pace
🏆 W15 Monastir

📊 UTR general: 9.84 vs 9.12 · Δ +0.72
🏟 UTR tierra: 9.90 vs 9.05 · Δ +0.85

💰 Pick: Kate Fakih @ 1.95
📉 Mercado: 50% (sin margen)

🧠 ¿Por qué se activó?
✅ Fakih tiene mejor UTR general.
✅ En tierra su ventaja aumenta a +0.85.
✅ Cuota atractiva para su superioridad UTR.
✅ Datos UTR actualizados y verificados.
```
(La frase UTRV_FRASE ya no se agrega; la reemplaza la lista ✅.)

**🚨 UTR Market Anomaly**
```
🚨 FULLTENIS · UTR MARKET ANOMALY

🎾 ⭐ Kate Fakih vs Francesca Pace
🏆 W15 Monastir · Tierra

📊 UTR: 9.84 vs 9.12 · Δ +0.72

💰 Cuota PRE: 1.70
🚨 Cuota ACTUAL: 4.20
🔴 Marcador: 3-6, 1-2

🧠 ¿Por qué se activó?
✅ Kate Fakih tiene UTR ligeramente superior (+0.72).
✅ Cuota actual extrema: 4.20.
✅ Supera el umbral de activación (4.00).
✅ Subió desde 1.70.

⚠️ JUGADORA CON UTR SUPERIOR A CUOTA EXTREMA
```
Grado según la diferencia: "similar" (< 0.25, y entonces "…y es quien paga
más"), "ligeramente superior" (< 1.0), "superior". JUGADOR/JUGADORA según el
sexo. Pre-partido: "⏳ Pre-partido" en vez del marcador.

**📰 FT NEWS**: ya en el formato pedido desde FT_NEWS v2 (anoche).

## Pruebas

- RankingFTR: `test_cmp_v2.py`, `test_utr_value.py`, `test_utr_anomalia.py`,
  `test_oddspapi_utr.py`, `test_oddspapi_vivo.py` actualizados; `correr_todo.sh`
  en orden; 46 invariantes.
- FT_INTELLIGENCE: `test_cuota_mal_puesta.py` +3 checks (ventana de minutos
  antes, no se repite al empezar); suite en verde.
- FT_DISCORD: `test_cuota_mal_puesta.py` +3 checks (pie y voz "EMPIEZA EN");
  suite en verde.

## Archivos tocados (v32 sobre v31)

RankingFTR: shared/version.py · production/engines/cuota_mal_puesta.py ·
production/engines/utr_value.py · production/engines/utr_anomalia.py ·
tests/test_cmp_v2.py · tests/test_utr_value.py · tests/test_utr_anomalia.py ·
tests/test_oddspapi_utr.py · tests/test_oddspapi_vivo.py · CAMBIOS-20261009-mensajes-rubn-v32.md
FT_INTELLIGENCE: ft_intelligence/config.py · ft_intelligence/al_empezar.py · tests/test_cuota_mal_puesta.py
FT_DISCORD: ft_discord/textos.py · tests/test_cuota_mal_puesta.py
