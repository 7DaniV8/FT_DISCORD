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
# v14 (13/10/2026): tipos de los que se le confirma a FT Intelligence qué se publicó y qué se dijo.
# v16 (13/10/2026, Rubén: "no quiero que aparezca como enviada una alerta que FT Discord no haya
# confirmado"): también las tres de FT NEWS; FT Intelligence le reenvía el acuse a FT_NEWS.
ACUSE_TIPOS = ("IMPLICACION", "NEWS_VALOR", "NEWS_INTERESANTE", "NEWS_NOTICIA",
               "UNDER_PICK")    # v17: ▼ el Under vuelve a RankingFTR con su acuse (llegó o por qué no)


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
                 voz_probabilidades: bool = True,
                 publicar_admin: Optional[Callable[[str], Awaitable[None]]] = None):
        self.fuente, self.publicar_texto, self.encolar_voz = fuente, publicar_texto, encolar_voz
        # v16: canal SOLO de administradores (None = no hay; lo de admin no se publica en ningún lado)
        self.publicar_admin = publicar_admin
        self.zona, self.tipos_texto, self.tipos_voz = zona, tipos_texto, tipos_voz
        self.max_antiguedad_min, self.silencio_voz = max_antiguedad_min, silencio_voz
        self.reloj = reloj or (lambda: datetime.now(timezone.utc))
        self.umbral_agrupar, self.voz_probabilidades = umbral_agrupar, voz_probabilidades
        # Para observar un día real: lo de la última hora y lo acumulado.
        self._hora: Counter = Counter()
        self.totales: Counter = Counter()

    # 📣 Admin → Discord (08/10/2026): la configuración que viene de RankingFTR
    # vía FT Intelligence. None = no llegó (o FT Intelligence viejo): mandan
    # las variables de Railway como siempre.
    config_remota: Optional[dict] = None

    def _permite(self, tipo: str, canal: str) -> bool:
        """canal: 'texto' | 'voz'. Con configuración remota, manda ella; sin
        ella, las variables de Railway."""
        c = self.config_remota
        if c:
            if c.get("pausa"):
                return False
            t = (c.get("tipos") or {}).get(tipo)
            if t is not None:
                return bool(t.get(canal))
        return tipo in (self.tipos_texto if canal == "texto" else self.tipos_voz)

    def _personalizar(self, tipo: str, texto: str, voz: str) -> tuple:
        """Encabezado (primera línea del mensaje) y frase de voz al inicio."""
        t = ((self.config_remota or {}).get("tipos") or {}).get(tipo) or {}
        cab = (t.get("encabezado") or "").strip()
        # v11: una 🔥 EXPERIMENTAL de FT NEWS nunca pierde la etiqueta por un encabezado
        # personalizado (Rubén: "mantener la etiqueta EXPERIMENTAL").
        if cab and texto and "EXPERIMENTAL" in texto.split("\n")[0].upper() and "EXPERIMENTAL" not in cab.upper():
            cab = ""
        # v13: una PRUEBA del circuito (🏆 Implicación) tampoco pierde su marca: el
        # encabezado no pisa la línea "🧪 PRUEBA DEL CIRCUITO".
        if cab and texto and "PRUEBA" in texto.split("\n")[0].upper() and "PRUEBA" not in cab.upper():
            cab = ""
        if cab and texto:
            lineas = texto.split("\n")
            lineas[0] = f"**{cab}**"
            texto = "\n".join(lineas)
        ini = (t.get("voz_inicio") or "").strip()
        if ini and voz:
            voz = f"{ini.rstrip('.')}. {voz}"
        return texto, voz

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

    async def _anunciar(self, tipo: str, texto: str, voz: str, r: dict) -> dict:
        """v14: devuelve el ACUSE {texto, voz, motivo}: si el mensaje se publicó en el canal y si la voz
        se encoló (y por qué no)."""
        texto, voz = self._personalizar(tipo, texto, voz)
        acuse = {"texto": False, "voz": False, "motivo": None}
        if not self._permite(tipo, "texto") and not self._permite(tipo, "voz"):
            r["apagadas"] += 1
            acuse["motivo"] = "tipo apagado en Admin → Discord (o pausa general)"
        if self._permite(tipo, "texto"):
            try:
                await self.publicar_texto(texto)
                r["texto"] += 1
                acuse["texto"] = True
            except Exception as e:               # noqa: BLE001 -- una falla no frena al resto
                r["errores"] += 1
                acuse["motivo"] = f"no se pudo publicar: {str(e)[:100]}"
                log.warning(f"[FTDiscord] no se pudo publicar ({tipo}): {e}")
        if self._permite(tipo, "voz"):
            hora = self.reloj().astimezone(ZoneInfo(self.zona)).hour
            if en_silencio(self.silencio_voz, hora):
                r["voz_omitida"] += 1
                acuse["motivo"] = acuse["motivo"] or "voz: horario de silencio"
            elif self.encolar_voz(voz):
                r["voz"] += 1
                acuse["voz"] = True
            else:
                r["voz_omitida"] += 1            # cola llena o sin voz
                acuse["motivo"] = acuse["motivo"] or "voz: cola llena o sin canal de voz"
        elif self._permite(tipo, "texto"):
            acuse["motivo"] = acuse["motivo"] or "voz apagada para este tipo"
        return acuse

    async def _anunciar_admin(self, tipo: str, texto: str, r: dict) -> dict:
        """v16: una señal marcada solo_admin (la 🧪 PRUEBA de FT NEWS). Va SOLO al canal de
        administradores, sin voz (el canal de voz es el de la comunidad) y sin encabezado
        personalizado. Sin canal de admin configurado, no se publica en ningún lado."""
        acuse = {"texto": False, "voz": False, "motivo": None, "canal": "admin"}
        if (self.config_remota or {}).get("pausa"):
            acuse["motivo"] = "pausa general en Admin → Discord"
            return acuse
        if self.publicar_admin is None:
            r["admin_sin_canal"] = r.get("admin_sin_canal", 0) + 1
            acuse["motivo"] = "no hay canal de administradores (falta DISCORD_CANAL_ADMIN_ID): no se publicó en ningún lado"
            return acuse
        try:
            await self.publicar_admin(texto)
            r["admin"] = r.get("admin", 0) + 1
            acuse["texto"] = True
            acuse["motivo"] = "solo canal de administradores; sin voz"
        except Exception as e:               # noqa: BLE001
            r["errores"] += 1
            acuse["motivo"] = f"no se pudo publicar en el canal de administradores: {str(e)[:100]}"
            log.warning(f"[FTDiscord] no se pudo publicar en admin ({tipo}): {e}")
        return acuse

    async def ciclo(self) -> dict:
        r = {"texto": 0, "voz": 0, "viejas": 0, "voz_omitida": 0, "errores": 0, "agrupadas": 0, "apagadas": 0}
        # 📣 La configuración de Admin se pide en cada ciclo (es chica); si no
        # llega, se conserva la última buena y, si nunca llegó, Railway manda.
        try:
            cfg = await self.fuente.configuracion()
        except Exception:                        # noqa: BLE001
            cfg = None
        if cfg is not None:
            self.config_remota = cfg
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
        acuses = []
        for s in nuevas:
            if s["tipo"] not in agrupadas:
                if (s.get("datos") or {}).get("solo_admin"):
                    a = await self._anunciar_admin(s["tipo"], texto_canal(s), r)
                else:
                    a = await self._anunciar(s["tipo"], texto_canal(s),
                                             texto_voz(s, self.zona, ahora, self.voz_probabilidades), r)
                    a["canal"] = "comunidad"
                if s["tipo"] in ACUSE_TIPOS:
                    acuses.append({"senal_id": s["id"], "tipo": s["tipo"], "en": ahora.isoformat(), **a})
        if acuses:
            # v14: FT Intelligence → RankingFTR saben qué llegó de verdad al canal y a la voz (la prueba
            # del circuito de Implicación enciende los avisos solo si vuelve con texto Y voz).
            try:
                await self.fuente.acusar(acuses)
            except Exception as e:               # noqa: BLE001 -- el acuse nunca frena el anuncio
                log.warning(f"[FTDiscord] no se pudo mandar el acuse: {e}")
        self._hora.update(r)
        self.totales.update(r)
        return r
