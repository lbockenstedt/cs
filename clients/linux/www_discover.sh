#!/bin/bash
version=0.01
log="/usr/local/scripts/sim.log"
debug="/usr/local/scripts/debug-www-discover.log"
echo WWW_Discover Script Version $version | tee "$debug" | tee -a "$log"
# No config helpers are needed: the orchestrator owns the on/off flag.
list=/usr/local/scripts/www_discover_websites.txt
if [[ ! -r "$list" ]]; then
  echo "Unable to read $list — skipping" | tee -a "$debug" "$log"
  exit 0
fi
# Ignore blank lines and use shuf for a uniform pick, without modulo bias.
url=$(sed '/^[[:space:]]*$/d; s/\r$//' "$list" | shuf -n 1)
if [[ -z "$url" ]]; then
  echo "No websites listed in $list — skipping" | tee -a "$debug" "$log"
  exit 0
fi
if ! command -v firefox-esr >/dev/null 2>&1; then
  echo "Firefox was not found in PATH — skipping" | tee -a "$debug" "$log"
  exit 0
fi
echo "Launching Firefox headless for $url" | tee -a "$debug" "$log"
# Keep cpulimit in the foreground so timeout owns the entire launch group.
# Kill-after also bounds a Firefox that ignores TERM; never kill by name.
if command -v cpulimit >/dev/null 2>&1; then
  timeout -k 5 60 cpulimit -f -l 10 -- firefox-esr --headless "$url" >> "$debug" 2>&1
else
  timeout -k 5 60 firefox-esr --headless "$url" >> "$debug" 2>&1
fi
exit 0
