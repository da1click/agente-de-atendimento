# Cobrança de Documentos (Diana) — Campos Carvalho

Este prompt NÃO é executado pela IA diretamente. O texto é enviado pelo
módulo `cobranca_documentos.py` (loop background) quando:

- O humano adiciona a label `cobrar-documentos` na conversa no Chatwoot.
- O loop detecta conversas com essa label e envia mensagens periódicas
  cobrando os documentos do cliente.

Ficando aqui como referência (o cliente pode editar os textos nesta
pasta e o time de engenharia replica em `cobranca_documentos.py`).

---

## DOCUMENTOS COBRADOS (exemplo — ajustar conforme demanda)

- Carteira de Trabalho Digital (PDF)
- Extrato do FGTS (PDF)

---

## INTERVALO E LIMITE

- Primeira cobrança: ~1 minuto após o humano adicionar a label (dá tempo
  de cancelar se acionou por engano).
- Intervalo entre cobranças: **12h**.
- Horário comercial: **8h–19h** (BRT). Fora disso, nada é enviado.
- Limite padrão: **5 tentativas**. Depois disso a cobrança desativa
  sozinha (motivo: `limite_atingido`).

---

## MENSAGENS VARIANTES (alternam a cada tentativa)

### Variante 1
"Oi, {nome}! Passando pra reforçar que precisamos da sua Carteira de
Trabalho Digital e do Extrato do FGTS em PDF pra avançar com a análise
do seu caso. Consegue me mandar aqui?"

### Variante 2
"Oi, tudo bem? Ainda estou aguardando sua Carteira de Trabalho Digital
e o Extrato do FGTS em PDF. Assim que receber, já encaminho pro advogado
conferir. Consegue enviar hoje?"

### Variante 3
"{nome}, tudo certo por aí? Sem a Carteira de Trabalho Digital e o
Extrato do FGTS a análise não avança. Me manda aqui quando puder, tá?"

---

## DESATIVAÇÃO AUTOMÁTICA

A cobrança para automaticamente quando:

1. **Humano remove a label** `cobrar-documentos` → motivo `label_removida`.
2. **Cliente envia anexo** (PDF ou imagem) → motivo `anexo_recebido`.
   A label também é removida automaticamente.
3. **Limite atingido** (5 tentativas sem resposta com anexo) → motivo
   `limite_atingido`.

---

## COMO OPERAR (para o time do escritório)

1. Abrir a conversa no Chatwoot.
2. Adicionar a label `cobrar-documentos`.
3. Esperar. A Diana envia a primeira mensagem em 1 minuto e continua
   cobrando a cada 12h (dentro do horário comercial), até 5 tentativas.
4. Para parar antes: remover a label.
5. Quando o cliente enviar os documentos, a label é removida sozinha.
