Há instruções pendentes de Max no WhatsApp. Trate todas agora, nesta execução.

1. Chame `pending_triggers`.
2. Para cada pendência, na ordem:
   - Instrução de texto (pergunta, pedido de resumo, tradução, etc.): responda com
     `send_text_message` no mesmo chat da pendência. Depois `mark_triggers_handled`.
   - Documento financeiro (boleto, nota, comprovante, recibo, recebimento; anexo do
     tipo `document` ou `image` que pareça financeiro): invoque a skill
     `arquivar-financeiro-whatsapp` e siga-a à risca, incluindo as conferências de
     dígitos verificadores. Confirme no WhatsApp onde salvou. Depois
     `mark_triggers_handled`.
   - Áudio: `transcribe_audio` e trate o conteúdo como a instrução.
3. Se uma pendência não puder ser concluída, responda a Max dizendo o que faltou e
   marque como tratada mesmo assim. Não deixe pendência sem resposta.
4. Termine com uma linha resumindo o que foi feito (isso vai só para o log).
