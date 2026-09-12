# 🚀 Evolution API MCP Server

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![MCP](https://img.shields.io/badge/MCP-1.1.2-green.svg)](https://modelcontextprotocol.io/)

**MCP Server para Evolution API** - Integração completa do WhatsApp com Claude Desktop via Model Context Protocol (MCP).

Este servidor permite que o Claude Desktop interaja com o WhatsApp através da [Evolution API](https://evolution-api.com/), possibilitando envio de mensagens, gerenciamento de conversas, busca de contatos e muito mais.

---

## ✨ Features

### 📤 Envio de Mensagens
- ✅ Mensagens de texto com preview de links
- ✅ Imagens com legendas
- ✅ Vídeos com legendas
- ✅ Documentos (PDF, DOCX, XLSX, etc)
- ✅ Áudios

### 💬 Gerenciamento de Conversas
- ✅ Listar conversas ativas com nomes
- ✅ Buscar mensagens por texto
- ✅ Obter mensagens de conversa específica
- ✅ Enriquecimento automático com nomes de contatos

### 👥 Gerenciamento de Contatos
- ✅ Listar contatos salvos
- ✅ Buscar contatos por ID
- ✅ Obter nome de contato por número
- ✅ Cache inteligente de nomes (5min TTL)

### ⚡ Performance
- ✅ Bulk fetch de contatos (1 request vs N+1)
- ✅ Cache em memória para nomes
- ✅ Enriquecimento automático de chats

### 🛡️ Qualidade
- ✅ Validação de números de telefone
- ✅ Type hints completos
- ✅ Error handling robusto
- ✅ Logs estruturados

### 🎙️ Áudios (v1.2)
- ✅ Transcrição de voice notes em texto, feita no servidor
- ✅ Backend local (`faster-whisper`) ou API compatível com a OpenAI (OpenAI, Groq, whisper.cpp)
- ✅ Cache de transcrição por mensagem
- ✅ `download_media(extract_text=True)` transcreve quando o anexo é áudio ou vídeo

### 🪙 Economia de tokens (v1.2)
- ✅ Respostas compactas por padrão (~10x menos tokens que o JSON bruto do WhatsApp)
- ✅ `download_media` grava o anexo em disco e devolve só caminho + metadados (nunca base64)
- ✅ `send_file` envia arquivo local sem passar base64 pelo chat
- ✅ `send_render` desenha a imagem a partir do SVG do modelo: o desenho viaja como texto
- ✅ `send_url` envia arquivo que já está na web pelo link (o servidor baixa, o chat só vê a URL)
- ✅ Extração de texto de PDF/txt (`extract_text=True`) para identificar documentos sem abri-los
- ✅ Busca textual local em `find_messages`/`get_chat_messages` (só as mensagens que casam voltam)
- ✅ Limites padrão menores (20 itens) e corte de texto configurável
- ✅ Erros HTTP truncados (sem páginas HTML/stack traces no contexto)

---

## 🪙 Uso econômico com Claude

O JSON bruto da Evolution API traz thumbnails em base64, chaves de mídia, hashes e
metadados de dispositivo em cada mensagem: 50 mensagens podem custar 30-50 mil tokens.
Nesta versão as tools devolvem apenas o essencial:

```json
{"id":"3EB0ABC","ts":"2025-09-11 14:03","from":"5511999999999","name":"Contador",
 "type":"document","text":"Segue o boleto","file":"boleto-setembro.pdf",
 "mime":"application/pdf","size":48213}
```

Fluxo recomendado para arquivos recebidos:

1. `get_chat_messages(number, limit=10)` ou `find_messages(query="boleto")`
2. `download_media(message_id=id, extract_text=True)` → `{path, file, mime, size, text}`
3. Use o `path` local (mover, renomear, subir para o Drive) ou `send_file(number, path)` para reenviar

| Variável | Padrão | Efeito |
|----------|--------|--------|
| `EVOLUTION_MEDIA_DIR` | `~/.evoapi-mcp/media` | Pasta onde `download_media` grava os anexos |
| `EVOLUTION_DEFAULT_LIMIT` | `20` | Itens por chamada quando `limit` não é informado |
| `EVOLUTION_MAX_TEXT_CHARS` | `500` | Corte do texto de cada mensagem (`0` = sem corte) |

Todas as tools de leitura aceitam `full=True` para devolver o objeto bruto quando você
realmente precisar dele. `get_media_base64` continua disponível por compatibilidade,
mas custa dezenas de milhares de tokens: prefira `download_media`.

Para extrair texto de PDF instale o extra opcional:

```bash
pip install -e ".[pdf]"
```

### Transcrição de áudios

Um voice note de 1 minuto tem cerca de 500 KB. Em base64 isso passa de 600 mil
caracteres, o que é impraticável mandar para o modelo. A transcrição roda no servidor
e só o texto volta:

```
transcribe_audio(message_id="3EB0ABC")
→ {"text":"Bom dia, consegue enviar o boleto hoje?","backend":"api",
   "model":"whisper-1","language":"pt","seconds":6.2,"path":"...","cached":false}
```

Mensagens de áudio aparecem nas listagens com `"type":"audio"` e `"voice":true` quando
são gravadas na hora. Passe o `id` delas para `transcribe_audio`. O resultado fica em
cache em `<EVOLUTION_MEDIA_DIR>/.transcripts/<id>.json`, então repetir a pergunta não
transcreve de novo.

Escolha um backend:

```bash
# Opção 1: API compatível com a OpenAI (nada para instalar)
EVOLUTION_TRANSCRIBE_API_KEY=sk-...          # ou OPENAI_API_KEY / GROQ_API_KEY
EVOLUTION_TRANSCRIBE_API_URL=https://api.groq.com/openai/v1/audio/transcriptions  # opcional

# Opção 2: modelo local, sem chave e sem rede
pip install -e ".[audio]"
EVOLUTION_TRANSCRIBE_BACKEND=local
EVOLUTION_TRANSCRIBE_MODEL=small
```

Sem nenhum dos dois, `transcribe_audio` devolve um erro explicando o que configurar.
`get_instance_info()` mostra o backend ativo.

### Documento sem camada de texto

Comprovante fotografado e PDF escaneado não têm texto para extrair. `view_media`
devolve a página como **imagem de verdade** pelo protocolo, e o modelo lê com a própria
visão, sem OCR:

```
view_media(message_id="...", page=1, pages=1)
```

Custa cerca de 1.500 tokens por página, contra dezenas de milhares do base64, que ainda
por cima chega como texto e é ilegível para o modelo. Para documento que já tem texto,
`download_media(extract_text=True)` continua muito mais barato.

Requer o extra `[image]` (pypdfium2 e Pillow).

### Imagem criada pelo modelo (gráfico, cartão, aviso)

Quando é o próprio modelo que cria a imagem, mandar o PNG em `send_image_base64` custa
o arquivo inteiro em base64 dentro da conversa. `send_render` inverte a rota: o modelo
escreve o **SVG**, que é texto, e o servidor rasteriza e envia.

```
send_render(number="5511999999999", svg="<svg xmlns=...>...</svg>", caption="vendas de setembro")
```

Um gráfico de barras de 1.159 caracteres de SVG vira um PNG cujo base64 tem 16.780:
14x mais barato neste caso, e a diferença cresce conforme a imagem ganha detalhe —
é o conteúdo do arquivo que pesa, não o envio. O PNG fica gravado em
`<EVOLUTION_MEDIA_DIR>/render/`, então dá para reenviar ou arquivar pelo `path`
devolvido sem desenhar de novo.

Escreva um SVG completo (com `xmlns` e `width`/`height` na raiz) e use fontes comuns —
`sans-serif`, `serif`, `monospace` —, porque as fontes disponíveis são as do servidor.
`width`/`height` redimensionam na rasterização e `background=null` mantém a transparência.
Para imagem que já existe em arquivo continue usando `send_file`, e para imagem que já
está na web, `send_image` com a URL (aí quem baixa é a Evolution, e o custo é o do link).

Requer o extra `[svg]` (cairosvg) e, no sistema, a `libcairo2` — já incluída na imagem
Docker. Sem ela, a tool devolve um erro dizendo o que instalar em vez de quebrar.

### Imagem que já existe em algum lugar

Quando o arquivo já está publicado, o link é tudo que precisa atravessar a conversa:

```
send_url(number="5511999999999", url="https://drive.google.com/file/d/1AbC.../view", caption="a arte")
```

O servidor baixa e envia. Link de compartilhamento do **Google Drive** e do **Dropbox**
é convertido sozinho para o link do arquivo em si — sem isso o download traz a página
HTML, não a imagem. O tipo (`image`, `video`, `audio`, `document`) sai do `Content-Type`,
o arquivo fica em `<EVOLUTION_MEDIA_DIR>/web/` e o teto de download é 16 MB.

No Drive, o arquivo precisa estar compartilhado como *qualquer pessoa com o link* —
o que também significa que qualquer um com o link o vê. Se não estiver, a tool diz isso
em vez de falhar de forma opaca.

`send_image(number, image_url)` continua sendo o mais barato de todos quando a URL é
direta: quem baixa é a própria Evolution, e o servidor nem toca no arquivo. `send_url`
é o atalho para quando isso não dá certo — link de compartilhamento, ou uma URL que a
Evolution não enxerga.

Por segurança, o download só sai para endereço público: `http`/`https`, e todo salto de
redirecionamento é conferido, para que o servidor não sirva de ponte para a rede interna
a partir de um link que chegou pelo WhatsApp.

### PDFs protegidos por senha

Boletos costumam vir cifrados. Passe a senha e o arquivo é gravado **já destravado**,
tanto no download quanto no arquivamento:

```
download_media(message_id="...", password="1234", extract_text=True)
archive_to_drive(message_id="...", folder="MR/2026/09.2026/BOLETO", password="1234")
```

Sem a senha, a extração de texto volta com `text_error` avisando que o PDF é protegido.

### Conversas `@lid`

O WhatsApp está migrando conversas de `<numero>@s.whatsapp.net` para `<id opaco>@lid`;
nesses casos o telefone só aparece em `remoteJidAlt`/`participantAlt`. As tools tratam isso:
`get_chat_messages(number)` resolve o jid real pela lista de conversas (com cache), as
mensagens compactas mostram o telefone em `from` sempre que ele existir, e `list_chats`
devolve `jid` (para passar às outras tools) e `number` separados. Um jid `@lid` ou `@g.us`
pode ser passado diretamente em `number`/`chat_id`.

### MCP via HTTP com token (Easypanel, Docker, acesso remoto)

Para expor o servidor MCP por Streamable HTTP protegido por Bearer token:

```bash
MCP_AUTH_TOKEN=um-segredo PORT=3000 python -m evoapi_mcp.mcp_http
```

O endpoint fica em `http://host:3000/mcp` e exige `Authorization: Bearer um-segredo`.
Isso substitui o wrapper externo que fazia monkeypatch em `find_messages`: todas as
tools (inclusive `get_media_base64`, `send_image_base64` e `send_document_base64`)
já estão no `server.py`. Em uma imagem Docker, troque o `CMD` por:

```dockerfile
CMD ["python", "-m", "evoapi_mcp.mcp_http"]
```

---

## 📋 Pré-requisitos

1. **Python 3.10+** (para uso local)
2. **Claude Desktop** instalado (para modo MCP stdio)
3. **Docker & Docker Compose** (para deploy completo)
4. **Instância Evolution API** rodando (ou use nosso Docker Compose)
   - Você precisa de:
     - URL base da API (ex: `https://api.example.com`)
     - API Token (apikey)
     - Nome da instância (instance name)

---

## 🐳 Quick Start com Docker (Recomendado!)

**Deploy completo Evolution API + MCP HTTP Server em 3 comandos:**

```bash
cd docker/
cp .env.docker.example .env.docker
# Edite .env.docker com suas credenciais
docker-compose up -d
```

**Resultado:**
- ✅ PostgreSQL rodando
- ✅ Redis rodando
- ✅ Evolution API em http://localhost:8080
- ✅ MCP HTTP Server em http://localhost:3000
- ✅ Swagger UI em http://localhost:3000/docs

**Documentação completa:** [docker/README.md](docker/README.md)

---

## 🔧 Instalação Local (Modo MCP Stdio)

### 1. Clone o Repositório

```bash
git clone https://github.com/PabloBispo/evoapi-mcp.git
cd evoapi-mcp
```

### 2. Instale as Dependências

```bash
# Usando uv (recomendado)
uv sync

# OU usando pip
pip install -e .
```

### 3. Configure as Variáveis de Ambiente

Crie um arquivo `.env` na raiz do projeto:

```bash
# Evolution API Configuration
EVOLUTION_BASE_URL=https://your-evolution-api.com
EVOLUTION_API_TOKEN=your-api-token-here
EVOLUTION_INSTANCE_NAME=your-instance-name

# Optional: Timeout (default: 30 seconds)
EVOLUTION_TIMEOUT=30
```

**Exemplo real:**
```bash
EVOLUTION_BASE_URL=https://pevo.ntropy.com.br
EVOLUTION_API_TOKEN=9795FDFBB464-495E-A823-28573A5D39EE
EVOLUTION_INSTANCE_NAME=personal_pablo_bispo_wpp
EVOLUTION_TIMEOUT=15
```

### 4. Configure o Claude Desktop

Edite o arquivo de configuração do Claude Desktop:

**macOS:**
```bash
~/Library/Application Support/Claude/claude_desktop_config.json
```

**Windows:**
```bash
%APPDATA%\Claude\claude_desktop_config.json
```

Adicione o servidor MCP:

```json
{
  "mcpServers": {
    "evolution-api": {
      "command": "uv",
      "args": [
        "--directory",
        "/caminho/completo/para/evoapi-mcp",
        "run",
        "evoapi-mcp"
      ],
      "env": {
        "EVOLUTION_BASE_URL": "https://your-evolution-api.com",
        "EVOLUTION_API_TOKEN": "your-api-token-here",
        "EVOLUTION_INSTANCE_NAME": "your-instance-name"
      }
    }
  }
}
```

**⚠️ IMPORTANTE:** Use o caminho **absoluto** completo para o diretório do projeto!

### 5. Reinicie o Claude Desktop

Feche completamente (⌘Q no macOS) e reabra o Claude Desktop.

---

## 🎯 Como Usar

### Exemplos de Comandos no Claude Desktop

#### 📤 Enviar Mensagens

```
Envie uma mensagem "Olá! Tudo bem?" para o número 5511999999999
```

```
Envie a imagem https://example.com/foto.jpg com legenda "Confira!" para 5511987654321
```

```
Envie o documento https://example.com/relatorio.pdf para 5511999999999
```

#### 💬 Consultar Conversas

```
Liste as 10 conversas mais recentes do meu WhatsApp
```

```
Mostre as últimas 50 mensagens do número 5511999999999
```

```
Busque mensagens que contenham a palavra "reunião"
```

#### 👥 Gerenciar Contatos

```
Liste os primeiros 20 contatos do meu WhatsApp
```

```
Qual é o nome do contato 5511987654321?
```

```
Mostre informações do contato 5511999999999
```

#### ℹ️ Status da Conexão

```
Verifique o status da conexão do WhatsApp
```

```
Mostre informações da instância
```

---

## 🛠️ Tools Disponíveis

### Envio de Mensagens

| Tool | Descrição | Parâmetros |
|------|-----------|------------|
| `send_text_message` | Envia mensagem de texto | `number`, `text`, `link_preview` |
| `send_image` | Envia imagem | `number`, `image_url`, `caption` |
| `send_video` | Envia vídeo | `number`, `video_url`, `caption` |
| `send_document` | Envia documento | `number`, `document_url`, `filename`, `caption` |
| `send_audio` | Envia áudio | `number`, `audio_url` |

### Conversas e Mensagens

| Tool | Descrição | Parâmetros |
|------|-----------|------------|
| `list_chats` | Lista conversas ativas | `limit` |
| `get_chat_messages` | Obtém mensagens de conversa | `number`, `limit` |
| `find_messages` | Busca mensagens por termo | `query`, `chat_id`, `limit` |

### Contatos

| Tool | Descrição | Parâmetros |
|------|-----------|------------|
| `get_contacts` | Lista contatos salvos | `limit` |
| `find_contact` | Busca contato específico | `contact_id`, `limit` |
| `get_contact_name_by_number` | Obtém nome por número | `number` |

### Status e Presença

| Tool | Descrição | Parâmetros |
|------|-----------|------------|
| `get_connection_status` | Verifica status da conexão | - |
| `get_instance_info` | Informações da instância | - |
| `set_presence` | Define status de presença | `status`, `number` |

---

## 🌐 Modos de Uso

Este projeto suporta **dois modos de operação**:

### 1. Modo Stdio (Claude Desktop)
- Comunicação via stdio (stdin/stdout)
- Integração nativa com Claude Desktop
- Melhor para uso pessoal local
- Configuração em `claude_desktop_config.json`

### 2. Modo HTTP (Docker/Servidor)
- API REST com Swagger UI
- Deploy em containers Docker
- Acesso remoto via HTTP
- Ideal para produção e equipes
- Swagger docs em `/docs`

**Você pode usar ambos simultaneamente!** 🎉

---

## 🔍 Troubleshooting

### ❌ Erro: "ModuleNotFoundError: No module named 'evoapi_mcp'"

**Solução:**
- Verifique se o caminho no `claude_desktop_config.json` é **absoluto** (não relativo)
- Use `pwd` para obter o caminho completo: `cd evoapi-mcp && pwd`

### ❌ Erro: "HTTP 401: Unauthorized"

**Solução:**
- Verifique se o `EVOLUTION_API_TOKEN` está correto
- Confirme que o token tem permissões necessárias

### ❌ Erro: "HTTP 404: Endpoint não encontrado"

**Solução:**
- Verifique se o `EVOLUTION_BASE_URL` está correto
- Confirme se a Evolution API está rodando
- Teste manualmente: `curl https://your-api.com/instance/connectionState/instance-name -H "apikey: your-token"`

### ❌ Os nomes dos contatos não aparecem

**Solução:**
- Reinicie o Claude Desktop para limpar o cache
- Verifique se os contatos estão salvos no WhatsApp
- Cache expira automaticamente após 5 minutos

### ❌ Listagem de conversas muito lenta

**Solução:**
- Já otimizado! Usa bulk fetch de contatos (2 requests ao invés de N+1)
- Se ainda estiver lento, verifique a conexão com a Evolution API

### 🔍 Como Ver os Logs

Os logs aparecem no **stderr** do processo MCP. Para vê-los:

**macOS/Linux:**
```bash
# Logs do Claude Desktop
tail -f ~/Library/Logs/Claude/mcp*.log
```

**Ou rode manualmente para debug:**
```bash
cd evoapi-mcp
uv run evoapi-mcp
# Depois teste chamando tools via stdin
```

---

## 🗺️ Roadmap

Veja o arquivo [ROADMAP.md](ROADMAP.md) para planos futuros:

### 🔴 FASE 1 - Correções Críticas (Curto Prazo)
- [ ] Unificar duplicações de código
- [ ] Adicionar validações robustas
- [ ] Cache com TTL

### 🟡 FASE 2 - Melhorias de Qualidade (Médio Prazo)
- [ ] Type safety com Pydantic
- [ ] Retry logic automático
- [ ] Sanitização de logs

### 🟢 FASE 3 - Novas Funcionalidades (Longo Prazo)
- [ ] Gerenciamento de grupos
- [ ] Deletar/editar mensagens
- [ ] Upload de arquivos locais
- [ ] Download de mídias recebidas
- [ ] Status (stories)

### 🧪 FASE 4 - DevOps
- [ ] Testes automatizados
- [ ] CI/CD com GitHub Actions
- [ ] Documentação completa

---

## 📚 Documentação Adicional

- **[ROADMAP.md](ROADMAP.md)** - Plano de desenvolvimento futuro
- **[TODO.md](TODO.md)** - Tarefas pendentes organizadas
- **[KNOWN_ISSUES.md](KNOWN_ISSUES.md)** - Problemas conhecidos e soluções
- **[FIXES.md](FIXES.md)** - Histórico de correções aplicadas

---

## 🤝 Contribuindo

Contribuições são bem-vindas! 🎉

### Como Contribuir

1. Fork o projeto
2. Crie uma branch para sua feature (`git checkout -b feature/amazing-feature`)
3. Commit suas mudanças (`git commit -m 'Add amazing feature'`)
4. Push para a branch (`git push origin feature/amazing-feature`)
5. Abra um Pull Request

### Diretrizes

- Adicione testes para novas funcionalidades
- Atualize a documentação
- Siga o estilo de código existente
- Use commits semânticos

---

## 📄 Licença

Este projeto está sob a licença MIT. Veja o arquivo [LICENSE](LICENSE) para mais detalhes.

---

## 🙏 Agradecimentos

- [Evolution API](https://evolution-api.com/) - API de WhatsApp incrível
- [Model Context Protocol](https://modelcontextprotocol.io/) - Protocolo MCP
- [Anthropic](https://anthropic.com/) - Claude Desktop
- [FastMCP](https://github.com/jlowin/fastmcp) - Framework Python para MCP

---

## 📞 Suporte

- 🐛 **Issues:** [GitHub Issues](https://github.com/PabloBispo/evoapi-mcp/issues)
- 💬 **Discussões:** [GitHub Discussions](https://github.com/PabloBispo/evoapi-mcp/discussions)

---

## ⭐ Star History

Se este projeto foi útil, considere dar uma estrela! ⭐

[![Star History Chart](https://api.star-history.com/svg?repos=PabloBispo/evoapi-mcp&type=Date)](https://star-history.com/#PabloBispo/evoapi-mcp&Date)

---

**Feito com ❤️ usando Claude Code**
