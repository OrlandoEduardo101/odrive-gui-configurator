# ODrive GUI Configurator

Uma interface gráfica moderna para configuração e monitoramento de placas ODrive, desenvolvida em Python e distribuída como aplicação compilada `.exe`.  O código é **aberto** e está disponível neste repositório para estudo, modificação e contribuições da comunidade.

---

🌍 Idiomas: [🇧🇷 Português](README-ptbr.md) | [🇺🇸 English](README.md)

## 🚀 Funcionalidades

### 🔌 Conexão e Controle
- **Conectar ao ODrive**: Inicia a comunicação e carrega os dados de configuração da placa.  
- **Desconectar do ODrive**: Interrompe manualmente a comunicação com a placa.  
- **Reiniciar ODrive**: Envia o comando de reinicialização para a placa.  
- **Limpar Erros**: Limpa todos os sinalizadores de erro do ODrive.  
- **Apagar Configuração**: Restaura o ODrive para as configurações de fábrica.  
- **Definir Eixo 0 para OCIOSO**: Coloca o eixo 0 no estado IDLE, necessário antes de salvar as configurações.  
- **Salvar Configuração**: Salva as configurações atuais na memória não volátil do ODrive.  
- **Backup e Restauração**: Permite salvar e restaurar a configuração do ODrive.  
- **Atualização de Firmware**: Facilita a atualização do firmware do ODrive.  
- **Fonte de Energia (DC)**: Configurações e leituras em tempo real da alimentação.  
- **Motor**: Configuração, calibração e status em tempo real do motor.  
- **Encoder**: Calibração, configurações e status em tempo real do encoder.  
- **CAN**: Configuração e parâmetros da comunicação CAN.  
- **Gráfico**: Monitoramento visual em tempo real dos parâmetros do ODrive.  
- **Terminal**: Interação direta com o ODrive por meio de comandos.  

### 📊 Monitoramento em Tempo Real
- Posição do encoder (em graus).  
- Velocidade estimada (voltas/s).  
- Tensão do barramento (VBus) em volts.  
- Limite de corrente configurado (A).  
- Estado atual do eixo (`AXIS_STATE_*`).  
- Gráfico em tempo real com atualização contínua.  

### 🧰 Erros e Diagnóstico
- **Mostrar Erros** – Abre uma janela dedicada que lista todos os erros atuais do ODrive, com destaque em HTML.  
- **Limpar Erros** – Botão para resetar os erros da placa.  

---

### 🌍 Idiomas
- 🇧🇷 Português (pt-BR)  
- 🇺🇸 English (en-US)  

## ⚙️ Capturas de Tela

### ⚡ Alimentação DC
- **Limites de Tensão:** Define a tensão mínima e máxima do barramento DC.  
- **Limites de Corrente:** Define corrente positiva, negativa e regenerativa máxima.  
- **Resistor de Frenagem:** Ativa/desativa e configura o valor do resistor.  
- **Aplicar Configurações:** Aplica imediatamente os parâmetros definidos.  
<img width="855" height="634" alt="{373377A9-7474-4532-914F-F4E180DFA31C}" src="https://github.com/user-attachments/assets/94a08381-b1d2-49ef-a22d-f3de763c1baa" />

### 🔧 Aba Motor
- **Configurações Principais:** Define pares de polos, constante de torque, tipo de motor e modo de controle padrão.  
- **Limites e Controle:** Configura limite de corrente, largura de banda e limites de potência.  
- **Calibração do Motor:** Executa calibração ajustando corrente/tensão e salva os dados.  
<img width="852" height="635" alt="{A25A820E-FB90-4CF0-8248-89FA0D35086A}" src="https://github.com/user-attachments/assets/336c007b-2d7b-4181-bcdf-e47d7ebaf644" />

### 🎛️ Aba Encoder
- **Configurações Básicas:** Seleciona o modo do encoder, define CPR, largura de banda e status de calibração.  
- **Método de Inicialização:** Sem ação, calibração em cada inicialização ou uso de Z-Index.  
- **Controle em Malha Fechada:** Permite habilitar automaticamente no início.  
<img width="853" height="635" alt="{7677AE8E-D0CF-46CD-A44C-389DFCD0BCB5}" src="https://github.com/user-attachments/assets/e1be79fe-bd5f-4b2a-a9d6-b8d541018676" />

### 🛰️ Aba CAN
- **ID do Nó:** Define o Node ID no barramento CAN.  
- **Taxa de Transmissão:** Configura a velocidade de comunicação.  
- **Aplicar Configurações:** Salva e aplica os parâmetros CAN no ODrive.  
<img width="853" height="632" alt="{7B386724-BCAA-47B6-AE4A-19F51466F49F}" src="https://github.com/user-attachments/assets/ab59045a-f1a3-4805-9057-cdeb2e1ff0df" />

### 🔄 Aba Firmware
- **Informações do Dispositivo:** Mostra versão de firmware, hardware e número de série.
- - **Pré-requisito (Windows):** Necessário instalar o **STM32CubeProgrammer** para atualizar. 
- **Etapas de Atualização:** Entrar em modo DFU e verificar status.  
- **Gravação de Firmware:** Selecionar arquivo e gravar na placa.  
<img width="852" height="633" alt="{3FB5C52C-C936-4CEE-AC65-B46ACCD4DC3A}" src="https://github.com/user-attachments/assets/5c654182-5b8f-40ed-8e20-bccd0979d07d" />

### 📈 Aba Gráfico
- **Exibição:** Posição real, posição alvo, velocidade e corrente Iq.  
- **Controles:** Pausar ou limpar o gráfico.  
- **Uso:** Útil para análise e ajuste em tempo real.  
<img width="851" height="634" alt="{A1854F11-C698-48BE-945D-F5190887CF5C}" src="https://github.com/user-attachments/assets/896d671a-e7d5-4a03-a816-cb6947967e32" />

### 🖥️ Aba Terminal
- **Entrada de Comandos:** Envia comandos diretamente ao ODrive.  
- **Saída:** Mostra histórico e respostas.  
- **Controles:** Limpar terminal ou acessar ajuda.  
<img width="853" height="635" alt="{DAF4C033-85CB-4471-8476-F3D2CEE79398}" src="https://github.com/user-attachments/assets/6e47380a-f9e4-4c31-be49-912923164c46" />

### 💾 Aba Backup
- **Exportar Configuração:** Salva em JSON (compatível com todas as versões).  
- **Importar Configuração:** Restaura a partir de JSON (⚠️ substitui todas as configs).  
- **Log do Processo:** Mostra detalhes das operações.  
<img width="854" height="632" alt="{9506F4D5-DE2F-4449-9048-AB5FBC5D55DD}" src="https://github.com/user-attachments/assets/60454cf5-7629-4c62-b608-9c5ea85bd227" />

### 🚨 Mostrar Erros
- **Visualizador de Erros:** Lista erros atuais (sistema, eixo, motor, encoder, controlador etc.).  
- **Detalhes:** Exibe códigos de falha (`MOTOR_FAILED`, `DRV_FAULT` etc.).  
- **Controles:**  
  - **Limpar Erros:** Reseta os erros da placa.  
  - **Fechar:** Fecha a janela.  
<img width="852" height="196" src="https://github.com/user-attachments/assets/4519d4f4-87ea-4991-afcd-cc3919761d7c" />
<img width="853" height="633" src="https://github.com/user-attachments/assets/ec188fde-c88b-445c-8e7d-f1c4fdc652d5" />



## 📝 Orientações Gerais

- Esta aplicação foi testada com o firmware **ODrive v0.5.6**.  
- Em cada aba, aplique as configurações e salve antes de prosseguir para a próxima.  
- Não inicie calibrações sem antes salvar as configurações aplicadas.  
- Quando o motor estiver em **controle de malha fechada (closed-loop)**, o ODrive não permitirá salvar configurações. Nesse caso, primeiro defina o eixo como **IDLE (ocioso)** para então salvar corretamente.  
- Sempre limpe os erros antes de prosseguir com novas configurações ou operações.  



## ⚙️ Detalhes Técnicos

- Desenvolvido em **Python 3.8+** com **PySide6 (Qt for Python)**.  
- Estrutura modular: cada aba está em `tabs/`.  
- Arquivo principal: `main.py`.  
- Gráficos em tempo real com **PyQtGraph**.  
- Comunicação com ODrive via lib oficial `odrive`.  
- Compilável para `.exe` com **PyInstaller**.  

### 📦 Instalação

```bash
# 1. Clone o repositório
git clone https://github.com/marcos6497/odrive-gui-configurator.git
cd odrive-gui

# 2. Crie um ambiente virtual
python -m venv venv

# 3. Ative o ambiente (Windows PowerShell)
venv\Scripts\Activate.ps1    

# 4. Instale as dependências
pip install -r requirements.txt

# 5. Execute a aplicação
python main.py

```

## 📦 Download
👉 A versão compilada `.exe` está disponível na aba [**Releases**](https://github.com/marcos6497/odrive-gui-configurator/releases).  
Basta baixar e executar — não é necessário instalar nada.  


## 💖 Apoie este Projeto
Se este projeto foi útil para você, considere apoiar o desenvolvimento:

- [💵 PayPal](https://www.paypal.com/donate/?business=HTDDRZL6XCVSE&no_recurring=0&currency_code=BRL)  
- 📱 Pix: `98f0244d-6a9b-4201-a9a0-433db36f16c0`


## 🎥 Demonstração em Vídeo (pt-BR)
Assista à demonstração completa do **ODrive GUI Configurator** no YouTube:  

[![ODrive GUI Configurator - Video](https://img.youtube.com/vi/gNRW3H_NcU8/0.jpg)](https://www.youtube.com/watch?v=gNRW3H_NcU8)



## 📄 Licença
Este projeto utiliza bibliotecas de terceiros. Consulte [licenses](LICENSES) para mais detalhes. O código é **aberto** e está disponível neste repositório para estudo, modificação e contribuições da comunidade.  

---

## Ferramentas para Direct Drive / OpenFFBoard

Este fork adiciona uma aba **Ajustes** com medição de Kt, verificação de calibração,
limites térmicos e um preset para OpenFFBoard, além de download automático do firmware
correto.

- [Funcionalidades, com imagens](docs/FUNCIONALIDADES.md)
- [Guia de ajuste passo a passo](docs/GUIA-AJUSTE-FFB.md)
