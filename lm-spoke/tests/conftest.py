"""pytest path bootstrap for the cs lm-spoke test suite.

The spoke's sibling modules (``cs_spoke``, ``client_api``, ``simulation_engine``,
``sim_config``, …) are imported as *bare* names, so ``lm-spoke/src`` must be on
``sys.path``. ``CSSpoke`` also inherits ``BaseSpoke`` from the LM ``core`` repo
(``core.src.base_spoke``), so core's parent dir must be on the path too — in dev
that's the sibling ``lm`` repo (``vscode/lm/core``), in prod ``/opt/lm/core``
 alongside the cs checkout. This conftest locates it generically so the tests
run in both layouts.
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent        # lm-spoke/tests
LM_SPOKE = HERE.parent                         # lm-spoke
SRC = LM_SPOKE / "src"
for p in (str(SRC), str(LM_SPOKE)):
    if p not in sys.path:
        sys.path.insert(0, p)

CS_REPO = LM_SPOKE.parent                      # cs repo root
VSROOT = CS_REPO.parent                        # .../vscode (dev)
# Each candidate is the LM `core` package dir; inserting its PARENT makes
# `core.src.base_spoke` importable.
for cand in (VSROOT / "lm" / "core", CS_REPO / "core", VSROOT / "core"):
    if (cand / "src" / "base_spoke.py").is_file():
        core_parent = str(cand.parent)
        if core_parent not in sys.path:
            sys.path.insert(0, core_parent)
        break