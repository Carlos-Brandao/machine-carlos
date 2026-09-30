"""Retira configurações ativas do Telegram após o backup de implantação."""
import argparse
from pathlib import Path

from dotenv import dotenv_values, load_dotenv, unset_key
from sqlalchemy import select


def retire(env_file: Path):
    load_dotenv(env_file, override=True, interpolate=False)
    from datetime import UTC, datetime
    from machine_admin.db import get_session_factory
    from machine_admin.models import ApiToken, IntegrationSecret, NotificationOutbox
    with get_session_factory()() as session:
        # Use somente tabelas/colunas anteriores à migração corrente.
        for token in session.scalars(select(ApiToken).where(ApiToken.name == 'system-telegram-controller')):
            token.revoked_at = datetime.now(UTC)
        for secret in session.scalars(select(IntegrationSecret).where(IntegrationSecret.key.like('TELEGRAM%'))):
            session.delete(secret)
        for delivery in session.scalars(select(NotificationOutbox).where(
            NotificationOutbox.channel == 'telegram',
            NotificationOutbox.status.in_(['pending', 'retry', 'processing']))):
            delivery.status = 'cancelled'
            delivery.locked_by = delivery.locked_until = delivery.next_attempt_at = None
        session.commit()
    for key in dotenv_values(env_file, interpolate=False):
        if key.startswith('TELEGRAM_'):
            unset_key(str(env_file), key)
    # Os ambientes retirados são preservados no backup, não acessíveis aos serviços.
    for name in ('telegram.env', 'notifications.env'):
        path = env_file.parent / 'env' / name
        if path.is_file():
            path.unlink()
    print('Telegram desativado: tokens locais revogados, env retirado, entregas canceladas.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--env-file', type=Path, required=True)
    retire(parser.parse_args().env_file)
