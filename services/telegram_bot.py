"""Interface Telegram para o backend de filas de consulta.

Este módulo não executa os robôs localmente. Ele coleta a seleção do usuário e
faz requisições ao backend, que continua sendo a única fonte de verdade para a
fila e o limite de concorrência.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

import requests

from machine_admin.secret_store import get_runtime_secret
from services.registry import enabled_municipalities, enabled_platforms
from services.telegram import TelegramClient


LOG = logging.getLogger(__name__)
SELECTION_TTL_SECONDS = 15 * 60


_ENABLED_MUNICIPALITIES = enabled_municipalities()
PREFEITURAS = tuple(
    (
        platform.name,
        tuple(
            (municipality.slug, municipality.name)
            for municipality in _ENABLED_MUNICIPALITIES
            if municipality.platform_slug == platform.slug
        ),
    )
    for platform in enabled_platforms()
    if any(
        municipality.platform_slug == platform.slug
        for municipality in _ENABLED_MUNICIPALITIES
    )
)
PREFEITURAS_POR_SLUG = {
    slug: nome for _, itens in PREFEITURAS for slug, nome in itens
}
SISTEMA_POR_PREFEITURA = {
    slug: sistema for sistema, itens in PREFEITURAS for slug, _ in itens
}


class ConfigurationError(RuntimeError):
    """A configuração obrigatória do serviço está ausente."""


@dataclass(frozen=True)
class Settings:
    telegram_token: str
    allowed_user_ids: frozenset[int]
    backend_url: str | None
    backend_token: str | None

    @classmethod
    def from_environment(cls) -> "Settings":
        token = get_runtime_secret("TELEGRAM_BOT_TOKEN")
        backend_url = os.getenv("BACKEND_API_URL", "").strip().rstrip("/")
        raw_user_ids = os.getenv("TELEGRAM_ALLOWED_USER_IDS", "")

        if not token:
            raise ConfigurationError("TELEGRAM_BOT_TOKEN não configurado.")
        try:
            user_ids = frozenset(
                int(value.strip()) for value in raw_user_ids.split(",") if value.strip()
            )
        except ValueError as exc:
            raise ConfigurationError(
                "TELEGRAM_ALLOWED_USER_IDS deve conter somente IDs numéricos separados por vírgula."
            ) from exc

        if not user_ids:
            raise ConfigurationError("TELEGRAM_ALLOWED_USER_IDS não configurado.")

        return cls(
            telegram_token=token,
            allowed_user_ids=user_ids,
            backend_url=backend_url or None,
            # Este é o token bootstrap usado para autenticar no backend, não
            # um segredo operacional distribuído pelo próprio endpoint.
            backend_token=(
                os.getenv("TELEGRAM_BACKEND_API_TOKEN", "").strip()
                or os.getenv("BACKEND_API_TOKEN", "").strip()
                or None
            ),
        )


@dataclass
class Selection:
    datasets: list[dict[str, Any]] = field(default_factory=list)
    selected: list[int] = field(default_factory=list)
    updated_at: float = field(default_factory=time.monotonic)

    def option(self, dataset_id: int) -> dict[str, Any] | None:
        return next(
            (item for item in self.datasets if int(item.get("id", 0)) == dataset_id),
            None,
        )


class BackendClient:
    def __init__(self, settings: Settings) -> None:
        self.base_url = settings.backend_url
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        if settings.backend_token:
            self.session.headers["Authorization"] = f"Bearer {settings.backend_token}"

    def ready_datasets(self) -> Any:
        return self._request("GET", "/api/datasets/ready")

    def create_batch(
        self, selections: list[dict[str, object]], telegram_user_id: int, chat_id: int
    ) -> Any:
        return self._request(
            "POST",
            "/api/jobs/batch",
            json={
                "jobs": selections,
                "requested_by": {
                    "telegram_user_id": telegram_user_id,
                    "telegram_chat_id": chat_id,
                },
            },
        )

    def status(self) -> Any:
        return self._request("GET", "/api/jobs/status")

    def queue(self) -> Any:
        return self._request("GET", "/api/jobs/queue")

    def clear_queue(self) -> Any:
        return self._request("POST", "/api/jobs/queue/clear", json={})

    def stop_running(self) -> Any:
        return self._request("POST", "/api/jobs/running/stop", json={})

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        if not self.base_url:
            raise RuntimeError("O backend ainda não foi configurado.")
        try:
            response = self.session.request(
                method, f"{self.base_url}{path}", timeout=20, **kwargs
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise RuntimeError("Não foi possível comunicar com o backend.") from exc

        try:
            return response.json()
        except ValueError:
            return {"message": response.text}


class TelegramBot:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.backend = BackendClient(settings)
        self.telegram = TelegramClient(settings.telegram_token)
        self.selections: dict[tuple[int, int], Selection] = {}

    def run_forever(self) -> None:
        LOG.info("Bot Telegram iniciado por long polling.")
        offset: int | None = None
        while True:
            try:
                updates = self._telegram("getUpdates", {"offset": offset, "timeout": 30})
                for update in updates:
                    offset = int(update["update_id"]) + 1
                    self.handle_update(update)
            except requests.RequestException:
                LOG.exception("Falha ao consultar Telegram; nova tentativa em 5 segundos.")
                time.sleep(5)
            except Exception:
                LOG.exception("Falha ao processar atualização do Telegram.")

    def set_commands(self) -> None:
        self._telegram(
            "setMyCommands",
            {
                "commands": [
                    {"command": "iniciar", "description": "Selecionar bases para consulta"},
                    {"command": "status", "description": "Ver robôs em execução"},
                    {"command": "fila", "description": "Ver bases na fila"},
                    {"command": "limparfila", "description": "Cancelar robôs aguardando"},
                    {"command": "pararrobos", "description": "Parar robôs em execução"},
                    {"command": "cancelar", "description": "Cancelar seleção atual"},
                    {"command": "ajuda", "description": "Ver instruções"},
                ]
            },
        )

    def handle_update(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            self._handle_callback(update["callback_query"])
        elif "message" in update:
            self._handle_message(update["message"])

    def _handle_message(self, message: dict[str, Any]) -> None:
        user_id, chat_id = self._user_and_chat(message)
        if not self._authorized(user_id):
            LOG.warning("Comando não autorizado do usuário Telegram %s", user_id)
            self._send(chat_id, "⛔ Você não tem permissão para controlar este bot.")
            return

        text = str(message.get("text", "")).strip()
        command = text.split(maxsplit=1)[0].split("@", 1)[0].lower()
        if command == "/iniciar":
            self._start_selection(chat_id, user_id)
        elif command == "/status":
            self._send_backend_summary(chat_id, "status")
        elif command == "/fila":
            self._send_backend_summary(chat_id, "queue")
        elif command == "/limparfila":
            self._request_queue_clear_confirmation(chat_id)
        elif command == "/pararrobos":
            self._request_running_stop_confirmation(chat_id)
        elif command == "/cancelar":
            self.selections.pop((chat_id, user_id), None)
            self._send(chat_id, "Seleção atual cancelada.")
        elif command in ("/ajuda", "/start"):
            self._send(chat_id, self._help_text())

    def _handle_callback(self, callback: dict[str, Any]) -> None:
        # Em callback_query, ``message.from`` é o próprio bot que enviou o
        # teclado. A permissão precisa ser validada contra quem clicou.
        user_id = int(callback["from"]["id"])
        chat_id = int(callback["message"]["chat"]["id"])
        callback_id = callback["id"]
        if not self._authorized(user_id):
            self._answer_callback(callback_id, "Sem permissão.", alert=True)
            return

        action = str(callback.get("data", ""))
        if action == "selection:noop":
            self._answer_callback(callback_id)
            return

        if action.startswith("select-dataset:"):
            try:
                dataset_id = int(action.removeprefix("select-dataset:"))
            except ValueError:
                self._answer_callback(callback_id, "Opção inválida.", alert=True)
                return
            selection = self._selection(chat_id, user_id)
            option = selection.option(dataset_id)
            if not option:
                self._answer_callback(
                    callback_id, "Esta seleção expirou. Use /iniciar novamente.", alert=True
                )
                return
            if dataset_id in selection.selected:
                selection.selected.remove(dataset_id)
            else:
                municipality_slug = str(option.get("municipality_slug", ""))
                selection.selected = [
                    selected_id
                    for selected_id in selection.selected
                    if str((selection.option(selected_id) or {}).get("municipality_slug", ""))
                    != municipality_slug
                ]
                selection.selected.append(dataset_id)
            selection.updated_at = time.monotonic()
            self._edit_selection(callback["message"], selection)
            self._answer_callback(callback_id)
            return

        if action == "selection:clear":
            self.selections.pop((chat_id, user_id), None)
            self._answer_callback(callback_id, "Seleção limpa.")
            self._delete_message(chat_id, callback["message"]["message_id"])
            return

        if action == "selection:cancel":
            self.selections.pop((chat_id, user_id), None)
            self._answer_callback(callback_id)
            self._delete_message(chat_id, callback["message"]["message_id"])
            return

        if action == "selection:confirm":
            selection = self._selection(chat_id, user_id)
            if not selection.selected:
                self._answer_callback(callback_id, "Selecione ao menos uma base.", alert=True)
                return
            selected_options = [
                selection.option(dataset_id) for dataset_id in selection.selected
            ]
            jobs = [
                {
                    "municipality_slug": str(option["municipality_slug"]),
                    "dataset_id": int(option["id"]),
                }
                for option in selected_options
                if option
            ]
            try:
                result = self.backend.create_batch(jobs, user_id, chat_id)
            except RuntimeError:
                self._answer_callback(callback_id, "Backend indisponível. Tente novamente.", alert=True)
                return

            self.selections.pop((chat_id, user_id), None)
            self._answer_callback(callback_id, "Bases enviadas ao backend.")
            self._delete_message(chat_id, callback["message"]["message_id"])
            nomes = ", ".join(
                f"{option.get('municipality_name')} · {option.get('name')}"
                for option in selected_options
                if option
            )
            created = result.get("created", []) if isinstance(result, dict) else []
            skipped = result.get("skipped", []) if isinstance(result, dict) else []
            self._send(
                chat_id,
                "🚀 INICIANDO CONSULTAS\n\n"
                f"Bases enviadas: {nomes}\n"
                f"Jobs adicionados à fila: {len(created)}"
                + (f"\nNão iniciados: {len(skipped)}" if skipped else ""),
            )
            return

        if action == "queue:clear:cancel":
            self._answer_callback(callback_id, "Limpeza cancelada.")
            self._delete_message(chat_id, callback["message"]["message_id"])
            return

        if action == "queue:clear:confirm":
            try:
                result = self.backend.clear_queue()
            except RuntimeError:
                self._answer_callback(callback_id, "Backend indisponível. Tente novamente.", alert=True)
                return
            self._answer_callback(callback_id, "Fila limpa.")
            self._delete_message(chat_id, callback["message"]["message_id"])
            count = int(result.get("cancelled_count", 0)) if isinstance(result, dict) else 0
            self._send(chat_id, f"🧹 Fila limpa. {count} robô(s) aguardando foram cancelados.")
            return

        if action == "running:stop:cancel":
            self._answer_callback(callback_id, "Parada cancelada.")
            self._delete_message(chat_id, callback["message"]["message_id"])
            return

        if action == "running:stop:confirm":
            try:
                result = self.backend.stop_running()
            except RuntimeError:
                self._answer_callback(callback_id, "Backend indisponível. Tente novamente.", alert=True)
                return
            self._answer_callback(callback_id, "Parada solicitada.")
            self._delete_message(chat_id, callback["message"]["message_id"])
            count = int(result.get("cancelled_count", 0)) if isinstance(result, dict) else 0
            self._send(chat_id, f"🛑 Parada solicitada para {count} robô(s) em execução.")
            return

    def _start_selection(self, chat_id: int, user_id: int) -> None:
        try:
            result = self.backend.ready_datasets()
        except RuntimeError:
            self._send(chat_id, "⚠️ Backend indisponível. Tente novamente em instantes.")
            return
        datasets = result.get("datasets", []) if isinstance(result, dict) else []
        if not datasets:
            self._send(
                chat_id,
                "Nenhuma base está pronta para execução. Consulte o painel para ver o bloqueio.",
            )
            return
        selection = Selection(datasets=list(datasets))
        self.selections[(chat_id, user_id)] = selection
        self._send(chat_id, self._selection_text(selection), self._selection_keyboard(selection))

    def _selection(self, chat_id: int, user_id: int) -> Selection:
        key = (chat_id, user_id)
        selection = self.selections.get(key)
        if selection is None or time.monotonic() - selection.updated_at > SELECTION_TTL_SECONDS:
            selection = Selection()
            self.selections[key] = selection
        return selection

    def _selection_text(self, selection: Selection) -> str:
        return (
            "Escolha uma base pronta por convênio. Você pode marcar vários convênios e "
            "confirmar quando terminar.\n\n"
            f"Selecionadas: {len(selection.selected)}"
        )

    def _selection_keyboard(self, selection: Selection) -> dict[str, Any]:
        rows: list[list[dict[str, str]]] = []
        current_processor = None
        for option in selection.datasets:
            processor = str(option.get("processor") or "Outros")
            if processor != current_processor:
                current_processor = processor
                rows.append(
                    [{"text": f"— {processor} —", "callback_data": "selection:noop"}]
                )
            dataset_id = int(option["id"])
            icon = "✅" if dataset_id in selection.selected else "☐"
            label = (
                f"{icon} {option.get('municipality_name')} · "
                f"{option.get('name')} ({option.get('rows', 0)})"
            )
            rows.append(
                [
                    {
                        "text": label[:64],
                        "callback_data": f"select-dataset:{dataset_id}",
                    }
                ]
            )
        rows.extend(
            [
                [{"text": f"✅ Confirmar ({len(selection.selected)})", "callback_data": "selection:confirm"}],
                [
                    {"text": "🧹 Limpar", "callback_data": "selection:clear"},
                    {"text": "✖ Cancelar", "callback_data": "selection:cancel"},
                ],
            ]
        )
        return {"inline_keyboard": rows}

    def _edit_selection(self, message: dict[str, Any], selection: Selection) -> None:
        self._edit_message(
            message["chat"]["id"],
            message["message_id"],
            self._selection_text(selection),
            self._selection_keyboard(selection),
        )

    def _send_backend_summary(self, chat_id: int, operation: str) -> None:
        try:
            result = self.backend.status() if operation == "status" else self.backend.queue()
        except RuntimeError:
            self._send(chat_id, "⚠️ Backend indisponível. Tente novamente em instantes.")
            return
        title = "🤖 Status dos robôs" if operation == "status" else "⏳ Fila de consultas"
        formatted = self._format_status(result) if operation == "status" else self._format_queue(result)
        self._send(chat_id, f"{title}\n\n{formatted}")

    def _request_queue_clear_confirmation(self, chat_id: int) -> None:
        self._send(
            chat_id,
            "🧹 Limpar fila?\n\nIsso cancelará apenas os robôs aguardando. Robôs em execução não serão interrompidos.",
            {
                "inline_keyboard": [
                    [{"text": "🧹 Sim, limpar fila", "callback_data": "queue:clear:confirm"}],
                    [{"text": "Cancelar", "callback_data": "queue:clear:cancel"}],
                ]
            },
        )

    def _request_running_stop_confirmation(self, chat_id: int) -> None:
        self._send(
            chat_id,
            "🛑 Parar robôs em execução?\n\nOs jobs em andamento receberão uma solicitação de parada. A fila aguardando não será alterada.",
            {
                "inline_keyboard": [
                    [{"text": "🛑 Sim, parar robôs", "callback_data": "running:stop:confirm"}],
                    [{"text": "Cancelar", "callback_data": "running:stop:cancel"}],
                ]
            },
        )

    @staticmethod
    def _format_status(result: Any) -> str:
        if not isinstance(result, dict):
            return "Não foi possível interpretar o status retornado pelo backend."

        running = result.get("running") or []
        parts = [TelegramBot._format_job_section("🤖 Robôs rodando", running, show_progress=True)]
        parts.append(
            TelegramBot._format_job_section(
                "⏳ Robôs aguardando", result.get("queued") or [], show_progress=False
            )
        )
        return "\n\n".join(parts)

    @staticmethod
    def _format_queue(result: Any) -> str:
        if not isinstance(result, dict):
            return "Não foi possível interpretar a fila retornada pelo backend."
        return TelegramBot._format_job_section(
            "⏳ Robôs aguardando", result.get("queued") or [], show_progress=False
        )

    @staticmethod
    def _format_job_section(title: str, jobs: Any, *, show_progress: bool) -> str:
        if not isinstance(jobs, list) or not jobs:
            return f"{title}\nNenhum."

        grouped: dict[str, list[dict[str, Any]]] = {}
        for job in jobs:
            if not isinstance(job, dict):
                continue
            slug = str(job.get("prefeitura", ""))
            system = SISTEMA_POR_PREFEITURA.get(slug, "Outros convênios")
            grouped.setdefault(system, []).append(job)

        lines = [title]
        for system, _ in PREFEITURAS:
            system_jobs = grouped.pop(system, [])
            if not system_jobs:
                continue
            lines.append(f"\n{system}")
            for job in system_jobs:
                slug = str(job.get("prefeitura", ""))
                lines.append(f"• {PREFEITURAS_POR_SLUG.get(slug, slug)}")
                if show_progress:
                    total = TelegramBot._job_value(job, "total_consultas", "total", "total_registros")
                    completed = TelegramBot._job_value(job, "realizadas", "processed", "concluidas")
                    lines.append(f"  Total de consultas: {total}")
                    lines.append(f"  Realizadas: {completed}")
        for system, system_jobs in grouped.items():
            lines.append(f"\n{system}")
            lines.extend(f"• {job.get('prefeitura', 'Desconhecido')}" for job in system_jobs)
        return "\n".join(lines)

    @staticmethod
    def _job_value(job: dict[str, Any], *keys: str) -> Any:
        for key in keys:
            if key in job and job[key] is not None:
                return job[key]
        return "—"

    @staticmethod
    def _help_text() -> str:
        return (
            "Comandos disponíveis:\n"
            "/iniciar — selecionar uma ou mais bases\n"
            "/status — consultar execuções em andamento\n"
            "/fila — consultar as próximas bases\n"
            "/limparfila — cancelar todos os robôs aguardando\n"
            "/pararrobos — solicitar parada dos robôs em execução\n"
            "/cancelar — cancelar a seleção atual\n"
            "/ajuda — mostrar esta mensagem"
        )

    def _authorized(self, user_id: int) -> bool:
        return user_id in self.settings.allowed_user_ids

    @staticmethod
    def _user_and_chat(message: dict[str, Any]) -> tuple[int, int]:
        return int(message["from"]["id"]), int(message["chat"]["id"])

    def _send(self, chat_id: int, text: str, reply_markup: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        self._telegram("sendMessage", payload)

    def _edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        self._telegram("editMessageText", payload)

    def _delete_message(self, chat_id: int, message_id: int) -> None:
        self._telegram("deleteMessage", {"chat_id": chat_id, "message_id": message_id})

    def _answer_callback(self, callback_id: str, text: str | None = None, alert: bool = False) -> None:
        payload: dict[str, Any] = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text
        if alert:
            payload["show_alert"] = True
        self._telegram("answerCallbackQuery", payload)

    def _telegram(self, method: str, payload: dict[str, Any]) -> Any:
        return self.telegram.call(method, payload)
