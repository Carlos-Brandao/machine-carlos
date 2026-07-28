# Machine — Bots de Consulta de Margem Consignável

Dispatcher unificado para automação de consultas de margem em múltiplos sistemas, com suporte a múltiplos convênios por bot.

---

## Requisitos

- Python 3.11+
- PostgreSQL 15+
- Google Chrome ou Microsoft Edge instalado

---

## Instalação

```bash
# 1. Clone o repositório e entre na pasta
cd machine

# 2. Crie e ative o ambiente virtual
python -m venv env
env\Scripts\activate        # Windows
# source env/bin/activate   # Linux/Mac

# 3. Instale as dependências
pip install -r requirements.txt

# 4. Instale os navegadores do Playwright
playwright install
```

Crie o banco e aplique a migration antes de iniciar os serviços:

```bash
alembic upgrade head
```

---

## Configuração — `.env`

Copie o `.env` de exemplo e preencha com os dados de cada convênio:

```env
# Lista de convênios disponíveis para o bot (separados por vírgula)
RF1_CONVENIOS=boa-vista
RF1_BOA_VISTA_URL_LOGIN=https://...
RF1_BOA_VISTA_URL_CONSULTA=https://...
RF1_BOA_VISTA_USUARIO=...
RF1_BOA_VISTA_SENHA=...
TWOCAPTCHA_API_KEY=...
HEADLESS=true

# Dados do convênio — padrão: {BOT}_{CONVENIO}_{CHAVE}
RF1_BOAVISTA_URL_LOGIN=https://...
RF1_BOAVISTA_URL_CONSULTA=https://...
```

> Cada bot lê automaticamente todas as variáveis com o prefixo `{BOT}_{CONVENIO}_` e as injeta como configuração. Slugs com hífen usam sublinhado na variável: `boa-vista` vira `RF1_BOA_VISTA_*`. Não é necessário alterar nenhum código ao adicionar novos convênios.

### Controle pelo Telegram

O processo `run_telegram_bot.py` oferece a interface de comandos e **não executa
robôs diretamente**. Ele envia a seleção de prefeituras ao backend, que deve
controlar a fila e o limite de concorrência.

Acrescente ao `.env`:

```env
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ALLOWED_USER_IDS=123456789
TELEGRAM_NOTIFICATION_CHAT_ID=-1001234567890
BACKEND_API_URL=https://seu-backend.exemplo
BACKEND_API_TOKEN=...
```

O backend precisa expor estes endpoints autenticados:

```text
POST /api/jobs/batch
GET  /api/jobs/status
GET  /api/jobs/queue
```

O primeiro recebe `prefeituras` (lista de slugs) e os identificadores Telegram
de quem solicitou a execução. Os demais retornam JSON que é apresentado pelo
bot.

Enquanto `BACKEND_API_URL` estiver vazio, o bot pode ser iniciado para testar
`/iniciar` e a seleção múltipla; a confirmação, `/status` e `/fila` informarão
que o backend ainda não está configurado.

Para registrar o menu de comandos e iniciar o processo:

```bash
python run_telegram_bot.py --set-commands
python run_telegram_bot.py
```

O bot aceita `/iniciar`, `/status`, `/fila`, `/limparfila`, `/pararrobos`, `/cancelar` e `/ajuda`.
`/iniciar` apresenta apenas os convênios ativos no cadastro central: Boa Vista,
Fortaleza, Maranguape, Teresina, GOV AM, Paulista, Paulista Previdência e
Mossoró.

O projeto usa **um único bot do Telegram**: o mesmo `TELEGRAM_BOT_TOKEN` opera
o controlador e envia as notificações dos robôs. Configure
`TELEGRAM_NOTIFICATION_CHAT_ID` para definir o destino dessas notificações.
`TELEGRAM_CHAT_ID` continua aceito temporariamente apenas para compatibilidade.

### Backend da fila

O painel e a API usam o mesmo processo FastAPI e o mesmo PostgreSQL:

```bash
python run_backend_api.py
```

Por padrão o serviço escuta somente em `127.0.0.1:8000`; publique-o atrás de um
proxy HTTPS. As rotas operacionais exigem `Authorization: Bearer <token>`.
O pool inicial de Boa Vista é iniciado separadamente:

```bash
python run_worker.py rf1 --workers 3
```

Cada worker reserva uma credencial diferente e pequenos lotes com
`FOR UPDATE SKIP LOCKED`. Três logins ativos permitem três sessões sobre a mesma
base, sem consultar o mesmo registro duas vezes. `run_scheduler.py` permanece
somente como entrada compatível e inicia esse mesmo pool RF1.

### Painel administrativo

O painel mínimo oferece:

- login administrativo com papéis `admin`, `operator` e `viewer`;
- criação e revogação de tokens com escopos;
- cofre cifrado para Telegram, 2Captcha e tokens internos;
- cadastro de múltiplas credenciais por convênio;
- importação cifrada de `.xlsx`/`.csv`, criação de jobs e exportação;
- dashboard de jobs, itens, falhas e progresso.

Configure no `.env`:

```env
DATABASE_URL=postgresql://machine:SENHA_FORTE@127.0.0.1:5432/machine
ADMIN_SESSION_SECRET=gere-um-valor-aleatorio-com-48-ou-mais-caracteres
APP_MASTER_KEY=gere-uma-chave-base64-urlsafe-de-32-bytes
ADMIN_ALLOWED_HOSTS=admin.seu-dominio.com,localhost,127.0.0.1
ADMIN_COOKIE_SECURE=true
BOOTSTRAP_ADMIN_EMAIL=admin@seu-dominio.com
BOOTSTRAP_ADMIN_PASSWORD=uma-senha-inicial-com-12-ou-mais-caracteres
```

Gere as duas chaves localmente:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
python -c "import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"
```

Depois do primeiro login, remova as duas variáveis `BOOTSTRAP_ADMIN_*`. Preserve
e faça backup seguro de `APP_MASTER_KEY`: trocá-la sem uma rotina de rotação
torna inacessíveis os segredos e dados já cifrados.

No painel, crie dois tokens:

- controlador Telegram: `jobs:read,jobs:write`, salvo como `BACKEND_API_TOKEN`;
- workers: `jobs:read,workers:execute`, salvo como `WORKER_API_TOKEN`.

Os segredos `TELEGRAM_BOT_TOKEN` e `TWOCAPTCHA_API_KEY` também podem ser movidos
para o cofre. O banco é a fonte preferencial; o `.env` permanece apenas como
fallback de transição. Reinicie processos externos após rotacionar um segredo.

### Banco de dados na VPS

Use PostgreSQL em produção e acrescente ao `.env`:

```env
DATABASE_URL=postgresql://machine:SENHA_FORTE@127.0.0.1:5432/machine
```

Alembic cria e versiona as tabelas de plataformas, prefeituras, usuários,
credenciais, bases, registros, jobs, leases, eventos e resultados. O Excel
continua sendo entrada e exportação; PostgreSQL é a base oficial. Bases, CPFs,
linhas originais, resultados e credenciais ficam cifrados com AES-GCM. Tokens
de API são armazenados somente como hash. Não versione a `DATABASE_URL` e faça
backup diário do banco e de `storage/`.

Se a VPS já recebeu o esquema PostgreSQL experimental anterior, não aplique a
migration inicial por cima dele. Use um banco novo ou prepare uma migração de
dados específica depois de conferir as tabelas existentes.

### Administração remota

`deploy.py` e `setup_cron.py --vps` leem as credenciais somente do ambiente.
Configure `MACHINE_SSH_HOST`, `MACHINE_SSH_USER` e, de preferência,
`MACHINE_SSH_KEY_FILE`. Antes do primeiro deploy, registre a chave pública do
servidor em `known_hosts` (`ssh-keyscan -H <host> >> ~/.ssh/known_hosts`).
Senhas, chaves privadas e endereços de produção não devem ser versionados.

### Regras de agendamento dos robôs

O scheduler usa sempre o fuso `America/Fortaleza`, aceita no máximo **3 robôs
simultâneos** e não inicia novos robôs aos sábados ou domingos. Jobs recebidos
fora da janela permanecem em fila até o próximo início permitido.

| Plataforma | Dias permitidos | Início | Encerramento |
|---|---|---:|---:|
| SafeConsig | segunda a sexta | 07:00 | 18:00 |
| FácilConsig | segunda a sexta | 07:00 | 21:00 |
| RF1 | segunda a sexta | 07:00 | 21:00 |
| GRID | segunda a sexta | 07:00 | 21:00 |

Regras de entrada na fila:

- Pedido de segunda a sexta **dentro** da janela: o scheduler pode iniciar o
  job imediatamente, desde que haja uma das três vagas.
- Pedido de segunda a sexta **fora** da janela: o job fica aguardando até a
  próxima janela da sua plataforma.
- Pedido no sábado ou domingo: o job fica aguardando até segunda-feira, às
  07:00 BRT.

### Mapeamento inicial

| Plataforma | Prefeituras já mapeadas |
|---|---|
| RF1 | Boa Vista |
| SafeConsig | Fortaleza, Maranguape |
| FácilConsig | Teresina, GOV AM, Paulista, Paulista Previdência, Mossoró |

GRID segue a regra de 07:00–21:00, mas ainda precisa ter sua prefeitura
associada no cadastro. Consiglog/Itabuna e EasyConsig permanecem desativados
até a integração dos respectivos runners ser validada.

Itabuna é um robô separado (`Consiglog`) e não compartilha o runner RF1 de Boa
Vista. Sua entrada permanece desativada até revalidarmos o portal alterado.

---

## Estrutura de pastas

```
machine/
├── main.py               ← entry point único
├── requirements.txt
├── .env                  ← credenciais e URLs (não versionar)
├── data/                 ← inputs copiados com timestamp
├── temp/                 ← saves parciais (apagados ao concluir)
├── completed/            ← resultados finais com timestamp
├── services/
│   ├── captcha.py        ← serviço de resolução de captcha (2captcha)
│   └── utils.py          ← utilitários compartilhados (ex: aguardar ENTER sem corrupção de stdin)
├── rf1/
│   └── rf1.py
├── facil/
│   └── facil.py
└── grid/
    └── grid.py
```

---

## Como usar

### Sintaxe

```bash
python main.py <bot> [convenio] [--list]
```

| Argumento    | Descrição                                              |
|--------------|--------------------------------------------------------|
| `bot`        | Qual bot executar: `rf1`, `facil`, `safeconsig` ou `grid` |
| `convenio`   | Qual convênio consultar (configurado no `.env`)        |
| `--list`     | Lista os convênios disponíveis para o bot informado    |

### Exemplos

```bash
# Listar convênios disponíveis de um bot
python main.py rf1 --list
python main.py facil --list

# Executar um bot com convênio específico
python main.py rf1 boavista
python main.py facil paulista
python main.py grid roraima
```

> Quando o bot tem apenas um convênio configurado no `.env`, o argumento `convenio` pode ser omitido.

### Fluxo de execução

1. O dispatcher valida o bot e o convênio
2. Abre uma janela de seleção de arquivo (planilha `.xlsx` ou `.csv`)
3. Copia o arquivo para `data/` com timestamp
4. Executa o bot
5. Salva progresso parcial em `temp/` a cada linha consultada
6. Ao concluir, move o resultado final para `completed/` e apaga o temp

**Nomenclatura dos arquivos gerados:**
```
data/rf1_boavista_20260421_143022.xlsx      ← input copiado
temp/rf1_boavista_20260421_143022.xlsx      ← parcial (apagado ao fim)
completed/rf1_boavista_20260421_143022.xlsx ← resultado final
```

---

## Formato do arquivo de entrada

Planilha `.xlsx` ou `.csv`. Colunas esperadas por bot:

| Bot     | Colunas obrigatórias       |
|---------|----------------------------|
| `rf1`   | `CPF`                      |
| `facil` | `cpf`, `matricula`         |
| `grid`  | `cpf`                      |

---

## Adicionando um novo convênio

Nenhuma linha de código precisa ser alterada. Basta editar o `.env`:

### Exemplo — novo convênio no bot `facil`

**Antes:**
```env
FACIL_CONVENIOS=paulista
```

**Depois:**
```env
FACIL_CONVENIOS=paulista,teresina

FACIL_TERESINA_URL=https://www.faciltecnologia.com.br/consigfacil/teresina
FACIL_TERESINA_USUARIO=usuario.teresina
FACIL_TERESINA_SENHA=Senha@123
```

**Uso:**
```bash
python main.py facil teresina
```

### Exemplo — novo convênio no bot `rf1`

```env
RF1_CONVENIOS=boavista,outroconsig

RF1_OUTROCONSIG_URL_LOGIN=https://outroconsig.rf1consig.com.br/.../Logar.aspx
RF1_OUTROCONSIG_URL_CONSULTA=https://outroconsig.rf1consig.com.br/.../CADPessoaListar.aspx
```

```bash
python main.py rf1 outroconsig
```

### Padrão de chaves no `.env` por bot

**`rf1`**
```env
RF1_{CONVENIO}_URL_LOGIN=
RF1_{CONVENIO}_URL_CONSULTA=
RF1_{CONVENIO}_USUARIO=
RF1_{CONVENIO}_SENHA=
```

**`facil`**
```env
FACIL_{CONVENIO}_URL=
FACIL_{CONVENIO}_USUARIO=
FACIL_{CONVENIO}_SENHA=
```

**`grid`**
```env
GRID_{CONVENIO}_URL_LOGIN=
GRID_{CONVENIO}_URL_PERFIL=
GRID_{CONVENIO}_URL_MARGEM=
GRID_{CONVENIO}_USUARIO=
GRID_{CONVENIO}_SENHA=
```

> O nome do convênio no `.env` deve ser em **maiúsculas** na chave e em **minúsculas** no argumento do CLI. Ex: `FACIL_TERESINA_URL` → `python main.py facil teresina`.

---

## Serviço de Captcha (2captcha)

Os bots `rf1`, `facil` e `safeconsig` utilizam o serviço [2captcha](https://2captcha.com) para resolução automática de captchas. Configure a chave no `.env`:

```env
TWOCAPTCHA_API_KEY=sua_chave_aqui
```

A chave é compartilhada entre todos os bots. O login é totalmente automatizado — nenhuma interação manual é necessária.

---

## Interrompendo a execução (Ctrl+C)

Pressione `Ctrl+C` a qualquer momento. O bot:

1. Termina o registro atual normalmente
2. Salva o progresso em `temp/`
3. Mantém o navegador aberto para inspeção
4. Aguarda você pressionar **ENTER** para fechar

Na próxima execução com o mesmo arquivo o bot retoma de onde parou automaticamente.

---

## Erros no terminal

Quando um registro falha, o terminal exibe o tipo do erro e o traceback completo:

```
[42/2310] mat=14796 cpf=228172470
  ERRO [Exception]: ERROR_ZERO_BALANCE
  Traceback (most recent call last):
    ...
```

O registro com erro é salvo na planilha com o campo `erro` preenchido, e o bot continua para o próximo CPF.

---

## Observações

- O bot `grid` usa Chromium por padrão, aceita `GRID_{CONVENIO}_BROWSER_CHANNEL`
  opcional e ainda exige resolução manual do reCAPTCHA
- Os bots `rf1` e `facil` fazem login automatizado via 2captcha
- O arquivo `.env` contém credenciais — **não versione este arquivo**
- Em caso de interrupção, o bot retoma de onde parou usando o arquivo em `temp/`
