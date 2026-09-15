# MINHOCA DE JARDIM — MASTER

## 1. O QUE É O PROJETO

Minhoca de Jardim é um SaaS brasileiro para download e processamento de vídeos.

Plataformas-alvo:

- TikTok
- Instagram Reels
- YouTube Shorts
- Pinterest

O produto permite ao usuário colar um link, processar o vídeo e baixar um arquivo MP4 preparado para reutilização e edição.

---

## 2. OBJETIVO DO PRODUTO

O objetivo é oferecer uma ferramenta simples, rápida e acessível para pessoas que trabalham com conteúdo e precisam baixar e preparar vídeos para edição e reutilização.

Fluxo principal:

1. Usuário acessa o Minhoca de Jardim.
2. Cola o link do vídeo.
3. Clica em "Processar e baixar".
4. O sistema tenta obter o vídeo.
5. O sistema processa o arquivo.
6. O sistema remove metadados.
7. O sistema gera uma nova versão MP4.
8. O usuário baixa o arquivo.
9. O usuário pode levar o arquivo para seu editor e adaptá-lo.

---

## 3. POSICIONAMENTO

O Minhoca de Jardim deve ser percebido como:

- simples;
- rápido;
- direto;
- acessível;
- moderno;
- útil;
- confiável.

Não deve parecer:

- ferramenta amadora;
- projeto experimental;
- site cheio de anúncios;
- promessa milagrosa;
- ferramenta de "hack";
- ferramenta que garante burlar sistemas das plataformas.

---

## 4. TECNOLOGIA

Backend:

- Python
- FastAPI
- SQLAlchemy
- yt-dlp
- FFmpeg

Banco:

- SQLite para desenvolvimento/local;
- PostgreSQL para produção.

Pagamentos:

- Mercado Pago.

Hospedagem:

- Render.

Código:

- GitHub.

Editor:

- VS Code.

---

## 5. ARQUITETURA ATUAL

Principais arquivos:

- `main.py` — aplicação principal, APIs, downloads, processamento, pagamentos e regras de negócio.
- `database.py` — conexão e inicialização do banco.
- `models.py` — modelos do banco.
- `add_vip.py` — ferramenta administrativa existente.
- `requirements.txt` — dependências Python.
- `Dockerfile` — configuração de execução/container.
- `static/index.html` — frontend principal.
- `static/logo.png` — logo.
- `static/favicon.png` — favicon.
- `static/banner.png` — banner.

---

## 6. PLANOS

### Semanal

Preço:

R$ 9,90

Duração:

7 dias

Limite:

10 downloads por dia

Recursos:

- limpeza de metadados;
- processamento de vídeo.

### Mensal

Preço:

R$ 16,90

Duração:

30 dias

Limite:

30 downloads por dia

Recursos:

- limpeza de metadados;
- processamento de vídeo.

### VIP Batch

Preço:

R$ 29,90

Duração:

30 dias

Limite:

downloads ilimitados

Recursos:

- downloads ilimitados;
- processamento em lote;
- limpeza de metadados.

---

## 7. PAGAMENTOS

Mercado Pago é o provedor de pagamentos.

Cartão:

- assinatura recorrente.

Pix:

- pagamento único;
- libera o período contratado;
- não é tratado como renovação automática.

A criação de assinaturas recorrentes utiliza Mercado Pago Preapproval.

O plano semanal deve utilizar:

- `frequency = 7`
- `frequency_type = days`

Nunca utilizar `weeks`, pois o Mercado Pago não aceita esse tipo de frequência nesse endpoint.

---

## 8. REGRAS DE ACESSO

Usuários gratuitos possuem limite diário.

Usuários pagos possuem limite de acordo com o plano.

Usuários administrativos possuem acesso administrativo conforme configuração do sistema.

Quando o usuário possui plano pago ativo, a interface não deve continuar apresentando a seção de planos como se ele ainda precisasse contratar.

---

## 9. DOWNLOAD E PROCESSAMENTO

O sistema utiliza diferentes estratégias/fallbacks para tentar obter vídeos.

A arquitetura possui integração com:

- yt-dlp;
- TikWM para fallback de TikTok;
- Cobalt como fallback;
- download direto quando aplicável.

O sistema não deve prometer que todas as URLs de todas as plataformas funcionarão.

Plataformas podem alterar suas políticas, endpoints, autenticação, rate limits ou mecanismos anti-bot.

---

## 10. PROCESSAMENTO DO MP4

O processamento atual utiliza FFmpeg para gerar uma nova versão compatível do vídeo.

Configuração atual:

- vídeo H.264;
- áudio AAC;
- pixel format `yuv420p`;
- `faststart`;
- remoção de metadados;
- preservação de resolução e proporção.

O objetivo é aumentar a compatibilidade do arquivo em navegadores, celulares e aplicativos de edição.

O sistema não deve prometer que o processamento torna o vídeo "impossível de identificar" pelas plataformas.

---

## 11. FRONTEND

O frontend atual está sendo migrado visualmente para o Layout Visual Final V1.

Direção:

- fundo escuro;
- cards escuros;
- ações em verde/esmeralda/turquesa;
- texto principal branco;
- texto secundário azul/cinza;
- bordas discretas;
- cantos arredondados;
- visual SaaS moderno;
- prioridade mobile-first.

O frontend real é a fonte de verdade.

Qualquer projeto feito no Lovable é apenas referência visual.

O projeto não deve ser recriado a partir do Lovable.

---

## 12. LAYOUT VISUAL V1 APROVADO

Eyebrow:

FERRAMENTA PARA REUTILIZAÇÃO DE CONTEÚDO

Headline:

Reutilize o conteúdo que já funcionou.

Subheadline:

Baixe vídeos do TikTok, Instagram Reels, YouTube Shorts e Pinterest. Prepare uma nova versão em MP4 e leve o arquivo para editar e adaptar.

CTA principal:

Processar e baixar

Placeholder:

Cole o link do vídeo…

Benefícios:

- Limpeza de metadados
- Nova versão em MP4
- Sem anúncios

Seção de processo:

Do link ao seu editor, sem complicação.

Etapas:

1. Rápido
   Cole o link e processe direto no aplicativo.

2. Arquivo preparado
   Limpeza de metadados e processamento para gerar uma nova versão em MP4.

3. Pronto para reutilizar
   Baixe o arquivo e leve para seu editor para adaptar ao seu próprio formato.

Card de edição:

Vai editar antes de publicar?

O usuário deve ser incentivado a adaptar o arquivo antes de publicar.

---

## 13. FAQ

Perguntas aprovadas:

- O que o Minhoca de Jardim faz?
- Quais plataformas são compatíveis?
- Como funciona o processamento?
- O Pix renova automaticamente?
- E o cartão?
- O Minhoca garante que meu conteúdo não será identificado pelas plataformas?

A resposta da última pergunta nunca deve criar uma garantia técnica falsa.

---

## 14. PAGAMENTO NO FRONTEND

Card:

Pagamento protegido pelo Mercado Pago

Texto:

Seus dados de pagamento são processados em ambiente seguro.

---

## 15. RODAPÉ

Minhoca de Jardim

Baixe. Prepare. Adapte. Publique.

© 2026 Minhoca de Jardim

---

## 16. PRINCÍPIOS TÉCNICOS

Antes de alterar qualquer código:

1. Entender o código atual.
2. Preservar funcionalidades existentes.
3. Fazer uma alteração por vez.
4. Validar sintaxe.
5. Testar localmente.
6. Testar a funcionalidade afetada.
7. Só depois considerar GitHub.
8. Só depois considerar Render.

Nunca alterar várias partes críticas simultaneamente sem necessidade.

---

## 17. PRINCÍPIOS DE SEGURANÇA

Nunca colocar:

- tokens;
- senhas;
- secrets;
- credenciais;

dentro de arquivos públicos ou commits.

Segredos devem permanecer em variáveis de ambiente.

O `.env` não deve ser enviado para o GitHub.

---

## 18. ORDEM DE PRIORIDADE

Prioridade máxima:

1. Produto funcionar.
2. Pagamento funcionar.
3. Download funcionar.
4. Processamento funcionar.
5. Banco funcionar.
6. Segurança funcionar.
7. Interface funcionar bem.
8. Métricas funcionarem.
9. Marketing e aquisição.
10. Melhorias futuras.

Não sacrificar funcionamento por estética.

---

## 19. ESTADO ATUAL EM 14/09/2026

O projeto está em fase de correção e preparação para comercialização.

Foram identificados e preparados ajustes importantes:

- correção da frequência do plano semanal do Mercado Pago;
- melhoria da geração do MP4;
- tentativa de aumentar compatibilidade com celulares;
- melhorias nas tentativas de download do YouTube;
- ocultação da seção de planos para usuários que já possuem plano pago.

Esses ajustes foram preparados localmente, mas ainda não devem ser enviados para GitHub ou Render.

Antes do deploy:

1. instalar dependências;
2. validar Python;
3. validar banco;
4. testar downloads;
5. testar MP4;
6. testar Pix;
7. testar cartão;
8. testar planos;
9. testar usuário pago;
10. testar usuário gratuito;
11. revisar frontend;
12. revisar segurança;
13. revisar Git;
14. somente então fazer deploy.

---

## 20. REGRA DO CÉREBRO CENTRAL

O Cérebro Central é responsável por:

- arquitetura;
- decisões técnicas;
- integração entre áreas;
- auditoria;
- ordem das etapas;
- definição do que pode ou não ser alterado.

O chat de Engenharia executa a implementação.

O chat de UX/Copy cuida de experiência visual, copy e conversão.

O chat de Marketing cuida de aquisição e crescimento.

Nenhum departamento deve alterar unilateralmente decisões de outro departamento sem passar pelo Cérebro Central.

---

## 21. ESTADO DE RETORNO

Marco de retorno:

14/09/2026

Local:

`C:\Users\Igor\OneDrive\Desktop\Minhoca de Jardim`

Situação:

- estrutura `docs/` recém-criada;
- documentação começando a ser consolidada;
- código ainda em correção local;
- GitHub ainda não deve ser alterado;
- Render ainda não deve ser alterado;
- testes locais ainda precisam ser executados;
- Layout V1 aprovado, mas ainda não transportado integralmente para o frontend real.

Este documento é a visão geral do projeto.

Decisões específicas devem ser registradas em `DECISOES.md`.

Pendências devem ser registradas em `PENDENCIAS.md`.

Alterações devem ser registradas em `CHANGELOG.md`.