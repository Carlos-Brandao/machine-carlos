"""Registro explícito de adapters transacionais e capacidade dos pools."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable

from services.registry import MUNICIPALITIES, PLATFORMS
from workers.adapters.consiglog import ConsiglogAdapter
from workers.adapters.facil import FacilAdapter
from workers.adapters.rf1 import RF1Adapter
from workers.engine import PortalAdapter


@dataclass(frozen=True, slots=True)
class AdapterRegistration:
    platform: str
    factory: Callable[[], PortalAdapter] | None
    available: bool
    reason: str | None = None


ADAPTERS: dict[str, AdapterRegistration] = {
    "rf1": AdapterRegistration("rf1", RF1Adapter, True),
    "facil": AdapterRegistration("facil", FacilAdapter, True),
    "consiglog": AdapterRegistration("consiglog", ConsiglogAdapter, True),
    "safeconsig": AdapterRegistration(
        "safeconsig",
        None,
        False,
        "SAFE possui apenas runner legado; adapter transacional não implementado.",
    ),
    "grid": AdapterRegistration(
        "grid",
        None,
        False,
        "Grid possui apenas runner legado; adapter transacional não implementado.",
    ),
    "easyconsig": AdapterRegistration(
        "easyconsig", None, False, "EasyConsig não possui runner implementado."
    ),
}


def configured_platforms() -> tuple[str, ...]:
    """Retorna pools habilitados no catálogo e, opcionalmente, no ambiente."""

    configured = [
        slug
        for slug, registration in ADAPTERS.items()
        if registration.available and PLATFORMS.get(slug) and PLATFORMS[slug].enabled
    ]
    raw = os.getenv("WORKER_PLATFORMS", "").strip()
    if not raw:
        return tuple(configured)
    requested = tuple(
        dict.fromkeys(value.strip().lower() for value in raw.split(",") if value.strip())
    )
    unknown = [slug for slug in requested if slug not in ADAPTERS]
    unavailable = [
        slug for slug in requested if slug in ADAPTERS and not ADAPTERS[slug].available
    ]
    if unknown:
        raise ValueError("Adapters desconhecidos: " + ", ".join(unknown))
    if unavailable:
        details = "; ".join(
            f"{slug}: {ADAPTERS[slug].reason}" for slug in unavailable
        )
        raise ValueError("Adapters indisponíveis: " + details)
    return tuple(slug for slug in requested if slug in configured)


def create_adapter(platform: str) -> PortalAdapter:
    try:
        registration = ADAPTERS[platform]
    except KeyError as exc:
        raise ValueError(f"Adapter desconhecido: {platform}") from exc
    if not registration.available or registration.factory is None:
        raise ValueError(registration.reason or f"Adapter {platform} indisponível.")
    return registration.factory()


def default_worker_count(platform: str) -> int:
    capacities = [
        municipality.max_workers
        for municipality in MUNICIPALITIES.values()
        if municipality.enabled and municipality.platform_slug == platform
    ]
    # Um pool atende vários convênios da mesma processadora. Somar os limites
    # permite, por exemplo, GOV AM e Paulista rodarem ao mesmo tempo; o backend
    # continua sendo a autoridade e nunca libera mais sessões que acessos/itens.
    catalog_default = sum(capacities) or 1
    raw = os.getenv(f"WORKER_COUNT_{platform.upper()}", str(catalog_default))
    try:
        return max(1, min(int(raw), 20))
    except ValueError as exc:
        raise ValueError(
            f"WORKER_COUNT_{platform.upper()} deve ser um número inteiro."
        ) from exc
