"""Adapters de portais disponíveis para o motor transacional."""

from workers.adapters.consiglog import ConsiglogAdapter
from workers.adapters.facil import FacilAdapter
from workers.adapters.rf1 import RF1Adapter

__all__ = ("RF1Adapter", "FacilAdapter", "ConsiglogAdapter")
