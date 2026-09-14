# Executor local do WhatsApp

Um script que fica rodando no Windows, pergunta ao servidor EVOAPI a cada 10 s se
há instrução pendente e, só quando há, acorda o Claude Code sem interface
(`claude -p`) para tratar a fila e terminar.

Substitui o laço `/loop 30s` dentro de um chat do Claude. Vantagens: custo zero
enquanto não há nada para fazer, contexto limpo a cada acionamento, e reinício
automático pelo Agendador de Tarefas se o script cair.

## Arquivos

| Arquivo | Papel |
|---|---|
| `watch.py` | o vigia: consulta a fila e chama `claude -p` |
| `PROMPT.md` | o que o Claude faz a cada acionamento |
| `CLAUDE.md` | contexto fixo: quem é Max, regras que não se quebram |
| `.claude/skills/arquivar-financeiro-whatsapp/` | a skill de arquivamento, carregada quando chega documento |
| `.env` | URL e token do servidor MCP (não vai para o git) |
| `instalar-tarefa.ps1` / `desinstalar-tarefa.ps1` | registram e removem a tarefa do Windows |
| `.local/executor.log` | o que aconteceu (rotaciona sozinho) |

## Requisitos

- Python 3 com `requests` (`pip install requests`).
- Claude Code instalado e **logado** nesta máquina. Conferir com `claude auth status`;
  se `loggedIn` for `false`, rode `claude auth login`. O executor não acorda o Claude
  sem login: registra o erro e espera.
- `.env` preenchido (copie de `.env.example`).

## Instalar

```powershell
.\instalar-tarefa.ps1
```

Registra a tarefa "EVOAPI Executor": sobe no logon, sem janela, reinicia a cada
minuto se cair, uma instância só. Já inicia na hora.

## Acompanhar

```powershell
Get-Content .local\executor.log -Tail 20 -Wait
```

Cada acionamento gera uma linha com turnos, duração, custo estimado e a frase final
do Claude. Ferramentas que ele tentou usar sem permissão aparecem em `negados`.

## Parar

```powershell
.\desinstalar-tarefa.ps1
```

## O que o Claude pode fazer

As ferramentas do conector `evoapi`, busca na web, leitura de skills e de arquivos,
e os servidores listados em `EXTRA_MCP_SERVERS` no `.env`. Sem Bash, sem escrita.
Se uma pendência exigir mais, o log mostra a negação em `negados`.

### Conectores da conta (Trello, Gmail, Agenda, Drive)

Os conectores ligados na sua conta claude.ai ficam disponíveis ao Claude Code
logado, com o prefixo `claude_ai_<Nome>`. Para o executor usá-los, liste-os em
`EXTRA_MCP_SERVERS`, por exemplo `claude_ai_Trello,claude_ai_Gmail`, e reinicie a
tarefa. Cada servidor extra acrescenta as descrições das ferramentas dele ao
contexto de cada acionamento, cerca de um centavo por execução, então liste só
o que o WhatsApp realmente precisa. Servidores locais cadastrados com
`claude mcp add` entram pelo próprio nome.

Para liberar só parte de um servidor, nomeie as ferramentas no formato
`<servidor>__<ferramenta>`. É assim que o Gmail entra só de leitura: buscar,
ler conversa, ler mensagem e listar marcadores. Enviar, responder, encaminhar
e apagar ficam de fora, e o Claude sabe que ficam.

## Quando algo trava

Se três acionamentos seguidos não tirarem nada da fila, o executor recua cinco
minutos antes de tentar de novo, para não queimar uso num item que não sai. Nesse
cenário o vigia do servidor avisa Max no WhatsApp em uns dez minutos.

## Limites

- Precisa do PC ligado e da sessão do Windows aberta. Suspensão para tudo.
- Conectores do app (Trello, Gmail, Agenda) não estão aqui. Arquivar no Drive
  funciona porque roda no servidor.
