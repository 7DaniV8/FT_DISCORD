"""
ft_discord/config.py — Configuración por variables de entorno (21/09/2026).
Se lee al importar; los tests fijan las variables antes.
"""
from __future__ import annotations

import os

# Desde el motor de vigilancia de FT Intelligence (22/09/2026): se ALERTA
# una sola vez por partido, 10 minutos antes de que empiece: REVISION_VOZ
# trae la ficha completa, que se escribe, y su conclusión, que se dice.
# REVISION_CUOTA (la revisión hecha al aparecer el partido con cuota) queda
# como registro interno y no se publica. Los tipos del
# barrido anterior (PARTIDO_NUEVO, CARGA_EXTREMA, MTO_RECIENTE, CUOTA_LEJOS,
# RECORDATORIO, CAMBIO_HORA) ya no se generan, pero se siguen sabiendo
# anunciar si alguien los vuelve a poner en estas variables.
# VENTAJA_LEVE (23/09/2026): solo las reglas VL01-VL05 que sonaron en
# RankingFTR (las silenciosas nunca llegan acá). Va por texto y por voz.
# PASAN_FILTRO (23/09/2026): la tendencia UNDER/OVER por sets de Pasan
# Filtro, solo cuando hay una regla activa. Mismo tratamiento.
# MTO_VIVO (24/09/2026): "Atención FullTennis. Tiempo médico solicitado
# por X", apenas llega el MTO. Texto y voz.
_TODOS = "REVISION_VOZ,VENTAJA_LEVE,PASAN_FILTRO,MTO_VIVO"
_VOZ = "REVISION_VOZ,VENTAJA_LEVE,PASAN_FILTRO,MTO_VIVO"


def _lista(nombre: str, defecto: str) -> set:
    return {x.strip().upper() for x in os.getenv(nombre, defecto).split(",") if x.strip()}


def _entero(nombre: str) -> int:
    v = os.getenv(nombre, "").strip()
    return int(v) if v else 0


DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
CANAL_TEXTO_ID = _entero("DISCORD_CANAL_TEXTO_ID")
CANAL_VOZ_ID = _entero("DISCORD_CANAL_VOZ_ID")          # 0 = sin voz

FT_INTEL_URL = os.getenv("FT_INTEL_URL", "").strip().rstrip("/")
FTR_SECRET = os.getenv("FTR_INTERNAL_SECRET", "").strip()

ZONA_HORARIA = os.getenv("FT_DISCORD_ZONA_HORARIA", "UTC").strip()
TIPOS_TEXTO = _lista("FT_DISCORD_TIPOS_TEXTO", _TODOS)
TIPOS_VOZ = _lista("FT_DISCORD_TIPOS_VOZ", _VOZ)
# Decir el FTR y el Elo en voz alta en el anuncio de partido nuevo.
VOZ_PROBABILIDADES = os.getenv("FT_DISCORD_VOZ_PROBABILIDADES", "1") != "0"
# Desde cuántas señales del mismo tipo en una vuelta se agrupan en una sola
# publicación y una sola frase (partidos nuevos y recordatorios).
UMBRAL_AGRUPAR = int(os.getenv("FT_DISCORD_UMBRAL_AGRUPAR", "3"))
# 1 = al arrancar, publicar y decir una frase de prueba (ver README).
PRUEBA = os.getenv("FT_DISCORD_PRUEBA", "0") == "1"
INTERVALO_SEG = float(os.getenv("FT_DISCORD_INTERVALO_SEG", "10"))
# Una señal más vieja que esto no se anuncia (por ejemplo, tras una caída).
MAX_ANTIGUEDAD_MIN = float(os.getenv("FT_DISCORD_MAX_ANTIGUEDAD_MIN", "30"))
# Horas locales sin voz, "00-07" (el texto sigue saliendo). Vacío = nunca.
SILENCIO_VOZ = os.getenv("FT_DISCORD_SILENCIO_VOZ", "").strip()
# Si se acumulan más avisos de voz que esto, los nuevos solo van por texto.
MAX_COLA_VOZ = int(os.getenv("FT_DISCORD_MAX_COLA_VOZ", "5"))
# -1 = empezar desde ahora (lo normal). Un número = repetir desde ese id.
DESDE_ID = int(os.getenv("FT_DISCORD_DESDE_ID", "-1"))

TTS_PROVEEDOR = os.getenv("TTS_PROVEEDOR", "google" if os.getenv("GOOGLE_TTS_API_KEY") else "ninguno")
GOOGLE_TTS_API_KEY = os.getenv("GOOGLE_TTS_API_KEY", "").strip()
TTS_IDIOMA = os.getenv("TTS_IDIOMA", "es-US").strip()
TTS_VOZ = os.getenv("TTS_VOZ", "es-US-Neural2-B").strip()
TTS_VELOCIDAD = float(os.getenv("TTS_VELOCIDAD", "1.0"))


def problemas() -> list:
    """Lo que falta para arrancar (vacío = todo bien)."""
    p = []
    if not DISCORD_TOKEN:
        p.append("falta DISCORD_TOKEN")
    if not CANAL_TEXTO_ID:
        p.append("falta DISCORD_CANAL_TEXTO_ID")
    if not FT_INTEL_URL or not FTR_SECRET:
        p.append("faltan FT_INTEL_URL / FTR_INTERNAL_SECRET")
    return p
