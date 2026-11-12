"""Thumbnails dos cortes.

1. Tira 8 quadros do corte (em volta do pico e espalhados), recortando só a imagem da live.
2. Com a chave do Gemini, a IA olha os quadros, escolhe os de reação/ação mais forte, diz onde está o
   foco da imagem e escreve 3 textos curtos. Sem a chave: quadro mais nítido perto do pico + textos do título.
3. O Pillow monta 3 variações em dois formatos: 1280x720 (thumbnail do YouTube) e 1080x1920
   (capa vertical para TikTok/Reels/Kwai).

Tudo fica em cortes_prontos/<id>/thumbs/. No painel dá para trocar quadro, texto, cor e layout.
"""
import os
import re
import threading
import time
import traceback
from typing import List

from pydantic import BaseModel

from . import clipper, config, titulos

LARG, ALT = 1280, 720
VLARG, VALT = 1080, 1920
N_QUADROS = 8
CORES = {"amarelo": "#FFE600", "branco": "#FFFFFF", "vermelho": "#FF2E4D", "verde": "#3DFF6E", "ciano": "#2EE6FF"}
LAYOUTS = ("lateral", "faixa", "topo")
FONTES = [r"C:\Windows\Fonts\impact.ttf", r"C:\Windows\Fonts\ariblk.ttf", r"C:\Windows\Fonts\arialbd.ttf"]
FONTE_TAG = [r"C:\Windows\Fonts\seguibl.ttf", r"C:\Windows\Fonts\ariblk.ttf", r"C:\Windows\Fonts\arialbd.ttf"]
# área da live dentro do corte 9:16 (o clipper põe a live em 1080 de largura, centralizada)
LIVE_CROP = clipper.LIVE_CROP  # (use clipper.fonte_16x9: nos cortes divididos a live fica no live.mp4)

_lock = threading.Lock()


def _pasta(cid):
    p = os.path.join(config.CLIPS_DIR, cid, "thumbs")
    os.makedirs(p, exist_ok=True)
    return p


def _url(cid, nome):
    return f"/arquivos/{cid}/thumbs/{nome}?v={int(time.time() * 1000)}"


def arquivo(cid, vertical=False):
    """Caminho da thumbnail escolhida (ou None)."""
    clip = clipper.get_clip(cid) or {}
    t = clip.get("thumbs") or {}
    vs = t.get("variacoes") or []
    if not vs:
        return None
    v = vs[min(t.get("escolhida", 0), len(vs) - 1)]
    p = os.path.join(_pasta(cid), v["arq_v" if vertical else "arq"])
    return p if os.path.exists(p) else None


# ------------------------------------------------------------------ 1. quadros
def _tempos(clip):
    d = float(clip.get("duracao") or 30)
    pico = float(clip.get("pico_rel") or d * 0.6)
    ts = [pico - 2, pico - 1, pico, pico + 0.7, pico + 1.5, d * 0.15, d * 0.4, d * 0.8]
    out = []
    for t in ts:
        t = round(min(max(t, 0.3), max(d - 0.3, 0.3)), 2)
        if all(abs(t - x) > 0.4 for x in out):
            out.append(t)
    i = 1
    while len(out) < N_QUADROS and i < 20:  # completa se os tempos se sobrepuseram (corte curto)
        t = round(d * i / 21, 2)
        if all(abs(t - x) > 0.3 for x in out):
            out.append(t)
        i += 1
    return sorted(out[:N_QUADROS])


def _extrair(cid, clip):
    pasta = _pasta(cid)
    video, recorte = clipper.fonte_16x9(cid)
    quadros = []
    for i, t in enumerate(_tempos(clip), 1):
        nome = f"quadro_{i}.jpg"
        clipper.run_ffmpeg(["-ss", f"{t:.2f}", "-i", video, "-frames:v", "1",
                            "-vf", f"{recorte}scale={LARG}:{ALT}:flags=lanczos", "-q:v", "2", nome], cwd=pasta)
        if os.path.exists(os.path.join(pasta, nome)):
            quadros.append({"arq": nome, "t": t})
    if not quadros:
        raise RuntimeError("não consegui tirar quadros do vídeo")
    return quadros


def _nitidez(caminho):
    from PIL import Image, ImageFilter, ImageStat
    im = Image.open(caminho).convert("L").resize((320, 180))
    return ImageStat.Stat(im.filter(ImageFilter.FIND_EDGES)).var[0]


# ------------------------------------------------------------------ 2. plano (IA ou automático)
class PlanoThumb(BaseModel):
    melhor_quadro: int
    segundo_quadro: int
    foco: str
    textos: List[str]


SISTEMA = """Você é designer de thumbnails de um canal brasileiro de cortes de lives.
Recebe quadros numerados de um corte e o contexto do que acontece nele.

- "melhor_quadro" e "segundo_quadro": números dos quadros com a reação mais forte (rosto com surpresa, grito,
  risada, raiva) ou a ação mais clara. Evite quadro borrado, menu, tela de carregamento, transição ou tela parada.
- "foco": onde está o rosto/ação principal no melhor quadro: "esquerda", "centro" ou "direita".
- "textos": 3 opções de texto para a thumbnail, cada uma com 2 a 4 palavras em CAIXA ALTA, português do Brasil,
  que gerem curiosidade e combinem com o que aconteceu. Não repita o título inteiro. Sem emoji, aspas ou hashtag.
  Seja fiel ao corte: nada de prometer algo que não está no vídeo."""


def _plano_ia(clip, pasta, quadros):
    from google import genai
    from google.genai import types
    from PIL import Image
    import io

    partes = [f"Título escolhido: {clip.get('titulo') or '-'}\n\n" + titulos._contexto(clip)]
    for i, q in enumerate(quadros, 1):
        im = Image.open(os.path.join(pasta, q["arq"])).convert("RGB")
        im.thumbnail((640, 360))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=80)
        partes.append(f"Quadro {i} (segundo {q['t']:.1f} do corte):")
        partes.append(types.Part(inline_data=types.Blob(data=buf.getvalue(), mime_type="image/jpeg")))
    cliente = genai.Client(api_key=titulos._chave_gemini())
    cfg = types.GenerateContentConfig(
        system_instruction=SISTEMA, response_mime_type="application/json",
        response_json_schema=titulos._schema_sem_refs(PlanoThumb), temperature=0.8,
    )
    resp = titulos.gemini_gerar(cliente, titulos.MODELOS_GEMINI, partes, cfg)
    p = PlanoThumb.model_validate_json(resp.text)
    n = len(quadros)
    m1 = min(max(p.melhor_quadro, 1), n) - 1
    m2 = min(max(p.segundo_quadro, 1), n) - 1
    textos = [_limpa_texto(t) for t in p.textos if _limpa_texto(t)]
    if not textos:
        return None
    foco = p.foco.lower() if p.foco.lower() in ("esquerda", "centro", "direita") else "centro"
    return {"ordem": [m1, m2 if m2 != m1 else (m1 + 1) % n], "foco": foco, "textos": textos[:3], "fonte": "ia"}


def _limpa_texto(t):
    t = re.sub(r"[\"“”#*]", "", str(t or "")).strip().upper()
    return " ".join(t.split()[:5])


def _plano_simples(clip, pasta, quadros):
    pico = float(clip.get("pico_rel") or 0)
    notas = []
    nit = [_nitidez(os.path.join(pasta, q["arq"])) for q in quadros]
    mx = max(nit) or 1
    for i, q in enumerate(quadros):
        notas.append((nit[i] / mx) - min(abs(q["t"] - pico), 6) / 12)  # nítido e perto do pico
    ordem = sorted(range(len(quadros)), key=lambda i: notas[i], reverse=True)
    soltas = {"de", "da", "do", "das", "dos", "que", "o", "a", "os", "as", "e", "em", "no", "na", "com", "um", "uma",
              "pra", "para", "essa", "esse", "nessa", "nesse", "isso", "por", "ao", "à", "se", "mais"}

    def palavras(s, n):
        ps = re.sub(r"[^\w\s!?]", " ", s or "").split()[:n]
        while len(ps) > 1 and ps[-1].lower() in soltas:  # não termina em palavra solta ("...NESSA")
            ps.pop()
        return " ".join(ps)
    cands = [_limpa_texto(palavras(clip.get("titulo"), 4)), _limpa_texto(palavras(clip.get("frase_pico"), 3)),
             "NINGUÉM ESPERAVA ISSO", "OLHA O QUE ACONTECEU"]
    textos = []
    for t in cands:
        if t and t not in textos:
            textos.append(t)
    return {"ordem": ordem[:2], "foco": "centro", "textos": textos[:3], "fonte": "auto"}


# ------------------------------------------------------------------ 3. montagem
def _fonte(tam, lista=FONTES):
    from PIL import ImageFont
    for f in lista:
        if os.path.exists(f):
            return ImageFont.truetype(f, tam)
    return ImageFont.load_default(tam)


def _quebrar(draw, texto, max_w, max_h, max_linhas, tam_max, tam_min=40):
    """Maior fonte em que o texto cabe na caixa, quebrando em até `max_linhas` linhas."""
    palavras = texto.split()
    for tam in range(tam_max, tam_min - 1, -4):
        f = _fonte(tam)
        sw = max(3, tam // 11)
        linhas, atual = [], ""
        for p in palavras:
            teste = (atual + " " + p).strip()
            if draw.textlength(teste, font=f) + 2 * sw <= max_w or not atual:
                atual = teste
            else:
                linhas.append(atual)
                atual = p
        if atual:
            linhas.append(atual)
        larguras = [draw.textlength(l, font=f) + 2 * sw for l in linhas]
        alt = len(linhas) * tam * 1.02
        if len(linhas) <= max_linhas and max(larguras) <= max_w and alt <= max_h:
            return f, sw, linhas, tam
    f = _fonte(tam_min)
    return f, max(3, tam_min // 11), [texto], tam_min


def _texto(img, linhas, f, sw, tam, x, y, alinhar, cores):
    """Escreve as linhas com contorno preto e sombra. x = esquerda/centro/direita conforme `alinhar`."""
    from PIL import Image, ImageDraw, ImageFilter
    sombra = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ds = ImageDraw.Draw(sombra)
    camada = Image.new("RGBA", img.size, (0, 0, 0, 0))
    dc = ImageDraw.Draw(camada)
    passo = int(tam * 1.02)
    for i, l in enumerate(linhas):
        w = dc.textlength(l, font=f)
        lx = x if alinhar == "esq" else (x - w / 2 if alinhar == "centro" else x - w)
        ly = y + i * passo
        off = max(4, tam // 14)
        ds.text((lx + off, ly + off), l, font=f, fill=(0, 0, 0, 200), stroke_width=sw, stroke_fill=(0, 0, 0, 200))
        dc.text((lx, ly), l, font=f, fill=cores[i] if isinstance(cores, list) else cores,
                stroke_width=sw, stroke_fill=(0, 0, 0, 255))
    sombra = sombra.filter(ImageFilter.GaussianBlur(max(3, tam // 18)))
    img.alpha_composite(sombra)
    img.alpha_composite(camada)


def _gradiente(img, lado, forca=200, alcance=0.6):
    """Escurece um lado da imagem (esquerda/direita/baixo/cima) para o texto saltar."""
    from PIL import Image
    w, h = img.size
    g = Image.new("L", (256, 1))
    g.putdata([int(forca * max(0.0, 1 - i / (255 * alcance))) for i in range(256)])
    if lado in ("esquerda", "direita"):
        g = g.resize((w, h))
        if lado == "direita":
            g = g.transpose(Image.FLIP_LEFT_RIGHT)
    else:
        g = g.rotate(-90, expand=True).resize((w, h))
        if lado == "baixo":
            g = g.transpose(Image.FLIP_TOP_BOTTOM)
    preto = Image.new("RGBA", (w, h), (0, 0, 0, 255))
    preto.putalpha(g)
    img.alpha_composite(preto)


def _tag(img, canal, x, y, ancora="esq", tam=None):
    """Etiqueta com o nome do streamer. ancora: esq (x = borda esquerda), dir (x = borda direita) ou centro."""
    from PIL import ImageDraw
    if not canal:
        return
    d = ImageDraw.Draw(img)
    tam = tam or max(26, img.size[0] // 40)
    f = _fonte(tam, FONTE_TAG)
    txt = canal.upper()[:24]
    w = d.textlength(txt, font=f)
    px, py = tam * 0.6, tam * 0.35
    x0 = x if ancora == "esq" else (x - w / 2 - px if ancora == "centro" else x - w - 2 * px)
    d.rounded_rectangle([x0, y, x0 + w + 2 * px, y + tam + 2 * py], radius=int(tam * 0.5), fill="#FF2E4D")
    d.text((x0 + px, y + py - tam * 0.12), txt, font=f, fill="white")


def _base(caminho, tamanho):
    from PIL import Image, ImageEnhance, ImageOps
    im = Image.open(caminho).convert("RGB")
    im = ImageOps.fit(im, tamanho, Image.LANCZOS)
    im = ImageEnhance.Color(im).enhance(1.25)
    im = ImageEnhance.Contrast(im).enhance(1.12)
    im = ImageEnhance.Sharpness(im).enhance(1.4)
    return im.convert("RGBA")


def montar(quadro, texto, cor, layout, foco, canal):
    """Thumbnail 1280x720."""
    from PIL import ImageDraw
    img = _base(quadro, (LARG, ALT))
    d = ImageDraw.Draw(img)
    cor = CORES.get(cor, cor)
    m = 44
    if layout == "lateral":
        lado = "direita" if foco == "esquerda" else "esquerda"
        _gradiente(img, lado, 215, 0.65)
        f, sw, linhas, tam = _quebrar(d, texto, LARG * 0.52, ALT * 0.8, 3, 170)
        y = (ALT - len(linhas) * tam * 1.02) / 2 - tam * 0.08
        if lado == "esquerda":
            _texto(img, linhas, f, sw, tam, m, y, "esq", cor)
            _tag(img, canal, LARG - m, ALT - m - 50, "dir")
        else:
            _texto(img, linhas, f, sw, tam, LARG - m, y, "dir", cor)
            _tag(img, canal, m, ALT - m - 50)
    elif layout == "faixa":
        _gradiente(img, "baixo", 230, 0.55)
        f, sw, linhas, tam = _quebrar(d, texto, LARG * 0.92, ALT * 0.38, 2, 150)
        y = ALT - m - len(linhas) * tam * 1.02
        _texto(img, linhas, f, sw, tam, LARG / 2, y, "centro", cor)
        _tag(img, canal, m, m)
    else:  # topo
        _gradiente(img, "cima", 220, 0.55)
        f, sw, linhas, tam = _quebrar(d, texto, LARG * 0.92, ALT * 0.42, 2, 150)
        cores = ["#FFFFFF"] * (len(linhas) - 1) + [cor] if len(linhas) > 1 else [cor]
        _texto(img, linhas, f, sw, tam, LARG / 2, m - tam * 0.05, "centro", cores)
        _tag(img, canal, m, ALT - m - 50)
    return img.convert("RGB")


def montar_vertical(quadro, texto, cor, canal):
    """Capa 1080x1920: fundo desfocado, a live no meio e o texto em cima (igual ao corte, sem legenda)."""
    from PIL import Image, ImageDraw, ImageEnhance, ImageFilter
    base = _base(quadro, (LARG, ALT))
    fundo = base.convert("RGB").resize((VALT * LARG // ALT, VALT)).crop(
        ((VALT * LARG // ALT - VLARG) // 2, 0, (VALT * LARG // ALT + VLARG) // 2, VALT))
    fundo = ImageEnhance.Brightness(fundo.filter(ImageFilter.GaussianBlur(40))).enhance(0.55).convert("RGBA")
    live = base.resize((VLARG, VLARG * ALT // LARG), Image.LANCZOS)
    y_live = 760
    fundo.alpha_composite(live, (0, y_live))
    d = ImageDraw.Draw(fundo)
    f, sw, linhas, tam = _quebrar(d, texto, VLARG * 0.9, y_live - 160, 3, 190, 60)
    h = len(linhas) * tam * 1.02
    _texto(fundo, linhas, f, sw, tam, VLARG / 2, (y_live - h) / 2 + 20, "centro", CORES.get(cor, cor))
    _tag(fundo, canal, VLARG / 2, y_live + live.size[1] + 50, "centro", tam=44)
    return fundo.convert("RGB")


def _salvar(img, caminho):
    img.save(caminho, "JPEG", quality=90, optimize=True, progressive=True)
    if os.path.getsize(caminho) > 1_900_000:  # limite do YouTube: 2 MB
        img.save(caminho, "JPEG", quality=78, optimize=True)


def _render(cid, nome, var, canal):
    pasta = _pasta(cid)
    q = os.path.join(pasta, var["quadro"])
    _salvar(montar(q, var["texto"], var["cor"], var["layout"], var.get("foco", "centro"), canal), os.path.join(pasta, nome + ".jpg"))
    _salvar(montar_vertical(q, var["texto"], var["cor"], canal), os.path.join(pasta, nome + "_v.jpg"))
    var.update(arq=nome + ".jpg", arq_v=nome + "_v.jpg", img=_url(cid, nome + ".jpg"), img_v=_url(cid, nome + "_v.jpg"))
    return var


# ------------------------------------------------------------------ geração
def gerar_agora(cid):
    """Gera tudo (quadros + 3 variações). Bloqueia: rode numa thread."""
    with _lock:
        clip = clipper.get_clip(cid)
        if not clip or clip.get("status") != "pronto":
            raise ValueError("Corte não encontrado ou ainda processando.")
        clipper.update_clip(cid, thumbs={**(clip.get("thumbs") or {}), "status": "gerando", "erro": None})
        try:
            pasta = _pasta(cid)
            quadros = _extrair(cid, clip)
            plano, aviso = None, None
            if titulos._chave_gemini():
                try:
                    plano = _plano_ia(clip, pasta, quadros)
                except Exception as e:  # noqa - sem internet, limite grátis...
                    traceback.print_exc()
                    aviso = f"IA indisponível ({type(e).__name__}); usei a escolha automática."
            plano = plano or _plano_simples(clip, pasta, quadros)
            o, t, foco = plano["ordem"], plano["textos"], plano["foco"]
            t = (t * 3)[:3]
            q1 = quadros[o[0]]["arq"]
            q2 = quadros[o[1] if len(o) > 1 else o[0]]["arq"]
            canal = clip.get("canal") or ""
            variacoes = [
                {"quadro": q1, "texto": t[0], "cor": "amarelo", "layout": "lateral", "foco": foco},
                {"quadro": q2, "texto": t[1], "cor": "branco", "layout": "faixa", "foco": foco},
                {"quadro": q1, "texto": t[2], "cor": "vermelho", "layout": "topo", "foco": foco},
            ]
            for i, v in enumerate(variacoes, 1):
                _render(cid, f"var_{i}", v, canal)
            clipper.update_clip(cid, thumbs={
                "status": "pronto", "fonte": plano["fonte"], "aviso": aviso, "foco": foco, "escolhida": 0,
                "quadros": [{**q, "img": _url(cid, q["arq"])} for q in quadros],
                "variacoes": variacoes, "textos": t, "gerado_em": time.strftime("%Y-%m-%dT%H:%M:%S"),
            })
        except Exception as e:  # noqa
            traceback.print_exc()
            clipper.update_clip(cid, thumbs={**(clipper.get_clip(cid).get("thumbs") or {}), "status": "erro",
                                             "erro": str(e)[-300:]})
            raise
    return clipper.get_clip(cid)["thumbs"]


def gerar(cid):
    """Gera em segundo plano (o painel acompanha pelo status)."""
    clip = clipper.get_clip(cid)
    if not clip or clip.get("status") != "pronto":
        raise ValueError("Corte não encontrado ou ainda processando.")
    if (clip.get("thumbs") or {}).get("status") == "gerando":
        return clip["thumbs"]
    clipper.update_clip(cid, thumbs={**(clip.get("thumbs") or {}), "status": "gerando", "erro": None})

    def rodar():
        try:
            gerar_agora(cid)
        except Exception:  # noqa - o erro já fica salvo no corte
            pass
    threading.Thread(target=rodar, daemon=True).start()
    return clipper.get_clip(cid)["thumbs"]


def personalizar(cid, quadro, texto, cor, layout):
    """Monta (ou refaz) a variação 'personalizada' e já deixa ela escolhida."""
    clip = clipper.get_clip(cid) or {}
    t = clip.get("thumbs") or {}
    if t.get("status") != "pronto":
        raise ValueError("Gere as thumbnails primeiro.")
    if quadro not in {q["arq"] for q in t.get("quadros") or []}:
        raise ValueError("Quadro inválido.")
    texto = _limpa_texto(texto)
    if not texto:
        raise ValueError("Escreva um texto.")
    if cor not in CORES or layout not in LAYOUTS:
        raise ValueError("Cor ou layout inválido.")
    var = {"quadro": quadro, "texto": texto, "cor": cor, "layout": layout, "foco": t.get("foco", "centro"), "personalizada": True}
    with _lock:
        _render(cid, "personalizada", var, clip.get("canal") or "")
    vs = [v for v in t["variacoes"] if not v.get("personalizada")] + [var]
    t.update(variacoes=vs, escolhida=len(vs) - 1)
    clipper.update_clip(cid, thumbs=t)
    return t


def escolher(cid, indice):
    clip = clipper.get_clip(cid) or {}
    t = clip.get("thumbs") or {}
    if not 0 <= indice < len(t.get("variacoes") or []):
        raise ValueError("Variação inválida.")
    t["escolhida"] = indice
    clipper.update_clip(cid, thumbs=t)
    return t
