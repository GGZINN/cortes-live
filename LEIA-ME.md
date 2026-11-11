# Cortes Live

Grava lives da **Twitch, YouTube e Kick**, detecta sozinho os momentos fortes e entrega
cortes **verticais 9:16 com legenda animada**, prontos para TikTok, Shorts e Reels.

## Como usar

1. Dê dois cliques em **`INICIAR.bat`**. O painel abre em http://localhost:8765
2. Cole o link do canal (ex.: `twitch.tv/canal`, `youtube.com/@canal/live`, `kick.com/canal`) e clique em **Monitorar**.
3. Pode deixar rodando: se o canal estiver offline, ele fica esperando e começa a gravar quando a live abrir.
4. Os cortes aparecem no painel e ficam salvos em `cortes_prontos/`.

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

Cada trecho recebe uma **nota de 0 a 10**. A **sensibilidade** define a partir de qual nota corta
(5–6 → nota 7+). No card da live, cada barra é um trecho analisado: **verde virou corte**,
amarela passou da nota mas já tinha sido cortada, cinza não valia.

**Sem a chave do Gemini**, ele usa a reserva: corta nos picos de áudio (grito, risada, explosão),
pegando 25 s antes e 10 s depois.

## Abas por streamer e os 2 melhores de cada 10

Os cortes ficam separados em **uma aba por streamer** (ex.: *alanzoka*, *Gaules*). Dentro de cada aba:

- **⭐ Para postar**: só os escolhidos. A cada **10 cortes** de um streamer (um *lote*), a IA compara
  os 10 (gancho, desfecho, se entende sozinho, emoção, duração, variedade) e escolhe os **2** que
  devem ser postados, explicando o porquê. Sem a chave do Gemini, escolhe pela maior nota.
- **Todos**: todos os cortes, separados por lote. Os que ficaram de fora aparecem apagadinhos, mas não são apagados.
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

## Situação por plataforma

Twitch, YouTube e Kick funcionam igual: a IA assiste o vídeo, então não depende do chat de
nenhuma plataforma. Na Twitch, os intervalos comerciais são pulados na gravação.

## Dicas

- **Sensibilidade**: comece em 5. Muitos cortes ruins? Baixe. Poucos cortes? Suba.
- **Legenda**: o modelo `small` é um bom equilíbrio. Sem placa de vídeo NVIDIA, `base` é mais rápido.
  Na primeira vez ele baixa o modelo (~500 MB para o `small`).
- O buffer guarda só os últimos 6 minutos de cada live em `dados/buffer`, então o disco não enche.
