"""Sugestões de título, legenda e hashtags para cada plataforma.

Sem configuração nenhuma, usa modelos prontos montados com a fala do streamer e o chat.
Com uma chave do Gemini (grátis) ou da Claude em Configurações, pede à IA títulos sob medida.
"""
import os
import re
import unicodedata
from typing import List

from pydantic import BaseModel

from . import config

PLATAFORMAS = ("tiktok", "shorts", "reels", "kwai")
MODELOS_GEMINI = ("gemini-3.6-flash", "gemini-3.5-flash-lite")  # ambos no plano gratuito
MODELO_CLAUDE = "claude-opus-5-5"


# ------------------------------------------------------------------ utilidades
def _tag(texto):
    """'Gaules TV' -> 'gaulestv' (só letras/números, sem espaço; para nomes de canal)."""
    t = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-zA-Z0-9_]", "", t).lower()


def _hashtag(texto):
    """'Mario Party Jamboree' -> 'mariopartyjamboree'; mantém acento ('engraçado'), tira espaço e símbolos."""
    t = unicodedata.normalize("NFC", str(texto or "")).lstrip("#")
    return re.sub(r"[^\w]", "", t, flags=re.UNICODE).replace("_", "").lower()


# hashtags genéricas demais: não ajudam ninguém a achar o vídeo e ocupam o lugar das que importam
GENERICAS = {"viral", "fyp", "foryou", "foryoupage", "fy", "fypシ", "trending", "explore", "explorar", "parati",
             "cortes", "cortesdelive", "corteslive", "cortesdelives", "video", "videos", "shortsvideo", "viralvideo",
             "reels", "reelsinstagram", "kwai", "tiktok", "youtube", "youtubeshorts", "streamer", "live", "lives"}
MAX_HASHTAGS = 5  # o YouTube mostra até 3 em cima do título; mais que isso dilui (e passar de 60 anula todas)


def hashtags_finais(clip, plataforma, sugeridas=(), jogo=None):
    """3 a 5 hashtags, da mais importante para a menos: streamer, jogo, cortes do streamer, tema."""
    canal = _hashtag(clip.get("canal"))
    jogo = _hashtag(jogo or clip.get("jogo"))
    ordem = [canal, jogo, f"cortesdo{canal}" if canal else ""]
    ordem += [_hashtag(t) for t in sugeridas or ()]
    out = []
    for t in ordem:
        if t and t not in GENERICAS and t != "shorts" and t not in out and 2 < len(t) <= 40:
            out.append(t)
    out = out[:MAX_HASHTAGS - (1 if plataforma == "shorts" else 0)]
    if plataforma == "shorts":
        out.append("shorts")  # por último: não toma o lugar das que aparecem em cima do título
    return out


def _url_completa(clip):
    u = _url_canal(clip)
    return ("https://" + u) if u and not u.startswith("http") else u


def _limpa(frase):
    frase = re.sub(r"\s+", " ", frase or "").strip(" ,.;:-")
    return frase[:1].upper() + frase[1:] if frase else ""


def _url_canal(clip):
    if clip.get("url_live"):
        return clip["url_live"].replace("https://", "").replace("www.", "").rstrip("/")
    canal = _tag(clip.get("canal"))
    if canal and clip.get("plataforma") == "twitch":
        return f"twitch.tv/{canal}"
    if canal and clip.get("plataforma") == "kick":
        return f"kick.com/{canal}"
    return ""


def credito(clip):
    canal = clip.get("canal") or "o streamer"
    url = _url_canal(clip)
    return f"🎥 Créditos: {canal}" + (f" ({url})" if url else "")


def hashtags_base(clip):
    tags = ["cortes", "cortesdelive"]
    c = _tag(clip.get("canal"))
    if c:
        tags.append(c)
    if clip.get("plataforma") in ("twitch", "kick", "youtube"):
        tags.append(clip["plataforma"])
    return tags


def descricao_youtube(clip, legenda, hashtags, jogo=None):
    """Descrição do Short: 1ª linha com palavras de busca, jogo, live com link clicável, convite e hashtags no fim."""
    canal = clip.get("canal") or "o streamer"
    jogo = jogo or clip.get("jogo")
    url = _url_completa(clip)
    linhas = [legenda.strip()] if legenda and legenda.strip() else []
    bloco = []
    if jogo:
        bloco.append(f"🎮 Jogo: {jogo}")
    bloco.append(f"🎥 Live de {canal}" + (f": {url}" if url else ""))
    bloco.append(f"🔔 Se inscreve para ver os melhores cortes do {canal} todo dia!")
    linhas.append("\n".join(bloco))
    if hashtags:
        linhas.append(" ".join("#" + h for h in hashtags))
    return "\n\n".join(linhas)


def tags_youtube(clip, palavras=(), jogo=None):
    """Tags (campo escondido do YouTube): termos que alguém digitaria na busca."""
    canal = clip.get("canal") or ""
    jogo = jogo or clip.get("jogo") or ""
    base = [canal, f"cortes do {canal}", f"{canal} cortes", f"melhores momentos {canal}"]
    if jogo:
        base += [jogo, f"{canal} {jogo}", f"{jogo} engraçado"]
    out, vistos = [], set()
    for t in base + list(palavras or ()):
        t = re.sub(r"\s+", " ", str(t or "").replace("#", "")).strip()
        if t and t.lower() not in vistos:
            vistos.add(t.lower())
            out.append(t)
    return out[:20]


def pacote_youtube(clip, sug=None):
    """O que vai para o YouTube: {titulo, descricao, tags, hashtags}. Mesma conta no painel e no envio."""
    s = sug or clip.get("sugestoes") or {}
    p = (s.get("plataformas") or {}).get("shorts") or {}
    jogo = s.get("jogo") or clip.get("jogo")
    base = p.get("titulo") or clip.get("titulo") or (s.get("titulos") or [None])[0] or clip.get("motivo") or "Corte"
    hashtags = hashtags_finais(clip, "shorts", p.get("hashtags") or [], jogo)
    return {
        "titulo": titulo_shorts(clip, base, jogo),
        "descricao": descricao_youtube(clip, p.get("legenda") or "", hashtags, jogo),
        "tags": tags_youtube(clip, (s.get("palavras_chave") or []) + hashtags[:-1], jogo),
        "hashtags": hashtags,
    }


def titulo_shorts(clip, titulo, jogo=None):
    """Título do Short: sem hashtag, até 100 caracteres, e com o nome do streamer ou do jogo (é o que se busca)."""
    t = re.sub(r"\s*#\w+", "", titulo or "").strip()
    canal = clip.get("canal") or ""
    jogo = jogo or clip.get("jogo") or ""
    tem_nome = any(n and n.lower() in t.lower() for n in (canal, jogo, _tag(canal)))
    if canal and not tem_nome and len(t) + len(canal) + 3 <= 70:
        t = f"{t} | {canal}"
    return t[:100]


# ------------------------------------------------------------------ sem IA (modelos prontos)
def _frase_forte(clip):
    frase = clip.get("frase_pico") or clip.get("transcricao") or ""
    palavras = frase.split()
    if len(palavras) > 9:
        frase = " ".join(palavras[:9]) + "…"
    return _limpa(frase)


def _por_modelo(clip):
    canal = clip.get("canal") or "o streamer"
    motivo = (clip.get("motivo") or "").lower()
    frase = _frase_forte(clip)
    titulos = []
    if clip.get("titulo_ia"):  # título de quem assistiu o corte (IA de cortes)
        titulos.append(clip["titulo_ia"])
    if frase and len(frase) > 8:
        titulos.append(f"\"{frase}\" 💀")
    if "kkkk" in motivo or "reagindo" in motivo:
        titulos += [f"{canal.upper()} NÃO AGUENTOU 😂", "O chat chorou de rir com isso 😂",
                    f"Ninguém segurou o riso na live do {canal}"]
    elif "chat" in motivo:
        titulos += ["O CHAT EXPLODIU nesse momento 🤯", f"Ninguém esperava isso na live do {canal}",
                    f"O momento que parou a live do {canal}"]
    else:  # áudio
        titulos += [f"{canal.upper()} PERDEU A CABEÇA 😱", f"O grito do {canal} nessa live 🔊",
                    f"{canal} não acreditou no que aconteceu"]
    titulos += [f"Isso aconteceu AO VIVO na live do {canal}", "O momento mais insano da live de hoje 🔥"]
    vistos, unicos = set(), []
    for t in titulos:
        if t.lower() not in vistos:
            vistos.add(t.lower())
            unicos.append(t)
    titulos = unicos[:5]

    jogo = clip.get("jogo")
    gancho = {
        "tiktok": "Assiste até o final 👀",
        "shorts": f"{canal} {'jogando ' + jogo if jogo else 'ao vivo'}: olha o que aconteceu nessa live!",
        "reels": "Marca aquele amigo que faria isso 😂",
        "kwai": "Segue pra mais cortes todo dia!",
    }
    plats = {}
    for p in PLATAFORMAS:
        plats[p] = {
            "titulo": titulo_shorts(clip, titulos[0], jogo) if p == "shorts" else titulos[0][:100],
            "legenda": gancho[p],
            "hashtags": hashtags_finais(clip, p, [], jogo),
        }
    return {"titulos": titulos, "plataformas": plats, "fonte": "modelo", "jogo": jogo,
            "palavras_chave": tags_youtube(clip, [], jogo)}


# ------------------------------------------------------------------ com IA (Claude)
class SugestaoPlataforma(BaseModel):
    titulo: str
    legenda: str
    hashtags: List[str]


class Sugestoes(BaseModel):
    titulos: List[str]
    jogo: str                 # nome do jogo do corte ("" se não for jogo)
    palavras_chave: List[str]  # termos de busca para as tags do YouTube
    tiktok: SugestaoPlataforma
    shorts: SugestaoPlataforma
    reels: SugestaoPlataforma
    kwai: SugestaoPlataforma


SISTEMA = """Você é editor de um canal brasileiro de cortes de lives (estilo TikTok/Shorts/Reels/Kwai).
Recebe a transcrição de um corte e o que a IA viu nele e escreve títulos, legendas e hashtags que fazem a pessoa
parar de rolar o feed E que ajudam o vídeo a ser achado na busca.

Regras gerais:
- Português do Brasil, linguagem de internet, natural. Pode usar CAIXA ALTA em 1 ou 2 palavras e no máximo 1 emoji por título.
- Escreva as palavras inteiras e com acento: nada de abreviação (mto, vc, pq, tbm, q) nem erro de digitação.
- Seja fiel ao que acontece no corte: nada de prometer algo que não está no vídeo (título enganoso derruba o vídeo).
- "titulos": 5 opções bem diferentes entre si (frase marcante do streamer entre aspas, curiosidade, exagero engraçado,
  descritivo com o nome do streamer, reação). Até 60 caracteres cada. Sem hashtag no título.
- "jogo": nome oficial do jogo do corte, como se busca no YouTube (ex.: "Mario Party Jamboree", "Counter-Strike 2");
  vazio se o corte não for de jogo ou se não der para saber. Use o que o contexto diz; não invente.
- "palavras_chave": 6 a 10 termos que alguém digitaria na busca para achar ESTE corte (ex.: "alanzoka mario party",
  "alanzoka rindo", "mario party jamboree engraçado"). Sem hashtag.

Para cada plataforma:
- "titulo": no Shorts, 40 a 70 caracteres, com o NOME DO STREAMER ou do JOGO escrito (é o que as pessoas buscam) e um gancho
  de curiosidade. Nas outras, o mesmo estilo dos "titulos".
- "legenda": 1 ou 2 linhas curtas que complementam o título sem repeti-lo. No Shorts, a primeira frase precisa ter o nome
  do streamer e o do jogo (se houver) e dizer o que acontece: ela é lida pela busca. Sem créditos, link ou hashtag
  (isso é adicionado depois).
- "hashtags": 3 a 5, da mais importante para a menos, sem o símbolo #, sem espaço (pode ter acento): o nome do streamer,
  o jogo, e o tema/situação do corte (ex.: "marioparty", "fail", "susto"). PROIBIDO hashtag genérica que não descreve o
  vídeo: viral, fyp, foryou, parati, trending, cortes, cortesdelive, reels, shorts, tiktok.
- Tom: TikTok mais jovem e direto; Shorts pensado para busca; Reels conversa e convida a marcar alguém; Kwai bem popular."""


def _aprendizado():
    """O que já funcionou no canal do YouTube (painel do canal). Import aqui dentro: canal → youtube → clipper → titulos."""
    try:
        from . import canal
        txt = canal.contexto_ia()
        return f"\n\n{txt}" if txt else ""
    except Exception:  # noqa
        return ""


def _contexto(clip):
    return _contexto_corte(clip) + _aprendizado()


def _contexto_corte(clip):
    return (
        f"Canal: {clip.get('canal')} ({clip.get('plataforma')})\n"
        f"Título da live: {clip.get('titulo_live') or '-'}\n"
        f"Por que foi cortado: {clip.get('motivo')}\n"
        + (f"Jogo da live: {clip.get('jogo')}\n" if clip.get("jogo") else "")
        + (f"O que a IA viu no corte: gancho = {clip.get('gancho')} | por que retém = {clip.get('por_que')} | "
           f"título sugerido por quem assistiu = {clip.get('titulo_ia')}\n" if clip.get("gancho") else "") +
        f"Frase dita no auge do momento: {clip.get('frase_pico') or '-'}\n"
        f"Duração: {clip.get('duracao')} s\n\n"
        f"Transcrição do corte:\n{clip.get('transcricao') or '(sem fala)'}"
    )


def _schema_sem_refs(model):
    """JSON Schema do Pydantic com os $ref expandidos (formato mais compatível)."""
    sch = model.model_json_schema()
    defs = sch.pop("$defs", {})

    def expand(n):
        if isinstance(n, dict):
            if "$ref" in n:
                return expand(defs[n["$ref"].split("/")[-1]])
            return {k: expand(v) for k, v in n.items() if k != "title"}
        if isinstance(n, list):
            return [expand(x) for x in n]
        return n
    return expand(sch)


# ---- Gemini (Google) - tem plano gratuito
def _chave_gemini():
    return config.get("chave_gemini").strip() or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")


def _gemini(clip):
    from google import genai
    from google.genai import types
    cliente = genai.Client(api_key=_chave_gemini())
    cfg = types.GenerateContentConfig(
        system_instruction=SISTEMA,
        response_mime_type="application/json",
        response_json_schema=_schema_sem_refs(Sugestoes),
        temperature=0.9,
    )
    resp = gemini_gerar(cliente, MODELOS_GEMINI, _contexto(clip), cfg)
    return Sugestoes.model_validate_json(resp.text) if resp and resp.text else None


GEMINI_TIMEOUT_MS = 180_000  # 3 min: a análise de 2 min de live leva ~1 min; mais que isso, travou

TRANSITORIOS = ("ReadError", "ReadTimeout", "ConnectError", "RemoteProtocolError", "WriteError", "TimeoutException",
                "500", "502", "503", "504", "UNAVAILABLE", "INTERNAL", "DEADLINE_EXCEEDED")


def gemini_gerar(cliente, modelos, contents, cfg, tentativas=3):
    """Chama o Gemini com tentativas: falha de rede passageira → tenta de novo;
    limite grátis/erro do modelo → passa para o próximo modelo da lista."""
    import time
    try:  # sem limite de tempo, uma chamada que não volta segura a fila (thumbs, títulos) para sempre
        from google.genai import types
        if getattr(cfg, "http_options", None) is None:
            cfg = cfg.model_copy(update={"http_options": types.HttpOptions(timeout=GEMINI_TIMEOUT_MS)})
    except Exception:  # noqa
        pass
    erro = None
    for modelo in modelos:
        for n in range(tentativas):
            try:
                return cliente.models.generate_content(model=modelo, contents=contents, config=cfg)
            except Exception as e:  # noqa
                erro = e
                txt = f"{type(e).__name__} {e}"
                if any(x in txt for x in TRANSITORIOS) and n + 1 < tentativas:
                    time.sleep(2 + 3 * n)
                    continue
                break  # não é passageiro: tenta o próximo modelo
    raise erro or RuntimeError("resposta vazia do Gemini")


# ---- Claude (Anthropic) - pago
def _chave_claude():
    return config.get("chave_claude").strip() or os.environ.get("ANTHROPIC_API_KEY")


def _claude(clip):
    import anthropic
    cliente = anthropic.Anthropic(api_key=_chave_claude())
    resp = cliente.beta.messages.parse(
        model=MODELO_CLAUDE,
        max_tokens=4000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"effort": "low"},
        system=SISTEMA,
        messages=[{"role": "user", "content": _contexto(clip)}],
        output_format=Sugestoes,
    )
    if resp.stop_reason != "end_turn":
        return None
    return resp.parsed_output


def _por_ia(clip):
    """Usa o Gemini se tiver chave; senão a Claude; senão None (modelos prontos)."""
    if _chave_gemini():
        s, provedor = _gemini(clip), "Gemini"
    elif _chave_claude():
        s, provedor = _claude(clip), "Claude"
    else:
        return None
    if s is None:
        return None
    jogo = (s.jogo or "").strip() or clip.get("jogo") or None
    plats = {}
    for p in PLATAFORMAS:
        sp = getattr(s, p)
        titulo = re.sub(r"\s*#\w+", "", sp.titulo or "").strip()
        plats[p] = {
            "titulo": titulo_shorts(clip, titulo, jogo) if p == "shorts" else titulo[:100],
            "legenda": sp.legenda.strip(),
            "hashtags": hashtags_finais(clip, p, sp.hashtags, jogo),
        }
    titulos = [re.sub(r"\s*#\w+", "", t).strip() for t in s.titulos if t.strip()][:6]
    if not titulos:
        return None
    return {"titulos": titulos, "plataformas": plats, "fonte": "ia", "provedor": provedor, "jogo": jogo,
            "palavras_chave": tags_youtube(clip, s.palavras_chave, jogo)}


def _jogo_provavel(clip):
    """Corte sem jogo anotado (ex.: anterior a esse campo): usa o jogo do corte mais próximo do mesmo streamer,
    até 2 h de diferença (mesma live). None se não houver."""
    from datetime import datetime
    try:
        from . import clipper
        quando = datetime.fromisoformat(clip.get("criado"))
    except Exception:  # noqa
        return None
    canal = (clip.get("canal") or "").lower()
    melhor, dist = None, 7200
    for c in clipper.list_clips():
        if c.get("id") == clip.get("id") or (c.get("canal") or "").lower() != canal:
            continue
        j = c.get("jogo") or (c.get("sugestoes") or {}).get("jogo")
        if not j:
            continue
        try:
            d = abs((datetime.fromisoformat(c.get("criado")) - quando).total_seconds())
        except Exception:  # noqa
            continue
        if d < dist:
            melhor, dist = j, d
    return melhor


def gerar(clip):
    """Retorna {titulos, plataformas:{tiktok|shorts|reels|kwai: {titulo, legenda, hashtags}}, fonte, aviso?}."""
    if not clip.get("jogo"):
        jp = _jogo_provavel(clip)
        if jp:  # entra no contexto da IA e nas hashtags/tags como o jogo do corte
            clip = {**clip, "jogo": jp}
    aviso = None
    try:
        r = _por_ia(clip)
        if r:
            r["credito"] = credito(clip)
            r["youtube"] = pacote_youtube(clip, r)
            return r
    except Exception as e:  # noqa - sem internet, chave inválida, limite grátis...
        txt = f"{type(e).__name__} {e}"
        if any(x in txt for x in ("SERVICE_DISABLED", "has not been used in project", "API_KEY_SERVICE_BLOCKED")):
            motivo = ("a chave existe, mas a API do Gemini está desligada ou bloqueada para ela. "
                      "Crie uma chave nova em aistudio.google.com/apikey")
        elif any(x in txt for x in ("API_KEY_INVALID", "API key not valid", "Authentication", "401", "PERMISSION_DENIED")):
            motivo = "chave inválida, confira em ⚙ Configurações"
        elif any(x in txt for x in ("429", "RESOURCE_EXHAUSTED", "quota", "RateLimit")):
            motivo = "limite gratuito atingido por agora, tente de novo em alguns minutos"
        elif any(x in txt for x in ("Connect", "Timeout", "getaddrinfo")):
            motivo = "sem conexão com a internet"
        else:
            motivo = type(e).__name__
        aviso = f"IA indisponível ({motivo}). Usando sugestões automáticas."
    r = _por_modelo(clip)
    r["credito"] = credito(clip)
    r["youtube"] = pacote_youtube(clip, r)
    if aviso:
        r["aviso"] = aviso
    return r
