# 🩹 MTO en vivo: MTO anteriores (22/10/2026)

Con `datos.mto_previos` (FT Intelligence, mismo día):
- texto: «🕒 **2 MTO** en las últimas 4 semanas · el último **hace 3 días**» (o «🕒 Último MTO **hace 194 días**»),
  después de la cuota del rival;
- voz: «… Rival con cuota 2.97. Pidió 2 tiempos médicos en las últimas 4 semanas, el último hace 3 días.»
Sin el dato, el aviso queda igual que antes. Test: tests/test_mto_vivo.py.

## Cuotas pre-partido y el favorito (mismo día)

Rubén: «¿podríamos sumarle cuáles eran las cuotas pre partido para saber quién era la favorita?»
Con `datos.cuota_pre_jugador`, `cuota_pre_rival`, `favorito_pre` (FT Intelligence):
- texto: «📊 Pre-partido: Kovacs L **1.65** · Sziklai E **2.20** → favorita: **Kovacs L**» (favorito/favorita según
  género; «→ parejas» si son iguales);
- la ⭐ de la línea del partido marca al favorito PRE-PARTIDO (antes: el de la cuota en vivo al momento del MTO);
- voz: «… Antes del partido era la favorita, con cuota 1.65.» / «… el favorito era su rival, X, con cuota 1.30.»

## Formato de Rubén y el momento del pedido (mismo día)

- 📊 Pre-partido: una línea por jugador («• Michiru Furuya **1.65**»); sin «→ favorita» (la ⭐ ya la marca).
- 🩹 SOLO si lo pidió entre puntos (Rubén: «no, solo si es entre puntos»; `datos.momento_mto == "entre_puntos"`):
  «🩹 Lo pidió **entre puntos** (game 15-30)» o «… en el tie-break (3-2)». Voz: «Lo pidió entre puntos, en medio de un
  game.» Cambio de lado, fin de set o entre games: no se dice nada (FT Intelligence igual guarda el dato).
- Orden: partido · pre-partido · cuota del rival · 🩹 momento · 🕒 MTO previos · 📺 video.

## Voz corta (mismo día)

Rubén: «quiero que el bot solo lea una pequeña parte del mensaje: tiempo médico solicitado por tal, la cuota del rival
y si lo pidió entre puntos». La voz dice SOLO: «Atención FullTennis. Tiempo médico solicitado por X. Rival con cuota
1.98. Lo pidió entre puntos. Partido con video.» (cada parte solo si hay dato / si fue entre puntos / si tiene 📺).
Las cuotas pre-partido y los MTO anteriores ya no se leen; siguen en el texto del canal.
