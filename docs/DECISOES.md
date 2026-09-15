# MINHOCA DE JARDIM — DECISÕES

## 14/09/2026 — Estrutura de documentação

Foi criada a estrutura oficial de documentação do projeto:

- MASTER.md
- DECISOES.md
- PENDENCIAS.md
- CHANGELOG.md
- COPY.md
- MARKETING.md
- METRICAS.md

Objetivo: manter uma fonte de verdade do projeto e evitar perda de contexto entre os diferentes chats/departamentos.

---

## 14/09/2026 — Cérebro Central

Este chat é o Cérebro Central do projeto.

Responsabilidades:

- arquitetura;
- decisões técnicas;
- auditoria;
- integração entre departamentos;
- priorização;
- controle de alterações;
- decisão final sobre mudanças estruturais.

---

## 14/09/2026 — Separação de departamentos

Engenharia:

Responsável pela implementação técnica.

UX/Copy:

Responsável pela experiência visual, copy e conversão.

Marketing:

Responsável por aquisição, divulgação e crescimento.

O Cérebro Central integra as decisões.

---

## 14/09/2026 — Layout Visual V1

O Layout Visual Final V1 foi aprovado.

Direção:

- dark SaaS;
- mobile-first;
- minimalista;
- moderno;
- fundo escuro;
- cards escuros;
- ações em verde/esmeralda/turquesa;
- logo/minhoca branca;
- sem redesign adicional neste momento.

O Lovable é apenas referência visual.

O código real continua sendo a fonte de verdade.

---

## 14/09/2026 — Preços oficiais

Semanal:

R$ 9,90

Mensal:

R$ 16,90

VIP Batch:

R$ 29,90

Não alterar preços sem nova decisão registrada aqui.

---

## 14/09/2026 — Modelo de pagamento

Cartão:

assinatura recorrente.

Pix:

pagamento único para o período contratado.

Não tratar Pix como renovação automática.

---

## 14/09/2026 — Plano semanal Mercado Pago

A frequência semanal correta é:

`frequency = 7`

`frequency_type = days`

O Mercado Pago não aceita `weeks` nesse contexto.

---

## 14/09/2026 — Processamento de vídeo

O processamento do MP4 utiliza FFmpeg com reencode para H.264/AAC, buscando maior compatibilidade entre dispositivos.

O sistema não promete evasão de detecção das plataformas.

---

## 14/09/2026 — Usuário pago

Usuários com plano pago não devem continuar vendo a seção de planos como se ainda precisassem contratar.

A interface deve ocultar a seção de planos quando o usuário já possui plano pago.

---

## 14/09/2026 — Deploy

Nenhuma alteração atual deve ser enviada para GitHub ou Render antes dos testes locais.

---

## REGRA

Toda decisão estrutural nova deve ser adicionada neste arquivo com:

- data;
- assunto;
- decisão;
- motivo quando necessário.