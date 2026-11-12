"""IA "editor de cortes": assiste a live (imagem + áudio) com o Gemini e escolhe os trechos
com mais potencial de retenção, já com o início no gancho e o fim logo após o desfecho.

A cada PASSO segundos, pega os últimos JANELA segundos gravados, gera uma versão leve
(360p, áudio mono) e pede ao Gemini os melhores momentos com nota de 0 a 10.
"""
import os
import re
import shutil
import threading
import time
from collections import deque
from typing import List

from pydantic import BaseModel

from . import config

JANELA = 120   # segundos de live que a IA assiste por vez
PASSO = 90     # de quanto em quanto tempo analisa (sobreposição de 30 s para não perder momentos na emenda)
MODELOS = ("gemini-3.6-flash", "gemini-3.5-flash-lite")  # ambos no plano gratuito


class Parte(BaseModel):
    inicio: str          # "MM:SS" relativo ao vídeo enviado
    fim: str
    enquadramento: str   # "dividido" (câmera em cima, jogo embaixo) | "rosto" (só a câmera) | "jogo" (só o jogo)


class Momento(BaseModel):
    inicio: str          # "MM:SS" relativo ao vídeo enviado
    fim: str
    nota: int            # 0..10 potencial de retenção/viral
    tipo: str            # reação, jogada, fail, história, polêmica, inesperado, emocionante, engraçado...
    gancho: str          # o que acontece nos primeiros segundos do corte
    por_que: str         # por que segura a pessoa até o fim
    titulo: str          # sugestão de título curto
    cena: str            # "jogo" (gameplay na tela) | "conversa" (falando com o chat, câmera na tela) | "outro"
    partes: List[Parte]  # montagem: pedaços em ordem (cortes secos), cada um com o enquadramento
    texto_gancho: str    # frase grande na tela nos primeiros segundos (CAIXA ALTA)
    complemento: str     # linha menor de piada/contexto entre parênteses ("" se não tiver)


class Analise(BaseModel):
    resumo: str          # o que está acontecendo nesse trecho da live (1 frase)
    jogo: str            # nome do jogo na tela (ex.: "Mario Party Jamboree"); "" se não for jogo
    momentos: List[Momento]


SISTEMA = """Você é editor-chefe de um canal brasileiro de cortes de lives com milhões de visualizações no TikTok, YouTube Shorts e Reels.
Você assiste trechos de lives (imagem + áudio) e escolhe SÓ os momentos que viram cortes com alta retenção.
Você decide pelo que aparece na tela e pelo que é falado; o chat da live NÃO importa.

O que faz um corte reter (use como critério):
1. GANCHO IMEDIATO: o corte começa exatamente no instante em que algo já prende a atenção (uma frase forte, uma pergunta,
   um grito/reação, uma ação acontecendo na tela). Nada de começo morto, silêncio ou explicação longa antes.
2. PROMESSA E RECOMPENSA: o começo deixa a pessoa curiosa ("o que vai acontecer?") e o trecho entrega um desfecho claro
   (punchline, jogada, reação, revelação, resultado). O corte termina logo depois do desfecho, deixando 1 a 2 segundos da reação.
3. SE ENTENDE SOZINHO: quem nunca viu a live entende e acha graça/se impressiona sem precisar de contexto.
4. O QUE MAIS RETÉM: reação forte e genuína (grito, risada, susto, raiva), jogada absurda ou clutch, fail/vergonha,
   algo inesperado acontecendo na tela, história com final engraçado, opinião polêmica ou frase marcante, momento emocionante,
   interação engraçada com outra pessoa.
5. DURAÇÃO: entre 12 e 60 segundos (o ideal é 20 a 40). Corte o que for enrolação.

Evite: tela parada, menu, carregamento, anúncio, tela de "já volto", conversa morna sem desfecho, leitura de doações/subs,
momentos que só fazem sentido com o chat ou com contexto anterior.

Seja MUITO exigente: a maior parte de uma live NÃO vira corte. Nota de 0 a 10 para o potencial de retenção:
10 = vai viralizar com certeza; 8 = corte muito bom; 7 = bom; 5 = mediano; abaixo de 5 = não vale.
Se nada no trecho valer a pena, devolva "momentos" vazio. Nunca invente o que não aconteceu.
"cena" de cada momento (define o formato do vídeo): "jogo" se durante o momento a tela mostra principalmente um jogo
sendo jogado; "conversa" se o streamer está falando com o chat/a câmera e a câmera ocupa a maior parte da tela (bate-papo,
reação a algo fora do jogo, IRL); "outro" para o resto (vídeo, navegador, tela de espera).
MONTAGEM (como os canais de cortes que passam de 1 milhão de views editam):
- "partes": os pedaços deste vídeo, em ordem, que contam a história do momento. Use cortes secos para tirar espera,
  carregamento, menu sem graça, silêncio e conversa paralela, e para pular de uma tentativa para o resultado.
  Se o momento é contínuo e sem enrolação, mande 1 parte só. Cada parte com 2 s ou mais; somadas, 15 a 60 s.
  O "inicio" do momento é o da 1ª parte e o "fim" o da última.
- "enquadramento" de cada parte (só vale quando a tela tem jogo + câmera do streamer):
  "dividido" = padrão (câmera em cima, jogo embaixo); "rosto" = só a câmera em tela cheia, para a REAÇÃO forte
  (gargalhada, grito, cara de choque, fala importante olhando pra câmera); "jogo" = só o jogo em tela cheia, para um
  detalhe ou jogada que precisa ser vista grande. Use "rosto" e "jogo" com moderação, em partes curtas (1 a 4 s):
  quebre uma parte em pedaços seguidos quando o enquadramento muda. Em conversa sem jogo, tudo "dividido".
- "texto_gancho": a frase que fica grande na tela nos primeiros segundos, em CAIXA ALTA, 3 a 7 palavras, que deixa
  curioso sem entregar o final. Ex.: "NEM ELE SABE COMO CONSEGUIU FAZER ISSO", "ELE NÃO ESPERAVA ESSA RESPOSTA".
- "complemento": linha curta de piada ou contexto entre parênteses, em CAIXA ALTA (ex.: "(ESSE É O VIDEO GAME 2)",
  "(O CHAT AVISOU)"); vazio se não tiver uma boa.
"jogo": o nome oficial do jogo que aparece na tela (ex.: "Mario Party Jamboree", "Counter-Strike 2", "Minecraft"),
como as pessoas buscam no YouTube; deixe vazio se não for jogo ou se não der para saber com certeza.
Timestamps no formato MM:SS, relativos ao início DESTE vídeo. Textos em português do Brasil."""


def limiar_nota():
    """sensibilidade 1-2 → só nota 9+, 5-6 → 7+, 9-10 → 5+"""
    s = config.get("sensibilidade")
    return 9 - (max(1, min(10, s)) - 1) // 2


def _seg(ts):
    """'01:23' / '1:23.5' / '83' -> segundos"""
    ts = str(ts).strip()
    try:
        partes = [float(p) for p in re.split(r"[:]", ts)]
    except ValueError:
        return None
    s = 0.0
    for p in partes:
        s = s * 60 + p
    return s


def chave_gemini():
    return (config.get("chave_gemini").strip() or os.environ.get("GEMINI_API_KEY")
            or os.environ.get("GOOGLE_API_KEY") or "")


def analisar_video(path, contexto):
    """Manda o vídeo para o Gemini e devolve (Analise, modelo_usado)."""
    from google import genai
    from google.genai import types
    from .titulos import _schema_sem_refs, gemini_gerar

    cliente = genai.Client(api_key=chave_gemini())
    with open(path, "rb") as f:
        dados = f.read()
    partes = [
        types.Part(inline_data=types.Blob(data=dados, mime_type="video/mp4"),
                   video_metadata=types.VideoMetadata(fps=1)),
        contexto,
    ]
    cfg = types.GenerateContentConfig(
        system_instruction=SISTEMA,
        response_mime_type="application/json",
        response_json_schema=_schema_sem_refs(Analise),
        media_resolution=types.MediaResolution.MEDIA_RESOLUTION_LOW,
    )
    r = gemini_gerar(cliente, MODELOS, partes, cfg)
    if not r or not r.text:
        raise RuntimeError("resposta vazia do Gemini")
    return Analise.model_validate_json(r.text), getattr(r, "model_version", None) or MODELOS[0]


ENQUADRAMENTOS = ("dividido", "rosto", "jogo")


def _partes(partes, a, b, dur):
    """Pedaços da montagem em segundos do vídeo analisado: [(ini, fim, enquadramento)], em ordem, sem sobrepor,
    cada um com 1 s ou mais, todos dentro de [a, b]. Sem partes válidas: o momento inteiro, dividido."""
    out = []
    for p in partes or []:
        x, y = _seg(p.inicio), _seg(p.fim)
        if x is None or y is None:
            continue
        x, y = max(a, x), min(b, y, dur)
        enq = (p.enquadramento or "").strip().lower()
        enq = enq if enq in ENQUADRAMENTOS else "dividido"
        if out and x < out[-1][1]:
            x = out[-1][1]                      # não volta no tempo nem repete pedaço
        if y - x >= 1.0:
            out.append([x, y, enq])
    if not out or sum(y - x for x, y, _ in out) < 6:
        return [[a, b, "dividido"]]
    juntos = [out[0]]                           # pedaços colados com o mesmo enquadramento viram um só
    for x, y, enq in out[1:]:
        if enq == juntos[-1][2] and x - juntos[-1][1] < 0.3:
            juntos[-1][1] = y
        else:
            juntos.append([x, y, enq])
    total, limitados = 0.0, []                  # a montagem inteira cabe em 60 s
    for x, y, enq in juntos:
        if total + (y - x) > 60:
            y = x + (60 - total)
        if y - x >= 1.0:
            limitados.append([x, y, enq])
            total += y - x
        if total >= 60:
            break
    return limitados


def _limpa_gancho(t, parenteses=False):
    t = re.sub(r"\s+", " ", str(t or "")).strip().strip('"“”').upper()
    if not t:
        return None
    if parenteses:
        t = "(" + t.strip("() ") + ")"
    return t[:70]


def _sobreposicao(a0, a1, b0, b1):
    inter = max(0.0, min(a1, b1) - max(a0, b0))
    return inter / max(1e-6, min(a1 - a0, b1 - b0))


class Analisador:
    """Uma thread por live. Chama stream.criar_corte(inicio, fim, meta) para cada momento aprovado."""

    def __init__(self, stream):
        self.stream = stream
        self.status = "aguardando a live"
        self.analises = deque(maxlen=40)   # histórico para o painel
        self.cortados = deque(maxlen=200)  # (inicio, fim) já cortados
        self.proxima = JANELA              # instante da live em que roda a próxima análise
        self._stop = threading.Event()
        self.dir = os.path.join(stream.workdir, "ia")
        os.makedirs(self.dir, exist_ok=True)
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        espera_erro = 0
        while not self._stop.wait(3):
            rec = self.stream.recorder
            if rec.status != "gravando":
                self.status = "aguardando a live"
                continue
            fim = rec.covered_until()
            if fim < self.proxima:
                falta = int(self.proxima - fim)
                if not self.analises:
                    self.status = f"assistindo… 1ª análise em {falta}s"
                continue
            if not chave_gemini():
                self.status = "sem chave do Gemini"
                self._stop.wait(20)
                continue
            inicio = max(0.0, fim - JANELA)
            try:
                self.status = "analisando o trecho…"
                self._analisar(inicio, fim)
                self.proxima = fim + PASSO
                espera_erro = 0
            except Exception as e:  # noqa
                txt = f"{type(e).__name__} {e}"
                if any(x in txt for x in ("429", "RESOURCE_EXHAUSTED", "quota")):
                    self.status = "limite grátis do Gemini atingido, esperando para tentar de novo"
                    espera_erro = min(600, max(60, espera_erro * 2))
                else:
                    self.status = f"erro na análise: {type(e).__name__}"
                    espera_erro = min(300, max(30, espera_erro * 2))
                self.proxima = rec.covered_until() + espera_erro

    def _analisar(self, inicio, fim):
        from .clipper import run_ffmpeg
        rec = self.stream.recorder
        segs = rec.segments_between(inicio, fim)
        if not segs:
            return
        bruto = os.path.join(self.dir, "janela.ts")
        leve = os.path.join(self.dir, "janela.mp4")
        with open(bruto, "wb") as out:
            for s in segs:
                try:
                    with open(s["path"], "rb") as f:
                        shutil.copyfileobj(f, out)
                except OSError:
                    pass
        off = max(0.0, inicio - segs[0]["start"])
        dur = min(fim, segs[-1]["end"]) - inicio
        run_ffmpeg(["-ss", f"{off:.2f}", "-t", f"{dur:.2f}", "-i", bruto,
                    "-vf", "scale=-2:360,fps=2", "-c:v", "libx264", "-preset", "veryfast", "-crf", "32",
                    "-c:a", "aac", "-b:a", "48k", "-ac", "1", "-movflags", "+faststart", leve])

        s = self.stream
        contexto = (f"Live de {s.name} ({s.platform}). Título da live: {s.title or '-'}.\n"
                    f"Este vídeo tem {int(dur)} segundos. Encontre os momentos que viram cortes de alta retenção.")
        t0 = time.time()
        analise, modelo = analisar_video(leve, contexto)
        gasto = time.time() - t0

        limiar = limiar_nota()
        aprovados = []
        for m in sorted(analise.momentos, key=lambda m: -m.nota):
            a, b = _seg(m.inicio), _seg(m.fim)
            if a is None or b is None or b <= a:
                continue
            a, b = max(0.0, a), min(dur, b)
            if b - a < 6:
                continue
            if b - a > 60 and len(m.partes or []) <= 1:
                b = a + 60  # trecho contínuo longo demais (com várias partes, quem limita é a soma delas)
            # termina colado no fim da janela e começa depois da emenda: a próxima análise pega ele inteiro
            if b >= dur - 2 and a >= JANELA - PASSO and fim - inicio >= JANELA - 1:
                continue
            if m.nota < limiar:
                continue
            pedacos = _partes(m.partes, a, b, dur)
            a, b = pedacos[0][0], pedacos[-1][1]
            ini_abs = inicio + max(0.0, a - 0.4)       # meio segundo antes: o gancho não começa cortado
            fim_abs = min(inicio + dur, inicio + b + 1.0)  # um respiro depois do desfecho
            if any(_sobreposicao(ini_abs, fim_abs, x0, x1) > 0.4 for x0, x1 in self.cortados):
                continue
            # pedaços no tempo do corte (0 = início do corte), com a mesma folga no começo e no fim
            rel = [[round(inicio + p0 - ini_abs, 2), round(inicio + p1 - ini_abs, 2), enq] for p0, p1, enq in pedacos]
            rel[0][0] = 0.0
            rel[-1][1] = round(fim_abs - ini_abs, 2)
            self.cortados.append((ini_abs, fim_abs))
            aprovados.append(m)
            s.criar_corte(ini_abs, fim_abs, {
                "motivo": f"IA: {m.tipo}", "pontuacao": m.nota, "gancho": m.gancho,
                "por_que": m.por_que, "titulo_ia": m.titulo, "pico_rel": min(3.0, (fim_abs - ini_abs) / 2),
                "cena": (m.cena or "").strip().lower() or None,  # jogo → tela dividida; conversa → formato de sempre
                "jogo": (analise.jogo or "").strip() or None,     # entra no título, hashtags e tags do YouTube
                "partes": rel,                                    # montagem: cortes secos + enquadramento de cada pedaço
                "texto_gancho": _limpa_gancho(m.texto_gancho),
                "complemento": _limpa_gancho(m.complemento, parenteses=True),
            })

        melhor = max((m.nota for m in analise.momentos), default=0)
        self.analises.append({
            "t": round(fim), "nota": melhor, "resumo": analise.resumo, "cortes": len(aprovados),
            "modelo": modelo, "segundos": round(gasto, 1),
        })
        self.status = "assistindo a live"
