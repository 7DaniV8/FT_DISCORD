"""
ft_discord/tts.py — Texto a voz (21/09/2026).

Por defecto, Google Cloud Text-to-Speech por su API REST v1, con una clave
de API restringida a ese servicio. Estable, con voces neuronales en
español, y con un tramo gratuito mensual muy por encima de lo que este bot
habla (cada aviso son unos 150 caracteres).

Intercambiable: cualquier objeto con sintetizar(texto) -> ruta de audio
(o None si falló). Sin proveedor, el bot solo escribe en el canal.
"""
from __future__ import annotations

import base64
import logging
import tempfile
from pathlib import Path
from typing import Optional

import httpx

log = logging.getLogger(__name__)
URL_GOOGLE = "https://texttospeech.googleapis.com/v1/text:synthesize"


class TTSGoogle:
    def __init__(self, clave: str, idioma: str, voz: str, velocidad: float = 1.0,
                 transport: Optional[httpx.BaseTransport] = None):
        self._http = httpx.Client(timeout=20, transport=transport)
        self._clave, self._idioma, self._voz, self._velocidad = clave, idioma, voz, velocidad
        self._dir = Path(tempfile.mkdtemp(prefix="ftd_tts_"))
        self._n = 0

    def sintetizar(self, texto: str) -> Optional[Path]:
        try:
            r = self._http.post(URL_GOOGLE, params={"key": self._clave}, json={
                "input": {"text": texto},
                "voice": {"languageCode": self._idioma, "name": self._voz},
                "audioConfig": {"audioEncoding": "OGG_OPUS", "speakingRate": self._velocidad}})
        except httpx.HTTPError as e:
            log.warning(f"[FTDiscord] TTS sin respuesta: {e}")
            return None
        if r.status_code != 200:
            # 400 suele ser un nombre de voz que no existe; 403, la clave.
            log.warning(f"[FTDiscord] TTS HTTP {r.status_code}: {r.text[:200]}")
            return None
        audio = (r.json() or {}).get("audioContent")
        if not audio:
            return None
        self._n += 1
        ruta = self._dir / f"aviso_{self._n % 50}.ogg"     # se reciclan: no llena el disco
        ruta.write_bytes(base64.b64decode(audio))
        return ruta


class TTSNinguno:
    def sintetizar(self, texto: str) -> Optional[Path]:
        return None


def crear(proveedor: str, **kw):
    if proveedor == "google" and kw.get("clave"):
        return TTSGoogle(kw["clave"], kw["idioma"], kw["voz"], kw.get("velocidad", 1.0))
    return TTSNinguno()
