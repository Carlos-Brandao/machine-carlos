"""Deployment transacional da aplicação na VPS.

Sem ``--activate`` este comando apenas prepara um release, preservando o
comportamento seguro do deploy antigo (nenhuma migration e nenhum restart).
Com ``--activate`` ele cria backup, aplica migrations, troca o symlink
``current`` atomicamente e valida o healthcheck. Em caso de falha depois da
troca, o symlink anterior é restaurado e os serviços são reiniciados.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import shlex
import subprocess
import tarfile
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv
from scp import SCPClient

from services.remote import (
    RemoteConfigurationError,
    RemoteSettings,
    create_ssh_client,
)


ROOT = Path(__file__).parent
ROOT_FILES = (
    "alembic.ini",
    "main.py",
    "requirements.txt",
    "run_admin.py",
    "run_backend_api.py",
    "run_scheduler.py",
    "run_notification_worker.py",
    "run_telegram_bot.py",
    "run_worker.py",
)
SOURCE_DIRS = (
    "deploy",
    "consiglog",
    "facil",
    "grid",
    "machine_admin",
    "migrations",
    "rf1",
    "safeconsig",
    "scripts",
    "services",
    "workers",
)
SERVICE_UNITS = (
    "machine-backend.service",
    "machine-rf1-worker.service",
    "machine-facil-worker.service",
    "machine-consiglog-worker.service",
    "machine-notifications.service",
    "machine-telegram.service",
    "machine-scheduler.service",
)
STOP_UNITS = (
    "machine-scheduler.service",
    "machine-rf1-worker.service",
    "machine-facil-worker.service",
    "machine-consiglog-worker.service",
    "machine-notifications.service",
    "machine-telegram.service",
    "machine-backend.service",
)
PERSISTENT_DIRS = (
    "storage",
    "job_logs",
    "data",
    "temp",
    "completed",
    "debug_captchas",
)
RELEASE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")


def _run(
    ssh,
    command: str,
    *,
    stdin_text: str | None = None,
    stream: bool = False,
) -> str:
    stdin, stdout, stderr = ssh.exec_command(command)
    if stdin_text is not None:
        stdin.write(stdin_text)
        stdin.channel.shutdown_write()
    channel = stdout.channel
    output_chunks: list[bytes] = []
    error_chunks: list[bytes] = []
    while True:
        received = False
        while channel.recv_ready():
            chunk = channel.recv(32768)
            output_chunks.append(chunk)
            if stream:
                print(chunk.decode("utf-8", errors="replace"), end="", flush=True)
            received = True
        while channel.recv_stderr_ready():
            chunk = channel.recv_stderr(32768)
            error_chunks.append(chunk)
            if stream:
                print(chunk.decode("utf-8", errors="replace"), end="", flush=True)
            received = True
        if (
            channel.exit_status_ready()
            and not channel.recv_ready()
            and not channel.recv_stderr_ready()
        ):
            break
        if not received:
            time.sleep(0.05)
    exit_code = channel.recv_exit_status()
    output = b"".join(output_chunks).decode("utf-8", errors="replace").strip()
    error = b"".join(error_chunks).decode("utf-8", errors="replace").strip()
    if exit_code:
        detail = (error or output)[-10_000:]
        raise RuntimeError(detail or f"Comando remoto falhou ({exit_code}).")
    return output


def _run_root_script(
    ssh,
    settings: RemoteSettings,
    script: str,
    *,
    stream: bool = False,
) -> str:
    command = "bash -se" if settings.username == "root" else "sudo -n bash -se"
    return _run(
        ssh,
        command,
        stdin_text="set -Eeuo pipefail\n" + script,
        stream=stream,
    )


def _git_revision() -> str:
    try:
        value = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "nogit"
    dirty = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return f"{value}-dirty" if dirty else value


def _default_release_id() -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{_git_revision()}"


def _validate_release_id(value: str) -> str:
    if not RELEASE_PATTERN.fullmatch(value):
        raise SystemExit(
            "Release inválido: use somente letras, números, ponto, traço e sublinhado."
        )
    return value


def _archive_filter(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
    parts = Path(info.name).parts
    if any(
        part in {".git", ".venv", ".env", "env", "__pycache__"}
        or part.startswith(".env.")
        for part in parts
    ):
        return None
    if info.name.endswith((".pyc", ".pyo", ".DS_Store", ".pem", ".key")):
        return None
    return info


def _build_archive(destination: Path, release_id: str) -> str:
    """Cria um artefato sem .env, bases, logs ou credenciais."""

    members: list[str] = []
    with tarfile.open(destination, "w:gz", format=tarfile.PAX_FORMAT) as archive:
        for relative in (*ROOT_FILES, *SOURCE_DIRS):
            source = ROOT / relative
            if not source.exists():
                continue
            archive.add(source, arcname=relative, filter=_archive_filter)
            members.append(relative)
        metadata = json.dumps(
            {
                "release_id": release_id,
                "revision": _git_revision(),
                "built_at": datetime.now(UTC).isoformat(),
                "members": members,
            },
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8")
        info = tarfile.TarInfo("release.json")
        info.size = len(metadata)
        info.mode = 0o644
        info.mtime = int(datetime.now(UTC).timestamp())
        archive.addfile(info, io.BytesIO(metadata))
    return hashlib.sha256(destination.read_bytes()).hexdigest()


def _prepare_host(ssh, settings: RemoteSettings) -> None:
    root = shlex.quote(settings.release_root)
    legacy = shlex.quote(settings.legacy_remote_dir)
    user = shlex.quote(settings.service_user)
    group = shlex.quote(settings.service_group)
    persistent = " ".join(shlex.quote(item) for item in PERSISTENT_DIRS)
    units = " ".join(shlex.quote(item) for item in SERVICE_UNITS)
    script = f"""
root={root}
legacy={legacy}
service_user={user}
service_group={group}
runtime_group=machine-runtime

if ! getent group "$service_group" >/dev/null; then
  groupadd --system "$service_group"
fi
if ! getent group "$runtime_group" >/dev/null; then
  groupadd --system "$runtime_group"
fi
if ! id -u "$service_user" >/dev/null 2>&1; then
  useradd --system --gid "$service_group" --home-dir "$root/shared/home" \\
    --shell /usr/sbin/nologin "$service_user"
fi
usermod -a -G "$runtime_group" "$service_user"
for account in machine-backend machine-worker machine-telegram machine-notify; do
  if ! getent group "$account" >/dev/null; then
    groupadd --system "$account"
  fi
  if ! id -u "$account" >/dev/null 2>&1; then
    useradd --system --gid "$account" --home-dir "$root/shared/home/$account" \\
      --shell /usr/sbin/nologin "$account"
  fi
  usermod -a -G "$runtime_group" "$account"
done
install -d -o root -g root -m 0755 "$root" "$root/releases" "$root/incoming"
install -d -o root -g root -m 0700 "$root/legacy-systemd"
install -d -o root -g "$runtime_group" -m 0750 "$root/shared" "$root/shared/home"
install -d -o root -g "$service_group" -m 0711 "$root/shared/env"
install -d -o "$service_user" -g "$service_group" -m 0750 \\
  "$root/shared/home/deploy"
install -d -o root -g "$service_group" -m 0750 "$root/backups"
install -d -o root -g "$runtime_group" -m 2770 "$root/shared/playwright"
install -d -o machine-backend -g machine-backend -m 0750 "$root/shared/home/machine-backend"
install -d -o machine-worker -g machine-worker -m 0750 "$root/shared/home/machine-worker"
install -d -o machine-telegram -g machine-telegram -m 0750 "$root/shared/home/machine-telegram"
install -d -o machine-notify -g machine-notify -m 0750 "$root/shared/home/machine-notify"
for name in {persistent}; do
  install -d -o root -g "$runtime_group" -m 2770 "$root/shared/$name"
done

# A primeira adoção conserva o .env e deixa a instalação antiga disponível
# como rollback. O segundo sync dos dados ocorre com os serviços parados.
if [[ ! -f "$root/shared/.env" ]]; then
  if [[ -f "$legacy/.env" ]]; then
    install -o root -g "$service_group" -m 0640 \\
      "$legacy/.env" "$root/shared/.env"
  else
    install -o root -g "$service_group" -m 0640 \\
      /dev/null "$root/shared/.env"
  fi
fi
chown root:"$service_group" "$root/shared/.env"
chmod 0640 "$root/shared/.env"
if [[ ! -e "$root/current" && -d "$legacy" ]]; then
  if [[ -d "$legacy/env" && ! -e "$legacy/.venv" ]]; then
    ln -s env "$legacy/.venv"
  fi
  ln -s "$legacy" "$root/current"
fi

# Snapshot único das units anteriores para que a primeira migração também
# possa voltar ao layout /root/ROBO_FACIL se a ativação falhar.
if [[ ! -f "$root/legacy-systemd/.captured" ]]; then
  : > "$root/legacy-systemd/present.list"
  for unit in {units}; do
    if [[ -f "/etc/systemd/system/$unit" ]]; then
      cp -a "/etc/systemd/system/$unit" "$root/legacy-systemd/$unit"
      echo "$unit" >> "$root/legacy-systemd/present.list"
    fi
  done
  touch "$root/legacy-systemd/.captured"
fi
"""
    _run_root_script(ssh, settings, script)


def _stage_release(
    ssh,
    settings: RemoteSettings,
    archive_path: Path,
    release_id: str,
    archive_sha256: str,
) -> None:
    remote_upload = f"/tmp/machine-{release_id}.tar.gz"
    with SCPClient(ssh.get_transport()) as scp:
        scp.put(str(archive_path), remote_path=remote_upload)

    root = shlex.quote(settings.release_root)
    upload = shlex.quote(remote_upload)
    release = shlex.quote(release_id)
    expected_hash = shlex.quote(archive_sha256)
    user = shlex.quote(settings.service_user)
    group = shlex.quote(settings.service_group)
    persistent = " ".join(shlex.quote(item) for item in PERSISTENT_DIRS)
    script = f"""
root={root}
upload={upload}
release_id={release}
service_user={user}
service_group={group}
expected_hash={expected_hash}
release="$root/releases/$release_id"
staging="$root/releases/.$release_id.staging"
trap 'rm -f -- "$upload"' EXIT

actual_hash=$(sha256sum "$upload" | awk '{{print $1}}')
[[ "$actual_hash" == "$expected_hash" ]] || {{ echo "Checksum do artefato inválido" >&2; exit 1; }}
[[ ! -e "$release" ]] || {{ echo "Release já existe: $release_id" >&2; exit 1; }}
rm -rf -- "$staging"
install -d -o "$service_user" -g "$service_group" -m 0755 "$staging"
tar -xzf "$upload" -C "$staging" --no-same-owner --no-same-permissions
rm -f -- "$upload"
[[ -f "$staging/requirements.txt" \
   && -f "$staging/run_backend_api.py" \
   && -f "$staging/scripts/backup_machine.py" \
   && -f "$staging/scripts/build_service_envs.py" \
   && -f "$staging/scripts/prune_releases.py" ]] || {{
  echo "Artefato incompleto" >&2
  exit 1
}}
for name in {persistent}; do
  ln -s "$root/shared/$name" "$staging/$name"
done
chown -R "$service_user:$service_group" "$staging"

as_service_user() {{
  if command -v runuser >/dev/null 2>&1; then
    runuser -u "$service_user" -- "$@"
  else
    sudo -u "$service_user" -- "$@"
  fi
}}
as_service_user python3 -m venv "$staging/.venv"
as_service_user env HOME="$root/shared/home/deploy" \\
  PLAYWRIGHT_BROWSERS_PATH="$root/shared/playwright" \\
  "$staging/.venv/bin/pip" install --disable-pip-version-check \\
  --requirement "$staging/requirements.txt"
as_service_user env HOME="$root/shared/home/deploy" \\
  PLAYWRIGHT_BROWSERS_PATH="$root/shared/playwright" \\
  "$staging/.venv/bin/playwright" install chromium
as_service_user "$staging/.venv/bin/python" -m compileall -q "$staging"
touch "$staging/.ready"
chown -R root:"$service_group" "$staging"
chmod -R u+rwX,go+rX,go-w "$staging"
mv "$staging" "$release"
"""
    _run_root_script(ssh, settings, script, stream=True)


def _current_target(ssh, settings: RemoteSettings) -> str | None:
    root = shlex.quote(settings.release_root)
    output = _run_root_script(
        ssh,
        settings,
        f'root={root}\nreadlink -f "$root/current" 2>/dev/null || true\n',
    )
    return output.strip() or None


def _service_install_script() -> str:
    units = " ".join(shlex.quote(item) for item in SERVICE_UNITS)
    return f"""
for unit in {units}; do
  source_unit="$release/deploy/$unit"
  [[ -f "$source_unit" ]] || {{ echo "Unidade ausente: $unit" >&2; exit 1; }}
  sed -e "s|/opt/machine|$root|g" \\
      -e "s|^User=machine$|User=$service_user|" \\
      -e "s|^Group=machine$|Group=$service_group|" \\
      "$source_unit" > "/etc/systemd/system/$unit.tmp"
  chmod 0644 "/etc/systemd/system/$unit.tmp"
  chown root:root "/etc/systemd/system/$unit.tmp"
  mv "/etc/systemd/system/$unit.tmp" "/etc/systemd/system/$unit"
done
"""


def _service_env_script() -> str:
    return """
"$release/.venv/bin/python" "$release/scripts/build_service_envs.py" \\
  --source "$root/shared/.env" --output-dir "$root/shared/env" \\
  --owner-group "$service_group"
"""


def _service_validation_script() -> str:
    """Gera funções shell que validam saúde e uma janela sem restarts."""

    return r"""
wait_for_backend() {
  local healthy=0
  local attempt
  for attempt in $(seq 1 12); do
    if curl --fail --silent --show-error --max-time 10 "$health_url" >/dev/null; then
      healthy=1
      break
    fi
    sleep 5
  done
  [[ "$healthy" == 1 ]] || {
    echo "Healthcheck do backend falhou" >&2
    return 1
  }
}

verify_units_stable() {
  local window_seconds=$1
  shift
  local -a monitored_units=("$@")
  local -A entered_at=()
  local -A restart_count=()
  local unit result current_entered current_restarts sample samples

  ((${#monitored_units[@]} > 0)) || {
    echo "Nenhuma unidade disponível para validação" >&2
    return 1
  }
  for unit in "${monitored_units[@]}"; do
    systemctl cat "$unit" >/dev/null 2>&1 || {
      echo "Unidade não instalada: $unit" >&2
      return 1
    }
    systemctl is-active --quiet "$unit" || {
      echo "Serviço inativo: $unit" >&2
      return 1
    }
    result=$(systemctl show "$unit" --property=Result --value)
    [[ "$result" == "success" ]] || {
      echo "Resultado inválido em $unit: $result" >&2
      return 1
    }
    entered_at["$unit"]=$(systemctl show "$unit" \
      --property=ActiveEnterTimestampMonotonic --value)
    restart_count["$unit"]=$(systemctl show "$unit" \
      --property=NRestarts --value)
  done

  samples=$((window_seconds / 5))
  ((samples > 0)) || samples=1
  for sample in $(seq 1 "$samples"); do
    sleep 5
    curl --fail --silent --show-error --max-time 10 "$health_url" >/dev/null || {
      echo "Healthcheck caiu durante a janela de estabilidade" >&2
      return 1
    }
    for unit in "${monitored_units[@]}"; do
      systemctl is-active --quiet "$unit" || {
        echo "Serviço caiu durante a validação: $unit" >&2
        return 1
      }
      result=$(systemctl show "$unit" --property=Result --value)
      current_entered=$(systemctl show "$unit" \
        --property=ActiveEnterTimestampMonotonic --value)
      current_restarts=$(systemctl show "$unit" --property=NRestarts --value)
      [[ "$result" == "success" \
         && "$current_entered" == "${entered_at[$unit]}" \
         && "$current_restarts" == "${restart_count[$unit]}" ]] || {
        echo "Serviço reiniciou ou falhou durante a validação: $unit" >&2
        return 1
      }
    done
  done
}
"""


def _activate_release(
    ssh,
    settings: RemoteSettings,
    release_id: str,
    *,
    backup: bool,
) -> None:
    root = shlex.quote(settings.release_root)
    legacy = shlex.quote(settings.legacy_remote_dir)
    release_name = shlex.quote(release_id)
    user = shlex.quote(settings.service_user)
    group = shlex.quote(settings.service_group)
    health = shlex.quote(settings.health_url)
    units = " ".join(shlex.quote(item) for item in SERVICE_UNITS)
    stop_units = " ".join(shlex.quote(item) for item in STOP_UNITS)
    runtime_units = " ".join(
        shlex.quote(item)
        for item in SERVICE_UNITS
        if item in {"machine-notifications.service", "machine-telegram.service"}
    )
    persistent = " ".join(shlex.quote(item) for item in PERSISTENT_DIRS)
    backup_command = (
        '"$release/.venv/bin/python" '
        '"$release/scripts/backup_machine.py" --root "$root" '
        f"--label {release_name} --keep {settings.keep_releases}"
        if backup
        else 'echo "ATENÇÃO: backup ignorado por opção explícita."'
    )
    backup_preflight = "command -v pg_dump >/dev/null" if backup else ":"
    install_units = _service_install_script()
    validate_services = _service_validation_script()
    script = f"""
root={root}
legacy={legacy}
release_id={release_name}
service_user={user}
service_group={group}
health_url={health}
release="$root/releases/$release_id"
[[ -f "$release/.ready" ]] || {{ echo "Release não está pronto: $release_id" >&2; exit 1; }}
[[ -s "$root/shared/.env" ]] || {{ echo "Configure $root/shared/.env antes de ativar" >&2; exit 1; }}
command -v curl >/dev/null
{backup_preflight}
{validate_services}

as_account() {{
  account=$1
  shift
  if command -v runuser >/dev/null 2>&1; then
    runuser -u "$account" -- "$@"
  else
    sudo -u "$account" -- "$@"
  fi
}}

"$release/.venv/bin/python" "$release/scripts/build_service_envs.py" \\
  --source "$root/shared/.env" --output-dir "$root/shared/env" \\
  --owner-group "$service_group"

# Para a instalação antiga antes da cópia final, evitando perder arquivos
# gravados entre a preparação do release e sua ativação.
echo "Encerrando serviços e aguardando logout gracioso dos portais..."
for unit in {stop_units}; do
  if systemctl cat "$unit" >/dev/null 2>&1; then
    systemctl stop "$unit"
    result=$(systemctl show "$unit" --property=Result --value)
    state=$(systemctl show "$unit" --property=ActiveState --value)
    [[ "$state" == "inactive" || "$state" == "failed" ]] || {{ echo "Serviço ainda ativo: $unit" >&2; exit 1; }}
    [[ "$result" != "timeout" ]] || {{ echo "Parada excedeu o limite: $unit" >&2; exit 1; }}
  fi
done
echo "Todos os serviços foram encerrados de forma graciosa."
current_before=$(readlink -f "$root/current" 2>/dev/null || true)
if [[ -d "$legacy" && ( "$current_before" == "$legacy" || ! -f "$root/shared/.legacy-imported" ) ]]; then
  for name in {persistent}; do
    if [[ -d "$legacy/$name" ]]; then
      cp -a "$legacy/$name/." "$root/shared/$name/"
    fi
  done
  for name in {persistent}; do
    chown -R root:machine-runtime "$root/shared/$name"
    chmod -R u+rwX,g+rwX,o-rwx "$root/shared/$name"
  done
  touch "$root/shared/.legacy-imported"
  chown root:"$service_group" "$root/shared/.legacy-imported"
  chmod 0640 "$root/shared/.legacy-imported"
fi

{backup_command}
as_account machine-backend env HOME="$root/shared/home/machine-backend" \\
  MACHINE_STORAGE_DIR="$root/shared/storage" \\
  PLAYWRIGHT_BROWSERS_PATH="$root/shared/playwright" \\
  "$release/.venv/bin/python" -c \\
  'import os,sys; from dotenv import load_dotenv; load_dotenv(sys.argv[1]); os.chdir(sys.argv[2]); from alembic.config import main; main(argv=["upgrade", "head"])' \\
  "$root/shared/env/backend.env" "$release"

# Os valores brutos de tokens não podem ser recuperados do hash no banco.
# Crie identidades próprias quando a instalação legada não preservou o valor
# ou usava um token sem o escopo necessário. Nenhum valor é impresso.
"$release/.venv/bin/python" "$release/scripts/ensure_service_tokens.py" \\
  --env-file "$root/shared/.env"
chown root:"$service_group" "$root/shared/.env"
chmod 0640 "$root/shared/.env"
"$release/.venv/bin/python" "$release/scripts/build_service_envs.py" \\
  --source "$root/shared/.env" --output-dir "$root/shared/env" \\
  --owner-group "$service_group"

{install_units}
next_link="$root/.current-$release_id"
rm -f -- "$next_link"
ln -s "$release" "$next_link"
mv -Tf "$next_link" "$root/current"
systemctl daemon-reload
systemctl enable {units} >/dev/null
systemctl restart machine-backend.service
wait_for_backend
systemctl restart {runtime_units}
systemctl restart machine-scheduler.service
verify_units_stable 30 {units}
"$release/.venv/bin/python" "$release/scripts/prune_releases.py" \\
  --root "$root" --keep {settings.keep_releases} \\
  || echo "Aviso: não foi possível remover releases antigos" >&2
echo "Release ativo: $release_id"
"""
    _run_root_script(ssh, settings, script, stream=True)


def _restore_previous(
    ssh,
    settings: RemoteSettings,
    previous_target: str | None,
) -> None:
    root = shlex.quote(settings.release_root)
    legacy = shlex.quote(settings.legacy_remote_dir)
    health = shlex.quote(settings.health_url)
    user = shlex.quote(settings.service_user)
    group = shlex.quote(settings.service_group)
    units = " ".join(shlex.quote(item) for item in SERVICE_UNITS)
    previous = shlex.quote(previous_target) if previous_target else ""
    install_units = _service_install_script()
    service_envs = _service_env_script()
    validate_services = _service_validation_script()
    script = f"""
root={root}
legacy={legacy}
previous={previous}
health_url={health}
service_user={user}
service_group={group}
{validate_services}
if [[ -n "$previous" && -d "$previous" ]]; then
  recovery_link="$root/.current-recovery"
  rm -f -- "$recovery_link"
  ln -s "$previous" "$recovery_link"
  mv -Tf "$recovery_link" "$root/current"
fi
if [[ "$previous" == "$legacy" && -f "$root/legacy-systemd/.captured" ]]; then
  for unit in {units}; do
    if grep -Fxq "$unit" "$root/legacy-systemd/present.list"; then
      cp -a "$root/legacy-systemd/$unit" "/etc/systemd/system/$unit"
    else
      rm -f -- "/etc/systemd/system/$unit"
    fi
  done
elif [[ -n "$previous" && -d "$previous/deploy" ]]; then
  release="$previous"
  {service_envs}
  {install_units}
fi
systemctl daemon-reload
recovery_units=()
for unit in {units}; do
  if systemctl cat "$unit" >/dev/null 2>&1; then
    recovery_units+=("$unit")
  fi
done
((${{#recovery_units[@]}} > 0)) || {{
  echo "Nenhuma unidade restaurável encontrada" >&2
  exit 1
}}
systemctl restart "${{recovery_units[@]}}"
wait_for_backend
verify_units_stable 30 "${{recovery_units[@]}}"
echo "Restauração do release anterior executada. Verifique o banco e os serviços."
"""
    _run_root_script(ssh, settings, script)


def _rollback(ssh, settings: RemoteSettings, requested_release: str | None) -> str:
    root = shlex.quote(settings.release_root)
    requested = shlex.quote(requested_release or "")
    health = shlex.quote(settings.health_url)
    user = shlex.quote(settings.service_user)
    group = shlex.quote(settings.service_group)
    units = " ".join(shlex.quote(item) for item in SERVICE_UNITS)
    install_units = _service_install_script()
    service_envs = _service_env_script()
    validate_services = _service_validation_script()
    script = f"""
root={root}
requested={requested}
health_url={health}
service_user={user}
service_group={group}
{validate_services}
current=$(readlink -f "$root/current" 2>/dev/null || true)
if [[ -n "$requested" ]]; then
  target="$root/releases/$requested"
else
  target=""
  while IFS= read -r candidate; do
    if [[ -f "$candidate/.ready" && "$(readlink -f "$candidate")" != "$current" ]]; then
      target="$candidate"
      break
    fi
  done < <(find "$root/releases" -mindepth 1 -maxdepth 1 -type d \\
    ! -name '.*.staging' -print0 | xargs -0 -r ls -1dt)
fi
[[ -n "$target" && -f "$target/.ready" ]] || {{ echo "Nenhum release anterior pronto encontrado" >&2; exit 1; }}
release="$target"
{service_envs}
{install_units}
next_link="$root/.current-rollback"
rm -f -- "$next_link"
ln -s "$target" "$next_link"
mv -Tf "$next_link" "$root/current"
systemctl daemon-reload
systemctl restart {units}
wait_for_backend
verify_units_stable 30 {units}
echo "Rollback ativo: $(basename "$target")"
"""
    return _run_root_script(ssh, settings, script)


def _status(ssh, settings: RemoteSettings) -> str:
    root = shlex.quote(settings.release_root)
    units = " ".join(shlex.quote(item) for item in SERVICE_UNITS)
    script = f"""
root={root}
echo "Atual: $(readlink -f "$root/current" 2>/dev/null || echo ausente)"
echo "Releases:"
find "$root/releases" -mindepth 1 -maxdepth 1 -type d -printf '  %TY-%Tm-%Td %TH:%TM  %f\n' 2>/dev/null | sort -r || true
echo "Serviços:"
for unit in {units}; do
  printf '  %-36s %s\n' "$unit" "$(systemctl is-active "$unit" 2>/dev/null || true)"
done
"""
    return _run_root_script(ssh, settings, script)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Deployment versionado da Machine")
    parser.add_argument(
        "action",
        nargs="?",
        choices=("deploy", "activate", "rollback", "status"),
        default="deploy",
    )
    parser.add_argument(
        "--release",
        help="ID do release (gerado automaticamente no deploy)",
    )
    parser.add_argument(
        "--activate",
        action="store_true",
        help="aplica migration, troca current e reinicia os serviços",
    )
    parser.add_argument(
        "--skip-backup",
        action="store_true",
        help="ativa sem backup (não recomendado; exige opção explícita)",
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    load_dotenv(ROOT / ".env")
    try:
        settings = RemoteSettings.from_environment()
        ssh = create_ssh_client(settings)
    except (RemoteConfigurationError, OSError) as exc:
        raise SystemExit(f"Falha ao conectar à VPS: {exc}") from exc

    try:
        if args.action == "status":
            print(_status(ssh, settings))
            return
        if args.action == "rollback":
            release = _validate_release_id(args.release) if args.release else None
            previous = _current_target(ssh, settings)
            try:
                print(_rollback(ssh, settings, release))
            except Exception:
                print("Rollback falhou; restaurando a versão que estava ativa...")
                _restore_previous(ssh, settings, previous)
                raise
            print("Atenção: rollback de código não desfaz migrations do banco.")
            return

        if args.action == "activate":
            if not args.release:
                raise SystemExit("Informe --release para ativar um release preparado.")
            release_id = _validate_release_id(args.release)
            _prepare_host(ssh, settings)
            previous = _current_target(ssh, settings)
            try:
                _activate_release(
                    ssh,
                    settings,
                    release_id,
                    backup=not args.skip_backup,
                )
            except Exception:
                print("Ativação falhou; restaurando o release anterior...")
                _restore_previous(ssh, settings, previous)
                raise
            print(f"Release {release_id} ativado e validado em {settings.health_url}.")
            return

        release_id = _validate_release_id(args.release or _default_release_id())
        print(f"Preparando release {release_id}...")
        _prepare_host(ssh, settings)
        with tempfile.TemporaryDirectory(prefix="machine-deploy-") as temporary:
            archive = Path(temporary) / f"{release_id}.tar.gz"
            checksum = _build_archive(archive, release_id)
            _stage_release(ssh, settings, archive, release_id, checksum)
        print(f"Release preparado em {settings.release_root}/releases/{release_id}.")

        if not args.activate:
            print("Nenhuma migration ou reinicialização foi executada.")
            print(f"Para ativar: python deploy.py activate --release {release_id}")
            return

        previous = _current_target(ssh, settings)
        try:
            _activate_release(
                ssh,
                settings,
                release_id,
                backup=not args.skip_backup,
            )
        except Exception:
            print("Ativação falhou; restaurando o release anterior...")
            _restore_previous(ssh, settings, previous)
            raise
        print(f"Release {release_id} ativado e validado em {settings.health_url}.")
        previous_name = Path(previous).name if previous else "<id>"
        print(f"Rollback: python deploy.py rollback --release {previous_name}")
    finally:
        ssh.close()


if __name__ == "__main__":
    main()
