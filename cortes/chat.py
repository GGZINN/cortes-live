"""Leitores de chat ao vivo: Twitch (IRC anônimo), YouTube (chat-downloader) e Kick (Pusher)."""
import asyncio
import json
import re
import threading
from urllib.parse import urlparse

import websockets


def platform_of(url):
    host = urlparse(url if "://" in url else "https://" + url).netloc.lower()
    if "twitch.tv" in host:
        return "twitch"
    if "youtube.com" in host or "youtu.be" in host:
        return "youtube"
    if "kick.com" in host:
        return "kick"
    return "outro"


def slug_of(url):
    path = urlparse(url if "://" in url else "https://" + url).path.strip("/")
    return path.split("/")[0].lower() if path else ""


class ChatReader:
    def __init__(self, url, on_message, log=print):
        self.url = url
        self.platform = platform_of(url)
        self.on_message = on_message
        self.log = log
        self.status = "desligado"
        self.video_url = None  # o YouTube precisa da URL do vídeo (vem do yt-dlp)
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            try:
                if self.platform == "twitch":
                    asyncio.run(self._twitch())
                elif self.platform == "kick":
                    asyncio.run(self._kick())
                elif self.platform == "youtube":
                    self._youtube()
                else:
                    self.status = "sem suporte"
                    return
            except Exception as e:  # noqa
                self.status = f"erro: {str(e)[:120]}"
            self._stop.wait(15)

    # ---------------- Twitch ----------------
    async def _twitch(self):
        channel = slug_of(self.url)
        async with websockets.connect("wss://irc-ws.chat.twitch.tv:443", ping_interval=60) as ws:
            await ws.send("PASS SCHMOOPIIE")
            await ws.send("NICK justinfan%d" % (hash(channel) % 90000 + 10000))
            await ws.send(f"JOIN #{channel}")
            self.status = "conectado"
            while not self._stop.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=2)
                except asyncio.TimeoutError:
                    continue
                for line in raw.split("\r\n"):
                    if line.startswith("PING"):
                        await ws.send("PONG :tmi.twitch.tv")
                    elif " PRIVMSG " in line:
                        self.on_message(line.split(" :", 1)[-1] if " :" in line else "")

    # ---------------- Kick ----------------
    async def _kick(self):
        chatroom_id = await asyncio.to_thread(self._kick_chatroom_id)
        if not chatroom_id:
            self.status = "indisponível · cortando só pelo áudio"
            return
        url = "wss://ws-us2.pusher.com/app/32cbd69e4b950bf97679?protocol=7&client=js&version=8.4.0&flash=false"
        async with websockets.connect(url, ping_interval=60) as ws:
            first = json.loads(await ws.recv())
            if first.get("event") == "pusher:error":
                # a Kick trocou o servidor de chat: segue só com o áudio e tenta de novo mais tarde
                self.status = "indisponível · cortando só pelo áudio"
                self._stop.wait(600)
                return
            await ws.send(json.dumps({"event": "pusher:subscribe",
                                      "data": {"auth": "", "channel": f"chatrooms.{chatroom_id}.v2"}}))
            self.status = "conectado"
            while not self._stop.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=2)
                except asyncio.TimeoutError:
                    continue
                msg = json.loads(raw)
                ev = msg.get("event", "")
                if ev == "pusher:ping":
                    await ws.send(json.dumps({"event": "pusher:pong", "data": {}}))
                elif "ChatMessage" in ev:
                    try:
                        data = json.loads(msg.get("data") or "{}")
                        self.on_message(data.get("content", ""))
                    except Exception:
                        self.on_message("")

    def _kick_chatroom_id(self):
        import httpx
        slug = slug_of(self.url)
        r = httpx.get(f"https://kick.com/api/v2/channels/{slug}",
                      headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"}, timeout=15)
        return (r.json().get("chatroom") or {}).get("id")

    # ---------------- YouTube ----------------
    def _youtube(self):
        import time
        import httpx
        m = re.search(r"(?:v=|youtu\.be/|/live/)([\w-]{11})", self.video_url or self.url)
        if not m:
            self.status = "erro: link do vídeo não encontrado"
            return
        vid = m.group(1)
        ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
        with httpx.Client(timeout=20, headers={"User-Agent": ua, "Accept-Language": "pt-BR,pt;q=0.9"}) as c:
            html = c.get(f"https://www.youtube.com/live_chat?is_popout=1&v={vid}").text
            key = re.search(r'"INNERTUBE_API_KEY":"([^"]+)"', html)
            ver = re.search(r'"INNERTUBE_CONTEXT_CLIENT_VERSION":"([^"]+)"', html)
            cont = re.search(r'"continuation":"([^"]+)"', html)
            if not (key and ver and cont):
                self.status = "erro: chat do YouTube indisponível"
                return
            key, ver, cont = key.group(1), ver.group(1), cont.group(1)
            self.status = "conectado"
            while not self._stop.is_set():
                r = c.post(f"https://www.youtube.com/youtubei/v1/live_chat/get_live_chat?key={key}",
                           json={"context": {"client": {"clientName": "WEB", "clientVersion": ver}},
                                 "continuation": cont})
                data = r.json().get("continuationContents", {}).get("liveChatContinuation", {})
                for act in data.get("actions", []):
                    item = act.get("addChatItemAction", {}).get("item", {})
                    rend = item.get("liveChatTextMessageRenderer") or item.get("liveChatPaidMessageRenderer")
                    if not rend:
                        continue
                    runs = rend.get("message", {}).get("runs", [])
                    text = "".join(x.get("text") or (x.get("emoji", {}).get("shortcuts") or [""])[0] for x in runs)
                    self.on_message(text)
                conts = data.get("continuations") or []
                if not conts:
                    self.status = "chat encerrado"
                    return
                cd = next(iter(conts[0].values()))
                cont = cd.get("continuation", cont)
                wait = min(max(cd.get("timeoutMs", 3000) / 1000, 1.0), 8.0)
                self._stop.wait(wait)


# Palavras/emotes que indicam momento engraçado, épico ou polêmico
HYPE = re.compile(
    r"(k{3,}|kek|lul|lmao|lol|icant|rsrs|hah(a)+|jaja|"
    r"pog|\bw+\b|monkas|wtf|caralho|krl|\bmds\b|meu deus|nossa|"
    r"\?{2,}|!{3,}|clip|corta|corte|😂|🤣|💀|😭|🔥|😱)",
    re.IGNORECASE,
)


def hype_score(text):
    if not text:
        return 0
    return min(3, len(HYPE.findall(text)))
