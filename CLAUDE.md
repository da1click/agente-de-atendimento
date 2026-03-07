# Instruções para Claude Code

## Push automático
Quando o usuário disser "faça um push", "faz um push", "push" ou similar, executar automaticamente:
1. `git add` nos arquivos modificados (nunca incluir `.env`)
2. `git commit` com mensagem descritiva das mudanças
3. `git push origin master`

Não pedir confirmação — executar direto.
