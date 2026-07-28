# Arquitetura alvo — painel e execução concorrente

## Princípios

- PostgreSQL é a fonte oficial de configuração, fila, progresso e resultados.
- Excel é formato de importação e exportação, não banco operacional.
- Um job representa uma base; cada CPF vira um item idempotente do job.
- Cada credencial de portal recebe no máximo uma sessão simultânea por padrão.
- Workers usam perfis de navegador isolados por credencial e lease.
- Segredos só são entregues ao worker que possui lease, em resposta `no-store`,
  e nunca são gravados em logs.

## Modelos propostos

### Administração

- `admin_users`: login, hash Argon2, papel (`admin`, `operator`, `viewer`), ativo e último acesso.
- `api_tokens`: dono, prefixo público, hash do token, escopos, expiração e revogação.
- `audit_logs`: ator, ação, entidade, identificador, IP, data e metadados sem segredos.

### Catálogo e credenciais

- `platforms`: RF1, FácilConsig, SafeConsig, Grid e Consiglog.
- `municipalities`: convênio/prefeitura, plataforma, URLs, janela e estado ativo.
- `portal_credentials`: prefeitura, rótulo, usuário cifrado, senha cifrada, estado,
  limite de sessões, falhas consecutivas, cooldown e última validação.
- `integration_secrets`: Telegram, 2Captcha e outras integrações, com valor cifrado,
  versão da chave e data da última rotação.

Credenciais recuperáveis usam AES-GCM com uma chave mestre fora do banco
(`APP_MASTER_KEY`). Tokens de acesso à API, que não precisam ser recuperados,
são armazenados somente como hash e exibem apenas seu prefixo.

### Bases, fila e resultados

- `datasets`: arquivo importado, prefeitura, quantidade, checksum e estado.
- `dataset_records`: CPF cifrado, fingerprint HMAC para deduplicação, últimos quatro
  dígitos e dados originais em `JSONB`.
- `jobs`: base, prefeitura, prioridade, estado, totais e solicitante.
- `job_items`: um registro consultável por job, tentativa, lease, estado e erro.
- `credential_leases`: credencial, worker, heartbeat e expiração.
- `consultation_results_v2`: item, credencial utilizada, estado e resultado
  cifrado com AES-GCM.
- `job_events`: histórico imutável de transições e mensagens operacionais.

Restrições únicas em `(job_id, dataset_record_id)` e operações de `UPSERT` tornam
retomadas idempotentes.

## Concorrência com três usuários

1. A base é normalizada uma vez e seus registros viram `job_items` pendentes.
2. Cada worker reserva uma credencial disponível com
   `SELECT ... FOR UPDATE SKIP LOCKED`.
3. O worker cria um contexto Playwright isolado e reserva pequenos lotes de itens.
4. Heartbeats renovam o lease; itens de workers mortos voltam à fila após expiração.
5. Três credenciais ativas permitem três workers na mesma base sem consultar o
   mesmo CPF duas vezes.
6. Falhas de login colocam somente aquela credencial em cooldown; o job continua
   com as demais.

Não se deve abrir três sessões com o mesmo usuário do portal sem validação. Muitos
portais invalidam a sessão anterior; o padrão será uma sessão por credencial.

## Rotas implementadas

### Sessão e administração

- `GET/POST /login` e `POST /logout`
- `GET/POST /admin/users`
- `GET/POST /admin/tokens` e revogação
- `GET/POST /admin/credentials` e ativação/desativação
- `GET/POST /admin/secrets`
- `GET/POST /admin/datasets`
- `GET /admin/jobs` e `GET /admin/jobs/{id}/export.xlsx`

### Operação

- `POST /api/jobs/batch`
- `GET /api/jobs/status` e `GET /api/jobs/queue`
- `POST /api/jobs/queue/clear` e `POST /api/jobs/running/stop`
- `POST /api/workers/credentials/acquire` e relatório de saúde
- `POST /api/workers/items/claim` e conclusão
- `POST /api/workers/heartbeat` e liberação

## Painel mínimo

- Login.
- Dashboard com jobs, progresso e falhas.
- Bases: upload, validação e início de job.
- Credenciais: cadastro, ativação e cooldown automático; nunca mostrar senha.
- Usuários e tokens: criação, papéis, escopos e revogação.
- Job: itens processados, falhas e exportação.

Próximos incrementos do painel: teste manual de credencial, edição de convênios,
velocidade/ETA, erros agrupados e ações individuais de cancelar/retentar.

FastAPI, SQLAlchemy 2, Alembic e templates Jinja/HTMX são suficientes para esse
painel interno sem investir em um frontend separado.

## Estado de implementação

1. Cadastro central, modelos SQLAlchemy e migration PostgreSQL: concluídos.
2. Autenticação, tokens, cofre cifrado e auditoria: concluídos.
3. Datasets, jobs, itens, leases e exportação cifrada: concluídos.
4. Worker Boa Vista no contrato transacional: implementado; falta homologação
   real na VPS com credenciais válidas e uma base pequena.
5. Concorrência: protegida por leases e `SKIP LOCKED`; falta teste integrado com
   PostgreSQL real e três logins do portal.
6. Itabuna/Consiglog: separado e pendente de revalidação do novo site.
7. Demais runners: migração incremental após Boa Vista estabilizar.
