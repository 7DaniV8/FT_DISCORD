"""
production/engines/discord_config.py — 📣 Admin → Discord (08/10/2026, Rubén:
"desde Admin poder activar, desactivar y modificar todo lo que llega a
Discord").

UNA sola pantalla para TODO lo que FT Discord dice: por cada tipo de aviso,
📝 texto sí/no, 🔊 voz sí/no, el encabezado del mensaje y una frase con la
que arranca la voz. Más una pausa general.

CÓMO VIAJA (sin acoplar los servicios):
  RankingFTR guarda la configuración (tabla discord_config) y la publica en
  GET /ft-intel/discord-config. FT Intelligence (v13) la copia en cada
  pasada y la sirve en su propio GET /discord-config. FT Discord (v8) la
  pide en cada ciclo y la aplica ANTES de escribir o hablar:
    - tipo con texto=0 → no se escribe;  voz=0 → no se dice;
    - pausa general → nada sale (las señales no se pierden: quedan en FT
      Intelligence; cuando se levanta la pausa, las nuevas salen; las viejas
      vencen por MAX_ANTIGUEDAD como siempre);
    - encabezado → reemplaza la primera línea del mensaje;
    - frase de voz → se dice antes de la frase del aviso.
  Si FT Discord no consigue la configuración (FT Intelligence viejo o
  caído), sigue con sus variables de Railway de siempre (FT_DISCORD_TIPOS_*).

CATALOGO: lo que FT Discord sabe anunciar hoy, con los defaults que hoy
tiene en código (los tipos que hoy no suenan arrancan apagados).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Optional

GLOBAL = "__GLOBAL__"

# (tipo, nombre, descripción, texto_default, voz_default, encabezado_default, grupo)
CATALOGO = (
    ("CUOTA_MAL_PUESTA", "🔥 Cuota Mal Puesta", "Las 🔥/💎 que pasan los controles; se dicen cuando el partido empieza.",
     1, 1, "🔥 FULLTENIS · CUOTA MAL PUESTA", "Estrategias"),
    ("UTR_VALUE", "📈 UTR VALUE", "Jugador superior por UTR que el mercado paga alto; al empezar el partido.",
     1, 1, "📈 FULLTENIS · UTR VALUE", "Estrategias"),
    ("UTR_MARKET_ANOMALY", "🚨 UTR Market Anomaly", "Anomalía de mercado por UTR; en vivo en el acto, pre-partido al empezar.",
     1, 1, "🚨 FULLTENIS · UTR MARKET ANOMALY", "Estrategias"),
    ("VENTAJA_LEVE", "⚖️ Ventaja Leve", "Reglas VL01–VL05 que sonaron en RankingFTR.",
     1, 1, "⚖️ FULLTENIS · VENTAJA LEVE", "Estrategias"),
    ("PASAN_FILTRO", "🎯 Pasan Filtro", "Tendencia UNDER/OVER por sets cuando hay una regla activa.",
     1, 1, "🎯 FULLTENIS · PASAN FILTRO", "Estrategias"),
    ("MAESTRO_AVISO", "🎓 Partido Maestro (aviso)", "Aviso previo de un Partido Maestro. Hoy apagado en Discord.",
     0, 0, "🎓 FULLTENIS · PARTIDO MAESTRO", "Estrategias"),
    ("MAESTRO_INICIO", "🎓 Partido Maestro (empezó)", "Cuando empieza un Partido Maestro. Hoy apagado en Discord.",
     0, 0, "🎓 FULLTENIS · EMPEZÓ EL MAESTRO", "Estrategias"),
    # v54f (14/10/2026, Rubén: «los Under deben sonar en Discord»): encendido de fábrica; solo los que salieron a
    # Telegram. Cada fila de ▼ Under en Vivo muestra si llegó (texto / voz) o por qué no.
    ("UNDER_PICK", "▼ Under", "Pick UNDER que salió a Telegram (Under en Vivo). En el acto, texto y voz.", 1, 1,
     "▼ FULLTENIS · UNDER", "Estrategias"),
    # v54h (14/10/2026, Rubén: «que suene Movimiento Rentable en Discord, las mismas que llegan a Telegram»)
    ("MOVIMIENTO_RENTABLE", "📉 Movimiento Rentable", "Los que salieron a Telegram; minutos antes o al empezar el partido.",
     1, 1, "📉 FULLTENIS · MOVIMIENTO RENTABLE", "Estrategias"),
    # v54i (15/10/2026, Rubén: «cuando las condiciones se cumplan, quiero que pueda mandar la señal a Discord»)
    ("UNDER_UTR", "🔥 Under UTR", "Solo pre-partido: ΔUTR ≥ 1.70, UNDER ≥ 16.5 y cuota ≥ 1.70 antes del primer punto. En el acto, texto y voz.",
     1, 1, "🔥 FULLTENNIS · UNDER UTR", "Estrategias"),
    ("MTO_VIVO", "🩹 Tiempo médico en vivo", "\"Tiempo médico solicitado por X\", apenas llega el MTO.",
     1, 1, "🩹 TIEMPO MÉDICO", "Avisos"),
    ("RECORDADOR", "⏰ Recordador", "El próximo partido de un jugador que pidió un usuario: minutos antes o en juego (formato de siempre).",
     1, 1, "⏰ RECORDADOR", "Avisos"),
    # v43 (11/10/2026, Rubén): recordatorios inteligentes. Salen solo con RECORDADOR_IA_DISCORD=1 (Admin → Estrategias).
    ("RECORDADOR_IA", "🩹 Recordatorio IA (5 min antes)", "Investigación de los dos jugadores, observación del usuario, probabilidad FullTennis BASE y conclusión.",
     1, 1, "🩹 FULLTENNIS · RECORDATORIO IA", "Avisos"),
    ("RECORDADOR_VIVO", "🔴 Recordatorio en vivo (inicio real)", "El segundo aviso, corto, cuando el partido realmente empieza.",
     1, 1, "🔴 FULLTENNIS · PARTIDO EN VIVO", "Avisos"),
    ("REVISION_VOZ", "🗣 Aviso de voz (revisión)", "La revisión del partido 10 minutos antes: ficha por texto, conclusión por voz.",
     1, 1, "📊 REVISIÓN DE CUOTA", "Avisos"),
    ("RADAR_VOZ", "📡 FT Market Anomaly (radar)", "Segundo motor de FT Intelligence. Hoy apagado en Discord.",
     0, 0, "📡 FT MARKET ANOMALY", "Avisos"),
    ("APRENDIZAJE", "🧠 Aprendizaje (hitos)", "Hitos del aprendizaje del radar. Solo texto.",
     1, 0, "🧠 APRENDIZAJE", "Avisos"),
    # 📰 FT NEWS INTELLIGENCE (08/10/2026): el investigador de noticias (servicio FT_NEWS).
    # 09/10/2026 23:13 (Rubén): las tres encendidas de fábrica, formato corto con el
    # favorecido y su cuota. El encabezado de la 🔥 lo pone FT_NEWS (dice EXPERIMENTAL
    # mientras las bandas no estén validadas); FT Discord no lo pisa.
    ("NEWS_VALOR", "📰🔥 FT NEWS · Posible valor (validado o EXPERIMENTAL)",
     "Noticia externa fuerte, cuota confirmada después de la noticia que no se movió, los dos investigados. En el acto.",
     1, 1, "🔥 FULLTENNIS · POSIBLE VALOR EXPERIMENTAL", "FT NEWS"),
    ("NEWS_INTERESANTE", "📰🔎 FT NEWS · Partido interesante",
     "Partido con noticia externa relevante y los dos jugadores investigados; dice a quién podría favorecer y qué falta.",
     1, 1, "🔎 FULLTENNIS · PARTIDO INTERESANTE", "FT NEWS"),
    ("NEWS_NOTICIA", "📰 FT NEWS · Noticia importante",
     "Noticia externa verificada sobre un jugador (lesión, enfermedad, retiro, regreso…), con o sin partido.",
     1, 1, "📰 FULLTENNIS · NOTICIA IMPORTANTE", "FT NEWS"),
    # 🏆 IMPLICACIÓN DE TORNEO (v45, 13/10/2026). Solo viajan los picks de reglas VALIDADAS y
    # solo con IMP_DISCORD_ACTIVO = 1 (Admin → Estrategias, apagado de fábrica); los 🧪
    # experimentales NUNCA. Las pruebas del circuito salen marcadas 🧪 PRUEBA.
    ("IMPLICACION", "🏆 Implicación de Torneo", "Favorito PRE que pierde el 1.er set defendiendo puntos (regla validada). En el acto.",
     1, 1, "🏆 FULLTENNIS · IMPLICACIÓN DE TORNEO", "Estrategias"),
    # 🎲 FT PROBABILIDAD (30/10/2026, Daniel): jugador con probabilidad del modelo > FT_PROB_CAND_PROB_MIN y cuota
    # actual > FT_PROB_CAND_CUOTA_MIN. Dos avisos como máximo por jugador y partido; cada uno se gobierna aparte.
    ("PROB_CANDIDATO", "🎲 FT Probabilidad · candidato detectado",
     "Aviso en cuanto un partido cumple la regla (probabilidad y cuota mínimas del Admin). De fábrica solo texto.",
     1, 0, "🎲 FULLTENNIS · FT PROBABILIDAD · CANDIDATO", "Estrategias"),
    ("PROB_CANDIDATO_INICIO", "🎲 FT Probabilidad · candidato al empezar",
     "Aviso corto antes del inicio (minutos del Admin), solo si con la cuota de ese momento todavía cumple.",
     1, 1, "🎲 FULLTENNIS · FT PROBABILIDAD · EMPIEZA PRONTO", "Estrategias"),
)
TIPOS = tuple(c[0] for c in CATALOGO)
_TABLA = False


def ensure_tabla(conn: sqlite3.Connection) -> None:
    global _TABLA
    if _TABLA:
        return
    conn.execute("""CREATE TABLE IF NOT EXISTS discord_config (
                        tipo            TEXT PRIMARY KEY,
                        texto           INTEGER NOT NULL DEFAULT 1,
                        voz             INTEGER NOT NULL DEFAULT 1,
                        encabezado      TEXT,
                        voz_inicio      TEXT,
                        actualizado_en  TEXT NOT NULL,
                        actualizado_por TEXT)""")
    conn.commit()
    _TABLA = True


def _guardadas(conn) -> dict:
    ensure_tabla(conn)
    return {r["tipo"]: dict(r) for r in conn.execute("SELECT * FROM discord_config").fetchall()}


def listar(conn: sqlite3.Connection) -> dict:
    """Para la pantalla: catálogo + lo guardado."""
    g = _guardadas(conn)
    glob = g.get(GLOBAL) or {}
    tipos = []
    for tipo, nombre, desc, t_def, v_def, cab_def, grupo in CATALOGO:
        f = g.get(tipo) or {}
        tipos.append({
            "tipo": tipo, "nombre": nombre, "descripcion": desc, "grupo": grupo,
            "texto": int(f.get("texto", t_def)), "voz": int(f.get("voz", v_def)),
            "texto_default": t_def, "voz_default": v_def,
            "encabezado": f.get("encabezado") or "", "encabezado_default": cab_def,
            "voz_inicio": f.get("voz_inicio") or "",
            "actualizado_en": f.get("actualizado_en"), "actualizado_por": f.get("actualizado_por"),
            "personalizado": bool(f)})
    return {"pausa": int(glob.get("texto", 1) == 0), "pausa_en": glob.get("actualizado_en"),
            "pausa_por": glob.get("actualizado_por"), "tipos": tipos}


def guardar(conn: sqlite3.Connection, tipo: str, texto: Optional[bool], voz: Optional[bool],
            encabezado: Optional[str], voz_inicio: Optional[str], por: str) -> dict:
    from production.api.deps import ejecutar_con_reintento
    if tipo not in TIPOS:
        raise ValueError(f"Tipo desconocido: {tipo}")
    ensure_tabla(conn)
    actual = _guardadas(conn).get(tipo) or {}
    defaults = next(c for c in CATALOGO if c[0] == tipo)
    t = int(actual.get("texto", defaults[3]) if texto is None else bool(texto))
    v = int(actual.get("voz", defaults[4]) if voz is None else bool(voz))
    cab = (encabezado if encabezado is not None else actual.get("encabezado")) or None
    vi = (voz_inicio if voz_inicio is not None else actual.get("voz_inicio")) or None
    if cab and len(cab) > 120:
        raise ValueError("El encabezado no puede pasar de 120 caracteres.")
    if vi and len(vi) > 200:
        raise ValueError("La frase de voz no puede pasar de 200 caracteres.")
    ahora = datetime.now(timezone.utc).isoformat()

    def _w():
        conn.execute("INSERT INTO discord_config (tipo, texto, voz, encabezado, voz_inicio, actualizado_en, "
                     "actualizado_por) VALUES (?,?,?,?,?,?,?) ON CONFLICT(tipo) DO UPDATE SET texto=excluded.texto, "
                     "voz=excluded.voz, encabezado=excluded.encabezado, voz_inicio=excluded.voz_inicio, "
                     "actualizado_en=excluded.actualizado_en, actualizado_por=excluded.actualizado_por",
                     (tipo, t, v, cab.strip() if cab else None, vi.strip() if vi else None, ahora, por or ""))
        conn.commit()
    ejecutar_con_reintento(_w, conn=conn, con_lock=True)
    return listar(conn)


def restablecer(conn: sqlite3.Connection, tipo: str) -> dict:
    """Vuelve a los valores de fábrica de ese tipo."""
    from production.api.deps import ejecutar_con_reintento
    ensure_tabla(conn)

    def _w():
        conn.execute("DELETE FROM discord_config WHERE tipo=?", (tipo,))
        conn.commit()
    ejecutar_con_reintento(_w, conn=conn, con_lock=True)
    return listar(conn)


def pausar(conn: sqlite3.Connection, pausa: bool, por: str) -> dict:
    """Pausa general: con pausa=True nada sale a Discord (texto ni voz)."""
    from production.api.deps import ejecutar_con_reintento
    ensure_tabla(conn)
    ahora = datetime.now(timezone.utc).isoformat()

    def _w():
        conn.execute("INSERT INTO discord_config (tipo, texto, voz, actualizado_en, actualizado_por) "
                     "VALUES (?,?,?,?,?) ON CONFLICT(tipo) DO UPDATE SET texto=excluded.texto, voz=excluded.voz, "
                     "actualizado_en=excluded.actualizado_en, actualizado_por=excluded.actualizado_por",
                     (GLOBAL, 0 if pausa else 1, 0 if pausa else 1, ahora, por or ""))
        conn.commit()
    ejecutar_con_reintento(_w, conn=conn, con_lock=True)
    return listar(conn)


def para_discord(conn: sqlite3.Connection) -> dict:
    """Lo que viaja a FT Intelligence → FT Discord. Compacto y completo:
    TODOS los tipos del catálogo con su valor efectivo."""
    lst = listar(conn)
    return {"version": 1, "generado_en": datetime.now(timezone.utc).isoformat(),
            "pausa": bool(lst["pausa"]),
            "tipos": {t["tipo"]: {"texto": bool(t["texto"]), "voz": bool(t["voz"]),
                                  "encabezado": t["encabezado"] or None, "voz_inicio": t["voz_inicio"] or None}
                      for t in lst["tipos"]}}
