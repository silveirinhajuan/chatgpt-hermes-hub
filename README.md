# Hermes ↔ ChatGPT Hub

Hub de conhecimento compartilhado entre o **Hermes Agent** e o **ChatGPT**. Os dois lados leem e escrevem no mesmo banco SQLite/FTS5; nenhum usa o outro como provedor de modelo. O hub só armazena, indexa e busca texto.

```
                 ┌──────────────────────────────────┐
                 │  Hub (SQLite FTS5 + Markdown)     │
                 │  search · fetch · remember · recent│
                 └───────▲──────────────────▲───────┘
                         │                  │
        MCP via túnel    │                  │  filesystem/CLI
        (HTTPS)          │                  │  (indexação)
                 ┌───────┴───────┐   ┌──────┴─────────────┐
                 │   ChatGPT     │   │   Hermes Agent     │
                 │  (conector)   │   │   (local)          │
                 └───────────────┘   └────────────────────┘
```

## O que já está pronto

- `data/hub.db` com índice FTS5.
- Indexador do Hermes: mensagens do `state.db`, `MEMORY.md`, `USER.md`, `SOUL.md` e skills.
- Indexador do export do ChatGPT (`conversations.json` ou ZIP).
- Servidor MCP com as ferramentas `search`, `fetch`, `remember`, `recent`, `stats` e `ask_hermes` (Q&A sem ferramentas).
- Hermes já configurado com o servidor MCP `hermes-hub` (5/5 ferramentas).
- Scripts de indexação, servidor e túnel.

Estado atual do índice (pode variar):

```json
{
  "hermes_message": "≈43 mil",
  "hermes_memory": 196,
  "hermes_skill": 207
}
```

## Comandos rápidos

```bash
cd ~/chatgpt-hermes-hub

# Criar/atualizar o schema
./.venv/bin/python -m hermes_hub init

# Indexar o Hermes (padrão: últimos 365 dias; incremental depois)
./scripts/index-all.sh --days 365

# Indexar todo o histórico do Hermes
./scripts/index-all.sh --all

# Indexar o export do ChatGPT (ZIP ou conversations.json)
./scripts/index-chatgpt.sh ~/Downloads/chatgpt-export.zip

# Testar busca
./.venv/bin/python -m hermes_hub search "cronograma ITA" --limit 5

# Estatísticas
./.venv/bin/python -m hermes_hub stats

# Perguntar ao Hermes (sem ferramentas)
./.venv/bin/python -m hermes_hub ask "Resuma o que você sabe sobre o cronograma ITA" --json
```

O indexador do Hermes é incremental: usa os watermarks `hermes_last_ts` e `hermes_last_message_id` e reprocessa uma janela de 2 dias/100 IDs para pegar mensagens novas. Use `--reset` para apagar e reindexar uma fonte.

## Conectar o Hermes

Já foi feito com:

```bash
hermes mcp add hermes-hub --command /home/juan/chatgpt-hermes-hub/scripts/run-server.sh
```

Em uma sessão aberta, rode `/reload-mcp`; em uma nova sessão, as ferramentas já aparecem. Para conferir:

```bash
hermes mcp list
```

## Conectar o ChatGPT (Secure MCP Tunnel)

O ChatGPT não inicia processos locais e não aceita `stdio`. O caminho oficial é o **OpenAI Secure MCP Tunnel**: um cliente roda na sua máquina, faz conexão de saída e o ChatGPT acessa o servidor local sem expor porta pública.

### Pré-requisitos

- Plano pago do ChatGPT com **Developer Mode** (Plus/Pro/Team/Enterprise/Edu, conforme rollout).
- `tunnel-client` instalado — já deixei em `~/.local/bin/tunnel-client` (v0.0.20). Se precisar reinstalar: https://github.com/openai/tunnel-client/releases
- Uma chave de API com permissões **Tunnels Read + Use**.
- Um túnel criado no painel da OpenAI Platform (guarde o `tunnel_...`).

### 1. Criar o perfil do túnel

```bash
export CONTROL_PLANE_API_KEY="sk-..."

cd ~/chatgpt-hermes-hub
./scripts/setup-tunnel.sh tunnel_XXXXXXXX
```

O script roda:

```bash
tunnel-client init \
  --profile hermes-hub \
  --tunnel-id tunnel_XXXXXXXX \
  --mcp-command "/home/juan/chatgpt-hermes-hub/scripts/run-server.sh"

tunnel-client doctor --profile hermes-hub --explain
```

### 2. Subir o túnel

```bash
tunnel-client run --profile hermes-hub
```

Deixe rodando. Se quiser modo somente leitura:

```bash
HERMES_HUB_READONLY=1 tunnel-client run --profile hermes-hub
```

### 3. Criar o conector no ChatGPT

No ChatGPT web:

1. `Settings → Security and login → Developer mode` (o menu muda de nome conforme o rollout).
2. `Plugins → Add → Create MCP App`.
3. `Connection: Tunnel` e selecione o túnel `hermes-hub` (ou cole o `tunnel_...`).
4. `Authentication: None`.
5. Crie o app e, em uma conversa nova, habilite as ferramentas do app.

Teste com:

> “Busque no hub o que eu tenho sobre o cronograma ITA.”

### Alternativa sem túnel oficial

Se o seu plano/workspace não tiver Secure MCP Tunnel, use um bridge `stdio → Streamable HTTP` (`supergateway` ou `mcp-proxy`) atrás de um Cloudflare Tunnel/ngrok com autenticação. A URL do conector precisa terminar no endpoint HTTP do bridge. O túnel oficial é preferível por não expor porta e por lidar com o `stdio` diretamente.

## Indexar o histórico do ChatGPT

1. No ChatGPT: `Settings → Data controls → Export data`.
2. Baixe o ZIP do e-mail (o link expira em 24h).
3. Rode:

```bash
./scripts/index-chatgpt.sh ~/Downloads/chatgpt-export.zip
```

O importador usa o `current_node` para pegar o branch ativo da conversa e ignora branches antigos. Conversas viram um documento com o título e a transcrição `Você`/`ChatGPT`.

**Limitação importante:** o ChatGPT não expõe memórias internas nem conversas por API. O export é o único caminho oficial para o histórico. Para o conhecimento novo, o caminho é o ChatGPT chamar `remember` no hub durante a conversa.

## Como é a troca bidirecional

| Fluxo | Como funciona | Tempo real? |
|---|---|---|
| ChatGPT lê o Hermes | `search`/`fetch` no hub indexado | Sim, após indexar |
| ChatGPT grava para o Hermes | ferramenta `remember` grava `chatgpt_note` | Sim |
| Hermes lê o ChatGPT | `search`/`fetch` no hub | Sim, após importar o export |
| Hermes grava para o ChatGPT | escreve no hub; ChatGPT consulta | Sim |
| Memória interna do ChatGPT | não sincroniza sozinha; sem API | Não |

O hub não injeta nada automaticamente no `MEMORY.md` do Hermes nem na memória do ChatGPT. Os dois precisam consultar o hub. Para o ChatGPT, vale colocar uma instrução no Project/app:

> “Antes de responder sobre o Juan, consulte o app `hermes-hub` com `search`. Quando eu pedir para lembrar de algo, use `remember`.”

Há um bloco pronto para colar em [`docs/chatgpt-project-instructions.md`](docs/chatgpt-project-instructions.md).

Para o Hermes, as ferramentas MCP já estão disponíveis; peça “busque no hub…” ou use o CLI.

## Ferramentas MCP

| Ferramenta | Para que serve |
|---|---|
| `search(query, limit, source)` | Busca full-text com snippet e ranking BM25 |
| `fetch(doc_id` ou `source+source_id, max_chars)` | Lê o documento completo |
| `remember(text, title, tags)` | Grava uma nota compartilhada (dedup por hash) |
| `recent(limit, days, source)` | Lista os documentos mais recentes |
| `stats()` | Contagem por fonte, tamanho do banco e watermarks |
| `ask_hermes(question, max_chars)` | Pergunta respondida pelo próprio Hermes (modo sem ferramentas) |

Fontes: `hermes_message`, `hermes_memory`, `hermes_skill`, `chatgpt_conversation`, `chatgpt_note`.

## Atualização periódica

Exemplo de cron diário (indexa as mensagens novas do Hermes):

```cron
17 4 * * * cd /home/juan/chatgpt-hermes-hub && ./scripts/index-all.sh --days 3 >> data/index.log 2>&1
```

O export do ChatGPT é manual (a OpenAI não tem API de export contínuo). Reimporte o ZIP quando quiser atualizar o histórico.

## Segurança e privacidade

- O hub contém dados pessoais: `MEMORY.md` (75 KB) e `USER.md` (11 KB), além de mensagens e skills.
- Ao conectar o ChatGPT, as consultas e os trechos retornados vão para a OpenAI. Se não quiser expor memórias, indexe só mensagens:

  ```bash
  ./scripts/index-all.sh --days 365 --no-memories
  ```

- Para remover memórias já indexadas sem apagar o resto:

  ```bash
  ./.venv/bin/python -m hermes_hub drop-source hermes_memory
  ```

- Comece em modo somente leitura (`HERMES_HUB_READONLY=1`) e só habilite `remember` quando quiser escrita.
- O servidor MCP não expõe terminal, arquivos, browser nem shell. Ele só lê/escreve o `hub.db`.
- `ask_hermes` invoca o modelo do Hermes em modo sem ferramentas. As perguntas podem chegar ao provedor de modelo configurado no Hermes e a resposta passa pela OpenAI ao ser lida no ChatGPT. Redação básica de segredos é aplicada na resposta.
- Prefira o Secure MCP Tunnel a abrir porta pública. Se usar Cloudflare/ngrok, coloque autenticação.
- O `remember` registra a nota com `source=chatgpt_note`; o histórico fica auditável no próprio banco.

## Solução de problemas

**`hermes mcp add` cancelou no prompt**
Rode com `yes |` ou responda `Y`:

```bash
yes | hermes mcp add hermes-hub --command /home/juan/chatgpt-hermes-hub/scripts/run-server.sh
```

**Ferramentas não aparecem no Hermes**
Rode `/reload-mcp` na sessão ou reinicie o Hermes.

**ChatGPT não conecta**
Confira `tunnel-client doctor --profile hermes-hub --explain`, se o túnel está `run`, e se o conector foi criado com `Connection: Tunnel`. O Developer Mode precisa estar ativo no web.

**Busca não acha conversas do ChatGPT**
Confirme que o export foi indexado (`stats` deve mostrar `chatgpt_conversation`) e que o ZIP contém `conversations.json`.

**Banco cresceu muito**
O FTS5 duplica parte do texto. Para reduzir, reindexe só uma janela (`--days 180`) ou remova fontes com `--reset` e `--no-memories`.

## Estrutura

```
~/chatgpt-hermes-hub/
├── hermes_hub/
│   ├── cli.py              # comandos
│   ├── db.py               # schema, FTS5, busca, remember
│   ├── index_hermes.py     # state.db + memórias + skills
│   ├── index_chatgpt.py    # export conversations.json
│   └── server.py           # servidor MCP stdio
├── scripts/
│   ├── run-server.sh
│   ├── index-all.sh
│   ├── index-chatgpt.sh
│   └── setup-tunnel.sh
├── tests/
└── data/hub.db
```
