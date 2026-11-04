"""
Bot de música privado para Discord.

Toca áudio em canais de voz a partir de:
  - links ou buscas do YouTube (vídeo, playlist ou texto livre);
  - links do Spotify (música, álbum ou playlist), resolvidos via busca no YouTube.

Só o dono do aplicativo (ou os IDs listados em OWNER_ID / ALLOWED_USER_IDS) pode usar os comandos.
"""
from __future__ import annotations

import asyncio
import json
import logging
import logging.handlers
import os
import random
import re
import shutil
import sys
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional

import aiohttp
import discord
import yt_dlp
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

# --------------------------------------------------------------------------- #
# Configuração
# --------------------------------------------------------------------------- #
TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID") or 0)
EXTRA_USERS = {
    int(x) for x in os.getenv("ALLOWED_USER_IDS", "").replace(";", ",").split(",") if x.strip().isdigit()
}
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID", "").strip()
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "").strip()
IDLE_TIMEOUT = int(os.getenv("IDLE_TIMEOUT") or 300)
DEFAULT_VOLUME = max(0, min(200, int(os.getenv("DEFAULT_VOLUME") or 50))) / 100
MAX_PLAYLIST = int(os.getenv("MAX_PLAYLIST") or 200)
FFMPEG_PATH = os.getenv("FFMPEG_PATH", "").strip() or shutil.which("ffmpeg") or "ffmpeg"
YTDL_COOKIES = os.getenv("YTDL_COOKIES", "").strip()

STREAM_TTL = 3600  # segundos que uma URL de stream já resolvida continua válida para reuso
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

log = logging.getLogger("musicbot")


def _js_runtimes() -> Optional[dict]:
    """yt-dlp precisa de um runtime JS para o YouTube. Deno é o padrão; se não houver, usa Node."""
    if shutil.which("deno"):
        return None
    if shutil.which("node"):
        return {"node": {}}
    return None


YTDL_BASE: dict = {
    "quiet": True,
    "no_warnings": True,
    "noprogress": True,
    "default_search": "ytsearch",
    "skip_download": True,
    "nocheckcertificate": True,
    "geo_bypass": True,
    "logger": logging.getLogger("yt_dlp"),
}
if js := _js_runtimes():
    YTDL_BASE["js_runtimes"] = js
if YTDL_COOKIES:
    YTDL_BASE["cookiefile"] = YTDL_COOKIES

# Extração rasa: só lista títulos/links (rápido, bom para playlists).
YTDL_FLAT = {**YTDL_BASE, "extract_flat": "in_playlist", "ignoreerrors": True, "playlistend": MAX_PLAYLIST}
# Extração completa de UM item: traz a lista de formatos; o formato de áudio é escolhido e testado por nós.
YTDL_STREAM = {**YTDL_BASE, "format": "bestaudio/best", "noplaylist": True, "ignore_no_formats_error": True}

# O YouTube muda as regras com frequência (PO Token, clientes bloqueados...). Se a estratégia
# padrão não entregar nenhuma URL que abra, o bot tenta as seguintes e passa a preferir a que funcionou.
STRATEGIES: list[tuple[str, dict]] = [
    # visionos primeiro: hoje é o cliente cujas URLs abrem sem PO Token; "default" mantém os
    # clientes padrão do yt-dlp como reserva dentro da mesma extração.
    ("visionos + padrão", {"extractor_args": {"youtube": {"player_client": ["visionos", "default"]}}}),
    ("padrão", {}),
    ("cliente android", {"extractor_args": {"youtube": {"player_client": ["android"]}}}),
    ("sem runtime JS", {"js_runtimes": {}}),
]
_preferred_strategy = 0
_bad_clients: dict[str, int] = {}  # cliente do YouTube (parâmetro c= da URL) -> vezes que devolveu 403
MAX_FORMAT_TRIES = 5  # quantos formatos testar por extração antes de trocar de estratégia

FFMPEG_BEFORE = "-nostdin -loglevel error -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
FFMPEG_OPTS = "-vn"

URL_RE = re.compile(r"^https?://", re.I)
SPOTIFY_RE = re.compile(
    r"(?:open\.spotify\.com/(?:intl-[\w-]+/)?|spotify:)(track|album|playlist)[/:]([A-Za-z0-9]+)"
)


class MusicError(Exception):
    """Erro esperado, mostrado ao usuário sem stack trace."""


# --------------------------------------------------------------------------- #
# Modelo
# --------------------------------------------------------------------------- #
@dataclass
class Track:
    title: str
    query: str  # URL do YouTube ou "ytsearch1:<texto>"
    requester: str
    duration: Optional[int] = None
    webpage_url: Optional[str] = None
    thumbnail: Optional[str] = None
    source: str = "youtube"  # youtube | spotify
    matched_title: Optional[str] = None  # título do vídeo escolhido (para faixas do Spotify)
    stream_url: Optional[str] = None
    stream_headers: Optional[dict] = None  # cabeçalhos (User-Agent) que o YouTube espera nessa URL
    resolved_at: float = 0.0

    @property
    def is_fresh(self) -> bool:
        return bool(self.stream_url) and (time.time() - self.resolved_at) < STREAM_TTL

    def link(self) -> str:
        safe = discord.utils.escape_markdown(self.title)
        return f"[{safe}]({self.webpage_url})" if self.webpage_url else f"**{safe}**"


def fmt_time(seconds: Optional[float]) -> str:
    if seconds is None:
        return "ao vivo"
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def progress_bar(elapsed: float, total: Optional[int], width: int = 18) -> str:
    if not total:
        return f"`{fmt_time(elapsed)}`"
    pos = int(width * min(elapsed, total) / total)
    return "▬" * pos + "🔘" + "▬" * (width - pos) + f"  `{fmt_time(elapsed)} / {fmt_time(total)}`"


# --------------------------------------------------------------------------- #
# YouTube (yt-dlp) — funções bloqueantes, sempre chamadas via asyncio.to_thread
# --------------------------------------------------------------------------- #
def _track_from_entry(e: dict, requester: str) -> Track:
    url = e.get("webpage_url") or e.get("original_url") or e.get("url")
    if url and not URL_RE.match(url) and e.get("id"):
        url = f"https://www.youtube.com/watch?v={e['id']}"
    thumb = e.get("thumbnail")
    if not thumb and e.get("thumbnails"):
        thumb = e["thumbnails"][-1].get("url")
    return Track(
        title=e.get("title") or "Sem título",
        query=url or f"ytsearch1:{e.get('title', '')}",
        requester=requester,
        duration=int(e["duration"]) if e.get("duration") else None,
        webpage_url=url,
        thumbnail=thumb,
    )


def resolve_query(raw: str, requester: str) -> tuple[list[Track], Optional[str]]:
    """Transforma um link/busca do YouTube em faixas. Retorna (faixas, nome_da_playlist)."""
    raw = raw.strip()
    query = raw if URL_RE.match(raw) else f"ytsearch1:{raw}"
    try:
        with yt_dlp.YoutubeDL(YTDL_FLAT) as ydl:
            info = ydl.extract_info(query, download=False)
    except yt_dlp.utils.DownloadError as e:
        raise MusicError(_clean_ytdl_error(e)) from e
    if not info:
        return [], None
    if "entries" in info:
        entries = [e for e in info["entries"] if e]
        if not entries:
            return [], None
        if not URL_RE.match(raw):  # busca por texto: só o primeiro resultado
            return [_track_from_entry(entries[0], requester)], None
        return [_track_from_entry(e, requester) for e in entries], info.get("title")
    return [_track_from_entry(info, requester)], None


def _yt_client(f: dict) -> str:
    """Cliente do YouTube que gerou a URL (parâmetro c=), ex.: VISIONOS, TVHTML5, WEB."""
    m = re.search(r"[?&]c=([A-Z0-9_]+)", f.get("url") or "")
    return m.group(1) if m else "?"


def _format_score(f: dict) -> tuple:
    """Ordem de preferência: clientes sem falhas, só-áudio antes de vídeo+áudio, http antes de HLS, maior bitrate."""
    audio_only = f.get("vcodec") in (None, "none")
    proto = f.get("protocol") or "https"
    rate = f.get("abr") or f.get("tbr") or 0
    return (_bad_clients.get(_yt_client(f), 0), 0 if audio_only else 1, 0 if proto.startswith("http") else 1, -rate)


def _audio_candidates(info: dict) -> list[dict]:
    out = []
    for f in info.get("formats") or []:
        if not f.get("url") or f.get("acodec") in (None, "none"):
            continue
        if (f.get("protocol") or "https") not in ("https", "http", "m3u8_native", "m3u8"):
            continue
        out.append(f)
    out.sort(key=_format_score)
    return out


def _url_works(ydl: yt_dlp.YoutubeDL, f: dict) -> bool:
    """Faz uma leitura mínima da URL pela mesma sessão do yt-dlp; o YouTube devolve 403 nas que não valem."""
    headers = dict(f.get("http_headers") or {})
    headers["Range"] = "bytes=0-1023"
    client = _yt_client(f)
    try:
        with ydl.urlopen(yt_dlp.networking.Request(f["url"], headers=headers)) as resp:
            resp.read(1)
        _bad_clients.pop(client, None)
        return True
    except Exception as e:  # noqa: BLE001
        _bad_clients[client] = _bad_clients.get(client, 0) + 1
        log.info("Formato %s (%s) recusado: %s", f.get("format_id"), client, str(e)[:100])
        return False


def _extract_playable(opts: dict, query: str) -> tuple[dict, Optional[dict]]:
    """Extrai o vídeo e devolve (info, formato_que_abre). formato é None se nenhum passou no teste."""
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(query, download=False)
        if info and "entries" in info:
            entries = [e for e in info["entries"] if e]
            if not entries:
                raise MusicError("nenhum resultado no YouTube.")
            info = entries[0]
        if not info:
            raise MusicError("não consegui obter informações desse vídeo.")
        for f in _audio_candidates(info)[:MAX_FORMAT_TRIES]:
            if _url_works(ydl, f):
                return info, f
    return info, None


def resolve_stream(track: Track) -> None:
    """Obtém uma URL de áudio que realmente abre para a faixa (e preenche metadados que faltavam)."""
    global _preferred_strategy
    order = [_preferred_strategy] + [i for i in range(len(STRATEGIES)) if i != _preferred_strategy]
    last_error: Optional[MusicError] = None
    for i in order:
        name, extra = STRATEGIES[i]
        try:
            info, fmt = _extract_playable({**YTDL_STREAM, **extra}, track.query)
        except yt_dlp.utils.DownloadError as e:
            last_error = MusicError(_clean_ytdl_error(e))
            log.info("Estratégia '%s' falhou para %s: %s", name, track.title, last_error)
            continue
        except MusicError as e:
            last_error = e
            continue
        if fmt is None:
            last_error = MusicError("o YouTube recusou todas as URLs de áudio desse vídeo.")
            log.info("Estratégia '%s': nenhum formato abriu para %s", name, track.title)
            continue
        if i != _preferred_strategy:
            log.warning("Passando a usar a estratégia '%s' do yt-dlp.", name)
            _preferred_strategy = i

        track.stream_url = fmt["url"]
        track.stream_headers = fmt.get("http_headers") or None
        track.resolved_at = time.time()
        track.webpage_url = info.get("webpage_url") or track.webpage_url
        track.thumbnail = info.get("thumbnail") or track.thumbnail
        if info.get("duration"):
            track.duration = int(info["duration"])
        if track.source == "spotify":
            track.matched_title = info.get("title")
        else:
            track.title = info.get("title") or track.title
        log.info("Áudio pronto: %s [fmt %s, %s]", track.title, fmt.get("format_id"), name)
        return
    raise last_error or MusicError("não consegui obter o áudio desse vídeo.")


def ffmpeg_before_options(track: Track) -> str:
    opts = FFMPEG_BEFORE
    ua = (track.stream_headers or {}).get("User-Agent")
    if ua:
        opts += ' -user_agent "' + ua.replace('"', "") + '"'
    return opts


def _clean_ytdl_error(e: Exception) -> str:
    msg = str(e)
    msg = re.sub(r"^ERROR:\s*", "", msg)
    msg = re.sub(r"^\[[^\]]+\]\s*[\w-]*:?\s*", "", msg)
    if "Sign in to confirm" in msg or "not a bot" in msg:
        msg += " (o YouTube pediu login: atualize o yt-dlp com update.bat ou configure YTDL_COOKIES no .env)"
    return msg[:300]


# --------------------------------------------------------------------------- #
# Spotify → faixas (resolvidas no YouTube na hora de tocar)
# --------------------------------------------------------------------------- #
def _spotify_track(name: str, artists: str, duration_ms: Optional[int], requester: str) -> Track:
    label = f"{artists} - {name}" if artists else name
    return Track(
        title=label,
        query=f"ytsearch1:{label}",
        requester=requester,
        duration=int(duration_ms // 1000) if duration_ms else None,
        source="spotify",
    )


class SpotifyResolver:
    def __init__(self) -> None:
        self.sp = None
        if SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET:
            import spotipy
            from spotipy.oauth2 import SpotifyClientCredentials

            self.sp = spotipy.Spotify(
                auth_manager=SpotifyClientCredentials(
                    client_id=SPOTIFY_CLIENT_ID, client_secret=SPOTIFY_CLIENT_SECRET
                ),
                requests_timeout=15,
            )
            log.info("Spotify: usando a API oficial (credenciais encontradas).")
        else:
            log.info("Spotify: sem credenciais, usando a página pública (playlists limitadas às primeiras faixas).")

    async def resolve(self, kind: str, sid: str, requester: str) -> tuple[list[Track], Optional[str]]:
        if self.sp:
            return await asyncio.to_thread(self._via_api, kind, sid, requester)
        return await self._via_embed(kind, sid, requester)

    # -- API oficial (spotipy) ------------------------------------------------
    def _via_api(self, kind: str, sid: str, requester: str) -> tuple[list[Track], Optional[str]]:
        import spotipy

        sp = self.sp
        items: list[dict] = []
        name: Optional[str] = None
        try:
            if kind == "track":
                items = [sp.track(sid)]
            elif kind == "album":
                album = sp.album(sid)
                name = album["name"]
                page = album["tracks"]
                while page and len(items) < MAX_PLAYLIST:
                    items += page["items"]
                    page = sp.next(page) if page.get("next") else None
            else:
                name = sp.playlist(sid, fields="name")["name"]
                page = sp.playlist_items(sid, additional_types=("track",))
                while page and len(items) < MAX_PLAYLIST:
                    items += [i["track"] for i in page["items"] if i and i.get("track")]
                    page = sp.next(page) if page.get("next") else None
        except spotipy.SpotifyException as e:
            raise MusicError(f"Spotify: {e.msg or e}".strip()) from e

        tracks = []
        for t in items[:MAX_PLAYLIST]:
            if not t or not t.get("name"):
                continue
            artists = ", ".join(a["name"] for a in t.get("artists", []) if a.get("name"))
            tracks.append(_spotify_track(t["name"], artists, t.get("duration_ms"), requester))
        return tracks, name

    # -- Página pública de embed (sem credenciais) ----------------------------
    async def _via_embed(self, kind: str, sid: str, requester: str) -> tuple[list[Track], Optional[str]]:
        url = f"https://open.spotify.com/embed/{kind}/{sid}"
        try:
            async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                    if resp.status != 200:
                        raise MusicError(f"o Spotify respondeu {resp.status} para esse link.")
                    text = await resp.text()
        except aiohttp.ClientError as e:
            raise MusicError(f"não consegui acessar o Spotify ({e}).") from e

        m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', text, re.S)
        if not m:
            raise MusicError("não consegui ler a página do Spotify (link privado ou formato mudou).")
        try:
            data = json.loads(m.group(1))
            entity = data["props"]["pageProps"]["state"]["data"]["entity"] or {}
        except (KeyError, TypeError, ValueError):
            raise MusicError("não consegui ler a página do Spotify (formato mudou).")

        name = entity.get("name") or entity.get("title")
        track_list = entity.get("trackList") or []
        if kind == "track" or not track_list:
            if not name:
                raise MusicError("não achei essa música no Spotify.")
            artists = ", ".join(a.get("name", "") for a in entity.get("artists") or [] if a.get("name"))
            artists = artists or (entity.get("subtitle") or "")
            return [_spotify_track(name, artists, entity.get("duration"), requester)], None

        tracks = [
            _spotify_track(t["title"], t.get("subtitle") or "", t.get("duration"), requester)
            for t in track_list[:MAX_PLAYLIST]
            if t.get("title")
        ]
        return tracks, name


# --------------------------------------------------------------------------- #
# Player por servidor
# --------------------------------------------------------------------------- #
class GuildPlayer:
    def __init__(self, bot: "MusicBot", guild: discord.Guild) -> None:
        self.bot = bot
        self.guild = guild
        self.queue: deque[Track] = deque()
        self.current: Optional[Track] = None
        self.volume = DEFAULT_VOLUME
        self.loop_mode = "off"  # off | track | queue
        self.channel: Optional[discord.abc.Messageable] = None
        self.started_at = 0.0
        self.paused_at: Optional[float] = None
        self.paused_total = 0.0
        self._on_done = "normal"  # normal | skip | stop
        self._closing = False  # True enquanto o próprio player está se desconectando
        self._wake = asyncio.Event()
        self._song_done = asyncio.Event()
        self._prefetch: Optional[asyncio.Task] = None
        self._prefetch_track: Optional[Track] = None
        self.task = asyncio.create_task(self._run(), name=f"player-{guild.id}")

    # -- utilidades -----------------------------------------------------------
    @property
    def vc(self) -> Optional[discord.VoiceClient]:
        vc = self.guild.voice_client
        return vc if isinstance(vc, discord.VoiceClient) else None

    @property
    def elapsed(self) -> float:
        if not self.current:
            return 0.0
        paused = self.paused_total + ((time.time() - self.paused_at) if self.paused_at else 0.0)
        return max(0.0, time.time() - self.started_at - paused)

    def add(self, tracks: list[Track]) -> None:
        self.queue.extend(tracks)
        self._wake.set()

    async def say(self, content: Optional[str] = None, *, embed: Optional[discord.Embed] = None) -> None:
        if not self.channel:
            return
        try:
            await self.channel.send(content=content, embed=embed)
        except discord.HTTPException as e:
            log.warning("Não consegui enviar mensagem: %s", e)

    def pause(self) -> None:
        vc = self.vc
        if vc and vc.is_playing():
            vc.pause()
            self.paused_at = time.time()

    def resume(self) -> None:
        vc = self.vc
        if vc and vc.is_paused():
            vc.resume()
            if self.paused_at:
                self.paused_total += time.time() - self.paused_at
                self.paused_at = None

    def skip(self) -> None:
        self._on_done = "skip"
        if self.vc:
            self.vc.stop()

    def stop(self) -> None:
        self.queue.clear()
        self._on_done = "stop"
        if self.vc:
            self.vc.stop()

    def set_volume(self, value: float) -> None:
        self.volume = value
        vc = self.vc
        if vc and isinstance(vc.source, discord.PCMVolumeTransformer):
            vc.source.volume = value

    async def disconnect(self, reason: Optional[str] = None) -> None:
        self._closing = True
        self.queue.clear()
        self._on_done = "stop"
        vc = self.vc
        if vc:
            vc.stop()
            try:
                await vc.disconnect(force=True)
            except Exception as e:  # noqa: BLE001
                log.warning("Erro ao desconectar: %s", e)
        self.current = None
        if reason:
            await self.say(f"👋 Saí do canal de voz ({reason}).")
        self.cleanup()

    def cleanup(self) -> None:
        """Encerra o player (sem desconectar): usado quando o bot já saiu/foi tirado do canal."""
        self.bot.players.pop(self.guild.id, None)
        vc = self.vc
        if vc and not self._closing and (vc.is_playing() or vc.is_paused()):
            vc.stop()
        if self._prefetch and not self._prefetch.done():
            self._prefetch.cancel()
        if self.task is not asyncio.current_task() and not self.task.done():
            self.task.cancel()

    # -- pré-carregamento da próxima faixa ------------------------------------
    def _start_prefetch(self) -> None:
        if self._prefetch and not self._prefetch.done():
            return
        nxt = self.queue[0] if self.queue else None
        if nxt and not nxt.is_fresh:
            self._prefetch_track = nxt
            self._prefetch = asyncio.create_task(self._do_prefetch(nxt))

    async def _do_prefetch(self, track: Track) -> None:
        try:
            await asyncio.to_thread(resolve_stream, track)
        except Exception as e:  # noqa: BLE001 — na hora de tocar tenta de novo e avisa
            log.info("Pré-carregamento falhou para %s: %s", track.title, e)

    # -- laço principal -------------------------------------------------------
    async def _wait_next(self) -> Optional[Track]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + IDLE_TIMEOUT
        while not self.queue:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                return None
        return self.queue.popleft()

    async def _run(self) -> None:
        try:
            while True:
                track = await self._wait_next()
                if track is None:
                    await self.disconnect("inatividade")
                    return
                vc = self.vc
                if vc is None or not vc.is_connected():
                    log.info("Sem conexão de voz em %s; encerrando player.", self.guild.name)
                    self.cleanup()
                    return

                # Garante a URL do áudio (reusa o pré-carregamento quando possível).
                try:
                    if self._prefetch and self._prefetch_track is track and not self._prefetch.done():
                        await self._prefetch
                    if not track.is_fresh:
                        await asyncio.to_thread(resolve_stream, track)
                except Exception as e:  # noqa: BLE001
                    log.warning("Falha ao resolver %s: %s", track.title, e)
                    await self.say(f"❌ Não consegui tocar **{discord.utils.escape_markdown(track.title)}**: {e}")
                    continue

                source = discord.PCMVolumeTransformer(
                    discord.FFmpegPCMAudio(
                        track.stream_url,
                        executable=FFMPEG_PATH,
                        before_options=ffmpeg_before_options(track),
                        options=FFMPEG_OPTS,
                    ),
                    volume=self.volume,
                )
                loop = asyncio.get_running_loop()
                self._song_done.clear()

                def _after(err: Optional[Exception], _loop=loop) -> None:
                    if err:
                        log.error("Erro durante a reprodução: %s", err)
                    _loop.call_soon_threadsafe(self._song_done.set)

                try:
                    vc.play(source, after=_after)
                except discord.ClientException as e:
                    await self.say(f"❌ Não consegui iniciar a reprodução: {e}")
                    continue

                self.current = track
                self.started_at = time.time()
                self.paused_at = None
                self.paused_total = 0.0
                self._on_done = "normal"
                await self.say(embed=now_playing_embed(self, header="▶️ Tocando agora"))
                self._start_prefetch()

                await self._song_done.wait()
                finished, self.current = self.current, None
                if finished is None:
                    continue
                if self._on_done == "stop":
                    continue
                if self.loop_mode == "track" and self._on_done == "normal":
                    finished.stream_url = None  # força nova resolução (URLs expiram)
                    self.queue.appendleft(finished)
                elif self.loop_mode == "queue":
                    finished.stream_url = None
                    self.queue.append(finished)
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            log.exception("O player de %s travou.", self.guild.name)
            await self.say("❌ O player deu erro e foi reiniciado. Use /play de novo.")
            self.bot.players.pop(self.guild.id, None)


def now_playing_embed(player: GuildPlayer, header: str = "🎵 Tocando agora") -> discord.Embed:
    t = player.current
    assert t is not None
    emb = discord.Embed(title=header, description=t.link(), color=discord.Color.blurple())
    if t.source == "spotify" and t.matched_title:
        emb.add_field(
            name="Vídeo encontrado", value=discord.utils.escape_markdown(t.matched_title)[:1024], inline=False
        )
    emb.add_field(name="Progresso", value=progress_bar(player.elapsed, t.duration), inline=False)
    emb.add_field(name="Pedida por", value=t.requester, inline=True)
    emb.add_field(name="Volume", value=f"{int(player.volume * 100)}%", inline=True)
    if player.loop_mode != "off":
        emb.add_field(name="Repetição", value="música atual" if player.loop_mode == "track" else "fila", inline=True)
    if player.queue:
        emb.set_footer(text=f"Próxima: {player.queue[0].title[:80]}  •  {len(player.queue)} na fila")
    if t.thumbnail:
        emb.set_thumbnail(url=t.thumbnail)
    return emb


# --------------------------------------------------------------------------- #
# Bot e comandos
# --------------------------------------------------------------------------- #
class OwnerOnlyTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        bot: MusicBot = interaction.client  # type: ignore[assignment]
        if interaction.user.id in bot.allowed_ids:
            return True
        await interaction.response.send_message("🔒 Este bot é privado.", ephemeral=True)
        return False

    async def on_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        original = getattr(error, "original", error)
        if isinstance(original, MusicError):
            msg = f"⚠️ {original}"
        elif isinstance(error, app_commands.CheckFailure):
            return
        else:
            cmd = interaction.command.name if interaction.command else "?"
            log.exception("Erro no comando /%s", cmd, exc_info=original)
            msg = f"❌ Deu erro: `{type(original).__name__}: {str(original)[:200]}`"
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass


class MusicBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()  # guilds + voice_states já vêm habilitados
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            tree_cls=OwnerOnlyTree,
            help_command=None,
        )
        self.players: dict[int, GuildPlayer] = {}
        self.spotify = SpotifyResolver()
        self.allowed_ids: set[int] = set(EXTRA_USERS)
        self._synced: set[int] = set()

    async def setup_hook(self) -> None:
        app = await self.application_info()
        owners = {OWNER_ID} if OWNER_ID else set()
        if app.team:
            owners |= {m.id for m in app.team.members}
        else:
            owners.add(app.owner.id)
        self.allowed_ids |= owners
        log.info("Usuários autorizados: %s", ", ".join(map(str, sorted(self.allowed_ids))))
        await self.add_cog(Music(self))

    async def on_ready(self) -> None:
        for guild in self.guilds:
            await self.sync_guild(guild)
        await self.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name="/play"))
        log.info("Online como %s em %d servidor(es).", self.user, len(self.guilds))

    async def on_guild_join(self, guild: discord.Guild) -> None:
        await self.sync_guild(guild)

    async def sync_guild(self, guild: discord.Guild) -> None:
        if guild.id in self._synced:
            return
        try:
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            self._synced.add(guild.id)
            log.info("Comandos sincronizados em %s.", guild.name)
        except discord.HTTPException as e:
            log.warning("Não consegui sincronizar comandos em %s: %s", guild.name, e)

    def player(self, guild: discord.Guild) -> GuildPlayer:
        p = self.players.get(guild.id)
        if p is None or p.task.done():
            p = GuildPlayer(self, guild)
            self.players[guild.id] = p
        return p

    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ) -> None:
        player = self.players.get(member.guild.id)
        if player is None:
            return
        if member.id == self.user.id:  # type: ignore[union-attr]
            if after.channel is None and not player._closing:  # bot foi tirado do canal por fora
                player.cleanup()
            return
        vc = player.vc
        if vc and before.channel == vc.channel and after.channel != vc.channel:
            asyncio.create_task(self._alone_check(player))

    async def _alone_check(self, player: GuildPlayer) -> None:
        await asyncio.sleep(IDLE_TIMEOUT)
        vc = player.vc
        if vc and vc.is_connected() and all(m.bot for m in vc.channel.members):
            await player.disconnect("fiquei sozinho no canal")


class Music(commands.Cog):
    def __init__(self, bot: MusicBot) -> None:
        self.bot = bot

    # -- helpers --------------------------------------------------------------
    async def ensure_voice(self, inter: discord.Interaction) -> discord.VoiceClient:
        member = inter.user
        channel = member.voice.channel if isinstance(member, discord.Member) and member.voice else None
        if channel is None:
            raise MusicError("Entre em um canal de voz primeiro.")
        vc = inter.guild.voice_client  # type: ignore[union-attr]
        if vc is None or not vc.is_connected():
            perms = channel.permissions_for(inter.guild.me)  # type: ignore[union-attr]
            if not (perms.connect and perms.speak):
                raise MusicError(f"Não tenho permissão de entrar e falar em **{channel.name}**.")
            try:
                vc = await channel.connect(self_deaf=True, timeout=20)
            except asyncio.TimeoutError:
                raise MusicError("O Discord demorou demais para conectar a voz. Tente de novo.")
            except discord.ClientException as e:
                raise MusicError(f"Não consegui conectar: {e}")
        elif vc.channel != channel:
            await vc.move_to(channel)
        return vc  # type: ignore[return-value]

    def require_player(self, inter: discord.Interaction) -> GuildPlayer:
        player = self.bot.players.get(inter.guild_id)  # type: ignore[arg-type]
        if player is None or player.vc is None or not player.vc.is_connected():
            raise MusicError("Não estou em um canal de voz. Use /play.")
        return player

    # -- comandos -------------------------------------------------------------
    @app_commands.command(name="play", description="Toca uma música ou playlist (link do YouTube/Spotify ou nome da música)")
    @app_commands.describe(busca="Link do YouTube, link do Spotify ou o nome da música")
    @app_commands.guild_only()
    async def play(self, inter: discord.Interaction, busca: str) -> None:
        await inter.response.defer()
        await self.ensure_voice(inter)
        player = self.bot.player(inter.guild)  # type: ignore[arg-type]
        player.channel = inter.channel  # type: ignore[assignment]
        requester = inter.user.display_name

        m = SPOTIFY_RE.search(busca)
        if m:
            tracks, collection = await self.bot.spotify.resolve(m.group(1), m.group(2), requester)
        else:
            tracks, collection = await asyncio.to_thread(resolve_query, busca, requester)
        if not tracks:
            raise MusicError("Não achei nada com isso.")

        was_idle = player.current is None and not player.queue
        player.add(tracks)

        if len(tracks) == 1:
            t = tracks[0]
            if was_idle:
                desc = f"🔎 Carregando {t.link()}…"
            else:
                desc = f"✅ {t.link()} adicionada à fila (posição {len(player.queue)})."
            emb = discord.Embed(description=desc, color=discord.Color.green())
            if t.duration:
                emb.set_footer(text=f"Duração: {fmt_time(t.duration)}")
        else:
            total = sum(t.duration or 0 for t in tracks)
            origem = "Spotify" if tracks[0].source == "spotify" else "YouTube"
            emb = discord.Embed(
                title="📃 Playlist adicionada",
                description=f"**{discord.utils.escape_markdown(collection or 'Sem nome')}** ({origem})\n"
                f"{len(tracks)} faixas • {fmt_time(total) if total else 'duração desconhecida'}",
                color=discord.Color.green(),
            )
            if len(tracks) >= MAX_PLAYLIST:
                emb.set_footer(text=f"Limite de {MAX_PLAYLIST} faixas por playlist (MAX_PLAYLIST no .env).")
        await inter.followup.send(embed=emb)

    @app_commands.command(name="skip", description="Pula a música atual")
    @app_commands.guild_only()
    async def skip(self, inter: discord.Interaction) -> None:
        player = self.require_player(inter)
        if not player.current:
            raise MusicError("Não tem nada tocando.")
        title = player.current.title
        player.skip()
        await inter.response.send_message(f"⏭️ Pulei **{discord.utils.escape_markdown(title)}**.")

    @app_commands.command(name="pause", description="Pausa a música")
    @app_commands.guild_only()
    async def pause(self, inter: discord.Interaction) -> None:
        player = self.require_player(inter)
        if not player.vc or not player.vc.is_playing():
            raise MusicError("Não tem nada tocando (ou já está pausado).")
        player.pause()
        await inter.response.send_message("⏸️ Pausado. Use /resume para continuar.")

    @app_commands.command(name="resume", description="Continua a música pausada")
    @app_commands.guild_only()
    async def resume(self, inter: discord.Interaction) -> None:
        player = self.require_player(inter)
        if not player.vc or not player.vc.is_paused():
            raise MusicError("Não está pausado.")
        player.resume()
        await inter.response.send_message("▶️ Continuando.")

    @app_commands.command(name="stop", description="Para a música e limpa a fila (continua no canal)")
    @app_commands.guild_only()
    async def stop(self, inter: discord.Interaction) -> None:
        player = self.require_player(inter)
        player.stop()
        await inter.response.send_message("⏹️ Parei e limpei a fila.")

    @app_commands.command(name="leave", description="Sai do canal de voz")
    @app_commands.guild_only()
    async def leave(self, inter: discord.Interaction) -> None:
        player = self.bot.players.get(inter.guild_id)  # type: ignore[arg-type]
        vc = inter.guild.voice_client  # type: ignore[union-attr]
        if player:
            await player.disconnect()
        elif vc:
            await vc.disconnect(force=True)
        else:
            raise MusicError("Não estou em um canal de voz.")
        await inter.response.send_message("👋 Saí do canal.")

    @app_commands.command(name="np", description="Mostra o que está tocando agora")
    @app_commands.guild_only()
    async def now_playing(self, inter: discord.Interaction) -> None:
        player = self.require_player(inter)
        if not player.current:
            raise MusicError("Não tem nada tocando.")
        header = "⏸️ Pausado" if player.paused_at else "🎵 Tocando agora"
        await inter.response.send_message(embed=now_playing_embed(player, header=header))

    @app_commands.command(name="queue", description="Mostra a fila de músicas")
    @app_commands.guild_only()
    async def queue(self, inter: discord.Interaction) -> None:
        player = self.require_player(inter)
        if not player.current and not player.queue:
            raise MusicError("A fila está vazia.")
        lines = []
        if player.current:
            lines.append(f"**Agora:** {player.current.link()} `{fmt_time(player.current.duration)}`")
        shown = list(player.queue)[:15]
        for i, t in enumerate(shown, 1):
            lines.append(f"`{i}.` {t.link()} `{fmt_time(t.duration)}`")
        rest = len(player.queue) - len(shown)
        if rest > 0:
            lines.append(f"… e mais {rest}.")
        total = sum(t.duration or 0 for t in player.queue)
        emb = discord.Embed(title="📜 Fila", description="\n".join(lines)[:4000], color=discord.Color.blurple())
        footer = f"{len(player.queue)} na fila • {fmt_time(total)} no total"
        if player.loop_mode != "off":
            footer += f" • repetição: {'música' if player.loop_mode == 'track' else 'fila'}"
        emb.set_footer(text=footer)
        await inter.response.send_message(embed=emb)

    @app_commands.command(name="volume", description="Ajusta o volume (0 a 200)")
    @app_commands.describe(nivel="Volume em porcentagem (100 = normal)")
    @app_commands.guild_only()
    async def volume(self, inter: discord.Interaction, nivel: app_commands.Range[int, 0, 200]) -> None:
        player = self.bot.player(inter.guild)  # type: ignore[arg-type]
        player.set_volume(nivel / 100)
        await inter.response.send_message(f"🔊 Volume: **{nivel}%**")

    @app_commands.command(name="shuffle", description="Embaralha a fila")
    @app_commands.guild_only()
    async def shuffle(self, inter: discord.Interaction) -> None:
        player = self.require_player(inter)
        if len(player.queue) < 2:
            raise MusicError("Precisa de pelo menos 2 músicas na fila.")
        items = list(player.queue)
        random.shuffle(items)
        player.queue = deque(items)
        await inter.response.send_message(f"🔀 Embaralhei {len(items)} músicas.")

    @app_commands.command(name="remove", description="Remove uma música da fila pela posição")
    @app_commands.describe(posicao="Número da música na fila (veja /queue)")
    @app_commands.guild_only()
    async def remove(self, inter: discord.Interaction, posicao: app_commands.Range[int, 1, None]) -> None:
        player = self.require_player(inter)
        if posicao > len(player.queue):
            raise MusicError(f"A fila só tem {len(player.queue)} música(s).")
        items = list(player.queue)
        removed = items.pop(posicao - 1)
        player.queue = deque(items)
        await inter.response.send_message(f"🗑️ Removi **{discord.utils.escape_markdown(removed.title)}**.")

    @app_commands.command(name="clear", description="Limpa a fila (não para a música atual)")
    @app_commands.guild_only()
    async def clear(self, inter: discord.Interaction) -> None:
        player = self.require_player(inter)
        n = len(player.queue)
        player.queue.clear()
        await inter.response.send_message(f"🧹 Fila limpa ({n} removidas).")

    @app_commands.command(name="loop", description="Repetição: desligada, música atual ou fila inteira")
    @app_commands.describe(modo="O que repetir")
    @app_commands.choices(
        modo=[
            app_commands.Choice(name="desligar", value="off"),
            app_commands.Choice(name="música atual", value="track"),
            app_commands.Choice(name="fila", value="queue"),
        ]
    )
    @app_commands.guild_only()
    async def loop(self, inter: discord.Interaction, modo: app_commands.Choice[str]) -> None:
        player = self.bot.player(inter.guild)  # type: ignore[arg-type]
        player.loop_mode = modo.value
        texto = {"off": "🔁 Repetição desligada.", "track": "🔂 Repetindo a música atual.", "queue": "🔁 Repetindo a fila."}
        await inter.response.send_message(texto[modo.value])


# --------------------------------------------------------------------------- #
DAVE_ENABLED = os.getenv("DAVE", "1").strip().lower() not in ("0", "false", "off", "nao", "não")


def _apply_voice_tweaks(level: int) -> None:
    """Ajustes na camada de voz do discord.py.

    - DAVE=0 no .env desliga a criptografia ponta a ponta (E2EE) de voz. Atenção: em 2026-11 o
      Discord já recusa conexões sem E2EE (fecha com código 4017), então isso só serve para teste.
      Se o bot entra na call e ninguém ouve, o culpado costuma ser a versão da lib `davey`
      (veja o README e o pin `davey<0.2` no requirements.txt).
    - Em LOG_LEVEL=DEBUG, registra a cada 250 pacotes (5 s) quantos pacotes de voz foram enviados.
    """
    from discord import voice_state as _vs

    if not DAVE_ENABLED:
        _vs.VoiceConnectionState.max_dave_protocol_version = property(lambda self: 0)  # type: ignore[assignment]
        log.warning("DAVE (E2EE de voz) desligado por configuração (DAVE=0).")

    if level <= logging.DEBUG:
        original = discord.VoiceClient.send_audio_packet
        counter = {"n": 0, "t": time.monotonic()}

        def counted(self: discord.VoiceClient, data: bytes, *, encode: bool = True) -> None:
            original(self, data, encode=encode)
            counter["n"] += 1
            if counter["n"] % 250 == 0:
                now = time.monotonic()
                rate = 250 / max(now - counter["t"], 1e-6)
                counter["t"] = now
                conn = self._connection
                log.debug(
                    "voz: %d pacotes enviados (%.0f/s) | dave_version=%s can_encrypt=%s seq=%s",
                    counter["n"], rate, conn.dave_protocol_version, conn.can_encrypt, self.sequence,
                )

        discord.VoiceClient.send_audio_packet = counted  # type: ignore[assignment]


def main() -> None:
    level = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)
    discord.utils.setup_logging(level=level)
    _apply_voice_tweaks(level)
    # Também grava em bot.log (na pasta do bot), para dar para ler depois que a janela fechou.
    log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.log")
    file_handler = logging.handlers.RotatingFileHandler(log_path, maxBytes=2_000_000, backupCount=2, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)-8s] %(name)s: %(message)s"))
    logging.getLogger().addHandler(file_handler)
    logging.getLogger("yt_dlp").setLevel(logging.ERROR)
    if level > logging.DEBUG:
        logging.getLogger("discord.gateway").setLevel(logging.WARNING)

    if not TOKEN:
        print("Falta o DISCORD_TOKEN. Copie .env.example para .env e cole o token do bot.", file=sys.stderr)
        sys.exit(1)
    if not (shutil.which(FFMPEG_PATH) or os.path.isfile(FFMPEG_PATH)):
        print(
            f"FFmpeg não encontrado ({FFMPEG_PATH}). Instale com 'winget install Gyan.FFmpeg' "
            "ou aponte FFMPEG_PATH no .env.",
            file=sys.stderr,
        )
        sys.exit(1)
    runtime = "node" if YTDL_BASE.get("js_runtimes") else "deno/padrão"
    log.info("FFmpeg: %s | runtime JS do yt-dlp: %s", FFMPEG_PATH, runtime)
    MusicBot().run(TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
