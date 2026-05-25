#!/usr/bin/env python3
"""Downloader per AnimeUnity (animeunity.so).

Dato il link di un anime e un range di episodi, estrae gli ID degli episodi dalla pagina
anime, risolve l'embed Vixcloud per ogni episodio e scarica il file MP4 in alta qualità.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import html
import json
import logging
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.7444.265 Safari/537.36"
EPISODES_ATTR_RE = re.compile(r'episodes\s*=\s*"(\[.*?\])"', re.S)
EMBED_URL_RE = re.compile(r'embed_url\s*=\s*"(?P<url>[^"]+)"')
DOWNLOAD_URL_RE = re.compile(r'window\.downloadUrl\s*=\s*(?:"(?P<url>[^"]+)"|\'(?P<url2>[^\']+)\')')
PLAYLIST_URL_RE = re.compile(r'"(?P<url>https?://[^"\']+/playlist/[^"\']+)"')
RELEASE_TAG_RE = re.compile(
    r'\.(?:\d{3,4}p|AMZN|WEB[-_.]?DL|WEBRip|BluRay|BDRip|HDTV|x264|x265|AVC|HEVC|H\.264|AAC2\.0|DDP2\.0|DTS|ITA|ENG|SUB|AC3|MKV|MP4|AVI|FLAC|HDR|UNCUT|REPACK|PROPER|LIMITED)(?:\.[^.]+)*$',
    re.I,
)


def clean_episode_filename(raw_name: str) -> str:
    raw_name = raw_name.strip().replace(" ", ".")
    extension = ""
    match = re.search(r"(\.[A-Za-z0-9]{1,5})$", raw_name)
    if match:
        extension = match.group(1)
        raw_name = raw_name[: -len(extension)]

    raw_name = RELEASE_TAG_RE.sub("", raw_name)
    raw_name = re.sub(r"\.+", ".", raw_name).strip(".")
    raw_name = raw_name.rstrip(".")

    return f"{raw_name}{extension}" if extension else raw_name


class EpisodeInfo:
    def __init__(self, number: int, episode_id: int, file_name: str, scws_id: Optional[int]):
        self.number = number
        self.episode_id = episode_id
        self.file_name = file_name
        self.scws_id = scws_id

    def filename(self, index_width: int = 3) -> str:
        if self.file_name:
            base = clean_episode_filename(self.file_name)
        else:
            base = f"episode_{self.number:0{index_width}d}"
        if not re.search(r"\.[A-Za-z0-9]{1,5}$", base):
            base += ".mp4"
        return sanitize_filename(base)


def sanitize_filename(name: str) -> str:
    name = name.strip().replace("/", "_").replace("\\", "_")
    name = re.sub(r'[<>:"|?*]', "_", name)
    return name


def build_session() -> requests.Session:
    session = requests.Session()
    retries = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["HEAD", "GET", "OPTIONS"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def parse_episode_range(range_value: str) -> List[int]:
    values: List[int] = []
    for part in range_value.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start_str, end_str = part.split("-", 1)
            start = int(start_str.strip())
            end = int(end_str.strip())
            if start <= end:
                values.extend(range(start, end + 1))
            else:
                values.extend(range(start, end - 1, -1))
        else:
            values.append(int(part))
    unique_values = []
    seen = set()
    for v in values:
        if v not in seen:
            seen.add(v)
            unique_values.append(v)
    return unique_values


def get_html(url: str, session: Optional[requests.Session] = None) -> str:
    session = session or requests.Session()
    headers = {"User-Agent": USER_AGENT}
    resp = session.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.text


def parse_episodes_from_anime_page(html_text: str) -> Dict[int, EpisodeInfo]:
    match = EPISODES_ATTR_RE.search(html_text)
    if not match:
        raise ValueError("Non è stato possibile trovare l'elenco degli episodi nella pagina anime.")

    raw = html.unescape(match.group(1))
    episodes_data = json.loads(raw)
    episodes: Dict[int, EpisodeInfo] = {}
    for item in episodes_data:
        try:
            number = int(item.get("number", 0))
        except Exception:
            continue
        episode_id = int(item.get("id", 0))
        file_name = item.get("file_name") or item.get("link") or f"episode_{number:03}.mp4"
        scws_id = item.get("scws_id")
        episodes[number] = EpisodeInfo(number=number, episode_id=episode_id, file_name=file_name, scws_id=scws_id)
    return episodes


def extract_embed_url(episode_page_html: str) -> str:
    match = EMBED_URL_RE.search(episode_page_html)
    if not match:
        raise ValueError("Non è stato possibile trovare l'embed URL nella pagina episodio.")
    return html.unescape(match.group("url"))


def extract_download_url(embed_page_html: str) -> str:
    match = DOWNLOAD_URL_RE.search(embed_page_html)
    if match:
        url = match.group("url") or match.group("url2")
        if url:
            return html.unescape(url)
    match = PLAYLIST_URL_RE.search(embed_page_html)
    if match:
        return html.unescape(match.group("url"))
    raise ValueError("Impossibile trovare un URL di download valido nella pagina embed.")


def make_episode_url(anime_url: str, episode_id: int) -> str:
    anime_url = anime_url.rstrip("/")
    return f"{anime_url}/{episode_id}"


def download_file(url: str, path: Path, session: Optional[requests.Session] = None, referer: Optional[str] = None) -> None:
    session = session or requests.Session()
    headers = {"User-Agent": USER_AGENT}
    if referer:
        headers["Referer"] = referer
    with session.get(url, headers=headers, stream=True, timeout=60) as resp:
        resp.raise_for_status()
        path.parent.mkdir(parents=True, exist_ok=True)
        total = int(resp.headers.get("Content-Length", "0"))
        logging.info("Scaricando %s (%s bytes)...", path.name, total if total else "?")
        with open(path, "wb") as out_file:
            for chunk in resp.iter_content(chunk_size=262144):
                if chunk:
                    out_file.write(chunk)
    logging.info("Download completato: %s", path)


def download_episode(episode: EpisodeInfo, anime_url: str, project_folder: Path, filename_width: int) -> None:
    session = build_session()
    try:
        episode_url = make_episode_url(anime_url, episode.episode_id)
        logging.info("Elaboro episodio %d -> %s", episode.number, episode_url)
        episode_html = get_html(episode_url, session=session)
        embed_url = extract_embed_url(episode_html)
        logging.info("Embed URL: %s", embed_url)
        embed_html = get_html(embed_url, session=session)
        download_url = extract_download_url(embed_html)
        logging.info("Download URL: %s", download_url)
        output_file = project_folder / episode.filename(index_width=filename_width)
        if output_file.exists():
            logging.info("File già esistente, salto: %s", output_file)
            return
        download_file(download_url, output_file, session=session, referer=embed_url)
    except Exception as exc:
        logging.exception("Errore durante il download dell'episodio %d: %s", episode.number, exc)
    finally:
        session.close()


def ensure_output_folder(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def main() -> int:
    parser = argparse.ArgumentParser(description="AnimeUnity Downloader: scarica episodi da animeunity.so")
    parser.add_argument("anime_url", help="URL della pagina anime, es: https://www.animeunity.so/anime/743-detective-conan")
    parser.add_argument("range", help="Range di episodi, es: 1-40 oppure 1,3,5-7")
    parser.add_argument("--output", "-o", default="downloads", help="Cartella di output")
    parser.add_argument("--workers", "-w", type=int, default=4, help="Numero di episodi da scaricare in parallelo")
    args = parser.parse_args()

    output_root = Path(args.output)
    ensure_output_folder(output_root)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(output_root / "animeunity_downloader.log", encoding="utf-8")],
    )

    session = build_session()
    anime_html = get_html(args.anime_url, session=session)
    episodes = parse_episodes_from_anime_page(anime_html)
    if not episodes:
        logging.error("Nessun episodio trovato nella pagina anime.")
        return 1

    requested_numbers = parse_episode_range(args.range)
    if not requested_numbers:
        logging.error("Range di episodi non valido: %s", args.range)
        return 1

    logging.info("Trovati %d episodi nella pagina anime.", len(episodes))
    logging.info("Richiesti episodi: %s", requested_numbers)

    slug = args.anime_url.rstrip("/").split("/")[-1]
    project_folder = output_root / slug
    ensure_output_folder(project_folder)

    filename_width = len(str(max(requested_numbers)))
    episodes_to_download: List[EpisodeInfo] = []
    for episode_number in requested_numbers:
        if episode_number not in episodes:
            logging.warning("Episodio %d non trovato, salto.", episode_number)
            continue
        episodes_to_download.append(episodes[episode_number])

    if not episodes_to_download:
        logging.error("Nessun episodio valido da scaricare.")
        return 1

    logging.info("Avvio download parallelo con %d worker.", args.workers)
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [executor.submit(download_episode, episode, args.anime_url, project_folder, filename_width)
                   for episode in episodes_to_download]
        for future in concurrent.futures.as_completed(futures):
            try:
                future.result()
            except Exception:
                pass

    logging.info("Elaborazione completata.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
