"""Stream Picker - local web UI around ffprobe that generates a .bat remux command.

Run:  python stream_picker.py [folder]
Then open http://127.0.0.1:8765 (opens automatically).

ffprobe/ffmpeg lookup order: env FFMPEG_DIR, folder of this script, PATH.
"""
import json
import os
import shutil
import subprocess
import sys
import threading
import webbrowser
import functools
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

PORT = 8765
HERE = Path(__file__).resolve().parent
BASE_DIR = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else HERE.parent
VIDEO_EXT = {".mkv", ".mp4", ".m4v", ".mov", ".avi", ".ts", ".m2ts", ".webm", ".wmv", ".flv", ".mpg", ".mpeg"}


@functools.lru_cache(maxsize=None)   # a PATH search per video made folder scans slow
def find_tool(name):
    for d in (os.environ.get("FFMPEG_DIR"), HERE):
        if d:
            p = Path(d) / f"{name}.exe"
            if p.is_file():
                return str(p)
    return shutil.which(name)


def resolve(raw):
    raw = raw.strip().strip('"').strip()
    p = Path(raw)
    if not p.is_absolute():
        p = BASE_DIR / p
    return p.resolve()


SUB_CODEC = {".srt": "subrip", ".ass": "ass", ".ssa": "ass", ".vtt": "webvtt", ".sup": "hdmv_pgs_subtitle"}
SUB_DIRS = ("subs", "subtitles", "sub", "titulky")
LANG_WORDS = {
    "eng": ("en", "eng", "english", "anglicky"),
    "cze": ("cs", "cz", "cze", "ces", "czech", "cesky", "cestina", "čeština", "česky"),
    "slo": ("sk", "slo", "slk", "slovak", "slovensky"),
    "ger": ("de", "ger", "deu", "german", "deutsch"),
    "fre": ("fr", "fre", "fra", "french"),
    "spa": ("es", "spa", "spanish"),
    "ita": ("ita", "italian"),
    "pol": ("pl", "pol", "polish"),
    "rus": ("ru", "rus", "russian"),
    "hun": ("hu", "hun", "hungarian"),
    "por": ("pt", "por", "portuguese"),
}
WORD_LANG = {w: code for code, words in LANG_WORDS.items() for w in words}
FLAG_WORDS = {"sdh", "hi", "cc", "forced", "full"}


def norm_tokens(name):
    return [t for t in "".join(c.lower() if c.isalnum() else " " for c in name).split() if t]


def episode(tokens):
    for t in tokens:
        if len(t) >= 6 and t[0] == "s" and "e" in t[1:]:
            a, _, b = t[1:].partition("e")
            if a.isdigit() and b.isdigit():
                return int(a), int(b)
    return None


def sub_info(tokens, scan_all):
    """Language / flags from trailing tokens (or every token for files inside a Subs folder)."""
    rest = list(tokens)
    lang, flags = None, set()
    while rest and (rest[-1] in WORD_LANG or rest[-1] in FLAG_WORDS):
        t = rest.pop()
        if t in FLAG_WORDS:
            flags.add(t)
        elif not lang:
            lang = WORD_LANG[t]
    if scan_all and not lang:
        lang = next((WORD_LANG[t] for t in tokens if t in WORD_LANG and len(t) > 2), None)
    return rest, lang, flags


def charenc(path):
    head = path.read_bytes()
    if head[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return "UTF-16"
    try:
        head.decode("utf-8")
        return None
    except UnicodeDecodeError:
        return "CP1250"   # Czech/Central European legacy encoding


# Release-name noise ignored by the fuzzy match ("Skyfall.2012.1080p.BluRay.x264-SPARKS" -> skyfall 2012)
JUNK_WORDS = {
    "480p", "576p", "720p", "1080p", "1080i", "2160p", "4k", "uhd", "sd", "hd", "fhd", "hdr", "hdr10", "hdr10plus", "dv", "dovi", "sdr",
    "bluray", "blu", "ray", "bdrip", "brrip", "bdremux", "remux", "webrip", "web", "webdl", "dl", "hdtv", "dvdrip", "dvd", "hdrip",
    "x264", "x265", "h264", "h265", "hevc", "avc", "xvid", "divx", "10bit", "8bit", "aac", "ac3", "eac3", "dd", "ddp", "dd5", "ddp5",
    "dts", "truehd", "atmos", "flac", "dual", "multi", "proper", "repack", "extended", "unrated", "remastered", "directors", "cut",
    "imax", "amzn", "nf", "dsnp", "hmax", "atvp", "yify", "yts", "rarbg", "sparks", "titulky", "subtitles", "subs", "sub",
}
STOP_WORDS = {"the", "and", "of", "a", "an", "in", "on", "to", "is", "der", "die", "das", "le", "la", "les"}


def is_year(t):
    return len(t) == 4 and t.isdigit() and t[:2] in ("19", "20")


def title_words(tokens):
    """Tokens that describe the film: no release noise, language/flag words, stop words or lone digits."""
    return {t for t in tokens if t not in JUNK_WORDS and t not in WORD_LANG and t not in FLAG_WORDS
            and t not in STOP_WORDS and (len(t) > 1 or not t.isdigit())}


def fuzzy_score(vwords, swords):
    """Share of the shorter name's words found in the other; 0 unless a real (non-year) word is shared."""
    common = vwords & swords
    if not any(not is_year(t) and len(t) >= 3 for t in common):
        return 0.0
    return len(common) / min(len(vwords), len(swords))


def sub_lang(tokens):
    """Language + flags, ignoring release noise so "Skyfall.2012.CZ.1080p.WEB-DL" still ends in cz."""
    _, lang, flags = sub_info([t for t in tokens if t not in JUNK_WORDS], True)
    return lang, flags


def sub_entry(p, folder, lang=None, flags=frozenset(), match="name", score=None):
    if lang is None:
        lang, flags = sub_lang(norm_tokens(p.stem))
    try:
        name = str(p.relative_to(folder))
    except ValueError:
        name = str(p)
    return {
        "path": str(p),
        "name": name,
        "codec": SUB_CODEC[p.suffix.lower()],
        "lang": lang,
        "sdh": bool(flags & {"sdh", "hi", "cc"}),
        "forced": "forced" in flags,
        "charenc": None if p.suffix.lower() == ".sup" else charenc(p),
        "match": match,   # name | fuzzy | manual
        "score": score,
    }


def folder_listing(folder, memo=None):
    """(files, subfolders) of `folder` via scandir (no extra disk access per entry). `memo` shares
    listings between the videos of one folder scan instead of re-reading the folder for each video."""
    key = ("ls", folder)
    if memo is not None and key in memo:
        return memo[key]
    files, dirs = [], []
    try:
        with os.scandir(folder) as it:
            for e in it:
                (dirs if e.is_dir() else files).append(Path(e.path))
    except OSError:
        pass
    if memo is not None:
        memo[key] = (files, dirs)
    return files, dirs


def find_subs(video, memo=None):
    """External subtitle files that belong to `video`: same folder or a Subs/Subtitles subfolder.
    Exact name / episode / Subs-folder rules first; otherwise a fuzzy match on the title words, and only
    when no other video in the folder matches that subtitle better."""
    folder = video.parent
    files, dirs = folder_listing(folder, memo)
    candidates = [(p, False) for p in files]
    for d in dirs:
        if d.name.lower() in SUB_DIRS:
            sub_files, sub_dirs = folder_listing(d, memo)
            candidates += [(p, True) for p in sub_files]
            own = d / video.stem   # Subs/<video name>/...
            if own in sub_dirs:
                candidates += [(p, "own") for p in folder_listing(own, memo)[0]]
    candidates = [(p, where) for p, where in candidates if p.suffix.lower() in SUB_CODEC]
    if not candidates:
        return []   # most folders have no subtitle files: skip all the name matching
    vtok = norm_tokens(video.stem)
    vnorm, vep = " ".join(vtok), episode(vtok)
    videos_here = [p for p in files if p.suffix.lower() in VIDEO_EXT]
    # Title words of every video in the folder, worked out once per folder
    words_key = ("words", folder)
    vwords = memo.get(words_key) if memo is not None else None
    if vwords is None:
        vwords = {v: title_words(norm_tokens(v.stem)) for v in videos_here}
        if memo is not None:
            memo[words_key] = vwords
    vwords = dict(vwords)
    vwords.setdefault(video, title_words(vtok))
    found = []
    for p, in_sub_dir in candidates:
        tok = norm_tokens(p.stem)
        rest, _, _ = sub_info(tok, True)
        lang, flags = sub_lang(tok)
        snorm = " ".join(rest)
        similar = len(snorm) >= 4 and (vnorm.startswith(snorm) or snorm.startswith(vnorm))
        sep = episode(tok)
        same_ep = vep is not None and vep == sep and tok[:1] == vtok[:1]
        lone_video = in_sub_dir is True and len(videos_here) == 1
        if similar or same_ep or lone_video or in_sub_dir == "own":
            found.append(sub_entry(p, folder, lang, flags))
            continue
        if vep is not None and sep is not None and vep != sep:
            continue   # another episode of the same show
        swords = title_words(tok)
        scores = {v: fuzzy_score(w, swords) for v, w in vwords.items()}
        best = scores[video]
        if best >= 0.5 and best >= max(scores.values()):
            found.append(sub_entry(p, folder, lang, flags, "fuzzy", round(best, 2)))
    return sorted(found, key=lambda s: (s["match"] != "name", s["name"].lower()))


# ffprobe results cached by path + size + modification time, so a folder rescan only probes new or changed
# files. Kept on disk next to the script; files that vanished from a scanned folder are forgotten.
CACHE_FILE = HERE / ".probe_cache.json"
_cache_lock = threading.Lock()
_cache_dirty = False
try:
    _cache = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
except (OSError, ValueError):
    _cache = {}


def ffprobe_json(ffprobe, path):
    """(ffprobe data, came from cache) for `path`; raises RuntimeError with ffprobe's message on failure."""
    global _cache_dirty
    st = path.stat()
    key, stamp = str(path).lower(), [st.st_size, st.st_mtime_ns]
    with _cache_lock:
        hit = _cache.get(key)
    if hit and hit["stamp"] == stamp:
        return hit["data"], True
    cmd = [ffprobe, "-v", "error", "-show_streams", "-show_format", "-show_chapters", "-of", "json", str(path)]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=120)
    except subprocess.TimeoutExpired:
        raise RuntimeError("ffprobe timed out")
    if r.returncode != 0:
        msg = r.stderr.decode("utf-8", "replace")
        if "moov atom not found" in msg:   # MP4/MOV whose recording or download was cut off
            raise RuntimeError("MP4 index (moov) missing: the recording or download was cut off. ffmpeg can't open it; "
                               "a tool like untrunc can rebuild it using an intact video from the same camera/phone.")
        raise RuntimeError(msg or "ffprobe failed")
    data = json.loads(r.stdout.decode("utf-8", "replace"))
    with _cache_lock:
        _cache[key] = {"stamp": stamp, "data": data}
        _cache_dirty = True
    return data, False


def save_cache(scanned_root=None, recursive=True, present=()):
    """Write the cache if it changed. After a folder scan, drop entries under that folder whose file is gone
    (only direct children when the scan wasn't recursive)."""
    global _cache_dirty
    with _cache_lock:
        if scanned_root is not None:
            prefix = str(scanned_root).lower().rstrip("\\/") + os.sep
            present = {str(p).lower() for p in present}
            gone = [k for k in _cache if k.startswith(prefix) and k not in present
                    and (recursive or os.sep not in k[len(prefix):])]
            for k in gone:
                del _cache[k]
            _cache_dirty |= bool(gone)
        if not _cache_dirty:
            return
        snapshot = json.dumps(_cache)
        _cache_dirty = False
    tmp = CACHE_FILE.with_suffix(".tmp")
    try:
        tmp.write_text(snapshot, encoding="utf-8")
        os.replace(tmp, CACHE_FILE)
    except OSError:
        pass   # a cache that can't be written only costs speed


def probe(raw, memo=None):
    ffprobe = find_tool("ffprobe")
    if not ffprobe:
        return 500, {"error": "ffprobe not found. Install ffmpeg (e.g. `winget install Gyan.FFmpeg`), "
                              "add it to PATH, or put ffprobe.exe + ffmpeg.exe next to stream_picker.py."}
    path = resolve(raw)
    if path.is_dir():
        return 200, {"dir": True, "path": str(path)}   # the page switches to the folder view
    if not path.is_file():
        return 404, {"error": f"File not found: {path}"}
    try:
        data, cached = ffprobe_json(ffprobe, path)
    except RuntimeError as e:
        return 500, {"error": str(e)}
    # External subtitles are looked up fresh every time: new .srt files show up without touching the video
    return 200, dict(data, path=str(path), external_subs=find_subs(path, memo), cached=cached,
                     ffmpeg=find_tool("ffmpeg") or "ffmpeg", cpus=os.cpu_count() or 1)


BROWSE_PS = r"""
Add-Type -AssemblyName System.Windows.Forms
[Console]::OutputEncoding = [Text.Encoding]::UTF8
Add-Type -Namespace SP -Name Win -MemberDefinition @'
[DllImport("user32.dll")] public static extern bool SetForegroundWindow(System.IntPtr hWnd);
[DllImport("user32.dll")] public static extern void keybd_event(byte vk, byte scan, uint flags, System.UIntPtr extra);
'@
# Invisible always-on-top owner in the screen centre: the dialog opens centred and above the browser
$owner = New-Object System.Windows.Forms.Form -Property @{
  TopMost = $true; ShowInTaskbar = $false; FormBorderStyle = 'None'; Opacity = 0
  StartPosition = 'CenterScreen'; Size = New-Object System.Drawing.Size(1, 1)
}
$owner.Show()
# Windows blocks background programs from taking focus; a tap of Alt counts as user input and lifts that lock
[SP.Win]::keybd_event(0x12, 0, 0, [UIntPtr]::Zero)
[SP.Win]::keybd_event(0x12, 0, 2, [UIntPtr]::Zero)
[void][SP.Win]::SetForegroundWindow($owner.Handle)
$owner.Activate()
$d = New-Object System.Windows.Forms.OpenFileDialog
$d.InitialDirectory = $env:SP_DIR
$d.Filter = $env:SP_FILTER
if ($env:SP_FOLDER) {
  # Folder mode on the normal Open dialog: files are hidden, the user opens a folder and clicks Open
  $d.Title = 'Open the folder, then click Open'
  $d.ValidateNames = $false; $d.CheckFileExists = $false; $d.CheckPathExists = $true
  $d.FileName = 'Select this folder'
}
if ($d.ShowDialog($owner) -eq 'OK') { if ($env:SP_FOLDER) { Split-Path $d.FileName } else { $d.FileName } }
"""


VIDEO_FILTER = "Video files|*.mkv;*.mp4;*.m4v;*.mov;*.avi;*.ts;*.m2ts;*.webm;*.wmv;*.flv;*.mpg;*.mpeg|All files|*.*"
SUB_FILTER = "Subtitle files|*.srt;*.ass;*.ssa;*.vtt;*.sup"


def pick_file(raw, file_filter, folder=False):
    # Native Windows dialog: a browser <input type=file> never reveals the full path
    start = BASE_DIR
    if raw.strip():
        p = resolve(raw)
        start = p if p.is_dir() else p.parent
    if not start.is_dir():
        start = BASE_DIR
    env = dict(os.environ, SP_DIR=str(start), SP_FILTER=file_filter, SP_FOLDER="1" if folder else "")
    r = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", BROWSE_PS], capture_output=True, env=env)
    return r.stdout.decode("utf-8", "replace").strip() or None


def browse(raw):
    return 200, {"path": pick_file(raw, VIDEO_FILTER)}


def browse_dir(raw):
    return 200, {"path": pick_file(raw, "Folders|no.files", folder=True)}


SCAN_LIMIT = 3000


def scan(raw, recursive):
    """ffprobe every new or changed video in a folder (in parallel; the rest come from the cache)."""
    root = resolve(raw)
    if not root.is_dir():
        return 404, {"error": f"Folder not found: {root}"}
    if recursive:   # os.walk uses scandir: no extra disk access per entry (matters on HDDs)
        paths = (Path(d, f) for d, _, names in os.walk(root) for f in names)
    else:
        paths = (p for p in folder_listing(root)[0])
    files = sorted((p for p in paths if p.suffix.lower() in VIDEO_EXT), key=lambda p: str(p).lower())
    truncated = len(files) > SCAN_LIMIT
    files = files[:SCAN_LIMIT]
    memo = {}   # folder listings shared by all videos of this scan
    with ThreadPoolExecutor(max_workers=min(8, os.cpu_count() or 4)) as pool:
        results = list(pool.map(lambda p: probe(str(p), memo), files))
    items = []
    for p, (status, data) in zip(files, results):
        item = {"path": str(p), "rel": str(p.relative_to(root)), "size": p.stat().st_size}
        if status == 200:
            item.update(streams=data["streams"], format=data.get("format", {}), external_subs=data["external_subs"],
                        cached=data["cached"])
        else:
            item["error"] = data.get("error", "ffprobe failed")
        items.append(item)
    if not truncated:   # a cut-off scan doesn't know which files are really gone
        save_cache(root, recursive, files)
    else:
        save_cache()
    return 200, {"dir": str(root), "files": items, "truncated": truncated, "limit": SCAN_LIMIT}


def browse_sub(raw):
    """Pick any subtitle file for the video `raw` (names that the automatic match can't pair)."""
    path = pick_file(raw, SUB_FILTER)
    if not path:
        return 200, {"sub": None}
    p = Path(path)
    if p.suffix.lower() not in SUB_CODEC:
        return 400, {"error": f"Not a subtitle file: {p.name}"}
    return 200, {"sub": sub_entry(p, resolve(raw).parent, match="manual")}


def list_files():
    files = sorted(p.name for p in BASE_DIR.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXT)
    dirs = sorted(p.name + os.sep for p in BASE_DIR.iterdir() if p.is_dir() and not p.name.startswith((".", "__")))
    return 200, {"dir": str(BASE_DIR), "files": dirs + files}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, status, body, ctype):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/":
            self.send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
            return
        if url.path == "/probe":
            status, data = probe(parse_qs(url.query).get("path", [""])[0])
            save_cache()
        elif url.path == "/browse":
            status, data = browse(parse_qs(url.query).get("path", [""])[0])
        elif url.path == "/browse_dir":
            status, data = browse_dir(parse_qs(url.query).get("path", [""])[0])
        elif url.path == "/scan":
            q = parse_qs(url.query)
            status, data = scan(q.get("path", [""])[0], q.get("recursive", ["1"])[0] == "1")
        elif url.path == "/browse_sub":
            status, data = browse_sub(parse_qs(url.query).get("path", [""])[0])
        elif url.path == "/list":
            status, data = list_files()
        else:
            status, data = 404, {"error": "not found"}
        self.send(status, json.dumps(data).encode("utf-8"), "application/json")


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}"
    print(f"Stream Picker running at {url}  (base folder: {BASE_DIR})")
    print(f"ffprobe: {find_tool('ffprobe') or 'NOT FOUND'}")
    print("Ctrl+C to stop.")
    threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
