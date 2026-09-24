#!/usr/bin/env bash
# Pruebas sin red ni Discord. La de contrato necesita FT_INTEL_REPO.
set -u
cd "$(dirname "$0")/.."
# El intérprete: el del servicio (/opt/venv) si existe. En el servidor,
# `python3` es el del sistema y no tiene las librerías (httpx): así fallaban
# dos suites sin mostrar por qué (22/09/2026). PYTHON lo puede fijar a mano.
PY="${PYTHON:-}"
if [ -z "$PY" ]; then
  if [ -x /opt/venv/bin/python ]; then PY=/opt/venv/bin/python; else PY=python3; fi
fi
fallas=0
for t in tests/test_bot.py tests/test_voz.py tests/test_mto_vivo.py tests/test_contrato_ft_intelligence.py; do
  echo "=== $t"
  salida=$("$PY" "$t" 2>/tmp/prueba_error.txt)
  codigo=$?
  echo "$salida" | tail -1
  if [ "$codigo" -ne 0 ]; then
    fallas=$((fallas + 1))
    echo "  (falló; últimas líneas del error:)"
    tail -5 /tmp/prueba_error.txt | sed 's/^/    /'
  fi
done
echo
if [ "$fallas" -eq 0 ]; then echo "TODO OK"; else echo "$fallas suite(s) con fallas"; exit 1; fi
