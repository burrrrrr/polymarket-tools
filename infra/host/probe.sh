#!/bin/bash
# Where does Polymarket see this machine, and how far away is its order book?
# Run it on a throwaway VM before choosing a location. It needs bash and curl,
# sends read-only GETs and no key, and works piped from curl into bash.
set -euo pipefail

CLOB=https://clob.polymarket.com
SAMPLES=10

echo "Edge the order book answers from:"
curl -fsS --max-time 20 "$CLOB/cdn-cgi/trace" | grep -E '^(colo|loc)=' | sed 's/^/  /'

echo "Geoblock verdict:"
geoblock=$(curl -sS --max-time 20 https://polymarket.com/api/geoblock)
case $geoblock in
    *'"blocked":true'*) echo "  BLOCKED: orders from this location are refused." ;;
    *'"blocked":false'*) echo "  allowed" ;;
    *) echo "  no verdict in the answer" ;;
esac
echo "  $(printf '%s' "$geoblock" | sed -E 's/"ip":"[^"]*",?//')"

# One curl given the URL several times keeps one connection open, so only the
# first request pays for DNS, TCP and the TLS handshake.
requests=()
for _ in $(seq $((SAMPLES + 1))); do
    requests+=(--output /dev/null "$CLOB/time")
done
timings=$(curl -sS --max-time 60 --write-out '%{time_total} %{http_code}\n' "${requests[@]}")

echo "GET $CLOB/time, $SAMPLES requests on one connection:"
printf '%s\n' "$timings" | awk 'NR == 1 { printf "  first request, with the handshake: %.0f ms\n", $1 * 1000 }'
printf '%s\n' "$timings" | awk 'NR > 1 && $2 == 200 { print $1 * 1000 }' | sort -n | awk '
    { ms[NR] = $1 }
    END {
        if (NR == 0) { print "  no request answered 200"; exit 1 }
        median = NR % 2 ? ms[(NR + 1) / 2] : (ms[NR / 2] + ms[NR / 2 + 1]) / 2
        printf "  min %.0f ms, median %.0f ms, max %.0f ms over %d answers\n", ms[1], median, ms[NR], NR
    }'
