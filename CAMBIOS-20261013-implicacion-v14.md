# FT Discord v14 — 🏆 Implicación: 🧪 EXPERIMENTAL — EN ESTUDIO y acuse (13/10/2026)

Va con **RankingFTR v47** y **FT Intelligence v22**.

- **Experimentales.** Un aviso experimental se publica con «🧪 EXPERIMENTAL — EN ESTUDIO · no es un pick validado»
  arriba (si faltara, se agrega) y con el pie `_🧪 Experimental · en estudio · IMP-R3 · señal #N_`. Un encabezado
  de Admin → Discord no pisa esa línea (regla de v11). La voz empieza con «Señal experimental, en estudio».
- **Acuse.** Después de anunciar una señal 🏆 IMPLICACION, el bot le confirma a FT Intelligence
  (`POST /senales/acuse`):
  - si el texto se publicó en el canal;
  - si la voz se encoló;
  - y, si la voz no salió, el motivo (horario de silencio, cola llena o sin canal de voz).

  Si FT Intelligence no lo recibe, el anuncio no se frena.
- Los demás tipos no cambian: ni texto, ni voz, ni horario, ni configuración.
- Pruebas:
  - `tests/test_implicacion_e2e.py`: 22 OK, de punta a punta con el código real de los tres servicios
    (prueba → acuse → encendido automático → IMP-R3 experimental con texto y voz → sin duplicados → resultado);
  - `test_bot`: 69 OK;
  - el resto de la suite igual.
  - `test_recorrido_completo` sigue con las mismas 7 fallas que ya tenía con la v44g: son de UTR Market Anomaly
    y no tienen relación con esto.
