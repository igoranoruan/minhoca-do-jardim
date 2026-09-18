# Minhoca de Jardim — Auditoria Final

Base preparada para substituição/deploy.

Mudanças principais:
- Free: 5 gerações por semana (segunda a domingo, horário de São Paulo).
- Checkout de cartão: pagamento único via Mercado Pago Checkout Pro; não cria recorrência.
- Pix: pagamento único.
- Webhook: validação de assinatura obrigatória em produção e proteção contra replay.
- Correlação de pagamentos por external_reference.
- Revogação de acesso em reembolso/chargeback da transação ativa.
- Quota paga com reset diário no horário de São Paulo.
- Proteção SSRF para URLs de arquivos externos com validação DNS/IP.
- PO Token/bgutil 2.0.0 fixado e inicialização do provider validada antes da API.
- Fallback Cobalt público removido; só é usado se COBALT_API_URLS for configurado com uma instância autorizada.
- Tratamento de erros de download preserva múltiplas tentativas.

Variáveis obrigatórias para produção:
- MP_ACCESS_TOKEN
- MP_WEBHOOK_SECRET
- PUBLIC_BASE_URL

Recomendadas:
- ADMIN_EMAILS
- BGUTIL_POT_BASE_URL
- ENVIRONMENT=production
- COBALT_API_URLS (somente se houver instância autorizada)
