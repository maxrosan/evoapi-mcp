---
name: arquivar-financeiro-whatsapp
description: "Arquiva documentos financeiros (nota, boleto, comprovante, recibo, recebimento) no Google Drive, buscando os arquivos no WhatsApp via conector EVOAPI. Use ao receber ou ao ser pedido para arquivar um documento."
---

# Arquivamento financeiro — WhatsApp + Google Drive

Max manda documentos financeiros pelo WhatsApp (conversa dele com ele mesmo) ou
anexa no chat. Esta skill define onde guardar, com que nome, e o que conferir antes.

## Buscar o documento no WhatsApp

Conector **EVOAPI** (Evolution API). A conversa dele com ele mesmo tem **dois
endereços**, por causa da migração do WhatsApp para endereçamento LID:

| JID | Conteúdo |
|---|---|
| `110818863673433@lid` | mensagens novas — o que ele acabou de mandar |
| `558499290327@s.whatsapp.net` | mensagens antigas (sem o nono dígito) |

Consulte **os dois** antes de dizer que não achou. `get_chat_messages(number=...)`
resolve o LID sozinho e informa no campo `chat` qual endereço acabou usando.

As mensagens voltam enxutas: `{id, ts, from, name, type, text, file, mime, size}`.
O identificador do anexo é o campo **`id`**. `messageId` era o nome antigo.

### Ordem das ferramentas

1. **Identificar, sempre primeiro:** `download_media(message_id, extract_text=True)`
   devolve `{path, file, mime, size, pages, text}`. Vem o **texto** do PDF, não o
   arquivo. Um PDF de 3 MB é identificado com cerca de 300 tokens.
2. **Áudio:** `transcribe_audio(message_id)` devolve o que ele falou, em texto.
   **Sem camada de texto:** quando o `download_media` disser que não há texto extraível,
   use `view_media(message_id)`. A página volta como imagem e você lê olhando, sem OCR.
   Cerca de 1.500 tokens por página. Nunca recorra ao base64 para isso.
3. **Arquivar:** `archive_to_drive(message_id, folder, filename)`. O servidor leva o
   arquivo do WhatsApp direto ao Drive e devolve só o link. Não use base64 para isso.

`download_media` grava no disco **do servidor** (`/data/media`), fora do seu alcance.
O `path` serve para passar a `send_file`, não para abrir o arquivo.

### Arquivar no Drive

`archive_to_drive(message_id, folder, filename)` faz o caminho inteiro no servidor:
baixa do WhatsApp, cria as pastas que faltarem e sobe ao Drive. Devolve
`{id, name, folder, size, link}`, algo como 90 tokens. Um comprovante que custaria
onze mil tokens em base64 sai por isso.

O `folder` é **relativo à pasta base do arquivamento**, então passe só
`{Empresa}/{AAAA}/{MM.AAAA}/{CATEGORIA}`, sem repetir FINANCEIRO.

O `filename` é o nome final já no padrão desta skill. Renomeie na hora de arquivar,
não depois.

Os arquivos ficam com a conta `maxrsan@gmail.com` como dona.

**Quando ainda é preciso base64:** praticamente nunca. PDF com senha o servidor
destrava sozinho (veja abaixo). Sobra o caso raro de um arquivo que você precise editar
antes de subir; aí use o `create_file` do conector do Drive com o conteúdo corrigido.

As ferramentas de base64 (`get_media_base64`, `send_*_base64`) **não existem mais** no
conector. Para descobrir o que é o documento, o papel é do `download_media`.

### Responder

- Texto: `send_text_message(number="5584999290327", text="...")`
- Arquivo que já está no servidor: `send_file(number, file_path, caption)`
- Arquivo que já está no Drive: `send_drive_file`
- Arquivo público na web: `send_url`

O conector expõe **26 ferramentas**. Se a sessão mostrar menos, ou ainda listar
`get_media_base64`, ela pegou uma lista velha — avise e abra uma sessão nova.

Não há monitoramento automático: ele avisa no chat quando mandar algo.

## Estrutura no Drive

Dentro de `Meu Drive/Claude/FINANCEIRO/`:

```
FINANCEIRO/
└── {Empresa ou Projeto}/
    └── {AAAA}/
        └── {MM.AAAA}/
            ├── NOTA/
            ├── BOLETO/
            ├── COMPROVANTE/
            ├── RECIBO/
            └── RECEBIMENTO/
```

Exemplo: `FINANCEIRO/MR/2026/08.2026/BOLETO/17.08.2026 - Econtec Contabilidade - R$ 350,00.pdf`

Crie as subpastas que faltarem. Para reorganizar arquivos que já estão no Drive, use
`update_file` com `title` + `parentId` — não refaça upload.

### Empresa ou Projeto (primeiro nível)

É **quem está por trás do gasto ou recebimento**, não quem emitiu o documento.
Exemplo: nota emitida pelo eletricista Daniel Michael Dantas, referente ao serviço na
obra, vai em `Construção - Paizinho Maria`, e não numa pasta com o nome dele.

| Pasta | Uso |
|---|---|
| `MR` | MR Empreendimentos — contabilidade, boletos dos lotes (Aldann / Paiva) |
| `FAS` | Faculdade do Seridó — serviços prestados e recebimentos |
| `Sol Prime` | Sol Prime Energias Renováveis — pagamentos a prestadores |
| `Construção - Paizinho Maria` | Obra da casa — material, mão de obra, prestadores |
| `Pessoal` | Contas pessoais (ex: Brisanet) |

Se não encaixar em nenhuma, **pergunte antes** de criar pasta nova.

### Categorias

| Pasta | O que vai nela |
|---|---|
| `NOTA` | Nota fiscal de serviço ou produto |
| `BOLETO` | Boleto a pagar (antes do pagamento) |
| `COMPROVANTE` | Pagamento efetuado — dinheiro que **saiu** |
| `RECIBO` | Recibo emitido ou recebido |
| `RECEBIMENTO` | Dinheiro **recebido** |

COMPROVANTE = saída; RECEBIMENTO = entrada. Havendo boleto e comprovante do mesmo
pagamento, guarde os dois, cada um na sua pasta.

## Nome do arquivo

```
DD.MM.AAAA - Emitente - R$ valor.extensão
```

- **Data:** boleto usa o **vencimento**; nota, recibo e comprovante usam a data de
  emissão/pagamento.
- **Emitente:** quem emitiu o documento, não quem pagou.
- Referência curta entre parênteses quando ajudar a identificar:
  `15.08.2026 - Aldann Construcoes (Lote 392) - R$ 490,15.pdf`

Nunca mantenha o nome original (ex: `CredCob-MAX...Protected.pdf`).

## PDFs protegidos por senha

Boletos da Aldann/Paiva pedem os primeiros dígitos do CPF — ele manda a senha junto.
Passe a senha e o servidor destrava sozinho, gravando e arquivando **a versão sem
senha**, para o arquivo seguir acessível sem depender de lembrar a senha:

```
download_media(message_id, password="1234", extract_text=True)
archive_to_drive(message_id, folder="...", filename="...", password="1234")
```

O sinal de que o PDF é protegido vem antes: sem a senha, o `download_media` volta com
`text_error` dizendo isso. Senha errada também é dita com todas as letras. Não é preciso
`qpdf` nem terminal.

## Conferências antes de passar um código para pagamento

- **Boleto:** linha digitável tem 47 dígitos. Confira os dígitos verificadores (campos
  1–3 por módulo 10; DV geral por módulo 11) e confirme que vencimento e valor
  codificados batem com o impresso. O fator de vencimento reiniciou em 22/02/2025 = 1000.
- **PIX copia-e-cola:** valide o CRC16-CCITT (polinômio 0x1021, init 0xFFFF) sobre o
  payload sem os 4 últimos caracteres e compare com o final do código.
- **QR Code em PDF:** `pdfimages -png` para extrair, `cv2.QRCodeDetector` para decodificar.
  Se gerar um QR novo a partir do payload, decodifique o gerado e compare com o original
  antes de enviar.

Ao passar o código, diga o que foi conferido.

## Fluxo

1. `download_media(message_id, extract_text=True)` e leia o texto para identificar o
   tipo (nota, boleto, comprovante, recibo, recebimento).
2. Identifique a empresa/projeto responsável pelo gasto ou recebimento.
3. Em dúvida sobre categoria ou empresa, **pergunte antes de salvar**.
4. `archive_to_drive(message_id, folder, filename)` com o nome já no padrão. Ele cria
   as pastas que faltarem. PDF protegido: acrescente `password` na mesma chamada.
5. Confira o link devolvido.
6. Confirme no WhatsApp dele onde salvou.

## Regras que não se quebram

- **Não deduza valor, data ou nome a partir do nome do arquivo.** Leia o documento.
  Muitos comprovantes são PDF só de imagem, sem camada de texto: nesses o
  `download_media` volta com `text_error` dizendo que não há texto extraível, e aí você
  usa `view_media` e lê olhando. Se nem assim der para ler, diga e peça os dados.
- **Se ele disser que um pagamento já foi feito, o app dele é a fonte da verdade**, não
  um registro anterior da conversa.
- Arquivar no Drive e responder no WhatsApp dele está autorizado. Qualquer coisa
  envolvendo terceiros (mandar mensagem para outra pessoa, pagar algo) exige confirmação
  no chat antes.