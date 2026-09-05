# Contexto do branch `ffb-tuning-tools`

Documento de retomada. Se você (ou um assistente) está pegando este trabalho em outra
máquina ou depois de um tempo, leia isto primeiro.

> **Nada aqui foi validado em bancada além do que está marcado como testado.** Onde diz
> "não testado", é literal.

---

## O que é este branch

Fork de `odrive-gui-configurator` (original de **Marcos Silva**), adicionando ferramentas
de ajuste para volante **direct drive** com **OpenFFBoard**.

**Regra que orientou tudo:** nada do comportamento original foi alterado. As abas, o worker
de telemetria e os fluxos do Marcos continuam funcionando exatamente como antes. Tudo novo
está em arquivos novos, e os arquivos dele receberam apenas registro e ligação.

`main` continua no commit original. Todo o trabalho está neste branch, deliberadamente,
porque o código controla um motor e não foi validado.

---

## O hardware alvo

Números do setup em que isto foi desenvolvido — vários diagnósticos dependem deles:

| | |
|---|---|
| Placa | ODrive **v3.6 56V** |
| Fonte | **24 V** (não confundir com a variante da placa) |
| Firmware | 0.5.6 (recomendado pelo guia ODrive do OpenFFBoard) |
| Motor | Hoverboard, **15 pares de polos**, cogging alto |
| Encoder | CPR **65536**, `bandwidth` 1000 (padrão) |
| `current_lim` | 35 A |
| Ganhos de velocidade | Padrões de fábrica (`vel_gain` 1/6, `vel_integrator_gain` 2/6) |
| Pacote python | `odrive==0.6.10.post0` — conecta normalmente na placa com fw 0.5.x |

Observação de campo: com `damper = 0` no OpenFFBoard o volante oscila e não para no centro;
o mínimo utilizável foi 20. **Isso é normal em direct drive** (sem amortecimento mecânico),
não é defeito, e não tem relação com os ganhos de velocidade — o FFB usa controle de torque,
que não passa por eles.

---

## O que foi construído

Uma aba de topo **"Ajustes"** (`TuningContainer`) com quatro sub-abas, na ordem em que devem
ser executadas. Ver `docs/GUIA-AJUSTE-FFB.md` para o procedimento.

| Sub-aba | Arquivo | Estado |
|---|---|---|
| 1. Calibração | `tabs/alignment_tab.py` | **não testado em bancada** |
| 2. Medição de Kt | `tabs/tuning_tab.py` | **não testado em bancada** |
| 3. Segurança | `tabs/safety_tab.py` | **não testado em bancada** |
| 4. Preset FFB | `tabs/preset_tab.py` | **não testado em bancada** |

Workers em `tabs/tuning_workers.py`. Fora da aba Ajustes:

- **Botão de estado do eixo** (`main.py`) — alterna IDLE ↔ malha fechada, e serve como
  abortar durante calibrações. *Funciona em hardware.*
- **Download automático de firmware** (`tabs/firmware_download.py` + `firmware_tab.py`) —
  detecta a placa e baixa o `.elf` correto dos releases da ODrive. O DFU e o flash já
  existiam. *Download verificado contra o GitHub real; o flash em si não foi testado.*
- **Sinal `extended_telemetry`** em `tabs/workers.py` — canal novo com temperaturas e
  corrente de barramento. O `telemetry_updated` original **não foi alterado**.

---

## Achados verificados no firmware

Tudo abaixo foi conferido no código-fonte, não deduzido. São a base das decisões de
implementação e economizam releitura.

**ODrive fw-v0.5.6:**

| Onde | O quê |
|---|---|
| `encoder.hpp:22` | `calib_scan_distance = 16π` rad elétricos = `8/pares_de_polos` voltas mecânicas. Com 15 pp: **0,53 volta** |
| `encoder.cpp:400-458` | `run_offset_calibration` **já varre para frente e para trás e tira a média** |
| `odrive-interface.yaml:1322` | A propriedade é **`encoder.config.phase_offset`** no 0.5.6 (era `offset` em 0.5.x mais antigos) |
| `motor.cpp:44` | Escala modulação→tensão: `V = mod × (2/3) × vbus` |
| `motor.cpp:585` | Feedforward de back-EMF: `vq += phase_vel × (2/3) × (Kt/pp)` → **`Kt = λ × 1,5 × pp`** |
| `odrive-interface.yaml:833-836` | `current_control` expõe `final_v_alpha/beta`; **não existe `mod_q`** |
| `can_simple.cpp:60` | `axis.watchdog_feed()` a **cada mensagem CAN recebida** |

**OpenFFBoard:**

| Onde | O quê |
|---|---|
| `ODriveCAN.cpp:158` | Se achar o eixo em IDLE no boot, dispara `FULL_CALIBRATION_SEQUENCE` — **sobrescreve o offset**. `startup_closed_loop_control = True` evita |
| `ODriveCAN.cpp:293` | `torque = (saída/32767) × maxtorque`, enviado em Nm; a ODrive divide por `torque_constant` |
| Guia ODrive | node_id 0, baud 500000, `CONTROL_MODE_TORQUE_CONTROL`, `enable_torque_mode_vel_limit = False`, fw 0.5.6 |

---

## O que não funcionou: a varredura de alinhamento

**Removida do código.** Registrada aqui para ninguém repetir.

A ideia era refinar o offset elétrico varrendo candidatos e escolhendo o que produz mais
torque por ampère. **Oito abordagens, nenhuma repetível em hardware:**

1. Menor corrente a velocidade fixa — o controlador de velocidade não segura setpoint neste motor
2. Média aritmética dos dois sentidos — **bug**: valores que dão a volta no círculo
3. Média circular — corrigiu o item 2, mas a janela punha o ótimo na emenda
4. Janela centrada — fez o primeiro ponto virar o pior caso, quebrando a checagem inicial
5. Filtro por velocidade mantida — nenhum offset segurava velocidade (0 a 11 turns/s de 3 pedidas)
6. Aceleração por ampère (controle de torque) — dispensa o loop de velocidade
7. **Bug**: `abs()` na aceleração fazia 180° elétricos empatar com o alinhamento perfeito
8. Torque menor para suavizar — ficou abaixo do piso de cogging, o motor nem se movia

Resultados em hardware: `−16,9°`, `−98,5°`, `+148,5°`, `−103,5°`, `+123,8/+178,9`,
`−90,0/+22,5`, `+180,0/−11,3`. Aleatórios.

**Por que era impossível daqui:** varrer o offset significa descomutar deliberadamente um
motor de alta potência. O drive se protege e recusa armar na maioria dos pontos ruins, e nos
que sobram o cogging de um motor de hoverboard é da ordem do sinal. Além disso, cada ponto
custa `idle → escreve → arma → mede → idle` por USB, amostrando a dezenas de Hz — contra os
8 kHz de quem faz isso dentro do firmware.

**A substituição:** ler o `run_offset_calibration` mostrou que a ODrive já faz a média
bidirecional, melhor. O que ela não sabe é **quanto varrer**. A aba 1 agora roda a
calibração nativa repetidamente com a distância padrão e depois em voltas mecânicas
inteiras, e compara. Os dois parâmetros que importam (`calib_scan_distance`,
`calibration_current`) já são expostos — **não é preciso forkar o firmware**.

**Cuidado conceitual registrado:** dispersão mede repetibilidade, não exatidão. Uma
varredura curta pode acertar sempre o *mesmo valor errado*. Voltas inteiras são corretas por
construção; a dispersão é só evidência de confiabilidade.

---

## Bugs encontrados por teste (e o que os pegou)

| Bug | Como apareceu |
|---|---|
| Janela de média de corrente não limpava entre capturas → Kt saía 1,1174 em vez de 0,49 | Teste funcional da aba de Kt |
| Layout esmagado (grupos a 58px de 246px pedidos) | Medição de geometria com a janela em tamanho real |
| SSL `CERTIFICATE_VERIFY_FAILED` no download de firmware | Download real contra o GitHub |
| `requirements.txt` é **UTF-16LE + CRLF** | Falha ao editar assumindo UTF-8 |
| Média aritmética / `abs()` na aceleração | Testes passavam **por sorte** — empates resolvidos pela ordem do dicionário |

Lição das duas últimas: teste o **mecanismo**, não só o desfecho. Quando dois candidatos
empatam, verificar apenas o resultado final não prova nada.

---

## Ambiente de desenvolvimento e testes

O app precisa de PySide6, odrive, pyqtgraph, qdarkstyle, ansi2html, certifi.

Para testar sem hardware, foi usado um venv separado com um **stub** de `odrive`/`fibre`
(módulo com `__getattr__` que resolve qualquer enum) e ODrives simuladas com física real
(torque contra inércia e atrito). Testes rodam com `QT_QPA_PLATFORM=offscreen`.

Padrão que funcionou bem: simular o comportamento observado em hardware (trava, dispara,
cogging) e verificar que a rotina se comporta corretamente — inclusive **falhando** quando
deve.

**Traduções:** editar `translations/pt_BR.ts` (XML, um `<context>` por classe, nomes de
contexto = nome da classe) e recompilar:

```
pyside6-lrelease translations/pt_BR.ts -qm translations/pt_BR.qm
```

O `.qm` está commitado, então não é preciso rodar isso para usar o app. Strings com
`QCoreApplication.translate("Classe", ...)` vão para o contexto nomeado; `self.tr(...)` vai
para o contexto da classe.

---

## O que falta

1. **Testar a aba 1** (qualidade da calibração) — uma execução já diz se a tese da distância
   de varredura está certa. É o próximo passo imediato.
2. **Testar a aba 2** (Kt) — é a que resolve o problema real de superaquecimento
3. **Testar as abas 3 e 4**
4. `LICENSES` aparece como deletado no working tree; é anterior a este trabalho e foi
   deixado de fora dos commits de propósito
5. Decidir se o branch entra em `main`, depois dos testes

**Prioridade:** a aba 2. O motor esquentando é o problema declarado, e o Kt correto é o que
resolve. A aba 1 é refinamento.
