# MINHOCA DE JARDIM — PENDÊNCIAS

## PRIORIDADE CRÍTICA

### 1. Executar validação local
Status: PENDENTE

Executar:

python -m pip install -r requirements.txt
python -m py_compile main.py database.py models.py
python -c "from database import init_db; init_db(); print('BANCO OK')"
python -c "import models; print('MODELS OK')"
python -c "from database import engine; from sqlalchemy import inspect; print(inspect(engine).get_table_names())"

---

### 2. Testar TikTok
Status: PENDENTE

---

### 3. Testar Instagram
Status: PENDENTE

Observação:

Testes anteriores apresentaram HTTP 429.

Necessário verificar comportamento após as novas alterações.

---

### 4. Testar Pinterest
Status: PENDENTE

---

### 5. Testar YouTube Shorts
Status: PENDENTE

Observação:

Testes anteriores apresentaram bloqueio relacionado a confirmação de bot.

---

### 6. Testar MP4 em celular
Status: PENDENTE

Verificar principalmente compatibilidade com iPhone/Arquivos.

---

### 7. Testar Pix
Status: PENDENTE

---

### 8. Testar cartão
Status: PENDENTE

---

### 9. Testar plano semanal
Status: PENDENTE

Confirmar:

- R$ 9,90;
- 7 dias;
- 10 downloads/dia;
- criação correta da assinatura.

---

### 10. Testar usuário pago
Status: PENDENTE

Confirmar:

- acesso liberado;
- limite correto;
- planos ocultos na interface.

---

### 11. Testar usuário gratuito
Status: PENDENTE

Confirmar:

- acesso gratuito;
- limite correto;
- planos visíveis.

---

## PRIORIDADE ALTA

### 12. Revisar frontend conforme Layout V1
Status: PENDENTE

O layout aprovado deve ser transportado para o frontend real depois que as correções funcionais estiverem estáveis.

---

### 13. Revisar segurança
Status: PENDENTE

Verificar:

- `.env`;
- secrets;
- webhook;
- GitHub;
- exposição de credenciais.

Existe histórico de secret do Mercado Pago que foi exposto anteriormente e deve ser considerado para rotação antes da comercialização, caso ainda não tenha sido rotacionado.

---

### 14. PostgreSQL no Render
Status: PENDENTE

Antes da comercialização, o banco de produção precisa utilizar armazenamento persistente.

SQLite em serviço Render sem persistência não deve ser considerado solução definitiva.

---

### 15. Auditoria final
Status: PENDENTE

Depois dos testes:

- revisar todos os arquivos;
- revisar banco;
- revisar pagamentos;
- revisar download;
- revisar frontend;
- revisar segurança;
- revisar Git;
- revisar Render.

---

## NÃO FAZER AGORA

- Não fazer deploy.
- Não fazer push.
- Não alterar preços.
- Não adicionar novos planos.
- Não adicionar novas seções ao layout.
- Não recriar o projeto pelo Lovable.
- Não alterar backend apenas por estética.
- Não prometer bypass de plataformas.