# Checkpoint e pendências

## Entregue neste checkpoint

- Painel administrativo FastAPI com login, papéis, usuários e auditoria.
- Tokens de API com hash, escopos e revogação.
- Cofre AES-GCM para integrações e credenciais de portais.
- PostgreSQL obrigatório e migration Alembic inicial.
- Bases, CPFs, linhas de origem e resultados cifrados.
- Jobs e itens idempotentes com leases, heartbeat e `SKIP LOCKED`.
- Pool RF1/Boa Vista com até três credenciais sobre a mesma base.
- Exportação XLSX sob demanda, sem manter resultado aberto em disco.
- Um único adaptador e um único token operacional do Telegram.
- Fênix removido; backend SQLite e scheduler legado removidos.
- Deploy sem encerramento automático de processos e unidades systemd atualizadas.
- Itabuna identificado corretamente como ConsigX/Consiglog separado.

## Bloqueios para homologação em produção

- Criar um PostgreSQL limpo na VPS e aplicar `alembic upgrade head`.
- Configurar `APP_MASTER_KEY`, segredo de sessão, domínio e proxy HTTPS.
- Criar os tokens internos do Telegram e dos workers com escopos mínimos.
- Cadastrar de um a três logins válidos de Boa Vista no painel.
- Executar smoke test com poucos CPFs e depois a base completa de 63 registros.
- Validar concorrência real com três contas e comportamento de sessão do RF1.
- Configurar backup conjunto do PostgreSQL, `storage/` e chave mestre.

## Próximas implementações

- Teste manual de credencial e edição de convênios pelo painel.
- ETA, velocidade, workers ativos e agrupamento de erros no dashboard.
- Cancelamento e retry individual de jobs/itens.
- Política de retenção e expurgo auditável de bases e resultados.
- Rotação versionada da chave mestre.
- Adaptar SafeConsig, FácilConsig e Grid ao contrato transacional de workers.
- Homologar uma consulta de Itabuna que retorne dados, para confirmar todos os
  campos de margem antes de iniciar lote de produção.
- Decidir a remoção dos três arquivos históricos de dados ainda versionados.

## Decisões mantidas

- Itabuna não compartilha o worker de Boa Vista.
- Uma sessão simultânea por credencial de portal é o padrão seguro.
- Excel é somente entrada/saída; PostgreSQL é a fonte de verdade.
- O recurso remoto noVNC/ngrok permanece inalterado por decisão do projeto.
- Segredos divulgados durante a implementação devem ser rotacionados antes da
  homologação em produção.
