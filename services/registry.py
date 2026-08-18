"""Cadastro único de plataformas, convênios e runners disponíveis."""

from __future__ import annotations

from dataclasses import dataclass, field


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
    operational_status: str = "draft"
    timezone: str = "America/Fortaleza"
    input_schema: dict[str, object] = field(
        default_factory=lambda: {
            "version": 1,
            "required": ["cpf"],
            "optional": ["registration"],
            "deduplication_key": ["cpf", "registration"],
        }
    )
    schedule_policy: dict[str, object] = field(
        default_factory=lambda: {
            "weekdays": [0, 1, 2, 3, 4],
            "start_hour": None,
            "end_hour": None,
        }
    )
    adapter_version: str | None = None


PLATFORMS: dict[str, PlatformDefinition] = {
    # Boa Vista (RF1) está disponível 24/7; não deve ficar retido pela antiga
    # janela comercial aplicada aos demais portais.
    "rf1": PlatformDefinition("rf1", "RF1", "rf1", start_hour=0, end_hour=24),
    "facil": PlatformDefinition("facil", "FACILCONSIG", "facil"),
    "safeconsig": PlatformDefinition(
        "safeconsig", "SAFE", "safeconsig", end_hour=18
    ),
    "grid": PlatformDefinition("grid", "Grid", "grid"),
    "consiglog": PlatformDefinition("consiglog", "CONSIGX", "consiglog"),
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
        operational_status="ready",
        timezone="America/Boa_Vista",
        input_schema={
            "version": 1,
            "required": ["cpf"],
            "optional": ["registration"],
            "deduplication_key": ["cpf"],
        },
        schedule_policy={
            "weekdays": [0, 1, 2, 3, 4, 5, 6],
            "start_hour": 0,
            "end_hour": 24,
        },
        adapter_version="rf1.v1",
    ),
    "pref2": MunicipalityDefinition("pref2", "Prefeitura 2", "rf1", enabled=False),
    "fortaleza": MunicipalityDefinition(
        "fortaleza", "Fortaleza", "safeconsig", adapter_version="safeconsig.legacy"
    ),
    "maranguape": MunicipalityDefinition(
        "maranguape", "Maranguape", "safeconsig", adapter_version="safeconsig.legacy"
    ),
    "teresina": MunicipalityDefinition(
        "teresina", "Teresina", "facil", adapter_version="facil.v1"
    ),
    "gov-am": MunicipalityDefinition(
        "gov-am",
        "GOV AM",
        "facil",
        login_url="https://faciltecnologia.com.br/consigfacil/amazonas",
        query_url="https://faciltecnologia.com.br/consigfacil/amazonas",
        operational_status="ready",
        timezone="America/Manaus",
        input_schema={
            "version": 1,
            "required": ["cpf"],
            "optional": ["registration"],
            "deduplication_key": ["cpf"],
        },
        adapter_version="facil.v1",
    ),
    "paulista": MunicipalityDefinition(
        "paulista",
        "Paulista",
        "facil",
        login_url="https://www.faciltecnologia.com.br/consigfacil/paulista",
        query_url="https://www.faciltecnologia.com.br/consigfacil/paulista",
        operational_status="ready",
        input_schema={
            "version": 1,
            "required": ["cpf", "registration"],
            "optional": [],
            "deduplication_key": ["cpf", "registration"],
        },
        adapter_version="facil.v1",
    ),
    "paulista-previdencia": MunicipalityDefinition(
        "paulista-previdencia",
        "Paulista Previdência",
        "facil",
        adapter_version="facil.v1",
    ),
    "mossoro": MunicipalityDefinition(
        "mossoro", "Mossoró", "facil", adapter_version="facil.v1"
    ),
    "itabuna": MunicipalityDefinition(
        "itabuna",
        "Itabuna",
        "consiglog",
        login_url="https://saec.consigx.com.br/Login.aspx",
        query_url="https://saec.consigx.com.br/Margem/ConsultaMargem.aspx",
        operational_status="testing",
        adapter_version="consiglog.v1",
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
