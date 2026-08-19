#!/usr/bin/env bash
# Render each slide of presentation.html to a PNG in docs/slides/.
#
#   ./docs/export-slides.sh            # 2x scale (2880x1800), good for print
#   SCALE=1 ./docs/export-slides.sh    # 1440x900
#
# The deck is one self-contained HTML file with inline SVG charts, so each
# slide is rendered by isolating its <section> with the deck's own <style> and
# screenshotting it headlessly. Nothing is redrawn by hand — the PNGs are the
# slides, so they cannot drift from the talk.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DECK="$HERE/presentation.html"
OUT="$HERE/slides"
SCALE="${SCALE:-2}"
W="${W:-1440}"
H="${H:-900}"

CHROME="${CHROME:-}"
if [[ -z "$CHROME" ]]; then
  for c in "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
           "$(command -v google-chrome || true)" \
           "$(command -v chromium || true)" \
           "$(command -v chromium-browser || true)"; do
    [[ -n "$c" && -x "$c" ]] && { CHROME="$c"; break; }
  done
fi
[[ -n "$CHROME" ]] || { echo "No Chrome/Chromium found. Set CHROME=/path/to/chrome" >&2; exit 1; }

PY="$(command -v python3)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

rm -rf "$OUT"; mkdir -p "$OUT"

# Split the deck into per-slide documents, each keeping the original preamble
# (fonts and CSS live there, and the fonts are inlined as base64).
"$PY" - "$DECK" "$TMP" <<'PY'
import pathlib, re, sys
deck, tmp = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
text = deck.read_text()
preamble = text[:text.index("<section>")]
slides = re.findall(r"<section>.*?</section>", text, re.S)
for i, s in enumerate(slides, 1):
    title = re.search(r"<h2>(.*?)</h2>", s, re.S)
    title = re.sub(r"<[^>]+>", "", title.group(1)).strip() if title else f"slide {i}"
    # One slide per file; min-height:100vh already sizes it to the window.
    (tmp / f"slide-{i:02d}.html").write_text(preamble + s + "</div></body>")
    print(f"{i:02d}\t{title[:60]}")
PY

n=0
for f in "$TMP"/slide-*.html; do
  base="$(basename "$f" .html)"
  "$CHROME" --headless --disable-gpu --hide-scrollbars \
    --window-size="$W,$H" --force-device-scale-factor="$SCALE" \
    --virtual-time-budget=5000 --run-all-compositor-stages-before-draw \
    --screenshot="$OUT/$base.png" "file://$f" 2>/dev/null
  [[ -s "$OUT/$base.png" ]] || { echo "  FAILED $base" >&2; continue; }
  n=$((n+1))
done

echo
echo "  $n slides -> $OUT  ($((W*SCALE))x$((H*SCALE)))"
ls -1 "$OUT" | sed 's/^/    /'
