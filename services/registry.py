"""Cadastro único de plataformas, convênios e runners disponíveis."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PlatformDefinition:
    slug: str
    name: str
    runner: str
    start_hour: int = 7
    end_hour: int = 21
    enabled: bool = True


@dataclass(frozen=True)
class MunicipalityDefinition:
    slug: str
    name: str
    platform_slug: str
    enabled: bool = True
    login_url: str | None = None
    query_url: str | None = None
    max_workers: int = 1


PLATFORMS: dict[str, PlatformDefinition] = {
    "rf1": PlatformDefinition("rf1", "RF1", "rf1"),
    "facil": PlatformDefinition("facil", "FácilConsig", "facil"),
    "safeconsig": PlatformDefinition(
        "safeconsig", "SafeConsig", "safeconsig", end_hour=18
    ),
    "grid": PlatformDefinition("grid", "Grid", "grid"),
    "consiglog": PlatformDefinition("consiglog", "Consiglog", "consiglog"),
    # O controlador antigo anunciava EasyConsig, mas não há runner no projeto.
    "easyconsig": PlatformDefinition(
        "easyconsig", "EasyConsig", "easyconsig", enabled=False
    ),
}

MUNICIPALITIES: dict[str, MunicipalityDefinition] = {
    "boa-vista": MunicipalityDefinition(
        "boa-vista",
        "Boa Vista",
        "rf1",
        login_url=(
            "https://boavista.rf1consig.com.br/SGConsignataria/"
            "ConsigAcessoUsuarioLogar.aspx"
        ),
        query_url=(
            "https://boavista.rf1consig.com.br/SGConsignataria/GESTOR/"
            "CADPessoaListar.aspx"
        ),
        max_workers=3,
    ),
    "pref2": MunicipalityDefinition("pref2", "Prefeitura 2", "rf1", enabled=False),
    "fortaleza": MunicipalityDefinition("fortaleza", "Fortaleza", "safeconsig"),
    "maranguape": MunicipalityDefinition("maranguape", "Maranguape", "safeconsig"),
    "teresina": MunicipalityDefinition("teresina", "Teresina", "facil"),
    "gov-am": MunicipalityDefinition("gov-am", "GOV AM", "facil"),
    "paulista": MunicipalityDefinition("paulista", "Paulista", "facil"),
    "paulista-previdencia": MunicipalityDefinition(
        "paulista-previdencia", "Paulista Previdência", "facil"
    ),
    "mossoro": MunicipalityDefinition("mossoro", "Mossoró", "facil"),
    "itabuna": MunicipalityDefinition(
        "itabuna",
        "Itabuna",
        "consiglog",
        login_url="https://saec.consigx.com.br/Login.aspx",
        query_url="https://saec.consigx.com.br/Margem/ConsultaMargem.aspx",
    ),
    "chapeco": MunicipalityDefinition(
        "chapeco", "Chapecó", "easyconsig", enabled=False
    ),
    "tamboril": MunicipalityDefinition(
        "tamboril", "Tamboril", "easyconsig", enabled=False
    ),
}


def enabled_platforms() -> tuple[PlatformDefinition, ...]:
    return tuple(platform for platform in PLATFORMS.values() if platform.enabled)


def enabled_municipalities() -> tuple[MunicipalityDefinition, ...]:
    return tuple(
        municipality
        for municipality in MUNICIPALITIES.values()
        if municipality.enabled and PLATFORMS[municipality.platform_slug].enabled
    )


def platform_for_municipality(slug: str) -> PlatformDefinition:
    try:
        municipality = MUNICIPALITIES[slug]
        return PLATFORMS[municipality.platform_slug]
    except KeyError as exc:
        raise ValueError(f"Prefeitura sem plataforma configurada: {slug}") from exc


def runner_names() -> tuple[str, ...]:
    return tuple(dict.fromkeys(platform.runner for platform in enabled_platforms()))
