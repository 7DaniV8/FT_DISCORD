# CAMBIOS — 19/10/2026 — 🕒 Partido que cambió de hora + pie corto de CUOTA EXTREMA (FT_DISCORD v20)

Con FT_INTELLIGENCE v31 y RankingFTR §35 (seguimiento de horarios).

- Cuota Mal Puesta, UTR VALUE, Movimiento Rentable y UTR MARKET ANOMALY pre-partido: si el partido cambió de hora,
  antes del pie va «🕒 El partido se retrasó: era 10:00, ahora 13:00» (o «se adelantó»). Las horas son marcas de
  Discord: cada uno las ve en su zona. Menos de 5 min de diferencia no se dice. Sin cambio, el mensaje queda igual.
  Los minutos de «⏳ EMPIEZA EN ~N MIN» ya vienen contados con la hora vigente (FT Intelligence).
- Mensaje corto «🚨 FULLTENIS · CUOTA EXTREMA» (RankingFTR UTRA_MENSAJE_CORTO): cierra con «⏳ Comienza en 10 minutos.»
  (o «▶️ Ya empezó.»), sin «Detectada hace…». La voz no cambia.
- Pruebas: `tests/test_horarios_pie.py` (nuevo, 12). Todas las demás siguen igual.
- Ojo, de antes: `test_recorrido_completo.py` (3) y `test_implicacion_e2e.py` esperan el RankingFTR anterior al 17/10
  (Cuota Mal Puesta V3 mide contra el mercado sin margen; Implicación cambió de reglas). Con ese RankingFTR pasan;
  no tienen que ver con este cambio (fallan igual con el seguimiento de horarios apagado).

## ✂️ Pie corto para todos (19/10/2026, Rubén: «que todos los mensajes que llegan a Discord sean más cortos»)
- Cuota Mal Puesta, UTR VALUE, Movimiento Rentable y UTR MARKET ANOMALY pre-partido cierran con
  «⏳ Empieza en aproximadamente 10 minutos.» (o «▶️ Ya empezó.»), sin «Detectada hace…» y sin línea en blanco antes
  (reemplaza el «⏳ Comienza en 10 minutos.» de CUOTA EXTREMA de más arriba). La línea «🕒 El partido se retrasó…» sigue
  yendo justo antes del pie.
- Variable opcional `FT_DISCORD_PIE_CORTO=0` → el pie anterior («⏳ EMPIEZA EN ~10 MIN · Detectada hace…»).
- Anomaly EN VIVO («🔴 En vivo · cuota detectada…») no cambia.
- Pruebas: `test_horarios_pie.py` (14, con el ejemplo de Rubén completo); `test_bot.py` y `test_cuota_mal_puesta.py`
  fijan el pie anterior (FT_DISCORD_PIE_CORTO=0); `test_contrato_ft_intelligence.py` acepta los dos pies.
