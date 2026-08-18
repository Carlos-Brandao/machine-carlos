# Contrato de adapters

Um adapter conhece somente o portal. Agenda, fila, concorrência, leases,
tentativas, notificações e Telegram pertencem ao backend e ao GenericWorker.

## Interface

Cada adapter declara:

- platform;
- version;
- batch_size;
- lease_seconds;
- open_session(credential);
- classify_exception(exc, stage, item).

A sessão aberta implementa consult(item), que devolve ExecutionOutcome, e
close(). O login deve ser confirmado positivamente e close deve ser idempotente.

## Envelope

Exemplo:

    {
      "outcome": "found",
      "requested": {"cpf": "00000000000", "registration": "ABC"},
      "confirmed": {"cpf": "00000000000", "registration": "ABC"},
      "person": {"name": "Pessoa"},
      "margins": {"consignable": "125,40"},
      "raw": {"Campo do portal": "valor"}
    }

Regras:

- found exige CPF confirmado igual ao solicitado;
- se matrícula foi solicitada, ela também deve ser confirmada;
- not_found exige mensagem ou estado negativo explícito do portal;
- ausência de seletor, timeout ou HTML desconhecido é retentável;
- credencial inválida, portal fora e integração fora são categorias distintas;
- dados específicos ficam em raw e nunca sobrepõem o envelope.

## Proibições

Um adapter não pode:

- buscar ou alterar jobs no banco;
- decidir dia ou horário;
- manter contador próprio de retry;
- enviar Telegram;
- ler arquivo Excel;
- escolher outro convênio;
- engolir exceção de captcha ou rede e retornar not_found;
- criar concorrência interna fora do GenericWorker.

## Checklist para homologação

1. login válido confirmado;
2. credencial inválida classificada;
3. portal indisponível classificado;
4. integração externa indisponível classificada;
5. found testado com CPF e matrícula;
6. not_found testado com evidência explícita;
7. timeout e seletor alterado retornam retry;
8. sessão sempre fecha e libera lease;
9. teste com duas credenciais não duplica item;
10. smoke de ao menos dez registros revisado manualmente;
11. adapter registrado em workers/registry.py;
12. convênio passa de testing para ready somente após o checklist.

SAFE e Grid permanecem indisponíveis até a implementação transacional cumprir
todos os itens.
