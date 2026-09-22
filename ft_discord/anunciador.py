"""
ft_discord/anunciador.py — Qué se anuncia y por dónde (21/09/2026).

Núcleo sin Discord adentro (se prueba con dobles):
  - arranca DESDE AHORA: al reiniciarse no repite señales viejas;
  - cada señal se anuncia una vez (el cursor de ids solo avanza);
  - una señal atrasada (más vieja que FT_DISCORD_MAX_ANTIGUEDAD_MIN,
    por ejemplo tras una caída) no se anuncia: llegaría tarde;
  - por tipo decide texto y/o voz; en las horas de silencio, solo texto;
  - RÁFAGAS: si en una vuelta llegan varios partidos nuevos (FullTenis
    publica el calendario en tandas) o varios recordatorios a la vez, van
    en UNA publicación y UNA frase, no una por partido.
Un error al publicar no frena a las siguientes señales.
"""
from __future__ import annotations

import asyncio
import logging
from collections import Counter
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional
from zoneinfo import ZoneInfo

from ft_discord.textos import texto_canal, texto_canal_grupo, texto_voz, texto_voz_grupo

log = logging.getLogger(__name__)
AGRUPABLES = ("PARTIDO_NUEVO", "RECORDATORIO")


def en_silencio(rango: str, hora_local: int) -> bool:
    """'00-07' -> de 0 a 6 inclusive; '22-07' cruza la medianoche."""
    if not rango:
        return False
    a, b = (int(x) for x in rango.split("-"))
    return a <= hora_local < b if a < b else (hora_local >= a or hora_local < b)


class Anunciador:
    def __init__(self, fuente, publicar_texto: Callable[[str], Awaitable[None]],
                 encolar_voz: Callable[[str], bool], zona: str, tipos_texto: set,
                 tipos_voz: set, max_antiguedad_min: float, silencio_voz: str = "",
                 reloj: Optional[Callable[[], datetime]] = None, umbral_agrupar: int = 3,
                 voz_probabilidades: bool = True):
        self.fuente, self.publicar_texto, self.encolar_voz = fuente, publicar_texto, encolar_voz
        self.zona, self.tipos_texto, self.tipos_voz = zona, tipos_texto, tipos_voz
        self.max_antiguedad_min, self.silencio_voz = max_antiguedad_min, silencio_voz
        self.reloj = reloj or (lambda: datetime.now(timezone.utc))
        self.umbral_agrupar, self.voz_probabilidades = umbral_agrupar, voz_probabilidades
        # Para observar un día real: lo de la última hora y lo acumulado.
        self._hora: Counter = Counter()
        self.totales: Counter = Counter()

    def resumen(self) -> tuple:
        """(lo de la última hora, lo acumulado desde el arranque); reinicia la hora."""
        hora, self._hora = dict(self._hora), Counter()
        return hora, dict(self.totales)
        self.cursor: Optional[int] = None

    async def arrancar(self, desde_id: int = -1) -> int:
        self.cursor = await self.fuente.ultimo_id() if desde_id < 0 else desde_id
        return self.cursor

    async def arrancar_con_reintentos(self, desde_id: int = -1, espera: float = 15.0,
                                      max_espera: float = 300.0) -> int:
        """Si FT Intelligence no responde al arrancar (caído, dirección mal
        puesta), reintenta con espera creciente en vez de dejar el bot
        conectado pero mudo hasta el próximo reinicio."""
        while True:
            try:
                return await self.arrancar(desde_id)
            except Exception as e:               # noqa: BLE001
                log.warning(f"[FTDiscord] FT Intelligence no responde al arrancar ({e}); "
                            f"reintento en {espera:.0f} s")
                await asyncio.sleep(espera)
                espera = min(espera * 2, max_espera)

    def _vieja(self, s: dict) -> bool:
        creado = datetime.fromisoformat(str(s["creado_en"]).replace("Z", "+00:00"))
        creado = creado if creado.tzinfo else creado.replace(tzinfo=timezone.utc)
        return (self.reloj() - creado).total_seconds() / 60 > self.max_antiguedad_min

    async def _anunciar(self, tipo: str, texto: str, voz: str, r: dict) -> None:
        if tipo in self.tipos_texto:
            try:
                await self.publicar_texto(texto)
                r["texto"] += 1
            except Exception as e:               # noqa: BLE001 -- una falla no frena al resto
                r["errores"] += 1
                log.warning(f"[FTDiscord] no se pudo publicar ({tipo}): {e}")
        if tipo in self.tipos_voz:
            hora = self.reloj().astimezone(ZoneInfo(self.zona)).hour
            if en_silencio(self.silencio_voz, hora):
                r["voz_omitida"] += 1
            elif self.encolar_voz(voz):
                r["voz"] += 1
            else:
                r["voz_omitida"] += 1            # cola llena o sin voz

    async def ciclo(self) -> dict:
        r = {"texto": 0, "voz": 0, "viejas": 0, "voz_omitida": 0, "errores": 0, "agrupadas": 0}
        nuevas = []
        for s in await self.fuente.leer(self.cursor):
            self.cursor = max(self.cursor, s["id"])
            if self._vieja(s):
                r["viejas"] += 1
            else:
                nuevas.append(s)
        ahora = self.reloj()
        agrupadas = set()
        for tipo in AGRUPABLES:
            grupo = [s for s in nuevas if s["tipo"] == tipo]
            if len(grupo) >= self.umbral_agrupar:
                agrupadas.add(tipo)
                r["agrupadas"] += len(grupo)
                await self._anunciar(tipo, texto_canal_grupo(tipo, grupo),
                                     texto_voz_grupo(tipo, grupo, self.zona, ahora), r)
        for s in nuevas:
            if s["tipo"] not in agrupadas:
                await self._anunciar(s["tipo"], texto_canal(s),
                                     texto_voz(s, self.zona, ahora, self.voz_probabilidades), r)
        self._hora.update(r)
        self.totales.update(r)
        return r
