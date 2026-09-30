# Validação operacional após a reorganização

Implementados: Telegram removido; executor genérico com reserva por login único,
heartbeat independente e pausa graciosa; seleção de contas; agendas persistentes;
API paginada; artefatos de exportação e webhooks.

## Critérios de liberação

- Testes PostgreSQL com duas contas e 1.000 itens, falha/reinício e pausa.
- Migration aditiva sobre cópia do esquema antigo; backup antes da produção.
- Smoke HTTP das telas, autorização e API.
- Teste real de login antes de reativar contas bloqueadas; login não confirmado
  exige correção da credencial ou análise do portal, não repetição indefinida.
- Homologar cada portal com retornos conhecidos antes de liberar lote inteiro.
- Grid e EasyConsig ainda não têm adapter transacional liberado.

Os testes sintéticos não demonstram que a senha atual de um portal está válida.
Não iniciar automaticamente bases pausadas durante a implantação.
