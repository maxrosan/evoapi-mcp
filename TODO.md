# TODO

O que está pendente de verdade. Documento curto de propósito: o histórico do que já
foi feito mora no `CHANGELOG.md`, e como usar mora no `README.md`.

**Última atualização:** 2026-09-12

---

## 🔴 Pendente com dono fora do código

- [ ] **Rotacionar o token da Evolution.** O token da instância esteve commitado em
  quatro arquivos deste repositório desde os primeiros commits. Os arquivos já foram
  limpos, mas o valor continua no histórico do git e segue válido até ser trocado no
  painel da Evolution. Trocar lá e atualizar o `.env` do deploy.

---

## 🎯 Próximo

- [ ] **Grupos.** Não há nenhuma tool: criar, listar membros, adicionar e remover.
  Hoje dá para mandar mensagem para um `@g.us`, mas não para administrá-lo.

- [ ] **Retry com backoff em `_make_request`** (`client.py:285`). Uma falha de rede
  passageira hoje sobe inteira para quem chamou; a Evolution reinicia com alguma
  frequência e isso vira erro visível sem precisar.

- [ ] **Upload resumível no Drive.** `upload_file` usa o upload simples, que corta em
  5 MB (`drive.py:MAX_SIMPLE_UPLOAD`). Vídeo e PDF grande não são arquiváveis hoje.

- [ ] **Gestão de mensagens:** apagar, editar e reagir com emoji.

---

## 📋 Quando sobrar tempo

- [ ] Cobertura de teste do `http_server.py` além dos endpoints novos — os antigos
  (chats, contatos, presença) nunca foram testados.
- [ ] `ruff` no CI, junto do pytest.
- [ ] Paginação real em `list_chats` e `get_contacts` (hoje é corte por `limit`).
- [ ] O README não documenta o bot "IA:" — quem lê só o README não sabe que ele
  existe, nem como ligar. Hoje isso só está no `CHANGELOG.md` e no `.env.example`.
- [ ] Os links de clone, issues e discussões no README ainda apontam para o
  repositório de origem (`PabloBispo/evoapi-mcp`). Decidir se ficam como atribuição
  ou passam para este fork.

---

## Como este arquivo funciona

Entra aqui o que está pendente e é verdade hoje. O que foi feito sai daqui e vai para
o `CHANGELOG.md` — um item concluído que fica para trás neste arquivo é pior que
nenhum item, porque manda quem lê procurar problema que já não existe.
