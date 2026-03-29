---
name: prompt-gap-analyzer
description: Analisa prompts e workflows para encontrar lacunas implicitas, contradicoes entre arquivos, fluxos incompletos e comportamentos forcados em contexto errado. Use sempre que o usuario pedir para revisar, auditar, melhorar ou corrigir um prompt de atendimento, chatbot ou assistente IA. Tambem use quando o usuario mencionar "lacunas", "o que falta no prompt", "prompt nao cobre", "IA alucina sobre X", "contradição", ou quando estiver criando/editando prompts de clientes.
---

# Prompt Gap Analyzer — Analisador de Lacunas Implicitas

Toda afirmacao positiva em um prompt cria uma zona de exclusao implicita. Se o prompt diz "aceitamos PIX e dinheiro", ele silenciosamente exclui cartao, boleto, cheque e qualquer outro meio de pagamento. Quando a IA recebe uma pergunta sobre cartao, ela nao tem instrucao explicita e pode alucinar uma resposta.

Esta skill encontra e classifica 5 tipos de problema em prompts:

1. **Negacoes implicitas** — o que o prompt afirma cria exclusoes silenciosas
2. **Contradicoes entre arquivos** — um arquivo diz X, outro diz o oposto
3. **Categorias ausentes** — assuntos inteiros que o prompt simplesmente ignora
4. **Fluxos incompletos** — listas de opcoes que nao cobrem cenarios reais comuns
5. **Comportamentos forcados em contexto errado** — regras que funcionam na maioria dos casos mas falham em situacoes especificas

## Principio Central: Afirmacao Positiva → Negacao Implicita

Cada frase afirmativa no prompt define um conjunto fechado. Tudo fora desse conjunto e uma exclusao implicita que precisa ser tratada. A IA precisa saber o que fazer quando perguntada sobre algo fora do conjunto.

**Exemplo concreto:**
- Prompt diz: "Atendemos direito previdenciario"
- Conjunto definido: {direito previdenciario}
- Exclusoes implicitas: direito trabalhista, criminal, familia, tributario, consumidor, etc.
- Lacuna: se alguem perguntar sobre divorcio, a IA nao tem instrucao do que responder

## Como Executar a Analise

### Passo 1: Ler TODOS os arquivos do prompt

Leia todo o conteudo do prompt/workflow antes de comecar — todos os arquivos, incluindo supervisor, base, e cada fase/agente. A analise so funciona com visao completa. Entenda o contexto: que tipo de negocio e, quem e o publico, qual o objetivo do atendimento.

### Passo 2: Verificar contradicoes entre arquivos

Antes de analisar lacunas, cruze as instrucoes de todos os arquivos procurando:
- Um arquivo proibe algo que outro arquivo usa (ex: base proibe "videochamada" mas outro arquivo usa exatamente esse termo)
- Regras que se sobrepõem de forma ambigua (ex: dois agentes com instrucoes diferentes para o mesmo cenario)
- Tools mencionadas em um arquivo mas nao disponiveis naquela fase

Contradicoes sao sempre CRITICAS porque a IA recebe instrucoes conflitantes e o comportamento fica imprevisivel.

### Passo 3: Extrair todas as afirmacoes positivas

Identifique cada frase que define algo concreto. Categorize por tipo:

| Categoria | O que procurar | Exemplo |
|-----------|---------------|---------|
| **Servicos/Areas** | Especialidades, tipos de atendimento | "Atuamos em direito previdenciario" |
| **Pagamento** | Formas aceitas, parcelamento, valores | "Aceitamos PIX e dinheiro" |
| **Horario** | Dias e horas de funcionamento | "Atendemos de segunda a sexta" |
| **Localizacao** | Onde atende, presencial/remoto | "Escritorio em Sao Paulo" |
| **Publico** | Quem atende, restricoes | "Atendemos pessoas fisicas" |
| **Convenios/Planos** | Planos aceitos, parcerias | "Aceitamos plano Unimed" |
| **Canais** | Como o cliente pode contatar | "Atendimento por WhatsApp" |
| **Processos** | Etapas, fluxos, documentos | "Primeiro agendamos uma consulta" |
| **Politicas** | Regras de cancelamento, garantias | "Consulta inicial gratuita" |
| **Honorarios/Valores** | Percentuais, custos, condicoes | "Honorarios de 30%" |

### Passo 4: Para cada afirmacao, derivar as exclusoes implicitas

Para cada item encontrado no Passo 3, pergunte: "O que fica de fora?"

Pense nas perguntas mais comuns que um cliente faria sobre alternativas:
- Se aceita X, aceita Y? (pagamento)
- Se atende area X, atende area Y? (servicos)
- Se funciona no horario X, funciona no horario Y? (horario)
- Se atende na cidade X, atende na cidade Y? (localizacao)

### Passo 5: Identificar categorias inteiramente ausentes

Alem das exclusoes dentro de categorias mencionadas, verifique se ha categorias inteiras que o prompt simplesmente ignora. Exemplos comuns:

- Prompt de escritorio de advocacia que nao menciona formas de pagamento dos honorarios
- Prompt de clinica que nao menciona horario de funcionamento
- Prompt de loja que nao menciona politica de troca/devolucao
- Prompt que tem valores/percentuais mas nao explica condicoes (sobre bruto ou liquido? inclui recurso?)

Uma categoria ausente e diferente de uma exclusao implicita: na exclusao, o prompt fala do assunto mas nao cobre tudo; na ausencia, o prompt nao toca no assunto de jeito nenhum.

### Passo 6: Verificar fluxos e listas incompletas

Quando o prompt define fluxos numerados ou listas de opcoes, verifique se cobrem os cenarios reais mais comuns do dominio.

Exemplo: um escritorio trabalhista que so cobre 3 tipos de caso (sem carteira, rescisao indireta, diferencas de verbas) mas ignora acidente de trabalho, assedio moral, horas extras, desvio de funcao, insalubridade, pejotizacao — todos casos trabalhistas comuns que clientes vao perguntar.

Nao exija cobertura de 100% dos cenarios possiveis, mas verifique se os cenarios mais frequentes estao cobertos. Use conhecimento do dominio para identificar os top 10 cenarios mais comuns daquele tipo de negocio.

### Passo 7: Verificar comportamentos forcados em contexto errado

Regras absolutas ("SEMPRE faca X ao encerrar") frequentemente falham em contextos especificos. Exemplos:

- "Sempre pedir avaliacao 5 estrelas ao encerrar" — mas e se o caso foi inviavel? Pedir 5 estrelas para quem acabou de ouvir "nao podemos ajudar" e inadequado
- "Sempre insistir no agendamento" — mas e se o cliente esta furioso?
- "Nunca encerrar sem converter" — mas e se e um fornecedor, nao um cliente?

Verifique cada regra absoluta e identifique contextos onde ela produz comportamento indesejado.

### Passo 8: Verificar termos vagos sem definicao

Identifique termos subjetivos que a IA pode interpretar de formas diferentes:
- "em breve", "rapidamente", "em pouco tempo" — quanto tempo exatamente?
- "especialista" — quem especificamente?
- "analise mais aprofundada" — o que isso significa na pratica para o cliente?

### Passo 9: Gerar o Relatorio de Lacunas

Apresente os resultados neste formato:

```
## Relatorio de Lacunas — [Nome do Cliente]

**Resumo:** X CRITICAS | Y ALTAS | Z MEDIAS | W BAIXAS

---

### [NIVEL] [Numero] — [Titulo curto]

**Tipo:** [Negacao implicita | Contradicao | Categoria ausente | Fluxo incompleto | Comportamento forcado | Termo vago]

**O que o prompt diz:**
> [citacao exata do prompt, com nome do arquivo e linha]

**Problema:**
[Descricao clara do problema]

**Perguntas que um cliente pode fazer e a IA nao saberia responder:**
1. [pergunta provavel 1]
2. [pergunta provavel 2]

**Sugestao de correcao:**
> [texto sugerido para adicionar ao prompt]

---
```

### Passo 10: Propor correcoes diretas

Para cada lacuna, sugira texto concreto. Use o padrao mais adequado:

**Padrao de Exclusao Explicita** (quando a lista de exclusoes e pequena):
```
Aceitamos APENAS PIX e dinheiro. NAO aceitamos cartao de credito, cartao de debito, boleto bancario ou cheque. Se o cliente perguntar sobre outras formas de pagamento, informe educadamente que trabalhamos apenas com PIX e dinheiro.
```

**Padrao de Redirecionamento** (quando as alternativas sao muitas):
```
Nossa especialidade e direito previdenciario. Se o cliente perguntar sobre outras areas do direito (trabalhista, criminal, familia, etc.), informe que nao atuamos nessa area e sugira que procure um advogado especializado.
```

**Padrao de Pergunta ao Dono** (quando voce nao sabe a resposta):
```
⚠️ PRECISO DA SUA RESPOSTA: O prompt menciona honorarios de 35%, mas nao diz:
- Sobre bruto ou liquido?
- Inclui fase de recurso?
- Ha custos adicionais (pericia, custas)?
→ Me informe para eu completar o prompt.
```

Use o terceiro padrao sempre que nao souber a resposta correta. A skill nunca deve inventar informacoes para preencher lacunas — deve perguntar ao dono do prompt.

## Categorias de Risco

Classifique cada lacuna por nivel de risco:

- **CRITICO** — A IA pode dar informacao errada que causa prejuizo real (ex: afirmar que faz um servico que nao faz, contradicoes entre arquivos que geram comportamento imprevisivel)
- **ALTO** — A IA pode criar expectativa falsa ou perder um lead valido (ex: nao ter fluxo para um tipo de caso comum, nao saber responder sobre pagamento)
- **MEDIO** — A IA pode confundir o cliente mas sem grande prejuizo (ex: nao saber informar horario, termo vago)
- **BAIXO** — Informacao complementar que seria util ter (ex: redes sociais, estacionamento)

Priorize as correcoes de CRITICO para BAIXO.

## Armadilhas Comuns

Fique atento a estes padroes que frequentemente geram lacunas:

1. **Listas incompletas**: "Documentos necessarios: RG e CPF" — e comprovante de residencia? E certidoes?
2. **Horarios sem excecoes**: "Atendemos das 9h as 18h" — e feriados? E sabado?
3. **Valores sem condicoes**: "Consulta por R$200" — e retorno? E urgencia? Sobre bruto ou liquido?
4. **Etapas sem alternativas**: "Agende pelo WhatsApp" — e se o cliente nao tem WhatsApp?
5. **Promessas sem limites**: "Respondemos rapidamente" — quanto tempo? E fora do horario?
6. **Areas sem fronteiras**: "Advocacia previdenciaria" — inclui BPC/LOAS? Aposentadoria rural? Revisao?
7. **Regras absolutas sem excecoes**: "SEMPRE peca avaliacao" — mesmo quando o caso e inviavel?
8. **Termos proibidos usados em outro arquivo**: base proibe "videochamada" mas outro agente usa
9. **Transferencia silenciosa**: transfere para humano sem explicar ao cliente por que esta transferindo
10. **Informacoes fisicas vs digitais contraditórias**: "100% online" mas lista enderecos fisicos

## Modo de Interacao

Ao apresentar os resultados ao usuario:

1. Comece com um resumo quantitativo: quantas lacunas por categoria de risco
2. Apresente as CRITICAS primeiro — essas precisam de correcao imediata
3. Para cada lacuna, mostre a correcao sugerida ja pronta para copiar/colar
4. Separe claramente: correcoes que voce pode aplicar vs perguntas que precisa do dono
5. Pergunte ao usuario se quer aplicar as correcoes automaticamente no prompt
6. Se o usuario confirmar, aplique as correcoes mantendo o estilo e tom do prompt original

## Importante

- NAO invente informacoes. Se voce nao sabe a resposta correta para a lacuna (ex: quais formas de pagamento o escritorio aceita), use o Padrao de Pergunta ao Dono
- As sugestoes de correcao devem manter o tom e estilo do prompt original
- Foque nas lacunas que realmente importam — nao gere dezenas de lacunas triviais
- Quando a lista de exclusoes possiveis e muito grande, use o padrao de redirecionamento
- Leia TODOS os arquivos do prompt antes de comecar — contradicoes entre arquivos so aparecem com visao completa
- Ao citar trechos do prompt, inclua o nome do arquivo e numero da linha para facilitar a correcao
