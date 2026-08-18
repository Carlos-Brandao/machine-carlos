"""Contrato canônico entre adapters de portal, workers e backend.

O envelope separa a decisão operacional (``outcome``) dos dados extraídos.  Os
adapters podem preservar o retorno histórico em ``raw`` enquanto migram, mas um
resultado ``found`` só deve ser criado depois que os identificadores solicitados
forem confirmados pelo portal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import re
from typing import Any


class OutcomeKind(StrEnum):
    FOUND = "found"
    NOT_FOUND = "not_found"
    RETRYABLE_ERROR = "retryable_error"
    PERMANENT_ERROR = "permanent_error"
    CREDENTIAL_ERROR = "credential_error"
    PORTAL_UNAVAILABLE = "portal_unavailable"
    INTEGRATION_UNAVAILABLE = "integration_unavailable"


REQUEUE_OUTCOMES = frozenset(
    {
        OutcomeKind.RETRYABLE_ERROR,
        OutcomeKind.CREDENTIAL_ERROR,
        OutcomeKind.PORTAL_UNAVAILABLE,
        OutcomeKind.INTEGRATION_UNAVAILABLE,
    }
)
SUCCESS_OUTCOMES = frozenset({OutcomeKind.FOUND, OutcomeKind.NOT_FOUND})


def _identifier(value: object) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _registration(value: object) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


@dataclass(frozen=True, slots=True)
class ExecutionOutcome:
    kind: OutcomeKind
    requested: dict[str, Any] = field(default_factory=dict)
    confirmed: dict[str, Any] = field(default_factory=dict)
    person: dict[str, Any] = field(default_factory=dict)
    margins: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    message: str | None = None
    stage: str | None = None
    retry_after_seconds: int | None = None
    end_session: bool = False

    @property
    def is_success(self) -> bool:
        return self.kind in SUCCESS_OUTCOMES

    @property
    def should_requeue(self) -> bool:
        return self.kind in REQUEUE_OUTCOMES

    def to_payload(self, *, include_legacy_flat: bool = True) -> dict[str, Any]:
        """Devolve o envelope JSON e, temporariamente, as colunas legadas.

        Manter os campos de ``raw`` no nível superior evita quebrar as planilhas
        existentes enquanto consumidores passam a ler o envelope normalizado.
        """

        payload: dict[str, Any] = dict(self.raw) if include_legacy_flat else {}
        payload.update(
            {
                "outcome": self.kind.value,
                "requested": dict(self.requested),
                "confirmed": dict(self.confirmed),
                "person": dict(self.person),
                "margins": dict(self.margins),
                "raw": dict(self.raw),
            }
        )
        if self.error_code or self.message or self.stage:
            payload["error"] = {
                "code": self.error_code,
                "message": self.message,
                "stage": self.stage,
            }
        return payload

    @classmethod
    def found(
        cls,
        *,
        requested: dict[str, Any],
        confirmed: dict[str, Any],
        raw: dict[str, Any],
        person: dict[str, Any] | None = None,
        margins: dict[str, Any] | None = None,
    ) -> "ExecutionOutcome":
        requested_cpf = _identifier(requested.get("cpf"))
        confirmed_cpf = _identifier(confirmed.get("cpf"))
        if not requested_cpf or confirmed_cpf != requested_cpf:
            raise ValueError(
                "Um resultado found exige o CPF solicitado confirmado pelo portal."
            )
        requested_registration = _registration(requested.get("registration"))
        confirmed_registration = _registration(confirmed.get("registration"))
        if (
            requested_registration
            and confirmed_registration != requested_registration
        ):
            raise ValueError(
                "Um resultado found exige a matrícula solicitada confirmada pelo portal."
            )
        return cls(
            kind=OutcomeKind.FOUND,
            requested=requested,
            confirmed=confirmed,
            person=person or {},
            margins=margins or {},
            raw=raw,
        )

    @classmethod
    def not_found(
        cls, *, requested: dict[str, Any], raw: dict[str, Any] | None = None
    ) -> "ExecutionOutcome":
        return cls(
            kind=OutcomeKind.NOT_FOUND,
            requested=requested,
            raw=raw or {"Status_Robo": "Não Encontrado"},
        )

    @classmethod
    def error(
        cls,
        kind: OutcomeKind,
        *,
        requested: dict[str, Any] | None = None,
        code: str | None = None,
        message: str | None = None,
        stage: str | None = None,
        retry_after_seconds: int | None = None,
        end_session: bool = False,
        raw: dict[str, Any] | None = None,
    ) -> "ExecutionOutcome":
        if kind in SUCCESS_OUTCOMES:
            raise ValueError("Use found() ou not_found() para resultados de sucesso.")
        return cls(
            kind=kind,
            requested=requested or {},
            raw=raw or {},
            error_code=code,
            message=message,
            stage=stage,
            retry_after_seconds=retry_after_seconds,
            end_session=end_session,
        )
