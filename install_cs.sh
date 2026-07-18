#!/bin/bash
# Canonical CS spoke installer entrypoint.
#
# install_all.sh runs each spoke's installer as `bash /opt/lm/<mod>/<installer>`
# (its MODULES map: ["cs"]="install_cs.sh"), and every other spoke ships its
# installer at the repo top level. The curl one-liner in the installer header
# also points at .../cs/main/install_cs.sh. This wrapper satisfies both so the
# CS spoke installs/updates correctly under install_all.sh — without it the
# modular step fails to find the installer, the venv isn't recreated, and the
# spoke crash-loops with status=203/EXEC.
#
# The real installer logic lives at lm-spoke/install_cs.sh (single source of
# truth); this file just execs it so there is nothing to keep in sync.
#
# When run via `curl ... | bash -s --` from an unrelated cwd there is no repo
# checkout beside us, so dirname("$0") is "." and ./lm-spoke/install_cs.sh
# won't exist. Fetch the real installer from GitHub in that case — it is
# self-contained (it git-clones the cs repo into /opt/lm/cs), so a tmp copy
# runs identically to a checkout-relative invocation.
set -euo pipefail
_real="$(dirname "$0")/lm-spoke/install_cs.sh"
if [ ! -f "$_real" ]; then
    _real="$(mktemp /tmp/lm-install-cs.XXXXXX.sh)"
    trap 'rm -f "$_real"' EXIT
    curl -sSL https://raw.githubusercontent.com/lbockenstedt/cs/main/lm-spoke/install_cs.sh -o "$_real"
fi
exec bash "$_real" "$@"