# Kanban Automatizado — Como Funciona

## O que e o Kanban?

O Kanban e um painel visual que mostra em qual etapa cada lead/cliente esta no atendimento. Ele e atualizado **automaticamente pela IA** — voce nao precisa mover nada manualmente.

Toda vez que a IA interage com um cliente, ela move o card para a etapa correta. Assim voce tem visao em tempo real de todos os atendimentos.

---

## Funis criados

### 1. Atendimento
Fluxo completo — do lead ao financeiro.

| Etapa | O que significa | Automacao |
|-------|----------------|-----------|
| **SDR** | IA esta qualificando o lead (triagem, coleta de dados, avaliacao) | Automatico — IA cria o card aqui ao receber a primeira mensagem |
| **COMERCIAL-CLOSE** | Lead qualificado e agendado. Advogado fecha o contrato | Automatico — IA move ao agendar/converter |
| **CONTRATO** | Assinatura do contrato com o cliente | Manual |
| **ENTREVISTA** | Consulta/entrevista com o advogado (videochamada) | Manual |
| **PASTA-DOCUMENTACAO** | Coleta de documentos do caso | Manual |
| **INICIAL-PROTOCOLO** | Peticao inicial protocolada no tribunal | Manual |
| **AUD1-TESTE ZOOM** | 1a audiencia — teste de conexao do Zoom | Manual |
| **AUD2-TESTEMUNHA** | 2a audiencia — oitiva de testemunhas | Manual |
| **ACORDO E RELACIONAMENTO** | Negociacao de acordo / pos-audiencia | Manual |
| **ANDAMENTO PROCESSUAL** | Caso em andamento no tribunal | Manual |
| **FINANCEIRO-OUTROS** | Pagamento de honorarios, encerramento | Manual |

### 2. Triagem / Encaminhamento
Casos que sairam do fluxo principal.

| Etapa | O que significa | Automacao |
|-------|----------------|-----------|
| **Transferido** | IA transferiu para atendimento humano (caso complexo, cliente existente, pediu advogado, etc) | Automatico |
| **Inviavel** | Caso avaliado como inviavel (sem sequela, sem laudo, fora do prazo) | Automatico |
| **Desqualificado** | Sem interesse, fora do escopo juridico, nao e potencial cliente | Automatico |
| **Nao Alfabetizado** | Cliente com dificuldade para ler/escrever, transferido para humano | Automatico |
| **Resolvido** | Caso encerrado/concluido | Manual |

---

## Como funciona na pratica

1. **Cliente envia mensagem** → IA cria card em "SDR" automaticamente
2. **IA qualifica e agenda** → Card move para "COMERCIAL-CLOSE"
3. **Advogado fecha contrato** → Equipe move para "CONTRATO" (manual)
4. **Caso segue o fluxo** → Equipe move pelas etapas ate "FINANCEIRO-OUTROS"

Se em qualquer momento:
- IA transfere → Card vai para "Transferido" (automatico)
- Caso inviavel → Card vai para "Inviavel" (automatico)
- Sem interesse → Card vai para "Desqualificado" (automatico)

---

## Importante

- **SDR e COMERCIAL-CLOSE sao automaticos** — a IA move os cards
- **CONTRATO em diante sao manuais** — a equipe do escritorio move conforme o caso avanca
- O Kanban fica dentro do Chatwoot, na aba de Funis
- Cada conta tem seus proprios funis independentes
- Novos contas criadas ja recebem os 2 funis automaticamente
