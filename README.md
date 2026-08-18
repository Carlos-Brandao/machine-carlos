# Machine — Central de Robôs

Aplicação para importar bases, executar consultas de margem em portais de
consignação, acompanhar tentativas e entregar o resultado pelo Telegram.

O sistema usa:

- FastAPI para painel e API;
- PostgreSQL como fonte oficial;
- Alembic para migrations;
- GenericWorker para execução concorrente;
- adapters isolados para RF1, FACILCONSIG e CONSIGX;
- Playwright nos portais;
- uma outbox durável para Telegram.

As regras oficiais estão em
[docs/BUSINESS_RULES.md](docs/BUSINESS_RULES.md). O contrato para implementar
novos portais está em
[docs/ADAPTER_CONTRACT.md](docs/ADAPTER_CONTRACT.md).

## Requisitos

- Python 3.11 ou superior;
- PostgreSQL 15 ou superior;
- Chromium do Playwright;
- proxy HTTPS para publicar o painel.

## Instalação local

    python -m venv env
    source env/bin/activate
    pip install -r requirements.txt
    playwright install chromium
    alembic upgrade head

Copie as configurações para um arquivo .env não versionado:

    DATABASE_URL=postgresql://machine:SENHA@127.0.0.1:5432/machine
    ADMIN_SESSION_SECRET=valor-aleatorio-com-48-ou-mais-caracteres
    APP_MASTER_KEY=chave-base64-urlsafe-de-32-bytes
    ADMIN_ALLOWED_HOSTS=localhost,127.0.0.1
    ADMIN_COOKIE_SECURE=false
    BOOTSTRAP_ADMIN_EMAIL=admin@exemplo.com
    BOOTSTRAP_ADMIN_PASSWORD=senha-inicial-com-12-ou-mais-caracteres

Geração das chaves:

    python -c "import secrets; print(secrets.token_urlsafe(48))"
    python -c "import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"

Depois do primeiro login, remova BOOTSTRAP_ADMIN_EMAIL e
BOOTSTRAP_ADMIN_PASSWORD. Preserve APP_MASTER_KEY em backup seguro: sem ela não
é possível recuperar dados já cifrados.

## Serviços

Desenvolvimento, em terminais separados:

    python run_backend_api.py
    python run_worker.py rf1
    python run_worker.py facil
    python run_worker.py consiglog
    python run_notification_worker.py
    python run_telegram_bot.py

run_scheduler.py permanece como supervisor local compatível e inicia somente os
adapters transacionais habilitados. Em produção, cada pool usa uma unidade
systemd independente; isso evita que FACIL ou outro worker seja iniciado duas
vezes.

SAFE, Grid e EasyConsig estão bloqueados para novos jobs até possuírem adapter
transacional homologado.

## Configuração no painel

A ordem recomendada é:

1. abra **Robôs e regras** e confira processadora, URLs, entrada, agenda e
   concorrência;
2. cadastre 2Captcha e Telegram em **Integrações**;
3. cadastre um ou mais acessos em **Acessos aos portais**;
4. crie tokens separados para workers e Telegram;
5. importe uma base em **Bases**;
6. inicie o job e acompanhe **Execuções** e **Eventos**;
7. acompanhe o arquivo final em **Envios**.

Prontidão é fail-closed: o painel explica o motivo e a próxima ação quando um
convênio não pode rodar.

## Formato da base

- XLSX ou CSV;
- primeira coluna obrigatoriamente CPF;
- segunda coluna MATRICULA quando presente;
- matrícula pode ser obrigatória conforme o convênio;
- CPF passa por validação dos dígitos verificadores;
- duplicatas podem manter a primeira, manter todas ou rejeitar a base;
- colunas adicionais são preservadas no registro de origem.

Cada base fica vinculada a um convênio e pode ser reutilizada em vários jobs.

## Concorrência e recuperação

O backend combina o limite do convênio, acessos utilizáveis, workers online e
itens prontos. PostgreSQL reserva credenciais e itens com
FOR UPDATE SKIP LOCKED. Cada item possui histórico de tentativas, lease,
backoff e limite persistente.

O worker não decide horário nem retry. Ele só executa jobs que a API marca como
executáveis. Se todos os itens estiverem aguardando backoff, nenhum login ou
captcha é aberto.

## Telegram

Existe um único TELEGRAM_BOT_TOKEN. O controlador seleciona convênio e base
existente; nunca cria um job sem base. O resultado é enviado somente ao chat
que solicitou o job.

Configure no cofre ou no .env:

    TELEGRAM_BOT_TOKEN=...
    TELEGRAM_ALLOWED_USER_IDS=123456789
    BACKEND_API_URL=http://127.0.0.1:8000
    TELEGRAM_BACKEND_API_TOKEN=...
    WORKER_API_URL=http://127.0.0.1:8000
    WORKER_API_TOKEN=...
    TWOCAPTCHA_API_KEY=...

Escopos:

- controlador Telegram: jobs:read,jobs:write;
- workers: jobs:read,workers:execute.

A conclusão grava o envio na outbox. Falhas não revertem o job nem repetem
consultas; a tela **Envios** mostra o erro e permite tentar novamente.

## Segurança

- não versione .env, banco, downloads ou credenciais;
- use HTTPS e ADMIN_COOKIE_SECURE=true em produção;
- mantenha tokens separados por serviço e com validade;
- faça backup diário do PostgreSQL, storage e APP_MASTER_KEY;
- use um usuário de sistema sem privilégios para os serviços;
- rotacione senhas e tokens divulgados fora do cofre.

As credenciais de portal permanecem compatíveis com o armazenamento legado em
texto no banco, mas também mantêm a cópia AES-GCM. Senhas do painel e tokens de
API nunca são armazenados em texto.

## Verificação

    PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -v
    alembic upgrade head --sql
    git diff --check

Antes de liberar um adapter ou convênio novo, faça smoke real e revise
manualmente ao menos dez retornos.

## Deployment

deploy.py cria releases imutáveis em `/opt/machine/releases`, faz backup antes
da migration, troca o symlink `current` de forma atômica e restaura o código
anterior se a ativação falhar. Para preparar sem mudar a produção:

    python deploy.py deploy

Para preparar e ativar em uma única operação:

    python deploy.py deploy --activate

Na ativação ele:

1. encerra os browsers graciosamente;
2. cria backup do PostgreSQL, storage e configuração;
3. aplica `alembic upgrade head`;
4. garante tokens separados para workers e Telegram;
5. instala unidades com usuários Linux e ambientes mínimos por serviço;
6. valida `/health` e uma janela sem reinícios dos processos.

O deploy não retoma jobs pausados/cancelados. Depois, valide um job pequeno no
painel. Rollback de código fica disponível por `python deploy.py rollback`;
migrations aditivas não são revertidas automaticamente.

Credenciais SSH vêm apenas de MACHINE_SSH_HOST, MACHINE_SSH_USER e
MACHINE_SSH_KEY_FILE ou MACHINE_SSH_PASSWORD.
