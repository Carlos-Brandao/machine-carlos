"""Regras explicáveis de prontidão dos convênios.

Nenhuma tela ou integração deve inferir que ``enabled`` significa "pode
rodar". Este módulo devolve causas e ações concretas para cada bloqueio.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from machine_admin.models import (
    IntegrationSecret,
    Municipality,
    Platform,
    PortalCredential,
    WorkerHeartbeat,
)


TRANSACTIONAL_ADAPTERS = frozenset({"rf1", "facil", "consiglog", "safeconsig"})
REQUIRED_SECRETS: dict[str, frozenset[str]] = {
    "rf1": frozenset({"TWOCAPTCHA_API_KEY"}),
    "facil": frozenset({"TWOCAPTCHA_API_KEY"}),
    "safeconsig": frozenset(
        {"TWOCAPTCHA_API_KEY", "SAFECONSIG_PROXY"}
    ),
    "consiglog": frozenset(),
}


@dataclass(frozen=True, slots=True)
class ReadinessIssue:
    code: str
    message: str
    action: str
    severity: str = "blocking"

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    municipality_slug: str
    state: str
    can_start: bool
    issues: tuple[ReadinessIssue, ...]
    active_credentials: int
    online_workers: int

    @property
    def summary(self) -> str:
        if self.can_start:
            return "Pronto para iniciar consultas."
        return self.issues[0].message if self.issues else "Convênio indisponível."

    def as_dict(self) -> dict[str, object]:
        return {
            "municipality_slug": self.municipality_slug,
            "state": self.state,
            "can_start": self.can_start,
            "summary": self.summary,
            "issues": [issue.as_dict() for issue in self.issues],
            "active_credentials": self.active_credentials,
            "online_workers": self.online_workers,
        }


def _secret_exists(session: Session, key: str) -> bool:
    return bool(session.get(IntegrationSecret, key) or os.getenv(key, "").strip())


def assess_municipality(
    session: Session,
    municipality: Municipality,
    *,
    require_online_worker: bool = True,
) -> ReadinessReport:
    platform = session.get(Platform, municipality.platform_slug)
    now = datetime.now(UTC)
    active_credentials = int(
        session.scalar(
            select(func.count())
            .select_from(PortalCredential)
            .where(
                PortalCredential.municipality_slug == municipality.slug,
                or_(
                    PortalCredential.status == "active",
                    and_(
                        PortalCredential.status == "cooldown",
                        or_(
                            PortalCredential.cooldown_until.is_(None),
                            PortalCredential.cooldown_until <= now,
                        ),
                    ),
                ),
            )
        )
        or 0
    )
    worker_conditions = [
        WorkerHeartbeat.platform_slug == municipality.platform_slug,
        WorkerHeartbeat.health_status.in_(["healthy", "degraded"]),
        WorkerHeartbeat.expires_at > now,
    ]
    expected_adapter_version = (
        getattr(municipality, "adapter_version", None) or ""
    ).strip()
    platform_online_workers = int(
        session.scalar(
            select(func.count())
            .select_from(WorkerHeartbeat)
            .where(*worker_conditions)
        )
        or 0
    )
    if expected_adapter_version:
        worker_conditions.append(
            WorkerHeartbeat.adapter_version == expected_adapter_version
        )
        online_workers = int(
            session.scalar(
                select(func.count())
                .select_from(WorkerHeartbeat)
                .where(*worker_conditions)
            )
            or 0
        )
    else:
        online_workers = platform_online_workers
    issues: list[ReadinessIssue] = []

    if not municipality.enabled:
        issues.append(
            ReadinessIssue(
                "agreement_disabled",
                "O convênio está desativado.",
                "Ative o convênio na configuração operacional.",
            )
        )
    if not platform or not platform.enabled:
        issues.append(
            ReadinessIssue(
                "processor_disabled",
                "A processadora está desativada.",
                "Ative a processadora antes de liberar o convênio.",
            )
        )
    state = getattr(municipality, "operational_status", "draft") or "draft"
    if state != "ready":
        label = {
            "draft": "ainda está em configuração",
            "testing": "está em homologação",
            "degraded": "está degradado",
            "paused": "está pausado",
            "retired": "foi encerrado",
        }.get(state, f"está no estado {state}")
        issues.append(
            ReadinessIssue(
                f"agreement_{state}",
                f"O convênio {label}.",
                "Conclua o checklist e altere o estado para Pronto.",
            )
        )
    runner = platform.runner if platform else municipality.platform_slug
    if runner not in TRANSACTIONAL_ADAPTERS:
        issues.append(
            ReadinessIssue(
                "adapter_unavailable",
                "Não existe adaptador homologado para esta processadora.",
                "Implemente e teste o adaptador antes de aceitar jobs.",
            )
        )
    if not municipality.login_url or not municipality.query_url:
        issues.append(
            ReadinessIssue(
                "portal_urls_missing",
                "As URLs de login e consulta não estão completas.",
                "Preencha as duas URLs na configuração do convênio.",
            )
        )
    if active_credentials < 1:
        issues.append(
            ReadinessIssue(
                "credential_missing",
                "Não existe credencial utilizável para este convênio.",
                "Cadastre ou reative ao menos uma credencial.",
            )
        )
    for key in REQUIRED_SECRETS.get(runner, frozenset()):
        if not _secret_exists(session, key):
            issues.append(
                ReadinessIssue(
                    "integration_secret_missing",
                    f"A integração obrigatória {key} não está configurada.",
                    "Cadastre o segredo na página Integrações.",
                )
            )
    if require_online_worker and online_workers < 1:
        if expected_adapter_version and platform_online_workers:
            issues.append(
                ReadinessIssue(
                    "adapter_version_mismatch",
                    "Os workers online usam outra versão do adaptador.",
                    f"Implante a versão {expected_adapter_version} antes de liberar jobs.",
                )
            )
        else:
            issues.append(
                ReadinessIssue(
                    "worker_offline",
                    "Nenhum worker compatível desta processadora está online.",
                    "Inicie ou verifique o serviço executor.",
                )
            )

    blocking = tuple(issue for issue in issues if issue.severity == "blocking")
    return ReadinessReport(
        municipality_slug=municipality.slug,
        state=state,
        can_start=not blocking,
        issues=tuple(issues),
        active_credentials=active_credentials,
        online_workers=online_workers,
    )
