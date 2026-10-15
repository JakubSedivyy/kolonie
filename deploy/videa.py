"""Stáhne název, popis a přepis (titulky) YouTube videí ze seznamu learning/videos.txt
do složky, kterou dostane jako argument (reports/videa). Spouští ho deploy/sync.sh.
Claude z přepisů vytáhne obchodní pravidla a otestuje je v laboratoři.
Už stažená videa přeskočí. Používá yt-dlp (instaluje se do .venv, když chybí)."""
import json
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIST = os.path.join(ROOT, "learning", "videos.txt")
PY = sys.executable


def video_id(url: str) -> str | None:
    m = re.search(r"(?:youtu\.be/|v=|shorts/|live/)([A-Za-z0-9_-]{11})", url)
    return m.group(1) if m else None


def vtt_to_text(vtt: str) -> str:
    lines, last = [], ""
    for ln in vtt.splitlines():
        ln = ln.strip()
        if not ln or ln.startswith(("WEBVTT", "Kind:", "Language:", "NOTE")) or "-->" in ln or ln.isdigit():
            continue
        ln = re.sub(r"<[^>]+>", "", ln).strip()
        if ln and ln != last:                      # automatické titulky opakují řádky
            lines.append(ln)
            last = ln
    return " ".join(lines)


def ensure_ytdlp():
    try:
        import yt_dlp  # noqa: F401
    except ImportError:
        subprocess.run([PY, "-m", "pip", "install", "-q", "--upgrade", "yt-dlp"], check=False)


def fetch(vid: str, out_dir: str) -> str:
    url = f"https://www.youtube.com/watch?v={vid}"
    with tempfile.TemporaryDirectory() as tmp:
        cmd = [PY, "-m", "yt_dlp", "--skip-download", "--write-info-json", "--write-subs", "--write-auto-subs",
               "--sub-langs", "cs,sk,en,en-orig,en-US,en-GB", "--sub-format", "vtt", "--no-warnings",
               "-o", os.path.join(tmp, "%(id)s.%(ext)s"), url]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        info, subs = {}, {}
        for f in os.listdir(tmp):
            p = os.path.join(tmp, f)
            if f.endswith(".info.json"):
                info = json.load(open(p))
            elif f.endswith(".vtt"):
                lang = f.split(".")[-2]
                subs[lang] = vtt_to_text(open(p, encoding="utf-8", errors="ignore").read())
    lang = next((l for l in ("cs", "sk", "en", "en-orig", "en-US", "en-GB") if subs.get(l)), None)
    text = [f"URL: {url}", f"Název: {info.get('title', '?')}", f"Kanál: {info.get('channel', '?')}",
            f"Délka: {round((info.get('duration') or 0) / 60)} min", f"Jazyk přepisu: {lang or 'žádný'}", "",
            "POPIS:", info.get("description", "") or "", "", "PŘEPIS:", subs.get(lang, "") if lang else ""]
    if not info and not subs:
        text.append(f"\n[nepodařilo se stáhnout]\n{r.stderr[-1500:]}")
    path = os.path.join(out_dir, f"{vid}.txt")
    open(path, "w", encoding="utf-8").write("\n".join(text))
    return "ok" if lang else ("bez přepisu" if info else "chyba")


def main():
    out_dir = sys.argv[1]
    os.makedirs(out_dir, exist_ok=True)
    if not os.path.exists(LIST):
        return
    ids = [video_id(l) for l in open(LIST, encoding="utf-8") if l.strip() and not l.startswith("#")]
    todo = [v for v in dict.fromkeys(ids) if v and not os.path.exists(os.path.join(out_dir, f"{v}.txt"))]
    if not todo:
        return
    ensure_ytdlp()
    for v in todo:
        try:
            print(v, fetch(v, out_dir))
        except Exception as e:
            print(v, "chyba", e)


if __name__ == "__main__":
    main()
