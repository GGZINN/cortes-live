# Política de Privacidade — Cortes Live

*English version below.*

Última atualização: 12 de novembro de 2026

O **Cortes Live** é um programa de código aberto que roda **no computador de quem o usa**. Ele cria cortes
de lives e, se a pessoa conectar o próprio canal, envia e agenda vídeos no YouTube e mostra os números do canal.
Não existe servidor do Cortes Live: o desenvolvedor **não recebe nenhum dado** de quem usa o programa.

## O Cortes Live usa os YouTube API Services

Para enviar vídeos, agendar postagens, editar vídeos e mostrar as estatísticas do canal, o Cortes Live usa os
**YouTube API Services** (YouTube Data API v3 e YouTube Analytics API). Ao conectar o canal, você também está
sujeito à **Política de Privacidade do Google**: <https://policies.google.com/privacy> e aos
**Termos de Serviço do YouTube**: <https://www.youtube.com/t/terms>.

## Quais dados o programa acessa e para quê

Só depois que você conecta o seu canal (login do Google com OAuth) e só do **seu** canal:

| Dado | Para quê |
|---|---|
| Nome, foto e números do canal (inscritos, views, quantidade de vídeos) | Mostrar no painel do canal |
| Lista dos seus vídeos: título, descrição, tags, visibilidade, data, duração, views, likes, comentários (contagem) | Mostrar a lista, permitir editar e comparar o desempenho |
| Relatórios do YouTube Analytics (views, tempo assistido, retenção, inscritos ganhos/perdidos) | Mostrar o painel e as views por dia |
| Envio de vídeos, thumbnails e alterações (título, descrição, tags, visibilidade, data de publicação) | Fazer o que você pediu no painel ou programou na agenda |

O programa **não** acessa comentários, mensagens, dados de pagamento, nem dados de outros canais.

## Onde os dados ficam

Tudo fica **somente no seu computador**, na pasta `dados/` do programa:

- `youtube_token.json`: a autorização do Google (o "login"). Só é usada para falar com o Google.
- `canal.json`: a cópia local dos números e da lista de vídeos do canal. É atualizada a cada 6 horas enquanto
  o canal está conectado e **apagada** quando você desconecta o canal. Se ela ficar mais de 30 dias sem ser
  atualizada, o programa a apaga sozinho.

Nada disso é enviado ao desenvolvedor nem a ninguém além dos serviços listados abaixo.

## Com quem os dados são compartilhados

- **Google / YouTube**: os vídeos e as informações que você manda publicar ou alterar.
- **Google Gemini API** (se você colocar a sua chave do Gemini): para escrever títulos e o "aprendizado" do
  canal, o programa envia ao Gemini a transcrição dos cortes e os títulos e números públicos dos seus vídeos
  (views, likes, retenção). O envio usa a **sua** chave e segue os termos do Google para a Gemini API.
- **Anthropic Claude API** (só se você colocar a sua chave da Claude e não houver chave do Gemini): o mesmo
  tipo de informação, para escrever títulos.

O programa não vende, não aluga e não usa os seus dados para publicidade. Não usa cookies, nem rastreamento.

## Como apagar os dados e revogar o acesso

- No painel: **▶ YouTube → Desconectar**. Isso apaga a autorização e a cópia local dos dados do canal.
- No Google: revogue o acesso do app a qualquer momento em <https://myaccount.google.com/permissions>
  (Configurações de segurança da Conta do Google).
- Apagar a pasta `dados/` remove tudo o que o programa guardou.

## Contato

Dúvidas ou pedidos: <https://github.com/GGZINN/cortes-live/issues>

---

# Privacy Policy — Cortes Live (English)

Last updated: November 12, 2026

**Cortes Live** is open-source software that runs **on the user's own computer**. It creates short clips from
live streams and, if the user connects their own channel, uploads and schedules videos on YouTube and shows the
channel's statistics. There is no Cortes Live server: the developer **does not receive any data** from users.

## Cortes Live uses YouTube API Services

To upload videos, schedule posts, edit videos and show channel statistics, Cortes Live uses the
**YouTube API Services** (YouTube Data API v3 and YouTube Analytics API). By connecting your channel you are also
subject to the **Google Privacy Policy**: <https://policies.google.com/privacy> and the
**YouTube Terms of Service**: <https://www.youtube.com/t/terms>.

## What data is accessed and why

Only after you connect your channel (Google sign-in with OAuth), and only for **your own** channel:

| Data | Purpose |
|---|---|
| Channel name, picture and statistics (subscribers, views, video count) | Show them in the channel dashboard |
| Your video list: title, description, tags, privacy status, date, duration, view/like/comment counts | Show the list, let you edit videos and compare performance |
| YouTube Analytics reports (views, watch time, average view percentage, subscribers gained/lost) | Show the dashboard and daily views |
| Uploading videos and thumbnails and updating metadata (title, description, tags, privacy, publish time) | Do what you requested in the app or scheduled in its calendar |

The software does **not** access comments, messages, payment data, or other channels' data.

## Where data is stored

Everything is stored **only on your computer**, in the program's `dados/` folder:

- `youtube_token.json`: the Google authorization token. It is only used to call Google's APIs.
- `canal.json`: a local copy of the channel statistics and video list. It is refreshed every 6 hours while the
  channel is connected and **deleted** when you disconnect the channel. If it is not refreshed for 30 days, the
  software deletes it automatically.

None of this is sent to the developer or to anyone other than the services listed below.

## Who data is shared with

- **Google / YouTube**: the videos and information you choose to publish or change.
- **Google Gemini API** (only if you add your own Gemini API key): to write titles and the channel "learning"
  insights, the software sends clip transcripts and your videos' titles and public statistics (views, likes,
  average view percentage) to Gemini, using **your** key and under Google's Gemini API terms.
- **Anthropic Claude API** (only if you add your own Claude key and no Gemini key is set): the same kind of
  information, to write titles.

The software does not sell, rent, or use your data for advertising. It uses no cookies and no tracking.

## How to delete data and revoke access

- In the app: **▶ YouTube → Disconnect** deletes the authorization token and the local copy of channel data.
- At Google: revoke the app's access at any time at <https://myaccount.google.com/permissions>
  (Google Account security settings).
- Deleting the `dados/` folder removes everything the software stored.

## Contact

Questions or requests: <https://github.com/GGZINN/cortes-live/issues>
