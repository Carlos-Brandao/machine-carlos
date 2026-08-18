# Checkpoint e pendências

## Entregue

- domínio explícito de processadora, convênio, prontidão, entrada e agenda;
- GenericWorker único para RF1, FACIL e ConsigX;
- outcomes canônicos com confirmação de CPF/matrícula;
- tentativas persistentes, backoff, leases e heartbeat de workers;
- SAFE, Grid e EasyConsig bloqueados até adapter transacional;
- bases reutilizáveis com CPF validado e política de duplicidade;
- exportação com namespaces estáveis, sem sobrescrever dados de origem;
- job com pausar, interromper, retomar e tentar novamente;
- um único bot Telegram, sempre com base explícita;
- outbox de Telegram com retry e tela de Envios;
- painel de Robôs e regras, prontidão explicável e ações auditadas;
- usuários ativáveis, papéis, reset de senha e tokens com expiração;
- migration 0006 aditiva, preservando dados existentes.

## Antes de homologar na VPS

1. fazer backup consistente do PostgreSQL, storage e .env;
2. aplicar alembic upgrade head;
3. instalar e habilitar pools RF1, FACIL, ConsigX e notificações exatamente uma
   vez cada;
4. confirmar WORKER_API_TOKEN, BACKEND_API_TOKEN, Telegram e 2Captcha;
5. validar /health e a presença dos workers no painel;
6. executar smoke pequeno de Boa Vista, GOV AM e Paulista;
7. revisar manualmente dez retornos e a planilha exportada de cada adapter;
8. testar um envio Telegram e seu reprocessamento;
9. monitorar logs, CPU, memória e consumo de captcha.

## Homologações de portal ainda necessárias

- Boa Vista: confirmar evidência negativa explícita em um CPF inexistente.
- GOV AM e Paulista: confirmar a evidência negativa explícita e os nomes de
  todas as margens.
- Itabuna: revalidar CPF confirmado após postback e manter estado testing até
  dez consultas revisadas.
- SAFE: implementar adapter transacional do zero.
- Grid: implementar adapter transacional do zero.

## Backlog técnico

- teste de integração da fila contra PostgreSQL real;
- métricas e alertas externos para workers, retries e outbox;
- política de retenção/expurgo auditável;
- rotação versionada da APP_MASTER_KEY;
- remover runners históricos depois da homologação dos adapters;
- testes de navegador gravados por portal para detectar mudança de seletor.

## Decisões

- um acesso de portal representa uma sessão simultânea;
- PostgreSQL é a fonte oficial; Excel é entrada e saída;
- o banco, não o catálogo Python, é a fonte das regras editáveis;
- o backend, não o worker, decide agenda, retry e executabilidade;
- not_found exige evidência do portal;
- resultado Telegram sempre usa o chat associado ao pedido;
- credenciais e tokens divulgados fora do cofre devem ser rotacionados.
