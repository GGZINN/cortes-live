"""Roteiros: a IA escreve roteiros de Short, de compilado e de vídeo original, que ficam salvos e editáveis.

O compilado usa os cortes prontos: a IA ordena, dá nome aos capítulos e escreve as pontes. Depois dá para
montar o vídeo 16:9 sozinho (cartela de abertura, uma cartela por capítulo, os cortes e o encerramento),
com capítulos na descrição, thumbnail e envio ao YouTube.

Sem chave de IA, cria um modelo pronto para preencher.
"""
import json
import os
import re
import shutil
import subprocess
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from typing import List

from pydantic import BaseModel

from . import clipper, config, thumbs, titulos, youtube

ARQ = os.path.join(config.DATA_DIR, "roteiros.json")
PASTA = os.path.join(config.CLIPS_DIR, "_roteiros")  # servido em /arquivos/_roteiros/
TIPOS = ("short", "compilado", "original")
MAX_CORTES = 20
W, H = 1920, 1080
CARTELA_S = 2.5
ABERTURA_S = 3.0
FINAL_S = 4.0

_lock = threading.RLock()
_roteiros = []


# ------------------------------------------------------------------ armazenamento
def _save():
    config.salvar_json(ARQ, _roteiros, indent=1)


def _carregar():
    global _roteiros
    if not os.path.exists(ARQ):
        return
    try:
        with open(ARQ, encoding="utf-8") as f:
            _roteiros = json.load(f)
        for r in _roteiros:  # trabalhos interrompidos ao fechar o programa
            if r.get("status") == "gerando":
                r.update(status="erro", erro="O programa foi fechado enquanto o roteiro era escrito. Gere de novo.")
            if (r.get("montagem") or {}).get("status") == "montando":
                r["montagem"].update(status="erro", erro="O programa foi fechado durante a montagem. Monte de novo.")
            if (r.get("youtube") or {}).get("status") in ("na fila", "enviando"):
                r["youtube"].update(status="erro", erro="O programa foi fechado durante o envio. Envie de novo.")
    except Exception:  # noqa
        traceback.print_exc()


def _agora():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _achar(rid):
    for r in _roteiros:
        if r["id"] == rid:
            return r
    raise ValueError("Roteiro não encontrado.")


def _atualiza(rid, **kw):
    with _lock:
        r = _achar(rid)
        for k, v in kw.items():
            if isinstance(v, dict) and isinstance(r.get(k), dict) and k in ("montagem", "youtube"):
                r[k].update(v)
            else:
                r[k] = v
        _save()
        return json.loads(json.dumps(r))


def _pasta(rid):
    p = os.path.join(PASTA, rid)
    os.makedirs(p, exist_ok=True)
    return p


def _url(rid, nome):
    return f"/arquivos/_roteiros/{rid}/{nome}?v={int(time.time() * 1000)}"


def listar():
    with _lock:
        resumo = [{k: r.get(k) for k in ("id", "tipo", "titulo", "status", "criado", "atualizado", "fonte")}
                  | {"montado": (r.get("montagem") or {}).get("status") == "pronto",
                     "no_youtube": bool((r.get("youtube") or {}).get("id")),
                     "n_secoes": len(r.get("secoes") or []), "tema": (r.get("pedido") or {}).get("tema")}
                  for r in _roteiros]
    return sorted(resumo, key=lambda r: r["criado"], reverse=True)


def videos_no_youtube():
    """{id do vídeo no YouTube: dados do roteiro} dos vídeos que saíram daqui (para o painel do canal)."""
    with _lock:
        return {r["youtube"]["id"]: {"rid": r["id"], "tipo": r.get("tipo"), "titulo": r.get("titulo"),
                                     "publicar_em": r["youtube"].get("publicar_em")}
                for r in _roteiros if (r.get("youtube") or {}).get("id")}


def obter(rid):
    with _lock:
        return json.loads(json.dumps(_achar(rid)))


def cortes_disponiveis():
    cs = [c for c in clipper.list_clips() if c.get("status") == "pronto"
          and os.path.exists(os.path.join(config.CLIPS_DIR, c["id"], "corte.mp4"))]
    cs.sort(key=lambda c: str(c.get("criado", "")), reverse=True)
    return [{"id": c["id"], "titulo": c.get("titulo") or c.get("motivo"), "canal": c.get("canal"), "capa": c.get("capa"),
             "duracao": c.get("duracao"), "escolhido": c.get("escolhido") is True, "criado": c.get("criado"),
             "nota": c.get("pontuacao")} for c in cs[:200]]


# ------------------------------------------------------------------ IA
class Secao(BaseModel):
    titulo: str
    fala: str
    na_tela: str
    corte: int
    duracao_s: int


class RoteiroIA(BaseModel):
    titulos: List[str]
    thumb_texto: str
    gancho: str
    secoes: List[Secao]
    encerramento: str
    descricao: str
    tags: List[str]


SISTEMA = """Você é roteirista de um canal brasileiro do YouTube que faz cortes e vídeos sobre streamers.
Seus roteiros seguram a audiência: gancho forte nos primeiros segundos, promessa clara do que vem, ritmo sem
enrolação, curiosidade aberta até o fim, recompensa que cumpre a promessa e chamada para ação natural no final.

Regras:
- Português do Brasil falado, natural, como um criador de conteúdo fala: frases curtas, gírias leves.
- Seja fiel: não invente fatos, falas ou acontecimentos sobre pessoas reais. O que a pessoa precisar conferir ou
  completar, deixe [entre colchetes].
- "titulos": 5 opções de título (até 70 caracteres), bem diferentes entre si.
- "thumb_texto": 2 a 4 palavras em CAIXA ALTA para a thumbnail.
- "gancho": a primeira fala do vídeo.
- "secoes": as partes do vídeo, na ordem. "titulo" = nome curto da parte; "fala" = o texto falado (narração);
  "na_tela" = o que aparece (imagem, corte, texto na tela, efeito); "duracao_s" = duração estimada em segundos;
  "corte" = número do corte usado nessa parte (0 se nenhum).
- "encerramento": a fala final, com chamada para ação.
- "descricao": 2 a 4 frases para a descrição do YouTube, sem hashtags (créditos e capítulos entram depois).
- "tags": 8 a 15 tags de busca, minúsculas, sem #."""


def _pedido_texto(tipo, p, cortes):
    tema = (p.get("tema") or "").strip()
    if tipo == "short":
        dur = int(p.get("duracao") or 45)
        return (f"Formato: SHORT vertical de até {dur} segundos.\nTema/ideia: {tema or '-'}\n"
                + (f"Streamer/assunto principal: {p['canal']}\n" if p.get("canal") else "")
                + (f"Estilo: {p['estilo']}\n" if p.get("estilo") else "")
                + f"Faça de 4 a 7 seções bem curtas, somando no máximo {dur} segundos. O gancho tem que prender em "
                  "até 2 segundos. Nada de introdução tipo \"fala, galera\".")
    if tipo == "original":
        mins = int(p.get("duracao") or 8)
        return (f"Formato: vídeo horizontal de cerca de {mins} minutos (umas {mins * 150} palavras de fala).\n"
                f"Tema: {tema or '-'}\n"
                + (f"Streamer/assunto principal: {p['canal']}\n" if p.get("canal") else "")
                + (f"Público e estilo: {p['estilo']}\n" if p.get("estilo") else "")
                + "Estruture em 5 a 10 seções com a fala completa, pronta para ler. Comece cada seção com um "
                  "mini-gancho para segurar a pessoa até o fim.")
    linhas = []
    for i, c in enumerate(cortes, 1):
        oque = c.get("por_que") or c.get("gancho") or c.get("motivo") or ""
        fala = re.sub(r"\s+", " ", c.get("transcricao") or "")[:300]
        linhas.append(f"Corte {i} — {c.get('canal')} — {round(c.get('duracao') or 0)} s — título: {c.get('titulo') or '-'}"
                      f" — o que acontece: {oque or '-'} — fala: \"{fala}\"")
    return ("Formato: COMPILADO horizontal com os cortes abaixo. O vídeo é montado assim: cartela de abertura com o "
            "título e, para cada seção, uma cartela com o \"titulo\" da seção seguida do corte.\n"
            + (f"Ideia do compilado: {tema}\n" if tema else "")
            + "Regras do compilado:\n"
              "- Use TODOS os cortes, cada um uma vez só: uma seção por corte, com \"corte\" = número dele.\n"
              "- Ordem: comece com um corte muito forte, mantenha o ritmo e deixe o melhor para o final.\n"
              "- \"titulo\" da seção = nome do capítulo (até 40 caracteres) que gera curiosidade sem entregar o final.\n"
              "- \"fala\" = narração opcional de ponte antes do corte (uma frase curta) ou vazio.\n"
              "- \"na_tela\" = o que acontece no corte, em poucas palavras.\n"
              "- \"duracao_s\" = duração do corte.\n\nCortes:\n" + "\n".join(linhas))


def _ia(conteudo):
    if titulos._chave_gemini():
        from google import genai
        from google.genai import types
        cliente = genai.Client(api_key=titulos._chave_gemini())
        cfg = types.GenerateContentConfig(system_instruction=SISTEMA, response_mime_type="application/json",
                                          response_json_schema=titulos._schema_sem_refs(RoteiroIA), temperature=0.9)
        resp = titulos.gemini_gerar(cliente, titulos.MODELOS_GEMINI, conteudo, cfg)
        return RoteiroIA.model_validate_json(resp.text), "Gemini"
    if titulos._chave_claude():
        import anthropic
        cliente = anthropic.Anthropic(api_key=titulos._chave_claude())
        resp = cliente.beta.messages.parse(
            model=titulos.MODELO_CLAUDE, max_tokens=16000, betas=["server-side-fallback-2026-07-01"],
            fallbacks="default", output_config={"effort": "medium"}, system=SISTEMA,
            messages=[{"role": "user", "content": conteudo}], output_format=RoteiroIA,
        )
        if resp.stop_reason != "end_turn" or not resp.parsed_output:
            raise RuntimeError("a Claude não terminou o roteiro")
        return resp.parsed_output, "Claude"
    return None, None


def _modelo(tipo, p, cortes):
    """Roteiro em branco (sem IA), pronto para preencher."""
    tema = (p.get("tema") or "").strip()
    canal = p.get("canal") or ""
    if tipo == "compilado":
        ordem = sorted(cortes, key=lambda c: float(c.get("pontuacao") or 0))  # melhor por último
        nome = canal or "os streamers"
        return {
            "titulos": [f"Os momentos mais insanos de {nome}", f"{nome}: os melhores cortes da semana",
                        f"TOP {len(cortes)} momentos de {nome}"],
            "thumb_texto": "MELHORES MOMENTOS",
            "gancho": f"Separei os {len(cortes)} momentos mais absurdos de {nome}. E o último... você não tá pronto.",
            "secoes": [{"titulo": (c.get("titulo") or "Momento")[:40], "fala": "", "na_tela": c.get("por_que") or "",
                        "cid": c["id"], "duracao_s": round(c.get("duracao") or 0)} for c in ordem],
            "encerramento": "Se curtiu, se inscreve e ativa o sininho pra não perder os próximos compilados!",
            "descricao": f"Os melhores momentos de {nome} em um vídeo só.",
            "tags": ["cortes", "melhores momentos", "compilado"] + ([canal.lower()] if canal else []),
        }
    partes = (["Gancho", "Contexto", "Virada", "Desfecho"] if tipo == "short"
              else ["Gancho", "Contexto", "Parte 1", "Parte 2", "Parte 3", "Conclusão"])
    return {
        "titulos": [tema[:70] or "Título do vídeo"], "thumb_texto": " ".join(tema.upper().split()[:3]) or "TEXTO DA THUMB",
        "gancho": "[a primeira frase: tem que prender em até 2 segundos]",
        "secoes": [{"titulo": t, "fala": "[escreva a fala]", "na_tela": "[o que aparece]", "cid": None, "duracao_s": 0}
                   for t in partes],
        "encerramento": "[chamada para ação: segue, comenta, assiste o próximo]",
        "descricao": tema, "tags": [canal.lower()] if canal else [],
    }


def _escrever(rid):
    """Gera o conteúdo do roteiro (roda numa thread)."""
    try:
        r = obter(rid)
        tipo, p = r["tipo"], r["pedido"]
        cortes = [c for c in (clipper.get_clip(cid) for cid in p.get("cortes") or []) if c]
        if tipo == "compilado" and not cortes:
            raise ValueError("Os cortes escolhidos não existem mais.")
        conteudo, aviso, fonte, provedor = None, None, "modelo", None
        try:
            ia, provedor = _ia(_pedido_texto(tipo, p, cortes))
            if ia:
                conteudo, fonte = _da_ia(ia, tipo, cortes), "ia"
        except Exception as e:  # noqa - sem internet, chave inválida, limite grátis...
            traceback.print_exc()
            aviso = f"IA indisponível ({type(e).__name__}). Criei um modelo para você preencher."
        if conteudo is None:
            conteudo = _modelo(tipo, p, cortes)
            if not aviso and not provedor:
                aviso = "Sem chave do Gemini: criei um modelo para você preencher. Com a chave, a IA escreve o roteiro."
        _atualiza(rid, **conteudo, titulo=(conteudo["titulos"] or [""])[0], status="pronto", erro=None,
                  fonte=fonte, provedor=provedor, aviso=aviso, atualizado=_agora())
    except Exception as e:  # noqa
        traceback.print_exc()
        _atualiza(rid, status="erro", erro=str(e)[:300])


def _da_ia(ia, tipo, cortes):
    secoes = []
    usados = set()
    for s in ia.secoes:
        cid = None
        if tipo == "compilado":
            if not 1 <= s.corte <= len(cortes) or s.corte in usados:
                continue  # número inválido ou repetido
            usados.add(s.corte)
            cid = cortes[s.corte - 1]["id"]
        secoes.append({"titulo": s.titulo.strip()[:80], "fala": s.fala.strip(), "na_tela": s.na_tela.strip(),
                       "cid": cid, "duracao_s": max(0, int(s.duracao_s))})
    if tipo == "compilado":  # a IA esqueceu algum corte: entra no fim
        for i, c in enumerate(cortes, 1):
            if i not in usados:
                secoes.append({"titulo": (c.get("titulo") or "Momento")[:40], "fala": "", "na_tela": c.get("por_que") or "",
                               "cid": c["id"], "duracao_s": round(c.get("duracao") or 0)})
        for s in secoes:
            s["duracao_s"] = round((clipper.get_clip(s["cid"]) or {}).get("duracao") or s["duracao_s"])
    return {
        "titulos": [t.strip()[:100] for t in ia.titulos if t.strip()][:6] or ["Sem título"],
        "thumb_texto": thumbs._limpa_texto(ia.thumb_texto) or "ASSISTA ATÉ O FIM",
        "gancho": ia.gancho.strip(), "secoes": secoes, "encerramento": ia.encerramento.strip(),
        "descricao": ia.descricao.strip(),
        "tags": [re.sub(r"\s+", " ", t.lstrip("#").strip().lower()) for t in ia.tags if t.strip()][:15],
    }


# ------------------------------------------------------------------ criar / editar
def criar(tipo, tema="", canal="", duracao=None, estilo="", cortes=None):
    if tipo not in TIPOS:
        raise ValueError("Tipo de roteiro inválido.")
    cortes = [c for c in (cortes or []) if clipper.get_clip(c)]
    if tipo == "compilado":
        if len(cortes) < 2:
            raise ValueError("Escolha pelo menos 2 cortes para o compilado.")
        if len(cortes) > MAX_CORTES:
            raise ValueError(f"No máximo {MAX_CORTES} cortes por compilado.")
        canais = {(clipper.get_clip(c) or {}).get("canal") for c in cortes}
        canal = canal or (canais.pop() if len(canais) == 1 else "")
    elif not (tema or "").strip():
        raise ValueError("Escreva o tema do roteiro.")
    r = {
        "id": uuid.uuid4().hex[:10], "tipo": tipo, "status": "gerando", "criado": _agora(), "atualizado": _agora(),
        "pedido": {"tema": (tema or "").strip()[:2000], "canal": (canal or "").strip()[:60], "duracao": duracao,
                   "estilo": (estilo or "").strip()[:500], "cortes": cortes},
        "titulo": "", "titulos": [], "thumb_texto": "", "gancho": "", "secoes": [], "encerramento": "",
        "descricao": "", "tags": [], "montagem": {}, "youtube": {},
    }
    with _lock:
        _roteiros.append(r)
        _save()
    threading.Thread(target=_escrever, args=(r["id"],), daemon=True).start()
    return obter(r["id"])


def regerar(rid):
    with _lock:
        r = _achar(rid)
        if r["status"] == "gerando":
            return obter(rid)
        r.update(status="gerando", erro=None)
        _save()
    threading.Thread(target=_escrever, args=(rid,), daemon=True).start()
    return obter(rid)


CAMPOS = ("titulo", "titulos", "thumb_texto", "gancho", "secoes", "encerramento", "descricao", "tags")


def salvar(rid, novo):
    with _lock:
        r = _achar(rid)
        if r["status"] == "gerando":
            raise ValueError("Espere a IA terminar de escrever.")
        mudou_video = False
        for k in CAMPOS:
            if k not in novo:
                continue
            v = novo[k]
            if k == "secoes":
                secoes = []
                for s in v or []:
                    cid = s.get("cid") or None
                    if cid and not clipper.get_clip(cid):
                        continue
                    secoes.append({"titulo": str(s.get("titulo") or "")[:120], "fala": str(s.get("fala") or ""),
                                   "na_tela": str(s.get("na_tela") or ""), "cid": cid,
                                   "duracao_s": int(float(s.get("duracao_s") or 0))})
                mudou_video |= [(s["titulo"], s["cid"]) for s in secoes] != [(s["titulo"], s["cid"]) for s in r["secoes"]]
                v = secoes
            elif k in ("titulos", "tags"):
                v = [str(x).strip() for x in v or [] if str(x).strip()]
            else:
                v = str(v or "")
                mudou_video |= k == "titulo" and v != r.get("titulo")
            r[k] = v
        if mudou_video and (r.get("montagem") or {}).get("status") == "pronto":
            r["montagem"]["desatualizada"] = True
        r["atualizado"] = _agora()
        _save()
        return json.loads(json.dumps(r))


def apagar(rid):
    with _lock:
        r = _achar(rid)
        if (r.get("montagem") or {}).get("status") == "montando" or (r.get("youtube") or {}).get("status") == "enviando":
            raise ValueError("Espere a montagem ou o envio terminar.")
        _roteiros.remove(r)
        _save()
    shutil.rmtree(os.path.join(PASTA, rid), ignore_errors=True)


# ------------------------------------------------------------------ montagem do compilado
def _tempo(s):
    s = int(s)
    h, m, x = s // 3600, s % 3600 // 60, s % 60
    return f"{h}:{m:02d}:{x:02d}" if h else f"{m:02d}:{x:02d}"


def _cartela(saida, grande, pequeno="", fundo=None, cor="#FFE600"):
    from PIL import Image, ImageDraw, ImageEnhance, ImageFilter
    if fundo and os.path.exists(fundo):
        img = Image.open(fundo).convert("RGB").resize((W, H))
        img = ImageEnhance.Brightness(img.filter(ImageFilter.GaussianBlur(28))).enhance(0.38).convert("RGBA")
    else:
        img = Image.new("RGBA", (W, H), (14, 15, 20, 255))
    d = ImageDraw.Draw(img)
    f, sw, linhas, tam = thumbs._quebrar(d, (grande or "").upper(), W * 0.86, H * 0.5, 3, 150, 56)
    alt = len(linhas) * tam * 1.02
    y = (H - alt) / 2 + (55 if pequeno else 0)
    if pequeno:
        fp = thumbs._fonte(84, thumbs.FONTE_TAG)
        wp = d.textlength(pequeno, font=fp)
        d.text(((W - wp) / 2, y - 130), pequeno, font=fp, fill=cor, stroke_width=4, stroke_fill="black")
    thumbs._texto(img, linhas, f, sw, tam, W / 2, y, "centro", "#FFFFFF")
    img.convert("RGB").save(saida, "JPEG", quality=92)


VIDEO = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-r", "30",
         "-video_track_timescale", "15360", "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2"]


def _seg_cartela(pasta, img, saida, dur):
    clipper.run_ffmpeg(["-loop", "1", "-framerate", "30", "-t", f"{dur}", "-i", img,
                        "-f", "lavfi", "-t", f"{dur}", "-i", "anullsrc=r=48000:cl=stereo",
                        "-vf", f"fade=t=in:st=0:d=0.25,fade=t=out:st={dur - 0.3:.2f}:d=0.3,format=yuv420p",
                        *VIDEO, "-shortest", saida], cwd=pasta)


def _seg_corte(pasta, cid, saida):
    video, recorte = clipper.fonte_16x9(cid)  # só a imagem da live, sem legenda (nos cortes divididos, o live.mp4)
    clipper.run_ffmpeg(["-i", video, "-vf", f"{recorte}scale={W}:{H}:flags=lanczos,setsar=1,fps=30,format=yuv420p",
                        "-af", "aresample=48000", *VIDEO, saida], cwd=pasta)


def montar(rid):
    with _lock:
        r = _achar(rid)
        if r["tipo"] != "compilado":
            raise ValueError("Só o compilado vira vídeo montado.")
        if r["status"] != "pronto":
            raise ValueError("O roteiro ainda não está pronto.")
        if (r.get("montagem") or {}).get("status") == "montando":
            return obter(rid)
        secoes = [s for s in r["secoes"] if s.get("cid") and os.path.exists(os.path.join(config.CLIPS_DIR, s["cid"], "corte.mp4"))]
        if len(secoes) < 2:
            raise ValueError("O compilado precisa de pelo menos 2 cortes que ainda existam.")
        r["montagem"] = {"status": "montando", "progresso": 0, "etapa": "preparando", "erro": None}
        _save()
    threading.Thread(target=_montar_agora, args=(rid,), daemon=True).start()
    return obter(rid)


def _montar_agora(rid):
    try:
        r = obter(rid)
        pasta = _pasta(rid)
        for f in os.listdir(pasta):  # limpa a montagem anterior
            if f.startswith(("seg_", "bg_", "cartela_")):
                os.remove(os.path.join(pasta, f))
        secoes = [s for s in r["secoes"] if s.get("cid") and os.path.exists(os.path.join(config.CLIPS_DIR, s["cid"], "corte.mp4"))]
        total = 2 + 2 * len(secoes) + 2
        feito = [0]

        def passo(etapa):
            feito[0] += 1
            _atualiza(rid, montagem={"progresso": round(feito[0] / total, 3), "etapa": etapa})

        segs = []
        # fundos das cartelas: um quadro do pico de cada corte
        for i, s in enumerate(secoes):
            clip = clipper.get_clip(s["cid"]) or {}
            pico = float(clip.get("pico_rel") or (clip.get("duracao") or 10) * 0.6)
            video, recorte = clipper.fonte_16x9(s["cid"])
            clipper.run_ffmpeg(["-ss", f"{pico:.2f}", "-i", video,
                                "-frames:v", "1", "-vf", f"{recorte}scale={W}:{H}", "-q:v", "3", f"bg_{i}.jpg"], cwd=pasta)
        titulo = r.get("titulo") or (r.get("titulos") or ["Compilado"])[0]
        _cartela(os.path.join(pasta, "cartela_abertura.jpg"), titulo, (r["pedido"].get("canal") or "").upper(),
                 os.path.join(pasta, f"bg_{len(secoes) - 1}.jpg"))
        _seg_cartela(pasta, "cartela_abertura.jpg", "seg_000.mp4", ABERTURA_S)
        segs.append(("seg_000.mp4", None))
        passo("abertura")

        n = len(secoes)
        for i, s in enumerate(secoes):
            nome_c, nome_v = f"seg_{2 * i + 1:03d}.mp4", f"seg_{2 * i + 2:03d}.mp4"
            _cartela(os.path.join(pasta, f"cartela_{i}.jpg"), s["titulo"] or f"Momento {i + 1}", f"#{n - i}" if n > 2 else "",
                     os.path.join(pasta, f"bg_{i}.jpg"))
            _seg_cartela(pasta, f"cartela_{i}.jpg", nome_c, CARTELA_S)
            segs.append((nome_c, i))
            passo(f"cartela {i + 1} de {n}")
            _seg_corte(pasta, s["cid"], nome_v)
            segs.append((nome_v, None))
            passo(f"corte {i + 1} de {n}")

        _cartela(os.path.join(pasta, "cartela_final.jpg"), "Inscreva-se para mais", "OBRIGADO POR ASSISTIR",
                 os.path.join(pasta, "bg_0.jpg"))
        nome_f = f"seg_{2 * n + 1:03d}.mp4"
        _seg_cartela(pasta, "cartela_final.jpg", nome_f, FINAL_S)
        segs.append((nome_f, None))

        with open(os.path.join(pasta, "lista.txt"), "w", encoding="utf-8") as f:
            f.write("".join(f"file '{nome}'\n" for nome, _ in segs))
        clipper.run_ffmpeg(["-f", "concat", "-safe", "0", "-i", "lista.txt", "-c", "copy", "-movflags", "+faststart",
                            "compilado.mp4"], cwd=pasta)
        passo("juntando")

        # capítulos: cada um começa na cartela dele; o primeiro em 00:00 (exigência do YouTube)
        t, capitulos = 0.0, []
        for nome, idx in segs:
            if idx is not None:
                capitulos.append((0.0 if idx == 0 else t, secoes[idx]["titulo"] or f"Momento {idx + 1}"))
            t += clipper.duracao(os.path.join(pasta, nome))
        cap_txt = "\n".join(f"{_tempo(a)} {b}" for a, b in capitulos)

        thumb = _thumb(rid, r, pasta, len(secoes) - 1)
        for f in os.listdir(pasta):
            if f.startswith("seg_"):
                os.remove(os.path.join(pasta, f))
        _atualiza(rid, montagem={"status": "pronto", "progresso": 1, "etapa": "pronto", "desatualizada": False,
                                 "arquivo": _url(rid, "compilado.mp4"), "duracao": round(t), "capitulos": cap_txt,
                                 "thumb": thumb, "montado_em": _agora()})
    except Exception as e:  # noqa
        traceback.print_exc()
        _atualiza(rid, montagem={"status": "erro", "erro": str(e)[-300:]})


def _thumb(rid, r, pasta, idx_bg):
    canal = (r.get("pedido") or {}).get("canal") or ""
    img = thumbs.montar(os.path.join(pasta, f"bg_{idx_bg}.jpg"), r.get("thumb_texto") or "MELHORES MOMENTOS",
                        "amarelo", "lateral", "centro", canal)
    thumbs._salvar(img, os.path.join(pasta, "thumb.jpg"))
    return _url(rid, "thumb.jpg")


def refazer_thumb(rid):
    r = obter(rid)
    pasta = _pasta(rid)
    bgs = sorted(f for f in os.listdir(pasta) if f.startswith("bg_"))
    if not bgs:
        raise ValueError("Monte o vídeo primeiro.")
    idx = max(int(re.findall(r"\d+", f)[0]) for f in bgs)
    return _atualiza(rid, montagem={"thumb": _thumb(rid, r, pasta, idx)})


def descricao_completa(r):
    """Descrição + capítulos + créditos dos streamers + hashtags."""
    partes = [r.get("descricao") or ""]
    m = r.get("montagem") or {}
    if m.get("capitulos"):
        partes.append("Capítulos:\n" + m["capitulos"])
    creditos = []
    for s in r.get("secoes") or []:
        c = clipper.get_clip(s.get("cid")) if s.get("cid") else None
        if c:
            linha = titulos.credito(c)
            if linha not in creditos:
                creditos.append(linha)
    if creditos:
        partes.append("\n".join(creditos))
    tags = " ".join("#" + re.sub(r"\s+", "", t) for t in (r.get("tags") or [])[:5])
    if tags:
        partes.append(tags)
    return "\n\n".join(p for p in partes if p.strip())


# ------------------------------------------------------------------ envio ao YouTube
def enviar_youtube(rid, titulo, descricao, privacidade="public", publicar_em=None):
    with _lock:
        r = _achar(rid)
        m = r.get("montagem") or {}
        caminho = os.path.join(_pasta(rid), "compilado.mp4")
        if m.get("status") != "pronto" or not os.path.exists(caminho):
            raise ValueError("Monte o vídeo antes de enviar.")
        if (r.get("youtube") or {}).get("status") in ("na fila", "enviando"):
            raise ValueError("Esse vídeo já está sendo enviado.")
        if not os.path.exists(youtube.TOKEN):
            raise ValueError("Conecte o canal do YouTube primeiro.")
        titulo = youtube._limpa(titulo, 100)
        if not titulo:
            raise ValueError("O título está vazio.")
        privacidade, publicar_em = youtube.validar_publicacao(privacidade, publicar_em)
        r["youtube"] = {"status": "enviando", "progresso": 0, "titulo": titulo, "privacidade": privacidade,
                        "publicar_em": publicar_em, "erro": None, "aviso": None, "id": None}
        _save()
    threading.Thread(target=_enviar_agora, args=(rid, caminho, titulo, descricao, privacidade, publicar_em, list(r.get("tags") or [])),
                     daemon=True).start()
    return obter(rid)


def _enviar_agora(rid, caminho, titulo, descricao, privacidade, publicar_em, tags):
    try:
        resp = youtube.subir(caminho, youtube.snippet(titulo, descricao, tags),
                             youtube._status_video(privacidade, publicar_em),
                             lambda p: _atualiza(rid, youtube={"progresso": p}))
        vid = resp["id"]
        st = resp.get("status") or {}
        aviso = None
        if st.get("privacyStatus") == "private" and (privacidade != "private" or publicar_em) and not st.get("publishAt"):
            aviso = ("O YouTube deixou o vídeo privado (projeto do Google Cloud sem auditoria). "
                     "Mude a visibilidade pelo YouTube Studio.")
        _atualiza(rid, youtube={"status": "enviado", "progresso": 1, "id": vid, "url": f"https://youtu.be/{vid}",
                                "studio": f"https://studio.youtube.com/video/{vid}/edit", "aviso": aviso,
                                "publicar_em": st.get("publishAt") or (None if aviso else publicar_em)})
        thumb = os.path.join(_pasta(rid), "thumb.jpg")
        if config.get("thumb_automatica") and os.path.exists(thumb):
            try:
                youtube.por_thumb(vid, thumb)
                _atualiza(rid, youtube={"thumb_ok": True, "thumb_erro": None})
            except RuntimeError as e:
                _atualiza(rid, youtube={"thumb_ok": False, "thumb_erro": str(e)})
    except Exception as e:  # noqa
        traceback.print_exc()
        _atualiza(rid, youtube={"status": "erro", "erro": youtube.msg_erro(e)})


_carregar()
