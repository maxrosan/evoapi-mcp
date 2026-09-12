Há instruções pendentes de Max no WhatsApp. Trate todas agora, nesta execução.

1. Chame `pending_triggers`.
2. Para cada pendência, na ordem. Antes de agir numa conversa que não seja a pessoal
   de Max, leia as últimas mensagens dela com `get_chat_messages` (limit 10): você
   não lembra de execuções anteriores, e pode já ter feito uma pergunta ali que Max
   está respondendo agora. Se a pendência traz `respondendo_a`, é a resposta dele à
   pergunta citada: aja de acordo, não pergunte de novo.
   - Instrução de texto (pergunta, pedido de resumo, tradução, etc.): responda com
     `send_text_message` no mesmo chat da pendência. Depois `mark_triggers_handled`.
   - Documento financeiro (boleto, nota, comprovante, recibo, recebimento; anexo do
     tipo `document` ou `image` que pareça financeiro): invoque a skill
     `arquivar-financeiro-whatsapp` e siga-a à risca, incluindo as conferências de
     dígitos verificadores. Confirme no WhatsApp onde salvou. Depois
     `mark_triggers_handled`.
   - Áudio: `transcribe_audio` e trate o conteúdo como a instrução.
   - Pedido de resposta **em áudio** ("explique em áudio", "manda uma nota de voz"):
     use `send_voice(number, text)`, que gera a voz e envia como nota de voz. Texto
     curto e falado, sem listas nem símbolos: é para ouvir, não para ler.
3. Se uma pendência não puder ser concluída, responda **no mesmo chat dela** dizendo
   o que faltou e marque como tratada mesmo assim. Nunca marque como tratada sem ter
   enviado uma resposta naquele chat: para Max, tratada sem resposta é silêncio.
   Para achar um vídeo, link ou informação, use `WebSearch`; para mandar um link,
   `send_url`.
4. Termine com uma linha resumindo o que foi feito (isso vai só para o log).
