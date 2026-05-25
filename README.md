# AnimeUnity Downloader

Downloader per AnimeUnity (animeunity.so).

Descrizione
- Estrae gli ID degli episodi dalla pagina dell'anime.
- Risolve gli embed (es. Vixcloud) per ogni episodio.
- Scarica il file MP4 in alta qualità nella cartella di output.

Dipendenze
- Python 3.8+
- requests
- urllib3

Installazione

Consiglio di usare un virtualenv:

```bash
python -m venv .venv
# Windows PowerShell
.venv\Scripts\Activate.ps1
# oppure cmd
.venv\Scripts\activate.bat
pip install -r requirements.txt
```

(se non hai un `requirements.txt`, installa `requests` manualmente: `pip install requests`)

Uso

```bash
python animeunity_downloader.py <anime_url> <range> [--output OUTPUT] [--workers N]
```

Esempio:

```bash
python animeunity_downloader.py https://www.animeunity.so/anime/743-detective-conan 1-12 -o downloads -w 4
```

Note
- Il parametro `--workers` ha valore di default 4 (scarica 4 episodi in parallelo).
- I log vengono scritti in `downloads/<slug>/animeunity_downloader.log`.

Limitazioni
- Il programma non tenta l'auth su siti protetti; funziona su embed pubblici raggiungibili.

Contribuire
- Apri issue o fai PR se vuoi aggiungere feature o correggere bug.

Autore
- Creato per uso personale.
