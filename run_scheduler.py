"""Compatibilidade: o scheduler antigo agora inicia o pool RF1 transacional."""

from __future__ import annotations

import sys

from run_worker import main


if __name__ == "__main__":
    if len(sys.argv) == 1:
        sys.argv.extend(["rf1", "--workers", "3"])
    main()
