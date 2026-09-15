# MINHOCA DE JARDIM — CHANGELOG

## 14/09/2026

### Documentação

Criada a estrutura oficial de documentação:

- MASTER.md
- DECISOES.md
- PENDENCIAS.md
- CHANGELOG.md
- COPY.md
- MARKETING.md
- METRICAS.md

### Backend

Preparados localmente ajustes para:

- corrigir frequência do plano semanal;
- melhorar compatibilidade dos arquivos MP4;
- reprocessar vídeo usando H.264/AAC;
- adicionar tentativas alternativas para YouTube;
- manter validações de tamanho e existência do arquivo.

### Frontend

Preparado localmente:

- ocultação da seção de planos para usuários com plano pago.

### Validação

Já realizada:

- `py_compile` do `main.py`;
- validação do JavaScript extraído do HTML;
- teste de geração MP4 com FFmpeg;
- verificação do codec e pixel format.

### Estado

As alterações ainda são locais.

Não foram enviadas para:

- GitHub;
- Render.

Os testes funcionais ainda precisam ser executados.