#!/usr/bin/env python3
"""
tests/test_voz.py — Reconexión de la voz, con dobles de Discord
(21/09/2026).

El caso de producción: alguien desconecta al bot del canal a mano
mientras funciona. Tiene que volver SOLO y poder HABLAR otra vez, sin
reiniciar el contenedor. Se reproduce el caso problemático: la conexión
muerta queda registrada en el servidor (guild.voice_client).

  1. Primera conexión, estado DAVE y un aviso dicho.
  2. Desconexión forzada con la conexión vieja registrada: la limpia,
     reconecta, y el siguiente aviso sale por la conexión NUEVA.
  3. "Already connected": adopta la conexión existente.
  4. Si lo mueven de canal, vuelve al suyo.
  5. El evento de Discord dispara la vuelta en segundos.
  6. Dos intentos a la vez: una sola conexión (candado).
  7. Una falla transitoria no rompe nada y el intento siguiente funciona.
  8. El resumen por hora para el día de observación.

Lo que esto NO prueba es la red real, DAVE y el audio en tu servidor:
eso es la prueba en vivo del README.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

import discord  # noqa: E402

from ft_discord import config  # noqa: E402
from ft_discord.anunciador import Anunciador  # noqa: E402
from ft_discord.bot import Bot, Voz  # noqa: E402

_fallos: list[str] = []
_ok = 0


def check(nombre: str, condicion: bool, detalle: str = ""):
    global _ok
    if condicion:
        _ok += 1
        print(f"  OK    {nombre}" + (f" -- {detalle}" if detalle else ""))
    else:
        _fallos.append(nombre)
        print(f"  FALLA {nombre}" + (f": {detalle}" if detalle else ""))


# ── dobles de Discord ────────────────────────────────────────────────
class FakeVC:
    def __init__(self, canal):
        self.channel, self.connected, self.voice_privacy_code = canal, True, "12345"
        self.reproducidos, self.desconexiones = [], 0

    def is_connected(self):
        return self.connected

    async def disconnect(self, force=False):
        self.connected = False
        self.desconexiones += 1
        if self.channel.guild.voice_client is self:
            self.channel.guild.voice_client = None

    async def move_to(self, canal):
        self.channel = canal

    def play(self, fuente, after):
        self.reproducidos.append(fuente)
        after(None)                               # el audio "termina" enseguida


class FakeGuild:
    voice_client = None


class FakeCanal:
    def __init__(self, cid, guild):
        self.id, self.guild, self.conexiones, self.falla = cid, guild, 0, None

    async def connect(self, **kw):
        # Igual que discord.py (abc.Connectable.connect): si el servidor ya
        # tiene una conexión registrada -- viva o muerta --, se niega.
        if self.guild.voice_client is not None:
            raise discord.ClientException("Already connected to a voice channel.")
        self.conexiones += 1
        await asyncio.sleep(0.01)                 # como la red: da lugar a carreras
        if self.falla:
            falla, self.falla = self.falla, None
            if isinstance(falla, discord.ClientException):
                self.guild.voice_client = FakeVC(self)    # discord.py ya tenía una viva
            raise falla
        vc = FakeVC(self)
        self.guild.voice_client = vc
        return vc

    def __str__(self):
        return f"canal-{self.id}"


class FakeCliente:
    def __init__(self, canales):
        self.canales = canales

    def get_channel(self, cid):
        return self.canales.get(cid)

    async def fetch_channel(self, cid):
        return self.canales[cid]


class FakeTTS:
    def sintetizar(self, texto):
        return Path(f"/tmp/{abs(hash(texto))}.ogg")


async def escenario():
    guild = FakeGuild()
    canal, otro = FakeCanal(99, guild), FakeCanal(77, guild)
    avisos_reconexion = []
    voz = Voz(FakeCliente({99: canal, 77: otro}), 99, FakeTTS(), 5,
              fuente_audio=lambda ruta: f"audio:{ruta}",
              al_reconectar=lambda: avisos_reconexion.append(1), espera_reconexion=0)

    print("\n1. Primera conexión")
    check("se conecta y muestra el estado DAVE", await voz.asegurar_conexion()
          and canal.conexiones == 1 and voz.estado_dave() == "activo (código 12345)")
    check("dice un aviso", await voz.decir_uno("hola") and len(voz.vc.reproducidos) == 1)
    check("la primera conexión no cuenta como reconexión", voz.reconexiones == 0 and not avisos_reconexion)

    print("\n2. Desconexión forzada con la conexión vieja registrada")
    vieja = voz.vc
    vieja.connected = False                       # lo echaron del canal...
    check("(escenario) la conexión muerta sigue registrada", guild.voice_client is vieja)
    ok = await voz.asegurar_conexion()
    check("limpia la conexión vieja y reconecta", ok and vieja.desconexiones == 1
          and canal.conexiones == 2 and voz.vc is not vieja and voz.vc.is_connected())
    check("cuenta la reconexión y avisa (en modo prueba, habla)",
          voz.reconexiones == 1 and avisos_reconexion == [1])
    check("VUELVE A HABLAR por la conexión nueva, sin reiniciar",
          await voz.decir_uno("otra vez") and len(voz.vc.reproducidos) == 1
          and len(vieja.reproducidos) == 1)

    print("\n3. 'Already connected'")
    voz.vc.connected = False
    guild.voice_client = None
    canal.falla = discord.ClientException("Already connected to a voice channel.")
    ok = await voz.asegurar_conexion()
    check("adopta la conexión que ya existía en vez de quedarse afuera",
          ok and voz.vc is guild.voice_client and voz.vc.is_connected())

    print("\n4. Si lo mueven de canal")
    voz.vc.channel = otro
    await voz.asegurar_conexion()
    check("vuelve al canal configurado", voz.vc.channel is canal)

    print("\n5. El evento de Discord")
    config.CANAL_VOZ_ID = 99
    programadas = []
    falso_bot = SimpleNamespace(voz=SimpleNamespace(reconectar_pronto=lambda: asyncio.sleep(0)),
                                user=SimpleNamespace(id=1))
    orig = asyncio.create_task
    asyncio.create_task = lambda coro: (programadas.append(1), orig(coro))[1]
    yo, otra = SimpleNamespace(id=1), SimpleNamespace(id=2)
    en_canal, fuera = SimpleNamespace(channel=SimpleNamespace(id=99)), SimpleNamespace(channel=None)
    await Bot.on_voice_state_update(falso_bot, yo, en_canal, fuera)             # lo echaron
    await Bot.on_voice_state_update(falso_bot, yo, en_canal, SimpleNamespace(channel=SimpleNamespace(id=77)))
    await Bot.on_voice_state_update(falso_bot, otra, en_canal, fuera)           # otra persona
    await Bot.on_voice_state_update(falso_bot, yo, fuera, en_canal)             # el bot entrando
    asyncio.create_task = orig
    check("desconectado o movido: programa la vuelta; otras personas o su propia entrada, no",
          len(programadas) == 2, str(len(programadas)))
    voz.vc.connected = False
    guild.voice_client = None
    await voz.reconectar_pronto()
    check("reconectar_pronto vuelve a conectar", voz.vc.is_connected())

    print("\n6. Dos intentos a la vez")
    voz.vc.connected = False
    guild.voice_client = None
    antes = canal.conexiones
    r = await asyncio.gather(voz.asegurar_conexion(), voz.asegurar_conexion())
    check("una sola conexión nueva (el candado)", all(r) and canal.conexiones == antes + 1,
          f"{canal.conexiones - antes} conexiones")

    print("\n7. Falla transitoria")
    voz.vc.connected = False
    guild.voice_client = None
    canal.falla = TimeoutError("Discord no respondió")
    check("si falla la conexión, el aviso se pierde sin romper nada",
          await voz.decir_uno("se pierde") is False)
    check("y el intento siguiente funciona", await voz.decir_uno("vuelve") and voz.vc.is_connected())


asyncio.run(escenario())

print("\n8. Resumen por hora")


class Fuente:
    def __init__(self):
        self.lotes = [[], []]

    async def ultimo_id(self):
        return 0

    async def leer(self, desde):
        return self.lotes.pop(0) if self.lotes else []


async def resumen():
    f = Fuente()
    a = Anunciador(f, lambda t: asyncio.sleep(0), lambda t: True, "UTC", set(), set(), 30)
    await a.arrancar(0)
    a.totales.update({"texto": 3, "voz": 1})
    a._hora.update({"texto": 3, "voz": 1})
    hora, total = a.resumen()
    hora2, total2 = a.resumen()
    check("la hora se reinicia; lo acumulado no",
          hora == {"texto": 3, "voz": 1} and hora2 == {} and total2 == {"texto": 3, "voz": 1})

asyncio.run(resumen())

print(f"\n{'─' * 60}")
if _fallos:
    print(f"{_ok} OK, {len(_fallos)} FALLAS:")
    for f in _fallos:
        print(f"  - {f}")
    sys.exit(1)
print(f"{_ok} comprobaciones OK.")
