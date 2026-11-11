# Cortes Live

Grava lives da **Twitch, YouTube e Kick**, detecta sozinho os momentos fortes e entrega
cortes **verticais 9:16 com legenda animada**, prontos para TikTok, Shorts e Reels.

## Como usar

1. Dê dois cliques em **`INICIAR.bat`**. O painel abre em http://localhost:8765
2. Cole o link do canal (ex.: `twitch.tv/canal`, `youtube.com/@canal/live`, `kick.com/canal`) e clique em **Monitorar**.
3. Pode deixar rodando: se o canal estiver offline, ele fica esperando e começa a gravar quando a live abrir.
4. Os cortes aparecem no painel e ficam salvos em `cortes_prontos/`.

## Como ele decide o que cortar

A cada segundo ele compara a live com o "normal" dos últimos 5 minutos:

| Sinal | O que mede |
|---|---|
| **Chat** | mensagens por segundo explodindo |
| **Hype** | `kkkk`, `KEKW`, `LUL`, `POG`, `???`, `😂`, `💀`, "clip"... |
| **Áudio** | grito, risada, música alta (volume bem acima do normal) |

Quando a pontuação passa a linha amarela do gráfico, ele salva os **25 s antes e 10 s depois**.
Se o momento continuar quente, o corte é esticado (até 60 s). Tudo ajustável em ⚙ Configurações.
Precisa de ~1 minuto de live para "aprender" o normal antes do primeiro corte.

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

- **Twitch**: vídeo + chat ✅
- **YouTube**: vídeo + chat ✅
- **Kick**: vídeo ✅. A Kick trocou o sistema de chat dela e o novo não é público,
  então nas lives da Kick o corte é decidido **só pelo áudio**.

## Dicas

- **Sensibilidade**: comece em 5. Muitos cortes ruins? Baixe. Poucos cortes? Suba.
- **Legenda**: o modelo `small` é um bom equilíbrio. Sem placa de vídeo NVIDIA, `base` é mais rápido.
  Na primeira vez ele baixa o modelo (~500 MB para o `small`).
- O buffer guarda só os últimos 6 minutos de cada live em `dados/buffer`, então o disco não enche.
