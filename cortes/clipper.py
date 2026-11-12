"""Transforma um trecho da live em corte vertical 9:16 com legenda animada."""
import gc
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import traceback

from . import config, curadoria, layout, titulos

FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
INDEX = os.path.join(config.CLIPS_DIR, "cortes.json")

ERRO_GPU = re.compile(r"cublas|cudnn|cuda", re.I)  # falhas da placa de vídeo: o corte pode ser refeito


def _bruto(cid):
    return os.path.join(config.CLIPS_DIR, cid, "bruto.ts")


FECHADO = "programa foi fechado durante o processamento"
_lock = threading.Lock()
clips = []  # lista de dicts (mais novo primeiro)
_retomar = []  # cortes interrompidos que ainda têm o vídeo bruto: voltam para a fila ao abrir o programa
_sem_titulo = []  # interrompidos depois do vídeo pronto (só faltava o título): viram "pronto" e o título é escrito
if os.path.exists(INDEX):
    try:
        with open(INDEX, encoding="utf-8") as f:
            clips = json.load(f)
        for c in clips:
            interrompido = c["status"] not in ("pronto", "erro") or (c["status"] == "erro" and c.get("erro") == FECHADO)
            falha_gpu = c["status"] == "erro" and ERRO_GPU.search(str(c.get("erro") or ""))
            if not (interrompido or falha_gpu):
                continue
            if os.path.exists(_bruto(c["id"])):
                c["status"], c["erro"] = "na fila", None
                _retomar.append(c["id"])
            elif interrompido and c.get("arquivo") and os.path.exists(os.path.join(config.CLIPS_DIR, c["id"], "corte.mp4")):
                c["status"], c["erro"] = "pronto", None
                _sem_titulo.append(c["id"])
            elif interrompido:
                c["status"], c["erro"] = "erro", FECHADO
    except Exception:
        clips = []


def _save():
    config.salvar_json(INDEX, clips, indent=1)


if curadoria.preencher_lotes_antigos(clips):  # cortes de antes dos lotes existirem
    _save()


def update_clip(cid, **kw):
    with _lock:
        for c in clips:
            if c["id"] == cid:
                c.update(kw)
        _save()


def add_clip(clip):
    with _lock:
        curadoria.atribuir_lote(clip, clips)
        clips.insert(0, clip)
        _save()


def list_clips():
    with _lock:
        return [dict(c) for c in clips]


def get_clip(cid):
    with _lock:
        for c in clips:
            if c["id"] == cid:
                return dict(c)
    return None


def delete_clip(cid):
    with _lock:
        for c in list(clips):
            if c["id"] == cid:
                clips.remove(c)
                shutil.rmtree(os.path.join(config.CLIPS_DIR, cid), ignore_errors=True)
        _save()


def run_ffmpeg(args, cwd=None):
    p = subprocess.run([config.ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", *args],
                       cwd=cwd, capture_output=True, creationflags=FLAGS)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode(errors="ignore")[-600:])


# ------------------------------------------------------------------ Whisper
_model = None
_model_name = None
_model_dev = None
_sem_gpu = False  # a placa já falhou uma vez (ex.: faltam as DLLs do CUDA): só processador até fechar o programa
_model_lock = threading.Lock()


def _whisper():
    """Devolve (modelo, "cuda"|"cpu"). Usa a placa de vídeo NVIDIA se der, senão o processador."""
    global _model, _model_name, _model_dev
    name = config.get("modelo_whisper")
    with _model_lock:
        if _model is None or _model_name != name:
            from faster_whisper import WhisperModel
            _model = None
            gpu = False
            if not _sem_gpu:
                try:
                    import ctranslate2
                    gpu = ctranslate2.get_cuda_device_count() > 0
                except Exception:
                    gpu = False
            if gpu:
                try:
                    _model, _model_dev = WhisperModel(name, device="cuda", compute_type="float16"), "cuda"
                except Exception:
                    _model = None
            if _model is None:
                _model, _model_dev = WhisperModel(name, device="cpu", compute_type="int8"), "cpu"
            _model_name = name
        return _model, _model_dev


def _palavras(modelo, audio, lang):
    segs, _ = modelo.transcribe(
        audio, language=None if lang == "auto" else lang, word_timestamps=True,
        vad_filter=True, beam_size=1,
    )
    words = []
    for s in segs:  # a transcrição roda aqui (é um gerador): erro da placa só aparece neste ponto
        for w in s.words or []:
            txt = w.word.strip()
            if txt:
                words.append({"w": txt, "s": float(w.start), "e": float(w.end)})
    return words


def transcribe(pcm_path):
    global _model, _sem_gpu
    import numpy as np
    audio = np.fromfile(pcm_path, dtype=np.int16).astype(np.float32) / 32768.0
    lang = config.get("idioma")
    modelo, dev = _whisper()
    try:
        return _palavras(modelo, audio, lang)
    except Exception as e:
        if dev != "cuda":
            raise
        erro = str(e)
    # A placa é detectada, mas o modelo só usa as bibliotecas do CUDA (cuBLAS/cuDNN) na hora de
    # transcrever: se elas não estiverem instaladas, a falha só aparece aqui. Passa para o processador.
    # (fora do except: o traceback segurava o modelo da placa na memória junto com o novo)
    print(f"[legenda] a placa de vídeo falhou ({erro}); usando o processador daqui em diante", flush=True)
    del modelo
    with _model_lock:
        _sem_gpu, _model = True, None
    gc.collect()
    modelo, _ = _whisper()
    return _palavras(modelo, audio, lang)


# ------------------------------------------------------------------ Legenda (ASS)
def _ts(t):
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def _esc(s):
    return s.replace("\\", "").replace("{", "(").replace("}", ")")


# Cores de destaque (formato ASS: &HBBGGRR&) - alterna a cada grupo de palavras
CORES_DESTAQUE = ["&H00E6FF&", "&H14FF39&", "&H00E6FF&", "&H5C3BFF&"]  # amarelo, verde, amarelo, rosa
LEG_X, LEG_Y = 540, 1450  # centro-baixo, logo abaixo do vídeo


def _ts_cs(cs):
    """centésimos de segundo -> h:mm:ss.cc"""
    cs = max(0, int(cs))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, c = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{c:02d}"


def _palavra(w):
    t = w.strip().strip(".,;:\"'“”…-").upper()
    return _esc(t)


def _agrupar(words, max_words=3, max_chars=15):
    """Grupos curtos (1-3 palavras): quebra em pausa, pontuação ou se ficar comprido."""
    groups, cur = [], []
    for w in words:
        if not _palavra(w["w"]):
            continue
        chars = sum(len(x["w"]) + 1 for x in cur) + len(w["w"])
        if cur and (len(cur) >= max_words or chars > max_chars
                    or w["s"] - cur[-1]["e"] > 0.45
                    or re.search(r"[.!?…,;:]$", cur[-1]["w"].strip())):
            groups.append(cur)
            cur = []
        cur.append(w)
    if cur:
        groups.append(cur)
    return groups


FONTE_GANCHO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fontes", "Montserrat-ExtraBold.ttf")
FONTES_REL = "../../cortes/fontes"  # pasta das fontes vista de dentro de cortes_prontos/<id> (o ffmpeg roda lá)
ESCALA_LIBASS = 1.575  # Montserrat: no mesmo \fs a libass desenha a 1/1,575 do tamanho do Pillow (medido)


def _linhas(texto, tam, max_w):
    """Quebra o texto em linhas que cabem em max_w px na fonte do gancho."""
    from PIL import ImageFont
    f = ImageFont.truetype(FONTE_GANCHO, tam)
    linhas, atual = [], ""
    for p in texto.split():
        t = (atual + " " + p).strip()
        if f.getlength(t) <= max_w or not atual:
            atual = t
        else:
            linhas.append(atual)
            atual = p
    if atual:
        linhas.append(atual)
    return linhas


def layout_gancho(gancho, complemento, modo):
    """Onde e em que tamanho vai o texto de gancho, e a área (y0, y1) do desfoque atrás dele.
    Na tela dividida fica logo abaixo da emenda câmera/jogo; no formato padrão, logo acima da imagem da live."""
    from . import layout
    # tamanhos em px "de verdade" (medidos com o Pillow); a libass desenha esta fonte menor no mesmo \fs,
    # por isso o \fs vai multiplicado por ESCALA_LIBASS. Meta (da referência): a linha mais longa ocupa ~85% da tela.
    tam = 104
    while True:
        ls = _linhas(gancho, tam, 920)
        if len(ls) <= 2 or tam <= 60:
            break
        tam -= 4
    tam_c = max(40, int(tam * 0.62))
    lc = _linhas(complemento, tam_c, 920) if complemento else []
    alt = int(len(ls) * tam * 1.45 + (len(lc) * tam_c * 1.45 + 6 if lc else 0))  # altura aproximada do bloco
    if modo == "dividido":   # logo abaixo da emenda câmera/jogo (o selo fica em cima da emenda)
        ancora, y_ancora = r"\an8", layout.ALT_CAM + 58
        y0, y1 = y_ancora - 34, y_ancora + alt + 26
    else:                    # formato padrão: a BASE do bloco fica acima do selo da live (não encosta nele)
        ancora, y_ancora = r"\an2", layout.TOPO_LIVE - 100
        y0, y1 = max(0, y_ancora - alt - 26), y_ancora + 30
    return {"tam": tam, "ls": ls, "tam_c": tam_c, "lc": lc, "ancora": ancora, "y_ancora": y_ancora,
            "y0": int(y0) // 2 * 2, "y1": int(y1) // 2 * 2}


def _eventos_gancho(gancho, complemento, ate, modo):
    """Texto de gancho dos primeiros segundos (estilo dos canais grandes de cortes): frase em amarelo, até 2 linhas,
    e a linha de piada menor em branco. O fundo desfocado atrás dele é feito no filtro do vídeo (layout.filtro_video)."""
    g = layout_gancho(gancho, complemento, modo)
    fim = _ts_cs(int(round(ate * 100)))
    # um bloco só (a libass cuida do espaço entre as linhas): frase em amarelo + complemento menor em branco
    pop = r"\fscx92\fscy92\t(0,120,\fscx100\fscy100)"
    texto = r"\N".join(_esc(x) for x in g["ls"])
    if g["lc"]:
        texto += (r"\N{\fs" + str(round(g["tam_c"] * ESCALA_LIBASS)) + r"\c&HFFFFFF&\bord1.5\shad3}"
                  + r"\N".join(_esc(x) for x in g["lc"]))
    return [f"Dialogue: 2,0:00:00.00,{fim},Gancho,,0,0,0,,"
            f"{{{g['ancora']}\\pos(540,{g['y_ancora']})\\fad(100,180){pop}\\fs{round(g['tam'] * ESCALA_LIBASS)}}}" + texto]


def duracao_gancho(dur):
    """Quanto tempo o texto de gancho fica na tela: ~3 s (2,5 a 4 s). Só o tempo de ler e já sair do caminho."""
    return round(min(4.0, max(2.5, dur * 0.12)), 2)


def build_ass(words, path, gancho=None, complemento=None, ate=0.0, modo="dividido"):
    """Legenda estilo cortes: as palavras aparecem conforme são faladas, a palavra
    atual "pula" na tela (zoom com elástico) colorida e cada frase entra subindo.
    Com gancho: o texto grande fica nos primeiros `ate` segundos e a legenda das falas começa depois dele."""
    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Corte,Arial Black,92,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,1,0,1,8,4,2,50,50,470,1
Style: Gancho,Montserrat ExtraBold,88,&H001AC2FF,&H001AC2FF,&H00101010,&H8C000000,0,0,0,0,100,100,0,0,1,2,4,8,40,40,0,1
Style: Complemento,Montserrat ExtraBold,48,&H00FFFFFF,&H00FFFFFF,&H00101010,&H8C000000,0,0,0,0,100,100,0,0,1,1.5,3,8,40,40,0,1
Style: Faixa,Arial,20,&H00000000,&H00000000,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    if gancho and ate > 0:
        words = [w for w in words if w["s"] >= ate - 0.1]  # as falas começam a aparecer quando o gancho sai
    groups = _agrupar(words)
    cs = lambda t: int(round(t * 100))  # noqa: E731
    lines = []
    for gi, g in enumerate(groups):
        cor = CORES_DESTAQUE[gi % len(CORES_DESTAQUE)]
        # até quando o grupo fica na tela: emenda no próximo se a pausa for curta (sem piscar)
        prox = cs(groups[gi + 1][0]["s"]) if gi + 1 < len(groups) else None
        fim = cs(g[-1]["e"] + 0.35)
        if prox is not None and (prox - cs(g[-1]["e"]) < 60 or fim > prox):
            fim = prox
        inicios = [cs(w["s"]) for w in g]
        for i in range(len(g)):
            ini = inicios[i] if i else inicios[0]
            end = inicios[i + 1] if i + 1 < len(g) else fim
            if i + 1 < len(g):
                end = max(end, ini + 1)
            if end <= ini:
                continue
            # entrada do grupo: sobe 25px e aparece em 120ms; depois fica parado
            if i == 0:
                pos = rf"\move({LEG_X},{LEG_Y + 25},{LEG_X},{LEG_Y},0,120)\fad(80,0)"
            else:
                pos = rf"\pos({LEG_X},{LEG_Y})"
            parts = []
            for j, x in enumerate(g):
                t = _palavra(x["w"])
                if j < i:     # já falada: branca
                    parts.append(r"{\alpha&H00&\c&HFFFFFF&\fscx100\fscy100}" + t)
                elif j == i:  # falando agora: colorida, com "pulo"
                    parts.append(rf"{{\alpha&H00&\c{cor}\fscx75\fscy75\t(0,90,\fscx122\fscy122)"
                                 rf"\t(90,180,\fscx110\fscy110)}}" + t)
                else:         # ainda não falada: reserva o espaço, invisível (a linha não fica pulando de lugar)
                    parts.append(r"{\alpha&HFF&\c&HFFFFFF&\fscx100\fscy100}" + t)
            lines.append(f"Dialogue: 0,{_ts_cs(ini)},{_ts_cs(end)},Corte,,0,0,0,,{{{pos}}}" + " ".join(parts))
    if gancho and ate > 0:
        lines = _eventos_gancho(gancho, complemento, ate, modo) + lines
    with open(path, "w", encoding="utf-8") as f:
        f.write(header + "\n".join(lines) + "\n")
    return bool(lines)


# ------------------------------------------------------------------ Processamento
jobs = queue.Queue()


def enqueue(clip_id, raw_path, offset, duration):
    jobs.put((clip_id, raw_path, offset, duration))


LIVE_CROP = "crop=1080:608:0:656"  # a imagem da live dentro do corte no formato padrão


def fonte_16x9(cid):
    """(vídeo, filtro) para tirar a imagem 16:9 da live de um corte: o live.mp4 dos cortes divididos,
    ou o recorte do meio do corte.mp4 no formato padrão. O filtro já vem com vírgula no fim (ou vazio)."""
    d = os.path.join(config.CLIPS_DIR, cid)
    live = os.path.join(d, "live.mp4")
    if os.path.exists(live):
        return live, ""
    return os.path.join(d, "corte.mp4"), LIVE_CROP + ","


def duracao(caminho):
    """Duração de um vídeo em segundos (0.0 se não der para ler)."""
    p = subprocess.run([config.ffmpeg(), "-hide_banner", "-i", caminho], capture_output=True, creationflags=FLAGS)
    m = re.search(rb"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", p.stderr)
    return int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3]) if m else 0.0


def _offset_estimado(raw, dur):
    """Cortes antigos não guardavam onde o trecho começa dentro do bruto.ts. O bruto são os pedaços de 4 s
    da gravação que cobrem o trecho, então a sobra (antes + depois) é o que passa da duração: metade de cada lado."""
    total = duracao(raw)
    return max(0.0, (total - dur) / 2) if total else 0.0


def _enfileirar(c):
    enqueue(c["id"], _bruto(c["id"]), c.get("bruto_offset"), float(c["duracao"]))


def reprocessar(cid):
    """Tenta de novo um corte que falhou, a partir do vídeo bruto guardado."""
    c = get_clip(cid)
    if not c:
        raise ValueError("corte não encontrado")
    if c["status"] != "erro":
        raise ValueError("só dá para tentar de novo um corte que falhou")
    if not os.path.exists(_bruto(cid)):
        raise ValueError("o vídeo bruto deste corte já foi apagado; não dá para refazer")
    update_clip(cid, status="na fila", erro=None)
    _enfileirar(c)
    return get_clip(cid)


def _esperar_disco(cid):
    """Com o disco quase cheio, o corte espera na fila em vez de falhar no meio."""
    avisou = False
    while config.disco_livre_mb() < config.DISCO_MIN_PROCESSAR_MB:
        if not avisou:
            update_clip(cid, status="esperando espaço no disco")
            avisou = True
        time.sleep(30)


def _montar(d, offset, partes, fonte="bruto.ts", saida="montado.mp4"):
    """Junta os pedaços escolhidos (cortes secos) num vídeo só. partes: [(ini, fim, enquadramento)] em
    segundos do corte. Devolve (arquivo, duração, trocas no tempo do vídeo montado)."""
    entradas, filtro, trocas, t = [], "", [], 0.0
    for i, (x, y, enq) in enumerate(partes):
        entradas += ["-ss", f"{offset + x:.3f}", "-t", f"{y - x:.3f}", "-i", fonte]
        filtro += f"[{i}:v:0]setpts=PTS-STARTPTS[v{i}];[{i}:a:0]asetpts=PTS-STARTPTS,aresample=async=1[a{i}];"
        trocas.append((round(t, 3), round(t + y - x, 3), enq))
        t += y - x
    filtro += "".join(f"[v{i}][a{i}]" for i in range(len(partes))) + f"concat=n={len(partes)}:v=1:a=1[v][a]"
    run_ffmpeg([*entradas, "-filter_complex", filtro, "-map", "[v]", "-map", "[a]",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-c:a", "aac", "-b:a", "192k",
                saida], cwd=d)
    return saida, round(t, 3), trocas


# ------------------------------------------------------------------ Ritmo: pausas e jump cuts
# Princípio dos cortes de referência: pausa entre frases acima de 0,3 s vira 0,2 s (pausa dentro da frase fica,
# a não ser que seja silêncio morto) e o plano muda de zoom a cada ~2,8 s, num corte seco, sempre no começo
# de uma palavra. Nada da fala é tirado: só o silêncio.
FIM_FRASE = re.compile(r"[.!?…]$")
PAUSA_FRASE, FICA_FRASE = 0.3, 0.2   # entre frases: acima de 0,3 s vira 0,2 s
PAUSA_MORTA, FICA_MORTA = 0.7, 0.25  # dentro da frase: só silêncio longo
ZOOM_PLANO = 1.10                    # os planos ímpares ficam 10% mais fechados (jump cut)
PLANO_S = 2.8


def _rms_db(audio, a, b, sr=16000):
    import numpy as np
    x = audio[max(0, int(a * sr)):max(0, int(b * sr))]
    if len(x) < sr * 0.05:
        return None
    return float(20 * np.log10(np.sqrt(np.mean(np.square(x))) + 1e-9))


def trechos_sem_pausa(words, dur, audio):
    """Trechos [(ini, fim)] do corte que ficam depois de tirar as pausas, ou None se não vale a pena.
    Só corta onde o áudio está mesmo baixo (12 dB abaixo da fala): risada, grito sem palavra, som do jogo
    e música alta não são pausa, mesmo sem nada transcrito."""
    if len(words) < 3:
        return None
    fala = sorted(x for x in (_rms_db(audio, w["s"], w["e"]) for w in words if w["e"] - w["s"] >= 0.08)
                  if x is not None)
    if not fala:
        return None
    limite = fala[len(fala) // 2] - 12

    def quieto(a, b):
        r = _rms_db(audio, a, b)
        return r is not None and r < limite

    tirar = []
    w0 = words[0]["s"]
    if w0 > 0.6 and quieto(0.0, w0 - 0.05):        # começo morto: o corte entra direto na 1ª palavra
        tirar.append((0.0, w0 - FICA_FRASE))
    for p, q in zip(words, words[1:]):
        vao = q["s"] - p["e"]
        if FIM_FRASE.search(p["w"].strip()) and vao > PAUSA_FRASE:
            fica = FICA_FRASE
        elif vao > PAUSA_MORTA:
            fica = FICA_MORTA
        else:
            continue
        a, b = p["e"] + fica / 2, q["s"] - fica / 2
        if b - a >= 0.1 and quieto(p["e"] + 0.03, q["s"] - 0.03):
            tirar.append((a, b))
    if sum(b - a for a, b in tirar) < 0.4:
        return None
    ficam, cur = [], 0.0
    for a, b in tirar:
        if a <= 0.01:          # o começo morto
            cur = b
            continue
        if a - cur < 0.25:     # sobraria um pedacinho: não corta esta pausa
            continue
        ficam.append((cur, a))
        cur = b
    ficam.append((cur, dur))
    return [(round(a, 3), round(b, 3)) for a, b in ficam if b - a > 0.05]


def _novo_tempo(ficam):
    """Função que leva um instante do corte original para o corte sem as pausas."""
    mapa, t = [], 0.0
    for a, b in ficam:
        mapa.append((a, b, t))
        t += b - a

    def f(x):
        for a, b, o in mapa:
            if x < a:
                return o
            if x <= b:
                return o + x - a
        return t
    return f


def _pedacos(ficam, trocas):
    """Cruza os trechos que ficam com o enquadramento de cada parte (rosto, jogo, dividido)."""
    pts = sorted({a for a, _ in ficam} | {b for _, b in ficam} | {x for x, _, _ in trocas} | {y for _, y, _ in trocas})
    out = []
    for a, b in ficam:
        for x, y in zip(pts, pts[1:]):
            i, j = max(a, x), min(b, y)
            if j - i <= 0.02:
                continue
            m = (i + j) / 2
            enq = next((e for p0, p1, e in trocas if p0 <= m < p1), "dividido")
            if out and out[-1][2] == enq and abs(out[-1][1] - i) < 0.001:
                out[-1][1] = j
            else:
                out.append([i, j, enq])
    return out


def planos_zoom(words, emendas, dur, cada=PLANO_S):
    """Trechos [(ini, fim)] em que a imagem do streamer fica mais fechada: os planos se alternam entre 100% e
    ZOOM_PLANO a cada ~`cada` segundos e em cada emenda (pausa tirada ou pedaço da montagem). A troca cai no
    começo de uma palavra ou fora da fala, nunca no meio de uma palavra."""
    inicios = [w["s"] for w in words]

    def livre(t):
        return not any(w["s"] < t < w["e"] for w in words)

    pontos = [0.0] + sorted(t for t in set(emendas) if 0.3 < t < dur - 0.3) + [dur]
    cortes = []
    for a, b in zip(pontos, pontos[1:]):
        cur = a
        while b - cur > cada * 1.5:
            alvo = cur + cada
            cand = [t for t in inicios if cur + 1.2 < t < b - 1.0] + ([alvo] if livre(alvo) else [])
            if not cand:
                break
            cur = min(cand, key=lambda t: abs(t - alvo))
            cortes.append(cur)
        if b < dur:
            cortes.append(b)
    bordas = [0.0] + sorted(set(round(t, 3) for t in cortes)) + [dur]
    return [(bordas[i], bordas[i + 1]) for i in range(1, len(bordas) - 1, 2)]


def _process(cid, raw, offset, dur):
    d = os.path.dirname(raw)
    t0 = time.time()
    if offset is None:
        offset = _offset_estimado(raw, dur)
    clip = get_clip(cid) or {}
    fonte, trocas = "bruto.ts", []
    partes = [p for p in clip.get("partes") or [] if len(p) == 3 and p[1] > p[0]]
    if partes:
        com_salto = any(partes[i + 1][0] - partes[i][1] > 0.3 for i in range(len(partes) - 1))
        if com_salto:  # a IA montou a história com pedaços separados: junta com cortes secos
            update_clip(cid, status="montando")
            fonte, dur, trocas = _montar(d, offset, partes)
            offset = 0.0
            update_clip(cid, duracao=round(dur, 1))
        else:          # trecho contínuo: só as trocas de enquadramento
            trocas = [(p[0], min(p[1], dur), p[2]) for p in partes]
    emendas = [a for a, _, _ in trocas[1:]] if fonte == "montado.mp4" else []
    gancho = clip.get("texto_gancho")
    # a fala é sempre transcrita: alimenta títulos, hashtags, curadoria e capas.
    # A opção "legenda" só decide se ela aparece escrita no vídeo.
    update_clip(cid, status="transcrevendo")
    run_ffmpeg(["-ss", f"{offset:.2f}", "-t", f"{dur:.2f}", "-i", fonte,
                "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "audio.pcm"], cwd=d)
    words = transcribe(os.path.join(d, "audio.pcm"))

    # pausas: só fora do gameplay (no jogo, o silêncio da fala costuma ser a jogada acontecendo na tela)
    if config.get("cortar_pausas") and clip.get("cena") != "jogo":
        import numpy as np
        audio = np.fromfile(os.path.join(d, "audio.pcm"), dtype=np.int16).astype(np.float32) / 32768.0
        ficam = trechos_sem_pausa(words, dur, audio)
        del audio
        if ficam:
            update_clip(cid, status="cortando as pausas")
            fonte, novo, trocas = _montar(d, offset, _pedacos(ficam, trocas), fonte, "enxuto.mp4")
            f = _novo_tempo(ficam)
            words = [dict(w, s=round(f(w["s"]), 3), e=round(max(f(w["e"]), f(w["s"]) + 0.05), 3)) for w in words]
            emendas = [a for a, _, _ in trocas[1:]]
            mudou = {"duracao": round(novo, 1), "pausas_tiradas": round(dur - novo, 1)}
            if clip.get("pico_rel") is not None:
                mudou["pico_rel"] = round(f(float(clip["pico_rel"])), 2)
            update_clip(cid, **mudou)
            offset, dur = 0.0, novo
    ate = duracao_gancho(dur) if gancho else 0.0
    words_video = words if config.get("legenda") else []
    zoom = planos_zoom(words, emendas, dur) if config.get("zoom_cortes") else None

    update_clip(cid, status="renderizando")
    clip = get_clip(cid) or {}
    # formato: gameplay com a câmera do streamer vira tela dividida (a IA acha a câmera num quadro do pico)
    t_quadro = offset + min(float(clip.get("pico_rel") or dur * 0.5), max(dur - 0.5, 0))
    lay = layout.decidir(clip, layout.quadro(os.path.join(d, fonte), t_quadro, config.ffmpeg()))
    # legenda das falas + texto de gancho dos primeiros segundos (num arquivo só)
    has_subs = False
    if words_video or gancho:
        has_subs = build_ass(words_video, os.path.join(d, "legenda.ass"), gancho, clip.get("complemento"), ate, lay["modo"])
    selo = bool(config.get("selo_live")) and layout.criar_selo(clip, os.path.join(d, "selo.png"))
    trocas_div = trocas if lay["modo"] == "dividido" else None
    caixa = None
    if gancho and has_subs:  # área do desfoque atrás do texto de gancho
        lg = layout_gancho(gancho, clip.get("complemento"), lay["modo"])
        caixa = (lg["y0"], lg["y1"], ate)
    # volume: −14 LUFS e pico abaixo de −1,5 dBTP (o padrão dos Shorts; as lives variam muito de volume)
    vf = ("[0:a:0]asetpts=PTS-STARTPTS,loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[aud];"
          + layout.filtro_video(lay["modo"], lay["camera"], selo, has_subs, lay.get("janela"), trocas_div, caixa, zoom))
    run_ffmpeg(["-ss", f"{offset:.2f}", "-t", f"{dur:.2f}", "-i", fonte, *(["-i", "selo.png"] if selo else []),
                "-filter_complex", vf, "-map", "[v]", "-map", "[aud]",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
                "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", "corte.mp4"], cwd=d)
    if lay["modo"] == "dividido":  # cópia 16:9 só da live, para thumbnails e compilados (o corte não tem mais a live inteira)
        run_ffmpeg(["-ss", f"{offset:.2f}", "-t", f"{dur:.2f}", "-i", fonte, "-vf", "scale=1280:-2,format=yuv420p",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-c:a", "aac", "-b:a", "128k",
                    "-movflags", "+faststart", "live.mp4"], cwd=d)
    update_clip(cid, layout=lay)
    run_ffmpeg(["-ss", f"{min(dur / 2, 8):.2f}", "-i", "corte.mp4", "-frames:v", "1",
                "-vf", "scale=360:-2:out_range=full,format=yuvj420p", "capa.jpg"], cwd=d)

    text = " ".join(w["w"] for w in words)
    clip = get_clip(cid) or {}
    pico = clip.get("pico_rel", dur * 0.7)
    frase = " ".join(w["w"] for w in words if pico - 6 <= w["s"] <= pico + 3)
    for f in ("bruto.ts", "audio.pcm", "montado.mp4", "enxuto.mp4"):
        try:
            os.remove(os.path.join(d, f))
        except OSError:
            pass
    update_clip(cid, transcricao=text, frase_pico=frase,
                arquivo=f"/arquivos/{cid}/corte.mp4", capa=f"/arquivos/{cid}/capa.jpg")

    update_clip(cid, status="escrevendo títulos")
    sug = titulos.gerar(get_clip(cid))
    update_clip(cid, status="pronto", sugestoes=sug, titulo=sug["titulos"][0] if sug["titulos"] else None,
                tempo_processo=round(time.time() - t0, 1))


def regenerate_suggestions(cid):
    clip = get_clip(cid)
    if not clip:
        return None
    sug = titulos.gerar(clip)
    update_clip(cid, sugestoes=sug, titulo=sug["titulos"][0] if sug["titulos"] else clip.get("titulo"))
    return get_clip(cid)


def _worker():
    while True:
        cid, raw, offset, dur = jobs.get()
        try:
            if not get_clip(cid):
                continue  # apagado enquanto esperava na fila
            _esperar_disco(cid)
            _process(cid, raw, offset, dur)
        except Exception as e:  # noqa
            traceback.print_exc()
            update_clip(cid, status="erro", erro=str(e)[-300:])
        try:
            curadoria.verificar()  # fechou um lote de 10? escolhe os 2 para postar
        except Exception:  # noqa
            traceback.print_exc()


threading.Thread(target=_worker, daemon=True).start()
for _cid in reversed(_retomar):  # do mais antigo para o mais novo
    _c = get_clip(_cid)
    if _c:
        _enfileirar(_c)


def _titulos_pendentes(ids):
    for cid in ids:
        try:
            regenerate_suggestions(cid)
        except Exception:  # noqa
            traceback.print_exc()


if _sem_titulo:
    threading.Thread(target=_titulos_pendentes, args=(list(_sem_titulo),), daemon=True).start()
