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
    """'Gaules TV' -> 'gaulestv' (hashtag válida, sem acento)."""
    t = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-zA-Z0-9_]", "", t).lower()


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


EXTRA = {
    "tiktok": ["fyp", "viral", "foryou"],
    "shorts": ["shorts"],
    "reels": ["reels", "viral"],
    "kwai": ["kwai", "viral"],
}


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

    base = hashtags_base(clip)
    gancho = {
        "tiktok": "Assiste até o final 👀",
        "shorts": "Se inscreve pra mais cortes!",
        "reels": "Marca aquele amigo que faria isso 😂",
        "kwai": "Segue pra mais cortes todo dia!",
    }
    plats = {}
    for p in PLATAFORMAS:
        plats[p] = {
            "titulo": titulos[0][:100],
            "legenda": gancho[p],
            "hashtags": base + EXTRA[p],
        }
    return {"titulos": titulos, "plataformas": plats, "fonte": "modelo"}


# ------------------------------------------------------------------ com IA (Claude)
class SugestaoPlataforma(BaseModel):
    titulo: str
    legenda: str
    hashtags: List[str]


class Sugestoes(BaseModel):
    titulos: List[str]
    tiktok: SugestaoPlataforma
    shorts: SugestaoPlataforma
    reels: SugestaoPlataforma
    kwai: SugestaoPlataforma


SISTEMA = """Você é editor de um canal brasileiro de cortes de lives (estilo TikTok/Shorts/Reels/Kwai).
Recebe a transcrição de um corte e dados do chat e escreve títulos e legendas que fazem a pessoa parar de rolar o feed.

Regras:
- Português do Brasil, linguagem de internet, natural. Pode usar CAIXA ALTA em 1 ou 2 palavras e no máximo 1 emoji por título.
- Seja fiel ao que acontece no corte: nada de prometer algo que não está no vídeo.
- Títulos curtos: até 60 caracteres. Uma frase marcante dita no corte, entre aspas, costuma funcionar muito bem.
- "titulos": 5 opções bem diferentes entre si (frase do streamer, reação do chat, curiosidade, exagero engraçado, descritivo).
- Para cada plataforma: "titulo" (no Shorts até 100 caracteres), "legenda" com 1 ou 2 linhas curtas que complementam o título
  (sem repetir o título, sem créditos e sem hashtags, que são adicionados depois) e "hashtags" com 3 a 6 itens, sem o símbolo #,
  minúsculas, sem acento. Inclua o nome do canal e o assunto/jogo se der para identificar.
- TikTok: tom mais jovem e direto. Shorts: título que funcione em busca. Reels: tom de conversa, convida a marcar alguém. Kwai: bem popular e direto."""


def _contexto(clip):
    return (
        f"Canal: {clip.get('canal')} ({clip.get('plataforma')})\n"
        f"Título da live: {clip.get('titulo_live') or '-'}\n"
        f"Por que foi cortado: {clip.get('motivo')}\n"
        f"Palavras mais repetidas no chat nesse momento: {', '.join(clip.get('palavras_chat') or []) or '-'}\n"
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
    erro = None
    for modelo in MODELOS_GEMINI:  # se o limite grátis de um estourar, tenta o próximo
        try:
            resp = cliente.models.generate_content(model=modelo, contents=_contexto(clip), config=cfg)
            if resp.text:
                return Sugestoes.model_validate_json(resp.text)
        except Exception as e:  # noqa
            erro = e
    if erro:
        raise erro
    return None


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
    base = hashtags_base(clip)
    plats = {}
    for p in PLATAFORMAS:
        sp = getattr(s, p)
        tags = []
        for t in list(sp.hashtags) + base + EXTRA[p][:1]:
            t = _tag(t.lstrip("#"))
            if t and t not in tags:
                tags.append(t)
        plats[p] = {"titulo": sp.titulo[:100], "legenda": sp.legenda.strip(), "hashtags": tags[:8]}
    titulos = [t.strip() for t in s.titulos if t.strip()][:6]
    if not titulos:
        return None
    return {"titulos": titulos, "plataformas": plats, "fonte": "ia", "provedor": provedor}


def gerar(clip):
    """Retorna {titulos, plataformas:{tiktok|shorts|reels|kwai: {titulo, legenda, hashtags}}, fonte, aviso?}."""
    aviso = None
    try:
        r = _por_ia(clip)
        if r:
            r["credito"] = credito(clip)
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
    if aviso:
        r["aviso"] = aviso
    return r
