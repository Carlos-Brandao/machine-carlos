# Regras de negócio operacionais

Este documento é o contrato funcional do sistema. Se código, painel e este
arquivo divergirem, a decisão deve ser centralizada no domínio persistido e a
divergência tratada como defeito.

## Vocabulário

- **Processadora**: tecnologia do portal, por exemplo RF1, FACILCONSIG ou
  CONSIGX. Define o adapter e uma janela de horário padrão.
- **Convênio**: órgão consultado, por exemplo Boa Vista, GOV AM, Paulista ou
  Itabuna. Sempre pertence a exatamente uma processadora.
- **Acesso ao portal**: usuário e senha de um convênio. Um acesso representa no
  máximo uma sessão simultânea.
- **Base**: arquivo importado e reutilizável, sempre vinculado a um convênio.
- **Job**: execução de uma base por um convênio.
- **Item**: uma linha consultável do job.
- **Worker**: executor genérico; ele não decide agenda, retry ou prontidão.
- **Adapter**: implementação específica de login, consulta, extração e
  classificação de um portal.

## Catálogo e prontidão

O banco é a fonte oficial. O catálogo em services/registry.py somente cria
registros ausentes e nunca sobrescreve alterações feitas no painel.

| Estado | Aceita novo job? | Uso |
|---|---:|---|
| draft | não | cadastro incompleto |
| testing | não | homologação com execução assistida |
| ready | sim, se o checklist passar | produção |
| degraded | não | incidente ou baixa confiabilidade |
| paused | não | pausa operacional |
| retired | não | convênio encerrado |

Mesmo em ready, um job só pode iniciar quando:

1. convênio e processadora estão ativos;
2. existe adapter transacional homologado;
3. URLs de login e consulta estão preenchidas;
4. existe ao menos um acesso realmente utilizável;
5. integrações obrigatórias, como 2Captcha, estão configuradas;
6. existe worker online da processadora;
7. o horário do convênio permite execução;
8. há item elegível agora;
9. o limite de concorrência não foi atingido.

O painel mostra a causa e a próxima ação de cada bloqueio.

## Entrada e bases

- A primeira coluna é sempre CPF.
- O CPF é normalizado para 11 dígitos e validado pelos dois dígitos
  verificadores.
- A segunda coluna é MATRICULA quando presente.
- Cada convênio informa se matrícula é obrigatória.
- Colunas adicionais são preservadas como dados de origem, sem criar colunas
  físicas no PostgreSQL.
- Uma base pertence a um único convênio, recebe nome amigável e pode iniciar
  vários jobs ao longo do tempo.

Políticas de duplicidade:

- keep_first: mantém a primeira ocorrência da chave lógica;
- keep_all: mantém todas;
- reject: rejeita a importação se houver repetição.

A chave lógica também pertence ao convênio: CPF ou CPF + matrícula. Bases
anteriores à migration preservam keep_all; novas bases usam keep_first por
padrão.

## Jobs e controles

Fluxo normal: queued → running → completed, completed_with_errors ou failed.

- paused devolve leases em andamento para a fila.
- cancelled encerra totalmente os itens ainda pendentes.
- blocked exige correção operacional antes de retomar.
- **Pausar** preserva o progresso e permite retomar.
- **Interromper** cancela definitivamente o restante daquela execução.
- **Retomar** continua itens pendentes de um job pausado ou bloqueado.
- **Tentar novamente** reabre somente itens falhos ou cancelados e concede três
  novas tentativas; itens concluídos não são repetidos.

Somente um job ativo por convênio é criado pelo painel ou Telegram.

## Concorrência, leases e workers

A capacidade efetiva é limitada pelo menor conjunto disponível entre:

- Municipality.max_workers;
- número de acessos utilizáveis;
- workers online;
- itens prontos.

O backend reserva acessos e itens com transações PostgreSQL e
FOR UPDATE SKIP LOCKED. Um acesso não pode ser usado por duas sessões ao mesmo
tempo. O worker renova o lease do acesso e dos itens; leases expirados podem ser
recuperados.

O GenericWorker consulta somente jobs marcados pelo backend com
executable=true. Não existe uma segunda regra de horário ou retry no executor.

## Resultado e retry

- found: o portal confirmou CPF e, quando solicitada, matrícula;
- not_found: o portal exibiu evidência negativa explícita;
- retryable_error: falha técnica temporária;
- permanent_error: erro definitivo daquele item;
- credential_error: acesso recusado;
- portal_unavailable: portal indisponível;
- integration_unavailable: dependência externa indisponível.

Timeout, seletor ausente, HTML inesperado ou bug não podem virar not_found. Na
dúvida, o adapter devolve falha retentável.

O backend persiste cada tentativa, aplica backoff exponencial e encerra no
limite do item (três por padrão). Enquanto todos os itens aguardam o próximo
retry, nenhum worker abre login, navegador ou captcha.

Jobs anteriores à migração do contrato canônico não permitem reconstruir com
segurança a diferença entre encontrado e não encontrado, pois o retorno antigo
está cifrado e guardava apenas completed/failed no índice. O painel os identifica
explicitamente como **legado sem classificação**; ele nunca converte esses
registros em encontrado por suposição.

## Exportação

O Excel mantém as colunas originais. Dados canônicos usam os prefixos
SOLICITADO_, CONFIRMADO_, SERVIDOR_ e MARGEM_; campos específicos do portal
usam RETORNO_. Assim um retorno nunca substitui silenciosamente o CPF ou outra
coluna de entrada. Se a própria base já tiver um desses nomes reservados, a
coluna produzida pelo sistema recebe o prefixo SAIDA_ e a original é preservada.

## Telegram e entregas

- Há um único TELEGRAM_BOT_TOKEN.
- O Telegram só cria jobs com base existente e explicitamente selecionada.
- O arquivo final é enviado exclusivamente ao telegram_chat_id do pedido.
- Concluir o job somente grava uma mensagem na outbox.
- Um processo separado tenta entregar até cinco vezes com backoff.
- Falhas e reprocessamento manual ficam na tela **Envios**.
- O agendamento da outbox é idempotente, mas o Telegram oferece entrega
  **at-least-once**: uma queda depois de o Telegram aceitar o arquivo e antes do
  commit pode gerar repetição. A legenda inclui o ID do envio para identificá-la.

## Segurança e auditoria

- Login do painel possui limitação de tentativas.
- Papéis: admin, operator e viewer.
- Tokens têm escopos, validade e revogação.
- Senhas do painel são hashes Argon2.
- Tokens de API são armazenados somente como hash.
- Dados de bases, resultados e cofre usam AES-GCM.
- Credenciais de portal ficam em texto consultável somente na tela de edição
  restrita a administradores, conforme a decisão operacional do projeto.
- Mudanças administrativas e ações de job são auditadas.

## Situação dos adapters

| Processadora | Adapter | Estado |
|---|---|---|
| RF1 | rf1.v1 | transacional |
| FACILCONSIG | facil.v1 | transacional |
| CONSIGX | consiglog.v1 | transacional/em homologação por convênio |
| SAFE | legado | indisponível para novos jobs |
| Grid | legado | indisponível para novos jobs |
| EasyConsig | ausente | indisponível |

SAFE e Grid só podem ser liberados após cumprir
[ADAPTER_CONTRACT.md](ADAPTER_CONTRACT.md).
