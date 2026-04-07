# Caixa
## Processo Operacional e Apresentação ao Cliente

### 1. Objetivo

O objetivo do novo processo de caixa é aproximar a aplicação da forma real como a empresa trabalha no dia a dia.

Em vez de tratar o caixa apenas como um local onde se marcam pagamentos, o sistema passa a acompanhar:

- o que foi recebido
- o que ficou em dívida
- o que saiu em despesas
- o que está por pagar a subcontratados
- o saldo corrido do período

Isto aproxima a aplicação da lógica da folha de caixa atualmente usada pela empresa.

---

### 2. Problema no modelo anterior

No modelo anterior, a aplicação funcionava mais à base de estados simples:

- a marcação era dada como paga ou não paga
- o valor da marcação ficava quase sempre definido logo à partida
- o caixa ficava separado da liquidação real
- as dívidas do cliente não eram tratadas de forma flexível
- o pagamento de subcontratados e as despesas não estavam integrados num fluxo operacional único

Na prática, isto criava diferença entre:

- o que acontece na receção
- o que a empresa controla na folha Excel
- o que ficava registado na aplicação

---

### 3. Novo princípio de funcionamento

O processo passa a ser dividido em 4 blocos principais:

#### 3.1. Preço previsto

Quando a marcação é criada, a aplicação continua a calcular um preço previsto com base em:

- serviço
- parceria
- desconto

Este valor serve como referência inicial.

#### 3.2. Liquidação real

No momento do pagamento, o operador decide o que realmente vai ser cobrado.

Exemplos:

- o cliente afinal não quer usar a parceria
- o cliente paga apenas a consulta de hoje
- o cliente paga a consulta de hoje e mais 2 antigas
- o cliente paga só uma parte

Ou seja, a aplicação deixa de depender apenas de um "pago / não pago" e passa a saber:

- quanto foi recebido
- a que consulta(s) foi associado
- quanto continua em aberto

#### 3.3. Caixa

O caixa passa a refletir os movimentos reais:

- entradas
- saídas
- método de pagamento
- numerário esperado

#### 3.4. Controlo mensal/corrido

Além da sessão diária, o sistema passa a apresentar um resumo corrido do mês para facilitar a leitura operacional:

- recebimentos lançados
- despesas lançadas
- saldo lançado
- dívidas em aberto
- pagamentos a subcontratados pagos
- pagamentos a subcontratados em aberto

---

### 4. Como funciona o processo no dia a dia

### Passo 1. Abrir caixa

No início do dia, a receção abre a sessão de caixa e define:

- data
- fundo inicial
- observações, se necessário

Isto cria o ponto de partida para os movimentos do dia.

### Passo 2. Realizar a marcação

A marcação continua a existir com o seu preço previsto.

Mas esse valor ainda não é, obrigatoriamente, o valor final cobrado.

### Passo 3. Fazer a liquidação no fim

Quando o cliente vai pagar, o operador pode:

- manter o preço automático
- retirar a parceria
- definir um valor manual

Além disso, pode registar:

- valor recebido agora
- método de pagamento
- referência
- observações

### Passo 4. Aplicar o pagamento

O pagamento pode ser usado para:

- pagar a marcação atual
- pagar uma ou várias dívidas antigas
- pagar parcialmente

Isto permite que a aplicação saiba exatamente:

- quanto o cliente pagou hoje
- quantas consultas ficaram liquidadas
- quanto continua em aberto

### Passo 5. Lançar no caixa

Os pagamentos registados no checkout passam a ser lançados no caixa.

Quando não entram automaticamente, ficam visíveis como pendentes para lançamento na sessão do dia.

Assim, a receção vê rapidamente:

- o que já entrou no caixa
- o que já foi recebido, mas ainda falta lançar

### Passo 6. Registar saídas

Despesas, ajustes ou outros movimentos são registados num lançamento manual, com:

- entrada ou saída
- método
- valor
- descrição
- data/hora
- notas

### Passo 7. Consultar o resumo operacional

Na área de caixa, o sistema apresenta:

- resumo do dia
- resumo corrido do mês
- recebimentos pendentes
- dívidas em aberto
- situação dos subcontratados

Isto reduz a necessidade de consultar várias áreas separadas.

### Passo 8. Fechar caixa

No fim do dia, a receção indica:

- numerário contado
- observações de fecho

O sistema calcula:

- numerário esperado
- diferença de caixa

---

### 5. O que a nova área de caixa mostra

#### 5.1. Resumo corrido do mês

Pensado para se aproximar da folha Excel.

Mostra:

- recebimentos lançados
- despesas lançadas
- saldo lançado
- dívidas em aberto
- subcontratados pagos
- subcontratados em aberto

#### 5.2. Caixa do dia

Mostra:

- abertura
- entradas
- saídas
- saldo lançado
- numerário esperado
- pendentes por lançar

#### 5.3. Recebimentos por lançar

Agrupa no mesmo local:

- pagamentos registados no checkout
- marcações antigas pagas fora do novo fluxo
- mensalidades de turma
- vendas rápidas de stock

#### 5.4. Dívidas do mês

Permite ver rapidamente:

- cliente
- data
- origem
- valor em aberto

#### 5.5. Subcontratados

Permite perceber:

- quanto já foi pago
- quanto ainda está em aberto

---

### 6. Benefícios para a empresa

Com este modelo, a empresa passa a ter:

- menos dependência da folha Excel
- maior controlo sobre o que foi realmente recebido
- maior clareza sobre dívidas dos clientes
- leitura mais simples do caixa diário e do mês
- melhor preparação para faturação automática no futuro

---

### 7. Benefícios para a receção

Para a receção, o fluxo fica mais direto porque:

- o pagamento deixa de ser apenas "sim ou não"
- é possível pagar consultas antigas no mesmo ato
- os pendentes aparecem concentrados no ecrã de caixa
- despesas e ajustes ficam registados no mesmo local
- o fecho diário fica mais claro

---

### 8. Benefícios para gestão

Para gestão e direção, o sistema passa a permitir:

- acompanhar o saldo lançado do mês
- ver rapidamente o valor em dívida
- controlar despesas lançadas
- perceber o que está por pagar a subcontratados
- comparar melhor a operação real com a faturação futura

---

### 9. Estado atual da implementação

Já está preparado:

- liquidação flexível da marcação
- pagamento de marcação atual e dívidas antigas
- registo de pagamentos do cliente
- associação dos pagamentos às marcações
- ligação desses pagamentos ao caixa
- novo ecrã de caixa com visão operacional e mensal

Ainda não está totalmente fechado:

- emissão automática de faturas na Moloni
- saída automática em caixa ao pagar subcontratados
- módulo estruturado de despesas com categorias próprias

---

### 10. Mensagem simples para apresentar ao cliente

Podemos resumir a proposta desta forma:

> A aplicação está a deixar de funcionar apenas como agenda com marcações pagas ou não pagas.
> Passa a funcionar como um verdadeiro controlo operacional de caixa, alinhado com a forma como a empresa trabalha hoje:
> recebimentos, dívidas, despesas, subcontratados e saldo corrido.

---

### 11. Versão curta para reunião

Se precisares de apresentar isto em 2 minutos:

#### Antes

- a aplicação marcava pagamentos, mas não espelhava bem a realidade do caixa
- havia diferença entre a operação real e o que ficava registado

#### Agora

- o pagamento pode liquidar várias consultas
- o caixa mostra entradas, saídas, dívidas e subcontratados
- a receção trabalha com um fluxo mais próximo da folha de caixa

#### Resultado

- mais controlo
- menos confusão
- base preparada para faturação automática no futuro

---

### 12. Próximo passo recomendado

O próximo passo natural será fechar o ciclo financeiro com:

- automatização das saídas de subcontratados
- estrutura de despesas mais completa
- integração final com a Moloni para emissão de documento fiscal

