"""Formato do corte vertical (1080x1920).

- "padrao": a live inteira no meio, com fundo desfocado em cima e embaixo.
- "dividido": para gameplay com a câmera do streamer sobreposta ao jogo. A câmera vai em cima (1080x768)
  e o jogo embaixo (1080x1152), ocupando a tela toda. A IA (Gemini) olha um quadro do corte e marca onde
  está a câmera.

Regra: jogo → dividido; streamer conversando com o chat → padrão. A cena vem da IA que assistiu a live e
escolheu o corte (campo "cena"). Quando ela disse "jogo", a posição da câmera de um corte recente do mesmo
streamer (até 15 min de live) é reaproveitada; sem a cena, cada corte é conferido pelo quadro dele.

E o selo: um logo pequeno da plataforma (Kick, Twitch, YouTube) com o link da live, no meio do vídeo.
"""
import io
import os
import threading
import traceback
from datetime import datetime
from typing import List

from pydantic import BaseModel

from . import config

W, H = 1080, 1920
ALT_CAM = 768                  # câmera em cima (40%); o jogo fica com os 60% de baixo
ALT_JOGO = H - ALT_CAM
TOPO_LIVE = 656                # no formato padrão, a imagem da live começa aqui (1080x608 no meio da tela)
VALE_POR_S = 15 * 60           # a detecção de um streamer vale para os cortes dos 15 min seguintes de live

_cache = {}                    # canal -> (horário do corte, resultado)
_lock = threading.Lock()


class Cena(BaseModel):
    tipo: str                  # "gameplay" | "conversa" | "outro"
    tem_camera: bool
    camera: List[int]          # [ymin, xmin, ymax, xmax] de 0 a 1000 (vazio se não tiver câmera)


SISTEMA = """Você recebe um quadro de uma live (Twitch, Kick ou YouTube) e descreve como a tela está montada.
- "tipo": "gameplay" se a maior parte da tela é um jogo; "conversa" se a maior parte é a câmera do streamer
  (bate-papo, reação, IRL); "outro" para o resto (tela de espera, vídeo, navegador).
- "tem_camera": true só se a câmera (webcam) do streamer aparece como uma janela separada, sobreposta ao jogo
  ou num canto da tela. Se a câmera ocupa a tela toda, é false.
- "camera": a caixa da janela da câmera em [ymin, xmin, ymax, xmax], coordenadas de 0 a 1000 em relação ao quadro.
  Só a imagem da câmera: sem moldura decorativa, nome, chat, alertas ou barra de doação. Lista vazia se não houver."""


def _rel(t):
    try:
        return datetime.fromisoformat(t)
    except (TypeError, ValueError):
        return datetime.now()


def _janela(c):
    """[ymin, xmin, ymax, xmax] 0-1000 -> (x, y, w, h) 0-1 da janela inteira da câmera (para tirá-la do jogo)."""
    y0, x0, y1, x1 = (max(0, min(1000, int(v))) / 1000 for v in c)
    return [round(x0, 4), round(y0, 4), round(x1 - x0, 4), round(y1 - y0, 4)]


def _caixa(c):
    """[ymin, xmin, ymax, xmax] 0-1000 -> (x, y, w, h) 0-1, um pouco para dentro (tira a borda da moldura).
    None se a caixa não faz sentido para virar a parte de cima do corte."""
    if len(c) != 4:
        return None
    y0, x0, y1, x1 = (max(0, min(1000, int(v))) / 1000 for v in c)
    w, h = x1 - x0, y1 - y0
    if w <= 0 or h <= 0:
        return None
    area, prop = w * h, (w * 16) / (h * 9)  # proporção real na tela 16:9
    if not (0.01 <= area <= 0.45 and 0.5 <= prop <= 3.2):
        return None
    mx, my = 0.03, 0.10  # um pouco para dentro (a caixa da IA costuma pegar a borda do placar/moldura em cima)
    return [round(x0 + w * mx, 4), round(y0 + h * my, 4), round(w * (1 - 2 * mx), 4), round(h * (1 - 2 * my), 4)]


ZOOM_MAX = 1.45
DESLOC_MAX = 0.06


def recorte_jogo(janela, prop_fonte=16 / 9):
    """Parte do quadro que vira o jogo embaixo: (x, y, largura, altura), de 0 a 1.
    Começa no recorte central que enche os 1080x1152. Se a janela da câmera aparece nele:
    1) dá zoom pelo centro (a mira continua no meio) até ela sair, no máximo ZOOM_MAX;
    2) se ainda sobrar, desliza o recorte para longe da câmera, no máximo DESLOC_MAX.
    Câmera no meio da tela: fica como está (melhor sobrar um pedaço do que estragar o jogo)."""
    lw = (W / ALT_JOGO) / prop_fonte  # largura do recorte com a altura inteira
    centro = (0.5 - lw / 2, 0.0, lw, 1.0)
    if not janela:
        return centro
    x, y, w, h = janela
    m = 0.02  # folga: a moldura da câmera costuma passar um pouco da caixa que a IA marca
    if x + w <= 0.5 or x >= 0.5:  # câmera num lado: tira pela largura
        esq = x + w <= 0.5
        borda = x + w + m if esq else x - m          # o recorte precisa começar depois (ou acabar antes) daqui
        metade = 0.5 - borda if esq else borda - 0.5
        z = min(max(1.0, (lw / 2) / metade), ZOOM_MAX) if metade > 0 else ZOOM_MAX
        cw, ch = lw / z, 1.0 / z
        x0 = 0.5 - cw / 2
        falta = (borda - x0) if esq else (x0 + cw - borda)
        if falta > DESLOC_MAX:
            return centro
        if falta > 0:
            x0 = borda if esq else borda - cw
        return (x0, 0.5 - ch / 2, cw, ch)
    if y + h <= 0.5 or y >= 0.5:  # câmera em cima ou embaixo, no meio: tira pela altura
        cima = y + h <= 0.5
        sobra = 1 - (y + h + m) if cima else y - m    # altura livre do outro lado da câmera
        if sobra <= 0:
            return centro
        z = max(1.0, 1 / sobra)
        if z > ZOOM_MAX:
            return centro
        cw, ch = lw / z, 1.0 / z
        return (0.5 - cw / 2, 1 - ch if cima else 0.0, cw, ch)
    return centro


def _perguntar(jpg):
    from google import genai
    from google.genai import types
    from .titulos import MODELOS_GEMINI, _chave_gemini, _schema_sem_refs, gemini_gerar
    partes = [types.Part(inline_data=types.Blob(data=jpg, mime_type="image/jpeg")), "Como está montada esta tela?"]
    cfg = types.GenerateContentConfig(system_instruction=SISTEMA, response_mime_type="application/json",
                                      response_json_schema=_schema_sem_refs(Cena), temperature=0.1)
    r = gemini_gerar(genai.Client(api_key=_chave_gemini()), MODELOS_GEMINI, partes, cfg)
    return Cena.model_validate_json(r.text)


def decidir(clip, quadro_jpg):
    """Escolhe o formato do corte. quadro_jpg: bytes de um quadro 16:9 da live (ou None).
    Devolve {"modo": "dividido"|"padrao", "camera": [x,y,w,h] 0-1 ou None, "tipo", "fonte", "motivo"}."""
    from .titulos import _chave_gemini
    if config.get("formato_corte") != "auto":
        return {"modo": "padrao", "camera": None, "tipo": None, "fonte": "config", "motivo": "formato fixo nas configurações"}
    if not _chave_gemini():
        return {"modo": "padrao", "camera": None, "tipo": None, "fonte": "sem_ia", "motivo": "sem chave do Gemini"}
    # a IA que assistiu a live já disse a cena deste corte: conversa com o chat fica sempre no formato de sempre
    cena = clip.get("cena")
    if cena in ("conversa", "outro"):
        return {"modo": "padrao", "camera": None, "tipo": cena, "fonte": "analise",
                "motivo": "conversa com o chat" if cena == "conversa" else "não é gameplay"}
    if not quadro_jpg:
        return {"modo": "padrao", "camera": None, "tipo": None, "fonte": "erro", "motivo": "não deu para tirar um quadro da live"}
    canal = (clip.get("canal") or "").lower()
    quando = _rel(clip.get("criado"))
    # reaproveitar só a posição da câmera, e só quando este corte é com certeza jogo: sem saber a cena do corte,
    # a IA olha o quadro dele (o streamer pode ter saído do jogo para conversar há poucos minutos)
    if cena == "jogo":
        with _lock:
            c = _cache.get(canal)
            if c and c[1]["modo"] == "dividido" and abs((quando - c[0]).total_seconds()) < VALE_POR_S:
                return dict(c[1], fonte="cache")
    try:
        cena = _perguntar(quadro_jpg)
    except Exception as e:  # noqa - sem resposta da IA, fica no formato de sempre
        traceback.print_exc()
        return {"modo": "padrao", "camera": None, "tipo": None, "fonte": "erro", "motivo": f"IA não respondeu ({type(e).__name__})"}
    cam = _caixa(cena.camera) if cena.tem_camera else None
    if cena.tipo == "gameplay" and cam:
        res = {"modo": "dividido", "camera": cam, "janela": _janela(cena.camera), "tipo": cena.tipo, "fonte": "ia",
               "motivo": "gameplay com a câmera do streamer"}
    else:
        motivo = ("gameplay sem câmera separada" if cena.tipo == "gameplay"
                  else "a câmera ocupa a tela" if cena.tipo == "conversa" else "não é gameplay")
        if cena.tipo == "gameplay" and cena.tem_camera and not cam:
            motivo = "a caixa da câmera veio estranha"
        res = {"modo": "padrao", "camera": None, "tipo": cena.tipo, "fonte": "ia", "motivo": motivo}
    with _lock:
        _cache[canal] = (quando, res)
    return res


# ------------------------------------------------------------------ montagem (filtros do ffmpeg)
def _enable(trechos):
    return "+".join(f"between(t,{a:.2f},{b:.2f})" for a, b in trechos)


def _fundo_gancho(entrada, y0, y1, ate):
    """Desfoque atrás do texto de gancho: a faixa da imagem fica borrada e um pouco mais escura, com as bordas
    de cima e de baixo suaves (sem corte duro), entra e sai com fade e some quando o gancho sai."""
    h = max(40, (y1 - y0) // 2 * 2)
    pena = max(12, min(48, h // 3))  # altura da borda suave
    fade_out = max(0.0, ate - 0.18)
    return (f";[{entrada}]split=2[gb0][gb1];"
            f"[gb1]trim=end={ate:.2f},crop={W}:{h}:0:{y0},gblur=sigma=24,eq=brightness=-0.13:saturation=0.85,"
            f"format=rgba,geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='255*min(1,min(Y,H-1-Y)/{pena})',"
            f"fade=t=in:st=0:d=0.12:alpha=1,fade=t=out:st={fade_out:.2f}:d=0.18:alpha=1[gbl];"
            f"[gb0][gbl]overlay=0:{y0}:eof_action=pass[gch]")


def _zoom(entrada, saida, w, h, zoom):
    """Jump cut: nos trechos `zoom` a imagem (w x h) fica ZOOM_PLANO mais fechada, pelo centro."""
    from .clipper import ZOOM_PLANO
    if not zoom:
        return f"[{entrada}]null[{saida}]"
    zw, zh = int(w * ZOOM_PLANO) // 2 * 2, int(h * ZOOM_PLANO) // 2 * 2
    return (f"[{entrada}]split=2[{saida}_0][{saida}_1];"
            f"[{saida}_1]scale={zw}:{zh}:flags=lanczos,crop={w}:{h}[{saida}_z];"
            f"[{saida}_0][{saida}_z]overlay=0:0:enable='{_enable(zoom)}'[{saida}]")


def filtro_video(modo, camera, selo, legenda, janela=None, trocas=None, gancho_caixa=None, zoom=None):
    """filter_complex do vídeo. Entrada 0 = live; entrada 1 = selo.png (se selo). Saída [v].
    camera: a parte da câmera que vai em cima; janela: a janela inteira dela (fica de fora do jogo).
    trocas (só na tela dividida): [(ini, fim, "rosto"|"jogo")] em segundos do corte: nesses trechos a tela
    inteira vira só a câmera (reação) ou só o jogo (detalhe), como na edição dos canais grandes.
    zoom: [(ini, fim)] em que a imagem do streamer fica mais fechada (jump cuts; o jogo não muda)."""
    if modo == "dividido":
        x, y, w, h = camera
        jx, jy, jw, jh = recorte_jogo(janela or camera)
        rosto = [(a, b) for a, b, e in trocas or [] if e == "rosto" and b - a > 0.3]
        jogo_cheio = [(a, b) for a, b, e in trocas or [] if e == "jogo" and b - a > 0.3]
        n = 2 + bool(rosto) + bool(jogo_cheio)
        saidas = "[a][b]" + ("[c]" if rosto else "") + ("[d]" if jogo_cheio else "")
        corta_cam = f"crop=w='floor(iw*{w}/2)*2':h='floor(ih*{h}/2)*2':x='floor(iw*{x})':y='floor(ih*{y})'"
        g = (f"[0:v]setpts=PTS-STARTPTS,split={n}{saidas};"
             f"[a]{corta_cam},"
             f"scale={W}:{ALT_CAM}:force_original_aspect_ratio=increase,crop={W}:{ALT_CAM},setsar=1[cam0];"
             + _zoom("cam0", "cam", W, ALT_CAM, zoom) + ";"
             f"[b]crop=w='floor(iw*{jw:.4f}/2)*2':h='floor(ih*{jh:.4f}/2)*2':x='floor(iw*{jx:.4f})':y='floor(ih*{jy:.4f})',"
             f"scale={W}:{ALT_JOGO}:force_original_aspect_ratio=increase:flags=lanczos,crop={W}:{ALT_JOGO},setsar=1[jogo];"
             "[cam][jogo]vstack=inputs=2[base]")
        if rosto:   # só a câmera, em tela cheia
            g += (f";[c]{corta_cam},scale={W}:{H}:force_original_aspect_ratio=increase:flags=lanczos,"
                  f"crop={W}:{H},setsar=1[rcheio];[base][rcheio]overlay=0:0:enable='{_enable(rosto)}'[base2]")
        if jogo_cheio:  # só o jogo, em tela cheia (miolo do jogo, sem a câmera)
            entrada = "base2" if rosto else "base"
            g += (f";[d]crop=w='floor(ih*9/16/2)*2':h=ih:x='(iw-ow)/2':y=0,scale={W}:{H}:flags=lanczos,setsar=1[gcheio];"
                  f"[{entrada}][gcheio]overlay=0:0:enable='{_enable(jogo_cheio)}'[base3]")
        atual_div = "base3" if jogo_cheio else ("base2" if rosto else "base")
        g = g if atual_div == "base" else g + f";[{atual_div}]null[basef]"
        y_selo = f"{ALT_CAM}-h/2"           # na emenda entre a câmera e o jogo
    else:
        g = ("[0:v]setpts=PTS-STARTPTS,split=2[a][b];"
             f"[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
             "boxblur=30:2,eq=brightness=-0.10:saturation=1.2[bg];"
             f"[b]scale={W}:-2[fg];")
        if zoom:  # jump cut: a imagem da live fica mais fechada, no mesmo lugar e do mesmo tamanho
            from .clipper import ZOOM_PLANO
            g += ("[fg]split=2[fg0][fg1];"
                  f"[fg1]scale=trunc(iw*{ZOOM_PLANO}/2)*2:-2:flags=lanczos,"
                  f"crop=trunc(iw/{ZOOM_PLANO}/2)*2:trunc(ih/{ZOOM_PLANO}/2)*2[fgz];"
                  "[bg][fg0]overlay=(W-w)/2:(H-h)/2[base0];"
                  f"[base0][fgz]overlay=(W-w)/2:(H-h)/2:enable='{_enable(zoom)}'[base]")
        else:
            g += "[bg][fg]overlay=(W-w)/2:(H-h)/2[base]"
        y_selo = f"{TOPO_LIVE - 22}-h"      # logo acima da imagem da live
    atual = "basef" if "[basef]" in g else "base"
    if gancho_caixa:  # (y0, y1, até quando): fundo desfocado atrás do texto de gancho
        y0, y1, ate = gancho_caixa
        g += _fundo_gancho(atual, int(y0), int(y1), float(ate))
        atual = "gch"
    if selo:
        g += f";[{atual}][1:v]overlay=(W-w)/2:{y_selo}[sel]"
        atual = "sel"
    from .clipper import FONTES_REL  # fonte do texto de gancho (Montserrat), vista da pasta do corte
    return g + f";[{atual}]" + (f"ass=legenda.ass:fontsdir={FONTES_REL}," if legenda else "") + "format=yuv420p[v]"


# ------------------------------------------------------------------ selo da plataforma
FONTES = ("C:/Windows/Fonts/seguibl.ttf", "C:/Windows/Fonts/ariblk.ttf", "C:/Windows/Fonts/arialbd.ttf")
CORES = {"kick": (83, 252, 24), "twitch": (145, 70, 255), "youtube": (255, 0, 51)}


def _fonte(tam):
    from PIL import ImageFont
    for f in FONTES:
        if os.path.exists(f):
            return ImageFont.truetype(f, tam)
    return ImageFont.load_default()


def _icone(plat, lado):
    """Ícone simples da plataforma, desenhado (sem baixar imagem nenhuma)."""
    from PIL import Image, ImageDraw
    esc = 4  # desenha grande e reduz: borda lisa
    L = lado * esc
    im = Image.new("RGBA", (L, L), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    cor = CORES.get(plat, (255, 255, 255))
    if plat == "youtube":
        d.rounded_rectangle([0, L * 0.15, L, L * 0.85], radius=L * 0.2, fill=cor)
        d.polygon([(L * 0.4, L * 0.32), (L * 0.4, L * 0.68), (L * 0.7, L * 0.5)], fill="white")
    elif plat == "twitch":
        d.rounded_rectangle([0, 0, L, L], radius=L * 0.22, fill=cor)
        d.polygon([(L * .25, L * .2), (L * .78, L * .2), (L * .78, L * .58), (L * .62, L * .72), (L * .5, L * .72),
                   (L * .38, L * .84), (L * .38, L * .72), (L * .25, L * .72)], fill="white")
        d.rectangle([L * .47, L * .33, L * .53, L * .5], fill=cor)
        d.rectangle([L * .62, L * .33, L * .68, L * .5], fill=cor)
    else:  # kick (e o resto): quadrado verde com um K em blocos
        d.rounded_rectangle([0, 0, L, L], radius=L * 0.22, fill=cor)
        u = L / 7  # grade de blocos, no estilo do logo
        k = [(1, 1, 3, 6), (3, 3, 4, 4), (4, 2, 5, 3), (5, 1, 6, 2), (4, 4, 5, 5), (5, 5, 6, 6)]
        for x0, y0, x1, y1 in k:
            d.rectangle([x0 * u, y0 * u, x1 * u, y1 * u], fill=(0, 0, 0))
    return im.resize((lado, lado), Image.LANCZOS)


def criar_selo(clip, caminho):
    """Gera o PNG do selo (ícone + link da live). Devolve False se não houver link para mostrar."""
    from PIL import Image, ImageDraw
    from .titulos import _url_canal
    url = _url_canal(clip)
    if not url:
        return False
    plat = (clip.get("plataforma") or "").lower()
    alt, icone, pad = 58, 38, 14
    fonte = _fonte(28)
    caixa = fonte.getbbox(url)
    larg = pad + icone + 12 + (caixa[2] - caixa[0]) + pad + 4
    im = Image.new("RGBA", (larg, alt), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([0, 0, larg - 1, alt - 1], radius=alt // 2, fill=(0, 0, 0, 175), outline=(255, 255, 255, 40), width=2)
    im.alpha_composite(_icone(plat, icone), (pad, (alt - icone) // 2))
    ty = (alt - (caixa[3] - caixa[1])) // 2 - caixa[1]
    d.text((pad + icone + 12, ty), url, font=fonte, fill=(255, 255, 255, 255))
    im.save(caminho)
    return True


def quadro(raw, t, ffmpeg):
    """Um quadro da live (16:9, 768 de largura) em JPEG, para a IA olhar. Fica salvo como cena.jpg na pasta do corte."""
    import subprocess
    destino = os.path.join(os.path.dirname(raw), "cena.jpg")
    # -ss depois do -i: no bruto.ts (pedaços .ts emendados) o salto rápido antes do -i não devolve quadro nenhum
    subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", raw, "-ss", f"{max(0.0, t):.2f}",
                    "-frames:v", "1", "-vf", "scale=768:-2:out_range=full,format=yuvj420p", "-q:v", "4", destino],
                   capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    try:
        with open(destino, "rb") as f:
            return f.read() or None
    except OSError:
        return None
