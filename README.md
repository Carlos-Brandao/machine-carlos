# Machine — Central de Robôs

Aplicação para importar bases, executar consultas de margem em portais de
consignação, acompanhar tentativas, agendar consultas e exportar resultados pelo painel ou API.

O sistema usa:

- FastAPI para painel e API;
- PostgreSQL como fonte oficial;
- Alembic para migrations;
- GenericWorker para execução concorrente;
- adapters isolados para RF1, FACILCONSIG, SAFE e CONSIGX;
- Playwright nos portais;
- agendamentos persistentes, exportações versionadas e webhooks assinados.

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
    python run_worker.py safeconsig
    python run_operational_scheduler.py

run_scheduler.py permanece como supervisor local compatível e inicia somente os
adapters transacionais habilitados. Em produção, cada pool usa uma unidade
systemd independente; isso evita que FACIL ou outro worker seja iniciado duas
vezes.

Grid e EasyConsig estão bloqueados para novos jobs até possuírem adapter
transacional homologado.

## Configuração no painel

A ordem recomendada é:

1. abra **Configurações → Convênios e regras** e confira URLs, janela e limite de acessos;
2. configure 2Captcha em **Configurações → Integrações**;
3. cadastre usuários diferentes em **Acessos** e use **Testar acesso**;
4. importe uma base em **Bases**;
5. abra **Nova consulta**, escolha a base, os acessos e o paralelismo;
6. acompanhe retornos, erros e progresso por acesso em **Consultas**;
7. use **Agendamentos** para recorrência e **Configurações → API** para integrações.

Prontidão é fail-closed: o painel explica o motivo e a próxima ação quando um
convênio não pode rodar.

## Formato da base

- XLSX ou CSV;
- primeira coluna obrigatoriamente CPF;
- segunda coluna MATRICULA quando presente;
- matrícula pode ser obrigatória conforme o convênio;
- CPF passa por validação dos dígitos verificadores;
- registros repetidos são ignorados pela chave do convênio (CPF ou CPF + matrícula);
- colunas adicionais são preservadas no registro de origem.

O upload solicita nome, convênio e tipo (Efetivos, Temporários, Comissionados ou
Geral). Cada convênio tem uma base ativa por tipo. Novos uploads para o mesmo
par acrescentam apenas registros ausentes, preservando nome e ID. Bases
específicas complementam a Geral, que também pode receber uploads diretos.

Em **Bases**, é possível consultar registros, editar nome/tipo e remover uma
base do catálogo. A remoção desativa suas agendas e preserva consultas anteriores;
registros de bases específicas permanecem na Geral. Cada novo job recebe uma
lista fixa dos registros existentes no momento da criação.

## Concorrência e recuperação

O backend combina o limite do convênio, acessos utilizáveis, workers online e
itens prontos. PostgreSQL reserva credenciais e itens com
FOR UPDATE SKIP LOCKED. Cada item possui histórico de tentativas, lease,
backoff e limite persistente.

O worker não decide horário nem retry. Ele só executa jobs que a API marca como
executáveis. Se todos os itens estiverem aguardando backoff, nenhum login ou
captcha é aberto.

## Agendamentos, API e entregas

Telegram foi retirado. Nenhum serviço ou robô envia mensagens pelo bot.
Dados históricos e backups são preservados; tokens locais antigos são revogados.

Agendamentos usam cron de cinco campos e fuso IANA, exibindo as próximas
execuções. Uma ocorrência só gera uma consulta; execuções sobrepostas da
mesma agenda são ignoradas e registradas. Após indisponibilidade, horários
fora da tolerância não geram uma tempestade de consultas atrasadas.

A documentação autenticada por token para uso das rotas está em `/docs`.
Tokens têm escopos separados; o painel usa sessão e CSRF.
Criação aceita chave de idempotência. Resultados são paginados. A exportação
por API gera artefato XLSX/CSV/JSON com snapshot e checksum, consultável até
ficar pronto. Webhooks informam metadados de conclusão, nunca o conteúdo da base.

Workers usam apenas `jobs:read,workers:execute`; não recebem senha do banco.
O serviço scheduler executa manutenção, agendas, exportações e entregas,
independente do número de processos web.

Rotas principais (todas exigem Bearer com o respectivo escopo):

| Operação | Rota | Escopo |
|---|---|---|
| Importar base | POST /api/v1/datasets (multipart) | datasets:write |
| Listar/detalhar bases | GET /api/v1/datasets e /{id} | datasets:read |
| Editar nome/tipo | PATCH /api/v1/datasets/{id} | datasets:write |
| Remover do catálogo | DELETE /api/v1/datasets/{id} | datasets:write |
| Criar consulta | POST /api/v1/jobs | jobs:write |
| Acompanhar | GET /api/v1/jobs/{id} | jobs:read |
| Resultados | GET /api/v1/jobs/{id}/results?after_id=0&limit=100 | results:read |
| Preparar arquivo | POST /api/v1/jobs/{id}/exports | exports:write |
| Estado/arquivo | GET /api/v1/exports/{id} e /download | exports:read |
| Agendar | POST /api/v1/schedules | schedules:write |

Importação exige os campos multipart `display_name`, `municipality_slug`,
`dataset_type` (`efetivos`, `temporarios`, `comissionados` ou `geral`) e `file`.
O retorno informa o ID da base criada ou complementada e a quantidade adicionada.

Exemplo de criação: `{"dataset_id": 6, "selected_credential_ids": [2, 8],
"max_parallel_accounts": 2}`, com header `Idempotency-Key: minha-solicitacao-unica`.
Use IDs reais do seu cadastro. O teto do convênio deve permitir dois acessos.
Exportação recebe `{"format":"xlsx"}` (ou csv/json), retorna 202 e ID; consulte
o estado antes de baixar. Operadores via API só acessam registros próprios;
administradores acessam o histórico global, sempre com escopo explícito.

Webhooks só aceitam domínios previamente listados em `WEBHOOK_ALLOWED_HOSTS`.
O receptor deve validar `X-Machine-Signature: t=timestamp,v1=hex`, calculado com
HMAC-SHA256 sobre `timestamp + '.' + corpo`, rejeitar timestamps antigos e
deduplicar pelo header `Idempotency-Key`. O segredo de assinatura é retornado
somente ao cadastrar o destino. Não coloque senhas no código nem em URLs.

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
4. retira o Telegram e garante token dedicado aos executores;
5. instala unidades com usuários Linux e ambientes mínimos por serviço;
6. valida `/health` e uma janela sem reinícios dos processos.

O deploy não retoma jobs pausados/cancelados. Depois, valide um job pequeno no
painel. Rollback de código fica disponível por `python deploy.py rollback`;
migrations aditivas não são revertidas automaticamente.

Credenciais SSH vêm apenas de MACHINE_SSH_HOST, MACHINE_SSH_USER e
MACHINE_SSH_KEY_FILE ou MACHINE_SSH_PASSWORD.
