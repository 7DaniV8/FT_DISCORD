"""
ft_discord/fuente.py — Lee las señales de FT Intelligence (21/09/2026).

GET /senales?desde_id=N con el secreto interno de FullTenis. El bot no
calcula nada: FT Intelligence decide qué decir; esto solo lo trae.
"""
from __future__ import annotations

from typing import Optional

import httpx

_MAX_ID = 9_223_372_036_854_775_807      # para pedir solo el último id


class ErrorFuente(Exception):
    pass


class Fuente:
    def __init__(self, url: str, secreto: str,
                 transport: Optional[httpx.AsyncBaseTransport] = None):
        self._http = httpx.AsyncClient(base_url=url, timeout=20, transport=transport,
                                       headers={"X-FTR-Internal-Secret": secreto})

    async def _get(self, desde_id: int, limite: int) -> dict:
        try:
            r = await self._http.get("/senales", params={"desde_id": desde_id, "limite": limite})
        except httpx.HTTPError as e:
            raise ErrorFuente(f"FT Intelligence sin respuesta: {e}") from e
        if r.status_code != 200:
            raise ErrorFuente(f"FT Intelligence HTTP {r.status_code}: {r.text[:120]}")
        return r.json()

    async def ultimo_id(self) -> int:
        return int((await self._get(_MAX_ID, 1)).get("ultimo_id") or 0)

    async def leer(self, desde_id: int, limite: int = 100) -> list:
        return (await self._get(desde_id, limite)).get("senales") or []

    async def cerrar(self) -> None:
        await self._http.aclose()
