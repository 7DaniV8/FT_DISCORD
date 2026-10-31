#!/usr/bin/env python3
"""tests/test_prob_candidato.py — 🎲 FT PROBABILIDAD en Discord (30/10/2026): texto y voz de los dos avisos."""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ft_discord import config  # noqa: E402
from ft_discord.anunciador import Anunciador  # noqa: E402
from ft_discord.textos import texto_canal, texto_voz  # noqa: E402

_fallos, _ok = [], 0


def check(n, cond, det=""):
    global _ok
    if cond:
        _ok += 1
        print(f"  OK    {n}")
    else:
        _fallos.append(n)
        print(f"  FALLA {n}" + (f": {det}" if det else ""))


AHORA = datetime(2026, 10, 30, 20, 0, tzinfo=timezone.utc)
det = {"id": 31, "tipo": "PROB_CANDIDATO", "jugador1": "Billy Harris", "jugador2": "Jose Pereira",
       "datos": {"texto_discord": "🎲 FULLTENNIS · FT PROBABILIDAD · CANDIDATO\n🎾 Billy Harris vs Jose Pereira\n✅ Candidato: **Billy Harris**\n"
                                  "📈 Probabilidad del modelo: 64,2 %\n💰 Cuota: 1.82  ·  valor esperado +16,8 %",
                 "texto_voz": "Atención FullTennis. FT Probabilidad detecta un candidato. Billy Harris, sesenta y cuatro por ciento. "
                              "Cuota uno punto ochenta y dos. Billy Harris contra Jose Pereira. Modelo experimental."}}
ini = {**det, "id": 32, "tipo": "PROB_CANDIDATO_INICIO",
       "datos": {"texto_discord": det["datos"]["texto_discord"].replace("CANDIDATO", "EMPIEZA EN 10 MIN", 1),
                 "texto_voz": "Atención FullTennis. En diez minutos, Billy Harris contra Jose Pereira. FT Probabilidad: Billy Harris, "
                              "sesenta y cuatro por ciento. Cuota uno punto ochenta y dos. Modelo experimental."}}

print("\n1. Texto y voz")
for s in (det, ini):
    t = texto_canal(s)
    check(f"{s['tipo']}: texto de RankingFTR tal cual, encabezado en negrita",
          t.startswith("**🎲 FULLTENNIS · FT PROBABILIDAD") and "✅ Candidato: **Billy Harris**" in t and "💰 Cuota: 1.82" in t, t)
    check(f"{s['tipo']}: la voz es la frase de RankingFTR, tal cual", texto_voz(s, "UTC", AHORA) == s["datos"]["texto_voz"])
check("la voz de inicio dice los minutos", texto_voz(ini, "UTC", AHORA).startswith("Atención FullTennis. En diez minutos,"))

print("\n2. Por defecto (sin configuración remota)")
check("detectado: solo texto", "PROB_CANDIDATO" in config.TIPOS_TEXTO and "PROB_CANDIDATO" not in config.TIPOS_VOZ)
check("al empezar: texto y voz", "PROB_CANDIDATO_INICIO" in config.TIPOS_TEXTO and "PROB_CANDIDATO_INICIO" in config.TIPOS_VOZ)


print("\n3. Admin → Discord manda sobre las variables")
CR = "2026-10-30T19:59:00+00:00"


class FuenteFalsa:
    def __init__(self, senales, cfg):
        self.s, self.cfg = senales, cfg

    async def ultimo_id(self):
        return 0

    async def leer(self, desde, limite=100):
        return [x for x in self.s if x["id"] > desde]

    async def configuracion(self):
        return self.cfg


def cfg(det_texto=1, det_voz=0, ini_texto=1, ini_voz=1, cab=None, voz_inicio=None, pausa=0):
    return {"pausa": pausa, "tipos": {
        "PROB_CANDIDATO": {"texto": det_texto, "voz": det_voz, "encabezado": None, "voz_inicio": None},
        "PROB_CANDIDATO_INICIO": {"texto": ini_texto, "voz": ini_voz, "encabezado": cab, "voz_inicio": voz_inicio}}}


async def escenario(conf):
    textos, voces = [], []

    async def publicar(t):
        textos.append(t)

    def encolar(t):
        voces.append(t)
        return True
    a = Anunciador(FuenteFalsa([{**det, "creado_en": CR}, {**ini, "creado_en": CR}], conf), publicar, encolar, "America/Bogota",
                   set(), set(), 30, reloj=lambda: AHORA)
    await a.arrancar(0)
    await a.ciclo()
    return textos, voces


t1, v1 = asyncio.run(escenario(cfg()))
check("de fábrica: los dos por texto; solo el de inicio por voz", len(t1) == 2 and len(v1) == 1 and v1[0].startswith("Atención FullTennis. En diez minutos"))
t2, v2 = asyncio.run(escenario(cfg(det_voz=1)))
check("Admin enciende la voz del detectado", len(t2) == 2 and len(v2) == 2)
t3, v3 = asyncio.run(escenario(cfg(ini_voz=0)))
check("Admin apaga la voz del inicio: nadie habla", len(t3) == 2 and v3 == [])
t4, v4 = asyncio.run(escenario(cfg(det_texto=0, det_voz=0)))
check("Admin apaga el detectado: solo sale el de inicio", len(t4) == 1 and "EMPIEZA EN 10 MIN" in t4[0])
t5, v5 = asyncio.run(escenario(cfg(cab="🎲 MI ENCABEZADO", voz_inicio="Ojo")))
check("Admin cambia el encabezado del inicio", any(x.startswith("**🎲 MI ENCABEZADO**") for x in t5))
check("Admin cambia la frase con la que arranca la voz", v5 and v5[0].startswith("Ojo. Atención FullTennis. En diez minutos"))
t6, v6 = asyncio.run(escenario(cfg(pausa=1)))
check("pausa general: no sale nada", t6 == [] and v6 == [])

print(f"\n{_ok} comprobaciones OK." if not _fallos else f"\n{len(_fallos)} FALLAS: {_fallos}")
sys.exit(1 if _fallos else 0)
