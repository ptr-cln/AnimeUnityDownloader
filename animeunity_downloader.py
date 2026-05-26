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
import os
import re
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Dict, List, Optional, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.7444.265 Safari/537.36"
EPISODES_ATTR_RE = re.compile(r'episodes\s*=\s*(?P<quote>["\'])?(?P<json>\[.*?\])(?P=quote)?', re.S)
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


class TkTextHandler(logging.Handler):
    def __init__(self, text_widget: tk.Text):
        super().__init__()
        self.text_widget = text_widget

    def emit(self, record: logging.LogRecord) -> None:
        msg = self.format(record) + "\n"

        def append() -> None:
            self.text_widget.configure(state="normal")
            self.text_widget.insert("end", msg)
            self.text_widget.see("end")
            self.text_widget.configure(state="disabled")

        self.text_widget.after(0, append)


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
    matches = list(EPISODES_ATTR_RE.finditer(html_text))
    if not matches:
        raise ValueError("Non è stato possibile trovare l'elenco degli episodi nella pagina anime.")

    episodes: Dict[int, EpisodeInfo] = {}
    for match in matches:
        raw = html.unescape(match.group("json"))
        try:
            episodes_data = json.loads(raw)
        except json.JSONDecodeError:
            continue

        for item in episodes_data:
            try:
                number = int(item.get("number", 0))
            except Exception:
                continue
            if number in episodes:
                continue
            episode_id = int(item.get("id", 0))
            file_name = item.get("file_name") or item.get("link") or f"episode_{number:03}.mp4"
            scws_id = item.get("scws_id")
            episodes[number] = EpisodeInfo(number=number, episode_id=episode_id, file_name=file_name, scws_id=scws_id)

    if not episodes:
        raise ValueError("Non è stato possibile trovare l'elenco degli episodi nella pagina anime.")
    return episodes


def extract_anime_id_from_url(anime_url: str) -> Optional[int]:
    match = re.search(r'/anime/(\d+)', anime_url)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def fetch_episodes_from_api(anime_id: int, start_range: int, end_range: int, session: Optional[requests.Session] = None) -> Tuple[List[EpisodeInfo], int]:
    session = session or build_session()
    url = f"https://www.animeunity.so/info_api/{anime_id}/1?start_range={start_range}&end_range={end_range}"
    logging.info("Sto richiedendo episodi %d-%d da info_api...", start_range, end_range)
    response = get_html(url, session=session)
    data = json.loads(response)
    episodes: List[EpisodeInfo] = []
    total = int(data.get("episodes_count", 0))
    for item in data.get("episodes", []):
        try:
            number = int(item.get("number", 0))
        except Exception:
            continue
        episode_id = int(item.get("id", 0))
        file_name = item.get("file_name") or item.get("link") or f"episode_{number:03}.mp4"
        scws_id = item.get("scws_id")
        episodes.append(EpisodeInfo(number=number, episode_id=episode_id, file_name=file_name, scws_id=scws_id))
    return episodes, total





def load_all_episodes(anime_url: str, session: Optional[requests.Session] = None) -> Dict[int, EpisodeInfo]:
    session = session or build_session()
    episodes = parse_episodes_from_anime_page(get_html(anime_url, session=session))
    anime_id = extract_anime_id_from_url(anime_url)
    if anime_id is None:
        return episodes

    max_number = max(episodes) if episodes else 0
    if max_number < 1:
        return episodes

    block_size = 120
    start = max_number + 1
    # Use API-provided total count when available to loop ranges correctly
    while True:
        end = start + block_size - 1
        block, total = fetch_episodes_from_api(anime_id, start, end, session=session)
        if not block:
            break
        added = 0
        for episode in block:
            if episode.number not in episodes:
                episodes[episode.number] = episode
                added += 1
        # if API provides total, stop when we've reached it
        if total and end >= total:
            break
        # otherwise continue to next block; also stop if no new episodes added
        if added == 0:
            break
        start += block_size

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


def download_episode(episode: EpisodeInfo, anime_url: str, project_folder: Path, filename_width: int) -> tuple[int, bool]:
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
            return episode.number, True
        download_file(download_url, output_file, session=session, referer=embed_url)
        return episode.number, True
    except Exception as exc:
        logging.exception("Errore durante il download dell'episodio %d: %s", episode.number, exc)
        return episode.number, False
    finally:
        session.close()


def ensure_output_folder(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def configure_logging(output_root: Path, gui_text: Optional[tk.Text] = None) -> Path:
    ensure_output_folder(output_root)
    log_path = output_root / "animeunity_downloader.log"
    handlers = [logging.StreamHandler(sys.stdout), logging.FileHandler(log_path, encoding="utf-8")]
    if gui_text is not None:
        gui_handler = TkTextHandler(gui_text)
        gui_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        handlers.append(gui_handler)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", handlers=handlers)
    return log_path


def download_job(anime_url: str, range_value: str, output_dir: str, workers: int, gui_text: Optional[tk.Text] = None) -> None:
    output_root = Path(output_dir)
    log_path = configure_logging(output_root, gui_text)

    try:
        session = build_session()
        episodes = load_all_episodes(anime_url, session=session)
        if not episodes:
            logging.error("Nessun episodio trovato nella pagina anime.")
            return

        requested_numbers = parse_episode_range(range_value)
        if not requested_numbers:
            logging.error("Range di episodi non valido: %s", range_value)
            return

        logging.info("Trovati %d episodi nella pagina anime.", len(episodes))
        logging.info("Richiesti episodi: %s", requested_numbers)

        slug = anime_url.rstrip("/").split("/")[-1]
        project_folder = output_root / slug
        ensure_output_folder(project_folder)

        filename_width = len(str(max(requested_numbers)))
        episodes_to_download: List[EpisodeInfo] = []
        missing_numbers: List[int] = []
        for episode_number in requested_numbers:
            if episode_number not in episodes:
                logging.warning("Episodio %d non trovato, salto.", episode_number)
                missing_numbers.append(episode_number)
                continue
            episodes_to_download.append(episodes[episode_number])

        if not episodes_to_download:
            logging.error("Nessun episodio valido da scaricare.")
            if missing_numbers:
                not_downloaded = sorted(set(missing_numbers))
                logging.warning("Episodi non scaricati: %s", not_downloaded)
                if gui_text is not None:
                    gui_text.after(0, lambda: messagebox.showwarning("Download incompleto", f"Episodi non scaricati: {', '.join(str(n) for n in not_downloaded)}"))
            return

        logging.info("Avvio download parallelo con %d worker.", workers)
        failed_numbers: List[int] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(download_episode, episode, anime_url, project_folder, filename_width)
                       for episode in episodes_to_download]
            for future in concurrent.futures.as_completed(futures):
                try:
                    episode_number, success = future.result()
                    if not success:
                        failed_numbers.append(episode_number)
                except Exception as exc:
                    logging.exception("Errore inatteso nel task di download: %s", exc)
        not_downloaded = sorted(set(missing_numbers + failed_numbers))
        if not_downloaded:
            logging.warning("Episodi non scaricati: %s", not_downloaded)
            if gui_text is not None:
                gui_text.after(0, lambda: messagebox.showwarning("Download incompleto", f"Episodi non scaricati: {', '.join(str(n) for n in not_downloaded)}"))

        logging.info("Elaborazione completata. Log: %s", log_path)
    except Exception as exc:
        logging.exception("Errore generale: %s", exc)


def load_episodes_for_url(anime_url: str) -> Dict[int, EpisodeInfo]:
    session = build_session()
    return load_all_episodes(anime_url, session=session)


def on_load_episodes(url_var: tk.StringVar, episode_listbox: tk.Listbox, status_label: tk.Label) -> None:
    anime_url = url_var.get().strip()
    if not anime_url:
        messagebox.showwarning("Attenzione", "Inserisci l'URL dell'anime prima di caricare gli episodi.")
        return

    def task() -> None:
        try:
            episodes = load_episodes_for_url(anime_url)
            episode_listbox.delete(0, "end")
            for number in sorted(episodes):
                episode = episodes[number]
                episode_listbox.insert("end", f"{episode.number} {episode.file_name}")
            status_label.config(text=f"Trovati {len(episodes)} episodi")
        except Exception as exc:
            messagebox.showerror("Errore", f"Impossibile caricare gli episodi:\n{exc}")
            status_label.config(text="Errore durante il caricamento degli episodi.")

    threading.Thread(target=task, daemon=True).start()


def on_start(url_var: tk.StringVar, range_var: tk.StringVar, output_var: tk.StringVar, workers_var: tk.StringVar, start_button: ttk.Button, status_label: tk.Label, log_text: tk.Text) -> None:
    anime_url = url_var.get().strip()
    output_dir = output_var.get().strip() or "downloads"
    workers = int(workers_var.get() or 4)
    range_value = range_var.get().strip()

    if not anime_url:
        messagebox.showwarning("Attenzione", "Inserisci l'URL dell'anime.")
        return

    if not range_value:
        messagebox.showwarning("Attenzione", "Inserisci un range di episodi, ad esempio 1-12 o 1,3,5-7.")
        return

    start_button.config(state="disabled")
    status_label.config(text="Download in corso...")
    log_text.configure(state="normal")
    log_text.delete("1.0", "end")
    log_text.configure(state="disabled")

    def task() -> None:
        download_job(anime_url, range_value, output_dir, workers, gui_text=log_text)
        start_button.config(state="normal")
        status_label.config(text="Download completato.")

    threading.Thread(target=task, daemon=True).start()


def on_browse_output(output_var: tk.StringVar) -> None:
    folder = filedialog.askdirectory(title="Seleziona cartella di output")
    if folder:
        output_var.set(folder)


def on_open_log(output_var: tk.StringVar) -> None:
    output_dir = output_var.get().strip() or "downloads"
    log_path = Path(output_dir) / "animeunity_downloader.log"
    if log_path.exists():
        try:
            os.startfile(str(log_path))
        except Exception as exc:
            messagebox.showerror("Errore", f"Impossibile aprire il file log:\n{exc}")
    else:
        messagebox.showinfo("Info", "Il file log non esiste ancora. Avvia un download per crearlo.")


def start_gui() -> None:
    root = tk.Tk()
    root.title("AnimeUnity Downloader")
    root.geometry("780x620")

    frame = ttk.Frame(root, padding=10)
    frame.pack(fill="both", expand=True)

    url_var = tk.StringVar()
    range_var = tk.StringVar()
    output_var = tk.StringVar(value="downloads")
    workers_var = tk.StringVar(value="4")

    ttk.Label(frame, text="URL dell'anime:").grid(row=0, column=0, sticky="w")
    url_entry = ttk.Entry(frame, textvariable=url_var, width=80)
    url_entry.grid(row=0, column=1, columnspan=3, sticky="ew", pady=2)

    ttk.Label(frame, text="Range episodi:").grid(row=1, column=0, sticky="w")
    range_entry = ttk.Entry(frame, textvariable=range_var, width=40)
    range_entry.grid(row=1, column=1, sticky="w", pady=2)
    ttk.Label(frame, text="Esempio: 1-12 oppure 1,3,5-7", foreground="gray").grid(row=2, column=1, sticky="w")
    ttk.Button(frame, text="Carica episodi", command=lambda: on_load_episodes(url_var, episode_listbox, status_label)).grid(row=1, column=2, sticky="w", padx=4)

    ttk.Label(frame, text="Cartella output:").grid(row=3, column=0, sticky="w")
    output_entry = ttk.Entry(frame, textvariable=output_var, width=60)
    output_entry.grid(row=3, column=1, sticky="w", pady=2)
    ttk.Button(frame, text="Sfoglia...", command=lambda: on_browse_output(output_var)).grid(row=3, column=2, sticky="w", padx=4)

    ttk.Label(frame, text="Worker paralleli:").grid(row=4, column=0, sticky="w")
    workers_entry = ttk.Entry(frame, textvariable=workers_var, width=10)
    workers_entry.grid(row=4, column=1, sticky="w", pady=2)

    start_button = ttk.Button(frame, text="Avvia download", command=lambda: on_start(url_var, range_var, output_var, workers_var, start_button, status_label, log_text))
    start_button.grid(row=4, column=2, sticky="w", padx=4)
    ttk.Button(frame, text="Apri log", command=lambda: on_open_log(output_var)).grid(row=4, column=3, sticky="w", padx=4)

    ttk.Label(frame, text="Lista episodi (solo visualizzazione):").grid(row=5, column=0, sticky="nw", pady=(10, 0))
    episode_listbox = tk.Listbox(frame, selectmode="none", height=12, width=80)
    episode_listbox.grid(row=5, column=1, columnspan=3, sticky="nsew", pady=(10, 0))
    episode_scroll = ttk.Scrollbar(frame, orient="vertical", command=episode_listbox.yview)
    episode_scroll.grid(row=5, column=4, sticky="ns", pady=(10, 0))
    episode_listbox.config(yscrollcommand=episode_scroll.set)

    status_label = ttk.Label(frame, text="Pronto.")
    status_label.grid(row=5, column=0, columnspan=4, sticky="w", pady=(10, 0))

    ttk.Label(frame, text="Log e stato:").grid(row=6, column=0, sticky="nw", pady=(10, 0))
    log_text = tk.Text(frame, wrap="word", state="disabled", width=90, height=18)
    log_text.grid(row=6, column=1, columnspan=3, sticky="nsew", pady=(10, 0))
    log_scroll = ttk.Scrollbar(frame, orient="vertical", command=log_text.yview)
    log_scroll.grid(row=6, column=4, sticky="ns", pady=(10, 0))
    log_text.config(yscrollcommand=log_scroll.set)

    frame.columnconfigure(1, weight=1)
    frame.rowconfigure(6, weight=1)

    root.mainloop()


def main() -> int:
    parser = argparse.ArgumentParser(description="AnimeUnity Downloader: scarica episodi da animeunity.so")
    parser.add_argument("anime_url", nargs="?", help="URL della pagina anime, es: https://www.animeunity.so/anime/743-detective-conan")
    parser.add_argument("range", nargs="?", help="Range di episodi, es: 1-40 oppure 1,3,5-7")
    parser.add_argument("--output", "-o", default="downloads", help="Cartella di output")
    parser.add_argument("--workers", "-w", type=int, default=4, help="Numero di episodi da scaricare in parallelo")
    args = parser.parse_args()

    if not args.anime_url or not args.range:
        start_gui()
        return 0

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
