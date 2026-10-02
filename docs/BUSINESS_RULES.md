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
- **Base**: coleção reutilizável de registros de um convênio e de um tipo.
  Um arquivo importado cria ou complementa essa coleção.
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

## Teste de acesso

- Testar acesso verifica somente o login; não consulta a base nem retoma uma consulta pausada.
- Se o login estiver ocupado, o painel informa qual consulta está usando-o.
- Um teste pode ser cancelado. O acesso só é liberado depois de fechar a sessão ou expirar a reserva.
- Testes não têm progresso retomável; um novo teste é uma nova autenticação e pode consumir captcha.
- O estado é atualizado automaticamente. O limite do teste em execução é cinco minutos; o encerramento respeita os timeouts da operação de rede em andamento.
- Cancelar não marca a senha como inválida. Cancelar um teste que ainda não começou permite novo teste sem a espera anti-repetição; testes iniciados mantêm a proteção de 15 minutos.

## Entrada e bases

- A primeira coluna é sempre CPF.
- O CPF é normalizado para 11 dígitos e validado pelos dois dígitos
  verificadores.
- A segunda coluna é MATRICULA quando presente.
- Cada convênio informa se matrícula é obrigatória.
- Colunas adicionais são preservadas como dados de origem, sem criar colunas
  físicas no PostgreSQL.
- O upload informa nome, convênio e tipo: Efetivos, Temporários, Comissionados
  ou Geral. Existe no máximo uma base ativa por convênio e tipo.
- Ao importar novamente para o mesmo convênio e tipo, a base conserva seu ID
  e nome. Somente registros ainda ausentes são acrescentados; os anteriores
  não são sobrescritos. O painel informa quantos foram acrescentados e ignorados.
- Cada importação específica também acrescenta os registros ausentes à Geral.
  A Geral é a união das bases específicas e dos registros enviados diretamente
  para ela, sem repetição da chave lógica.
- A chave lógica pertence ao convênio: CPF ou CPF + matrícula. CPF igual com
  matrícula diferente é um vínculo distinto quando o convênio usa os dois campos.
- Nome e tipo podem ser editados; um tipo já ocupado não pode receber outra
  base ativa. O convênio de uma base permanece fixo.
- Remover retira a base do catálogo ativo e desativa suas agendas, preservando
  registros, jobs e resultados históricos. Remover uma base específica mantém
  seus registros na Geral. Para remover a Geral, remova antes as específicas.
- Jobs recebem uma lista fixa dos registros existentes na criação. Complementar
  uma base não altera jobs já criados; a próxima execução usa a base atualizada.
- Bases anteriores à migração permanecem sem classificação, com seus IDs,
  registros e vínculos preservados. A opção Editar permite atribuir um tipo
  explicitamente; novos uploads sempre exigem tipo.

O cadastro de acessos solicita apenas convênio, identificação, usuário e senha.
A processadora é obtida do convênio; não há campo separado de consignatária.
Seleções internas já usadas pelos adaptadores de portais são preservadas.

## Jobs e controles

Fluxo normal: queued → running → completed, completed_with_errors ou failed.

- pausing impede novas reservas e espera a consulta em andamento/logout; somente depois vira paused.
- cancelling aguarda fechamento das sessões antes de cancelar os itens restantes.
- blocked exige correção operacional antes de retomar.
- **Pausar** preserva o progresso e permite retomar.
- **Interromper** cancela definitivamente o restante daquela execução.
- **Retomar** continua itens pendentes de um job pausado ou bloqueado.
- **Tentar novamente** reabre somente itens falhos ou cancelados e concede três
  novas tentativas; itens concluídos não são repetidos.

Podem existir várias consultas na fila. Apenas uma roda por convênio; pausadas não bloqueiam novas consultas. A seleção de contas é congelada ao criar a consulta.

## Concorrência, leases e workers

A capacidade efetiva é limitada pelo menor conjunto disponível entre:

- limite de acessos solicitado na consulta e teto explícito do convênio;
- número de acessos utilizáveis;
- workers online;
- itens prontos.

O backend reserva acessos e itens com transações PostgreSQL e
FOR UPDATE SKIP LOCKED. Um acesso não pode ser usado por duas sessões ao mesmo
tempo. O worker renova o lease do acesso e dos itens; leases expirados podem ser
recuperados. Cada reserva tem um token de geração: resposta atrasada não sobrescreve a geração atual. Heartbeat independe do tempo gasto no navegador/captcha. Repetir a confirmação da mesma geração não duplica resultados.

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
limite do item (três erros técnicos por padrão). Login, portal fora e integração fora não consomem esse orçamento. Três logins não confirmados bloqueiam somente aquele acesso até correção/teste; as contas saudáveis continuam. Enquanto todos os itens aguardam o próximo
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

## Agendas e entregas

- Não há integração Telegram ativa.
- Agenda: base fixa, seleção de acessos, limite, cron e fuso explícitos.
- Ocorrências são únicas por agenda+horário; sobreposição da mesma agenda é ignorada.
- Horários perdidos fora da tolerância são registrados e não reexecutados em massa.
- API de criação aceita chave de idempotência. Mesma chave e payload reaproveitam a consulta.
- Resultados por API usam cursor. Exportação por API é assíncrona e congela o snapshot solicitado.
- Nova tentativa incrementa a versão do resultado e preserva exportações anteriores.
- Webhooks assinados transmitem apenas identificação e estado; o consumidor busca os dados com token.
- Entrega de webhook é at-least-once: o receptor deve deduplicar pelo ID do evento.
- Uma queda depois da consulta ao portal pode obrigar nova consulta; somente a persistência é idempotente.

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
| SAFE | safeconsig.v1 | transacional; prontidão por convênio |
| Grid | legado | indisponível para novos jobs |
| EasyConsig | ausente | indisponível |

Convênios novos e Grid só podem ser liberados após cumprir
[ADAPTER_CONTRACT.md](ADAPTER_CONTRACT.md).
