# Instruções para Claude Code

## Push automático
Quando o usuário disser "faça um push", "faz um push", "push" ou similar, executar automaticamente:
1. `git add` nos arquivos modificados (nunca incluir `.env`)
2. `git commit` com mensagem descritiva das mudanças
3. `git push origin master`

Não pedir confirmação — executar direto.

## Protocolo de Sessão

### Início de sessão (SEMPRE executar ao iniciar conversa)
1. `git pull origin master` — sincronizar com o remoto
2. Informar ao usuário o que foi atualizado

### Durante a sessão
- **Após cada tarefa complexa**: fazer push imediatamente (não acumular)
- **Sempre testar o projeto** após mudanças (rodar e verificar que funciona)
- **Ao atingir ~70% do contexto**: executar /compact automaticamente

### Fim de sessão
- Garantir que todas as mudanças foram commitadas e pushadas
- Nunca deixar trabalho sem push
