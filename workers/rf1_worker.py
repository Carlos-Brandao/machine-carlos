"""Shim compatível do RF1 sobre o motor transacional comum."""

from __future__ import annotations

from workers.adapters.rf1 import RF1Adapter
from workers.engine import GenericWorker


class RF1Worker(GenericWorker):
    """Mantém o import antigo sem reintroduzir agenda ou fila locais."""

    def __init__(self, *args, **kwargs) -> None:
        kwargs.setdefault("adapter", RF1Adapter())
        super().__init__(*args, **kwargs)


LegacyRF1Worker = RF1Worker
