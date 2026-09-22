"""
ft_discord/bot.py — El bot de Discord (21/09/2026).

  python -m ft_discord.bot

Escribe cada señal en un canal de texto y, si hay canal de voz y
proveedor de voz configurados, se queda conectado a ese canal las 24 h
(como un miembro más, en silencio) y la dice en voz alta.

VOZ Y CIFRADO. Desde marzo de 2026 Discord exige DAVE (cifrado de extremo
a extremo) para entrar a cualquier canal de voz. discord.py lo soporta
desde la 2.7 con la dependencia `davey`, que instala el extra [voice].
Además hacen falta FFmpeg y libopus en el sistema (ver Dockerfile).
"""
from __future__ import annotations

import asyncio
import logging
import sys

import discord

from ft_discord import config
from ft_discord import tts as tts_mod
from ft_discord.anunciador import Anunciador
from ft_discord.fuente import ErrorFuente, Fuente
from ft_discord.textos import TEXTO_PRUEBA_VOZ, TEXTO_RECONEXION, texto_prueba_canal

log = logging.getLogger("ft_discord")


def _cargar_opus() -> None:
    if discord.opus.is_loaded():
        return
    for nombre in ("libopus.so.0", "libopus.so", "opus"):
        try:
            discord.opus.load_opus(nombre)
            return
        except OSError:
            continue
    log.warning("[FTDiscord] no se encontró libopus: la voz no va a funcionar")


class Voz:
    """
    Conectado al canal de voz las 24 h; dice lo que se le encola, de a uno.

    RECONEXIÓN (el caso de producción: alguien desconecta al bot a mano).
    discord.py no reconecta solo tras una desconexión forzada: la toma como
    intencional. Y según la versión, la conexión vieja puede quedar
    registrada en el servidor y hacer fallar el siguiente connect() con
    "Already connected to a voice channel". Por eso:
      - el bot se entera EN EL MOMENTO (on_voice_state_update) y vuelve a
        los pocos segundos; la revisión de cada 60 s queda de respaldo;
      - antes de reconectar limpia la conexión vieja; si igual aparece
        "Already connected", adopta la que existe;
      - si lo movieron a otro canal, vuelve al suyo;
      - un candado impide dos intentos de conexión a la vez.
    """

    def __init__(self, cliente, canal_id: int, tts, max_cola: int,
                 fuente_audio=None, al_reconectar=None, espera_reconexion: float = 5.0):
        self.cliente, self.canal_id, self.tts = cliente, canal_id, tts
        self.cola: asyncio.Queue = asyncio.Queue()
        self.max_cola = max_cola
        self.vc = None
        self._estuvo_conectado = False
        self._candado = asyncio.Lock()
        # Inyectables para probar sin Discord ni FFmpeg.
        self._fuente_audio = fuente_audio or discord.FFmpegPCMAudio
        self._al_reconectar = al_reconectar
        self._espera_reconexion = espera_reconexion
        self.reconexiones = 0

    def estado_dave(self) -> str:
        """El código de privacidad existe si la sesión DAVE quedó negociada
        (discord.py >= 2.7)."""
        codigo = getattr(self.vc, "voice_privacy_code", None) if self.vc else None
        return f"activo (código {codigo})" if codigo else "sin código todavía"

    def encolar(self, texto: str) -> bool:
        if self.cola.qsize() >= self.max_cola:
            return False
        self.cola.put_nowait(texto)
        return True

    async def _canal(self):
        return self.cliente.get_channel(self.canal_id) or await self.cliente.fetch_channel(self.canal_id)

    async def asegurar_conexion(self) -> bool:
        async with self._candado:
            canal = await self._canal()
            if self.vc and self.vc.is_connected():
                if getattr(getattr(self.vc, "channel", None), "id", self.canal_id) != self.canal_id:
                    log.warning("[FTDiscord] me movieron de canal de voz: vuelvo al configurado")
                    await self.vc.move_to(canal)
                return True
            guild = getattr(canal, "guild", None)
            vieja = getattr(guild, "voice_client", None)
            if vieja is not None and vieja.is_connected():
                self.vc = vieja                  # ya hay una conexión viva: se adopta
                return True
            if vieja is not None:                # conexión muerta que quedó registrada
                try:
                    await vieja.disconnect(force=True)
                except Exception:                # noqa: BLE001
                    pass
            reconexion = self._estuvo_conectado
            if reconexion:
                log.warning("[FTDiscord] la voz se desconectó: reconectando")
            try:
                self.vc = await canal.connect(self_deaf=True, reconnect=True, timeout=30)
            except discord.ClientException as e:
                actual = getattr(guild, "voice_client", None)
                if actual is not None and actual.is_connected():
                    self.vc = actual             # "Already connected": se adopta
                else:
                    log.warning(f"[FTDiscord] no se pudo entrar al canal de voz: {e}")
                    return False
            except Exception as e:               # noqa: BLE001
                log.warning(f"[FTDiscord] no se pudo entrar al canal de voz: {e}")
                return False
            self._estuvo_conectado = True
            log.info(f"[FTDiscord] conectado al canal de voz {canal} · DAVE: {self.estado_dave()}")
        if reconexion:
            self.reconexiones += 1
            if self._al_reconectar:
                self._al_reconectar()
        return True

    async def reconectar_pronto(self) -> None:
        """Llamado al detectar la desconexión: vuelve en segundos, sin
        esperar la revisión de cada minuto."""
        await asyncio.sleep(self._espera_reconexion)
        await self.asegurar_conexion()

    async def vigilar(self) -> None:
        while True:                              # respaldo: siempre conectado
            await self.asegurar_conexion()
            await asyncio.sleep(60)

    async def decir_uno(self, texto: str) -> bool:
        ruta = await asyncio.to_thread(self.tts.sintetizar, texto)
        if not ruta or not await self.asegurar_conexion():
            return False
        loop = asyncio.get_running_loop()
        fin = asyncio.Event()
        try:
            # f=fin: cada audio avisa a SU evento; uno anterior que termine
            # tarde no puede cortar el aviso siguiente.
            self.vc.play(self._fuente_audio(str(ruta)),
                         after=lambda e, f=fin: loop.call_soon_threadsafe(f.set))
            await asyncio.wait_for(fin.wait(), timeout=90)
            return True
        except Exception as e:                   # noqa: BLE001
            log.warning(f"[FTDiscord] no se pudo reproducir el aviso: {e}")
            return False

    async def trabajar(self) -> None:
        while True:
            await self.decir_uno(await self.cola.get())
            await asyncio.sleep(1.0)


class Bot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())   # incluye voice_states
        self._arrancado = False
        self.voz = None

    async def on_voice_state_update(self, member, before, after):
        """Si desconectan (o mueven) al bot a mano, vuelve enseguida."""
        if self.voz and self.user and member.id == self.user.id and before.channel \
                and (after.channel is None or after.channel.id != config.CANAL_VOZ_ID):
            log.warning("[FTDiscord] me sacaron del canal de voz: vuelvo en unos segundos")
            asyncio.create_task(self.voz.reconectar_pronto())

    async def on_ready(self):
        if self._arrancado:                      # on_ready se repite tras reconectar
            return
        self._arrancado = True
        canal = self.get_channel(config.CANAL_TEXTO_ID) or await self.fetch_channel(config.CANAL_TEXTO_ID)

        async def publicar(texto: str) -> None:
            await canal.send(texto[:1900], allowed_mentions=discord.AllowedMentions.none())

        tts = tts_mod.crear(config.TTS_PROVEEDOR, clave=config.GOOGLE_TTS_API_KEY,
                            idioma=config.TTS_IDIOMA, voz=config.TTS_VOZ,
                            velocidad=config.TTS_VELOCIDAD)
        voz = None
        if config.CANAL_VOZ_ID and not isinstance(tts, tts_mod.TTSNinguno):
            _cargar_opus()
            voz = Voz(self, config.CANAL_VOZ_ID, tts, config.MAX_COLA_VOZ,
                      # En modo prueba, habla tras cada reconexión: así la prueba
                      # confirma que VOLVIÓ A HABLAR, no solo que volvió.
                      al_reconectar=(lambda: voz.encolar(TEXTO_RECONEXION)) if config.PRUEBA else None)
            self.voz = voz
            asyncio.create_task(voz.vigilar())
            asyncio.create_task(voz.trabajar())
        fuente = Fuente(config.FT_INTEL_URL, config.FTR_SECRET)
        anunciador = Anunciador(fuente, publicar, voz.encolar if voz else (lambda t: False),
                                config.ZONA_HORARIA, config.TIPOS_TEXTO, config.TIPOS_VOZ,
                                config.MAX_ANTIGUEDAD_MIN, config.SILENCIO_VOZ,
                                umbral_agrupar=config.UMBRAL_AGRUPAR,
                                voz_probabilidades=config.VOZ_PROBABILIDADES)
        cursor = await anunciador.arrancar(config.DESDE_ID)
        print(f"[FTDiscord] listo como {self.user} | texto #{canal} | voz: "
              f"{'sí' if voz else 'no'} | desde la señal #{cursor}", flush=True)
        if config.PRUEBA:
            await self._prueba(publicar, voz)
        asyncio.create_task(self._bucle(anunciador))

    async def _prueba(self, publicar, voz) -> None:
        """FT_DISCORD_PRUEBA=1: frase fija al arrancar, para probar la cadena
        conexión -> DAVE -> reproducción sin esperar una señal real."""
        if not voz:
            motivo = ("falta DISCORD_CANAL_VOZ_ID" if not config.CANAL_VOZ_ID
                      else "falta GOOGLE_TTS_API_KEY (o TTS_PROVEEDOR)")
            await publicar(f"🔧 **Prueba de FT Discord** · el texto funciona; voz no configurada: {motivo}")
            return
        ok = await voz.asegurar_conexion()
        nombre = getattr(getattr(voz.vc, "channel", None), "name", str(config.CANAL_VOZ_ID))
        await publicar(texto_prueba_canal(nombre, voz.estado_dave() if ok else "no se pudo conectar"))
        voz.encolar(TEXTO_PRUEBA_VOZ)

    async def _bucle(self, anunciador: Anunciador) -> None:
        loop = asyncio.get_running_loop()
        proximo_resumen = loop.time() + 3600
        while True:
            if loop.time() >= proximo_resumen:
                # Para el día de observación: volumen de textos, voz,
                # agrupaciones y omisiones, sin contar líneas a mano.
                hora, total = anunciador.resumen()
                rec = self.voz.reconexiones if self.voz else 0
                print(f"[FTDiscord] RESUMEN última hora: {_resumen(hora)} | desde el arranque: "
                      f"{_resumen(total)} | reconexiones de voz: {rec}", flush=True)
                proximo_resumen += 3600
            try:
                r = await anunciador.ciclo()
                if any(r.values()):
                    log.info(f"[FTDiscord] {r}")
            except ErrorFuente as e:
                log.warning(f"[FTDiscord] {e}")
            except Exception as e:               # noqa: BLE001
                log.error(f"[FTDiscord] error inesperado: {e}", exc_info=True)
            await asyncio.sleep(config.INTERVALO_SEG)


def _resumen(c: dict) -> str:
    return (f"textos {c.get('texto', 0)} · voz {c.get('voz', 0)} · agrupadas "
            f"{c.get('agrupadas', 0)} · voz omitida {c.get('voz_omitida', 0)} · atrasadas "
            f"{c.get('viejas', 0)} · errores {c.get('errores', 0)}")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from discord import voice_state
    print(f"[FTDiscord] discord.py {discord.__version__} · soporte DAVE: "
          f"{'sí' if getattr(voice_state, 'has_dave', False) else 'NO (falta davey)'}", flush=True)
    faltan = config.problemas()
    if faltan:
        print("[FTDiscord] no arranca: " + "; ".join(faltan), flush=True)
        return 1
    Bot().run(config.DISCORD_TOKEN, log_handler=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
