#!/usr/bin/env python3
"""nightjar — a terminal audio player.

Live FFT visualizers, a 10-band EQ, internet radio, gapless playback, and
themes — built-in, or derived from any oh-my-posh theme file.

Decoding is done by ffmpeg, output goes to PipeWire/PulseAudio/ALSA via
pw-cat / pacat / paplay / aplay, DSP is numpy.
"""

import codecs
import colorsys
import fcntl
import io
import json
import os
import queue
import random
import re
import select
import shutil
import signal
import subprocess
import sys
import termios
import threading
import time
import tomllib
import tty
import unicodedata
import urllib.request
import zipfile
from collections import deque
from pathlib import Path

import numpy as np

APP = "nightjar"
VERSION = "0.2.0"
RATE = 48000
BLOCK = 2048          # frames per DSP block
SUB = 512             # frames per write to the output pipe
LATENCY = 0.075       # rough output latency (pipe + sound server), seconds
LAT_FRAMES = int(LATENCY * RATE)
CONF_DIR = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / APP
POSH_DIR = CONF_DIR / "posh-themes"
CONF_FILE = CONF_DIR / "config.json"
STATIONS_FILE = CONF_DIR / "stations.m3u"
AUDIO_EXTS = {".mp3", ".flac", ".ogg", ".oga", ".opus", ".wav", ".m4a", ".aac", ".alac",
              ".wma", ".aiff", ".aif", ".ape", ".wv", ".mka", ".webm", ".mp4", ".mpc", ".dsf"}
URL_RE = re.compile(r"^[a-z][a-z0-9+.\-]*://", re.I)


# ───────────────────────────── colour helpers ──────────────────────────────

def hex_rgb(h):
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def rgb_hex(r, g, b):
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(round(v)))) for v in (r, g, b))


def mix(a, b, t):
    ra, rb = hex_rgb(a), hex_rgb(b)
    return rgb_hex(*(x + (y - x) * t for x, y in zip(ra, rb)))


def gradient(cols, t):
    if len(cols) == 1:
        return cols[0]
    t = max(0.0, min(1.0, t))
    seg = t * (len(cols) - 1)
    i = min(int(seg), len(cols) - 2)
    return mix(cols[i], cols[i + 1], seg - i)


def hls(h):
    r, g, b = hex_rgb(h)
    return colorsys.rgb_to_hls(r / 255, g / 255, b / 255)


def luminance(h):
    r, g, b = (v / 255 for v in hex_rgb(h))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def rgb_to_256(r, g, b):
    cube = [0, 95, 135, 175, 215, 255]

    def q(v):
        return 0 if v < 48 else 1 if v < 115 else (v - 35) // 40
    qr, qg, qb = q(r), q(g), q(b)
    ci = 16 + 36 * qr + 6 * qg + qb
    cd = (cube[qr] - r) ** 2 + (cube[qg] - g) ** 2 + (cube[qb] - b) ** 2
    avg = (r + g + b) // 3
    gi = 232 + min(23, max(0, (avg - 8) // 10))
    gv = 8 + 10 * (gi - 232)
    gd = (gv - r) ** 2 + (gv - g) ** 2 + (gv - b) ** 2
    return gi if gd < cd else ci


# ───────────────────────────────── themes ──────────────────────────────────

class Theme:
    def __init__(self, name, bg, fg, dim, accent, accent2, bars, sel_bg=None, source="built-in"):
        self.name, self.bg, self.fg = name, bg, fg
        self.dim = dim or mix(fg, bg, 0.5)
        self.accent, self.accent2 = accent, accent2
        self.bars = list(bars)
        self.sel_bg = sel_bg or mix(accent, bg, 0.78)
        self.panel = mix(bg, fg, 0.07)
        self.unlit = mix(bg, self.dim, 0.35)
        self.source = source
        self.grad = [gradient(self.bars, i / 31) for i in range(32)]


BUILTIN_THEMES = [
    Theme("catppuccin-mocha", "#1e1e2e", "#cdd6f4", "#6c7086", "#cba6f7", "#89b4fa",
          ["#a6e3a1", "#f9e2af", "#f38ba8"], "#313244"),
    Theme("tokyo-night", "#1a1b26", "#c0caf5", "#565f89", "#7aa2f7", "#bb9af7",
          ["#7dcfff", "#bb9af7", "#f7768e"], "#283457"),
    Theme("gruvbox", "#282828", "#ebdbb2", "#928374", "#fabd2f", "#83a598",
          ["#b8bb26", "#fe8019", "#fb4934"], "#3c3836"),
    Theme("dracula", "#282a36", "#f8f8f2", "#6272a4", "#bd93f9", "#ff79c6",
          ["#50fa7b", "#f1fa8c", "#ff5555"], "#44475a"),
    Theme("nord", "#2e3440", "#eceff4", "#616e88", "#88c0d0", "#81a1c1",
          ["#a3be8c", "#ebcb8b", "#bf616a"], "#3b4252"),
    Theme("rose-pine", "#191724", "#e0def4", "#6e6a86", "#ebbcba", "#c4a7e7",
          ["#31748f", "#9ccfd8", "#eb6f92"], "#26233a"),
    Theme("phosphor", "#050a05", "#33ff66", "#1f7a35", "#66ff99", "#33cc55",
          ["#1a8c3a", "#33ff66", "#ccffcc"], "#0f2a14"),
    Theme("synthwave", "#140b26", "#f5e6ff", "#7a5c99", "#ff2a6d", "#05d9e8",
          ["#05d9e8", "#ff2a6d", "#f9f871"], "#2a1748"),
]

# ── oh-my-posh theme import ──
# oh-my-posh themes describe prompt segments with background/foreground colours
# (hex, palette references "p:name", ANSI names, or Go templates containing
# hex). We collect every colour in document order and derive a player palette.

ANSI_NAMES = {
    "black": "#000000", "red": "#cd3131", "green": "#0dbc79", "yellow": "#e5e510",
    "blue": "#2472c8", "magenta": "#bc3fbc", "cyan": "#11a8cd", "white": "#e5e5e5",
    "darkgray": "#666666", "lightred": "#f14c4c", "lightgreen": "#23d18b",
    "lightyellow": "#f5f543", "lightblue": "#3b8eea", "lightmagenta": "#d670d6",
    "lightcyan": "#29b8db", "lightwhite": "#ffffff",
}
COLOR_TOKEN_RE = re.compile(r"p:([A-Za-z0-9_.\-]+)|#([0-9a-fA-F]{6}|[0-9a-fA-F]{3})(?![0-9a-fA-F])")
INLINE_TAG_RE = re.compile(r"<((?:#[0-9a-fA-F]{3,6}|p:[A-Za-z0-9_.\-]+)[^>]*)>")
POSH_FILE_RE = re.compile(r"\.omp\.(json|ya?ml|toml)$", re.I)


def _colors_in(value, palette, depth=0):
    if not isinstance(value, str) or depth > 6:
        return []
    v = value.strip()
    if v.lower() in ANSI_NAMES:
        return [ANSI_NAMES[v.lower()]]
    out = []
    for m in COLOR_TOKEN_RE.finditer(v):
        if m.group(1):
            pv = palette.get(m.group(1))
            if pv:
                out += _colors_in(pv, palette, depth + 1)
        else:
            s = m.group(2).lower()
            if len(s) == 3:
                s = "".join(c * 2 for c in s)
            out.append("#" + s)
    return out


def _walk_colors(node, palette, out, key=""):
    if isinstance(node, dict):
        for k, v in node.items():
            if k in ("palette", "palettes", "$schema"):
                continue
            _walk_colors(v, palette, out, str(k))
    elif isinstance(node, list):
        for v in node:
            _walk_colors(v, palette, out, key)
    elif isinstance(node, str):
        k = key.lower()
        role = "bg" if "background" in k else "fg" if "foreground" in k else "other" if "color" in k else None
        if role:
            out += [(role, c) for c in _colors_in(node, palette)]
        else:  # inline colour tags in templates: <#ff0000>text</> or <p:red,#000>text</>
            for tag in INLINE_TAG_RE.findall(node):
                out += [("other", c) for c in _colors_in(tag, palette)]


def theme_from_colors(name, pairs, accent_hint=None, source=""):
    def uniq(seq):
        seen, out = set(), []
        for c in seq:
            if c not in seen:
                seen.add(c)
                out.append(c)
        return out
    bgs = uniq(c for r, c in pairs if r == "bg")
    fgs = uniq(c for r, c in pairs if r == "fg")
    oth = uniq(c for r, c in pairs if r == "other")
    allc = uniq(bgs + oth + fgs)
    if len(allc) < 2:
        return None

    def L(c): return hls(c)[1]
    def S(c): return hls(c)[2]
    def H(c): return hls(c)[0]

    def hd(a, b):
        d = abs(H(a) - H(b))
        return min(d, 1 - d)

    vivid = [c for c in allc if S(c) >= 0.25 and 0.22 <= L(c) <= 0.88]
    if not vivid:
        vivid = [c for c in allc if 0.2 <= L(c) <= 0.92]
    if not vivid:
        return None
    accent = accent_hint if accent_hint and 0.2 <= L(accent_hint) <= 0.9 else vivid[0]
    accent2 = next((c for c in vivid if hd(c, accent) > 0.08), None) or mix(accent, "#ffffff", 0.35)
    pool = []
    for c in vivid:
        if all(hd(c, p) > 0.04 or abs(L(c) - L(p)) > 0.15 for p in pool):
            pool.append(c)
    if len(pool) >= 3:
        picks = [pool[0], pool[len(pool) // 2], pool[-1]]
    elif len(pool) == 2:
        picks = [pool[0], mix(pool[0], pool[1], 0.5), pool[1]]
    else:
        picks = [mix(accent, "#000000", 0.35), accent, mix(accent, "#ffffff", 0.45)]
    picks.sort(key=luminance)
    darks = [c for c in allc if L(c) < 0.16]
    bg = darks[0] if darks else mix(accent, "#000000", 0.9)
    lights = [c for c in fgs + allc if L(c) >= 0.78]
    fg = lights[0] if lights else "#e6e6e6"
    return Theme(name, bg, fg, mix(fg, bg, 0.5), accent, accent2, picks,
                 mix(accent, bg, 0.75), source)


def parse_posh(path):
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return None
    name = path.name
    for suf in (".omp.json", ".omp.yaml", ".omp.yml", ".omp.toml", ".json", ".yaml", ".yml", ".toml"):
        if name.lower().endswith(suf):
            name = name[: -len(suf)]
            break
    data = None
    try:
        if path.suffix.lower() == ".json":
            data = json.loads(text)
        elif path.suffix.lower() == ".toml":
            data = tomllib.loads(text)
    except Exception:
        data = None
    pairs, accent = [], None
    if isinstance(data, dict):
        palette = dict(data.get("palette") or {})
        pl = data.get("palettes") or {}
        if isinstance(pl, dict) and isinstance(pl.get("list"), dict):
            for v in pl["list"].values():
                if isinstance(v, dict):
                    palette = {**v, **palette}
                    break
        _walk_colors(data, palette, pairs)
        ac = _colors_in(data.get("accent_color"), palette)
        accent = ac[0] if ac else None
    else:  # YAML (no parser in stdlib) or broken file: scan lines
        palette = {}
        for m in re.finditer(r"^\s*([A-Za-z0-9_.\-]+)\s*:\s*[\"']?(#[0-9a-fA-F]{3,6})\b", text, re.M):
            palette.setdefault(m.group(1), m.group(2))
        for m in re.finditer(r"^\s*-?\s*([A-Za-z_]*(?:background|foreground|color)[A-Za-z_]*)\s*:\s*(.+)$",
                             text, re.M | re.I):
            k = m.group(1).lower()
            role = "bg" if "background" in k else "fg" if "foreground" in k else "other"
            pairs += [(role, c) for c in _colors_in(m.group(2), palette)]
    return theme_from_colors("posh:" + name, pairs, accent, str(path))


def posh_dirs():
    home = Path.home()
    ds = [POSH_DIR]
    if os.environ.get("POSH_THEMES_PATH"):
        ds.append(Path(os.environ["POSH_THEMES_PATH"]).expanduser())
    ds += [home / ".cache/oh-my-posh/themes", home / ".poshthemes", home / ".config/oh-my-posh",
           home / ".config/oh-my-posh/themes", home / ".local/share/oh-my-posh/themes",
           home / ".oh-my-posh/themes", Path("/usr/share/oh-my-posh/themes"),
           Path("/opt/homebrew/opt/oh-my-posh/themes"), Path("/usr/local/opt/oh-my-posh/themes")]
    return ds


def load_posh_themes(extra=()):
    files = [Path(p).expanduser() for p in extra]
    active = os.environ.get("POSH_THEME")
    if active and not URL_RE.match(active) and Path(active).expanduser().is_file():
        files.append(Path(active).expanduser())
    for d in posh_dirs():
        if d.is_dir():
            files += sorted(p for p in d.iterdir() if p.is_file() and POSH_FILE_RE.search(p.name))
    themes, seen = [], set()
    for f in files:
        t = parse_posh(f)
        if t and t.name not in seen:
            seen.add(t.name)
            themes.append(t)
    return themes


def fetch_posh_themes():
    url = "https://github.com/JanDeDobbeleer/oh-my-posh/releases/latest/download/themes.zip"
    print(f"Downloading {url} …")
    req = urllib.request.Request(url, headers={"User-Agent": f"{APP}/{VERSION}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
    POSH_DIR.mkdir(parents=True, exist_ok=True)
    n = 0
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for info in z.infolist():
            base = os.path.basename(info.filename)
            if POSH_FILE_RE.search(base):
                (POSH_DIR / base).write_bytes(z.read(info))
                n += 1
    print(f"Installed {n} oh-my-posh themes into {POSH_DIR}")


# ───────────────────────────── tracks / loading ────────────────────────────

class Track:
    __slots__ = ("src", "title", "artist", "album", "duration", "is_url", "probed", "name")

    def __init__(self, src, name=None):
        self.src = src
        self.is_url = bool(URL_RE.match(src))
        self.title = self.artist = self.album = self.duration = None
        self.probed = False
        self.name = name

    def label(self):
        if self.name:
            return self.name
        if self.title:
            return f"{self.artist} — {self.title}" if self.artist else self.title
        return self.src if self.is_url else Path(self.src).stem


def parse_m3u(p):
    tracks, title = [], None
    for line in p.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            title = line.split(",", 1)[1].strip() if "," in line else None
            continue
        if line.startswith("#"):
            continue
        src = line if URL_RE.match(line) else str((p.parent / Path(line).expanduser()).resolve())
        t = Track(src)
        if title:
            if t.is_url:
                t.name = title
            else:
                t.title = title
        tracks.append(t)
        title = None
    return tracks


def parse_pls(p):
    files, titles = {}, {}
    for line in p.read_text(errors="replace").splitlines():
        m = re.match(r"\s*(File|Title)(\d+)\s*=\s*(.*)", line, re.I)
        if m:
            (files if m.group(1).lower() == "file" else titles)[int(m.group(2))] = m.group(3).strip()
    out = []
    for k in sorted(files):
        src = files[k]
        if not URL_RE.match(src):
            src = str((p.parent / src).resolve())
        t = Track(src, titles.get(k) if URL_RE.match(src) else None)
        out.append(t)
    return out


def expand(arg):
    arg = arg.strip()
    if not arg:
        return []
    if URL_RE.match(arg):
        return [Track(arg)]
    p = Path(arg).expanduser()
    if p.is_dir():
        out = []
        for root, dirs, files in os.walk(p):
            dirs.sort(key=str.lower)
            for f in sorted(files, key=str.lower):
                if Path(f).suffix.lower() in AUDIO_EXTS:
                    out.append(Track(os.path.join(root, f)))
        return out
    if p.is_file():
        suf = p.suffix.lower()
        if suf in (".m3u", ".m3u8"):
            return parse_m3u(p)
        if suf == ".pls":
            return parse_pls(p)
        return [Track(str(p.resolve()))]
    return []


def radio_stations():
    if STATIONS_FILE.is_file():
        user = parse_m3u(STATIONS_FILE)
        if user:
            return user
    soma = [("groovesalad", "SomaFM · Groove Salad (ambient/downtempo)"),
            ("dronezone", "SomaFM · Drone Zone (atmospheric)"),
            ("deepspaceone", "SomaFM · Deep Space One (deep ambient)"),
            ("defcon", "SomaFM · DEF CON Radio (hacking music)"),
            ("secretagent", "SomaFM · Secret Agent (spy lounge)"),
            ("lush", "SomaFM · Lush (vocal chill)"),
            ("spacestation", "SomaFM · Space Station Soma (electronica)"),
            ("indiepop", "SomaFM · Indie Pop Rocks!"),
            ("u80s", "SomaFM · Underground 80s"),
            ("thetrip", "SomaFM · The Trip (progressive house)"),
            ("fluid", "SomaFM · Fluid (instrumental hip-hop)"),
            ("bootliquor", "SomaFM · Boot Liquor (americana)"),
            ("sonicuniverse", "SomaFM · Sonic Universe (jazz)"),
            ("beatblender", "SomaFM · Beat Blender (deep house)")]
    tracks = [Track(f"https://ice1.somafm.com/{sid}-128-mp3", name) for sid, name in soma]
    for pool, sid, name in BBC_STATIONS:
        tracks.append(Track(f"http://as-hls-ww-live.akamaized.net/pool_{pool}/live/ww/{sid}/{sid}.isml/"
                            f"{sid}-audio%3d128000.norewind.m3u8", f"BBC · {name}"))
    return tracks


# BBC HLS streams (worldwide edition, 128k AAC). The pool number is per station;
# the BBC changes these occasionally — override via ~/.config/nightjar/stations.m3u.
BBC_STATIONS = [
    ("01505109", "bbc_radio_one", "Radio 1"),
    ("62063831", "bbc_radio_one_dance", "Radio 1 Dance"),
    ("11351741", "bbc_radio_one_anthems", "Radio 1 Anthems"),
    ("92079267", "bbc_1xtra", "1Xtra"),
    ("74208725", "bbc_radio_two", "Radio 2"),
    ("23461179", "bbc_radio_three", "Radio 3"),
    ("30624046", "bbc_radio_three_unwind", "Radio 3 Unwind"),
    ("55057080", "bbc_radio_fourfm", "Radio 4"),
    ("26173715", "bbc_radio_four_extra", "Radio 4 Extra"),
    ("89021708", "bbc_radio_five_live", "Radio 5 Live"),
    ("81827798", "bbc_6music", "6 Music"),
    ("22108647", "bbc_asian_network", "Asian Network"),
    ("87948813", "bbc_world_service", "World Service"),
    ("43322914", "bbc_radio_scotland_fm", "Radio Scotland"),
    ("97517794", "bbc_radio_wales_fm", "Radio Wales"),
    ("24792333", "bbc_radio_cymru", "Radio Cymru"),
    ("31244774", "bbc_radio_ulster", "Radio Ulster"),
    ("98137350", "bbc_london", "Radio London"),
]
BBC_SID_RE = re.compile(r"/live/(?:ww|uk)/(bbc_[a-z0-9_]+)/")


def bbc_now_playing(sid):
    """Current track (music stations) or programme (speech) from the BBC's public RMS API."""
    hdr = {"User-Agent": f"{APP}/{VERSION}"}
    base = "https://rms.api.bbc.co.uk/v2"
    try:
        req = urllib.request.Request(f"{base}/services/{sid}/segments/latest?experience=domestic&offset=0&limit=1",
                                     headers=hdr)
        with urllib.request.urlopen(req, timeout=8) as r:
            for s in json.load(r).get("data", []):
                if s.get("offset", {}).get("now_playing"):
                    t = s.get("titles", {})
                    return " — ".join(p for p in (t.get("primary"), t.get("secondary")) if p) or None
    except Exception:
        pass
    req = urllib.request.Request(f"{base}/broadcasts/poll/{sid}", headers=hdr)
    with urllib.request.urlopen(req, timeout=8) as r:
        data = json.load(r).get("data", [])
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for b in data:
        if b.get("start", "") <= now < b.get("end", "9"):
            t = b.get("titles", {})
            return " · ".join(p for p in (t.get("primary"), t.get("secondary")) if p) or None
    return None


def probe(t):
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                            "format=duration:format_tags=title,artist,album:stream_tags=title,artist,album",
                            "-of", "json", t.src], capture_output=True, timeout=15)
        d = json.loads(r.stdout or b"{}")
        fmt = d.get("format", {})
        tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
        for s in d.get("streams", []):
            for k, v in (s.get("tags") or {}).items():
                tags.setdefault(k.lower(), v)
        dur = fmt.get("duration")
        if dur and dur != "N/A":
            t.duration = float(dur)
        t.title = tags.get("title") or t.title
        t.artist = tags.get("artist") or t.artist
        t.album = tags.get("album") or t.album
    except Exception:
        pass
    finally:
        t.probed = True


class Prober(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.q = deque()
        self.cv = threading.Condition()
        self.start()

    def add(self, tracks, front=False):
        with self.cv:
            for t in tracks:
                if not t.is_url and not t.probed:
                    (self.q.appendleft if front else self.q.append)(t)
            self.cv.notify()

    def run(self):
        while True:
            with self.cv:
                while not self.q:
                    self.cv.wait()
                t = self.q.popleft()
            if not t.probed:
                probe(t)


# ──────────────────────────────── audio ────────────────────────────────────

EQ_FREQS = [31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]
EQ_LABELS = ["31", "62", "125", "250", "500", "1k", "2k", "4k", "8k", "16k"]
EQ_PRESETS = {
    "Flat":        [0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    "Rock":        [5, 4, 3, 1, -1, -1, 1, 3, 4, 5],
    "Pop":         [-1, 1, 3, 4, 3, 0, -1, -1, 1, 2],
    "Jazz":        [3, 2, 1, 2, -1, -1, 0, 1, 2, 3],
    "Classical":   [4, 3, 2, 1, -1, -1, 0, 2, 3, 4],
    "Electronic":  [5, 4, 1, 0, -2, 2, 1, 2, 4, 5],
    "Hip-Hop":     [6, 5, 2, 3, -1, -1, 1, -1, 2, 3],
    "Bass Boost":  [7, 6, 5, 3, 1, 0, 0, 0, 0, 0],
    "Treble Boost": [0, 0, 0, 0, 0, 1, 3, 5, 6, 7],
    "Vocal":       [-2, -3, -2, 1, 3, 4, 3, 1, 0, -1],
    "Acoustic":    [4, 3, 2, 1, 2, 2, 3, 3, 2, 1],
    "Loudness":    [6, 4, 0, 0, -2, 0, -1, -3, 4, 2],
    "Late Night":  [3, 3, 2, 0, -1, 0, 1, 1, -2, -4],
    "Custom":      None,
}


class EQ:
    """Linear-phase FIR equaliser applied with FFT overlap-add."""
    NFFT = 4096
    TAPS = 2049  # BLOCK + TAPS - 1 == NFFT

    def __init__(self):
        self.H = None
        self.tail = np.zeros((self.NFFT - BLOCK, 2), np.float32)

    def set(self, gains):
        g = np.asarray(gains, float)
        if not np.any(g):
            self.H = None
            return
        f = np.fft.rfftfreq(self.NFFT, 1 / RATE)
        db = np.interp(np.log2(np.maximum(f, 1.0)), np.log2(EQ_FREQS), g)
        h = np.fft.irfft(10 ** (db / 20), self.NFFT)
        h = np.roll(h, self.TAPS // 2)[: self.TAPS] * np.hanning(self.TAPS)
        pre = 10 ** (-max(0.0, g.max()) * 0.5 / 20)   # headroom for boosts
        self.H = (np.fft.rfft(h, self.NFFT) * pre).astype(np.complex64)

    def reset(self):
        self.tail[:] = 0

    def process(self, x):
        H = self.H
        if H is None:
            return x
        X = np.fft.rfft(x, self.NFFT, axis=0)
        y = np.fft.irfft(X * H[:, None], self.NFFT, axis=0).astype(np.float32)
        y[: self.NFFT - BLOCK] += self.tail
        self.tail = y[BLOCK:].copy()
        return y[:BLOCK]


class Output:
    CANDIDATES = [
        ["pw-cat", "--playback", "--raw", "--rate", str(RATE), "--channels", "2",
         "--format", "s16", "--latency", "30ms", "--media-role", "Music", "-"],
        ["pacat", "--playback", "--raw", "--format=s16le", f"--rate={RATE}", "--channels=2",
         "--latency-msec=30", f"--client-name={APP}"],
        ["paplay", "--raw", "--format=s16le", f"--rate={RATE}", "--channels=2", f"--client-name={APP}"],
        ["aplay", "-q", "-t", "raw", "-f", "S16_LE", "-r", str(RATE), "-c", "2"],
    ]

    def __init__(self):
        self.proc = None
        self.name = None
        self.start()

    def start(self):
        for c in self.CANDIDATES:
            if shutil.which(c[0]):
                try:
                    self.proc = subprocess.Popen(c, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                                 stderr=subprocess.DEVNULL, bufsize=0, start_new_session=True)
                except OSError:
                    continue
                try:  # shrink the pipe so visuals stay in sync with what you hear
                    fcntl.fcntl(self.proc.stdin.fileno(), 1031, 8192)  # F_SETPIPE_SZ
                except OSError:
                    pass
                self.name = c[0]
                return
        raise RuntimeError("no audio output found (need pw-cat, pacat, paplay or aplay)")

    def write(self, b):
        try:
            self.proc.stdin.write(b)
        except (BrokenPipeError, OSError, ValueError):
            self.close()
            time.sleep(0.3)
            self.start()

    def close(self):
        if self.proc:
            try:
                self.proc.stdin.close()
            except Exception:
                pass
            try:
                self.proc.kill()
                self.proc.wait(timeout=1)
            except Exception:
                pass
            self.proc = None


class Decoder:
    ERR_RE = re.compile(r"(Error|No such file|Invalid data|not found|refused|denied|timed out|failed)", re.I)

    def __init__(self, track, start, on_meta):
        cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-loglevel", "info"]
        if track.src.lower().startswith(("http://", "https://")):
            cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5", "-icy", "1"]
        if start > 0:
            cmd += ["-ss", f"{start:.3f}"]
        cmd += ["-i", track.src, "-vn", "-sn", "-f", "f32le", "-ac", "2", "-ar", str(RATE), "pipe:1"]
        self.error = None
        self.got = 0
        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, start_new_session=True)
        threading.Thread(target=self._stderr, args=(on_meta,), daemon=True).start()

    def _stderr(self, on_meta):
        try:
            for raw in self.proc.stderr:
                line = raw.decode("utf-8", "replace").strip()
                m = re.search(r"StreamTitle\s*:\s*(.*)$", line)
                if m:
                    on_meta("title", m.group(1).strip())
                    continue
                m = re.match(r"icy-name\s*:\s*(.*)$", line)
                if m:
                    on_meta("station", m.group(1).strip())
                    continue
                if self.ERR_RE.search(line):
                    self.error = re.sub(r"^\[[^\]]*\]\s*", "", line)
        except (ValueError, OSError):
            pass

    def read(self, frames):
        try:
            data = self.proc.stdout.read(frames * 8)
        except (ValueError, OSError):
            return None
        n = len(data) // 8 if data else 0
        if n == 0:
            return None
        self.got += n
        return np.frombuffer(data[: n * 8], dtype="<f4").reshape(n, 2)

    def close(self):
        try:
            self.proc.kill()
            self.proc.wait(timeout=1)
        except Exception:
            pass
        for f in (self.proc.stdout, self.proc.stderr):
            try:
                f.close()
            except Exception:
                pass


def icy_now_playing(url):
    """Read one ICY metadata block from a shoutcast/icecast stream → StreamTitle or None."""
    req = urllib.request.Request(url, headers={"Icy-MetaData": "1", "User-Agent": f"{APP}/{VERSION}"})
    with urllib.request.urlopen(req, timeout=8) as r:
        metaint = int(r.headers.get("icy-metaint") or 0)
        if not metaint or metaint > 1 << 20:
            return None
        r.read(metaint)
        n = r.read(1)
        if not n:
            return None
        meta = r.read(n[0] * 16).rstrip(b"\0").decode("utf-8", "replace")
    m = re.search(r"StreamTitle='(.*?)';", meta)
    return m.group(1).strip() if m and m.group(1).strip() else None


class Engine:
    """Owns the decode → EQ → volume → output pipeline on its own thread."""

    def __init__(self, events):
        self.events = events
        self.out = Output()
        self.eq = EQ()
        self.cmds = queue.Queue()
        self.dec = self.track = self.token = None
        self.next_dec = self.next_track = self.next_token = None
        self.paused = False
        self.paused_at = 0.0
        self.volume = 80
        self.muted = False
        self.start = 0.0
        self.frames = 0
        self.ring = np.zeros((RATE, 2), np.float32)
        self.wpos = 0
        self.rlock = threading.Lock()
        self.stream_title = None
        self.station = None
        self.alive = True
        threading.Thread(target=self._run, daemon=True).start()
        threading.Thread(target=self._icy_loop, daemon=True).start()

    def _icy_loop(self):
        last_track, next_poll = None, 0.0
        while self.alive:
            tr = self.track
            if tr is not None and tr.is_url and tr.src.lower().startswith(("http://", "https://")):
                if tr is not last_track:
                    last_track, next_poll = tr, time.monotonic() + 1.5
                if time.monotonic() >= next_poll and not self.paused:
                    bbc = BBC_SID_RE.search(tr.src)
                    next_poll = time.monotonic() + (30 if bbc else 15)
                    try:
                        title = bbc_now_playing(bbc.group(1)) if bbc else icy_now_playing(tr.src)
                        if title and self.track is tr:
                            self.stream_title = title
                    except Exception:
                        pass
            else:
                last_track = None
            time.sleep(0.5)

    # ── public API (called from UI thread) ──
    def play(self, track, token, start=0.0):
        self.paused = False
        self.cmds.put(("play", track, token, start))

    def set_next(self, track, token):
        self.cmds.put(("next", track, token))

    def seek(self, t):
        self.cmds.put(("seek", t))

    def stop(self):
        self.cmds.put(("stop",))

    def toggle(self):
        if self.track is None:
            return
        if self.paused:
            self.paused = False
            if self.track.is_url and time.time() - self.paused_at > 5:
                self.cmds.put(("seek", 0.0))  # reconnect a stale live stream
        else:
            self.paused = True
            self.paused_at = time.time()

    @property
    def position(self):
        if self.track is None:
            return 0.0
        return self.start + max(0, self.frames - LAT_FRAMES) / RATE

    def samples(self, n):
        with self.rlock:
            end = self.wpos - LAT_FRAMES
            if end < n:
                return np.zeros((n, 2), np.float32)
            return self.ring[np.arange(end - n, end) % RATE]

    def shutdown(self):
        self.alive = False
        for d in (self.dec, self.next_dec):
            if d:
                d.close()
        self.out.close()

    # ── worker ──
    def _meta(self, kind, value):
        if kind == "title":
            self.stream_title = value or None
        else:
            self.station = value or None

    def _open(self, start):
        self.dec = Decoder(self.track, start, self._meta)
        self.start = start
        self.frames = 0
        self.eq.reset()

    def _kill_all(self):
        for d in (self.dec, self.next_dec):
            if d:
                d.close()
        self.dec = self.next_dec = self.next_track = self.next_token = None

    def _handle(self, c):
        op = c[0]
        if op == "play":
            _, tr, tok, st = c
            self._kill_all()
            self.track, self.token = tr, tok
            self.stream_title = self.station = None
            self._open(st)
        elif op == "seek":
            if self.track is None:
                return
            st = max(0.0, c[1])
            if self.track.duration:
                st = min(st, max(0.0, self.track.duration - 0.5))
            if self.dec:
                self.dec.close()
            self._open(st)
        elif op == "next":
            _, tr, tok = c
            if tr is self.next_track and tok == self.next_token:
                return
            if self.next_dec:
                self.next_dec.close()
                self.next_dec = None
            self.next_track, self.next_token = tr, tok
        elif op == "stop":
            self._kill_all()
            self.track = self.token = None

    def _eof(self):
        d = self.dec
        got, err = d.got, d.error
        d.close()
        self.dec = None
        if got == 0:
            self.events.put(("error", self.track, err or "could not decode"))
        if self.next_track is not None:
            self.track, self.token = self.next_track, self.next_token
            self.dec = self.next_dec or Decoder(self.track, 0, self._meta)
            self.next_dec = self.next_track = self.next_token = None
            self.start, self.frames = 0.0, 0
            self.stream_title = self.station = None
            self.events.put(("advanced", self.token))
        else:
            self.events.put(("ended", self.token))
            self.track = self.token = None

    def _run(self):
        while self.alive:
            try:
                while True:
                    self._handle(self.cmds.get_nowait())
            except queue.Empty:
                pass
            if self.dec is None or self.paused:
                try:
                    self._handle(self.cmds.get(timeout=0.02))
                except queue.Empty:
                    pass
                continue
            x = self.dec.read(BLOCK)
            if x is None:
                self._eof()
                continue
            if self.dec.got == len(x):
                self.events.put(("started", self.token))
            n = len(x)
            if n < BLOCK:
                x = np.vstack([x, np.zeros((BLOCK - n, 2), np.float32)])
            y = self.eq.process(x)[:n]
            for i in range(0, n, SUB):
                chunk = y[i:i + SUB]
                m = len(chunk)
                with self.rlock:
                    self.ring[np.arange(self.wpos, self.wpos + m) % RATE] = chunk
                    self.wpos += m
                g = 0.0 if self.muted else (self.volume / 100) ** 2
                pcm = (np.clip(chunk * g, -1, 1) * 32767).astype("<i2").tobytes()
                self.out.write(pcm)
                self.frames += m
                if self.paused or not self.cmds.empty():
                    break
            tr, nt = self.track, self.next_track
            if (nt is not None and self.next_dec is None and tr is not None and tr.duration
                    and not tr.is_url and not nt.is_url and self.position > tr.duration - 4):
                self.next_dec = Decoder(nt, 0, self._meta)  # preload for gapless


# ─────────────────────────────── terminal ──────────────────────────────────

def char_width(c):
    if c < "̀":
        return 1
    if unicodedata.combining(c):
        return 0
    return 2 if unicodedata.east_asian_width(c) in "WF" else 1


def text_width(s):
    return sum(char_width(c) for c in s)


def trunc(s, n):
    if n <= 0:
        return ""
    if text_width(s) <= n:
        return s
    out, w = [], 0
    for c in s:
        cw = char_width(c)
        if w + cw > n - 1:
            break
        out.append(c)
        w += cw
    return "".join(out) + "…"


class Screen:
    def __init__(self, truecolor):
        self.tc = truecolor
        self.cache = {}
        self.resize()

    def resize(self):
        sz = shutil.get_terminal_size((80, 24))
        self.w, self.h = max(30, sz.columns), max(10, sz.lines)
        self.prev = [None] * self.h
        os.write(1, b"\x1b[2J")

    def clear(self, fg, bg):
        w, h = self.w, self.h
        self.ch = [[" "] * w for _ in range(h)]
        self.fg = [[fg] * w for _ in range(h)]
        self.bg = [[bg] * w for _ in range(h)]
        self.bd = [[False] * w for _ in range(h)]

    def put(self, x, y, text, fg=None, bg=False, bold=False):
        if y < 0 or y >= self.h:
            return x + text_width(text)
        rc, rf, rb, rd, w = self.ch[y], self.fg[y], self.bg[y], self.bd[y], self.w
        for c in text:
            cw = char_width(c)
            if cw == 0:
                continue
            if x >= w:
                break
            if x >= 0:
                if cw == 2 and x + 1 >= w:
                    break
                rc[x], rf[x], rd[x] = c, fg, bold
                if bg is not False:
                    rb[x] = bg
                if cw == 2:
                    rc[x + 1], rf[x + 1] = "", fg
                    if bg is not False:
                        rb[x + 1] = bg
            x += cw
        return x

    def fill(self, x, y, w, h, bg):
        for yy in range(max(0, y), min(self.h, y + h)):
            rc, rb = self.ch[yy], self.bg[yy]
            for xx in range(max(0, x), min(self.w, x + w)):
                rc[xx] = " "
                rb[xx] = bg

    def _code(self, col, fg):
        key = (col, fg)
        c = self.cache.get(key)
        if c is None:
            if col is None:
                c = "39" if fg else "49"
            else:
                r, g, b = hex_rgb(col)
                base = "38" if fg else "48"
                c = f"{base};2;{r};{g};{b}" if self.tc else f"{base};5;{rgb_to_256(r, g, b)}"
            self.cache[key] = c
        return c

    def flush(self):
        out = []
        for y in range(self.h):
            parts, state = [], None
            rc, rf, rb, rd = self.ch[y], self.fg[y], self.bg[y], self.bd[y]
            for x in range(self.w):
                c = rc[x]
                if c == "":
                    continue
                st = (rf[x], rb[x], rd[x])
                if st != state:
                    parts.append("\x1b[0;" + ("1;" if st[2] else "") + self._code(st[0], True)
                                 + ";" + self._code(st[1], False) + "m")
                    state = st
                parts.append(c)
            row = "".join(parts)
            if row != self.prev[y]:
                out.append(f"\x1b[{y + 1};1H{row}\x1b[0m")
                self.prev[y] = row
        if out:
            data = "".join(out).encode()
            while data:
                n = os.write(1, data)
                data = data[n:]


KEYSEQ = {"[A": "up", "[B": "down", "[C": "right", "[D": "left", "OA": "up", "OB": "down",
          "OC": "right", "OD": "left", "[H": "home", "[F": "end", "OH": "home", "OF": "end",
          "[1~": "home", "[4~": "end", "[7~": "home", "[8~": "end", "[5~": "pgup", "[6~": "pgdn",
          "[3~": "delete", "[1;2C": "S-right", "[1;2D": "S-left", "[1;2A": "S-up", "[1;2B": "S-down",
          "[Z": "S-tab", "[2~": "insert"}


def parse_keys(s):
    out, i = [], 0
    while i < len(s):
        c = s[i]
        if c == "\x1b":
            if i + 1 < len(s) and s[i + 1] in "[O":
                j = i + 2
                while j < len(s) and not (s[j].isalpha() or s[j] == "~"):
                    j += 1
                out.append(KEYSEQ.get(s[i + 1:j + 1], "unknown"))
                i = j + 1
                continue
            out.append("esc")
        elif c in "\r\n":
            out.append("enter")
        elif c in "\x7f\x08":
            out.append("backspace")
        elif c == "\t":
            out.append("tab")
        elif c == "\x03":
            out.append("ctrl-c")
        elif c == "\x0e":
            out.append("down")
        elif c == "\x10":
            out.append("up")
        elif c == "\x15":
            out.append("ctrl-u")
        elif c >= " ":
            out.append(c)
        i += 1
    return out


def braille(dots):
    """bool array (h*4, w*2) → int array (h, w) of braille offsets."""
    H, W = dots.shape
    d = dots.reshape(H // 4, 4, W // 2, 2)
    wts = np.array([[1, 8], [2, 16], [4, 32], [64, 128]])
    return (d * wts[None, :, None, :]).sum(axis=(1, 3))


def fmt_time(s):
    if s is None:
        return "--:--"
    s = int(max(0, s))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60:02d}:{s % 60:02d}"


def load_config():
    try:
        return json.loads(CONF_FILE.read_text())
    except Exception:
        return {}


# ───────────────────────────────── app ─────────────────────────────────────

class App:
    VIZ_MODES = ["bars", "mirror", "led", "dots", "scope", "fire", "vu"]

    def __init__(self, library, themes, cfg, theme_name=None, viz=None):
        self.events = queue.Queue()
        self.engine = Engine(self.events)
        self.prober = Prober()
        self.lists = {"Library": library, "Radio": radio_stations()}
        self.tab = "Library" if library or cfg.get("tab") != "Radio" else "Radio"
        self.cursor = {"Library": 0, "Radio": 0}
        self.scroll = {"Library": 0, "Radio": 0}
        self.play_tab = None
        self.queue = []
        self.history = []
        self.shuf_next = None
        self.shuffle = bool(cfg.get("shuffle", False))
        self.repeat = cfg.get("repeat", "all") if cfg.get("repeat") in ("off", "all", "one") else "all"
        self.engine.volume = int(cfg.get("volume", 80))
        self.themes = themes
        want = theme_name or cfg.get("theme") or "catppuccin-mocha"
        self.theme_idx = next((i for i, t in enumerate(themes) if t.name == want), 0)
        self.transparent = bool(cfg.get("transparent", False))
        self.viz = viz if viz in self.VIZ_MODES else cfg.get("viz", "bars")
        if self.viz not in self.VIZ_MODES:
            self.viz = "bars"
        self.full_viz = False
        self.eq_custom = list(cfg.get("eq_custom") or [0] * 10)[:10]
        self.eq_name = cfg.get("eq", "Flat") if cfg.get("eq") in EQ_PRESETS else "Flat"
        self.eq_band = 0
        self.apply_eq()
        self.msg, self.msg_until = "", 0.0
        self.overlay = None
        self.prompt = None
        self.tp = None
        self.fails = 0
        self.tok_seq = 0
        self.tokens = {}
        self.pending = None
        self.list_rows = 10
        self.running = True
        self.resized = False
        self.sm = {}
        self.fire = None
        self.vu = np.zeros(2)
        self.vu_pk = np.zeros(2)
        self.win = np.hanning(2048).astype(np.float32)
        self.freqs = np.fft.rfftfreq(2048, 1 / RATE)
        self.edges = {}
        self.S = Screen(os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit"))
        self.prober.add(library)
        if not themes[self.theme_idx].name.startswith("posh:") and not any(
                t.name.startswith("posh:") for t in themes):
            self.flash("Tip: run `nightjar --fetch-posh-themes` to add the oh-my-posh theme pack", 6)

    # ── helpers ──
    @property
    def th(self):
        return self.themes[self.theme_idx]

    @property
    def BG(self):
        return None if self.transparent else self.th.bg

    def flash(self, m, secs=3.0):
        self.msg, self.msg_until = m, time.monotonic() + secs

    def save_config(self):
        cfg = {"volume": self.engine.volume, "theme": self.th.name, "viz": self.viz, "eq": self.eq_name,
               "eq_custom": self.eq_custom, "shuffle": self.shuffle, "repeat": self.repeat,
               "transparent": self.transparent, "tab": self.tab}
        try:
            CONF_DIR.mkdir(parents=True, exist_ok=True)
            CONF_FILE.write_text(json.dumps(cfg, indent=2))
        except OSError:
            pass

    def gains(self):
        return self.eq_custom if self.eq_name == "Custom" else EQ_PRESETS[self.eq_name]

    def apply_eq(self):
        self.engine.eq.set(self.gains())

    @staticmethod
    def index_of(lst, tr):
        return next((i for i, t in enumerate(lst) if t is tr), None)

    def cur_idx(self):
        if self.play_tab is None or self.engine.track is None:
            return None
        return self.index_of(self.lists[self.play_tab], self.engine.track)

    def _tok(self, tab, tr):
        self.tok_seq += 1
        self.tokens[self.tok_seq] = (tab, tr)
        return self.tok_seq

    # ── playback logic ──
    def play_at(self, tab, idx, start=0.0):
        lst = self.lists[tab]
        if not lst or idx is None or not 0 <= idx < len(lst):
            return
        tr = lst[idx]
        if self.engine.track is not None and self.engine.track is not tr:
            self.history.append(self.engine.track)
            self.history = self.history[-200:]
        self.play_tab = tab
        self.shuf_next = None
        self.engine.play(tr, self._tok(tab, tr), start)
        self.engine.track = tr  # optimistic, so the UI updates immediately
        self.prober.add([tr], front=True)
        self.schedule_next(after=tr)

    def peek_next(self, after=None, manual=False):
        tab = self.play_tab
        if tab is None or tab == "Radio":
            return None
        lst = self.lists[tab]
        if not lst:
            return None
        q = [t for t in self.queue if self.index_of(lst, t) is not None]
        if q:
            return q[0]
        cur = self.index_of(lst, after if after is not None else self.engine.track)
        if self.repeat == "one" and cur is not None and not manual:
            return lst[cur]
        if self.shuffle:
            if self.shuf_next is None or self.index_of(lst, self.shuf_next) is None:
                recent = set(id(t) for t in self.history[-max(1, len(lst) // 2):])
                choices = [t for i, t in enumerate(lst) if i != cur and id(t) not in recent] or \
                          [t for i, t in enumerate(lst) if i != cur] or lst
                self.shuf_next = random.choice(choices)
            return self.shuf_next
        nxt = 0 if cur is None else cur + 1
        if nxt >= len(lst):
            if self.repeat == "off":
                return None
            nxt = 0
        return lst[nxt]

    def schedule_next(self, after=None):
        tr = self.peek_next(after)
        self.pending = tr
        self.engine.set_next(tr, self._tok(self.play_tab, tr) if tr else None)

    def next_track(self):
        if self.play_tab is None:
            return
        if self.play_tab == "Radio":
            lst = self.lists["Radio"]
            i = self.cur_idx()
            return self.play_at("Radio", 0 if i is None else (i + 1) % len(lst))
        tr = self.peek_next(manual=True)
        if tr is None:
            lst = self.lists[self.play_tab]
            tr = lst[0] if lst else None
        if tr is None:
            return
        if self.queue and self.queue[0] is tr:
            self.queue.pop(0)
        self.play_at(self.play_tab, self.index_of(self.lists[self.play_tab], tr))

    def prev_track(self):
        if self.play_tab is None:
            return
        tr = self.engine.track
        if tr and tr.duration and self.engine.position > 3:
            return self.engine.seek(0)
        lst = self.lists[self.play_tab]
        while self.history:
            h = self.history.pop()
            i = self.index_of(lst, h)
            if i is not None:
                self.play_at(self.play_tab, i)
                self.history.pop() if self.history and self.history[-1] is tr else None
                return
        i = self.cur_idx()
        if i is not None and lst:
            self.play_at(self.play_tab, (i - 1) % len(lst))

    def handle_events(self):
        while True:
            try:
                ev = self.events.get_nowait()
            except queue.Empty:
                return
            kind = ev[0]
            if kind == "started":
                self.fails = 0
            elif kind == "error":
                _, tr, err = ev
                self.fails += 1
                self.flash(f"Can't play {trunc(tr.label(), 40)}: {trunc(err, 60)}", 5)
                lst = self.lists.get(self.play_tab) or []
                if self.fails >= max(1, len(lst)):
                    self.engine.stop()
                    self.engine.track = None
            elif kind == "advanced":
                tab, tr = self.tokens.get(ev[1], (None, None))
                if tr is None:
                    continue
                prev = self.history[-1] if self.history else None
                if prev is not tr:
                    pass
                if self.queue and self.queue[0] is tr:
                    self.queue.pop(0)
                self.shuf_next = None
                self.prober.add([tr], front=True)
                self.schedule_next(after=tr)
            elif kind == "ended":
                if self.play_tab == "Radio":
                    self.flash("Stream ended")
                elif self.fails == 0:
                    self.flash("End of playlist")

    # ── drawing ──
    def draw(self):
        S, th = self.S, self.th
        S.clear(th.fg, self.BG)
        w, h = S.w, S.h
        self.draw_header()
        if self.full_viz:
            self.draw_now(2, compact=True)
            self.draw_viz(2, 5, w - 4, h - 7)
        else:
            self.draw_now(2)
            top = 7
            avail = h - top - 1
            if avail >= 14:
                vh = max(5, min(18, avail * 2 // 5))
                self.draw_viz(2, top, w - 4, vh)
                top += vh + 1
            self.draw_list(top, h - 1 - top)
        self.draw_footer()
        if self.overlay == "help":
            self.draw_help()
        elif self.overlay == "themes":
            self.draw_theme_picker()
        elif self.overlay == "eq":
            self.draw_eq()
        S.flush()

    def draw_header(self):
        S, th, w = self.S, self.th, self.S.w
        S.fill(0, 0, w, 1, th.sel_bg)
        x = S.put(0, 0, f" ◉ {APP} ", th.bg, th.accent, True)
        x += 1
        for tab in ("Library", "Radio"):
            n = len(self.lists[tab])
            active = tab == self.tab
            x = S.put(x, 0, f" {tab} {n} ", th.bg if active else th.dim,
                      th.accent2 if active else th.sel_bg, active) + 1
        right = f" {th.name} "
        S.put(w - text_width(right), 0, right, th.dim, th.sel_bg)

    def draw_now(self, y, compact=False):
        S, th, e, w = self.S, self.th, self.engine, self.S.w
        tr = e.track
        if tr is None:
            S.put(2, y, "♫", th.accent, bold=True)
            S.put(4, y, "Nothing playing", th.dim)
            if not compact:
                S.put(4, y + 1, "Select a track and press Enter · Tab switches to Radio · ? for help", th.dim)
        else:
            if tr.is_url:
                title = tr.name or e.station or tr.src
                sub = e.stream_title or (e.station if tr.name else "") or "live stream"
            else:
                title = tr.title or Path(tr.src).stem
                sub = " · ".join(p for p in (tr.artist, tr.album) if p) or str(Path(tr.src).parent)
            S.put(2, y, "♫", th.accent, bold=True)
            if compact:
                S.put(4, y, trunc(f"{title}  —  {sub}", w - 6), th.fg, bold=True)
            else:
                S.put(4, y, trunc(title, w - 6), th.fg, bold=True)
                S.put(4, y + 1, trunc(sub, w - 6), th.accent2)
        py = y + (1 if compact else 2)
        state = "▶" if tr and not e.paused else "❚" if tr else "■"
        pos = e.position
        dur = tr.duration if tr else None
        left = f"{state} {fmt_time(pos if tr else None)} "
        right = f" {fmt_time(dur)}" if dur else ("  ● LIVE" if tr and tr.is_url else "")
        x = S.put(2, py, left, th.accent if tr and not e.paused else th.dim, bold=True)
        bw = w - 2 - x - text_width(right)
        if bw > 3:
            if dur:
                frac = max(0.0, min(1.0, pos / dur))
                k = int(frac * (bw - 1))
                S.put(x, py, "━" * k, th.accent)
                S.put(x + k, py, "●", th.accent2)
                S.put(x + k + 1, py, "─" * (bw - k - 1), th.unlit)
            elif tr and tr.is_url and not e.paused:
                k = int(time.monotonic() * 12) % bw
                S.put(x, py, "┄" * bw, th.unlit)
                S.put(x + k, py, "◆", th.accent)
            else:
                S.put(x, py, "─" * bw, th.unlit)
        S.put(x + max(0, bw), py, right, th.bars[-1] if right.strip().endswith("LIVE") else th.dim,
              bold=right.strip().endswith("LIVE"))
        if compact:
            return
        sy = py + 1
        x = S.put(2, sy, "VOL ", th.dim)
        v = e.volume
        for i in range(10):
            on = v >= (i + 1) * 10 - 5
            x = S.put(x, sy, "▮" if on else "▯", th.grad[int(i / 9 * 31)] if on and not e.muted else th.unlit)
        x = S.put(x, sy, " MUTE" if e.muted else f" {v:>3}%", th.bars[-1] if e.muted else th.fg)
        x = S.put(x, sy, "   EQ ", th.dim)
        x = S.put(x, sy, f"[{self.eq_name}]", th.accent2)
        x = S.put(x, sy, "   VIZ ", th.dim)
        x = S.put(x, sy, self.viz, th.fg)
        x = S.put(x, sy, "   ⇄ ", th.dim)
        x = S.put(x, sy, "shuffle" if self.shuffle else "in order", th.accent if self.shuffle else th.fg)
        x = S.put(x, sy, "   ↻ ", th.dim)
        x = S.put(x, sy, {"off": "no repeat", "all": "repeat all", "one": "repeat one"}[self.repeat],
                  th.accent if self.repeat != "off" else th.fg)
        if self.queue:
            x = S.put(x, sy, f"   +{len(self.queue)} queued", th.accent2)

    def draw_list(self, y, height):
        S, th, w = self.S, self.th, self.S.w
        if height < 3:
            return
        lst = self.lists[self.tab]
        x0, bw = 1, w - 2
        title = f" {self.tab} · {len(lst)} "
        S.put(x0, y, "╭─", th.unlit)
        tx = S.put(x0 + 2, y, title, th.accent, bold=True)
        S.put(tx, y, "─" * (x0 + bw - 1 - tx), th.unlit)
        S.put(x0 + bw - 1, y, "╮", th.unlit)
        for yy in range(y + 1, y + height - 1):
            S.put(x0, yy, "│", th.unlit)
            S.put(x0 + bw - 1, yy, "│", th.unlit)
        S.put(x0, y + height - 1, "╰" + "─" * (bw - 2) + "╯", th.unlit)
        rows = height - 2
        self.list_rows = rows
        if not lst:
            msg = ("No tracks yet. Press a to add a file, folder or URL — or start with: nightjar ~/Music"
                   if self.tab == "Library" else f"No stations. Add some with a, or create {STATIONS_FILE}")
            S.put(x0 + 3, y + 1 + rows // 2, trunc(msg, bw - 6), th.dim)
            return
        cur = self.cursor[self.tab] = max(0, min(self.cursor[self.tab], len(lst) - 1))
        sc = self.scroll[self.tab]
        if cur < sc:
            sc = cur
        if cur >= sc + rows:
            sc = cur - rows + 1
        sc = max(0, min(sc, max(0, len(lst) - rows)))
        self.scroll[self.tab] = sc
        nd = len(str(len(lst)))
        playing = self.engine.track
        inner = bw - 2
        for i in range(rows):
            idx = sc + i
            if idx >= len(lst):
                break
            t = lst[idx]
            yy = y + 1 + i
            sel = idx == cur
            bgc = th.sel_bg if sel else False
            if sel:
                S.fill(x0 + 1, yy, inner, 1, th.sel_bg)
            is_play = t is playing and self.tab == self.play_tab
            qpos = next((k for k, q in enumerate(self.queue) if q is t), None)
            if is_play:
                mark, mcol = ("▶" if not self.engine.paused else "❚"), th.accent
            elif qpos is not None:
                mark, mcol = str(qpos + 1) if qpos < 9 else "+", th.accent2
            else:
                mark, mcol = " ", th.dim
            x = S.put(x0 + 2, yy, mark, mcol, bgc, True)
            x = S.put(x + 1, yy, f"{idx + 1:>{nd}} ", th.dim, bgc)
            dur = "live" if t.is_url else fmt_time(t.duration) if t.duration else ""
            avail = x0 + bw - 3 - x - len(dur) - 1
            S.put(x, yy, trunc(t.label(), avail), th.accent if is_play else th.fg, bgc, is_play or sel)
            S.put(x0 + bw - 2 - len(dur), yy, dur, th.dim, bgc)
        if len(lst) > rows:
            th_h = max(1, rows * rows // len(lst))
            th_y = int(sc / max(1, len(lst) - rows) * (rows - th_h))
            for k in range(th_h):
                S.put(x0 + bw - 1, y + 1 + th_y + k, "┃", th.accent)

    def draw_footer(self):
        S, th, w, h = self.S, self.th, self.S.w, self.S.h
        y = h - 1
        S.fill(0, y, w, 1, th.sel_bg)
        if self.prompt:
            p = self.prompt
            x = S.put(1, y, p["label"] + ": ", th.accent2, th.sel_bg, True)
            room = w - x - 2
            txt = p["text"][-room:] if room > 0 else ""
            x = S.put(x, y, txt, th.fg, th.sel_bg)
            S.put(x, y, "▏", th.accent, th.sel_bg)
            return
        if self.msg and time.monotonic() < self.msg_until:
            S.put(1, y, trunc(self.msg, w - 2), th.fg, th.sel_bg)
            return
        hints = [("space", "play"), ("n/p", "track"), ("←→", "seek"), ("+-", "vol"), ("e/E", "eq"),
                 ("v/V", "viz"), ("t/T", "theme"), ("tab", "radio"), ("/", "find"), ("a", "add"),
                 ("?", "help"), ("q", "quit")]
        x = 1
        for k, d in hints:
            need = len(k) + len(d) + 3
            if x + need > w:
                break
            x = S.put(x, y, k, th.accent, th.sel_bg, True)
            x = S.put(x + 1, y, d, th.dim, th.sel_bg) + 2

    def box(self, w, h, title):
        S, th = self.S, self.th
        w, h = min(w, S.w - 2), min(h, S.h - 2)
        x, y = (S.w - w) // 2, (S.h - h) // 2
        S.fill(x, y, w, h, th.panel)
        S.put(x, y, "╭" + "─" * (w - 2) + "╮", th.accent, th.panel)
        for yy in range(y + 1, y + h - 1):
            S.put(x, yy, "│", th.accent, th.panel)
            S.put(x + w - 1, yy, "│", th.accent, th.panel)
        S.put(x, y + h - 1, "╰" + "─" * (w - 2) + "╯", th.accent, th.panel)
        S.put(x + 2, y, f" {title} ", th.accent, th.panel, True)
        return x, y, w, h

    def draw_help(self):
        S, th = self.S, self.th
        keys = [("space", "play / pause"), ("enter", "play selected"), ("n  >", "next track"),
                ("p  <", "previous track"), ("← →", "seek 5s (shift: 30s)"), ("+ -  m", "volume / mute"),
                ("j k ↑ ↓", "move"), ("g G pgup pgdn", "jump / page"), ("tab", "Library ⇄ Radio"),
                ("/", "find in list"), ("a", "add file, folder, URL"), ("d", "remove from list"),
                ("A", "queue up next"), ("w", "save list as .m3u"), ("s", "shuffle"),
                ("r", "repeat off/all/one"), ("e", "next EQ preset"), ("E", "EQ editor"),
                ("v", "next visualizer"), ("V", "fullscreen visualizer"), ("t", "next theme"),
                ("T", "theme picker"), ("B", "transparent background"), ("q", "quit")]
        half = (len(keys) + 1) // 2
        x, y, bw, bh = self.box(76, half + 6, "keys")
        for i, (k, d) in enumerate(keys):
            col, row = divmod(i, half)
            cx = x + 3 + col * 37
            S.put(cx, y + 2 + row, f"{k:>14}", th.accent, th.panel, True)
            S.put(cx + 15, y + 2 + row, trunc(d, 21), th.fg, th.panel)
        S.put(x + 3, y + bh - 2, trunc(f"themes: {len(self.themes)} loaded · posh dir: {POSH_DIR}", bw - 6),
              th.dim, th.panel)

    def theme_matches(self):
        f = self.tp["filter"].lower()
        return [i for i, t in enumerate(self.themes) if f in t.name.lower()]

    def draw_theme_picker(self):
        S, th = self.S, self.th
        m = self.theme_matches()
        nposh = sum(t.name.startswith("posh:") for t in self.themes)
        x, y, bw, bh = self.box(64, max(12, min(len(m) + 7, S.h - 4)), f"themes · {len(self.themes)}")
        S.put(x + 2, y + 1, "filter: ", th.dim, th.panel)
        fx = S.put(x + 10, y + 1, self.tp["filter"], th.fg, th.panel)
        S.put(fx, y + 1, "▏", th.accent, th.panel)
        rows = bh - 5
        sel = self.tp["sel"] = max(0, min(self.tp["sel"], len(m) - 1))
        sc = max(0, min(sel - rows // 2, len(m) - rows))
        for r in range(rows):
            k = sc + r
            if k >= len(m):
                break
            t = self.themes[m[k]]
            yy = y + 2 + r
            on = k == sel
            bgc = th.sel_bg if on else th.panel
            S.fill(x + 1, yy, bw - 2, 1, bgc)
            S.put(x + 2, yy, "›" if on else " ", th.accent, bgc, True)
            S.put(x + 4, yy, trunc(t.name, bw - 20), t.accent if on else th.fg, bgc, on)
            sx = x + bw - 13
            for c in (t.bg, t.accent, t.accent2, *t.bars):
                sx = S.put(sx, yy, "█", c, bgc)
                sx += 0
            S.put(sx, yy, "█", t.fg, bgc)
        if not m:
            S.put(x + 4, y + 3, "no match", th.dim, th.panel)
        foot = ("type to filter · ↑↓ choose · enter apply · esc cancel" if nposh else
                "no oh-my-posh themes found — run: nightjar --fetch-posh-themes")
        S.put(x + 2, y + bh - 2, trunc(foot, bw - 4), th.dim, th.panel)
        if m:
            src = self.themes[m[sel]].source
            S.put(x + 2, y + bh - 3, trunc(src.replace(str(Path.home()), "~"), bw - 4), th.unlit, th.panel)

    def draw_eq(self):
        S, th = self.S, self.th
        x, y, bw, bh = self.box(70, 24, f"equalizer · {self.eq_name}")
        g = self.gains()
        sh = bh - 7
        if sh < 5:
            return
        top = y + 2

        def row(db):
            return top + int(round((12 - db) / 24 * (sh - 1)))
        sp = max(4, (bw - 10) // 10)
        ox = x + 7
        zr = row(0)
        S.put(x + 2, row(12), "+12", th.dim, th.panel)
        S.put(x + 2, zr, "  0", th.dim, th.panel)
        S.put(x + 2, row(-12), "-12", th.dim, th.panel)
        S.put(ox, zr, "┄" * (sp * 10), th.unlit, th.panel)
        for i in range(10):
            cx = ox + i * sp + sp // 2
            on = i == self.eq_band
            for r in range(sh):
                S.put(cx, top + r, "│", th.unlit, th.panel)
            vr = row(g[i])
            lo, hi = sorted((vr, zr))
            for r in range(lo, hi + 1):
                frac = abs(row(0) - r) / max(1, (sh - 1) / 2)
                S.put(cx, r, "█", th.grad[int(min(1, frac) * 31)], th.panel)
            S.put(cx - 1, vr, "◀◆▶" if on else "━◆━", th.accent2 if on else th.accent, th.panel, True)
            lab = EQ_LABELS[i]
            S.put(cx - len(lab) // 2, top + sh, lab, th.bg if on else th.dim, th.accent2 if on else th.panel, on)
            val = f"{g[i]:+d}"
            S.put(cx - len(val) // 2, top + sh + 1, val, th.fg if on else th.dim, th.panel)
        S.put(x + 2, y + bh - 2, trunc("←→ band · ↑↓ gain · 0 zero · p preset · R reset all · esc close", bw - 4),
              th.dim, th.panel)

    # ── visualizers ──
    def spectrum(self, s, nb):
        mag = np.abs(np.fft.rfft(s.mean(axis=1) * self.win))
        if nb not in self.edges:
            e = np.geomspace(35, 16000, nb + 1)
            idx = np.searchsorted(self.freqs, e)
            for i in range(1, len(idx)):
                idx[i] = max(idx[i], idx[i - 1] + 1)
            idx = np.minimum(idx, len(mag) - 1)
            fc = np.sqrt(e[:-1] * e[1:])
            self.edges[nb] = (idx, 3.0 * np.log2(fc / 1000))
        idx, tilt = self.edges[nb]
        vals = np.maximum.reduceat(mag, idx[:-1])[:nb]
        if len(vals) < nb:
            vals = np.pad(vals, (0, nb - len(vals)))
        db = 20 * np.log10(vals / 512 + 1e-9) + tilt
        lv = np.clip((db + 62) / 58, 0, 1)
        st, pk = self.sm.get(nb, (np.zeros(nb), np.zeros(nb)))
        st = np.where(lv > st, st + (lv - st) * 0.6, np.maximum(lv, st - 0.04))
        pk = np.maximum(st, pk - 0.012)
        self.sm[nb] = (st, pk)
        return st, pk

    def draw_viz(self, x, y, w, h):
        if w < 8 or h < 3:
            return
        e = self.engine
        s = e.samples(2048) if e.track is not None and not e.paused else np.zeros((2048, 2), np.float32)
        getattr(self, "viz_" + self.viz)(x, y, w, h, s)

    def gcol(self, frac):
        return self.th.grad[int(max(0.0, min(1.0, frac)) * 31)]

    def viz_bars(self, x, y, w, h, s):
        S, th = self.S, self.th
        bwid = 2 if w >= 60 else 1
        nb = max(4, (w + 1) // (bwid + 1))
        lv, pk = self.spectrum(s, nb)
        ox = x + (w - (nb * (bwid + 1) - 1)) // 2
        eighths = " ▁▂▃▄▅▆▇█"
        for i in range(nb):
            cx = ox + i * (bwid + 1)
            hh = lv[i] * h * 8
            full, rem = int(hh // 8), int(hh % 8)
            for r in range(min(h, full + 1)):
                ch = "█" if r < full else eighths[rem]
                if ch != " ":
                    S.put(cx, y + h - 1 - r, ch * bwid, self.gcol(r / max(1, h - 1)))
            pr = min(h - 1, int(pk[i] * h))
            if pk[i] > 0.02 and pr >= full:
                S.put(cx, y + h - 1 - pr, "▔" * bwid, th.fg)

    def viz_mirror(self, x, y, w, h, s):
        S, th = self.S, self.th
        nb = max(4, (w + 1) // 2)
        lv, _ = self.spectrum(s, nb)
        top = max(1, h * 3 // 5)
        bot = h - top
        eighths = " ▁▂▃▄▅▆▇█"
        ox = x + (w - (nb * 2 - 1)) // 2
        for i in range(nb):
            cx = ox + i * 2
            hh = lv[i] * top * 8
            full, rem = int(hh // 8), int(hh % 8)
            for r in range(min(top, full + 1)):
                ch = "█" if r < full else eighths[rem]
                if ch != " ":
                    S.put(cx, y + top - 1 - r, ch, self.gcol(r / max(1, top - 1)))
            hb = lv[i] * bot * 2 * 0.75
            fb, rb = int(hb // 2), hb % 2 >= 1
            for r in range(min(bot, fb + 1)):
                ch = "█" if r < fb else ("▀" if rb else " ")
                if ch != " ":
                    S.put(cx, y + top + r, ch, mix(self.gcol(r / max(1, top - 1)), th.bg, 0.55 + 0.3 * r / max(1, bot)))

    def viz_led(self, x, y, w, h, s):
        S, th = self.S, self.th
        bwid = 2 if w >= 70 else 1
        nb = max(4, (w + 1) // (bwid + 1))
        lv, pk = self.spectrum(s, nb)
        ox = x + (w - (nb * (bwid + 1) - 1)) // 2
        b0, b1, b2 = th.bars
        for i in range(nb):
            cx = ox + i * (bwid + 1)
            lit = int(round(lv[i] * h))
            pr = min(h - 1, int(pk[i] * h))
            for r in range(h):
                f = r / max(1, h - 1)
                col = b0 if f < 0.6 else b1 if f < 0.85 else b2
                if r < lit:
                    c = col
                elif r == pr and pk[i] > 0.03:
                    c = th.fg
                else:
                    c = th.unlit
                S.put(cx, y + h - 1 - r, "▄" * bwid, c)

    def viz_dots(self, x, y, w, h, s):
        S = self.S
        W, H = w * 2, h * 4
        lv, _ = self.spectrum(s, max(8, w // 2))
        li = np.interp(np.linspace(0, len(lv) - 1, W), np.arange(len(lv)), lv)
        tops = H - (li * H).astype(int)
        rows = np.arange(H)[:, None]
        dots = (rows >= tops[None, :]) & ((rows < tops[None, :] + 2) | ((H - rows) % 3 == 0))
        codes = braille(dots)
        for cy in range(h):
            line = "".join(chr(0x2800 + c) if c else " " for c in codes[cy])
            S.put(x, y + cy, line, self.gcol((h - 1 - cy) / max(1, h - 1)))

    def viz_scope(self, x, y, w, h, s):
        S = self.S
        mono = s.mean(axis=1)
        n = len(mono)
        half = mono[: n // 2]
        zc = np.where((half[:-1] < 0) & (half[1:] >= 0))[0]
        st = int(zc[0]) if len(zc) else 0
        W, H = w * 2, h * 4
        seg = mono[st: st + min(n - st, 1024)]
        if len(seg) < 2:
            seg = np.zeros(2)
        v = seg[np.linspace(0, len(seg) - 1, W).astype(int)]
        g = min(6.0, 0.9 / max(float(np.abs(v).max()), 0.02))
        py = ((1 - (v * g + 1) / 2) * (H - 1)).round().astype(int).clip(0, H - 1)
        dots = np.zeros((H, W), bool)
        prev = py[0]
        for i in range(W):
            lo, hi = (prev, py[i]) if prev <= py[i] else (py[i], prev)
            dots[lo:hi + 1, i] = True
            prev = py[i]
        codes = braille(dots)
        mid = (h - 1) / 2
        for cy in range(h):
            line = "".join(chr(0x2800 + c) if c else " " for c in codes[cy])
            S.put(x, y + cy, line, self.gcol(abs(cy - mid) / max(1, mid)))

    def viz_fire(self, x, y, w, h, s):
        S, th = self.S, self.th
        if self.fire is None or self.fire.shape != (h + 1, w):
            self.fire = np.zeros((h + 1, w))
        F = self.fire
        lv, _ = self.spectrum(s, max(8, w // 3))
        seed = np.interp(np.linspace(0, len(lv) - 1, w), np.arange(len(lv)), lv)
        F[-1] = np.clip(seed * (0.7 + 0.7 * np.random.rand(w)), 0, 1.2)
        below = F[1:]
        F[:-1] = np.clip((below * 2 + np.roll(below, 1, 1) + np.roll(below, -1, 1)) / 4 * 0.93 - 0.012
                         - 0.02 * np.random.rand(h, w), 0, 1.2)
        chars = " .:-=+*#%@"
        pal = [th.unlit] + th.bars + [mix(th.bars[-1], "#ffffff", 0.6)]
        for r in range(h):
            row = F[r]
            for c in range(w):
                v = row[c]
                if v < 0.06:
                    continue
                S.put(x + c, y + r, chars[min(9, int(v * 9))], gradient(pal, min(1.0, v)))

    def viz_vu(self, x, y, w, h, s):
        S, th = self.S, self.th
        rms = np.sqrt(np.mean(s.astype(np.float64) ** 2, axis=0))
        lv = np.clip((20 * np.log10(rms + 1e-9) + 3 + 48) / 48, 0, 1)
        self.vu = np.where(lv > self.vu, self.vu + (lv - self.vu) * 0.5, np.maximum(lv, self.vu - 0.03))
        self.vu_pk = np.maximum(self.vu, self.vu_pk - 0.008)
        t = max(1, min(3, (h - 4) // 2))
        top = y + max(0, (h - (2 * t + 3)) // 2)
        bx, bw = x + 3, w - 11
        parts = "▏▎▍▌▋▊▉█"
        for ch in range(2):
            ry = top + ch * (t + 1)
            S.put(x, ry + t // 2, "LR"[ch], th.accent, bold=True)
            cells = self.vu[ch] * bw * 8
            full, rem = int(cells // 8), int(cells % 8)
            pkx = int(self.vu_pk[ch] * (bw - 1))
            for r in range(t):
                for c in range(bw):
                    if c < full:
                        S.put(bx + c, ry + r, "█", self.gcol(c / max(1, bw - 1)))
                    elif c == full and rem:
                        S.put(bx + c, ry + r, parts[rem - 1], self.gcol(c / max(1, bw - 1)))
                    else:
                        S.put(bx + c, ry + r, "░", th.unlit)
                S.put(bx + pkx, ry + r, "▌", th.fg)
            db = self.vu[ch] * 48 - 48
            S.put(bx + bw + 1, ry + t // 2, f"{db:5.1f}dB" if self.vu[ch] > 0 else "  -∞ dB", th.dim)
        sy = top + 2 * (t + 1)
        for mark in (-48, -36, -24, -12, -6, -3, 0):
            px = bx + int((mark + 48) / 48 * (bw - 1))
            S.put(px, sy, "╹", th.dim)
            lab = str(mark)
            S.put(min(bx + bw - len(lab), max(bx, px - len(lab) // 2)), sy + 1, lab, th.dim)

    # ── input ──
    def open_prompt(self, label, on_submit, text="", on_change=None, complete=False):
        self.prompt = {"label": label, "text": text, "submit": on_submit, "change": on_change,
                       "complete": complete}

    def prompt_key(self, k):
        p = self.prompt
        if k == "esc" or k == "ctrl-c":
            self.prompt = None
            return
        if k == "enter":
            self.prompt = None
            p["submit"](p["text"])
            return
        if k == "backspace":
            p["text"] = p["text"][:-1]
        elif k == "ctrl-u":
            p["text"] = ""
        elif k == "tab" and p["complete"]:
            p["text"] = self.complete_path(p["text"])
        elif len(k) == 1:
            p["text"] += k
        else:
            return
        if p["change"]:
            p["change"](p["text"])

    @staticmethod
    def complete_path(text):
        exp = os.path.expanduser(text)
        d, base = os.path.split(exp)
        try:
            names = sorted(n for n in os.listdir(d or ".") if n.startswith(base) and
                           (not n.startswith(".") or base.startswith(".")))
        except OSError:
            return text
        if not names:
            return text
        pre = os.path.commonprefix(names)
        full = os.path.join(d, pre)
        if len(names) == 1 and os.path.isdir(full):
            full += "/"
        home = str(Path.home())
        if text.startswith("~") and full.startswith(home):
            full = "~" + full[len(home):]
        return full

    def find(self, text):
        if not text:
            return
        lst = self.lists[self.tab]
        t = text.lower()
        for i, tr in enumerate(lst):
            if t in tr.label().lower() or (not tr.is_url and t in tr.src.lower()):
                self.cursor[self.tab] = i
                return

    def add(self, text):
        tracks = expand(text)
        if not tracks:
            self.flash(f"Nothing playable found at {text}")
            return
        self.lists[self.tab].extend(tracks)
        self.prober.add(tracks)
        self.flash(f"Added {len(tracks)} item{'s' * (len(tracks) != 1)} to {self.tab}")
        if self.engine.track is not None and self.play_tab == self.tab:
            self.schedule_next()

    def save_m3u(self, text):
        p = Path(text).expanduser()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            lines = ["#EXTM3U"]
            for t in self.lists[self.tab]:
                lines.append(f"#EXTINF:{int(t.duration or -1)},{t.label()}")
                lines.append(t.src)
            p.write_text("\n".join(lines) + "\n")
            self.flash(f"Saved {len(self.lists[self.tab])} entries to {p}")
        except OSError as ex:
            self.flash(f"Save failed: {ex}")

    def set_theme(self, i, save=True):
        self.theme_idx = i % len(self.themes)
        self.S.cache.clear()
        self.fire = None
        if save:
            self.save_config()

    def themes_key(self, k):
        tp = self.tp
        m = self.theme_matches()
        if k in ("esc", "ctrl-c"):
            self.set_theme(tp["orig"], save=False)
            self.overlay = None
            return
        if k == "enter":
            self.overlay = None
            self.save_config()
            self.flash(f"Theme: {self.th.name}")
            return
        if k in ("up", "S-tab"):
            tp["sel"] -= 1
        elif k in ("down", "tab"):
            tp["sel"] += 1
        elif k == "pgup":
            tp["sel"] -= 10
        elif k == "pgdn":
            tp["sel"] += 10
        elif k == "home":
            tp["sel"] = 0
        elif k == "end":
            tp["sel"] = len(m) - 1
        elif k == "backspace":
            tp["filter"] = tp["filter"][:-1]
            tp["sel"] = 0
        elif len(k) == 1:
            tp["filter"] += k
            tp["sel"] = 0
        m = self.theme_matches()
        if m:
            tp["sel"] = max(0, min(tp["sel"], len(m) - 1))
            self.set_theme(m[tp["sel"]], save=False)

    def eq_key(self, k):
        if k in ("esc", "E", "q"):
            self.overlay = None
            return True
        g = list(self.gains())
        if k in ("left", "h"):
            self.eq_band = (self.eq_band - 1) % 10
        elif k in ("right", "l", "tab"):
            self.eq_band = (self.eq_band + 1) % 10
        elif k in ("up", "k", "down", "j", "0"):
            if k == "0":
                g[self.eq_band] = 0
            else:
                g[self.eq_band] = max(-12, min(12, g[self.eq_band] + (1 if k in ("up", "k") else -1)))
            self.eq_custom, self.eq_name = g, "Custom"
            self.apply_eq()
            self.save_config()
        elif k in ("p", "e"):
            self.cycle_eq()
        elif k == "R":
            self.eq_custom, self.eq_name = [0] * 10, "Custom"
            self.apply_eq()
        else:
            return False
        return True

    def cycle_eq(self):
        names = list(EQ_PRESETS)
        self.eq_name = names[(names.index(self.eq_name) + 1) % len(names)]
        self.apply_eq()
        self.save_config()
        self.flash(f"EQ: {self.eq_name}")

    def on_key(self, k):
        if self.prompt:
            return self.prompt_key(k)
        if self.overlay == "help":
            self.overlay = None
            return
        if self.overlay == "themes":
            return self.themes_key(k)
        if self.overlay == "eq" and self.eq_key(k):
            return
        e = self.engine
        lst = self.lists[self.tab]
        cur = self.cursor[self.tab]
        page = max(1, self.list_rows - 1)
        if k in ("q", "ctrl-c"):
            self.running = False
        elif k == "esc":
            self.full_viz = False
            self.overlay = None
        elif k in ("j", "down"):
            self.cursor[self.tab] = min(len(lst) - 1, cur + 1)
        elif k in ("k", "up"):
            self.cursor[self.tab] = max(0, cur - 1)
        elif k == "pgdn":
            self.cursor[self.tab] = min(len(lst) - 1, cur + page)
        elif k == "pgup":
            self.cursor[self.tab] = max(0, cur - page)
        elif k in ("g", "home"):
            self.cursor[self.tab] = 0
        elif k in ("G", "end"):
            self.cursor[self.tab] = max(0, len(lst) - 1)
        elif k == "enter":
            self.play_at(self.tab, cur)
        elif k == " ":
            if e.track is None:
                self.play_at(self.tab, cur)
            else:
                e.toggle()
        elif k in ("n", ">", "."):
            self.next_track()
        elif k in ("p", "<", ","):
            self.prev_track()
        elif k in ("right", "left", "S-right", "S-left"):
            if e.track and e.track.duration:
                step = {"right": 5, "left": -5, "S-right": 30, "S-left": -30}[k]
                e.seek(e.position + step)
        elif k in ("+", "="):
            e.volume = min(100, e.volume + 5)
            e.muted = False
        elif k in ("-", "_"):
            e.volume = max(0, e.volume - 5)
        elif k == "m":
            e.muted = not e.muted
        elif k == "tab":
            self.tab = "Radio" if self.tab == "Library" else "Library"
        elif k == "s":
            self.shuffle = not self.shuffle
            self.shuf_next = None
            self.schedule_next()
            self.flash("Shuffle on" if self.shuffle else "Shuffle off")
        elif k == "r":
            self.repeat = {"off": "all", "all": "one", "one": "off"}[self.repeat]
            self.schedule_next()
        elif k == "e":
            self.cycle_eq()
        elif k == "E":
            self.overlay = "eq"
        elif k == "v":
            self.viz = self.VIZ_MODES[(self.VIZ_MODES.index(self.viz) + 1) % len(self.VIZ_MODES)]
            self.flash(f"Visualizer: {self.viz}")
        elif k == "V":
            self.full_viz = not self.full_viz
        elif k == "t":
            self.set_theme(self.theme_idx + 1)
            self.flash(f"Theme: {self.th.name}")
        elif k == "T":
            self.overlay = "themes"
            self.tp = {"filter": "", "sel": self.theme_idx, "orig": self.theme_idx}
        elif k == "B":
            self.transparent = not self.transparent
            self.save_config()
        elif k == "?":
            self.overlay = "help"
        elif k == "/":
            self.open_prompt("find", lambda t: None, on_change=self.find)
        elif k == "a":
            self.open_prompt("add file / folder / URL (tab completes)", self.add, complete=True)
        elif k == "w":
            self.open_prompt("save as", self.save_m3u, text=f"~/Music/{APP}-{self.tab.lower()}.m3u",
                             complete=True)
        elif k in ("d", "delete") and lst:
            t = lst.pop(cur)
            self.queue = [q for q in self.queue if q is not t]
            self.cursor[self.tab] = min(cur, len(lst) - 1)
            if self.play_tab == self.tab:
                self.schedule_next()
        elif k == "A" and lst and self.tab == "Library":
            t = lst[cur]
            if any(q is t for q in self.queue):
                self.queue = [q for q in self.queue if q is not t]
                self.flash("Removed from queue")
            else:
                self.queue.append(t)
                self.flash(f"Queued #{len(self.queue)}: {trunc(t.label(), 50)}")
            self.cursor[self.tab] = min(len(lst) - 1, cur + 1)
            if self.play_tab == "Library":
                self.schedule_next()

    def run(self):
        fd = sys.stdin.fileno()
        dec = codecs.getincrementaldecoder("utf-8")("replace")
        signal.signal(signal.SIGWINCH, lambda *a: setattr(self, "resized", True))
        nxt = 0.0
        while self.running:
            self.handle_events()
            busy = self.engine.track is not None and not self.engine.paused
            frame = 1 / 30 if busy or self.viz == "fire" else 1 / 12
            r, _, _ = select.select([fd], [], [], max(0.0, nxt - time.monotonic()))
            if r:
                data = os.read(fd, 4096)
                if not data:
                    break
                for k in parse_keys(dec.decode(data)):
                    self.on_key(k)
                    if not self.running:
                        break
            if time.monotonic() >= nxt or r:
                if self.resized:
                    self.resized = False
                    self.S.resize()
                self.draw()
                nxt = time.monotonic() + frame


USAGE = f"""{APP} {VERSION} — terminal audio player

usage: {APP} [options] [files | folders | playlists | URLs ...]

  --theme NAME          start with a theme (see --list-themes)
  --posh FILE           use an oh-my-posh theme file (.omp.json/.yaml/.toml)
  --viz MODE            visualizer: {", ".join(App.VIZ_MODES)}
  --list-themes         list built-in and discovered oh-my-posh themes
  --fetch-posh-themes   download the official oh-my-posh theme pack into
                        {POSH_DIR}
  -h, --help            show this help

With no arguments, ~/Music is loaded. Radio stations come from
{STATIONS_FILE} if it exists. Press ? inside the player for keys.
"""


def main():
    args = sys.argv[1:]
    paths, theme_name, posh_files, viz = [], None, [], None
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-h", "--help"):
            print(USAGE)
            return
        if a == "--version":
            print(VERSION)
            return
        if a == "--fetch-posh-themes":
            try:
                fetch_posh_themes()
            except Exception as ex:
                sys.exit(f"download failed: {ex}")
            return
        if a in ("--theme", "--posh", "--viz") and i + 1 < len(args):
            v = args[i + 1]
            if a == "--theme":
                theme_name = v
            elif a == "--posh":
                posh_files.append(v)
            else:
                viz = v
            i += 2
            continue
        if a == "--list-themes":
            for t in BUILTIN_THEMES + load_posh_themes():
                print(f"{t.name:32} {t.source}")
            return
        paths.append(a)
        i += 1

    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            sys.exit(f"{APP}: {tool} is required but was not found in PATH")
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        sys.exit(f"{APP}: needs an interactive terminal")

    posh = load_posh_themes(posh_files)
    themes = BUILTIN_THEMES + posh
    if posh_files and not theme_name:
        first = parse_posh(Path(posh_files[0]).expanduser())
        if first:
            theme_name = first.name
        else:
            print(f"{APP}: couldn't read colours from {posh_files[0]}", file=sys.stderr)

    library = []
    if paths:
        for p in paths:
            library += expand(p)
    elif (Path.home() / "Music").is_dir():
        library = expand(str(Path.home() / "Music"))

    cfg = load_config()
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    app = None
    try:
        tty.setraw(fd)
        os.write(1, b"\x1b[?1049h\x1b[?25l\x1b[?7l\x1b[2J")
        app = App(library, themes, cfg, theme_name, viz)
        app.run()
    finally:
        os.write(1, b"\x1b[0m\x1b[?7h\x1b[?25h\x1b[?1049l")
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        if app:
            app.save_config()
            app.engine.shutdown()


if __name__ == "__main__":
    main()
