# Cortes Live

Grava lives da **Twitch, YouTube e Kick**, detecta sozinho os momentos fortes e entrega
cortes **verticais 9:16 com legenda animada**, prontos para TikTok, Shorts e Reels.

## Como usar

1. Dê dois cliques em **`INICIAR.bat`**. O painel abre em http://localhost:8765
2. Cole o link do canal (ex.: `twitch.tv/canal`, `youtube.com/@canal/live`, `kick.com/canal`) **ou de um vídeo já publicado** (ex.: `youtube.com/watch?v=...`, um VOD da Twitch) e clique em **Adicionar**.
3. Pode deixar rodando: se o canal estiver offline, ele fica esperando e começa a gravar quando a live abrir.
4. Os cortes aparecem no painel e ficam salvos em `cortes_prontos/`.

## Cortes de vídeos (não só de lives)

O mesmo campo aceita o link de um vídeo já publicado. O programa percebe sozinho que não é uma live, baixa o vídeo
(até 1080p; acima de 2 horas, 720p), e a IA assiste tudo em blocos de 2 minutos, sem esperar o tempo passar.
Daí em diante é igual à live: mesmos critérios de nota, mesma montagem, legenda, títulos e curadoria.

- O cartão mostra `analisado X de Y`. Um vídeo de 1 hora leva uns 20 a 40 minutos (depende do limite do Gemini).
- Quando o vídeo acaba, o lote daquele canal é fechado na hora e a IA escolhe os melhores, mesmo sem chegar a 10 cortes.
- Se o programa fechar no meio, o vídeo continua de onde parou (não repete cortes). Vídeo terminado não é refeito;
  para refazer, tire do painel e cole o link de novo.
- Precisa da chave do Gemini (no modo só áudio o vídeo não é cortado). O link de um canal (`youtube.com/@canal`) não
  conta como vídeo: cole o link do vídeo.

## Como ele decide o que cortar: a IA assiste a live

Com a chave do Gemini (grátis, veja abaixo), **a IA assiste a live**, imagem e áudio, em blocos
de 2 minutos (um a cada 1min30, com sobreposição para não perder nada na emenda). Ela escolhe
os trechos pelo que **aparece na tela e é falado**. O chat não é usado.

Critérios que ela segue (do que mais segura gente em TikTok/Shorts/Reels):

1. **Gancho imediato**: o corte começa no instante em que algo já prende (frase forte, grito, ação na tela), sem começo morto.
2. **Promessa e recompensa**: o começo deixa curioso e o trecho entrega um desfecho. O corte termina logo depois dele.
3. **Se entende sozinho**, sem precisar ter visto a live.
4. **O que mais retém**: reação forte e genuína, jogada absurda, fail/vergonha, algo inesperado, história com final engraçado, frase polêmica ou marcante, momento emocionante.
5. **12 a 60 s** (ideal 20 a 40).
6. **Evita**: tela parada, menu, carregamento, anúncio, leitura de doações, conversa morna.

Cada trecho recebe uma **nota de 0 a 10**, que é a soma de critérios fixos:
- abre uma lacuna de curiosidade: 0 a 3;
- tem reação, conflito ou opinião forte: 0 a 2;
- mostra algo específico (número, nome, jogada): 0 a 2;
- entra direto: 0 a 1;
- tem o desfecho dentro do corte: 0 a 1;
- se entende sozinho: 0 a 1.

Cortes da mesma live não repetem o mesmo texto de gancho. A **sensibilidade** define a partir de qual nota corta
(5–6 → nota 7+). No card da live, cada barra é um trecho analisado: **verde virou corte**,
amarela passou da nota mas já tinha sido cortada, cinza não valia.

**Tipos de momento.** A IA repara no que o streamer fala, em como ele age e em como ele joga, e cada tipo tem o seu
jeito de cortar: notícia ou assunto em alta, opinião forte, conversa engraçada, história, reação, jogada ou fail,
dica ou detalhe do jogo. Na notícia, por exemplo, o corte começa na frase que anuncia o assunto e termina no
veredito do streamer.

**Base de conhecimento.** Antes de dar nota, a IA recebe o que se sabe sobre os assuntos da live:
- **Pesquisa:** a cada análise ela anota até 3 assuntos falados (uma notícia, um jogo, uma pessoa, uma polêmica).
  O programa pesquisa cada um em segundo plano no Google Notícias (manchetes dos últimos 7 dias) e na Wikipédia,
  o Gemini resume e o resumo vale por 12 horas. A análise seguinte da mesma live já usa.
- **Notas fixas:** arquivos `.md` em `cortes/base/` (vêm com o programa; hoje, `gta6.md`) e em `dados/base/`
  (as suas). A primeira linha diz quando a nota vale, por exemplo `chaves: gta 6, vice city`. Ela entra quando
  uma das chaves aparece no título da live, no jogo ou nos assuntos falados.
- Um momento sobre um assunto em alta ganha 1 ponto na nota. A base só ajuda a IA a entender o assunto: o corte
  continua mostrando só o que foi dito e mostrado na live.
- Para desligar a pesquisa: ⚙ Configurações → **Pesquisar os assuntos da live**. O que está na base aparece em
  `http://localhost:8765/api/conhecimento`.

**GTA 6.** A nota `gta6.md` ensina o que vira corte em live de GTA 6 (primeira reação, comparação com o GTA 5,
detalhe escondido, caos e bug, dica, opinião, missão) e o cuidado com spoiler: o texto de gancho nunca entrega o
desfecho da história.

**Reserva com a OpenAI.** O plano grátis do Gemini tem limite diário. Com uma chave da OpenAI em ⚙ Configurações →
**Chave da OpenAI**, quando o limite do Gemini acaba o mesmo pedido vai para a OpenAI (escolha dos cortes, títulos,
tela dividida, curadoria, thumbnails, roteiros e pesquisa de assuntos), e o programa volta a tentar o Gemini a cada
30 minutos. A API da OpenAI é paga. Ela não recebe vídeo: o trecho da live vai como um quadro a cada 2 s mais a
transcrição do áudio, então ela vê e lê, mas não ouve o tom da voz. No card da live, o modelo da análise aparece
como `openai:...` quando a reserva está em uso.

**Sem a chave do Gemini**, ele usa a reserva: corta nos picos de áudio (grito, risada, explosão),
pegando 25 s antes e 10 s depois.

## Formato do corte: tela dividida no gameplay

A regra é:

- **Jogo** (gameplay com a câmera do streamer num canto): tela dividida, ocupando a tela toda, com a
  **câmera em cima** (40%) e o **jogo embaixo** (60%). O jogo recebe um pouco de zoom pelo centro (a mira continua
  no meio) até a janela da câmera sair dele.
- **Streamer conversando com o chat** (a câmera ocupa a tela): o formato de sempre, com a live inteira no meio e
  fundo desfocado. Cenas sem câmera separada também ficam assim.

Quem decide a cena é a IA que assiste a live: para cada corte ela diz se é jogo ou conversa. No jogo, outra
chamada da IA olha um quadro do corte e marca onde está a câmera; essa posição vale para os próximos 15 minutos
de live do mesmo streamer. Sem a chave do Gemini, todos ficam no formato de sempre. Para desligar a tela dividida:
⚙ Configurações → **Formato do corte** → *Sempre a live inteira no meio*.

**Selo da live**: um selo pequeno com o logo da plataforma (Kick, Twitch ou YouTube) e o link da live
(ex.: `kick.com/coringa`) no meio do vídeo: na emenda entre a câmera e o jogo, ou logo acima da live no formato
de sempre. Dá para esconder em ⚙ Configurações → **Selo da live**.

**Ritmo:**
- **Pausas cortadas.** A pausa entre frases acima de 0,3 s vira 0,2 s, o silêncio morto no meio da frase vira 0,25 s, e o corte entra direto na primeira palavra. Só sai o silêncio de verdade: risada, grito e som alto do jogo ficam, e o final com a reação também fica. No gameplay as pausas não são cortadas, porque nelas a jogada costuma estar acontecendo.
- **Cortes com zoom.** A imagem do streamer alterna entre 100% e 110% a cada ~2,8 s e em cada emenda. É um jump cut seco, sem efeito, e sempre cai no começo de uma palavra. O jogo não muda de zoom.
- **Volume.** Todo corte sai em −14 LUFS, o padrão dos Shorts, com o pico abaixo de −1,5 dB.

Dá para desligar as pausas e o zoom em ⚙ Configurações.

Os cortes divididos guardam também uma cópia 16:9 só da live (`live.mp4`, ~6 MB), usada nas thumbnails e nos
compilados, porque o corte vertical não tem mais a live inteira.

## Abas por streamer e os 2 melhores de cada 10

Os cortes ficam separados em **uma aba por streamer** (ex.: *alanzoka*, *Gaules*). Dentro de cada aba:

- **⭐ Para postar**: só os escolhidos. A cada **10 cortes** de um streamer (um *lote*), a IA compara
  os 10 (gancho, desfecho, se entende sozinho, emoção, duração, variedade) e escolhe os **2** que
  devem ser postados, explicando o porquê. Sem a chave do Gemini, escolhe pela maior nota.
- **Todos**: todos os cortes, separados por lote. Fechado o lote, os que ficaram de fora são **apagados do disco sozinhos**
  (dá para desligar em ⚙ Configurações → *Cortes que a IA não escolheu*). Cortes que você agendou, postou ou usou num
  roteiro nunca são apagados. O link *Apagar agora os não escolhidos que já existem*, no mesmo lugar, limpa os antigos.
- **Escolher agora**: escolhe os 2 melhores do lote atual sem esperar completar 10.

## Publicar (títulos + passo a passo)

Em cada corte pronto, clique em **Publicar**:

- **Títulos sugeridos**: 5 opções; clique em uma para usar.
- Escolha a plataforma: **TikTok, YouTube Shorts, Instagram Reels ou Kwai**. Para cada uma vem
  a **legenda pronta** (título + texto + créditos do streamer + hashtags), com botão de copiar.
  No Shorts, título (contador de 100 caracteres) e descrição vêm separados.
- **Baixar vídeo** (o arquivo já sai com o nome do título) e **Abrir** a página de envio da plataforma.
- **Como postar**: o passo a passo de cada plataforma e dicas.

Sem configurar nada, os títulos são montados a partir da frase dita no pico e da reação do chat.

### Títulos por IA de graça (Gemini)

1. Entre em https://aistudio.google.com/apikey com sua conta Google.
2. Clique em **Create API key** e copie a chave (começa com `AIza`).
3. No painel: ⚙ Configurações → **Chave do Gemini** → cole → Salvar.

Pronto: os próximos cortes já saem com títulos escritos pela IA. Nos cortes antigos, abra
**Publicar** e clique em **↻ Gerar de novo**. Usa o `gemini-3.6-flash` e, se o limite gratuito
dele estourar, o `gemini-3.5-flash-lite` (os dois são do plano grátis). No plano grátis o Google
pode usar o texto enviado (transcrição do corte) para melhorar os produtos dele.

Se preferir a Claude (paga, centavos por corte), use o campo *Chave da Claude*. Ela só é usada
quando não há chave do Gemini. As chaves ficam salvas só neste computador, em `dados/settings.json`.

## Postar e agendar direto no YouTube

Conecte seu canal uma vez e envie os cortes sem baixar nada: **Publicar** → aba **YouTube Shorts** →
**Enviar ao YouTube**. Dá para publicar na hora, deixar não listado/privado ou **agendar**: o próprio
YouTube publica na hora marcada, mesmo com o PC desligado.

Configurar (uma vez, ~10 min, grátis). O botão **▶ Conectar YouTube** no topo do painel tem os links:

1. Crie um projeto no [Google Cloud](https://console.cloud.google.com/projectcreate).
2. Ative a **YouTube Data API v3** e a **YouTube Analytics API**.
3. Em **Google Auth Platform → Branding**, clique em *Começar* (nome do app, seu e-mail, público *Externo*).
4. Em **Público-alvo**, clique em **Publicar app**. Em modo *Teste*, o login expira a cada 7 dias.
5. Em **Clientes → Criar cliente**, escolha **App para computador** e baixe o JSON.
6. No painel: **▶ Conectar YouTube** → envie o JSON → **Conectar canal**. Na tela do Google, se aparecer
   "O Google não verificou este app", clique em *Avançado → Acessar*. O app é seu.

**Importante:** o Google deixa **privados** os vídeos enviados por projetos novos até eles passarem na
[auditoria da API do YouTube](https://support.google.com/youtube/contact/yt_api_form) (grátis, leva alguns
dias). Peça logo depois de configurar. Enquanto isso, o painel avisa e você muda a visibilidade pelo Studio.
A cota grátis da API limita os envios a poucos por dia (dá para pedir aumento no mesmo formulário da
auditoria); ela volta à meia-noite do Pacífico (4h–5h em Brasília).

A credencial e o login ficam só neste computador, em `dados/`.

## Agenda do YouTube

Na aba **📅 Agenda do YouTube** você define os **horários de postagem** (ex.: 12:00, 18:00, 21:00) e os
**dias da semana**. A partir daí:

- **Preencher sozinho**: os cortes ⭐ escolhidos entram sozinhos nos horários livres dos próximos dias,
  os mais recentes primeiro e sem repetir o mesmo streamer em sequência.
- Para pôr um corte específico: **+ Escolher corte** num horário livre, ou **Publicar → YouTube Shorts →
  📅 Próximo horário da agenda** (usa o título e a descrição que estiverem na tela).
- Cada vídeo **sobe ao YouTube algumas horas antes** do horário (padrão: 3 h), como privado com data de
  publicação; o YouTube publica sozinho na hora. Até subir, dá para mudar o título, o horário ou tirar da agenda.
- Depois de subir, ainda dá para mudar o horário; tirar da agenda deixa o vídeo no canal como privado.
- Se a cota da API acabar, a agenda pausa e volta sozinha quando a cota renova. Se um horário passar
  sem o vídeo ter subido (PC desligado, por exemplo), ele vai para o próximo horário livre.
- O painel precisa estar aberto para os vídeos subirem; depois de subirem, o PC pode ficar desligado.

## Thumbnails

Em **Publicar**, na coluna do vídeo, clique em **🖼 Criar thumbnail**. O app tira 8 quadros do corte
(só a imagem da live, sem a legenda) e monta **3 opções** em dois formatos:

- **16:9 (1280×720)**: a thumbnail do YouTube.
- **9:16 (1080×1920)**: capa vertical para subir no TikTok, Reels e Kwai.

Com a chave do Gemini, a IA escolhe os quadros com a reação mais forte e escreve os textos (2 a 4 palavras).
Sem a chave, usa o quadro mais nítido perto do pico e textos tirados do título.

No editor dá para escolher uma das opções ou **personalizar**: trocar o quadro, o texto, a cor e o layout
(texto ao lado, embaixo ou em cima) e clicar em **Montar**.

Ao enviar ao YouTube (na hora ou pela agenda), a thumbnail escolhida vai junto; se o corte ainda não tiver
uma, ela é gerada na hora. Dá para desligar isso em **▶ YouTube**. Thumbnail personalizada exige o canal
**verificado por telefone** (youtube.com/verify). No Shorts, ela aparece no canal e na busca; no feed de
Shorts o YouTube mostra o próprio vídeo.

## Roteiros

Na aba **📝 Roteiros**, clique em **+ Novo roteiro** e escolha o tipo:

- **📱 Short**: roteiro de 30 a 60 s para gravar. Gancho, blocos curtos com fala e o que aparece na tela, e
  chamada final.
- **🎙 Vídeo original**: roteiro completo de um vídeo longo (3 a 15 min) sobre um tema, pronto para ler.
- **🎬 Compilado**: escolha de 2 a 20 cortes prontos (os ⭐ escolhidos aparecem primeiro). A IA ordena os
  cortes (começa forte, deixa o melhor para o fim), dá nome aos capítulos e escreve as pontes.

Tudo fica salvo e editável: título (com 5 opções), texto da thumbnail, gancho, partes (mudar a ordem, editar,
remover), encerramento, descrição e tags. Dá para **copiar** o roteiro ou baixar em **.txt**.

No compilado, **🎬 Montar vídeo** gera o vídeo 16:9 sozinho: cartela de abertura com o título, uma cartela
por capítulo (#N, contagem regressiva), o corte (só a imagem da live, sem a legenda) e uma cartela final.
Os **capítulos com os tempos** entram sozinhos na descrição, junto com os créditos dos streamers. O app
também monta a thumbnail 16:9 e dá para **enviar ao YouTube** (na hora, agendado, não listado ou privado).

A IA escreve a partir do tema; ela não assiste vídeos nem pesquisa na internet. O que você precisar conferir
ela deixa [entre colchetes]. Sem chave de IA, o app cria um modelo em branco para preencher.

## Painel do canal

A aba **📊 Canal** mostra o canal do YouTube conectado:

- **Números**: inscritos, views, horas assistidas, retenção média e likes dos últimos 28 dias, e um gráfico de
  views por dia (passe o mouse numa barra para ver o dia). O YouTube Analytics leva 2 a 3 dias para fechar os
  números, então os últimos dias aparecem baixos.
- **Vídeos**: todos os vídeos do canal (até 200) com views, likes e retenção, em ordem de recentes, mais vistos
  ou retenção. Os que saíram do app aparecem marcados (corte do streamer X ou roteiro).
- **✎ Editar**: muda título, descrição, tags e visibilidade de um vídeo já publicado, direto no YouTube. O resto
  das configurações do vídeo fica como está. Cada edição gasta 51 das 10.000 unidades da cota diária.
- **🧠 Aprendizado**: com pelo menos 5 vídeos públicos com 2 dias ou mais, o app compara os vídeos e mostra o que
  repetir, o que evitar, os mais vistos e os melhores horários para postar (com botão para usar na agenda).
  Essas lições entram sozinhas nas instruções da IA ao escrever os títulos e ao escolher os 2 melhores de cada 10.
  - **Aprender com**: por padrão, só os vídeos postados pelo Cortes Live. Se o canal só posta cortes, escolha
    **Todos os vídeos do canal** para aproveitar o histórico. Num canal com outro tipo de conteúdo, deixe no
    padrão: senão a IA aprende com vídeos que não têm nada a ver com os cortes.
  - **Usar nas instruções da IA**: desmarque para ver as lições sem que elas mudem os títulos.
  - A sugestão de horários só aparece quando dá para comparar: 4 horários ou mais com 2 vídeos ou mais cada.

Os dados são atualizados sozinhos a cada 6 horas (ou no botão **↻ Atualizar**, que gasta umas 10 unidades da
cota). Retenção, horas assistidas e o gráfico precisam da **YouTube Analytics API** ativada no projeto do Google
Cloud (passo 2 da configuração do YouTube). Sem ela, o painel mostra o resto e avisa.

## Situação por plataforma

Twitch, YouTube e Kick funcionam igual: a IA assiste o vídeo, então não depende do chat de
nenhuma plataforma. Na Twitch, os intervalos comerciais são pulados na gravação.

## Dicas

- **Sensibilidade**: comece em 5. Muitos cortes ruins? Baixe. Poucos cortes? Suba.
- **Legenda**: o modelo `small` é um bom equilíbrio. Sem placa de vídeo NVIDIA, `base` é mais rápido.
  Na primeira vez ele baixa o modelo (~500 MB para o `small`).
- **Placa de vídeo**: a legenda só usa a placa NVIDIA se as bibliotecas do CUDA 12 (cuBLAS e cuDNN)
  estiverem instaladas. Se não estiverem, ela passa sozinha para o processador (mais lento, mas funciona).
- **Espaço em disco**: cada corte guarda o vídeo bruto (~20–40 MB) até ficar pronto, e o corte pronto ocupa
  ~20 MB. Com menos de **1 GB** livre, o app para de criar cortes novos; com menos de 512 MB, a gravação
  espera; com menos de 200 MB, a fila de processamento espera. Tudo volta sozinho quando liberar espaço.
  Se um corte falhar, use **↻ Tentar de novo** no card.
- O buffer guarda só os últimos 6 minutos de cada live em `dados/buffer`, então o disco não enche.
