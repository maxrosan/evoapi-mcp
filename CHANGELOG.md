# Changelog

Todas as mudanças notáveis neste projeto serão documentadas neste arquivo.

O formato é baseado em [Keep a Changelog](https://keepachangelog.com/pt-BR/1.0.0/),
e este projeto adere ao [Semantic Versioning](https://semver.org/lang/pt-BR/).

---

## [Não publicado]

### 🖼️ Imagem saindo daqui sem base64 no chat

Faltava o caminho de volta: `download_media`, `view_media` e `archive_to_drive` já evitavam
o base64 na **entrada**, mas uma imagem criada pelo modelo ainda saía por `send_image_base64`,
custando o arquivo inteiro dentro da conversa. Duas rotas novas fecham isso.

### ✨ Adicionado

- **`send_render(number, svg, caption?, file_name?, width?, height?, background?)`**: o modelo
  escreve o **SVG** e o servidor rasteriza e envia. O desenho atravessa a conversa como texto
  (um gráfico de barras dá ~1.200 caracteres, contra ~16.800 do base64 do PNG) e a diferença
  cresce conforme a imagem ganha detalhe. O PNG fica em `<EVOLUTION_MEDIA_DIR>/render/`, para
  reenviar sem redesenhar. Rasterização por `cairosvg`, no extra novo `[svg]`
- **`send_url(number, url, caption?, media_type?, file_name?)`**: envia arquivo que já está na
  web a partir do link; o download é do servidor e pela conversa passa só a URL. Converte
  sozinho o link de compartilhamento do Google Drive e do Dropbox no link do arquivo, deduz o
  tipo pelo `Content-Type` e explica o erro (link privado, 404, arquivo grande demais) em vez
  de deixar a Evolution falhar de forma opaca. Teto de 16 MB, arquivo em `<EVOLUTION_MEDIA_DIR>/web/`
- **`rendering.render_svg`** e o módulo **`weblink.py`** (normalização de link, download com teto
  e recusa de endereço interno)
- Extra `[svg]` no `pyproject.toml` e `libcairo2` na imagem Docker

### 🔒 Segurança

- `send_url` só busca endereço `http`/`https` público: cada salto de redirecionamento é resolvido
  e conferido, e IP privado, loopback, link-local ou reservado é recusado. Sem isso um link vindo
  numa mensagem do WhatsApp faria o servidor buscar coisas na rede interna e devolvê-las ao remetente
- `render_svg` rasteriza com `unsafe=False`: o SVG não lê arquivo do servidor nem entidade externa

### 📝 Alterado

- `send_image_base64` e `send_image` passam a apontar a alternativa barata na própria descrição,
  que é o que o modelo lê na hora de escolher a tool

### 🧹 Manutenção do servidor

- **Faxina do `media_dir`**: nada apagava nada, e num container o disco cheio derruba
  download, envio, transcrição e arquivamento de uma vez, com erros que não falam em disco.
  Agora a pasta tem prazo (`EVOLUTION_MEDIA_TTL_DAYS`, padrão 30 dias), varrido no máximo
  uma vez por dia por quem grava, com a tool `cleanup_media(days?, dry_run?)` para adiantar.
  `.transcripts` nunca é apagado, e o uso de disco aparece em `get_instance_info` e em
  `GET /instance/status`. Novo módulo `storage.py`
- **`send_drive_file(number, file_ref? | folder+name, ...)`**: fecha o ciclo do
  `archive_to_drive`, que era mão única — arquivo guardado no Drive volta ao WhatsApp sem
  depender da cópia local. `DriveClient` ganhou `download_file`, `get_metadata`,
  `find_in_folder` e `file_id_from` (aceita id ou link). Continua no escopo `drive.file`:
  alcança o que este servidor criou, sem escopo restrito nem arquivo público

### 🔧 Infraestrutura

- **CI no GitHub Actions**: pytest em Python 3.10, 3.11 e 3.12 com os extras instalados
  (incluindo `libcairo2`, para que os testes de rasterização rodem em vez de serem pulados),
  o servidor MCP subindo e listando as tools, e um job que constrói a imagem Docker e
  confere que ela rasteriza — dependência de sistema faltando só aparece ali
- **API REST alinhada com as tools**: `POST /messages/render`, `/messages/url`,
  `/messages/drive`, `/media/archive`, `/media/view` e `/media/cleanup`. As duas superfícies
  vinham divergindo em silêncio; um teste agora falha se um envio existir só no MCP
- Primeiros testes do `http_server.py`, que não tinha nenhum

### 🔒 Segurança

- O token da instância Evolution, commitado em quatro arquivos desde os primeiros commits,
  foi trocado por placeholder. **Ele continua no histórico do git e válido até ser rotacionado
  no painel da Evolution** — está registrado no `TODO.md`

### 🧭 Interação: qual tool usar, e o WhatsApp deixando de ficar mudo

- **`instructions` no servidor MCP.** São cinco caminhos para mandar uma imagem, e a
  regra de escolha estava espalhada pelas descrições ("para X use Y") — regra repetida
  em cinco lugares funciona enquanto o modelo lê as cinco, e falha calada quando ele
  escolhe a primeira que serve. Agora ela é dita uma vez, no nível do servidor, e chega
  antes da escolha
- **Tools de base64 escondidas por padrão** (`send_image_base64`, `send_document_base64`,
  `get_media_base64`): tool visível é tool escolhida, e essas são justamente o caminho
  caro que o resto do projeto existe para evitar. `EVOLUTION_BASE64_TOOLS=1` traz de volta.
  A superfície cai de 28 para 26 tools (25 sem o `react_to_message`, que entrou junto)
- **`react_to_message(number, message_id, emoji, from_me?)`**: responder com um sinal em
  vez de mais uma mensagem. String vazia remove a reação
- **Sinal de vida do bot "IA:"**: ao ser acionado ele reage com 👀 e liga o "digitando…";
  ao responder, troca por ✅. Antes disso a conversa ficava parada por dezenas de segundos,
  sem distinguir "pensando" de "morreu". Falha ao sinalizar nunca impede a resposta.
  `EVOLUTION_BOT_FEEDBACK=0` desliga

### 🐛 Corrigido

- **Reação nunca mais aciona o bot.** O texto de uma reação é o próprio emoji e, na
  conversa pessoal, toda mensagem do dono é instrução: um 👍 de Max virava a instrução
  "👍", e o 👀 do próprio bot teria voltado como novo acionamento. Duas travas: o tipo
  reação nunca vira instrução (`webhook.summarize_event`) e o id da reação entra na lista
  de enviados, como o de qualquer envio

### 📚 Documentação

- `ROADMAP.md`, `KNOWN_ISSUES.md`, `NEXT_STEPS.md`, `SUMMARY.md` e `FIXES.md` foram removidos:
  descreviam o projeto de outubro de 2025, com a biblioteca `evolutionapi` que já saiu, tools
  que não existem mais e pendências já resolvidas. `TODO.md` foi reescrito com o que é verdade
  hoje; `README.md` e `CHANGELOG.md` seguem como os documentos vivos

---

## [1.2.0] - 2026-09-11

### 🪙 Economia de tokens com Claude

Esta release reduz drasticamente o volume de texto que cada tool devolve ao LLM e elimina o tráfego de base64 pelo chat.

### ✨ Adicionado

- **`formatters.py`**: formatadores compactos para mensagens, chats, contatos e resultados de envio
  (campos nulos omitidos, JSON sem espaços, acentos sem escape, ~10x menor que o objeto bruto)
- **`download_media(message_id, save_dir?, filename?, extract_text?, max_chars?)`**: baixa o anexo
  para `EVOLUTION_MEDIA_DIR` e devolve só `{path, file, mime, size, type}`; com `extract_text=True`
  extrai o texto de PDF (via `pypdf`, extra opcional `[pdf]`) ou txt
- **`send_file(number, file_path, caption?, media_type?, file_name?)`**: envia arquivo local;
  o servidor gera o base64, o LLM nunca vê o conteúdo
- **`send_document_base64` / `send_image_base64` / `get_media_base64`**: paridade com o conector
  remoto, marcadas como caras (prefira `send_file` / `download_media`)
- **Busca textual local** em `find_messages` e `get_chat_messages(query=...)`: varre até 500
  mensagens no servidor e devolve só as que casam
- **`get_contacts(search=...)`**: filtra contatos por nome/número sem listar todos
- **`clear_cache`** exposta como tool
- Parâmetros `page`, `max_text` e `full` nas tools de leitura
- Configuração: `EVOLUTION_MEDIA_DIR`, `EVOLUTION_DEFAULT_LIMIT` (20), `EVOLUTION_MAX_TEXT_CHARS` (500)
- HTTP: `GET /messages` (busca), `POST /messages/file`, `POST /messages/base64`, `POST /media/download`,
  `full`/`search`/`query` nos endpoints de leitura
- Suite pytest em `tests/` (formatadores e cliente com HTTP simulado)
- **Endereçamento `@lid`**: `resolve_chat_jid` encontra a conversa real pela lista de chats
  (`remoteJidAlt`), mensagens compactas preferem o telefone (`participantAlt`/`remoteJidAlt`)
  ao id opaco, envio aceita jid (`@lid`/`@g.us`) sem mutilá-lo em número
- **`mcp_http.py`** (`evoapi-mcp-http`): MCP via Streamable HTTP com Bearer token
  (`MCP_AUTH_TOKEN`, `PORT`), substituindo o wrapper externo usado no Easypanel
- Console scripts `evoapi-mcp` (stdio) e `evoapi-mcp-http`
- **Arquivamento no Google Drive pelo servidor**: tool `archive_to_drive` leva o anexo
  do WhatsApp ao Drive sem passar o arquivo pela conversa, criando as pastas que
  faltarem. O conector de Drive do Claude só aceita conteúdo embutido, então arquivar
  um boleto custava dezenas de milhares de tokens e um PDF grande simplesmente não
  cabia. Escopo `drive.file` de propósito: `drive` é restrito e obrigaria verificação
  do Google, e em modo de testes o refresh token expiraria a cada 7 dias
- `scripts/google_oauth_setup.py` obtém o refresh token e grava em arquivo local,
  sem imprimir o segredo na tela
- Configuração: `EVOLUTION_DRIVE_CLIENT_ID`, `_CLIENT_SECRET`, `_REFRESH_TOKEN`,
  `_ROOT_ID`, `_ROOT`
- **`view_media`: documento devolvido como imagem**, para o modelo ler comprovante
  fotografado e PDF escaneado com a própria visão, sem OCR. É o que faltava para o
  arquivamento inteiro caber num chat comum, sem sessão com terminal. Renderização por
  pypdfium2 (BSD/Apache, sem dependência de sistema) no extra opcional `[image]`;
  imagens limitadas a 1568px no lado maior, cerca de 1.500 tokens por página
- **PDF protegido por senha destravado no servidor**: `download_media` e
  `archive_to_drive` aceitam `password` e gravam o arquivo já sem senha, usando pypdf.
  Antes isso exigia `qpdf` na máquina do usuário, o que prendia o fluxo a uma sessão
  com terminal; agora um chat comum dá conta. Sem a senha, um PDF cifrado volta com
  `text_error` dizendo exatamente o que fazer
- **Transcrição de áudios**: tool `transcribe_audio(message_id | file_path)` converte voice
  notes em texto no servidor, com cache por mensagem em `<media_dir>/.transcripts/`.
  Backends: API compatível com a OpenAI (OpenAI, Groq, whisper.cpp) ou `faster-whisper`
  local (extra opcional `[audio]`). `download_media(extract_text=True)` transcreve
  automaticamente anexos de áudio e vídeo; `get_instance_info()` reporta o backend ativo
- Mensagens de áudio compactas trazem `voice: true` para voice notes e `seconds`
- HTTP: `POST /media/transcribe`
- Configuração: `EVOLUTION_TRANSCRIBE_BACKEND`, `_API_URL`, `_API_KEY`, `_MODEL`,
  `_LANGUAGE`, `_TIMEOUT`, `_MAX_MB`

### 🔄 Alterado

- Tools devolvem uma string JSON compacta em vez de dict (evita `indent=2` e duplicação em `structuredContent`)
- Docstrings das tools encurtadas (menos tokens no esquema enviado a cada requisição)
- `findMessages` agora envia `where.key.remoteJid` + `page`/`offset` (v2) além de `limit` (v1);
  antes `chatId`/`limit` eram ignorados pela API v2
- Limites padrão: 50 → 20 mensagens; `list_chats`/`get_contacts` deixam de devolver tudo
- Erros HTTP têm o corpo resumido (HTML e stack traces removidos, 400 chars)
- `get_contact_name` cai para o mapa de contatos em cache quando o filtro por id não retorna nada
- `mcp` fixado em `<2` (a 2.x renomeou `FastMCP`)

### 🐛 Corrigido

- HTTP: `/chats` chamava `find_chats(limit=)` inexistente; `/instance/status` e `/presence`
  chamavam métodos/argumentos inexistentes

---

## [Não lançado]

### 👀 Observação de eventos (primeiro passo do bot "IA:")

- **Receptor de webhook da Evolution API** em `/webhook/<segredo>`, ativado por
  `EVOLUTION_WEBHOOK_SECRET`. Nesta fase ele apenas registra o que chega: não responde,
  não chama modelo nenhum e não grava em disco. Existe para responder à pergunta que
  pode derrubar o plano do bot, que é se a Evolution avisa quando a mensagem é digitada
  no celular do dono da instância
- Leitura do que foi observado em `/webhook/<segredo>/log`, com filtros `minhas=1` e
  `acionamentos=1`
- O acionamento exige **duas** condições, `fromMe` e o prefixo `IA:`. Um terceiro
  escrevendo "IA:" num grupo não aciona nada. A primeira versão errava isso e os
  testes pegaram
- Privacidade: no máximo 80 caracteres de prévia por mensagem, nunca conteúdo de mídia,
  histórico só em memória

### 🤖 O bot "IA:" (desligado por padrão)

- **`bot.py`**: quando Max escreve "IA:" numa conversa, o servidor responde ali mesmo.
  Ligado por `EVOLUTION_BOT_ENABLED=1`; **sem essa variável nada responde a ninguém**,
  porque passar a falar com terceiros não pode acontecer por acidente num deploy
- **O raio de alcance é estrutural, não uma instrução**: o modelo não recebe nenhuma
  ferramenta de envio. A resposta final é o único canal de saída e vai para a conversa
  que acionou. Ele não tem como escrever para outra pessoa nem que queira
- Ferramentas de leitura presas à conversa: transcrever áudio, ler documento e buscar
  no histórico dela
- Travas: só `fromMe` mais o prefixo aciona; ids já vistos e os enviados pelo próprio
  bot são ignorados, o que fecha o laço; a conversa pessoal fica de fora; teto de gasto
  diário em dólares
- O histórico das outras pessoas entra como dado, e o prompt de sistema diz isso, porque
  conversa de grupo é território hostil para injeção
- Modelo padrão `claude-opus-5` com fallback de recusa, ajustável por
  `EVOLUTION_BOT_MODEL`. Extra opcional `[bot]`
- Processamento fora do ciclo da requisição: a Evolution recebe o 200 na hora e a
  resposta sai numa thread, senão ela reenviaria o evento

### 🔁 Fila de pendências (para responder por sessão em laço)

- **`pending_triggers`** e **`mark_triggers_handled`**: uma sessão do Claude rodando em
  laço pergunta o que chegou e confirma o que tratou, em vez de varrer conversas. Cada
  pendência vem com id, conversa e a instrução já separada do prefixo
- Serve a quem prefere não pôr chave da Anthropic no servidor: o cérebro passa a ser a
  sessão. Em troca, a resposta deixa de ser imediata e depende da máquina ligada, e a
  trava de raio de alcance vira instrução em vez de estrutura, porque a sessão tem todas
  as ferramentas, inclusive as de envio

### 🙋 Conversa pessoal: ali tudo é instrução

- **Na conversa do dono com ele mesmo, toda mensagem dele é instrução, sem prefixo.**
  É como ele já usava antes desta funcionalidade existir, e a regra do "IA:" tinha
  quebrado isso sem que ninguém percebesse. Nas demais conversas e grupos, o prefixo
  continua obrigatório
- `EVOLUTION_OWNER_NUMBER` identifica essa conversa. O jid dela é opaco (`...@lid`), então
  o telefone é comparado também contra `remoteJidAlt`, pelos últimos 8 dígitos, porque o
  WhatsApp escreve o mesmo número ora com o nono dígito, ora sem. Sem a variável, nada é
  tratado como conversa pessoal e a regra antiga vale em tudo
- A instrução passa a ser guardada inteira, separada da prévia, que segue curta por
  privacidade. Antes uma instrução longa chegava cortada em 80 caracteres
- **Todo envio nosso é marcado como já tratado.** Na conversa pessoal a instância não
  distingue o que o dono digitou do que o assistente respondeu; sem isso a própria
  resposta voltaria como pedido e o laço não pararia
- Corrigido: a detecção de conversa pessoal no bot usava `instance_name`, que é um
  apelido ("Max 1") e não um telefone. Nunca tinha reconhecido nada

### ⏰ Mensagens agendadas

- Novas tools `schedule_message(number, text, when, voice)`, `list_scheduled` e
  `cancel_scheduled(id)`. O pedido fica na tabela `scheduled_messages` do mesmo
  Postgres, e uma thread do servidor (`scheduler.py`) envia o que venceu a cada 30 s,
  em texto ou em nota de voz. Sobrevive a restart e deploy
- `when` aceita "AAAA-MM-DD HH:MM" no fuso de Max (`EVOLUTION_TIMEZONE`, padrão
  America/Fortaleza), ISO com deslocamento, ou só "HH:MM" (hoje, ou amanhã se já
  passou). Passado é recusado
- A mensagem enviada é marcada como tratada: na conversa pessoal ela voltaria como
  instrução. Falha de envio fica registrada com o erro e não é repetida
- Executor: cada acionamento passa a começar com a data e hora atuais no fuso de Max,
  porque a sessão nasce sem relógio e "amanhã às 9h" precisava de referência
- Motivação: Max perguntou se podia pedir pelo WhatsApp para agendar uma mensagem. O
  executor não tem memória entre acionamentos; o servidor e o banco têm

### 🎙️ Comando de voz: "Computador, ..."

- Áudio de Max passa a ser transcrito em segundo plano assim que chega pelo webhook.
  Nas conversas com terceiros, se a transcrição começar com a palavra de ativação
  (`EVOLUTION_WAKE_WORD`, padrão "computador"), vira instrução na fila, já em texto,
  com `voz: true`. É o "IA:" falado
- Na conversa pessoal todo áudio dele já era para ser instrução, mas não era: sem texto,
  o filtro deixava passar. Agora entra, com a palavra de ativação tirada se vier
- "Computadores estão caros" não aciona: a palavra tem de estar inteira e no começo.
  Áudio de terceiro nunca é transcrito. Sem backend de transcrição o recurso fica
  desligado e diz isso na subida
- Executor: pendência com `voz: true` pode ter nome próprio errado na transcrição
  ("praquê ele" por "pra Keilla"); ele usa o chat e o contexto para desfazer

### 🗣️ Texto para voz: `send_voice`

- Nova tool `send_voice(number, text, voice=None)`: gera a fala e envia como **nota de
  voz** (a de forma de onda), não como arquivo. É o caminho inverso da transcrição
- Dois backends em `speech.py`: `edge` (vozes neurais do Microsoft Edge via `edge-tts`,
  sem chave, com vozes brasileiras: Francisca, Antonio, Thalita) e `api` (qualquer
  endpoint compatível com `/v1/audio/speech` da OpenAI, com `EVOLUTION_TTS_API_KEY`).
  `auto` prefere a API quando há chave, senão o Edge. Padrão: `pt-BR-FranciscaNeural`
- O MP3 fica em `media_dir/voz`, e o mesmo texto na mesma voz reaproveita o arquivo;
  a Evolution converte para o formato de nota de voz no envio
- `edge-tts` entra como dependência básica, não como extra: o Dockerfile do Easypanel
  tem a lista de extras fixa e um extra novo nunca chegaria à imagem. `get_instance_info`
  expõe `speech`
- Executor: pedidos "em áudio" passam a usar `send_voice`
- Motivação: "explique para Keilla em áudio" saiu em texto, porque não existia o
  caminho texto → voz. O servidor só transcrevia

### 💬 Resposta com citação vale como instrução

- Quando o assistente pergunta algo numa conversa com terceiro e Max responde
  **citando** a pergunta, a resposta entra na fila sem precisar de "IA:". Só vale se
  a mensagem citada for conhecida (enviada ou tratada pelo assistente); citar uma
  mensagem qualquer continua não acionando nada, e terceiro citando não aciona nunca
- `pending_triggers` ganha `respondendo_a` com o texto citado, para o executor saber a
  que pergunta Max respondeu, mesmo sem lembrar da execução anterior
- Executor: antes de agir num chat de terceiro, lê as últimas mensagens dele para ver
  o próprio histórico; nunca marca como tratada sem responder no mesmo chat; ganha
  `WebSearch`/`WebFetch` para pedidos como "ache um vídeo sobre X"
- Motivação: um pedido "envie pra Keilla um vídeo" virou pergunta de confirmação no
  chat da Keilla, Max respondeu lá com "IA:", e a execução seguinte, sem memória da
  anterior, perguntou tudo de novo, ainda por cima na conversa pessoal dele

### 🖥️ Executor local (`runner/`)

- **Substitui o laço `/loop 30s` no chat.** Um script no Windows (`runner/watch.py`)
  consulta `pending_triggers` a cada 10 s, sem custo de modelo, e só quando há
  pendência acorda o Claude Code sem interface (`claude -p`, Sonnet, contexto limpo)
  para tratar a fila e terminar. Registrado como tarefa do Windows: sobe no logon,
  reinicia se cair, uma instância só
- O Claude só enxerga as ferramentas do conector `evoapi`, `Skill` e `Read`; o
  contexto vem de `runner/CLAUDE.md`, a instrução de cada acionamento de
  `runner/PROMPT.md`, e a skill de arquivamento fica em `runner/.claude/skills`
- Sem login no Claude Code o executor não acorda ninguém e diz isso no log; três
  acionamentos seguidos sem esvaziar a fila recuam cinco minutos, e o vigia do
  servidor avisa Max
- Motivação: cada volta do laço no chat era um turno pago mesmo com a fila vazia, e
  a sessão morria em silêncio. Aqui a vigília é HTTP e a morte do processo é coberta
  pelo Agendador de Tarefas

### 🛡️ Vigia da fila

- **O servidor avisa quando o laço morre.** Se uma instrução ficar mais de
  `EVOLUTION_WATCHDOG_MINUTES` (padrão 10) na fila sem ser tratada, ele manda uma
  mensagem na conversa pessoal do dono dizendo quantas estão paradas e desde quando.
  Repete no máximo a cada `EVOLUTION_WATCHDOG_COOLDOWN_MINUTES` (padrão 60) e rearma
  sozinho quando a fila volta a andar
- Motivação: a sessão do laço morreu depois de um redeploy e o servidor passou nove
  horas enfileirando instruções que ninguém consumia, sem nada na tela acusar. Essa é
  a única falha que o servidor consegue ver por conta própria, então passa a gritar
- O aviso é uma mensagem do dono na conversa dele, o que a tornaria instrução: o id é
  marcado como tratado assim que o envio devolve, e o vigia ignora pendências que
  comecem com o seu próprio marcador, caso a marcação falhe
- Eventos ganham carimbo numérico `ts`; `get_instance_info` passa a expor `vigia` e
  `dono_configurado`. Sem `EVOLUTION_OWNER_NUMBER` o vigia fica desligado e diz por quê

### 💾 Registro persistente do que já foi tratado

- **`store.py`**: com `EVOLUTION_DB_URL` apontando para um Postgres, o "já respondi isto"
  sobrevive a restart e deploy. Sem a variável, cai para memória, como antes
- Sem isso, um deploy fazia o laço responder de novo a instruções antigas, em conversa
  de terceiro. É o tipo de erro que aparece publicamente
- Banco fora do ar não derruba nada: degrada para memória e diz isso em
  `get_instance_info`. E numa falha de consulta o registro responde "já tratado",
  porque repetir uma resposta em público é pior que atrasá-la
- O bot do servidor e a sessão em laço passam a compartilhar o mesmo registro, então um
  não repete o que o outro já respondeu
- Extra opcional `[db]` (psycopg)

---

## [1.1.0] - 2025-10-24

### 🐳 Docker & HTTP Support

Esta release adiciona suporte completo a Docker e modo HTTP, permitindo deploy em produção e acesso via API REST.

### ✨ Adicionado

#### HTTP Server (FastAPI)
- **Servidor HTTP REST** completo expondo todas as 14 ferramentas MCP
- **Swagger UI interativo** em `/docs` para testar endpoints
- **ReDoc** em `/redoc` com documentação alternativa
- **CORS configurado** para permitir chamadas de frontends
- **Pydantic models** para validação de requests
- **Healthcheck endpoint** em `/health` para monitoramento
- **14 endpoints REST:**
  - `POST /messages/text` - Enviar mensagem de texto
  - `POST /messages/media` - Enviar mídia
  - `GET /chats` - Listar conversas
  - `GET /contacts` - Listar contatos
  - `GET /messages/{number}` - Buscar mensagens
  - `GET /instance/status` - Status da instância
  - `POST /presence` - Definir presença
  - `POST /messages/mark-read` - Marcar como lido
  - `POST /chats/archive` - Arquivar conversa
  - `DELETE /chats/{number}` - Deletar conversa
  - `GET /profile/picture/{number}` - Foto de perfil
  - `GET /profile/status/{number}` - Status/bio
  - `POST /check-number` - Verificar número no WhatsApp
  - `GET /profile/business/{number}` - Perfil comercial
  - `POST /cache/clear` - Limpar cache manualmente

#### Docker Compose Stack
- **Stack completa** com 4 serviços orquestrados:
  - PostgreSQL 15 (database para Evolution API)
  - Redis 7 (cache e queue)
  - Evolution API (WhatsApp gateway)
  - MCP HTTP Server (nosso servidor REST)
- **Dockerfile multi-stage** para imagem otimizada
- **Healthchecks** em todos os serviços
- **Volumes persistentes** para dados críticos
- **Network isolada** para comunicação entre containers
- **Variáveis de ambiente** via `.env.docker`
- **Usuário não-root** no container (segurança)

#### Documentação Docker
- **docker/README.md** (500+ linhas) com:
  - Quick start (3 comandos)
  - Guia de QR code para conectar WhatsApp
  - Exemplos de uso da API
  - Troubleshooting completo
  - Procedimentos de backup/restore
  - Comandos úteis (logs, restart, cleanup)
  - Práticas de segurança
- **docker/.env.docker.example** com template de configuração
- **README.md principal atualizado** com seção Docker

### 🔧 Modificado

#### Dependencies
- Adicionado `fastapi>=0.104.0` para servidor HTTP
- Adicionado `uvicorn[standard]>=0.24.0` para ASGI server

#### README.md
- Nova seção "Quick Start com Docker"
- Seção "Modos de Uso" explicando stdio vs HTTP
- Pré-requisitos atualizados incluindo Docker

### 📊 Estatísticas

- **1 novo servidor HTTP** com 14 endpoints REST
- **500+ linhas** de documentação Docker
- **398 linhas** de código HTTP server
- **Stack completa** production-ready
- **Dual-mode** support (stdio + HTTP)

### 🎯 Use Cases

**Modo Stdio (Local):**
- Uso pessoal com Claude Desktop
- Desenvolvimento e testes
- Sem necessidade de servidor

**Modo HTTP (Docker):**
- Deploy em produção
- Acesso remoto/equipes
- Integração com outros sistemas
- Auto-healing com healthchecks
- Escalabilidade horizontal

### 🔒 Segurança

- Container roda com usuário não-root
- Multi-stage build (menor superfície de ataque)
- Variáveis sensíveis via environment
- `.env.docker` no gitignore
- HTTPS recomendado para produção (via reverse proxy)

---

## [1.0.0] - 2025-10-24

### 🎉 Primeira Release Estável!

Esta é a primeira release production-ready do Evolution API MCP Server, com todas as issues críticas resolvidas e funcionalidade completa.

### ✨ Adicionado

#### Core Features
- **14 ferramentas MCP** para integração completa com WhatsApp via Evolution API:
  - `get_chats` - Lista conversas recentes com enriquecimento de nomes
  - `get_contacts` - Busca contatos (unificado com filtros opcionais)
  - `get_messages` - Busca mensagens de uma conversa
  - `send_text` - Envia mensagens de texto
  - `send_media` - Envia mídias (imagem, vídeo, documento, áudio)
  - `get_instance_status` - Status da instância
  - `set_presence` - Define presença (online, offline, etc)
  - `mark_as_read` - Marca mensagem como lida
  - `archive_chat` - Arquiva conversa
  - `delete_chat` - Deleta conversa
  - `get_profile_picture` - Busca foto de perfil
  - `get_profile_status` - Busca status/bio
  - `check_number` - Verifica se número está no WhatsApp
  - `get_business_profile` - Busca perfil comercial

#### Otimizações de Performance
- **Cache inteligente de contatos** com TTL de 5 minutos
- **Enriquecimento automático** de nomes em conversas (bulk fetch)
- **Método `clear_cache()`** para limpeza manual do cache
- Redução de N+1 requests para 2 requests fixos

#### Validações Robustas
- Validação de `media_type` contra tipos permitidos
- Validação de URLs (HTTP/HTTPS) para mídias
- Validação de tamanho de texto (65KB limit do WhatsApp)
- Validação de tamanho de caption (1024 caracteres)
- Mensagens de erro descritivas antes de chamar a API

#### Documentação Completa
- README.md com instalação, configuração e exemplos
- ROADMAP.md com plano de desenvolvimento de 4 fases
- TODO.md com tarefas granulares
- KNOWN_ISSUES.md com issues documentadas e soluções
- LICENSE (MIT)
- Este CHANGELOG.md

### 🔧 Corrigido

#### Issue #1: Duplicação de Código ✅
- **Problema:** Funções `fetch_contacts()` e `find_contacts()` duplicadas
- **Solução:** Unificadas em `fetch_contacts(contact_id=None)`
- **Impacto:** Código mais limpo, menos confusão para LLM

#### Issue #2: Cache Sem Expiração ✅
- **Problema:** Cache de nomes nunca expirava, causando nomes desatualizados
- **Solução:** Implementado TTL de 5 minutos com auto-refresh
- **Impacto:** Nomes sempre atualizados sem necessidade de restart

#### Issue #3: Validações Ausentes ✅
- **Problema:** Validações só na API, erros tardios e genéricos
- **Solução:** Validações client-side com mensagens descritivas
- **Impacto:** Erros detectados imediatamente com feedback claro

#### Issue #4: Endpoint Incorreto de Contatos ✅
- **Problema:** Endpoint `/chat/contacts/{instanceId}` retornava 404
- **Solução:** Corrigido para `/chat/findContacts/{instanceId}`
- **Impacto:** Nomes de contatos aparecendo corretamente

#### Issue #5: Formato de Resposta Incorreto ✅
- **Problema:** Esperava `{"data": [...]}` mas recebia lista direta
- **Solução:** Atualizado parsing para aceitar lista direta
- **Impacto:** 923 contatos detectados e 922 nomes mapeados

### 🧪 Testado

- **Suite de testes automáticos** (`test_phase1.py`)
- **11 testes, 100% de sucesso:**
  - 6 testes de validação
  - 3 testes de cache
  - 2 testes de deduplicação
- Testado com instância real (1170+ contatos)

### 📚 Documentação

#### Arquivos Criados
- `README.md` - Guia completo de uso
- `ROADMAP.md` - Planejamento de 4 fases
- `TODO.md` - Tarefas granulares
- `KNOWN_ISSUES.md` - Documentação de issues
- `LICENSE` - MIT License
- `CHANGELOG.md` - Este arquivo

#### Documentação de Código
- Docstrings completas em todas funções
- Type hints em Python 3.10+
- Exemplos de uso em docstrings
- Comentários explicativos em lógica complexa

### 🏗️ Estrutura Técnica

```
evoapi-mcp/
├── src/evoapi_mcp/
│   ├── __init__.py
│   ├── server.py        # MCP Server (14 tools)
│   ├── client.py        # HTTP Client com validações
│   └── config.py        # Configuração
├── test_phase1.py       # Suite de testes
├── README.md
├── ROADMAP.md
├── TODO.md
├── KNOWN_ISSUES.md
├── CHANGELOG.md
├── LICENSE
└── pyproject.toml
```

### 🔒 Segurança

- API key nunca exposta em logs
- Validação de URLs para prevenir SSRF
- Validação de inputs antes de processar
- Timeout configurável para prevenir DoS

### 📦 Dependências

- `fastmcp >= 0.6.0` - Framework MCP
- `requests >= 2.32.3` - HTTP client
- `python-dotenv >= 1.0.1` - Gerenciamento de .env

### 🎯 Compatibilidade

- **Python:** 3.10+
- **Evolution API:** v2.x
- **Claude Desktop:** Latest
- **OS:** macOS, Linux, Windows

### 📊 Estatísticas

- **14 ferramentas MCP** implementadas
- **5 issues críticas** resolvidas
- **1170+ contatos** testados em produção
- **922 nomes** enriquecidos automaticamente
- **100% testes** passando

---

## [0.1.0] - 2025-10-23

### Versão Inicial (Pré-Release)

- Implementação inicial do MCP Server
- Integração básica com Evolution API
- 14 ferramentas funcionais
- Documentação básica

---

## Links

- [GitHub Repository](https://github.com/PabloBispo/evoapi-mcp)
- [Evolution API Documentation](https://doc.evolution-api.com/)
- [Model Context Protocol](https://modelcontextprotocol.io/)

---

## Convenções de Versionamento

Este projeto usa [Semantic Versioning](https://semver.org/):

- **MAJOR** (1.x.x): Mudanças incompatíveis na API
- **MINOR** (x.1.x): Novas funcionalidades compatíveis
- **PATCH** (x.x.1): Correções de bugs compatíveis

## Tipos de Mudanças

- `Adicionado` - Novas funcionalidades
- `Modificado` - Mudanças em funcionalidades existentes
- `Descontinuado` - Funcionalidades que serão removidas
- `Removido` - Funcionalidades removidas
- `Corrigido` - Correções de bugs
- `Segurança` - Correções de vulnerabilidades
