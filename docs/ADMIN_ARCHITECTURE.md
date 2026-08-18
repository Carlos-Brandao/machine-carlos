# Arquitetura administrativa e operacional

## Componentes

1. **Backend FastAPI**: painel, autenticação, API, prontidão e autoridade da
   fila.
2. **PostgreSQL**: catálogo, bases, itens, tentativas, leases, resultados,
   auditoria, workers e outbox.
3. **GenericWorker**: laço comum de execução.
4. **Adapters**: RF1, FACILCONSIG e CONSIGX.
5. **Notification worker**: entrega durável, com semântica at-least-once.
6. **Telegram controller**: interface do usuário; não executa navegador.

## Limites de responsabilidade

O backend decide se um job é executável a partir do estado do convênio, adapter,
URLs, segredos, acessos, worker online, agenda, not_before e itens prontos.

O GenericWorker:

- anuncia saúde;
- consulta jobs executable;
- adquire acesso;
- abre sessão pelo adapter;
- reserva itens;
- renova leases;
- envia outcome ou requeue;
- sempre fecha a sessão e libera acesso.

O adapter não conhece banco, agenda, Excel, Telegram ou retry.

## Domínio

- platforms representam processadoras;
- municipalities representam convênios;
- portal_credentials são acessos vinculados ao convênio;
- datasets e dataset_records formam bases reutilizáveis;
- automation_jobs e job_items formam a execução;
- job_item_attempts preserva cada tentativa;
- credential_leases impede sessão duplicada;
- worker_heartbeats torna capacidade observável;
- consultation_results_v2 guarda o envelope cifrado;
- job_events_v2 forma a linha do tempo;
- notification_outbox desacopla entrega externa;
- audit_logs registra mudanças administrativas.

## Fluxo

1. arquivo é validado, normalizado e cifrado;
2. base pronta inicia um job com um item por registro;
3. worker se anuncia antes de consultar a fila;
4. API marca o job executable ou explica o bloqueio;
5. aquisição de acesso e claim usam locks transacionais;
6. adapter confirma o identificador e classifica o retorno;
7. backend persiste tentativa, resultado ou próximo retry;
8. último item atualiza o estado do job;
9. job final grava mensagem na outbox;
10. notification worker cria o Excel e envia ao chat do pedido.

## Capacidade

A concorrência real de um convênio nunca ultrapassa max_workers. Também é
limitada por acessos utilizáveis, workers online e itens prontos. Uma trava no
convênio serializa aquisição e evita abrir captchas excedentes quando resta
apenas um item.

## Falhas

- worker morto: lease expira, tentativa vira abandoned e item volta à fila;
- timeout do portal: retryable_error com backoff;
- credencial inválida: somente aquele acesso fica invalid;
- portal ou integração indisponível: acesso entra em cooldown sem transformar
  o item em não encontrado;
- Telegram fora: outbox tenta novamente sem afetar o job;
- alteração de seletor: adapter retorna retry, nunca not_found por silêncio.

## Segurança

Sessões do painel usam cookie seguro e CSRF. Senhas administrativas usam
Argon2. Tokens de API usam hash e escopos. Bases, resultados e segredos usam
AES-GCM com contexto. Respostas que entregam credencial ao worker usam
Cache-Control no-store. Por decisão operacional, administradores podem conferir
a senha de portal na tela de edição; tokens e segredos de integração continuam
sem leitura no painel.

Workers e Telegram não recebem DATABASE_URL nem APP_MASTER_KEY. Eles obtêm
somente os segredos operacionais permitidos pelo próprio escopo em uma rota
interna no-store; a rotação feita no painel é observada sem reiniciar o serviço.

## Operação

Telas:

- Visão geral: prontidão e jobs recentes;
- Execuções: progresso e controles;
- Eventos: diagnóstico;
- Envios: Telegram e retry;
- Bases: importação e reutilização;
- Robôs e regras: catálogo, entrada, agenda e capacidade;
- Acessos aos portais: pool por convênio;
- Usuários, Tokens e Integrações: administração.

Consulte BUSINESS_RULES.md para as regras funcionais e ADAPTER_CONTRACT.md para
novos portais.
