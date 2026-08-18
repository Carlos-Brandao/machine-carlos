"""Shim compatível do ConsigX sobre o motor transacional comum."""

from __future__ import annotations

from workers.adapters.consiglog import ConsiglogAdapter
from workers.engine import GenericWorker


class ConsiglogWorker(GenericWorker):
    """Mantém o import antigo sem regras paralelas de execução."""

    def __init__(self, *args, **kwargs) -> None:
        kwargs.setdefault("adapter", ConsiglogAdapter())
        super().__init__(*args, **kwargs)


LegacyConsiglogWorker = ConsiglogWorker
