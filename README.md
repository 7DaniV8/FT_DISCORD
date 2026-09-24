# FT Discord

Anuncia en Discord las señales de **FT Intelligence**: por escrito en un
canal de texto y, si se configura, **en voz alta** en un canal de voz donde
queda conectado las 24 horas, como un miembro más. Si no hay señales, se
queda callado.

```
FullTenis ──► FT Intelligence (decide QUÉ decir) ──/senales──► FT Discord (decide CÓMO)
                                                                 ├─► canal de texto
                                                                 └─► canal de voz (Google TTS)
```

No calcula nada ni lee FullTenis: todo lo que dice viene de FT Intelligence.
Es un servicio aparte a propósito: un problema con el audio nunca puede
frenar la captura de datos de FT Intelligence.

## Qué anuncia

Anuncia **una ficha por jugador vigilado**: la revisión de cuota que arma
el motor de vigilancia de FT Intelligence. Un jugador queda vigilado cuando
🩹 pidió MTO y ganó igual, o 🔄 se retiró de un partido. Cuando aparece su
próximo partido con cuota, sale **un solo mensaje**:

```
🧠 📊 FT INTELLIGENCE · REVISIÓN DE CUOTA
Carlos vs Pedro · M25 Sapporo · (la hora, en la zona de cada uno)
🩹 Carlos pidió MTO el 21/09 y ganó igual (contra Mario, 6-3 6-4)
🔎 Causa: sin confirmar (investigador pendiente)
¿Por qué Pedro está a 1.95?
Pedro: mercado 49% · FTR 43% · Elo 49%
Carga (8 días): Carlos 7 · Pedro 3
Rivales recientes (percentil FTR): Carlos 12 · Pedro 91
Conclusión (explicada): La cuota de Pedro podría estar incorporando…
```

Sale **una sola alerta por partido, 10 minutos antes de que empiece**: la
ficha escrita (titulada "EMPIEZA EN 10 MINUTOS") y, por voz: "Atención
FullTennis. En diez minutos empieza Carlos contra Pedro. Carlos pidió
atención médica ayer y ganó igual. Pedro paga uno punto noventa y cinco. La cuota de Pedro
podría estar incorporando el tiempo médico de Carlos, la mayor carga de
Carlos y que el Elo ve el partido como el mercado."

La conclusión puede ser **en línea** (la cuota coincide con el FTR),
**explicada** o **sin explicación suficiente**, y las tres se anuncian:
que los datos no expliquen la cuota también es información.

Todo es **informativo**: describe el contexto, no recomienda apostar. En el
canal de texto la hora aparece como marca de tiempo de Discord, así cada
persona la ve en su propia zona horaria.

**Solo alerta un CANDIDATO.** FT Intelligence revisa a cada vigilado, pero
solo manda la alerta cuando encuentra una discrepancia clara entre el FTR y
el mercado respaldada por varias piezas independientes (la segunda puerta:
DESCARTADO, OBSERVAR o CANDIDATO). Los descartados y los observados se
guardan para el backtest y no suenan. La alerta de un CANDIDATO dice el
lado con valor y su cuota, mercado contra FTR y Elo, el evento del vigilado,
la salud y las piezas a favor:

```
🔥 FT INTELLIGENCE · CANDIDATO · EMPIEZA EN 10 MINUTOS
Pedro @3.8. El mercado le asigna 25%, mientras el FTR lo sitúa en 40% (Elo 42%).
🩹 Carlos pidió MTO el 21/09 y ganó igual
🔎 Salud: sin confirmar (investigador pendiente)
A favor: el Elo también ve a Pedro por encima del mercado; Carlos llega más cargado.
```

Nada se publica antes: el momento lo decide FT Intelligence con la hora
vigente del partido. Un partido sin hora confirmada no se alerta (no hay
cómo saber cuándo faltan 10 minutos); si la hora aparece después, sí. Si
los dos jugadores están vigilados, es una sola alerta que menciona a ambos.

**🎾 Ventaja Leve (23/09/2026).** Tipo `VENTAJA_LEVE`: solo las reglas
VL01-VL05 que sonaron en FullTenis. El texto y la frase vienen armados
desde FullTenis (el mismo % y la misma muestra que Telegram); la voz es
corta y no lee Elo, FTR, muestra ni cuota. Las Ventaja Leve silenciosas
nunca llegan al bot. **🔥 Pasan Filtro** (tipo `PASAN_FILTRO`) funciona
igual: solo cuando hay una regla UNDER/OVER activa.

**Variables de tipos.** Por defecto `FT_DISCORD_TIPOS_TEXTO` y
`FT_DISCORD_TIPOS_VOZ` valen `REVISION_VOZ,VENTAJA_LEVE,PASAN_FILTRO`. Si en Railway quedó cargada
otra lista de una versión anterior (por ejemplo `MTO_RECIENTE`), **hay que
borrarla**: pisaría el valor por defecto y el bot no diría las revisiones.

Reglas para no saturar:
- Cada señal se anuncia **una sola vez**. FT Intelligence no repite señales y el bot no relee lo ya anunciado.
- Al reiniciarse, el bot **empieza desde ese momento**: no repite lo viejo.
- Una señal atrasada (más de 30 minutos, por ejemplo tras una caída) no se anuncia.
- Si se acumulan más de 5 avisos de voz, los siguientes van solo por texto.
- Opcional: horas de silencio para la voz (`FT_DISCORD_SILENCIO_VOZ=00-07`).

Los partidos ITF suelen venir sin hora: se anuncian con "sin hora
confirmada" y no tienen recordatorio.

## 1. Crear el bot en Discord

1. En el [portal de desarrolladores](https://discord.com/developers/applications):
   **New Application** → pestaña **Bot** → **Reset Token**. Ese token es
   `DISCORD_TOKEN` (se ve una sola vez; guardarlo).
2. No hace falta activar ningún *Privileged Gateway Intent*.
3. **Invitarlo al servidor**: pestaña **OAuth2 → URL Generator** → scope
   `bot` → permisos **View Channels**, **Send Messages**, **Connect** y
   **Speak** → abrir la URL generada y elegir el servidor.
4. **IDs de los canales**: en Discord, *Ajustes → Avanzado → Modo
   desarrollador*. Después, clic derecho en el canal → **Copiar ID**. Uno
   para el canal de texto y otro para el de voz.

## 2. La voz (opcional)

Sin esto, el bot funciona igual y solo escribe.

1. En Google Cloud, un proyecto con facturación activada.
2. Habilitar **Cloud Text-to-Speech API**.
3. **Credenciales → Crear credenciales → Clave de API**, y restringirla a
   esa API. Esa es `GOOGLE_TTS_API_KEY`.

Cada aviso son unos 150 caracteres. El tramo gratuito mensual de Google
está muy por encima de lo que este bot habla; confirmarlo en su página de
precios. La voz por defecto es `es-US-Neural2-B` (masculina). Se cambia con
`TTS_VOZ`; la lista está en la documentación de voces de Google. Si el
nombre no existe, el aviso sale solo por texto y el log lo dice.

## 3. Desplegar en Railway

1. Subir este repositorio a GitHub (privado), con el **contenido** de la
   carpeta en la raíz.
2. En el proyecto de FullTenis: **New → GitHub Repo → FT_DISCORD**. Railway
   usa el `Dockerfile`, que instala FFmpeg y libopus. No hace falta volumen.
3. En FT Intelligence: **Settings → Networking → Generate Domain**, para
   que el bot pueda leer `/senales` (protegido con el secreto interno).
4. Variables del bot (ver `.env.example`): `DISCORD_TOKEN`,
   `DISCORD_CANAL_TEXTO_ID`, `DISCORD_CANAL_VOZ_ID`, `FT_INTEL_URL` (el
   dominio del paso 3), `FTR_INTERNAL_SECRET`, `GOOGLE_TTS_API_KEY` y
   `FT_DISCORD_ZONA_HORARIA` (por ejemplo `America/Bogota`), que es la zona
   en la que se dice la hora en voz alta.

## 4. Prueba real en tu Discord (antes de dar la voz por terminada)

Las pruebas automáticas cubren la lógica, los textos, la cola y el pedido
de voz, pero **no** la cadena real: conexión → DAVE → reproducción →
reconexión. Eso se prueba así, en este orden:

0. **Entrar al canal de voz ANTES de desplegar.** El cifrado DAVE se negocia
   con quienes están en la llamada; el bot no habla hasta que queda listo.
1. **Arranque.** Con `FT_DISCORD_PRUEBA=1`, desplegar. En el log:
   `discord.py 2.7.1 · soporte DAVE: sí` y
   `listo como … | voz: sí | desde la señal #N`.
2. **Conexión y DAVE.** En el log:
   `conectado al canal de voz … · DAVE: versión 1, negociando`. Es normal: el
   cifrado termina de negociarse un momento después, y recién entonces habla.
   El bot aparece en el canal de voz, ensordecido.
3. **Reproducción.** En el canal de texto sale
   `🔧 Prueba de FT Discord · voz en … · cifrado DAVE: …`, y en el canal
   de voz se escucha: *"Atención FullTennis. Esta es una prueba de voz. Si
   me escuchas, la voz funciona."*
4. **Reconexión, con el bot funcionando** (el caso que va a pasar en
   producción). Desconectarlo del canal a mano (clic derecho →
   Desconectar). En unos 5 segundos tiene que volver **solo**, sin
   reiniciar el contenedor. El log muestra
   `me sacaron del canal de voz: vuelvo en unos segundos` y después
   `conectado al canal de voz`. Con la prueba activa, además **habla**:
   *"Volví a conectarme al canal. La voz sigue funcionando."* Eso prueba que
   vuelve a hablar, no solo que vuelve. Repetirlo **moviéndolo** a otro
   canal: vuelve al suyo.
5. **Reinicio.** Reiniciar el servicio: vuelve a entrar al canal y **no**
   repite señales viejas.
6. Quitar `FT_DISCORD_PRUEBA` (o ponerlo en 0).

Si falla un paso, el log dice cuál:
- **Sin DAVE:** "soporte DAVE: NO" significa que falta la librería `davey`.
- **Sin permisos:** revisar **Connect** y **Speak** en el canal.
- **Sin audio con el bot conectado:** revisar la clave de Google y el nombre de voz.
- **No encuentra libopus:** es un problema de la imagen; el Dockerfile lo instala.
- **No vuelve tras desconectarlo:** el log dice `no se pudo entrar al canal de voz` con el motivo.
- **`el cifrado DAVE no quedó listo en 15 s`:** el bot se salteó ese aviso para no
  mandar audio sin cifrar. discord.py 2.7.1 lo manda sin el cifrado de extremo a
  extremo si la sesión DAVE todavía no terminó de negociarse, y Discord corta la
  llamada con el código 4006. Pasó en la primera prueba real, y por eso el bot
  espera. Si se repite con gente en el canal, pegar el log completo.

Por qué importa el paso 4: cuando alguien echa al bot, discord.py no
reconecta solo, y la conexión muerta puede quedar registrada en el servidor.
En ese caso el siguiente intento falla con "Already connected to a voice
channel", y un bot ingenuo queda afuera hasta que se reinicia. Este lo
detecta en el momento, limpia la conexión vieja y vuelve; si igual aparece
ese error, adopta la conexión existente. `tests/test_voz.py` reproduce ese
caso exacto.

## 5. Observar un día completo

Después de la prueba, dejarlo 24 horas **sin tocar reglas**. Los números:
- **El bot:** cada hora deja en el log una línea
  `RESUMEN última hora: textos … · voz … · agrupadas … · voz omitida … · atrasadas … · errores … | desde el arranque: … | reconexiones de voz: …`.
- **FT Intelligence:** `python scripts/revisar.py` muestra, en "MOTOR DE
  VIGILANCIA", cuántos MTO y retiros dispararon o se descartaron (y por
  qué), cuántos jugadores están vigilados y las revisiones por conclusión.

Con eso se decide, con datos:
- los umbrales de la conclusión (`FT_INTEL_REVISION_EN_LINEA_PP`,
  `FT_INTEL_REVISION_GRANDE_PP`, en FT Intelligence);
- si todas las conclusiones van por voz o solo algunas;
- si hacen falta horas de silencio.

## 6. Verificar

En los logs:
```
[FTDiscord] listo como FullTennis#1234 | texto #alertas | voz: sí | desde la señal #57
[FTDiscord] conectado al canal de voz Anuncios
```
El bot aparece en el canal de voz, en silencio. Con la próxima señal,
escribe y habla.

Para una prueba inmediata, sin esperar una señal nueva:
`FT_DISCORD_DESDE_ID=0` repite todas las señales existentes. Usarlo solo
para probar y después borrarlo.

**Importante: la voz hay que probarla en vivo.** Desde marzo de 2026
Discord exige el cifrado DAVE para entrar a cualquier canal de voz, y los
bots que no lo soportan quedan afuera. discord.py lo soporta desde la
versión 2.7, con la librería `davey`, que está fijada en `requirements.txt`.
Es reciente (0.1.6): si el bot entra pero no se oye, o no logra entrar,
revisar el log y los permisos **Connect** y **Speak** del canal.

## Pruebas

```bash
pip install -r requirements.txt
bash tests/correr_todo.sh
FT_INTEL_REPO=/ruta/a/FT_INTELLIGENCE bash tests/correr_todo.sh   # + contrato
```
| Suite | Qué cubre |
|---|---|
| `test_bot.py` (44) | español hablado y escrito, partido nuevo con FTR/Elo, ráfagas agrupadas, anunciador (arranque, una vez, atrasadas, silencio, fallas), pedido a Google, configuración, textos de prueba |
| `test_voz.py` (15) | reconexión con dobles de Discord: desconexión forzada con la conexión muerta registrada, vuelve a hablar, "Already connected", lo mueven de canal, carreras, fallas, resumen por hora |
| `test_contrato_ft_intelligence.py` (4) | el bot contra el `/senales` **real** de FT Intelligence |

Ninguna usa la red ni Discord.

## Estructura

```
ft_discord/
  config.py      variables de entorno
  fuente.py      lee /senales de FT Intelligence
  textos.py      cómo se escribe y cómo se dice cada señal
  tts.py         texto a voz (Google), intercambiable
  anunciador.py  qué se anuncia y por dónde (sin Discord adentro)
  bot.py         Discord: canal de texto, voz 24/7 con reconexión y cola
```

## Seguridad de la clave de voz

La clave de Google viaja en la cabecera `X-Goog-Api-Key`, nunca en la
dirección, y el bot no registra las direcciones de sus pedidos HTTP. Hasta la
v3, la clave iba en la dirección y quedaba escrita en el log de Railway: si se
usó una versión anterior, hay que **regenerar la clave** en Google Cloud
(Credentials → la clave → Regenerate key) y cargar la nueva en
`GOOGLE_TTS_API_KEY`.
