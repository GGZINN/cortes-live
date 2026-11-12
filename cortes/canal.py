"""Painel do canal (fase 5): números do canal e dos vídeos, edição de vídeos publicados e aprendizado.

- YouTube Data API: canal (inscritos, views), lista de vídeos com views, likes e comentários, edição.
- YouTube Analytics API: últimos 28 dias, views por dia e, por vídeo, retenção e tempo médio assistido.
  (o Analytics chega com 2 a 3 dias de atraso; vídeos privados não têm dados).
- Aprendizado: com MIN_VIDEOS vídeos públicos com 2+ dias, compara os vídeos (título, horário, streamer) e gera
  lições que entram nas instruções da IA ao escrever títulos e ao escolher os cortes, e sugere horários para a agenda.

Tudo fica em dados/canal.json e é atualizado sozinho a cada 6 horas (ou pelo botão no painel).
Cota: atualizar gasta ~10 unidades da cota diária (10.000); editar um vídeo gasta 51.
"""
import json
import os
import re
import statistics
import threading
import time
import traceback
from datetime import date, datetime, timedelta, timezone
from typing import List

from pydantic import BaseModel

from . import clipper, config, roteiros, youtube

ARQ = os.path.join(config.DATA_DIR, "canal.json")
ATUALIZAR_A_CADA = 6 * 3600
MAX_VIDEOS = 200
MIN_VIDEOS = 5                  # vídeos públicos com 2+ dias para começar a aprender
IDADE_MIN = timedelta(days=2)
DIAS_PERIODO = 28
REAPRENDER_A_CADA = 24 * 3600

_lock = threading.RLock()
_dados = {}                     # {atualizado, canal, periodo, por_dia, videos, erro_analytics, aprendizado}
_estado = {"atualizando": False, "aprendendo": False, "erro": None}


VALIDADE = timedelta(days=30)  # política da API: dados guardados precisam ser atualizados ou apagados em 30 dias


def _carregar():
    global _dados
    try:
        with open(ARQ, encoding="utf-8") as f:
            _dados = json.load(f)
    except (OSError, ValueError):
        _dados = {}
    ultimo = _dt(_dados.get("atualizado"))
    if _dados and (not ultimo or datetime.now(timezone.utc) - ultimo > VALIDADE):
        esquecer()


def esquecer():
    """Apaga a cópia local dos dados do canal (ao desconectar, ou se ficou 30 dias sem atualizar)."""
    global _dados
    with _lock:
        _dados = {}
        try:
            os.remove(ARQ)
        except OSError:
            pass


def _save():
    config.salvar_json(ARQ, _dados, indent=1)


def _agora():
    return datetime.now(timezone.utc)


def _iso(d):
    return d.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _dt(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def _segundos(iso):
    """'PT1M5S' -> 65."""
    m = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", iso or "")
    if not m:
        return None
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def _int(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ busca no YouTube
def _video(v):
    sn, st, stats, cd = v["snippet"], v.get("status") or {}, v.get("statistics") or {}, v.get("contentDetails") or {}
    fotos = sn.get("thumbnails") or {}
    agendado = st.get("publishAt") if st.get("privacyStatus") == "private" else None
    return {
        "id": v["id"], "titulo": sn.get("title", ""), "descricao": sn.get("description", ""),
        "tags": sn.get("tags") or [], "categoria": sn.get("categoryId"), "publicado": sn.get("publishedAt"),
        "privacidade": st.get("privacyStatus"), "agendado_para": agendado, "upload": st.get("uploadStatus"),
        "duracao": _segundos(cd.get("duration")),
        "thumb": (fotos.get("medium") or fotos.get("default") or {}).get("url"),
        "views": _int(stats.get("viewCount")), "likes": _int(stats.get("likeCount")),
        "comentarios": _int(stats.get("commentCount")),
        "url": f"https://youtu.be/{v['id']}", "studio": f"https://studio.youtube.com/video/{v['id']}/edit",
    }


def _ids_dos_videos(yt, uploads):
    from googleapiclient.errors import HttpError
    ids, token = [], None
    while len(ids) < MAX_VIDEOS:
        try:
            r = yt.playlistItems().list(part="contentDetails", playlistId=uploads, maxResults=50,
                                        pageToken=token).execute()
        except HttpError as e:
            if e.resp.status == 404:  # canal sem nenhum vídeo ainda
                return ids
            raise
        ids += [i["contentDetails"]["videoId"] for i in r.get("items") or []]
        token = r.get("nextPageToken")
        if not token:
            break
    return ids[:MAX_VIDEOS]


def _relatorio(yta, **kw):
    r = yta.reports().query(ids="channel==MINE", **kw).execute()
    cols = [c["name"] for c in r.get("columnHeaders") or []]
    return [dict(zip(cols, linha)) for linha in r.get("rows") or []]


def _analytics(criado_em):
    """Últimos 28 dias, views por dia e métricas por vídeo (desde a criação do canal)."""
    yta = youtube._servico("youtubeAnalytics", "v2")
    fim = date.today()
    ini = fim - timedelta(days=DIAS_PERIODO - 1)
    tot = _relatorio(yta, startDate=ini.isoformat(), endDate=fim.isoformat(),
                     metrics="views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,"
                             "subscribersGained,subscribersLost,likes,comments,shares")
    t = tot[0] if tot else {}
    periodo = {
        "dias": DIAS_PERIODO, "inicio": ini.isoformat(), "fim": fim.isoformat(),
        "views": _int(t.get("views")) or 0, "minutos": _int(t.get("estimatedMinutesWatched")) or 0,
        "media_s": _int(t.get("averageViewDuration")), "retencao": t.get("averageViewPercentage"),
        "inscritos_ganhos": _int(t.get("subscribersGained")) or 0, "inscritos_perdidos": _int(t.get("subscribersLost")) or 0,
        "likes": _int(t.get("likes")) or 0, "comentarios": _int(t.get("comments")) or 0,
        "compartilhamentos": _int(t.get("shares")) or 0,
    }
    dias = {d["day"]: d for d in _relatorio(yta, startDate=ini.isoformat(), endDate=fim.isoformat(),
                                             metrics="views,subscribersGained,subscribersLost",
                                             dimensions="day", sort="day")}
    por_dia = []
    for i in range(DIAS_PERIODO):  # o Analytics pula dias sem dados: completa com zero
        d = (ini + timedelta(days=i)).isoformat()
        x = dias.get(d) or {}
        por_dia.append({"dia": d, "views": _int(x.get("views")) or 0,
                        "inscritos": (_int(x.get("subscribersGained")) or 0) - (_int(x.get("subscribersLost")) or 0)})
    ini_videos = (_dt(criado_em).date() if _dt(criado_em) else fim - timedelta(days=3650))
    por_video = {v["video"]: v for v in _relatorio(
        yta, startDate=ini_videos.isoformat(), endDate=fim.isoformat(), dimensions="video", sort="-views",
        maxResults=200, metrics="views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,subscribersGained")}
    return periodo, por_dia, por_video


def _ligacoes():
    """Liga cada vídeo do canal ao corte ou roteiro que o gerou aqui no app."""
    m = {}
    for c in clipper.list_clips():
        y = c.get("youtube") or {}
        if y.get("id"):
            m[y["id"]] = {"tipo": "corte", "cid": c["id"], "streamer": c.get("canal"), "motivo": c.get("motivo"),
                          "publicar_em": y.get("publicar_em")}
    for vid, r in roteiros.videos_no_youtube().items():
        m[vid] = {"tipo": "roteiro", "rid": r["rid"], "formato": r.get("tipo"), "publicar_em": r.get("publicar_em")}
    return m


def atualizar_agora():
    yt = youtube._servico()
    r = yt.channels().list(part="snippet,statistics,contentDetails", mine=True).execute()
    if not r.get("items"):
        raise RuntimeError("Essa conta Google não tem canal no YouTube.")
    c = r["items"][0]
    fotos = c["snippet"].get("thumbnails") or {}
    canal = {
        "id": c["id"], "titulo": c["snippet"]["title"], "criado": c["snippet"].get("publishedAt"),
        "foto": (fotos.get("default") or {}).get("url"), "url": f"https://www.youtube.com/channel/{c['id']}",
        "inscritos": _int(c["statistics"].get("subscriberCount")),
        "inscritos_ocultos": bool(c["statistics"].get("hiddenSubscriberCount")),
        "views": _int(c["statistics"].get("viewCount")), "videos": _int(c["statistics"].get("videoCount")),
    }
    ids = _ids_dos_videos(yt, c["contentDetails"]["relatedPlaylists"]["uploads"])
    videos = []
    for i in range(0, len(ids), 50):
        resp = yt.videos().list(part="snippet,statistics,status,contentDetails", id=",".join(ids[i:i + 50])).execute()
        videos += [_video(v) for v in resp.get("items") or []]
    periodo, por_dia, erro_an = None, [], None
    try:
        periodo, por_dia, por_video = _analytics(canal["criado"])
        for v in videos:
            a = por_video.get(v["id"])
            if a:
                v.update(retencao=a.get("averageViewPercentage"), media_s=_int(a.get("averageViewDuration")),
                         minutos=_int(a.get("estimatedMinutesWatched")), inscritos=_int(a.get("subscribersGained")))
    except Exception as e:  # noqa - sem o Analytics o painel ainda mostra o resto
        traceback.print_exc()
        erro_an = youtube.msg_erro(e)
        if type(e).__name__ == "HttpError" and youtube._motivo(e)[0] in ("accessNotConfigured", "SERVICE_DISABLED"):
            erro_an = ("A YouTube Analytics API não está ativada no seu projeto do Google Cloud. Ative em "
                       "console.cloud.google.com/apis/library/youtubeanalytics.googleapis.com e clique em Atualizar.")
    lig = _ligacoes()
    for v in videos:
        v["origem"] = lig.get(v["id"])
    with _lock:
        _dados.update(atualizado=_iso(_agora()), canal=canal, periodo=periodo, por_dia=por_dia,
                      videos=videos, erro_analytics=erro_an)
        _save()
    try:
        _aprender(forcar=False)
    except Exception:  # noqa
        traceback.print_exc()


def _rodar(chave, fn):
    if _estado[chave]:
        return
    _estado[chave] = True
    _estado["erro"] = None

    def run():
        try:
            fn()
        except Exception as e:  # noqa
            traceback.print_exc()
            _estado["erro"] = youtube.msg_erro(e)
        finally:
            _estado[chave] = False
    threading.Thread(target=run, daemon=True).start()


def atualizar():
    if not os.path.exists(youtube.TOKEN):
        raise ValueError("Conecte o canal do YouTube primeiro (botão ▶ no topo).")
    _rodar("atualizando", atualizar_agora)
    return estado()


def estado():
    with _lock:
        d = json.loads(json.dumps(_dados))
    for v in d.get("videos") or []:
        v.pop("descricao", None)  # a lista não precisa; o editor busca o vídeo inteiro
    return d | {"atualizando": _estado["atualizando"], "aprendendo": _estado["aprendendo"], "erro": _estado["erro"],
                "conectado": os.path.exists(youtube.TOKEN), "aprendizado_ativo": config.get("aprendizado_ativo"),
                "aprendizado_base": config.get("aprendizado_base"), "min_videos": MIN_VIDEOS}


def video(vid):
    with _lock:
        for v in _dados.get("videos") or []:
            if v["id"] == vid:
                return dict(v)
    raise ValueError("Vídeo não encontrado. Clique em Atualizar.")


# ------------------------------------------------------------------ editar vídeo publicado
def editar(vid, titulo, descricao, tags, privacidade):
    """Muda título, descrição, tags e visibilidade de um vídeo do canal. Mantém o resto como está."""
    titulo = youtube._limpa(titulo, 100)
    if not titulo:
        raise ValueError("O título está vazio.")
    if privacidade not in youtube.PRIVACIDADES:
        raise ValueError("Visibilidade inválida.")
    yt = youtube._servico()
    itens = yt.videos().list(part="snippet,status", id=vid).execute().get("items") or []
    if not itens:
        raise ValueError("O vídeo não foi encontrado no canal (foi apagado?).")
    atual = itens[0]
    sn = atual["snippet"]
    # no update, campo que não vai no pedido é apagado: copia os que podem ser escritos
    novo_sn = {k: sn[k] for k in ("defaultLanguage", "defaultAudioLanguage") if sn.get(k)}
    novo_sn.update(title=titulo, description=youtube._limpa(descricao, 5000), tags=youtube._tags(tags),
                   categoryId=sn.get("categoryId") or config.get("youtube_categoria") or "20")
    body, partes = {"id": vid, "snippet": novo_sn}, ["snippet"]
    st = atual.get("status") or {}
    aviso = None
    if privacidade != st.get("privacyStatus"):
        novo_st = {k: st[k] for k in ("embeddable", "license", "publicStatsViewable", "selfDeclaredMadeForKids") if k in st}
        novo_st["privacyStatus"] = privacidade  # sem publishAt: mudar a visibilidade tira o agendamento
        body["status"], partes = novo_st, ["snippet", "status"]
        if st.get("publishAt"):
            aviso = "O agendamento foi removido porque a visibilidade mudou."
    resp = yt.videos().update(part=",".join(partes), body=body).execute()
    real = (resp.get("status") or {}).get("privacyStatus") or st.get("privacyStatus")
    if "status" in body and real != privacidade:
        aviso = ("O YouTube manteve o vídeo como privado. Isso acontece enquanto o projeto do Google Cloud não passa "
                 "na auditoria da API. Mude a visibilidade pelo YouTube Studio.")
    with _lock:
        for v in _dados.get("videos") or []:
            if v["id"] == vid:
                v.update(titulo=novo_sn["title"], descricao=novo_sn["description"], tags=novo_sn["tags"],
                         privacidade=real, agendado_para=None if "status" in body else v.get("agendado_para"))
        _save()
    for c in clipper.list_clips():  # mantém o corte de origem com o título novo
        y = c.get("youtube") or {}
        if y.get("id") == vid:
            clipper.update_clip(c["id"], youtube=y | {"titulo": novo_sn["title"], "privacidade_real": real})
    return {"video": video(vid), "aviso": aviso}


# ------------------------------------------------------------------ aprendizado
FORMATOS = [  # (nome, como aparece na lição, teste)
    ("aspas", "uma frase entre aspas", lambda t: bool(re.search(r'["“”]', t))),
    ("emoji", "emoji", lambda t: any(ord(ch) >= 0x1F300 or 0x2600 <= ord(ch) <= 0x27BF for ch in t)),
    ("caixa_alta", "palavra em CAIXA ALTA", lambda t: bool(re.search(r"\b[A-ZÁÉÍÓÚÂÊÔÃÕÇ]{3,}\b", t))),
    ("pergunta", "pergunta", lambda t: "?" in t),
    ("curto", "até 40 caracteres", lambda t: len(t) <= 40),
]


def _n(x):
    x = round(x)
    if x >= 1_000_000:
        return f"{x / 1_000_000:.1f} mi".replace(".", ",")
    if x >= 1000:
        return f"{x / 1000:.1f} mil".replace(".", ",")
    return str(x)


def _hora_local(v):
    q = _dt((v.get("origem") or {}).get("publicar_em") or v.get("publicado"))
    return q.astimezone().hour if q else None


def _padroes(eleg):
    """Lições tiradas só dos números (sem IA): formato de título, horário e streamer."""
    out = []
    for _, nome, teste in FORMATOS:
        com = [v["views"] for v in eleg if teste(v["titulo"])]
        sem = [v["views"] for v in eleg if not teste(v["titulo"])]
        if len(com) >= 2 and len(sem) >= 2:
            mc, ms = statistics.median(com), statistics.median(sem)
            if mc >= 1.3 * max(ms, 1):
                out.append(f"Títulos com {nome} tiveram mediana de {_n(mc)} views, contra {_n(ms)} sem.")
            elif ms >= 1.3 * max(mc, 1):
                out.append(f"Títulos sem {nome} foram melhor: mediana de {_n(ms)} views, contra {_n(mc)} com.")
    por_streamer = {}
    for v in eleg:
        s = (v.get("origem") or {}).get("streamer")
        if s:
            por_streamer.setdefault(s, []).append(v["views"])
    ranking = sorted(((statistics.median(l), s, len(l)) for s, l in por_streamer.items() if len(l) >= 2), reverse=True)
    if len(ranking) >= 2 and ranking[0][0] >= 1.3 * max(ranking[-1][0], 1):
        out.append(f"Cortes de {ranking[0][1]} foram os que mais deram views (mediana de {_n(ranking[0][0])}, "
                   f"{ranking[0][2]} vídeos); os de {ranking[-1][1]} os que menos ({_n(ranking[-1][0])}).")
    return out


def _horarios(eleg):
    por_hora = {}
    for v in eleg:
        h = _hora_local(v)
        if h is not None:
            por_hora.setdefault(h, []).append(v["views"])
    lista = sorted(({"hora": f"{h:02d}:00", "videos": len(l), "mediana": statistics.median(l)}
                    for h, l in por_hora.items()), key=lambda x: -x["mediana"])
    # só sugere quando dá para comparar: 4+ horários com 2+ vídeos cada, e só os que ficaram acima da mediana geral
    validos = [x for x in lista if x["videos"] >= 2]
    geral = statistics.median(v["views"] for v in eleg)
    bons = [x for x in validos if x["mediana"] > geral][:3]
    sugestao = sorted(x["hora"] for x in bons) if len(validos) >= 4 and len(bons) >= 2 else []
    return lista, sugestao


class Licoes(BaseModel):
    licoes: List[str]
    evitar: List[str]
    resumo: str


SISTEMA = """Você analisa o desempenho dos vídeos de um canal brasileiro do YouTube (principalmente Shorts de cortes de lives).
Recebe os vídeos públicos com views, likes, retenção média, duração, dia/hora de publicação e o streamer de origem,
além de padrões já calculados a partir dos números.

Escreva o que o canal deve repetir nos próximos títulos e na escolha dos cortes:
- "licoes": 3 a 6 regras curtas, práticas e específicas DESTE canal (tipo de título, tema, streamer, formato, duração),
  sempre apoiadas nos números recebidos. Não invente padrões que os dados não mostram.
- "evitar": 1 a 3 coisas que performaram mal (lista vazia se não houver nada claro).
- "resumo": 1 frase sobre o momento do canal.
Sobre horários: só tire conclusão de horários com 2 ou mais vídeos; um vídeo só num horário não prova nada.
Se houver poucos vídeos ou nenhuma diferença clara, diga isso e seja cauteloso. Português do Brasil."""


def _linha(v):
    q = _dt((v.get("origem") or {}).get("publicar_em") or v.get("publicado"))
    quando = q.astimezone().strftime("%a %H:%M") if q else "-"
    ret = f"{v['retencao']:.0f}%" if v.get("retencao") is not None else "-"
    o = v.get("origem") or {}
    return (f"- {v['views']} views | {v.get('likes') or 0} likes | retenção {ret} | {v.get('duracao') or '-'} s | "
            f"{quando} | {o.get('streamer') or o.get('formato') or 'outro'} | \"{v['titulo']}\"")


def _licoes_ia(eleg, padroes, horarios):
    from google import genai
    from google.genai import types
    from .titulos import MODELOS_GEMINI, _chave_gemini, _schema_sem_refs, gemini_gerar
    if not _chave_gemini():
        return None
    ordem = sorted(eleg, key=lambda v: -v["views"])[:60]
    texto = (f"{len(eleg)} vídeos públicos com 2+ dias. Mediana de views: {statistics.median(v['views'] for v in eleg)}.\n\n"
             "Vídeos (do mais visto ao menos visto):\n" + "\n".join(_linha(v) for v in ordem) +
             "\n\nPadrões calculados:\n" + ("\n".join(f"- {p}" for p in padroes) or "- nenhum padrão claro") +
             "\n\nViews por horário de publicação (mediana):\n" +
             ("\n".join(f"- {h['hora']}: {_n(h['mediana'])} ({h['videos']} vídeos)" for h in horarios) or "- sem dados"))
    cfg = types.GenerateContentConfig(system_instruction=SISTEMA, response_mime_type="application/json",
                                      response_json_schema=_schema_sem_refs(Licoes), temperature=0.4)
    r = gemini_gerar(genai.Client(api_key=_chave_gemini()), MODELOS_GEMINI, texto, cfg)
    return Licoes.model_validate_json(r.text)


def _aprender(forcar=True):
    with _lock:
        videos = [dict(v) for v in _dados.get("videos") or []]
        antes = _dados.get("aprendizado") or {}
    limite = _agora() - IDADE_MIN
    fonte = config.get("aprendizado_base")
    eleg = [v for v in videos if v.get("privacidade") == "public" and v.get("views") is not None
            and _dt(v.get("publicado")) and _dt(v["publicado"]) <= limite
            and (fonte == "canal" or v.get("origem"))]  # padrão: só o que saiu do Cortes Live
    base = {"n": len(eleg), "min": MIN_VIDEOS, "base": fonte, "gerado_em": _iso(_agora())}
    if len(eleg) < MIN_VIDEOS:
        novo = base | {"status": "poucos_dados"}
    else:
        assinatura = [fonte] + sorted([v["id"], v["views"] // 50] for v in eleg)  # muda com vídeo novo ou views
        idade = _agora() - (_dt(antes.get("gerado_em")) or datetime.min.replace(tzinfo=timezone.utc))
        if not forcar and antes.get("assinatura") == assinatura and idade.total_seconds() < REAPRENDER_A_CADA:
            return
        ordem = sorted(eleg, key=lambda v: -v["views"])
        padroes = _padroes(eleg)
        horarios, sugestao = _horarios(eleg)
        novo = base | {
            "status": "ok", "assinatura": assinatura, "mediana": statistics.median(v["views"] for v in eleg),
            "top": [{"id": v["id"], "titulo": v["titulo"], "views": v["views"], "retencao": v.get("retencao")} for v in ordem[:5]],
            "fundo": [{"id": v["id"], "titulo": v["titulo"], "views": v["views"], "retencao": v.get("retencao")} for v in ordem[-3:]],
            "padroes": padroes, "horarios": horarios, "sugestao_horarios": sugestao,
            "licoes": [], "evitar": [], "resumo": None, "fonte": "dados", "aviso": None,
        }
        try:
            ia = _licoes_ia(eleg, padroes, horarios)
            if ia:
                novo.update(licoes=[x.strip() for x in ia.licoes if x.strip()][:6],
                            evitar=[x.strip() for x in ia.evitar if x.strip()][:3],
                            resumo=ia.resumo.strip() or None, fonte="ia")
        except Exception as e:  # noqa - fica só com os padrões dos números
            traceback.print_exc()
            novo["aviso"] = f"A IA não respondeu ({type(e).__name__}); ficaram só os padrões dos números."
    with _lock:
        _dados["aprendizado"] = novo
        _save()


def aprender():
    if not _dados.get("videos"):
        raise ValueError("Atualize os dados do canal primeiro.")
    _rodar("aprendendo", lambda: _aprender(forcar=True))
    return estado()


def contexto_ia():
    """Trecho para as instruções da IA (títulos e escolha dos cortes). Vazio se não há o que ensinar."""
    if not config.get("aprendizado_ativo"):
        return ""
    with _lock:
        a = dict(_dados.get("aprendizado") or {})
    if a.get("status") != "ok":
        return ""
    regras = a.get("licoes") or a.get("padroes") or []
    de_onde = "todos os vídeos do canal" if a.get("base") == "canal" else "vídeos postados por este app"
    linhas = [f"O que já funcionou no canal do YouTube onde isso vai ser postado (dados reais de {a['n']} {de_onde}):"]
    linhas += [f"- {x}" for x in regras[:6]]
    if a.get("evitar"):
        linhas.append("Evitar: " + "; ".join(a["evitar"][:3]))
    if a.get("top"):
        linhas.append("Títulos que mais deram views: " + " | ".join(f'"{v["titulo"]}" ({v["views"]} views)' for v in a["top"][:5]))
    linhas.append("Use como referência, sem copiar os títulos e sem deixar de ser fiel ao que acontece no vídeo.")
    return "\n".join(linhas)


def sugestao_horarios():
    with _lock:
        a = _dados.get("aprendizado") or {}
        return {"horarios": a.get("sugestao_horarios") or [], "detalhe": a.get("horarios") or []} if a.get("status") == "ok" else None


# ------------------------------------------------------------------ atualização automática
def _loop():
    time.sleep(30)
    while True:
        try:
            ultimo = _dt(_dados.get("atualizado"))
            vencido = not ultimo or (_agora() - ultimo).total_seconds() > ATUALIZAR_A_CADA
            if vencido and os.path.exists(youtube.TOKEN) and not _estado["atualizando"]:
                _estado["atualizando"] = True
                try:
                    atualizar_agora()
                    _estado["erro"] = None
                except Exception as e:  # noqa
                    traceback.print_exc()
                    _estado["erro"] = youtube.msg_erro(e)
                finally:
                    _estado["atualizando"] = False
        except Exception:  # noqa
            traceback.print_exc()
        time.sleep(15 * 60)


_carregar()
threading.Thread(target=_loop, daemon=True).start()
