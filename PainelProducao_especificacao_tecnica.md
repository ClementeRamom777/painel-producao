\# Painel de Produção Industrial — Especificação Técnica



\*\*Autor da especificação:\*\* Ramom Clemente

\*\*Stack:\*\* Python 3.13 · pandas · SQLite · matplotlib · openpyxl · PyYAML

\*\*Repositório:\*\* `painel-producao` (privado durante o estudo)

\*\*Data:\*\* setembro de 2026



\---



\## 1. O problema que o projeto resolve



Chão de fábrica gera dado o tempo todo — quantas folhas a máquina rodou, quanto tempo levou o setup, quanto foi para o lixo, se a ordem saiu no prazo. Esse dado quase sempre mora em planilha, e planilha de produção é \*\*suja por natureza\*\*: o operador escreve o nome da máquina de três jeitos diferentes, a data vem em quatro formatos, alguém deixa a célula vazia, alguém digita uma linha duas vezes, e no fim da planilha tem uma linha "TOTAL" que não é uma ordem de produção.



Enquanto esse dado não é tratado, ninguém consegue responder perguntas simples:



\- Qual máquina produz mais folha boa por hora?

\- Onde eu perco mais material?

\- Quanto tempo eu queimo em setup por turno?

\- Qual o meu percentual real de entrega no prazo?



O projeto é um \*\*pipeline completo\*\*: lê as planilhas, limpa, valida, grava em banco, calcula os indicadores e gera um painel visual — com um relatório explícito de tudo que foi corrigido ou rejeitado no caminho.



\---



\## 2. Escopo



\### O que o sistema faz



1\. \*\*Gera dados sintéticos realistas\*\* — planilhas de produção com sujeira proposital, para que o projeto seja executável por qualquer pessoa sem depender de dado real de empresa.

2\. \*\*Lê\*\* planilhas `.xlsx` de uma pasta de entrada, mesmo com cabeçalho fora da primeira linha.

3\. \*\*Limpa e normaliza\*\* — datas, números, nomes de máquina, espaços, maiúsculas.

4\. \*\*Valida\*\* regras de negócio e separa o que é aproveitável do que é inconsistente.

5\. \*\*Grava\*\* em SQLite de forma \*\*idempotente\*\* (rodar duas vezes não duplica nada).

6\. \*\*Calcula\*\* os indicadores de chão de fábrica.

7\. \*\*Gera\*\* um painel HTML autocontido e um relatório Excel formatado.

8\. \*\*Registra\*\* tudo em log e num relatório de qualidade de dados.



\### O que o sistema não faz (fronteira explícita)



\- Não é sistema de apontamento em tempo real. É análise em lote (batch).

\- Não tem interface web interativa nem banco de dados de servidor.

\- Não integra com ERP. A entrada é planilha, porque é assim que a maioria das fábricas realmente exporta.



> Essa fronteira é intencional e deve estar no README. Saber dizer o que o sistema \*\*não\*\* faz é sinal de maturidade técnica.



\---



\## 3. Decisões de engenharia



Estas quatro decisões são o conteúdo da entrevista. Cada uma precisa aparecer no README com o \*\*porquê\*\*, não só com o quê.



\### Decisão 1 — Configuração externa, nada cravado no código



Nomes de coluna, sinônimos de máquina, limites de validação e metas de KPI ficam em `config.yaml`. Quando a fábrica renomear a coluna "TIRAGEM" para "QTD PREVISTA", ninguém precisa abrir código Python.



\*\*Alternativa descartada:\*\* dicionário Python dentro do módulo. Mais rápido de escrever, mas transforma toda mudança de processo em alteração de código.



\### Decisão 2 — Falha visível, nunca silenciosa



Nenhuma linha some sem deixar rastro. Toda linha corrigida ou rejeitada vai para `saida/relatorio\_qualidade.csv` com: arquivo de origem, número da linha, coluna, valor original, motivo e ação tomada. Se o percentual de rejeição passar do limite definido em `config.yaml`, o programa termina com \*\*código de saída 1\*\*.



\*\*Por quê:\*\* dado que some sem aviso produz relatório bonito e errado. Um painel que mente é pior que painel nenhum. E o código de saída permite que isso rode em agendador e alerte sozinho quando a entrada degradar.



\*\*Alternativa descartada:\*\* `df.dropna()` e seguir em frente. É uma linha de código, e é como se perde a confiança do usuário para sempre.



\### Decisão 3 — Idempotência por hash da linha



Cada ordem gravada carrega um `hash\_linha` — SHA-256 da chave natural normalizada (número da OS + data de entrada + máquina). O `INSERT` usa `ON CONFLICT DO UPDATE`. Reprocessar a mesma pasta dez vezes produz exatamente o mesmo banco.



\*\*Por quê:\*\* em automação de verdade o script vai rodar de novo. Vai rodar de novo porque caiu no meio, porque chegou planilha corrigida, porque alguém clicou duas vezes. Se rodar de novo duplica dado, o sistema é uma armadilha.



\*\*Alternativa descartada:\*\* apagar a tabela e reimportar tudo. Funciona, mas destrói o histórico de importações e não permite carga incremental.



\### Decisão 4 — Camadas separadas



`leitura` → `limpeza` → `validacao` → `banco` → `indicadores` → `apresentacao`. Cada módulo tem uma responsabilidade e recebe o que precisa como parâmetro (injeção de dependência).



\*\*Por quê:\*\* dá para testar a limpeza sem ter planilha, e calcular indicador sem ter banco. Módulo que só faz uma coisa é módulo que dá para consertar sem medo.



\---



\## 4. Modelo de dados



\### 4.1 A planilha de entrada (o que o gerador produz)



Uma linha = uma ordem de produção apontada.



| Coluna na planilha | Significado | Tipo real |

|---|---|---|

| `OS` | Número da ordem de serviço | texto |

| `CLIENTE` | Código do cliente (`CLI-001`) — anonimizado | texto |

| `PRODUTO` | Ex.: Folder A4 4x4 couché 150g | texto |

| `MAQUINA` | Equipamento que rodou o job | texto (normalizar) |

| `OPERADOR` | Matrícula de quem apontou (`OP-03`) | texto |

| `TURNO` | 1, 2 ou 3 | texto (normalizar) |

| `DATA ENTRADA` | Quando entrou em produção | data |

| `PRAZO` | Data prometida ao cliente | data |

| `DATA ENTREGA` | Quando saiu (vazio = em produção) | data ou vazio |

| `TIRAGEM PREVISTA` | Quantidade pedida | inteiro |

| `TIRAGEM BOA` | Quantidade aprovada | inteiro |

| `REFUGO` | Quantidade perdida | inteiro |

| `SETUP (MIN)` | Tempo de preparação | número |

| `PRODUCAO (MIN)` | Tempo de máquina rodando | número |



> \*\*Clientes e operadores são identificados por código, não por nome.\*\* Não há nome inventado nem empresa real no repositório. É também o tratamento correto para dado de pessoa: matrícula identifica sem expor. Os indicadores não dependem do nome — dependem do agrupamento.



\### 4.2 A sujeira que o gerador injeta de propósito



O gerador recebe uma taxa de sujeira em `config.yaml` e aplica:



1\. \*\*Nome de máquina em variantes\*\* — `Heidelberg SM52`, `heidelberg sm-52`, `HEIDELBERG  SM 52 `, `Heid SM52`.

2\. \*\*Datas em quatro formatos\*\* — `05/03/2026`, `2026-03-05`, `5-mar-26`, e serial do Excel (`46083`).

3\. \*\*Números como texto\*\* — `5.000`, `5 000`, `5000,0`.

4\. \*\*Células vazias\*\* — em campos opcionais (data de entrega) e obrigatórios (tiragem boa).

5\. \*\*Linhas duplicadas\*\* — a mesma OS apontada duas vezes.

6\. \*\*Valores impossíveis\*\* — refugo maior que a tiragem, tempo negativo, entrega anterior à entrada.

7\. \*\*Cabeçalho fora da linha 1\*\* — duas ou três linhas de título/logo acima do cabeçalho real.

8\. \*\*Lixo no rodapé\*\* — linhas em branco e uma linha `TOTAL` com somatórios.

9\. \*\*Espaço sobrando\*\* em todo campo de texto.



> \*\*Isso é o coração do projeto.\*\* Qualquer júnior lê um CSV limpo. O que diferencia é lidar com o que a fábrica realmente exporta.



\### 4.3 Esquema do banco (SQLite)



```sql

CREATE TABLE IF NOT EXISTS maquina (

&#x20;   id             INTEGER PRIMARY KEY AUTOINCREMENT,

&#x20;   nome\_canonico  TEXT NOT NULL UNIQUE

);



CREATE TABLE IF NOT EXISTS ordem\_producao (

&#x20;   id                 INTEGER PRIMARY KEY AUTOINCREMENT,

&#x20;   hash\_linha         TEXT    NOT NULL UNIQUE,

&#x20;   numero\_os          TEXT    NOT NULL,

&#x20;   cliente            TEXT,

&#x20;   produto            TEXT,

&#x20;   maquina\_id         INTEGER NOT NULL REFERENCES maquina(id),

&#x20;   operador           TEXT,

&#x20;   turno              TEXT,

&#x20;   data\_entrada       TEXT    NOT NULL,

&#x20;   data\_prazo         TEXT,

&#x20;   data\_entrega       TEXT,

&#x20;   tiragem\_prevista   INTEGER,

&#x20;   tiragem\_boa        INTEGER,

&#x20;   refugo             INTEGER,

&#x20;   tempo\_setup\_min    REAL,

&#x20;   tempo\_producao\_min REAL,

&#x20;   arquivo\_origem     TEXT    NOT NULL,

&#x20;   importado\_em       TEXT    NOT NULL

);



CREATE INDEX IF NOT EXISTS idx\_ordem\_data    ON ordem\_producao(data\_entrada);

CREATE INDEX IF NOT EXISTS idx\_ordem\_maquina ON ordem\_producao(maquina\_id);



CREATE TABLE IF NOT EXISTS execucao\_importacao (

&#x20;   id                INTEGER PRIMARY KEY AUTOINCREMENT,

&#x20;   executado\_em      TEXT    NOT NULL,

&#x20;   arquivos\_lidos    INTEGER NOT NULL,

&#x20;   linhas\_lidas      INTEGER NOT NULL,

&#x20;   linhas\_gravadas   INTEGER NOT NULL,

&#x20;   linhas\_corrigidas INTEGER NOT NULL,

&#x20;   linhas\_rejeitadas INTEGER NOT NULL,

&#x20;   duracao\_segundos  REAL    NOT NULL

);

```



\*\*Por que `maquina` é tabela separada:\*\* normalização. O nome canônico existe uma vez só; a ordem aponta para ele. Se amanhã a máquina ganhar campos (fabricante, ano, velocidade nominal), tem onde colocar.



\*\*Por que data como TEXT:\*\* SQLite não tem tipo DATE. O padrão da própria ferramenta é texto no formato `YYYY-MM-DD`, que ordena corretamente como string. Isso deve estar comentado no código.



\---



\## 5. Indicadores



| Indicador | Fórmula | Leitura |

|---|---|---|

| \*\*Produtividade\*\* | `tiragem\_boa / (tempo\_producao\_min / 60)` | folhas boas por hora de máquina |

| \*\*Refugo\*\* | `refugo / (tiragem\_boa + refugo) \* 100` | percentual de material perdido |

| \*\*Tempo médio de setup\*\* | `média(tempo\_setup\_min)` por máquina | minutos parados por job |

| \*\*Cumprimento de prazo (OTD)\*\* | `entregues até o prazo / total entregue \* 100` | percentual de entrega no prazo |

| \*\*Aproveitamento de tempo\*\* | `tempo\_producao / (tempo\_producao + tempo\_setup) \* 100` | quanto do tempo total foi produção |



Cada indicador é calculado no total, por máquina, por turno e por mês. As metas ficam em `config.yaml` — o painel pinta verde ou vermelho comparando com a meta.



\*\*Cuidado obrigatório no código:\*\* divisão por zero. Ordem com `tempo\_producao\_min = 0` existe na vida real. A função de indicador retorna `None` nesses casos e o painel mostra "sem dado", nunca `inf` nem `NaN` na tela.



\---



\## 6. Saídas



\### 6.1 `saida/painel.html`



Arquivo único, autocontido, \*\*abre offline com dois cliques\*\*. Os gráficos são gerados com matplotlib, convertidos em PNG e embutidos como `data:image/png;base64,...` dentro do HTML. Sem CDN, sem internet, sem pasta de imagens ao lado.



Tema escuro, mesma paleta do portfólio: fundo `#060a0c`, cartões `#0c1519`, verde `#29e07f`.



Estrutura da página:



1\. Cabeçalho com período analisado, data de geração e total de ordens.

2\. Cinco cartões de KPI com o valor, a meta e o indicador de cor.

3\. Gráfico de barras: produtividade por máquina.

4\. Gráfico de barras: percentual de refugo por máquina, com linha da meta.

5\. Gráfico de linha: evolução mensal do OTD.

6\. Gráfico de barras horizontais: tempo médio de setup por máquina.

7\. Tabela das 10 ordens mais atrasadas.

8\. Rodapé com o resumo de qualidade de dados: lidas, gravadas, corrigidas, rejeitadas.



O item 8 é deliberado. Um painel que mostra a própria taxa de erro de ingestão é um painel em que dá para confiar.



\### 6.2 `saida/relatorio\_producao.xlsx`



Gerado com openpyxl, com cabeçalho formatado, largura de coluna ajustada e painel congelado. Abas:



\- \*\*Resumo\*\* — os KPIs consolidados

\- \*\*Por máquina\*\* — indicadores por equipamento

\- \*\*Por cliente\*\* — volume e refugo

\- \*\*Atrasos\*\* — ordens entregues fora do prazo

\- \*\*Qualidade dos dados\*\* — o que foi corrigido e rejeitado



\### 6.3 `saida/relatorio\_qualidade.csv`



Uma linha por ocorrência: `arquivo`, `linha\_planilha`, `coluna`, `valor\_original`, `problema`, `acao` (corrigido / rejeitado), `valor\_final`.



\### 6.4 `logs/execucao.log`



Log via `logging`, formato `data hora | nível | módulo | mensagem`. Nível configurável em `config.yaml`.



\---



\## 7. Estrutura de pastas



```

painel-producao/

├─ .gitignore

├─ README.md

├─ requirements.txt

├─ config.yaml

├─ dados/

│  ├─ entrada/            <- planilhas .xlsx (ignoradas pelo git)

│  └─ producao.db         <- SQLite (ignorado pelo git)

├─ saida/                 <- ignorada pelo git

├─ logs/                  <- ignorada pelo git

├─ docs/

│  └─ painel\_exemplo.png  <- print do painel, esse VAI para o git

├─ src/

│  ├─ \_\_init\_\_.py

│  ├─ config.py

│  ├─ gerador\_dados.py

│  ├─ leitura.py

│  ├─ limpeza.py

│  ├─ validacao.py

│  ├─ banco.py

│  ├─ indicadores.py

│  ├─ painel.py

│  ├─ relatorio\_excel.py

│  └─ main.py

└─ tests/

&#x20;  ├─ test\_limpeza.py

&#x20;  ├─ test\_validacao.py

&#x20;  └─ test\_indicadores.py

```



\### `.gitignore`



```

\_\_pycache\_\_/

\*.py\[cod]

.venv/

venv/

.idea/

.vscode/

dados/entrada/\*.xlsx

dados/\*.db

logs/

saida/

```



> Depois de criar o `.gitignore`, conferir com `git check-ignore -v` antes de confiar. Regra mal escrita já escondeu arquivo essencial antes.



\---



\## 8. Interface de linha de comando



Subcomandos com `argparse`:



```powershell

python -m src.main gerar-dados --meses 6 --arquivos 3

python -m src.main importar --entrada dados/entrada

python -m src.main indicadores --de 2026-01-01 --ate 2026-06-30

python -m src.main painel

python -m src.main relatorio

python -m src.main tudo

```



Todos aceitam `--config caminho.yaml` e `--verbose`.



Códigos de saída: `0` sucesso · `1` erro de execução ou rejeição acima do limite · `2` erro de configuração.



\---



\## 9. Construção por blocos



Cada bloco só começa depois que o anterior roda e passa no critério de aceite.



\### Bloco 1 — Fundação e configuração



Criar a estrutura de pastas, `requirements.txt`, `.gitignore` e `src/config.py`.



`config.py` carrega o YAML, valida que as chaves obrigatórias existem e devolve um objeto de configuração. Se faltar chave, levanta erro claro dizendo \*\*qual\*\* chave falta e \*\*em qual arquivo\*\* — e o programa sai com código 2.



\*Aceite:\* `python -m src.config` imprime a configuração carregada; apagar uma chave do YAML produz mensagem de erro compreensível.



\### Bloco 2 — Gerador de dados sintéticos



`gerador\_dados.py`. Gera N arquivos `.xlsx` na pasta de entrada, com M meses de histórico, aplicando a sujeira da seção 4.2 na taxa configurada. Usa semente fixa para ser reproduzível.



\*Aceite:\* abrir uma planilha gerada no Excel e enxergar as nove sujeiras da lista. Rodar duas vezes com a mesma semente gera arquivos idênticos.



\### Bloco 3 — Leitura



`leitura.py`. Encontra a linha do cabeçalho real, lê com pandas, descarta rodapé e linhas em branco, e mapeia os nomes de coluna usando os apelidos do `config.yaml`. Devolve um DataFrame cru e a lista de arquivos lidos.



\*Aceite:\* lê as três planilhas geradas e informa quantas linhas vieram de cada arquivo, mesmo com o cabeçalho na linha 4.



\### Bloco 4 — Limpeza



`limpeza.py`. Funções pequenas e independentes:



\- `normalizar\_texto(valor)`

\- `normalizar\_maquina(valor, sinonimos)`

\- `converter\_data(valor)` — inclusive serial do Excel

\- `converter\_numero(valor)` — separador de milhar e vírgula decimal

\- `normalizar\_turno(valor)`



Cada função devolve o valor limpo \*\*e\*\* a descrição da correção aplicada, para alimentar o relatório de qualidade.



\*Aceite:\* os testes em `tests/test\_limpeza.py` passam para todos os formatos da lista.



\### Bloco 5 — Validação



`validacao.py`. Aplica as regras de negócio:



\- Campos obrigatórios preenchidos

\- `refugo <= tiragem\_prevista \* limite`

\- Tempos não negativos

\- `data\_entrega >= data\_entrada`

\- `data\_prazo >= data\_entrada`



Separa o DataFrame em aprovadas e rejeitadas, monta o relatório de qualidade e calcula a taxa de rejeição.



\*Aceite:\* as linhas com valores impossíveis do gerador aparecem no relatório com o motivo correto.



\### Bloco 6 — Banco



`banco.py`. Cria o esquema se não existir, insere/atualiza máquinas, calcula o `hash\_linha`, grava as ordens com `ON CONFLICT DO UPDATE` e registra a execução.



\*Aceite:\* importar duas vezes seguidas e conferir que `SELECT COUNT(\*) FROM ordem\_producao` não muda.



\### Bloco 7 — Indicadores



`indicadores.py`. Lê do banco (a fonte da verdade a partir daqui) e devolve os cinco indicadores no total, por máquina, por turno e por mês. Trata divisão por zero.



\*Aceite:\* `python -m src.main indicadores` imprime os números e eles batem com uma conferência manual feita no Excel sobre a mesma amostra.



\### Bloco 8 — Painel HTML



`painel.py`. Gera os gráficos com matplotlib em tema escuro, converte para base64, monta o HTML e grava em `saida/painel.html`.



\*Aceite:\* abrir o arquivo com a internet desligada e ver tudo funcionando.



\### Bloco 9 — Relatório Excel



`relatorio\_excel.py`. Monta as cinco abas com openpyxl, com formatação.



\*Aceite:\* o arquivo abre no Excel sem aviso de reparo e as abas estão formatadas.



\### Bloco 10 — Orquestração e testes



`main.py` com os subcomandos, logging configurado, tratamento de exceção no topo e códigos de saída corretos. Fechar os testes das três funções críticas.



\*Aceite:\* `python -m src.main tudo` executa da geração ao painel em um comando; `pytest` passa; forçar rejeição alta no config faz o programa sair com código 1.



\---



\## 10. README do projeto — o que precisa estar lá



1\. \*\*O problema\*\*, em duas frases, sem jargão.

2\. \*\*Print do painel\*\* (`docs/painel\_exemplo.png`) logo no começo.

3\. \*\*Como rodar\*\* — quatro comandos, do clone ao painel aberto.

4\. \*\*Aviso honesto sobre os dados:\*\*



&#x20;  > Os dados deste projeto são \*\*sintéticos\*\*, gerados pelo próprio repositório. Nenhum dado de empresa real foi utilizado, e clientes e operadores aparecem apenas como código (`CLI-001`, `OP-03`) — o mesmo tratamento que se daria a dado real ao compartilhá-lo fora da empresa. O gerador injeta inconsistências propositais para que o pipeline de tratamento seja exercitado de verdade.



5\. \*\*As quatro decisões de engenharia\*\* da seção 3, cada uma com o porquê e a alternativa descartada.

6\. \*\*O que o sistema não faz\*\* — a fronteira da seção 2.

7\. \*\*Declaração de uso de IA\*\*, texto padrão obrigatório:



&#x20;  > Código inicial gerado com IA (Claude Code), depois reescrito à mão e revisado bloco a bloco até eu conseguir explicar cada parte sem consultar. As decisões de design foram minhas.



\---



\## 11. Onde eu posso estar errando



\- \*\*Volume de dados.\*\* Dimensionado para milhares de linhas. Se um dia virar milhões, a arquitetura de "carrega tudo em memória" quebra, e a resposta certa passa a ser processamento em pedaços ou banco de servidor. Preciso saber dizer isso numa entrevista.

\- \*\*Detecção automática do cabeçalho.\*\* A heurística funciona bem, mas não é infalível. Planilha com duas tabelas na mesma aba quebra. Limitação aceitável, deve estar no README.

\- \*\*Serial do Excel.\*\* A conversão depende do sistema de datas da planilha (1900 ou 1904). Assumido 1900, padrão no Windows.

\- \*\*Produtividade em folhas por hora\*\* é simplificação. Produtividade real de gráfica considera formato, número de cores e gramatura. O indicador é honesto para comparar a mesma máquina ao longo do tempo, e frágil entre máquinas de portes diferentes.



\---



\## 12. Estado ao final desta etapa



\*\*Aprendido:\*\* pipeline de dados de ponta a ponta, tratamento de dado sujo com pandas, modelagem relacional e chave estrangeira, idempotência por hash, gráfico com matplotlib, Excel formatado, subcomandos em argparse, teste automatizado.



\*\*Ainda não dominado:\*\* SQL além do básico, performance em volume grande, banco de servidor, agendamento em produção.



\*\*Próximo passo:\*\* Motor de Orçamento de Produção — usa o mesmo banco como fonte de custo histórico e fecha o ciclo orçamento → produção → entrega.



\---



\*Sapere aude.\*

