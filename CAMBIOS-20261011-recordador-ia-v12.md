# FT Discord v12 — 🩹 RECORDATORIO IA y 🔴 PARTIDO EN VIVO (11/10/2026)

Va con **RankingFTR v43** ("RECORDATORIOS INTELIGENTES CON IA") y **FT
Intelligence v20**. Para revisión antes de producción.

## Qué cambia

- Dos tipos nuevos, además del ⏰ `RECORDADOR` de siempre:
  - 🩹 `RECORDADOR_IA` — 5 minutos antes de la hora programada: observación del
    usuario + PROBABILIDAD FULLTENNIS BASE + conclusión.
  - 🔴 `RECORDADOR_VIVO` — al inicio real, corto.
- `ft_discord/textos.py`: `texto_recordador` / `voz_recordador` atienden los tres
  tipos. El **formato aprobado viene armado de RankingFTR y se publica tal
  cual** (sin "Favorito:", sin cuotas, sin "apostar", sin Elo/FTR); acá solo el
  pie (`_Seguimiento FullTennis · señal #N_`). Si llegara sin texto, un
  encabezado mínimo por tipo y nada inventado. La voz es la frase de RankingFTR
  (porcentajes ya en palabras) con los nombres como se dicen.
- `ft_discord/config.py`: `RECORDADOR_IA` y `RECORDADOR_VIVO` entran por defecto
  en texto y voz. Como siempre, Admin → Discord (grupo Avisos) manda sobre las
  variables de Railway: ahí se prenden/apagan por separado.
- Nada de esto suena hasta que en RankingFTR Admin ponga
  `RECORDADOR_IA_DISCORD=1`: hasta entonces RankingFTR manda el ⏰ de siempre
  (modo prueba).

## v12b (11/10/2026 10:27): prueba real de punta a punta

Rubén: "antes de dejarlo funcionando definitivamente, realiza una prueba real
de los dos avisos y confirma que llegan correctamente a Discord".
`tests/test_recordador_ia_e2e.py` (con `RANKINGFTR_REPO` ≥ v43b y
`FT_INTEL_REPO` ≥ v20) recorre el camino entero con el código real de los tres
servicios y el **anunciador real** de este bot: Admin → 🧪 Probar en RankingFTR
→ `/ft-intel/recordador` → FT Intelligence (señales RECORDADOR_IA /
RECORDADOR_VIVO + Matriz de Admin) → `/senales` → FT Discord escribe y dice los
dos avisos (y un aviso viejo anterior a v43 con el ⏰ de siempre, una sola vez)
→ FT Intelligence reporta → RankingFTR marca 📣 Discord ✔. **16 OK.** Se agregó
a `tests/correr_todo.sh`. Lo único que no se puede comprobar desde acá es el
canal de Discord en Railway: para eso está el botón 🧪 en la pestaña.

## Pruebas

- `tests/test_recordador.py`: **14 OK** (sección 4 nueva: defaults, formato IA
  tal cual + pie, nada inventado, voz "cincuenta y cinco por ciento", VIVO corto,
  sin texto → encabezado mínimo sin porcentajes).
- `tests/test_bot.py`: defaults actualizados.
- Suite `tests/correr_todo.sh`: todas OK salvo `test_recorrido_completo.py`,
  que **ya fallaba desde FT Intelligence ≥ v16** (nota del CAMBIOS v11). Se
  avanzó: el puente de prueba ahora pasa el cuerpo de los POST y siembra
  `fixtures_ftr_elo`, con lo que la revalidación FOTO 2 de Cuota Mal Puesta ya
  funciona en la prueba; lo que sigue fallando es la parte de UTR MARKET
  ANOMALY (reglas que cambiaron en RankingFTR después de escribirse esa prueba).
  No es de esta entrega; queda para revisar aparte.

## Archivos tocados (v12 sobre v11)

`ft_discord/textos.py`, `ft_discord/config.py`, `tests/test_recordador.py`,
`tests/test_bot.py`, `tests/test_recorrido_completo.py`,
`tests/test_recordador_ia_e2e.py` (v12b, nuevo), `tests/correr_todo.sh` (v12b),
este CAMBIOS.
