from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

import deploy
from scripts.build_service_envs import _selected_values
from services.remote import RemoteSettings


class DeploymentHardeningTests(unittest.TestCase):
    def test_safeconsig_worker_is_managed_by_deploy_and_scheduler(self) -> None:
        unit = "machine-safeconsig-worker.service"

        self.assertIn(unit, deploy.SERVICE_UNITS)
        self.assertIn(unit, deploy.STOP_UNITS)
        self.assertIn("safeconsig", deploy.SOURCE_DIRS)

        service = (Path(deploy.__file__).parent / "deploy" / unit).read_text()
        scheduler = (
            Path(deploy.__file__).parent / "deploy" / "machine-scheduler.service"
        ).read_text()
        self.assertIn("ExecStart=/opt/machine/current/.venv/bin/python -u run_worker.py safeconsig", service)
        self.assertIn(f"Wants=machine-rf1-worker.service machine-facil-worker.service {unit}", scheduler)
        self.assertIn(f"After=machine-rf1-worker.service machine-facil-worker.service {unit}", scheduler)

    def test_service_environments_do_not_leak_database_or_master_key(self) -> None:
        source = {
            "DATABASE_URL": "postgresql://secret",
            "APP_MASTER_KEY": "master-secret",
            "ADMIN_SESSION_SECRET": "session-secret",
            "WORKER_API_URL": "http://127.0.0.1:8000",
            "WORKER_API_TOKEN": "worker-token",
            "TELEGRAM_BACKEND_API_TOKEN": "telegram-token",
            "TELEGRAM_BOT_TOKEN": "bot-token",
            "BACKEND_API_URL": "http://127.0.0.1:8000",
        }

        worker = _selected_values(source, "worker")
        telegram = _selected_values(source, "telegram")
        backend = _selected_values(source, "backend")

        self.assertNotIn("DATABASE_URL", worker)
        self.assertNotIn("APP_MASTER_KEY", worker)
        self.assertNotIn("DATABASE_URL", telegram)
        self.assertNotIn("APP_MASTER_KEY", telegram)
        self.assertEqual("postgresql://secret", backend["DATABASE_URL"])
        self.assertEqual("master-secret", backend["APP_MASTER_KEY"])

    def test_activation_backs_up_migrates_tokens_then_switches_release(self) -> None:
        settings = RemoteSettings(
            host="example.invalid",
            username="root",
            remote_dir="/root/ROBO_FACIL",
            password="test",
            key_filename=None,
        )
        captured: list[str] = []

        with patch("deploy._run_root_script", side_effect=lambda *_args, **_kwargs: captured.append(_args[2]) or ""):
            deploy._activate_release(
                object(), settings, "release-test", backup=True
            )

        script = captured[0]
        self.assertLess(script.index("backup_machine.py"), script.index("upgrade"))
        self.assertLess(script.index("upgrade"), script.index("ensure_service_tokens.py"))
        self.assertLess(script.index("ensure_service_tokens.py"), script.index("mv -Tf"))
        self.assertIn("verify_units_stable 30", script)

    def test_rollback_tolerates_units_absent_from_previous_release(self) -> None:
        strict = deploy._service_install_script()
        recovery = deploy._service_install_script(strict=False)

        self.assertIn('echo "Unidade ausente: $unit"', strict)
        self.assertIn('exit 1', strict)
        self.assertIn('systemctl stop "$unit"', recovery)
        self.assertIn('rm -f -- "/etc/systemd/system/$unit"', recovery)
        self.assertIn('continue', recovery)


if __name__ == "__main__":
    unittest.main()
