#!/usr/bin/env bash
# Pruebas sin red ni Discord. La de contrato necesita FT_INTEL_REPO.
set -u
cd "$(dirname "$0")/.."
fallas=0
for t in tests/test_bot.py tests/test_voz.py tests/test_contrato_ft_intelligence.py; do
  echo "=== $t"
  salida=$(python3 "$t" 2>/dev/null)
  codigo=$?
  echo "$salida" | tail -1
  [ "$codigo" -ne 0 ] && fallas=$((fallas + 1))
done
echo
if [ "$fallas" -eq 0 ]; then echo "TODO OK"; else echo "$fallas suite(s) con fallas"; exit 1; fi
