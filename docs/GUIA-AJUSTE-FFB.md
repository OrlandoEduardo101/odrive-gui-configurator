# Guia de Ajuste — ODrive + OpenFFBoard (Direct Drive)

Ordem de calibração para volante direct drive usando as ferramentas da aba **Ajustes**.

> **Estas rotinas nunca foram testadas em hardware.** Foram validadas contra uma ODrive
> simulada, que não tem atrito real, cogging real nem resposta térmica. Rode a primeira
> vez com limite de corrente baixo, eixo livre e a mão no cancelar.

---

## Por que a ordem importa

Cada etapa depende da anterior. Fora de ordem, o resultado é silenciosamente errado:

| Etapa | Depende de | Se pular ou inverter |
|---|---|---|
| Índice Z | — | O alinhamento não persiste no boot |
| Calibração | Índice Z | O offset carrega o viés de cogging |
| Kt | Calibração | Referencial dq torto corrompe a leitura de Iq |
| `current_lim` | Kt | Você não sabe quantos Nm está pedindo |
| Térmica | `current_lim` | O motor queima antes de qualquer aviso |
| Preset FFB | Tudo acima | O OpenFFBoard sobrescreve a calibração |

---

## Pré-requisitos

- Motor calibrado e encoder calibrado (abas **Motor** e **Encoder**)
- STM32CubeProgrammer instalado, se for atualizar firmware
- Firmware **0.5.6** — recomendado pelo guia ODrive do OpenFFBoard
- **Eixo livre**: nada preso ao volante nas etapas 2 e 4

---

## 1. Índice Z e calibração persistente

Aba **Encoder**:

1. Marque **"Use Z-Index (Fast Startup)"**
2. Marque **"Save Calibration (pre_calibrated)"**
3. Recalibre o encoder com índice
4. Aba **Motor** → marque **"Save Motor Calibration (Pre-Calibrated)"**
5. **Save Configuration** (exige estado IDLE)

Sem o índice Z o offset é recalculado a cada boot, e todo o trabalho da etapa 2 se perde.

---

## 2. Qualidade da calibração do encoder

**Ajustes → 1. Calibração.**

A calibração da ODrive já varre para frente e para trás e tira a média, o que cancela
cogging — mas só ao longo da distância varrida. O padrão é `calib_scan_distance = 16π`
radianos **elétricos**, que em voltas mecânicas dá:

```
16π / (2π × pares_de_polos) = 8 / pares_de_polos
```

Com 15 pares de polos isso é **0,53 volta** — pouco mais de meia volta do rotor. O cogging
se repete com a posição mecânica, então em meia volta ele não se cancela: a calibração
carrega o viés daquele arco.

A rotina roda a calibração várias vezes como está, depois várias vezes em **voltas
mecânicas inteiras**, e mostra:

- a **dispersão** de cada ajuste (o quanto o resultado se move entre execuções)
- a **diferença entre as médias** — que é o viés de cogging sendo removido

Ao final ela deixa a varredura longa aplicada e o encoder calibrado com ela.

**Dispersão baixa não significa correto.** Uma varredura curta pode repetir o mesmo erro
com muita consistência. Voltas inteiras são corretas por construção; a dispersão só mostra
se o resultado é confiável o bastante.

3. **Save Configuration** ao terminar.

---

## 3. Medir o Kt

**Ajustes → 2. Medição de Kt.** Dois métodos; rode o automático primeiro.

### 3a. Automático (back-EMF) — sem hardware extra

Gira o motor sem carga e lê a tensão aplicada, que sobe com a velocidade
proporcionalmente ao fluxo do ímã.

- Velocidade máxima: 10 turns/s
- Velocidades amostradas: 5
- Limite de corrente: 10 A

Precisão típica ±5%. **Confira o R²**: abaixo de 0,98 o resultado não é confiável.

### 3b. Peso (mais preciso, ±2%)

Só se quiser confirmar. Braço de comprimento conhecido no eixo, na **horizontal**
(posição de 3 ou 9 horas), motor em malha fechada segurando.

1. Preencha raio (mm) e ângulo (0° se horizontal)
2. Pendure um peso, **espere a leitura ficar verde**, digite a massa, **Capturar**
3. Repita com massas diferentes — 3 ou 4 pontos bastam

A regressão pela inclinação cancela sozinha o peso da haste, o atrito e o cogging.
Não é preciso atingir o torque máximo do motor.

### Cruzamento

Com os dois métodos rodados, a aba compara. Até 5% de diferença, confie em qualquer um.

4. **Apply Kt to ODrive** → **Save Configuration**

---

## 4. Definir o limite de corrente

Agora o número faz sentido:

```
current_lim = torque_desejado_Nm / Kt
```

Exemplo: motor de 10 Nm com Kt medido de 0,49 → `10 / 0,49 = 20,4 A`.

**Não copie o limite de corrente de outra pessoa.** Ele depende do Kt e da capacidade
térmica do *seu* motor. O mesmo 35 A que funciona no motor de alguém pode significar
17 Nm no seu — muito acima do que ele aguenta.

Aba **Motor** → `Current Limit`. Depois **Ajustes → 3. Segurança** mostra o pico em Nm que
isso representa, para conferir.

---

## 5. Proteção térmica

**Ajustes → 3. Segurança.** A ODrive 3.6 tem termistor de FET embarcado — é leitura grátis.

Entre o limite inferior e o superior a corrente é **reduzida gradualmente**; acima do
superior dá erro e desarma. Num volante essa faixa importa: um corte seco no meio da
curva deixa o volante mole sem aviso.

| Sensor | Redução | Erro |
|---|---|---|
| Placa (FET) | 80 °C | 100 °C |
| Motor (NTC opcional) | conforme o motor | conforme o motor |

O sensor da placa só mede o estágio de potência. Só um NTC colado no enrolamento mede o
cobre que realmente queima.

**Rode o teste de Kt com a temperatura à vista** para saber o quanto seu motor esquenta
de fato antes de escolher os limites.

---

## 6. Watchdog

Mesma aba. Desarma o eixo se nenhum comando chegar dentro do tempo limite — se o CAN
cair, o volante não fica aplicando força contra você.

**Seguro com OpenFFBoard via CAN:** o firmware da ODrive alimenta o watchdog a cada
mensagem CAN recebida (`can_simple.cpp`), e o OpenFFBoard envia comando de torque a cada
ciclo. Padrão de 0,5 s.

Se você controla a ODrive de outra forma, confirme que ela envia algo periodicamente
antes de habilitar.

---

## 7. Preset FFB

**Ajustes → 4. Preset FFB.** Tabela de atual → proposto, grava só o que difere.

Confira o **Node ID** e a **taxa do CAN** contra a config do seu OpenFFBoard
(padrão: node 0, 500000).

A linha mais importante é `startup_closed_loop_control = True`:

> Se o OpenFFBoard encontrar o eixo em IDLE no boot, ele dispara
> `FULL_CALIBRATION_SEQUENCE`, que **recalibra e sobrescreve o offset da etapa 2**.
> Armar no boot faz ele encontrar o eixo pronto e pular a calibração.

8. **Save Configuration** → **Reboot ODrive** → confirme que ela sobe já armada.

---

## 8. Ajustar o `maxtorque` no OpenFFBoard

O OpenFFBoard escala a saída assim:

```
torque_Nm = (saída_FFB / 32767) × maxtorque      → enviado por CAN
Iq = torque_Nm / torque_constant                  → feito pela ODrive
```

Ajuste `maxtorque` para o torque de pico que você definiu na etapa 4.

**Aviso:** com o Kt correto, o volante pode *parecer mais fraco* do que estava antes. Ele
não ficou mais fraco — ficou honesto. Se antes o Kt estava baixo demais, a ODrive
comandava corrente demais e você recebia mais força do que pedia, pagando em calor.

Compense no **ganho do jogo ou do driver**, que é o lugar certo. Voltar a baixar o Kt é
voltar a cozinhar o motor.

---

## Referência rápida

| # | Etapa | Onde | Salvar depois |
|---|---|---|---|
| 1 | Índice Z + pre_calibrated | Encoder / Motor | sim |
| 2 | Qualidade da calibração | Ajustes → 1. Calibração | sim |
| 3 | Medir e aplicar Kt | Ajustes → 2. Medição de Kt | sim |
| 4 | `current_lim` | Motor | sim |
| 5 | Limites térmicos | Ajustes → 3. Segurança | sim |
| 6 | Watchdog | Ajustes → 3. Segurança | sim |
| 7 | Preset FFB | Ajustes → 4. Preset FFB | sim + reboot |
| 8 | `maxtorque` | OpenFFBoard | — |

**Save Configuration exige estado IDLE.** O botão de estado do eixo alterna entre IDLE e
malha fechada, e durante uma calibração ele serve como abortar.

---

## Armadilhas

- **Kt não é ajuste, é medição.** É propriedade física do motor. Não existe "Kt ideal";
  existe o Kt verdadeiro do seu motor.
- **Kt de outro motor não serve.** Nem que seja o mesmo encoder — o encoder dá ângulo,
  o Kt vem dos ímãs e do enrolamento.
- **A corrente da fonte não é a corrente de fase.** Em baixa rotação elas diferem muito:
  `V_bus × I_bus = I_fase² × R`. Uma fonte de 30 A não limita a fase a 30 A.
- **A ODrive não detecta resistor de frenagem queimado.** Não há sensor nesse caminho.
  O `dc_bus_overvoltage_trip_level` é o único recurso — ajuste-o abaixo do que sua fonte
  tolera, e use a rampa de sobretensão em Ajustes → Segurança.
- **Anti-cogging não está incluído** de propósito. No firmware 0.5.x ele é reconhecidamente
  problemático e há relatos de piora.
