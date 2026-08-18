"""Shim compatível do FACILCONSIG sobre o motor transacional comum."""

from __future__ import annotations

from workers.adapters.facil import FacilAdapter
from workers.engine import GenericWorker


class FacilWorker(GenericWorker):
    """Mantém o import antigo sem regras paralelas de execução."""

    def __init__(self, *args, **kwargs) -> None:
        kwargs.setdefault("adapter", FacilAdapter())
        super().__init__(*args, **kwargs)


LegacyFacilWorker = FacilWorker
