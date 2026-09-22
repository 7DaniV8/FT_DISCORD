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

| Señal | Ejemplo de lo que dice en voz alta |
|---|---|
| Partido nuevo + hora | "Atención FullTennis. Partido nuevo: A contra B, hoy a las nueve y treinta de la noche. El FTR da favorito a A con sesenta y ocho por ciento; el Elo, setenta y cuatro por ciento." |
| Carga extrema | "Atención FullTennis. Carga extrema. Jugador A llega con ocho partidos en los últimos ocho días; Jugador B, con uno. Juegan hoy a las nueve y treinta de la noche." |
| MTO reciente | "Atención FullTennis. Jugador A pidió atención médica hace dos días. Juega contra Jugador B mañana a las tres en punto de la tarde." |
| Cuota lejos del FTR | "Atención FullTennis. Cuota fuera de lo normal en A contra B, hoy a las… La casa le da a A sesenta y nueve por ciento; el FTR, ochenta y cinco. Lo explicaría esto: A pidió tiempo médico hace 2 días." |
| Recordatorio | "Atención FullTennis. Faltan quince minutos para A contra B, a las nueve y treinta de la noche." |
| Cambio de hora | "Atención FullTennis. Cambio de horario. A contra B ahora se juega hoy a las diez en punto de la noche." |

Todas son **informativas**: describen el contexto, no recomiendan apostar.
En el canal de texto la hora aparece como marca de tiempo de Discord, así
cada persona la ve en su propia zona horaria.

**Partido nuevo + hora**, la función original. FullTenis ve unos 194
partidos por día, así que:
- **Por voz, cada partido se dice UNA vez: 15 minutos antes** (el
  recordatorio, con la hora). El aviso de partido nuevo va **por texto**,
  como calendario. Para decirlo también en voz al detectarlo, agregar
  `PARTIDO_NUEVO` a `FT_DISCORD_TIPOS_VOZ`.
- **Las ráfagas se agrupan.** FullTenis publica el calendario en tandas:
  si en una vuelta llegan 3 o más partidos nuevos, o 3 o más recordatorios
  a la vez, van en **una** publicación (una lista) y **una** frase
  ("Siete partidos nuevos en el calendario. El primero: …").
- Qué niveles se anuncian lo decide FT Intelligence
  (`FT_INTEL_PARTIDO_NUEVO_NIVELES`). Por defecto, solo los partidos con
  hora conocida.

Si el Elo no está de acuerdo con el FTR sobre quién es favorito, la voz lo
dice ("el Elo prefiere al rival, con…") en vez de leer un porcentaje que
confundiría.

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

1. **Arranque.** Con `FT_DISCORD_PRUEBA=1`, desplegar. En el log:
   `discord.py 2.7.1 · soporte DAVE: sí` y
   `listo como … | voz: sí | desde la señal #N`.
2. **Conexión y DAVE.** En el log:
   `conectado al canal de voz … · DAVE: activo (código …)`. El bot aparece
   en el canal de voz, ensordecido.
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
- **FT Intelligence:** `python scripts/revisar.py` muestra las señales por
  tipo de las últimas 24 horas.

Con eso se decide, con datos:
- qué niveles anunciar (`FT_INTEL_PARTIDO_NUEVO_NIVELES`);
- qué tipos van por voz (`FT_DISCORD_TIPOS_VOZ`);
- desde cuántos se agrupa (`FT_DISCORD_UMBRAL_AGRUPAR`);
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
