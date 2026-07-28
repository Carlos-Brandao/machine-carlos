"""Cliente interno usado pelos processos de automação."""

from __future__ import annotations

from typing import Any

import requests


class WorkerAPIError(RuntimeError):
    pass


class WorkerAPIConflict(WorkerAPIError):
    pass


class WorkerAPIClient:
    def __init__(self, base_url: str, token: str) -> None:
        if not base_url or not token:
            raise ValueError("WORKER_API_URL e WORKER_API_TOKEN são obrigatórios.")
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            }
        )

    def request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        response = self.session.request(
            method, f"{self.base_url}{path}", timeout=35, **kwargs
        )
        if response.status_code == 409:
            raise WorkerAPIConflict(self._detail(response))
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise WorkerAPIError(self._detail(response)) from exc
        data = response.json()
        if not isinstance(data, dict):
            raise WorkerAPIError("Resposta inválida da API interna.")
        return data

    @staticmethod
    def _detail(response: requests.Response) -> str:
        try:
            body = response.json()
            return str(body.get("detail") or body.get("error") or "Falha na API interna.")
        except ValueError:
            return "Falha na API interna."
