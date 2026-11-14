"""
AURA — a music player and local library editor (formerly Unreleased).

Runs entirely on your own machine. Scans folders you point it at, caches
everything in a small database, lets you edit tags one track at a time, fills
in metadata in bulk, and finds duplicates four different ways.
"""

import hashlib
import html as htmllib
from html.parser import HTMLParser
import io
import json
import os
import re
import secrets
import shutil
import socket
import sqlite3
import struct
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
import subprocess
import sys
import threading
import time
import unicodedata
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
from pathlib import Path

# The Windows exe is built without a console window. Then there is nowhere for
# output to go, and some libraries fail on a missing stdout/stderr — give them one.
if sys.stdout is None or sys.stderr is None:
    _devnull = open(os.devnull, "w", encoding="utf-8")
    sys.stdout = sys.stdout or _devnull
    sys.stderr = sys.stderr or _devnull

from flask import Flask, Response, abort, jsonify, request, send_file

try:
    import numpy as np
except Exception:
    np = None

try:
    import soundfile as _sf
except Exception:
    _sf = None

from mutagen import File as MutagenFile
from mutagen.id3 import (ID3, ID3NoHeaderError, APIC, TIT2, TPE1, TPE2, TALB,
                         TDRC, TCON, TRCK, TBPM, TKEY, COMM, TXXX, USLT, SYLT)

# The data folder keeps its old name so an existing library, ratings, versions
# and settings carry straight over to AURA.
APP_NAME = "Unreleased"
FROZEN = getattr(sys, "frozen", False)


def resource_dir():
    if FROZEN:
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent


def data_dir():
    if FROZEN:
        base = (os.environ.get("APPDATA") or os.environ.get("XDG_DATA_HOME")
                or str(Path.home() / ".local" / "share"))
        d = Path(base) / APP_NAME
    else:
        d = Path(__file__).resolve().parent
    d.mkdir(parents=True, exist_ok=True)
    return d


DATA_DIR = data_dir()
DB_PATH = DATA_DIR / "library.db"
CONFIG_PATH = DATA_DIR / "config.json"

AUDIO_EXT = {".mp3", ".flac", ".m4a", ".wav", ".aiff", ".aif", ".ogg", ".opus", ".wma"}

FP_BANDS, FP_SLICES = 32, 128
FP_BITS = (FP_SLICES - 1) * (FP_BANDS - 1)

# Fields the editor and the bulk tools can write.
TAG_FIELDS = ["title", "artist", "albumartist", "album", "year", "genre",
              "track_no", "bpm", "music_key", "producer", "comment",
              "notes", "user_tags", "era"]

app = Flask(__name__)
TOKEN = secrets.token_urlsafe(24)
ALLOWED_ORIGINS = set()

SCAN_STATE = {"running": False, "done": 0, "total": 0, "message": "", "finished_at": 0}
FFMPEG = shutil.which("ffmpeg")
CAN_FINGERPRINT = np is not None and (_sf is not None or FFMPEG is not None)


@app.before_request
def guard():
    if not request.path.startswith("/api/"):
        return None
    origin = request.headers.get("Origin")
    if origin and origin not in ALLOWED_ORIGINS:
        abort(403)
    if request.method != "GET" and request.headers.get("X-Token") != TOKEN:
        abort(403)
    return None


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------

def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL, filename TEXT,
    folder TEXT, ext TEXT, size INTEGER, mtime REAL, duration REAL,
    bitrate INTEGER, samplerate INTEGER, file_hash TEXT, audio_hash TEXT,
    fingerprint BLOB, title TEXT, artist TEXT, album TEXT, albumartist TEXT,
    year TEXT, genre TEXT, track_no TEXT, bpm TEXT, music_key TEXT,
    producer TEXT DEFAULT '', comment TEXT, has_art INTEGER DEFAULT 0,
    rating INTEGER DEFAULT 0, notes TEXT DEFAULT '', user_tags TEXT DEFAULT '',
    missing INTEGER DEFAULT 0, first_seen REAL, last_scan REAL
);
CREATE INDEX IF NOT EXISTS idx_file_hash  ON tracks(file_hash);
CREATE INDEX IF NOT EXISTS idx_audio_hash ON tracks(audio_hash);

CREATE TABLE IF NOT EXISTS undo_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, label TEXT, n INTEGER
);
CREATE TABLE IF NOT EXISTS undo_items (
    batch_id INTEGER, track_id INTEGER, field TEXT, old_value TEXT
);
CREATE INDEX IF NOT EXISTS idx_undo ON undo_items(batch_id);

-- Which tracks are versions of the same song. status is 'grouped' (a member
-- of `song`) or 'ignored' (the user said "not a version", so it is never
-- suggested again).
CREATE TABLE IF NOT EXISTS versions (
    track_id INTEGER PRIMARY KEY, song TEXT DEFAULT '', label TEXT DEFAULT '',
    status TEXT DEFAULT 'grouped'
);
CREATE INDEX IF NOT EXISTS idx_versions_song ON versions(song COLLATE NOCASE);

-- Player: hearted songs and your own playlists. Kept in the app's library,
-- never written into the music files.
CREATE TABLE IF NOT EXISTS favorites (
    track_id INTEGER PRIMARY KEY, added REAL
);
CREATE TABLE IF NOT EXISTS playlists (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, created REAL, updated REAL
);
CREATE TABLE IF NOT EXISTS playlist_items (
    playlist_id INTEGER NOT NULL, track_id INTEGER NOT NULL, pos INTEGER NOT NULL,
    PRIMARY KEY (playlist_id, track_id)
);
CREATE INDEX IF NOT EXISTS idx_pl_items ON playlist_items(playlist_id, pos);

-- How often and how recently each song was played (for Most / Recently played).
CREATE TABLE IF NOT EXISTS plays (
    track_id INTEGER PRIMARY KEY, count INTEGER DEFAULT 0, last_played REAL
);
-- Every listen, for the Stats page: how many seconds of which song, when.
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY, track_id INTEGER, ts REAL, seconds REAL
);
CREATE INDEX IF NOT EXISTS idx_history_ts ON history(ts);
-- Marked moments in a song.
CREATE TABLE IF NOT EXISTS bookmarks (
    id INTEGER PRIMARY KEY, track_id INTEGER, t REAL, label TEXT DEFAULT '', created REAL
);
CREATE INDEX IF NOT EXISTS idx_bookmarks ON bookmarks(track_id);
-- "The full version of a snippet you have just arrived".
CREATE TABLE IF NOT EXISTS snippet_alerts (
    id INTEGER PRIMARY KEY, full_id INTEGER, snippet_ids TEXT, song TEXT, ts REAL,
    dismissed INTEGER DEFAULT 0
);
-- Every time a song starts (for History in the queue).
CREATE TABLE IF NOT EXISTS play_log (
    id INTEGER PRIMARY KEY, track_id INTEGER, ts REAL
);
CREATE INDEX IF NOT EXISTS idx_play_log_ts ON play_log(ts);
-- Waveform shape of each song, for the seek bar. Recomputed when the file changes.
CREATE TABLE IF NOT EXISTS waveforms (
    track_id INTEGER PRIMARY KEY, mtime REAL, peaks BLOB
);
-- Web links for cover art (for Discord). url '' = looked, found nothing.
CREATE TABLE IF NOT EXISTS art_links (
    key TEXT PRIMARY KEY, url TEXT, ts REAL
);
-- Small key/value store: the player's saved state (resume, queue, settings).
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY, value TEXT
);
"""

DEFAULT_CONFIG = {
    "folders": [],
    "quarantine": "",
    "rename_pattern": "{title}",
    "write_tags_to_file": True,
    "fingerprint": True,
    # Rescan by itself when files are added, changed or removed in the folders.
    "watch_folders": True,
    # Discord "Listening to" status. The Application ID comes from a free app at
    # discord.com/developers/applications — see README → Discord status.
    "discord": False,
    "discord_client_id": "",
    # Cover art on Discord. Discord can only show a picture from a web link:
    # released songs get the official cover from Apple's iTunes catalog; your
    # own covers can be uploaded to a public image host (off by default).
    "discord_art_lookup": True,
    "discord_art_upload": False,
    # Auto-sync speech model: "base" (fast, ~145 MB), "small" (better, ~480 MB),
    # "large-v3-turbo" (best, ~1.6 GB). Downloaded once, the first time it's used.
    "autosync_model": "small",
    # Language the songs are sung in ("" = let the model guess each time, slower and
    # sometimes wrong on intros), and the second, precise pass that times every word.
    "autosync_language": "en",
    # Transcribe (writing the words down from scratch) is the hardest job, so it gets
    # the most accurate model by default ("same" = use the auto-sync model). After
    # transcribing, AURA also looks online for real lyrics that match what it heard.
    "transcribe_model": "large-v3-turbo",
    "transcribe_lookup": True,
    "autosync_precise": True,
    # Work out BPM and musical key in the background for songs that have none.
    "detect_bpm_key": True,
    # Values stamped onto every track by the auto-fill tool.
    "defaults": {"artist": "", "albumartist": "", "album": "", "genre": "", "year": ""},
}


def init_db():
    with db() as conn:
        conn.executescript(SCHEMA)
        # migrate older databases forward without losing anything
        have = {r["name"] for r in conn.execute("PRAGMA table_info(tracks)")}
        for col, decl in [("producer", "TEXT DEFAULT ''"),
                          ("user_tags", "TEXT DEFAULT ''"),
                          ("notes", "TEXT DEFAULT ''"),
                          ("rating", "INTEGER DEFAULT 0"),
                          ("lyrics", "TEXT DEFAULT ''"),
                          ("has_synced", "INTEGER DEFAULT 0"),
                          ("gain_db", "REAL"),
                          ("era", "TEXT DEFAULT ''"),
                          ("analysed", "INTEGER DEFAULT 0")]:
            if col not in have:
                conn.execute("ALTER TABLE tracks ADD COLUMN %s %s" % (col, decl))


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if CONFIG_PATH.exists():
        try:
            stored = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            d = stored.pop("defaults", None)
            cfg.update(stored)
            if isinstance(d, dict):
                cfg["defaults"].update({k: v for k, v in d.items()
                                        if k in cfg["defaults"]})
        except Exception:
            pass
    return cfg


def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def quarantine_dir(cfg=None):
    cfg = cfg or load_config()
    q = (cfg.get("quarantine") or "").strip()
    if q:
        return Path(q)
    folders = cfg.get("folders") or []
    return Path(folders[0]) / "_duplicates" if folders else None


# --------------------------------------------------------------------------
# undo
# --------------------------------------------------------------------------

def record_undo(conn, label, changes):
    """changes: {track_id: {field: old_value}}. Returns the batch id."""
    if not changes:
        return None
    cur = conn.execute("INSERT INTO undo_batches (ts, label, n) VALUES (?,?,?)",
                       (time.time(), label, len(changes)))
    batch = cur.lastrowid
    rows = [(batch, tid, f, "" if v is None else str(v))
            for tid, fields in changes.items() for f, v in fields.items()]
    conn.executemany(
        "INSERT INTO undo_items (batch_id, track_id, field, old_value) VALUES (?,?,?,?)",
        rows)
    # keep the last 20 batches only
    old = [r["id"] for r in conn.execute(
        "SELECT id FROM undo_batches ORDER BY id DESC LIMIT -1 OFFSET 20")]
    if old:
        marks = ",".join("?" for _ in old)
        conn.execute("DELETE FROM undo_items WHERE batch_id IN (%s)" % marks, old)
        conn.execute("DELETE FROM undo_batches WHERE id IN (%s)" % marks, old)
    return batch


# --------------------------------------------------------------------------
# hashing / fingerprinting
# --------------------------------------------------------------------------

def file_sha256(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def audio_stream_hash(path):
    """Hash only the audio payload, skipping ID3v2 at the head and ID3v1 at the
    tail, so a file you retagged still matches its untouched twin."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            start = 0
            head = f.read(10)
            if head[:3] == b"ID3":
                flags = head[5]
                tag_size = 0
                for b in head[6:10]:
                    tag_size = (tag_size << 7) | (b & 0x7F)
                start = 10 + tag_size + (10 if flags & 0x10 else 0)
            end = size
            if size > 128:
                f.seek(size - 128)
                if f.read(3) == b"TAG":
                    end = size - 128
            if end <= start:
                return None
            f.seek(start)
            h = hashlib.md5()
            remaining = end - start
            while remaining > 0:
                b = f.read(min(1 << 20, remaining))
                if not b:
                    break
                remaining -= len(b)
                h.update(b)
            return h.hexdigest()
    except Exception:
        return None


def _resample(x, sr, target_sr):
    if np is None or x is None or len(x) < 2:
        return None
    if sr == target_sr:
        return x.astype(np.float32)
    n_out = int(len(x) * target_sr / float(sr))
    if n_out < 2:
        return None
    return np.interp(np.linspace(0.0, len(x) - 1.0, n_out),
                     np.arange(len(x), dtype=np.float64), x).astype(np.float32)


def decode_mono(path, target_sr=11025, max_seconds=420):
    if np is None:
        return None
    if _sf is not None:
        try:
            with _sf.SoundFile(str(path)) as f:
                sr = f.samplerate
                data = f.read(frames=int(sr * max_seconds), dtype="float32",
                              always_2d=True)
            if data.size:
                return _resample(data.mean(axis=1), sr, target_sr)
        except Exception:
            pass
    try:
        x = decode_av(path, target_sr, max_seconds)
        if x is not None and len(x) >= target_sr * 3:
            return x
    except Exception:
        pass
    if FFMPEG:
        try:
            proc = subprocess.run(
                [FFMPEG, "-v", "quiet", "-i", str(path), "-map", "0:a:0",
                 "-ac", "1", "-ar", str(target_sr), "-t", str(max_seconds),
                 "-f", "s16le", "-"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=180,
                creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0))
            if len(proc.stdout) >= target_sr * 2 * 3:
                return np.frombuffer(proc.stdout, dtype="<i2").astype(np.float32) / 32768.0
        except Exception:
            pass
    return None


def decode_av(path, target_sr=11025, max_seconds=420):
    """Mono float32 at target_sr via PyAV (handles M4A, AAC, WMA, OPUS…)."""
    import av
    out, total = [], 0
    limit = int(target_sr * max_seconds)
    with av.open(str(path)) as inp:
        stream = next((st for st in inp.streams if st.type == "audio"), None)
        if stream is None:
            return None
        res = av.AudioResampler(format="flt", layout="mono", rate=target_sr)
        for frame in inp.decode(stream):
            for rf in res.resample(frame):
                a = rf.to_ndarray().reshape(-1)
                out.append(a)
                total += len(a)
            if total >= limit:
                break
    if not out:
        return None
    return np.concatenate(out)[:limit].astype(np.float32)


def fingerprint(path):
    """Haitsma-Kalker style sub-fingerprint: per time slice and frequency band,
    one bit for whether the energy difference between neighbouring bands rose or
    fell. Survives re-encoding, so two renders of one song match while unrelated
    songs sit near a 50% bit difference."""
    if np is None:
        return None
    x = decode_mono(path)
    if x is None or len(x) < 11025 * 3:
        return None
    try:
        n, hop = 2048, 512
        frames = 1 + (len(x) - n) // hop
        if frames < FP_SLICES:
            return None
        win = np.hanning(n).astype(np.float32)
        edges = np.unique(np.geomspace(6, n // 2, FP_BANDS + 1).astype(int))
        if len(edges) < FP_BANDS + 1:
            edges = np.linspace(6, n // 2, FP_BANDS + 1).astype(int)
        bands = np.empty((frames, FP_BANDS), dtype=np.float32)
        for s in range(0, frames, 512):
            e = min(s + 512, frames)
            idx = np.arange(n)[None, :] + hop * np.arange(s, e)[:, None]
            spec = np.abs(np.fft.rfft(x[idx] * win, axis=1))
            for i in range(FP_BANDS):
                lo, hi = edges[i], max(edges[i + 1], edges[i] + 1)
                bands[s:e, i] = spec[:, lo:hi].mean(axis=1)
        bands = np.log(bands + 1e-9)
        cuts = np.linspace(0, frames, FP_SLICES + 1).astype(int)
        energy = np.stack([bands[cuts[i]:max(cuts[i + 1], cuts[i] + 1)].mean(axis=0)
                           for i in range(FP_SLICES)])
        dd = np.diff(np.diff(energy, axis=1), axis=0)
        return np.packbits((dd > 0).astype(np.uint8).ravel()).tobytes()
    except Exception:
        return None


def fp_similarity(a, b):
    if np is None or not a or not b or len(a) != len(b):
        return 0.0
    ba = np.unpackbits(np.frombuffer(a, dtype=np.uint8))[:FP_BITS]
    bb = np.unpackbits(np.frombuffer(b, dtype=np.uint8))[:FP_BITS]
    if ba.size == 0:
        return 0.0
    return 1.0 - float(np.count_nonzero(ba != bb)) / ba.size


# --------------------------------------------------------------------------
# tags
# --------------------------------------------------------------------------

def _txt(frames):
    return str(frames[0]).strip() if frames else ""


def read_tags(path):
    out = {k: "" for k in TAG_FIELDS}
    out.update({"has_art": 0, "rating": 0, "duration": 0.0,
                "bitrate": 0, "samplerate": 0})
    try:
        mf = MutagenFile(path)
    except Exception:
        return out
    if mf is None:
        return out

    info = getattr(mf, "info", None)
    if info is not None:
        out["duration"] = float(getattr(info, "length", 0) or 0)
        out["bitrate"] = int(getattr(info, "bitrate", 0) or 0)
        out["samplerate"] = int(getattr(info, "sample_rate", 0) or 0)

    tags = mf.tags
    if tags is None:
        return out

    if isinstance(tags, ID3):
        out["title"] = _txt(tags.getall("TIT2"))
        out["artist"] = _txt(tags.getall("TPE1"))
        out["album"] = _txt(tags.getall("TALB"))
        out["albumartist"] = _txt(tags.getall("TPE2"))
        out["year"] = _txt(tags.getall("TDRC")) or _txt(tags.getall("TYER"))
        out["genre"] = _txt(tags.getall("TCON"))
        out["track_no"] = _txt(tags.getall("TRCK"))
        out["bpm"] = _txt(tags.getall("TBPM"))
        out["music_key"] = _txt(tags.getall("TKEY"))
        comments = tags.getall("COMM")
        out["comment"] = str(comments[0].text[0]).strip() if comments and comments[0].text else ""
        out["has_art"] = 1 if tags.getall("APIC") else 0
        for fr in tags.getall("TXXX"):
            desc = (fr.desc or "").upper()
            val = str(fr.text[0]).strip() if fr.text else ""
            if desc == "UNRELEASED_RATING":
                try:
                    out["rating"] = int(val)
                except ValueError:
                    pass
            elif desc == "UNRELEASED_NOTES":
                out["notes"] = val
            elif desc == "UNRELEASED_TAGS":
                out["user_tags"] = val
            elif desc in ("PRODUCER", "UNRELEASED_PRODUCER"):
                out["producer"] = val
            elif desc in ("ERA", "UNRELEASED_ERA"):
                out["era"] = val
    else:
        def g(*keys):
            for k in keys:
                try:
                    v = tags.get(k)
                except (ValueError, KeyError, TypeError):
                    v = None
                if v:
                    return str(v[0]).strip() if isinstance(v, list) else str(v).strip()
            return ""
        out["title"] = g("title", "\xa9nam")
        out["artist"] = g("artist", "\xa9ART")
        out["album"] = g("album", "\xa9alb")
        out["albumartist"] = g("albumartist", "aART")
        out["year"] = g("date", "\xa9day")
        out["genre"] = g("genre", "\xa9gen")
        out["track_no"] = g("tracknumber")
        out["bpm"] = g("bpm", "tmpo")
        out["comment"] = g("comment", "\xa9cmt")
        try:
            covr = tags.get("covr")
        except (ValueError, KeyError, TypeError):
            covr = None
        out["has_art"] = 1 if (covr or getattr(mf, "pictures", None)) else 0
    return out


def write_tags(path, data, write_extras=True):
    p = Path(path)
    if p.suffix.lower() == ".mp3":
        try:
            tags = ID3(path)
        except ID3NoHeaderError:
            tags = ID3()

        def setf(cls, value):
            tags.delall(cls.__name__)
            if value:
                tags.add(cls(encoding=3, text=[str(value)]))

        setf(TIT2, data.get("title", ""))
        setf(TPE1, data.get("artist", ""))
        setf(TALB, data.get("album", ""))
        setf(TPE2, data.get("albumartist", ""))
        setf(TDRC, data.get("year", ""))
        setf(TCON, data.get("genre", ""))
        setf(TRCK, data.get("track_no", ""))
        setf(TBPM, data.get("bpm", ""))
        setf(TKEY, data.get("music_key", ""))

        tags.delall("COMM")
        if data.get("comment"):
            tags.add(COMM(encoding=3, lang="eng", desc="", text=[data["comment"]]))

        if write_extras:
            wanted = {"UNRELEASED_RATING": str(data.get("rating", 0) or ""),
                      "UNRELEASED_NOTES": data.get("notes", ""),
                      "UNRELEASED_TAGS": data.get("user_tags", ""),
                      "PRODUCER": data.get("producer", ""),
                      "ERA": data.get("era", "")}
            for fr in list(tags.getall("TXXX")):
                if (fr.desc or "").upper() in wanted or (fr.desc or "").upper() == "UNRELEASED_STATUS":
                    tags.delall("TXXX:%s" % fr.desc)
            for desc, val in wanted.items():
                if val:
                    tags.add(TXXX(encoding=3, desc=desc, text=[val]))
        tags.save(path, v2_version=3)
        return True

    mf = MutagenFile(path, easy=True)
    if mf is None:
        return False
    if mf.tags is None:
        try:
            mf.add_tags()
        except Exception:
            return False
    for src, dst in {"title": "title", "artist": "artist", "album": "album",
                     "albumartist": "albumartist", "year": "date",
                     "genre": "genre", "track_no": "tracknumber",
                     "bpm": "bpm", "music_key": "initialkey"}.items():
        val = data.get(src, "")
        try:
            if val:
                mf[dst] = [str(val)]
            elif dst in mf:
                del mf[dst]
        except Exception:
            pass
    mf.save()
    return True


def set_artwork(path, image_bytes, mime):
    """Embed (or with no bytes, remove) the cover. MP3, WAV, AIFF, FLAC, M4A, OGG, OPUS."""
    ext = Path(path).suffix.lower()
    mime = mime or "image/jpeg"
    if ext == ".mp3":
        try:
            tags = ID3(path)
        except ID3NoHeaderError:
            tags = ID3()
        tags.delall("APIC")
        if image_bytes:
            tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=image_bytes))
        tags.save(path, v2_version=3)
        return True
    if ext in (".wav", ".aif", ".aiff"):
        mf = MutagenFile(path)
        if mf.tags is None:
            mf.add_tags()
        mf.tags.delall("APIC")
        if image_bytes:
            mf.tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=image_bytes))
        mf.save()
        return True
    if ext == ".flac":
        from mutagen.flac import FLAC, Picture
        mf = FLAC(path)
        mf.clear_pictures()
        if image_bytes:
            pic = Picture()
            pic.type, pic.mime, pic.desc, pic.data = 3, mime, "Cover", image_bytes
            mf.add_picture(pic)
        mf.save()
        return True
    if ext in (".m4a", ".mp4", ".aac"):
        from mutagen.mp4 import MP4, MP4Cover
        mf = MP4(path)
        if mf.tags is None:
            mf.add_tags()
        if image_bytes:
            fmt = MP4Cover.FORMAT_PNG if "png" in mime else MP4Cover.FORMAT_JPEG
            mf.tags["covr"] = [MP4Cover(image_bytes, imageformat=fmt)]
        else:
            mf.tags.pop("covr", None)
        mf.save()
        return True
    if ext in (".ogg", ".opus"):
        import base64
        from mutagen.flac import Picture
        mf = MutagenFile(path)
        if mf.tags is None:
            mf.add_tags()
        if "metadata_block_picture" in mf.tags:
            del mf.tags["metadata_block_picture"]
        if image_bytes:
            pic = Picture()
            pic.type, pic.mime, pic.desc, pic.data = 3, mime, "Cover", image_bytes
            mf.tags["metadata_block_picture"] = [base64.b64encode(pic.write()).decode("ascii")]
        mf.save()
        return True
    raise OSError("%s files can't hold cover art — convert it to MP3 first" % ext.lstrip(".").upper())


def get_artwork(path):
    try:
        mf = MutagenFile(path)
    except Exception:
        return None, None
    if mf is None:
        return None, None
    tags = mf.tags
    if isinstance(tags, ID3):
        pics = tags.getall("APIC")
        if pics:
            return pics[0].data, pics[0].mime or "image/jpeg"
    if getattr(mf, "pictures", None):
        pic = mf.pictures[0]
        return pic.data, pic.mime or "image/jpeg"
    if tags and "covr" in tags:
        c = tags["covr"][0]
        return bytes(c), ("image/png" if getattr(c, "imageformat", 0) == 14 else "image/jpeg")
    try:
        b64 = tags.get("metadata_block_picture") if tags is not None else None
    except Exception:
        b64 = None
    if b64:
        try:
            import base64
            from mutagen.flac import Picture
            pic = Picture(base64.b64decode(b64[0]))
            return pic.data, pic.mime or "image/jpeg"
        except Exception:
            pass
    return None, None


# --------------------------------------------------------------------------
# filename parsing
# --------------------------------------------------------------------------

SMALL_WORDS = {"a", "an", "and", "as", "at", "but", "by", "для", "for", "from",
               "in", "into", "nor", "of", "on", "onto", "or", "over", "the",
               "to", "up", "vs", "with"}
BITRATES = {"128", "160", "192", "224", "256", "320"}
QUALITY = re.compile(r"\b(cdq|hq|lq|full|lossless|\d{2,3}\s?kbps|mp3|wav|master(?:ed)?ing)\b",
                     re.I)
FEAT_RE = re.compile(r"[\(\[\{]?\s*(?:feat|ft|featuring|w/|with)\.?\s+([^\)\]\}\[\(]+)[\)\]\}]?",
                     re.I)
PROD_RE = re.compile(r"[\(\[\{]?\s*prod(?:uced)?\.?\s*(?:by)?\s*[:\-]?\s*([^\)\]\}\[\(]+)[\)\]\}]?",
                     re.I)


def smart_title(s):
    """Title-case only when the original carries no capitalisation intent, so
    deliberate styling like ALLCAPS or lowercase-as-aesthetic survives."""
    letters = [c for c in s if c.isalpha()]
    if not letters:
        return s
    mixed = any(c.isupper() for c in letters) and any(c.islower() for c in letters)
    if mixed:
        return s
    words = s.split()
    out = []
    for i, w in enumerate(words):
        low = w.lower()
        if i not in (0, len(words) - 1) and low in SMALL_WORDS:
            out.append(low)
        else:
            out.append(low[:1].upper() + low[1:])
    return " ".join(out)


def parse_filename(filename, known_artist=""):
    """Pull a clean title, features and producer out of a messy filename.

    Deliberately conservative: it strips things that are certainly not part of a
    song's name (track indexes, duplicate markers, bitrate and quality tags) and
    lifts out feat/prod credits, but never invents information."""
    stem = os.path.splitext(filename)[0]
    s = stem.replace("_", " ")
    s = re.sub(r"\s+", " ", s).strip()

    # leading track index: "01 - ", "01.", "(03)"
    s = re.sub(r"^[\(\[]?\d{1,3}[\)\]]?\s*[-–—.)\]]\s*", "", s).strip()
    # bare leading index with no separator ("07 track name"). Only when it is
    # zero-padded, so titles that genuinely open with a number — "21 Guns",
    # "99 Problems" — are left alone.
    s = re.sub(r"^0\d{0,2}\s+(?=\S)", "", s).strip()
    # trailing duplicate markers: " (1)", " - Copy", " copy 2"
    s = re.sub(r"\s*[-–—]?\s*copy(\s*\d+)?$", "", s, flags=re.I)
    s = re.sub(r"\s*\(\s*\d{1,2}\s*\)\s*$", "", s).strip()

    producer = ""
    m = PROD_RE.search(s)
    if m:
        producer = m.group(1).strip(" -–—_.&,")
        s = (s[:m.start()] + " " + s[m.end():]).strip()

    features = ""
    m = FEAT_RE.search(s)
    if m:
        features = m.group(1).strip(" -–—_.&,")
        s = (s[:m.start()] + " " + s[m.end():]).strip()

    # quality / bitrate noise
    s = QUALITY.sub(" ", s)
    s = " ".join(w for w in s.split()
                 if not (w.strip("()[]{}") in BITRATES and len(s.split()) > 1))

    # "Artist - Title" where the left side is the artist we already know
    if " - " in s and known_artist:
        left, right = s.split(" - ", 1)
        if SequenceMatcher(None, left.strip().lower(),
                           known_artist.strip().lower()).ratio() > 0.85:
            s = right.strip()

    s = re.sub(r"[\(\[\{]\s*[\)\]\}]", " ", s)          # emptied brackets
    s = re.sub(r"\s*[-–—_]+\s*$", "", s)
    s = re.sub(r"^\s*[-–—_]+\s*", "", s)
    s = re.sub(r"\s+", " ", s).strip(" -–—_.")

    title = smart_title(s) if s else ""
    if features:
        title = "%s (feat. %s)" % (title, smart_title(features)) if title else title
    return {"title": title, "producer": smart_title(producer) if producer else ""}


QUALITY_TAGS = [("CDQ", r"\bcdq\b"), ("HQ", r"\bhq\b"), ("LQ", r"\blq\b"), ("Snippet", r"\bsnippet\b"),
                ("Leak", r"\bleak(?:ed)?\b"), ("OG", r"\bog(?:\s+file)?\b"), ("Session", r"\bsession(?:s)?\b"),
                ("Instrumental", r"\binstrumental\b|\binst\b"), ("Acapella", r"\ba\s?cap+el+a\b")]


def parse_extras(filename):
    """What else a filename says: track number, artist (from 'Artist - Title'),
    and quality/leak labels."""
    stem = os.path.splitext(filename)[0].replace("_", " ")
    stem = re.sub(r"\s+", " ", stem).strip()
    out = {"track_no": "", "artist": "", "quality": []}
    m = re.match(r"^[\(\[]?(\d{1,3})[\)\]]?\s*[-–—.)\]]\s*", stem) or re.match(r"^(0\d{0,2})\s+(?=\S)", stem)
    if m:
        out["track_no"] = str(int(m.group(1)))
        stem = stem[m.end():].strip()
    if " - " in stem:
        left = stem.split(" - ", 1)[0].strip()
        if left and len(left) <= 40 and not re.search(r"[\(\[\{]", left) and len(left.split()) <= 5 \
                and not re.fullmatch(r"[\d\s.]+", left):
            out["artist"] = left
    low = stem.lower()
    out["quality"] = [name for name, rx in QUALITY_TAGS if re.search(rx, low)]
    return out


# --------------------------------------------------------------------------
# scanning
# --------------------------------------------------------------------------

def iter_audio_files(folders, skip_dirs=()):
    skip = {os.path.normcase(os.path.abspath(str(d))) for d in skip_dirs if d}
    seen = set()
    for folder in folders:
        root = Path(folder)
        if not root.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            here = os.path.normcase(os.path.abspath(dirpath))
            if any(here == s or here.startswith(s + os.sep) for s in skip):
                dirnames[:] = []
                continue
            dirnames[:] = [d for d in dirnames if not d.startswith(".")
                           and d.lower() not in {"node_modules", "_duplicates", "_originals"}]
            for name in filenames:
                if Path(name).suffix.lower() in AUDIO_EXT and not name.endswith(".converting.mp3"):
                    p = str(Path(dirpath) / name)
                    key = os.path.normcase(p)
                    if key not in seen:
                        seen.add(key)
                        yield p


def analyse(path, do_fingerprint):
    """All the slow, per-file work. Runs on a worker thread; touches no database."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    tags = read_tags(path)
    return {
        "path": path, "filename": os.path.basename(path),
        "folder": os.path.dirname(path),
        "ext": Path(path).suffix.lower().lstrip("."),
        "size": st.st_size, "mtime": st.st_mtime,
        "duration": tags["duration"], "bitrate": tags["bitrate"],
        "samplerate": tags["samplerate"],
        "file_hash": file_sha256(path),
        "audio_hash": audio_stream_hash(path) if path.lower().endswith(".mp3") else None,
        "fingerprint": fingerprint(path) if do_fingerprint else None,
        "title": tags["title"], "artist": tags["artist"], "album": tags["album"],
        "albumartist": tags["albumartist"], "year": tags["year"],
        "genre": tags["genre"], "track_no": tags["track_no"], "bpm": tags["bpm"],
        "music_key": tags["music_key"], "producer": tags["producer"],
        "comment": tags["comment"], "has_art": tags["has_art"],
        "_rating": tags["rating"], "_notes": tags["notes"],
        "_user_tags": tags["user_tags"], "_era": tags["era"],
        "lyrics": (_lyr := read_lyrics_full(path))["plain"][:20000],
        "has_synced": 1 if _lyr["synced"] else 0,
        "gain_db": None,
        "missing": 0,
    }


SCAN_LOCK = threading.Lock()


def scan_library(deep=True, auto=False):
    """Only one scan at a time; a second request while one runs is dropped."""
    if not SCAN_LOCK.acquire(blocking=False):
        return False
    try:
        SCAN_STATE["auto"] = auto
        _scan_library(deep)
    finally:
        SCAN_LOCK.release()
    return True


def _scan_library(deep=True):
    cfg = load_config()
    SCAN_STATE.update(running=True, done=0, total=0, message="Listing files…")
    try:
        files = list(iter_audio_files(cfg.get("folders", []),
                                      skip_dirs=[quarantine_dir(cfg)]))
        SCAN_STATE["total"] = len(files)
        do_fp = bool(deep and cfg.get("fingerprint", True))

        conn = None
        new_paths = []
        conn = db()
        existing = {os.path.normcase(r["path"]): dict(r)
                    for r in conn.execute("SELECT * FROM tracks")}
        now = time.time()
        alive, todo = set(), []

        for path in files:
            key = os.path.normcase(path)
            alive.add(key)
            prev = existing.get(key)
            try:
                st = os.stat(path)
            except OSError:
                continue
            if (prev and prev["size"] == st.st_size
                    and abs((prev["mtime"] or 0) - st.st_mtime) < 1
                    and prev["file_hash"]):
                conn.execute("UPDATE tracks SET last_scan=?, missing=0 WHERE id=?",
                             (now, prev["id"]))
                SCAN_STATE["done"] += 1
            else:
                todo.append(path)
        conn.commit()

        # The heavy part — decoding and hashing — is I/O and numpy bound, so it
        # parallelises well and this is where nearly all the wall time goes.
        if todo:
            SCAN_STATE["message"] = "Reading %d new or changed files…" % len(todo)
            workers = max(2, min(8, (os.cpu_count() or 4)))
            lock = threading.Lock()
            with ThreadPoolExecutor(max_workers=workers) as ex:
                def safe_analyse(p):
                    try:
                        return analyse(p, do_fp)
                    except Exception:
                        return None
                for row in ex.map(safe_analyse, todo):
                    if row is None:
                        continue
                    with lock:
                        SCAN_STATE["done"] += 1
                        SCAN_STATE["message"] = row["filename"]
                    key = os.path.normcase(row["path"])
                    prev = existing.get(key)
                    rating = row.pop("_rating")
                    notes = row.pop("_notes")
                    utags = row.pop("_user_tags")
                    era = row.pop("_era")
                    row["last_scan"] = now
                    if prev:
                        row["era"] = era or prev.get("era") or ""
                        if row["fingerprint"] is None and prev["fingerprint"]:
                            row["fingerprint"] = prev["fingerprint"]
                        row["rating"] = rating or prev["rating"] or 0
                        row["notes"] = notes or prev["notes"] or ""
                        row["user_tags"] = utags or prev["user_tags"] or ""
                        cols = ", ".join("%s=?" % k for k in row)
                        conn.execute("UPDATE tracks SET %s WHERE id=?" % cols,
                                     (*row.values(), prev["id"]))
                    else:
                        row["rating"] = rating or 0
                        row["notes"] = notes or ""
                        row["user_tags"] = utags or ""
                        row["era"] = era or ""
                        row["first_seen"] = now
                        new_paths.append(row["path"])
                        conn.execute(
                            "INSERT INTO tracks (%s) VALUES (%s)" %
                            (", ".join(row.keys()), ", ".join("?" for _ in row)),
                            tuple(row.values()))
                    if SCAN_STATE["done"] % 25 == 0:
                        conn.commit()

        for key, prev in existing.items():
            if key not in alive:
                conn.execute("UPDATE tracks SET missing=1 WHERE id=?", (prev["id"],))
        conn.commit()
        if new_paths and existing:
            try:
                ids = [r["id"] for r in conn.execute(
                    "SELECT id FROM tracks WHERE path IN (%s)" % ",".join("?" * len(new_paths)), new_paths)]
                find_snippet_alerts(conn, ids)
            except Exception:
                pass
        conn.close()
        SCAN_STATE["message"] = "Scanned %d files." % len(files)
    except Exception as exc:
        SCAN_STATE["message"] = "Scan failed: %s" % (exc or type(exc).__name__)
        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass
    finally:
        SCAN_STATE["running"] = False
        SCAN_STATE["finished_at"] = time.time()


def is_snippet(row):
    hay = " ".join(str(row[k] or "") for k in ("filename", "title", "user_tags")).lower()
    return "snippet" in hay


def _base_song(row):
    return _norm_words(clean_song_title(row["title"] or Path(row["filename"]).stem))


def find_snippet_alerts(conn, new_ids=None):
    """Pair full songs with snippets of the same song that aren't grouped yet.
    new_ids: only look at these (just-scanned) tracks; None = whole library."""
    rows = [dict(r) for r in conn.execute(
        "SELECT t.id, t.title, t.filename, t.user_tags, t.artist, v.song AS song FROM tracks t "
        "LEFT JOIN versions v ON v.track_id=t.id AND v.status='grouped' WHERE t.missing=0")]
    by = {}
    for r in rows:
        b = _base_song(r)
        if b:
            by.setdefault(b, []).append(r)
    known = {r["full_id"] for r in conn.execute("SELECT full_id FROM snippet_alerts")}
    made = 0
    for b, group in by.items():
        snips = [r for r in group if is_snippet(r)]
        fulls = [r for r in group if not is_snippet(r)]
        if not snips or not fulls:
            continue
        for f in fulls:
            if new_ids is not None and f["id"] not in new_ids and not any(s_["id"] in new_ids for s_ in snips):
                continue
            if f["id"] in known:
                continue
            todo = [s_ for s_ in snips if not (s_["song"] and f["song"] and s_["song"].lower() == f["song"].lower())]
            if not todo:
                continue
            fa, sa = _norm_words(f["artist"] or ""), _norm_words(todo[0]["artist"] or "")
            if fa and sa and fa.split()[0] != sa.split()[0]:
                continue
            conn.execute("INSERT INTO snippet_alerts (full_id, snippet_ids, song, ts) VALUES (?,?,?,?)",
                         (f["id"], json.dumps([s_["id"] for s_ in todo]),
                          f["song"] or clean_song_title(f["title"] or Path(f["filename"]).stem), time.time()))
            known.add(f["id"])
            made += 1
    conn.commit()
    return made


# --------------------------------------------------------------------------
# duplicates
# --------------------------------------------------------------------------

VERSION_WORDS = re.compile(
    r"\b(final|finalfinal|master|mastered|mixdown|mix|remix|edit|version|ver|"
    r"v\d+|take\d*|draft|demo|copy|new|old|rough|bounce|export|wip|"
    r"\d{1,2}[-_]?\d{1,2}[-_]?\d{2,4})\b")


def normalise_title(name):
    s = unicodedata.normalize("NFKD", name).lower()
    s = re.sub(r"\.[a-z0-9]{2,5}$", "", s)
    s = re.sub(r"\(\s*\d+\s*\)", " ", s)
    s = re.sub(r"[\[\](){}_\-\u2014\u2013]+", " ", s)
    s = VERSION_WORDS.sub(" ", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\s+\d{2,4}\s*$", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def find_duplicates(fp_threshold=0.78):
    conn = db()
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM tracks WHERE missing=0 ORDER BY id")]
    grouped = grouped_map(conn)
    conn.close()
    # Tracks the user has grouped as versions of one song are different mixes
    # on purpose. Checks 1 and 2 (byte-identical copies) still apply, but the
    # fuzzy checks 3 and 4 must never offer to move a version out of the way.
    for r in rows:
        r["song"] = grouped.get(r["id"], "")

    groups, claimed = [], set()

    def emit(tier, label, members, confidence):
        ids = tuple(sorted(m["id"] for m in members))
        if len(ids) < 2 or ids in claimed:
            return
        claimed.add(ids)
        groups.append({"tier": tier, "label": label, "confidence": confidence,
                       "tracks": [public(m) for m in members]})

    by_file = {}
    for r in rows:
        if r["file_hash"]:
            by_file.setdefault(r["file_hash"], []).append(r)
    for m in by_file.values():
        emit(1, "Identical file", m, "certain")

    by_audio = {}
    for r in rows:
        if r["audio_hash"]:
            by_audio.setdefault(r["audio_hash"], []).append(r)
    for m in by_audio.values():
        emit(2, "Same audio, different tags", m, "certain")

    if np is not None:
        fps = [r for r in rows if r["fingerprint"] and r["id"] not in grouped]
        used = set()
        for i, a in enumerate(fps):
            if a["id"] in used:
                continue
            cluster = [a]
            for b in fps[i + 1:]:
                if b["id"] in used:
                    continue
                da, dbb = a["duration"] or 0, b["duration"] or 0
                if da and dbb and abs(da - dbb) / max(da, dbb) > 0.25:
                    continue
                if fp_similarity(a["fingerprint"], b["fingerprint"]) >= fp_threshold:
                    cluster.append(b)
            if len(cluster) > 1:
                for m in cluster:
                    used.add(m["id"])
                emit(3, "Sounds like the same song", cluster, "likely")

    buckets = {}
    for r in rows:
        if r["id"] in grouped:
            continue
        norm = normalise_title(r["title"] or r["filename"] or "")
        if not norm:
            continue
        r["_norm"] = norm
        buckets.setdefault(norm[:4], []).append(r)
    for bucket in buckets.values():
        for i, a in enumerate(bucket):
            cluster = [a]
            for b in bucket[i + 1:]:
                if SequenceMatcher(None, a["_norm"], b["_norm"]).ratio() < 0.82:
                    continue
                da, dbb = a["duration"] or 0, b["duration"] or 0
                if da and dbb and abs(da - dbb) > 3.0:
                    continue
                cluster.append(b)
            if len(cluster) > 1:
                emit(4, "Similar name and length", cluster, "possible")

    groups.sort(key=lambda g: (g["tier"], -len(g["tracks"])))
    return groups


# --------------------------------------------------------------------------
# versions — several takes of one song (demo, mixdown, final, instrumental…)
# --------------------------------------------------------------------------

# Words that mark a file as "a version of" something rather than part of the
# title. Only trusted at the END of a name or inside brackets, so a title that
# merely contains one ("Clean Slate") is left alone.
_LT = (r"(?:v\d+(?:\.\d+)?|final(?:\s*final)*|finalfinal|master(?:ed)?|mixdown|"
       r"mix(?:\s*\d+)?|remix|edit|demo|rough(?:\s+mix)?|draft|wip|bounce|export|"
       r"take\s*\d+|instrumental|inst|a\s?cappella|acapella|acap|clean|dirty|"
       r"explicit|radio\s+edit|extended|sped\s*up|slowed(?:\s*(?:\+|and|&)?\s*reverb)?|"
       r"reverb|snippet|preview|alt(?:ernate)?(?:\s+(?:mix|version|take))?|acoustic|"
       r"stems?|version\s*\d+|ver\s*\d+|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4})")
LABEL_ONLY = re.compile(r"%s(?:[\s,&+\-]+%s)*" % (_LT, _LT), re.I)
LABEL_TRAIL = re.compile(r"[\s\-\u2013\u2014.]+(%s)\s*$" % _LT, re.I)
BRACKET = re.compile(r"[\(\[\{]([^\)\]\}]*)[\)\]\}]")


def _tidy_label(label):
    label = re.sub(r"\s+", " ", label or "").strip()
    if label and label == label.lower():
        label = " ".join(w[:1].upper() + w[1:] for w in label.split())
    return label


def split_version(raw):
    """'Redline v2 (Instrumental)' -> ('Redline', 'V2 Instrumental')."""
    s_ = re.sub(r"\s+", " ", (raw or "").replace("_", " ")).strip()
    labels = []

    def take(m):
        inner = m.group(1).strip()
        if inner and LABEL_ONLY.fullmatch(inner):
            labels.append(inner)
            return " "
        return m.group(0)

    s_ = BRACKET.sub(take, s_)
    while True:
        s_ = s_.strip()
        m = LABEL_TRAIL.search(s_)
        if not m or not s_[:m.start()].strip(" -\u2013\u2014_."):
            break
        labels.insert(0, m.group(1).strip())
        s_ = s_[:m.start()]
    base = re.sub(r"\s+", " ", s_).strip(" -\u2013\u2014_.")
    return base, _tidy_label(" ".join(labels))


def song_key(name):
    s_ = unicodedata.normalize("NFKD", name or "").lower()
    return re.sub(r"[^a-z0-9]+", "", s_)


def version_info(row):
    """(song name, version label, grouping key) for one track. The title tag
    wins for the song name; the label may come from either the tag or the
    filename, so tracks that were all auto-filled to the same title still keep
    the 'v2' / 'mixdown' hint that only their filename carries."""
    title = (row.get("title") or "").strip()
    fname = parse_filename(row.get("filename") or "")["title"]
    tb, tl = split_version(title) if title else ("", "")
    fb, fl = split_version(fname)
    base = tb or fb or Path(row.get("filename") or "").stem
    return base, (tl or fl), song_key(base)


def grouped_map(conn=None):
    own = conn is None
    conn = conn or db()
    m = {r["track_id"]: r["song"] for r in conn.execute(
        "SELECT track_id, song FROM versions WHERE status='grouped'")}
    if own:
        conn.close()
    return m


def _mark_twins(tracks):
    """Flag tracks inside one group that are byte- or audio-identical to
    another member: those are copies, not versions."""
    seen = {}
    for t in tracks:
        for k in ("file_hash", "audio_hash"):
            if t.get(k):
                seen.setdefault((k, t[k]), []).append(t["id"])
    twins = {i for ids in seen.values() if len(ids) > 1 for i in ids}
    for t in tracks:
        t["twin"] = t["id"] in twins


def _order(tracks):
    tracks.sort(key=lambda t: (t.get("mtime") or 0, (t.get("filename") or "").lower()))
    return tracks


def build_versions():
    conn = db()
    rows = [dict(r) for r in conn.execute("SELECT * FROM tracks WHERE missing=0")]
    vrows = {r["track_id"]: dict(r) for r in conn.execute("SELECT * FROM versions")}
    conn.close()
    by_id = {r["id"]: r for r in rows}

    saved = {}
    for tid, v in vrows.items():
        if v["status"] != "grouped" or tid not in by_id:
            continue
        t = public(by_id[tid])
        t["label"] = v["label"] or ""
        saved.setdefault(v["song"].lower(), {"song": v["song"], "tracks": []})["tracks"].append(t)
    saved_by_key = {song_key(g["song"]): g["song"] for g in saved.values()}

    pending = {}
    for r in rows:
        if r["id"] in vrows:            # grouped already, or dismissed
            continue
        base, label, key = version_info(r)
        if not key:
            continue
        t = public(r)
        t["label"], t["base"] = label, base
        pending.setdefault(key, []).append(t)

    suggestions = []
    for key, ts in pending.items():
        if key in saved_by_key:
            suggestions.append({"song": saved_by_key[key], "existing": True,
                                "tracks": _order(ts)})
        elif len({t.get("audio_hash") or t.get("file_hash") or t["id"] for t in ts}) >= 2:
            # needs two genuinely different recordings: exact copies of one
            # file are a job for the Duplicates tab, not versions
            names = {}
            for t in ts:
                names[t["base"]] = names.get(t["base"], 0) + 1
            song = sorted(names, key=lambda n: (-names[n], n))[0]
            suggestions.append({"song": song, "existing": False, "tracks": _order(ts)})

    groups = sorted(saved.values(), key=lambda g: g["song"].lower())
    for g in groups:
        _order(g["tracks"])
    suggestions.sort(key=lambda g: g["song"].lower())
    for g in groups + suggestions:
        _mark_twins(g["tracks"])
    return {"groups": groups, "suggestions": suggestions}


def version_titles(song, tracks):
    """Distinct, readable titles for one song's versions.
    {track_id: 'Song (Label)'}. A single unlabelled take keeps the plain name;
    several unlabelled takes become Version 1, Version 2… oldest first."""
    unlabeled = [t for t in tracks if not (t.get("label") or "").strip()]
    out, used, n_un = {}, {}, 0
    for t in tracks:
        label = (t.get("label") or "").strip()
        if not label and len(unlabeled) > 1:
            n_un += 1
            label = "Version %d" % n_un
        title = "%s (%s)" % (song, label) if label else song
        k = title.lower()
        if k in used:
            used[k] += 1
            title = "%s (%s %d)" % (song, label or "Version", used[k])
        else:
            used[k] = 1
        out[t["id"]] = title
    return out


# --------------------------------------------------------------------------
# lyrics
# --------------------------------------------------------------------------

LRC_TS = re.compile(r"\[(\d{1,3}):(\d{1,2}(?:[.:]\d{1,3})?)\]")
LRC_META = re.compile(r"^\s*\[(ar|ti|al|au|by|length|re|ve|offset|#)\s*:[^\]]*\]\s*$", re.I)


def parse_lrc(text):
    """'[01:02.50]line' text -> [(62.5, 'line'), ...] sorted, or None if untimed."""
    if not text or not LRC_TS.search(text):
        return None
    offset = 0.0
    m = re.search(r"\[offset\s*:\s*([+-]?\d+)\s*\]", text, re.I)
    if m:
        offset = int(m.group(1)) / 1000.0
    out = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        stamps = list(LRC_TS.finditer(raw))
        if not stamps:
            continue
        line = LRC_TS.sub("", raw).strip()
        for st in stamps:
            mins = int(st.group(1))
            secs = float(st.group(2).replace(":", "."))
            out.append((max(0.0, round(mins * 60 + secs - offset, 3)), line))
    out.sort(key=lambda x: x[0])
    return out or None


def to_lrc(lines):
    rows = []
    for t, line in lines:
        t = max(0.0, float(t))
        rows.append("[%02d:%05.2f]%s" % (int(t // 60), t - 60 * int(t // 60), line))
    return "\n".join(rows)


WORDS_TAG = "AURA_WORDS"
_ELRC_LINE = re.compile(r"^\[(\d{1,3}):(\d{1,2}(?:\.\d{1,3})?)\](.*)$")
_ELRC_WORD = re.compile(r"<(\d{1,3}):(\d{1,2}(?:\.\d{1,3})?)>([^<]*)")


def _lrc_ts(t):
    t = max(0.0, float(t))
    return "%02d:%05.2f" % (int(t // 60), t - 60 * int(t // 60))


def words_to_elrc(synced, words, ends=None):
    """Word-by-word timings as enhanced LRC: [00:12.30]<00:12.30>I <00:12.52>been <00:13.10>
    (the last, empty stamp is where the line ends)."""
    rows = []
    for i, (t, line) in enumerate(synced):
        ws = words[i] if words and i < len(words) else None
        if not ws:
            rows.append("[%s]" % _lrc_ts(t))
            continue
        body = "".join("<%s>%s " % (_lrc_ts(wt), str(w).replace("<", "").replace(">", "")) for wt, w in ws)
        end = ends[i] if ends and i < len(ends) and ends[i] else None
        rows.append("[%s]%s%s" % (_lrc_ts(t), body.rstrip(" ") if end is None else body, ("<%s>" % _lrc_ts(end)) if end is not None else ""))
    return "\n".join(rows)


def parse_elrc(text):
    """-> [(line_t, [(t, word), ...], end or None), ...]"""
    out = []
    for raw in (text or "").replace("\r\n", "\n").split("\n"):
        m = _ELRC_LINE.match(raw.strip())
        if not m:
            continue
        lt = int(m.group(1)) * 60 + float(m.group(2))
        ws, end = [], None
        for mm in _ELRC_WORD.finditer(m.group(3)):
            wt = int(mm.group(1)) * 60 + float(mm.group(2))
            w = mm.group(3).strip()
            if w:
                ws.append((round(wt, 3), w))
            else:
                end = round(wt, 3)
        out.append((round(lt, 3), ws, end))
    return out


def _words_for(synced, raw):
    """Match stored word timings to the timed lines; anything that no longer fits
    (the lyrics were edited or re-timed elsewhere) is dropped line by line."""
    if not synced or not raw:
        return None, None
    el = parse_elrc(raw)
    if len(el) != len(synced):
        return None, None
    squash = lambda x: re.sub(r"\s+", " ", x or "").strip()
    words, ends, any_ = [], [], False
    for (t, line), (lt, ws, end) in zip(synced, el):
        if ws and abs(lt - t) < 0.06 and squash(" ".join(w for _, w in ws)) == squash(line):
            words.append(ws)
            ends.append(end)
            any_ = True
        else:
            words.append(None)
            ends.append(None)
    return (words, ends) if any_ else (None, None)


def _read_words_tag(tags):
    try:
        if isinstance(tags, ID3):
            fr = tags.getall("TXXX:" + WORDS_TAG)
            return str(fr[0].text[0]) if fr and fr[0].text else ""
        for key in ("----:com.apple.iTunes:" + WORDS_TAG, WORDS_TAG, WORDS_TAG.lower()):
            try:
                v = tags.get(key)
            except Exception:
                v = None
            if v:
                v = v[0] if isinstance(v, list) else v
                return bytes(v).decode("utf-8", "replace") if isinstance(v, (bytes, bytearray)) else str(v)
    except Exception:
        pass
    return ""


def plain_from_lrc(text):
    keep = []
    for raw in (text or "").replace("\r\n", "\n").split("\n"):
        if LRC_META.match(raw):
            continue
        keep.append(LRC_TS.sub("", raw).rstrip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(keep)).strip()


def sidecar_lrc(path):
    p = Path(path)
    for cand in (p.with_suffix(".lrc"), p.with_suffix(".LRC")):
        if cand.exists():
            return cand
    return None


def _read_text_file(p):
    raw = Path(p).read_bytes()
    for enc in ("utf-8-sig", "utf-16", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def read_lyrics_full(path):
    """{'plain': text, 'synced': [(seconds, line)] or None, 'source': where synced came from,
    'words': per line [(seconds, word)] or None, 'ends': per line end time or None}."""
    plain, synced, source = "", None, ""
    words_raw = ""
    try:
        mf = MutagenFile(path)
    except Exception:
        mf = None
    tags = getattr(mf, "tags", None) if mf is not None else None
    if tags is not None:
        words_raw = _read_words_tag(tags)
        if isinstance(tags, ID3):
            sy = tags.getall("SYLT")
            if sy:
                fr = sy[0]
                unit = 1000.0 if getattr(fr, "format", 2) == 2 else None
                if unit and fr.text:
                    synced = [(round(ts / unit, 3), str(txt).strip()) for txt, ts in fr.text]
                    synced.sort(key=lambda x: x[0])
                    source = "file"
            us = tags.getall("USLT")
            if us:
                plain = str(us[0].text or "").strip()
        else:
            for key in ("\xa9lyr", "lyrics", "LYRICS", "unsyncedlyrics", "UNSYNCEDLYRICS",
                        "syncedlyrics", "SYNCEDLYRICS"):
                try:
                    v = tags.get(key)
                except Exception:
                    v = None
                if v:
                    plain = (str(v[0]) if isinstance(v, list) else str(v)).strip()
                    break
    if plain and LRC_TS.search(plain):
        timed = parse_lrc(plain)
        if timed and not synced:
            synced, source = timed, "file"
        plain = plain_from_lrc(plain)
    if not synced:
        side = sidecar_lrc(path)
        if side:
            try:
                txt = _read_text_file(side)
                timed = parse_lrc(txt)
                if timed:
                    synced, source = timed, "lrc"
                    if not plain:
                        plain = plain_from_lrc(txt)
            except OSError:
                pass
    if synced and not plain:
        plain = "\n".join(line for _, line in synced)
    words, ends = (None, None)
    if synced and source == "file":
        words, ends = _words_for(synced, words_raw)
    return {"plain": plain, "synced": synced, "source": source, "words": words, "ends": ends}


def read_lyrics(path):
    return read_lyrics_full(path)["plain"]


def write_lyrics(path, text, synced=None, words=None, ends=None):
    """Plain lyrics always; timed lines too when given. MP3 keeps both side by
    side (USLT + SYLT); other formats store LRC text in their lyrics tag.
    words/ends: per-line word timings, kept in their own AURA_WORDS tag so other
    players still see ordinary line-by-line lyrics."""
    text = (text or "").replace("\r\n", "\n").strip()
    if synced and not text:
        text = "\n".join(line for _, line in synced)
    ext = Path(path).suffix.lower()
    wtext = words_to_elrc(synced, words, ends) if (synced and words and any(words)) else ""
    if ext in (".wav", ".aif", ".aiff"):      # these carry an ID3 tag inside, same as MP3
        wf = MutagenFile(path)
        if wf is None:
            return False
        if wf.tags is None:
            wf.add_tags()
        wf.tags.delall("USLT")
        wf.tags.delall("SYLT")
        if text:
            wf.tags.add(USLT(encoding=3, lang="eng", desc="", text=text))
        if synced:
            wf.tags.add(SYLT(encoding=3, lang="eng", format=2, type=1, desc="",
                             text=[(line, int(round(t * 1000))) for t, line in synced]))
        wf.tags.delall("TXXX:" + WORDS_TAG)
        if wtext:
            wf.tags.add(TXXX(encoding=3, desc=WORDS_TAG, text=[wtext]))
        wf.save()
        return True
    if ext == ".mp3":
        try:
            tags = ID3(path)
        except ID3NoHeaderError:
            tags = ID3()
        tags.delall("USLT")
        tags.delall("SYLT")
        if text:
            tags.add(USLT(encoding=3, lang="eng", desc="", text=text))
        if synced:
            tags.add(SYLT(encoding=3, lang="eng", format=2, type=1, desc="",
                          text=[(line, int(round(t * 1000))) for t, line in synced]))
        tags.delall("TXXX:" + WORDS_TAG)
        if wtext:
            tags.add(TXXX(encoding=3, desc=WORDS_TAG, text=[wtext]))
        tags.save(path, v2_version=3)
        return True
    mf = MutagenFile(path)
    if mf is None:
        return False
    if mf.tags is None:
        try:
            mf.add_tags()
        except Exception:
            return False
    is_mp4 = ext in (".m4a", ".mp4", ".aac")
    key = "\xa9lyr" if is_mp4 else "LYRICS"
    wkey = ("----:com.apple.iTunes:" + WORDS_TAG) if is_mp4 else WORDS_TAG
    value = to_lrc(synced) if synced else text
    try:
        if value:
            mf.tags[key] = [value]
        elif key in mf.tags:
            del mf.tags[key]
        if wtext:
            mf.tags[wkey] = [wtext.encode("utf-8")] if is_mp4 else [wtext]
        elif wkey in mf.tags:
            del mf.tags[wkey]
        mf.save()
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------
# online lyrics (LRCLIB — free, no account)
# --------------------------------------------------------------------------

LRCLIB_URL = "https://lrclib.net"
HTTP_UA = "AURA music player (desktop)"


def http_json(url, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": HTTP_UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def lrclib_lookup(title, artist, album="", duration=0):
    """Best match from LRCLIB or None. Raises OSError when offline."""
    q = {"track_name": title, "artist_name": artist}
    if album:
        q["album_name"] = album
    if duration:
        q["duration"] = int(round(duration))
    try:
        hit = http_json(LRCLIB_URL + "/api/get?" + urllib.parse.urlencode(q))
        if hit and (hit.get("syncedLyrics") or hit.get("plainLyrics")):
            return hit
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
    res = http_json(LRCLIB_URL + "/api/search?" + urllib.parse.urlencode(
        {"track_name": title, "artist_name": artist}))
    res = [r for r in (res or []) if r.get("syncedLyrics") or r.get("plainLyrics")]
    if not res:
        return None
    res.sort(key=lambda r: (not r.get("syncedLyrics"), abs((r.get("duration") or 0) - (duration or 0))))
    best = res[0]
    if duration and best.get("duration") and abs(best["duration"] - duration) > 12:
        return None
    return best


# --------------------------------------------------------------------------
# lyrics finder — several sources, picked by you (helps a lot with unreleased songs)
# --------------------------------------------------------------------------

GENIUS_URL = "https://genius.com"
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
LEAK_WORDS = re.compile(r"\b(snippet|cdq|hq|lq|og|leak(?:ed)?|unreleased|unrel|unr|full|"
                        r"remaster(?:ed)?|v\d+|version\s*\d+|session|sessions|demo|"
                        r"rough|reference|ref|throwaway|alt|alternate)\b", re.I)


def clean_song_title(title):
    """'Bad Energy (prod. Nick Mira) [v2] {CDQ}' -> 'Bad Energy'."""
    t = split_version(title or "")[0] or title or ""
    t = re.sub(r"[\(\[\{][^\)\]\}]*(?:prod|feat|ft\.|with|w/|snippet|cdq|leak|v\d|og|session|unrel)[^\)\]\}]*[\)\]\}]", " ", t, flags=re.I)
    t = re.sub(r"\b(?:feat|ft|featuring|prod(?:uced)?(?:\s+by)?)\.?\s.*$", " ", t, flags=re.I)
    t = LEAK_WORDS.sub(" ", t)
    t = re.sub(r"[\(\[\{]\s*[\)\]\}]", " ", t)
    return re.sub(r"\s+", " ", t).strip(" -_.")


def _sim(a, b):
    a, b = _norm_words(a), _norm_words(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def http_text(url, timeout=12, ua=BROWSER_UA):
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "text/html,application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def lrclib_search(q):
    res = http_json(LRCLIB_URL + "/api/search?" + urllib.parse.urlencode({"q": q}), timeout=8) or []
    out = []
    for r in res[:15]:
        if not (r.get("plainLyrics") or r.get("syncedLyrics")):
            continue
        timed = parse_lrc(r.get("syncedLyrics") or "")
        out.append({"source": "lrclib", "title": r.get("trackName") or "", "artist": r.get("artistName") or "",
                    "album": r.get("albumName") or "", "duration": r.get("duration") or 0,
                    "synced": [{"t": t, "text": line} for t, line in timed] if timed else None,
                    "lyrics": (r.get("plainLyrics") or plain_from_lrc(r.get("syncedLyrics") or "")).strip()})
    return out


def genius_search(q, by_lyrics=False):
    data = json.loads(http_text(GENIUS_URL + "/api/search/multi?" + urllib.parse.urlencode({"per_page": 5, "q": q}),
                                timeout=8))
    out, seen = [], set()
    kinds = ("song", "lyric") if by_lyrics else ("song",)
    for sec in (data.get("response") or {}).get("sections") or []:
        for hit in sec.get("hits") or []:
            if hit.get("type") not in kinds and sec.get("type") not in kinds:
                continue
            r = hit.get("result") or {}
            url = r.get("url") or ""
            if not url or url in seen:
                continue
            seen.add(url)
            out.append({"source": "genius", "title": r.get("title") or "",
                        "artist": (r.get("primary_artist") or {}).get("name") or r.get("artist_names") or "",
                        "album": "", "duration": 0, "synced": None, "lyrics": None, "url": url})
    return out


class _GeniusLyrics(HTMLParser):
    """Collects the text of Genius's lyrics containers."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0          # >0 while inside a lyrics container
        self.skip = 0           # >0 while inside a part Genius marks as not-lyrics
        self.parts = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if self.depth:
            if tag == "br":
                self.parts.append("\n")
                return
            if tag in ("div", "span", "a", "i", "b", "em", "strong", "p"):
                self.depth += 1
                if self.skip or a.get("data-exclude-from-selection") == "true":
                    self.skip += 1
        elif tag == "div" and a.get("data-lyrics-container") == "true":
            self.depth = 1
            if self.parts:
                self.parts.append("\n")

    def handle_startendtag(self, tag, attrs):
        if self.depth and tag == "br":
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if self.depth and tag in ("div", "span", "a", "i", "b", "em", "strong", "p"):
            if self.skip:
                self.skip -= 1
            self.depth -= 1

    def handle_data(self, data):
        if self.depth and not self.skip:
            self.parts.append(data)


def genius_lyrics(url):
    host = urllib.parse.urlparse(url).hostname or ""
    if not (host == "genius.com" or host.endswith(".genius.com")) and not url.startswith(GENIUS_URL):
        raise ValueError("not a Genius link")
    parser = _GeniusLyrics()
    parser.feed(http_text(url))
    text = "".join(parser.parts)
    lines = []
    for ln in text.split("\n"):
        ln = ln.strip()
        if re.fullmatch(r"\[[^\]]*\]", ln):     # [Verse 1], [Chorus: ...] aren't sung
            if lines and lines[-1] != "":
                lines.append("")
            continue
        lines.append(ln)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def find_lyrics(row, query=None):
    title = clean_song_title(row["title"] or Path(row["filename"]).stem)
    artist = (row["artist"] or "").split(",")[0].split(" & ")[0]
    artist = re.split(r"\s+(?:feat|ft)\.?\s", artist, flags=re.I)[0].strip()
    queries = [query.strip()] if query and query.strip() else [q for q in (
        ("%s %s" % (artist, title)).strip(), title) if q]
    jobs = []
    for k, q in enumerate(dict.fromkeys(queries)):
        jobs += [("lrclib#%d" % k, lrclib_search, q), ("genius#%d" % k, genius_search, q)]
    got, errors = run_parallel(jobs, limit=9.0)
    errors = [e.replace("#0", "").replace("#1", "") for e in errors]
    results = []
    for name, _, _ in jobs:
        results += got.get(name) or []
    dur = row["duration"] or 0
    seen, out = set(), []
    for r in results:
        key = (r["source"], _norm_words(r["title"]), _norm_words(r["artist"]))
        if key in seen:
            continue
        seen.add(key)
        sc = _sim(r["title"], title) * 2 + (_sim(r["artist"], artist) if artist else 0.5)
        if r["synced"]:
            sc += 0.3
        if dur and r.get("duration"):
            sc -= min(1.0, abs(r["duration"] - dur) / 30.0)
        r["score"] = round(sc, 3)
        out.append(r)
    out.sort(key=lambda r: -r["score"])
    return {"query": queries[0], "results": out[:10],
            "offline": bool(errors) and not results, "errors": errors[:4]}


# --------------------------------------------------------------------------
# auto-sync — speech recognition times the lyrics to the song
# --------------------------------------------------------------------------

AUTOSYNC_MODELS = {"base": 145_000_000, "small": 484_000_000, "large-v3-turbo": 1_620_000_000}
AS = {"jobs": [], "lock": threading.Lock(), "wake": threading.Event(), "worker": None,
      "model": None, "model_name": None, "downloading": None, "next_id": 1}


def autosync_engine():
    """Is the speech engine there? Checked without loading it (loading takes seconds)."""
    if AS.get("engine") is None:
        import importlib.util
        def there(name):
            if sys.modules.get(name) is not None:
                return True
            try:
                return importlib.util.find_spec(name) is not None
            except Exception:
                return False
        ok = there("faster_whisper") and (there("ctranslate2") or "faster_whisper" in sys.modules)
        AS["engine"] = ok
    return (True, "") if AS["engine"] else (False, "The speech engine isn't included in this build.")


def model_dir():
    d = DATA_DIR / "models"
    d.mkdir(parents=True, exist_ok=True)
    return d


MODEL_REPOS = {"base": "Systran/faster-whisper-base", "small": "Systran/faster-whisper-small",
               "large-v3-turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo"}


def _model_folder(name):
    repo = MODEL_REPOS.get(name) or ("Systran/faster-whisper-" + name)
    return model_dir() / ("models--" + repo.replace("/", "--"))


def model_ready(name):
    return any(_model_folder(name).glob("snapshots/*/model.bin"))


def _dir_size(p):
    total = 0
    for root, _, files in os.walk(p):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def get_whisper(name):
    if AS["model"] is not None and AS["model_name"] == name:
        return AS["model"]
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    from faster_whisper import WhisperModel
    AS["model"] = None
    if not model_ready(name):
        AS["downloading"] = name
    try:
        cores = os.cpu_count() or 4
        threads = max(2, min(12, cores // 2 if cores > 8 else cores))
        m = WhisperModel(name, device="cpu", compute_type="int8", cpu_threads=threads,
                         download_root=str(model_dir()))
    finally:
        AS["downloading"] = None
    AS["model"], AS["model_name"] = m, name
    return m


def _tok(text):
    t = unicodedata.normalize("NFKD", text or "").lower().replace("’", "'")
    t = re.sub(r"'", "", t)
    return re.findall(r"[a-z0-9]+", t)


def align_words(lines, words):
    """lines: lyric lines (str). words: [(word, start, end), ...] from speech recognition.
    Returns ([{"t", "text", "guessed", "words": [(t, word)], "end"}], confidence 0..1)."""
    disp = [ln.split() for ln in lines]
    L = []                                   # (token, line, token index in line, word index in line)
    for i, dws in enumerate(disp):
        k = 0
        for wi, dw in enumerate(dws):
            for tk in _tok(dw):
                L.append((tk, i, k, wi))
                k += 1
    W = [(tk, st, en) for w, st, en in words for tk in _tok(w)]
    n, m = len(L), len(W)
    blank = lambda ln, i: {"t": None, "text": ln, "guessed": True, "words": None, "end": None}
    if not lines:
        return [], 0.0
    if not n or not m:
        return [blank(ln, i) for i, ln in enumerate(lines)], 0.0
    cache = {}

    def score(a, b):
        if a == b:
            return 2.0
        key = (a, b)
        if key not in cache:
            cache[key] = 1.0 if (len(a) > 2 and len(b) > 2 and SequenceMatcher(None, a, b).ratio() >= 0.75) else -1.0
        return cache[key]

    GAP = -0.6
    # Needleman-Wunsch, free gaps at the ends of the transcript (intros, outros, ad-libs)
    prev = [0.0] * (m + 1)
    back = []
    for i in range(1, n + 1):
        cur = [prev[0] + GAP] + [0.0] * m
        bi = bytearray(m + 1)
        bi[0] = 1
        a = L[i - 1][0]
        last = i == n
        for j in range(1, m + 1):
            d = prev[j - 1] + score(a, W[j - 1][0])
            u = prev[j] + GAP
            lft = cur[j - 1] + (0.0 if last else GAP)
            if d >= u and d >= lft:
                cur[j], bi[j] = d, 0
            elif u >= lft:
                cur[j], bi[j] = u, 1
            else:
                cur[j], bi[j] = lft, 2
        back.append(bi)
        prev = cur
    i, j = n, max(range(m + 1), key=lambda jj: prev[jj])
    match = {}
    while i > 0 and j > 0:
        mv = back[i - 1][j]
        if mv == 0:
            if score(L[i - 1][0], W[j - 1][0]) > 0:
                match[i - 1] = j - 1
            i, j = i - 1, j - 1
        elif mv == 1:
            i -= 1
        else:
            j -= 1
    # a time for each line from its first matched word, adjusted for words before it
    per_line, counts, matched_in = {}, [0] * len(lines), [0] * len(lines)
    wtime = [dict() for _ in lines]          # line -> {word index: start}
    last_end = [None] * len(lines)
    for li, (tk, line, k, wi) in enumerate(L):
        counts[line] += 1
        if li in match:
            st, en = W[match[li]][1], W[match[li]][2]
            matched_in[line] += 1
            if line not in per_line:
                per_line[line] = max(0.0, st - k * 0.28)
            wtime[line].setdefault(wi, st)
            last_end[line] = en
    times = [per_line.get(i) for i in range(len(lines))]
    anchors = [i for i, t in enumerate(times) if t is not None]
    guessed = [times[i] is None or (counts[i] and matched_in[i] / counts[i] < 0.3) for i in range(len(lines))]
    if not anchors:
        return [blank(ln, i) for i, ln in enumerate(lines)], 0.0
    first, last = anchors[0], anchors[-1]
    for i in range(first - 1, -1, -1):
        times[i] = max(0.0, times[i + 1] - 2.5)
    for i in range(last + 1, len(lines)):
        times[i] = times[i - 1] + 2.5
    for a, b in zip(anchors, anchors[1:]):
        if b - a > 1:
            span = max(0.0, times[b] - times[a])
            w = [max(1, counts[k]) for k in range(a, b)]
            tot = float(sum(w))
            acc = 0.0
            for k in range(a + 1, b):
                acc += w[k - 1 - a]
                times[k] = times[a] + span * acc / tot
    for i in range(1, len(times)):          # never go backwards
        if times[i] < times[i - 1] + 0.2:
            times[i] = times[i - 1] + 0.2
    out = []
    for i, ln in enumerate(lines):
        nxt = times[i + 1] if i + 1 < len(lines) else None
        end = last_end[i] if last_end[i] is not None and last_end[i] > times[i] else None
        ws, end = spread_words(disp[i], wtime[i], times[i], nxt, end)
        out.append({"t": round(times[i], 2), "text": ln, "guessed": bool(guessed[i]), "words": ws, "end": end})
    return out, round(len(match) / float(n), 3)


def spread_words(dws, known, start, nxt, end=None):
    """Times for every word of a line. known: {word index: time} for the words that were
    heard; the rest are placed between their neighbours by length."""
    if not dws:
        return None, None
    limit = (nxt - 0.05) if nxt is not None else None
    if end is None:
        last_known = max(known.values()) if known else start
        end = max(last_known + 0.4, start + 0.32 * len(dws))
    if limit is not None:
        end = min(end, limit)
    end = max(end, start + 0.1)
    pts = {0: start}
    for wi, t in known.items():
        if 0 < wi < len(dws) and start < t < end:
            pts[wi] = t
    pts[len(dws)] = end
    keys = sorted(pts)
    # drop anything out of order
    clean, lastt = [], -1.0
    for k in keys:
        if pts[k] > lastt or k == 0:
            clean.append(k)
            lastt = pts[k]
    if clean[-1] != len(dws):
        clean.append(len(dws))
        pts[len(dws)] = max(end, lastt + 0.1)
    ts = [0.0] * len(dws)
    for a, b in zip(clean, clean[1:]):
        w = [max(1, len(x)) for x in dws[a:b]]
        tot, acc = float(sum(w)), 0.0
        for k in range(a, b):
            ts[k] = pts[a] + (pts[b] - pts[a]) * acc / tot
            acc += w[k - a]
    return [(round(t, 2), w) for t, w in zip(ts, dws)], round(pts[len(dws)], 2)


def align_lyrics(lines, words):
    """Line times only (older callers). words: [(word, start), ...]."""
    out, conf = align_words(lines, [(w, st, st + 0.25) for w, st in words])
    return [(o["t"], o["text"], o["guessed"]) for o in out], conf


def refine_forced(model, audio, out, language, should_stop=lambda: False, on_progress=None):
    """Second, precise pass. The first pass only tells us roughly where each line is and
    can mishear words; here the real lyric words are laid over the audio one window at a
    time (forced alignment), which times every word, including the misheard ones.
    Changes `out` in place. Returns (windows accepted, windows tried)."""
    from faster_whisper.tokenizer import Tokenizer
    from faster_whisper.audio import pad_or_trim
    sr = 16000
    tok = Tokenizer(model.hf_tokenizer, model.model.is_multilingual, task="transcribe",
                    language=(language or "en"))
    total = len(audio) / float(sr)
    idx = [i for i, o in enumerate(out) if o["t"] is not None and o["text"].split()]
    accepted = tried = 0
    pos = 0
    while pos < len(idx):
        if should_stop():
            break
        first = idx[pos]
        ws = max(0.0, out[first]["t"] - 0.8)
        group, ntok, toks = [], 0, []
        while pos < len(idx):
            i = idx[pos]
            if group and (out[i]["t"] - ws > 22.0 or ntok > 190):
                break
            ids = tok.encode(" " + " ".join(out[i]["text"].split()))
            ids = [t for t in ids if t < tok.eot]
            if group and ntok + len(ids) > 220:
                break
            group.append(i)
            toks.append(ids)
            ntok += len(ids)
            pos += 1
        nxt_i = idx[pos] if pos < len(idx) else None
        we = min(total, ws + 30.0, (out[nxt_i]["t"] + 0.6) if nxt_i is not None else total)
        if we - ws < 0.5 or not ntok:
            continue
        tried += 1
        chunk = audio[int(ws * sr):int(we * sr)]
        feats = model.feature_extractor(chunk)
        frames = min(feats.shape[-1], 3000)
        enc = model.encode(pad_or_trim(feats))
        flat = [t for ids in toks for t in ids]
        res = model.find_alignment(tok, [flat], enc, frames)
        res = res[0] if res and isinstance(res[0], list) else res
        if on_progress:
            on_progress(min(1.0, we / total) if total else 1.0)
        if not res:
            continue
        # fold the model's word pieces back onto our own words, by letters
        pieces = [(str(r["word"]), float(r["start"]) + ws, float(r["end"]) + ws, float(r.get("probability", 0))) for r in res]
        strip = lambda x: re.sub(r"\s+", "", x)
        pi, new, ok = 0, {}, True
        for i in group:
            lw = []
            for dw in out[i]["text"].split():
                need, got, st, en, pr = len(strip(dw)), 0, None, None, []
                while pi < len(pieces) and got < need:
                    w_, s_, e_, p_ = pieces[pi]
                    got += len(strip(w_))
                    st = s_ if st is None else st
                    en = e_
                    pr.append(p_)
                    pi += 1
                if st is None:
                    ok = False
                    break
                lw.append((st, en, dw, sum(pr) / len(pr) if pr else 0.0))
            if not ok:
                break
            new[i] = lw
        if not ok or not new:
            continue
        # Trust check: where the first pass clearly heard a line, both passes must agree.
        diffs = [abs(new[i][0][0] - out[i]["t"]) for i in group if i in new and not out[i]["guessed"]]
        if diffs and sorted(diffs)[len(diffs) // 2] > 1.5:
            continue
        accepted += 1
        for i in group:
            lw = new.get(i)
            if not lw:
                continue
            nxt = out[i + 1]["t"] if i + 1 < len(out) and out[i + 1]["t"] is not None else None
            if out[i]["guessed"] and (sum(x[3] for x in lw) / len(lw)) < 0.2:
                continue                      # still unsure: keep the estimate and the flag
            ts, lastt = [], -1.0
            for st, en, dw, pr in lw:
                st = max(st, lastt + 0.02)
                ts.append((round(st, 2), dw))
                lastt = st
            end = max(lw[-1][1], ts[-1][0] + 0.1)
            if nxt is not None and i + 1 in new:
                end = min(end, new[i + 1][0][0])
            out[i]["t"] = ts[0][0]
            out[i]["words"] = ts
            out[i]["end"] = round(end, 2)
            if out[i]["guessed"] and (sum(x[3] for x in lw) / len(lw)) >= 0.35:
                out[i]["guessed"] = False
    # keep everything in order after the per-window changes
    lastt = -1.0
    for o in out:
        if o["t"] is None:
            continue
        if o["t"] <= lastt:
            shift = lastt + 0.05 - o["t"]
            o["t"] = round(o["t"] + shift, 2)
            if o["words"]:
                o["words"] = [(round(t + shift, 2), w) for t, w in o["words"]]
        lastt = o["t"]
    return accepted, tried


def match_heard_lyrics(row, heard_lines, heard_words):
    """After transcribing: look for real lyrics online that match what was heard.
    Searches by the song's name and by a few of the heard lines themselves, so it
    also finds songs whose file is named differently. Returns
    {"title", "artist", "source", "lines"} or None."""
    cands, votes = {}, {}

    def add(r, w=1):
        key = (r["source"], r.get("url") or (_norm_words(r["title"]), _norm_words(r["artist"])))
        cands.setdefault(key, r)
        votes[key] = votes.get(key, 0) + w

    try:
        for r in find_lyrics(row)["results"][:4]:
            add(r)
    except Exception:
        pass
    long_lines = sorted({ln for ln in heard_lines if 4 <= len(ln.split()) <= 14}, key=lambda x: -len(x))
    picks = long_lines[:1] + long_lines[len(long_lines) // 2:len(long_lines) // 2 + 1] + long_lines[-1:]
    jobs = [("p%d" % i, lambda q: genius_search(q, True), " ".join(ln.split()[:10]))
            for i, ln in enumerate(dict.fromkeys(picks))]
    if jobs:
        got, _ = run_parallel(jobs, limit=9.0)
        for rs in got.values():
            for r in (rs or [])[:3]:
                add(r, 2)
    order = sorted(cands, key=lambda k: -votes[k])[:5]
    need = [(str(i), genius_lyrics, cands[k]["url"]) for i, k in enumerate(order)
            if cands[k].get("lyrics") is None and cands[k].get("url")]
    if need:
        got, _ = run_parallel(need, limit=10.0)
        for i, k in enumerate(order):
            if str(i) in got:
                cands[k]["lyrics"] = got[str(i)]
    n_heard = sum(len(_tok(w)) for w, _, _ in heard_words) or 1
    best = None
    for k in order:
        text = cands[k].get("lyrics") or ""
        lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
        if len(lines) < 2:
            continue
        out, conf = align_words(lines, heard_words)
        n_lyr = sum(len(_tok(ln)) for ln in lines) or 1
        cover = conf * n_lyr / n_heard            # how much of what was heard is in these lyrics
        if cover < 0.5:
            continue
        if conf < 0.5:
            # the file is only part of the song (a snippet): keep the part that was heard
            hit = [i for i, o in enumerate(out) if not o["guessed"]]
            if not hit:
                continue
            lines = lines[hit[0]:hit[-1] + 1]
        score = cover + conf * 0.25
        if best is None or score > best[0]:
            best = (score, {"title": cands[k]["title"], "artist": cands[k]["artist"],
                            "source": "Genius" if cands[k]["source"] == "genius" else "LRCLIB", "lines": lines})
    return best[1] if best else None


def words_to_lines(words, max_words=10, gap=0.6):
    """Turn transcribed words into lyric lines by pauses, punctuation and length.
    -> [(t, text, [(t, word)], end)]"""
    out, cur = [], []

    def flush():
        if cur:
            txt = re.sub(r"\s+([,.?!])", r"\1", " ".join(w for w, _, _ in cur)).strip()
            if txt:
                txt = txt[0].upper() + txt[1:]
                ws = [(round(st, 2), w) for w, st, _ in cur]
                ws[0] = (ws[0][0], txt.split()[0]) if txt.split() else ws[0]
                out.append((round(cur[0][1], 2), txt, ws if len(ws) == len(txt.split()) else None, round(cur[-1][2], 2)))
        cur.clear()

    last_end = None
    for w, st, en in words:
        w = w.strip()
        if not w:
            continue
        pause = (st - last_end) if last_end is not None else 0.0
        if cur and (pause > gap or len(cur) >= max_words or
                    (len(cur) >= 5 and pause > 0.3) or (len(cur) >= 6 and cur[-1][0][-1:] == ",")):
            flush()
        cur.append((w, st, en))
        last_end = en
        if w[-1:] in ".?!" and len(cur) >= 3:
            flush()
    flush()
    return out


def _job_public(j):
    return {k: j.get(k) for k in ("id", "track_id", "title", "state", "progress", "mode", "confidence",
                                  "error", "saved", "autosave", "result", "note")}


def autosync_enqueue(tid, autosave=False):
    row = _track_row(tid)
    if not row:
        return None
    with AS["lock"]:
        for j in AS["jobs"]:
            if j["track_id"] == tid and j["state"] in ("queued", "running", "loading", "matching"):
                return j
        j = {"id": AS["next_id"], "track_id": tid, "title": row["title"] or Path(row["filename"]).stem,
             "state": "queued", "progress": 0.0, "mode": None, "confidence": None, "error": "",
             "saved": False, "autosave": autosave, "result": None, "cancel": False}
        AS["next_id"] += 1
        AS["jobs"].append(j)
    if not AS["worker"] or not AS["worker"].is_alive():
        AS["worker"] = threading.Thread(target=_autosync_worker, daemon=True)
        AS["worker"].start()
    AS["wake"].set()
    return j


class _Cancelled(Exception):
    pass


def _run_autosync(j):
    row = _track_row(j["track_id"])
    if not row or not os.path.exists(row["path"]):
        raise OSError("the file is missing")
    cfg = load_config()
    lf = read_lyrics_full(row["path"])
    lyrics = lf["plain"]
    lines = [ln.strip() for ln in lyrics.split("\n") if ln.strip()]
    j["mode"] = "align" if lines else "transcribe"
    audio = decode_mono(row["path"], target_sr=16000, max_seconds=900)
    if audio is None or not len(audio):
        raise OSError("couldn't decode the audio")
    audio = np.asarray(audio, dtype=np.float32)
    dur = len(audio) / 16000.0
    j["state"] = "loading"
    name = cfg.get("autosync_model") or "small"
    if j["mode"] == "transcribe" and (cfg.get("transcribe_model") or "same") != "same":
        name = cfg["transcribe_model"]
    j["model"] = name
    model = get_whisper(name)
    j["state"] = "running"
    lang = (cfg.get("autosync_language") or "").strip().lower() or None
    precise = bool(cfg.get("autosync_precise", True))
    share = 0.7 if (precise and j["mode"] == "align") else (0.85 if j["mode"] == "transcribe" else 1.0)           # how much of the progress bar the first pass gets
    opts = dict(word_timestamps=True, vad_filter=False, condition_on_previous_text=False, language=lang)
    if j["mode"] == "align":
        # The words are already known, so this pass only has to find where they are:
        # the quickest decoding is enough.
        opts.update(beam_size=1, best_of=1, temperature=0.0,
                    initial_prompt=" ".join(" ".join(lines).split())[:220])
    else:
        hint = "Lyrics of the song %s%s:" % (clean_song_title(row["title"] or Path(row["filename"]).stem),
                                           (" by " + row["artist"].split(",")[0]) if row["artist"] else "")
        opts.update(beam_size=5, initial_prompt=hint, hallucination_silence_threshold=2.0)
    try:
        segments, info = model.transcribe(audio, **opts)
    except TypeError:                         # an older engine without one of the options
        for k in ("hallucination_silence_threshold", "best_of"):
            opts.pop(k, None)
        segments, info = model.transcribe(audio, **opts)
    words = []
    for seg in segments:
        if j["cancel"]:
            raise _Cancelled()
        j["progress"] = round(min(0.99, (seg.end or 0) / dur) * share, 3) if dur else 0.0
        for w in (seg.words or []):
            words.append((w.word, float(w.start), float(w.end)))
    def timed(lines_, lyrics_):
        """Lay known lyric lines over what was heard; then the precise pass."""
        out, conf = align_words(lines_, words)
        note = ""
        if precise and any(o["t"] is not None for o in out):
            try:
                okw, tried = refine_forced(
                    model, audio, out, lang or getattr(info, "language", None) or "en",
                    should_stop=lambda: j["cancel"],
                    on_progress=lambda f: j.__setitem__("progress", round(share + (1 - share) * min(0.99, f), 3)))
                note = "precise pass: %d of %d parts" % (okw, tried)
            except Exception as exc:
                note = "precise pass skipped (%s)" % (str(exc) or type(exc).__name__)[:120]
            if j["cancel"]:
                raise _Cancelled()
        res = {"lyrics": lyrics_, "note": note,
               "lines": [{"t": o["t"], "text": o["text"], "guessed": o["guessed"],
                          "words": [{"t": t, "w": w} for t, w in o["words"]] if o["words"] else None,
                          "end": o["end"]} for o in out]}
        return res, out, conf

    if j["mode"] == "align":
        res, out, conf = timed(lines, lyrics)
        j["confidence"], j["note"] = conf, res["note"]
        j["result"] = res
        if j["autosave"] and conf >= 0.6 and all(o["t"] is not None for o in out):
            write_lyrics(row["path"], lyrics, [(o["t"], o["text"]) for o in out],
                         [o["words"] for o in out], [o["end"] for o in out])
            _reindex_lyrics(row["id"], row["path"])
            j["saved"] = True
    else:
        lns = words_to_lines(words)
        if not lns:
            raise OSError("no singing was recognised")
        heard = {"lyrics": "\n".join(tx for _, tx, _, _ in lns), "note": "",
                 "lines": [{"t": t, "text": tx, "guessed": False,
                            "words": [{"t": wt, "w": w} for wt, w in ws] if ws else None, "end": en}
                           for t, tx, ws, en in lns]}
        j["result"] = heard
        j["note"] = ""
        if cfg.get("transcribe_lookup", True):
            j["state"] = "matching"
            try:
                found = match_heard_lyrics(row, [tx for _, tx, _, _ in lns], words)
            except Exception:
                found = None
            j["state"] = "running"
            if found and not j["cancel"]:
                res, out, conf = timed(found["lines"], "\n".join(found["lines"]))
                if all(o["t"] is not None for o in out):
                    res["found"] = {"title": found["title"], "artist": found["artist"], "source": found["source"]}
                    res["heard"] = heard
                    j["confidence"], j["note"] = conf, res["note"]
                    j["result"] = res
    j["progress"] = 1.0


def _reindex_lyrics(tid, path):
    lf = read_lyrics_full(path)
    st = os.stat(path)
    conn = db()
    conn.execute("UPDATE tracks SET lyrics=?, has_synced=?, size=?, mtime=? WHERE id=?",
                 (lf["plain"][:20000], 1 if lf["synced"] else 0, st.st_size, st.st_mtime, tid))
    conn.commit()
    conn.close()


def _autosync_worker():
    while True:
        job = None
        with AS["lock"]:
            job = next((j for j in AS["jobs"] if j["state"] == "queued"), None)
        if job is None:
            AS["wake"].clear()
            if not AS["wake"].wait(30):
                return
            continue
        try:
            _run_autosync(job)
            job["state"] = "done"
        except _Cancelled:
            job["state"] = "cancelled"
        except Exception as exc:
            job["state"] = "error"
            job["error"] = str(exc) or type(exc).__name__


# --------------------------------------------------------------------------
# volume leveling — loudness of each song, measured in the background
# --------------------------------------------------------------------------

LEVEL_TARGET_DB = -14.0


def measure_gain(path):
    """dB to add so the song plays at about the same loudness as the rest."""
    if np is None:
        return 0.0
    y = decode_mono(path, target_sr=11025, max_seconds=420)
    if y is None or not len(y):
        return 0.0
    block = 4410
    n = len(y) // block
    if n < 1:
        return 0.0
    blocks = np.asarray(y[: n * block], dtype=np.float64).reshape(n, block)
    power = (blocks ** 2).mean(axis=1)
    gated = power[power > 1e-5]  # ignore near-silence (-50 dB)
    if not len(gated):
        return 0.0
    loud = 10 * np.log10(gated.mean())
    return float(max(-12.0, min(9.0, LEVEL_TARGET_DB - loud)))


BG = {"busy": False, "levelled": 0, "analysed": 0}

KS_MAJOR = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
KS_MINOR = [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]
KEY_NAMES = ["C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"]


def _stft_mag(x, n=2048, hop=512):
    if x is None or len(x) < n * 4:
        return None
    frames = 1 + (len(x) - n) // hop
    win = np.hanning(n).astype(np.float32)
    out = []
    for i in range(0, frames, 256):
        idx = np.arange(n)[None, :] + hop * np.arange(i, min(frames, i + 256))[:, None]
        out.append(np.abs(np.fft.rfft(x[idx] * win, axis=1)).astype(np.float32))
    return np.vstack(out)


def onset_envelope(x, sr, hop=256):
    S = _stft_mag(x, 1024, hop)
    if S is None:
        return None
    S = np.log1p(S * 10)
    flux = np.maximum(0, np.diff(S, axis=0)).sum(axis=1)
    flux -= np.convolve(flux, np.ones(16) / 16, mode="same")
    return np.maximum(flux, 0).astype(np.float32)


def detect_bpm(x, sr):
    flux = onset_envelope(x, sr)
    if flux is None or len(flux) < 300:
        return None
    fps = sr / 256.0
    flux = flux - flux.mean()
    n = len(flux)
    ac = np.fft.irfft(np.abs(np.fft.rfft(flux, 2 * n)) ** 2)[:n]
    if not ac[0]:
        return None
    ac /= ac[0]

    def score(bpm):
        lag = 60 * fps / bpm
        tot = 0.0
        for k, w in ((1, 1.0), (2, 0.5), (4, 0.25)):
            l = lag * k
            if l < n - 1:
                i = int(l)
                f = l - i
                tot += w * (ac[i] * (1 - f) + ac[i + 1] * f)
        return tot

    best = max(np.arange(60, 200.5, 0.5),
               key=lambda b: score(b) * (0.4 + 0.6 * np.exp(-0.5 * (np.log2(b / 120.0) / 0.5) ** 2)))
    best = max(np.arange(best - 1, best + 1.01, 0.1), key=score)
    while best < 70:
        best *= 2
    while best > 180:
        best /= 2
    return int(round(best))


def detect_key(x, sr):
    S = _stft_mag(x, 4096, 2048)
    if S is None:
        return None
    freqs = np.fft.rfftfreq(4096, 1.0 / sr)
    sel = (freqs > 55) & (freqs < 2000)
    pc = ((np.round(12 * np.log2(freqs[sel] / 440.0)) + 9) % 12).astype(int)
    E = S[:, sel] ** 2
    E = E / (E.sum(axis=1, keepdims=True) + 1e-9)
    tot = E.sum(axis=0)
    chroma = np.array([tot[pc == k].sum() for k in range(12)])
    if chroma.sum() <= 0:
        return None
    best, best_r = None, -9.0
    for i in range(12):
        for prof, minor in ((KS_MAJOR, False), (KS_MINOR, True)):
            r = float(np.corrcoef(chroma, np.roll(np.array(prof), i))[0, 1])
            if r > best_r:
                best, best_r = (i, minor), r
    return KEY_NAMES[best[0]] + ("m" if best[1] else "")


def analyse_bpm_key(row):
    """Fill BPM and key where they're empty. Writes them into the file too."""
    need_bpm = not str(row["bpm"] or "").strip()
    need_key = not str(row["music_key"] or "").strip()
    out = {}
    if not (need_bpm or need_key) or not os.path.exists(row["path"]):
        return out
    x = decode_mono(row["path"], target_sr=11025, max_seconds=240)
    if x is None or len(x) < 11025 * 8:
        return out
    if need_bpm:
        b = detect_bpm(x, 11025)
        if b:
            out["bpm"] = str(b)
    if need_key:
        k = detect_key(x, 11025)
        if k:
            out["music_key"] = k
    return out


def background_worker(once=False):
    """Fills in what scans leave for later: a one-time lyrics index for libraries
    from before lyrics search existed, then volume levels for new or changed songs."""
    while True:
        try:
            conn = db()
            done = conn.execute("SELECT value FROM kv WHERE key='lyrics_indexed'").fetchone()
            if not done:
                rows = conn.execute("SELECT id, path FROM tracks WHERE missing=0").fetchall()
                for r in rows:
                    try:
                        lf = read_lyrics_full(r["path"])
                        conn.execute("UPDATE tracks SET lyrics=?, has_synced=? WHERE id=?",
                                     (lf["plain"][:20000], 1 if lf["synced"] else 0, r["id"]))
                    except Exception:
                        pass
                conn.execute("INSERT OR REPLACE INTO kv (key, value) VALUES ('lyrics_indexed', '1')")
                conn.commit()
            todo = conn.execute("SELECT id, path FROM tracks WHERE missing=0 AND gain_db IS NULL "
                                "LIMIT 8").fetchall()
            conn.close()
            if todo and not SCAN_STATE["running"]:
                BG["busy"] = True
                for r in todo:
                    try:
                        g = measure_gain(r["path"]) if os.path.exists(r["path"]) else 0.0
                    except Exception:
                        g = 0.0
                    c2 = db()
                    c2.execute("UPDATE tracks SET gain_db=? WHERE id=?", (round(g, 2), r["id"]))
                    c2.commit()
                    c2.close()
                    BG["levelled"] += 1
                continue
            if load_config().get("detect_bpm_key", True) and not SCAN_STATE["running"] and not CONV["running"]:
                c3 = db()
                todo = c3.execute("SELECT * FROM tracks WHERE missing=0 AND COALESCE(analysed,0)=0 LIMIT 4").fetchall()
                c3.close()
                if todo:
                    BG["busy"] = True
                    for r in todo:
                        try:
                            found = analyse_bpm_key(r)
                        except Exception:
                            found = {}
                        c2 = db()
                        if found:
                            merged = dict(r)
                            merged.update(found)
                            try:
                                write_tags(r["path"], merged, load_config().get("write_tags_to_file", True))
                                _touch_file(c2, r["id"], r["path"])
                            except Exception:
                                pass
                            c2.execute("UPDATE tracks SET %s WHERE id=?" % ", ".join("%s=?" % k for k in found),
                                       (*found.values(), r["id"]))
                        c2.execute("UPDATE tracks SET analysed=1 WHERE id=?", (r["id"],))
                        c2.commit()
                        c2.close()
                        BG["analysed"] += 1
                    continue
            BG["busy"] = False
            if once:
                return
        except Exception:
            BG["busy"] = False
            if once:
                return
        time.sleep(4)


# --------------------------------------------------------------------------
# Discord "Listening to" status, over Discord's local IPC
# --------------------------------------------------------------------------

class DiscordRPC:
    def __init__(self):
        self.lock = threading.Lock()
        self.fp = None
        self.sock = None
        self.client_id = None
        self.last = None
        self.error = ""

    def _paths(self):
        if os.name == "nt":
            return [r"\\?\pipe\discord-ipc-%d" % i for i in range(10)]
        base = [os.environ.get(k) for k in ("XDG_RUNTIME_DIR", "TMPDIR", "TMP", "TEMP")] + ["/tmp"]
        out = []
        for b in [x for x in base if x]:
            for sub in ("", "app/com.discordapp.Discord", "snap.discord"):
                for i in range(10):
                    out.append(os.path.join(b, sub, "discord-ipc-%d" % i))
        return out

    def _write(self, op, payload):
        data = json.dumps(payload).encode("utf-8")
        msg = struct.pack("<II", op, len(data)) + data
        if self.sock is not None:
            self.sock.sendall(msg)
        else:
            self.fp.write(msg)
            self.fp.flush()

    def _readn(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf)) if self.sock is not None else self.fp.read(n - len(buf))
            if not chunk:
                raise OSError("Discord closed the connection")
            buf += chunk
        return buf

    def _read(self):
        op, ln = struct.unpack("<II", self._readn(8))
        return op, json.loads(self._readn(ln).decode("utf-8") or "{}")

    @staticmethod
    def _same(prev, act):
        """Same song and about the same position: no need to resend."""
        if prev is None:
            return False
        if act is None or prev == "cleared":
            return act is None and prev == "cleared"
        strip = lambda a: {k: v for k, v in a.items() if k != "timestamps"}
        if strip(prev) != strip(act):
            return False
        ps, ns = prev.get("timestamps", {}).get("start", 0), act.get("timestamps", {}).get("start", 0)
        return abs(ps - ns) < 2500

    def close(self):
        for h in (self.sock, self.fp):
            try:
                if h is not None:
                    h.close()
            except Exception:
                pass
        self.sock = self.fp = None
        self.last = None

    def _connect(self, client_id):
        self.close()
        for path in self._paths():
            try:
                if os.name == "nt":
                    self.fp = open(path, "r+b", buffering=0)
                else:
                    if not os.path.exists(path):
                        continue
                    sk = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    sk.settimeout(5)
                    sk.connect(path)
                    self.sock = sk
            except OSError:
                self.fp = self.sock = None
                continue
            try:
                self._write(0, {"v": 1, "client_id": str(client_id)})
                op, data = self._read()
                if data.get("evt") == "READY":
                    self.client_id = client_id
                    return True
                self.error = data.get("data", {}).get("message") or "Discord refused the connection"
            except (OSError, ValueError, struct.error) as exc:
                self.error = str(exc)
            self.close()
        if not self.error:
            self.error = "Discord isn't running"
        return False

    def update(self, client_id, activity):
        """activity None clears the status. Returns (ok, message)."""
        with self.lock:
            if not client_id:
                self.close()
                return False, "Add your Discord Application ID first"
            if self.client_id == client_id and (self.fp or self.sock) and self._same(self.last, activity):
                return True, "Connected"
            for attempt in range(2):
                if not (self.fp or self.sock) or self.client_id != client_id:
                    self.error = ""
                    if not self._connect(client_id):
                        return False, self.error
                try:
                    self._write(1, {"cmd": "SET_ACTIVITY", "nonce": str(uuid.uuid4()),
                                    "args": {"pid": os.getpid(), "activity": activity}})
                    op, data = self._read()
                    if data.get("evt") == "ERROR" and activity and "type" in activity:
                        activity = {k: v for k, v in activity.items() if k != "type"}
                        continue
                    if data.get("evt") == "ERROR":
                        return False, data.get("data", {}).get("message") or "Discord rejected the status"
                    self.last = activity if activity is not None else "cleared"
                    return True, "Connected"
                except (OSError, ValueError, struct.error) as exc:
                    self.error = str(exc)
                    self.close()
            return False, self.error or "Couldn't reach Discord"


DISCORD = DiscordRPC()


# ---- cover art links for Discord ----

ITUNES_URL = "https://itunes.apple.com"
CATBOX_URL = "https://catbox.moe/user/api.php"
ART_RETRY_DAYS = 7


def _norm_words(x):
    x = unicodedata.normalize("NFKD", x or "").lower()
    x = re.sub(r"[\(\[].*?[\)\]]", " ", x)
    x = re.sub(r"\b(feat|ft|featuring)\b.*$", " ", x)
    return re.sub(r"[^a-z0-9]+", " ", x).strip()


def _art_cache(conn, key):
    row = conn.execute("SELECT url, ts FROM art_links WHERE key=?", (key,)).fetchone()
    if not row:
        return None
    if not row["url"] and time.time() - (row["ts"] or 0) > ART_RETRY_DAYS * 86400:
        return None          # a miss is retried after a week
    return row["url"]


def _art_store(conn, key, url):
    conn.execute("INSERT OR REPLACE INTO art_links (key, url, ts) VALUES (?,?,?)", (key, url or "", time.time()))
    conn.commit()


def itunes_cover(title, artist):
    """Official cover link for a released song, or '' when there is no clear match."""
    t, a = _norm_words(split_version(title)[0] or title), _norm_words(artist.split(",")[0])
    if not t or not a:
        return ""
    q = urllib.parse.urlencode({"term": "%s %s" % (artist.split(",")[0], split_version(title)[0] or title),
                                "entity": "song", "limit": 10})
    res = http_json(ITUNES_URL + "/search?" + q, timeout=8).get("results") or []
    for r in res:
        rt, ra = _norm_words(r.get("trackName", "")), _norm_words(r.get("artistName", ""))
        if rt == t and (a in ra or ra in a) and r.get("artworkUrl100"):
            return r["artworkUrl100"].replace("100x100bb", "512x512bb")
    return ""


def upload_cover(data, mime):
    """Upload image bytes to catbox.moe (anonymous, public link). Returns the link."""
    ext = {"image/png": "png", "image/webp": "webp", "image/gif": "gif"}.get((mime or "").lower(), "jpg")
    boundary = "----aura" + uuid.uuid4().hex
    body = b"".join([
        ("--%s\r\nContent-Disposition: form-data; name=\"reqtype\"\r\n\r\nfileupload\r\n" % boundary).encode(),
        ("--%s\r\nContent-Disposition: form-data; name=\"fileToUpload\"; filename=\"cover.%s\"\r\n"
         "Content-Type: %s\r\n\r\n" % (boundary, ext, mime or "image/jpeg")).encode(),
        data, ("\r\n--%s--\r\n" % boundary).encode()])
    req = urllib.request.Request(CATBOX_URL, data=body, method="POST", headers={
        "User-Agent": HTTP_UA, "Content-Type": "multipart/form-data; boundary=" + boundary})
    with urllib.request.urlopen(req, timeout=30) as r:
        url = r.read().decode("utf-8", "replace").strip()
    if not url.startswith("https://") or len(url) > 250:
        raise OSError("upload failed: %s" % url[:120])
    return url


def cover_link(row, cfg, allow_network=True):
    """A web link for this song's cover: cached, iTunes, or uploaded. '' if none."""
    conn = db()
    try:
        title = row["title"] or Path(row["filename"]).stem
        artist = row["artist"] or ""
        if cfg.get("discord_art_lookup", True) and artist:
            key = "itunes:%s|%s" % (_norm_words(artist), _norm_words(split_version(title)[0] or title))
            url = _art_cache(conn, key)
            if url is None and allow_network:
                try:
                    url = itunes_cover(title, artist)
                    _art_store(conn, key, url)
                except Exception:
                    url = None
            if url:
                return url
        if cfg.get("discord_art_upload") and row["has_art"]:
            data, mime = get_artwork(row["path"])
            if data:
                key = "upload:" + hashlib.sha1(data).hexdigest()
                url = _art_cache(conn, key)
                if url is None and allow_network:
                    try:
                        url = upload_cover(data, mime)
                        _art_store(conn, key, url)
                    except Exception:
                        url = None
                if url:
                    return url
        return ""
    finally:
        conn.close()


DISCORD_NOW = {"tid": None, "activity": None, "cid": ""}


def _discord_fill_art(tid, cid):
    """Find the cover in the background, then refresh the status if the same song still plays."""
    row = _track_row(tid)
    if not row:
        return
    url = cover_link(row, load_config(), allow_network=True)
    if url and DISCORD_NOW["tid"] == tid and DISCORD_NOW["activity"]:
        act = json.loads(json.dumps(DISCORD_NOW["activity"]))
        act.setdefault("assets", {})["large_image"] = url
        act["assets"]["small_image"] = "aura"
        act["assets"]["small_text"] = "AURA"
        DISCORD_NOW["activity"] = act
        DISCORD.update(cid, act)


# --------------------------------------------------------------------------
# version + updates (GitHub Releases)
# --------------------------------------------------------------------------

def app_version_info():
    try:
        v = json.loads((resource_dir() / "version.json").read_text(encoding="utf-8"))
    except Exception:
        v = {}
    return {"version": str(v.get("version") or "0.0.0-dev"), "repo": str(v.get("repo") or "")}


def version_tuple(v):
    nums = re.findall(r"\d+", str(v).lstrip("vV").split("-")[0])
    return tuple(int(n) for n in (nums + ["0", "0", "0"])[:3])


GITHUB_API = "https://api.github.com"
UPDATE = {"checked": 0, "result": None, "state": "idle", "done": 0, "total": 0, "error": ""}


def check_update(force=False):
    info = app_version_info()
    if not info["repo"]:
        return {"available": False, "reason": "This copy wasn't built by the release pipeline, so it can't update itself."}
    if not force and UPDATE["result"] and time.time() - UPDATE["checked"] < 600:
        return UPDATE["result"]
    rel = http_json("%s/repos/%s/releases/latest" % (GITHUB_API, info["repo"]), timeout=10)
    latest = str(rel.get("tag_name") or "").lstrip("vV")
    asset = next((a for a in rel.get("assets", []) if str(a.get("name", "")).lower().endswith("setup.exe")), None)
    out = {"available": bool(latest) and version_tuple(latest) > version_tuple(info["version"]) and asset is not None,
           "current": info["version"], "latest": latest, "notes": (rel.get("body") or "")[:4000],
           "page": rel.get("html_url") or "", "url": asset.get("browser_download_url") if asset else "",
           "size": asset.get("size") if asset else 0}
    UPDATE.update(checked=time.time(), result=out)
    return out


def _download_and_install(url, size):
    try:
        UPDATE.update(state="downloading", done=0, total=size or 0, error="")
        dest = Path(tempfile.gettempdir()) / ("AURA-Setup-%d.exe" % int(time.time()))
        req = urllib.request.Request(url, headers={"User-Agent": HTTP_UA})
        with urllib.request.urlopen(req, timeout=30) as r, open(dest, "wb") as out:
            UPDATE["total"] = int(r.headers.get("Content-Length") or size or 0)
            while True:
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                out.write(chunk)
                UPDATE["done"] += len(chunk)
        if dest.stat().st_size < 100_000:
            raise OSError("the download looks incomplete")
        UPDATE["state"] = "installing"
        # A clean environment for everything started from here: otherwise the new AURA
        # inherits this copy's PyInstaller settings, tries to run from this copy's temp
        # folder (deleted as we exit) and never opens.
        env = {k: v for k, v in os.environ.items()
               if not (k.startswith("_PYI") or k.startswith("_MEI") or k in ("TCL_LIBRARY", "TK_LIBRARY"))}
        env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        log = Path(tempfile.gettempdir()) / "AURA-update.log"
        # Reopen the installed copy (a stand-alone AURA.exe in Downloads isn't the one updated).
        exe = str(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "AURA" / "AURA.exe")
        if FROZEN and (Path(sys.executable).parent / "unins000.exe").exists():
            exe = sys.executable
        q = lambda x: "'" + str(x).replace("'", "''") + "'"
        # A small hidden PowerShell helper runs the installer, waits for it to finish,
        # then starts the updated AURA — so reopening doesn't depend on the installer.
        script = (
            "$ErrorActionPreference='Continue';"
            "Start-Sleep -Milliseconds 1500;"
            "$p=Start-Process -FilePath %s -ArgumentList @('/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART',"
            "'/CLOSEAPPLICATIONS',%s) -PassThru;"
            "$p.WaitForExit();"
            "Add-Content -LiteralPath %s -Value ('installer finished: ' + $p.ExitCode);"
            "Start-Sleep -Seconds 1;"
            "Start-Process -FilePath %s;"
            "Add-Content -LiteralPath %s -Value 'started AURA'"
        ) % (q(dest), q('/LOG="%s"' % log), q(str(log) + ".txt"), q(exe), q(str(log) + ".txt"))
        no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        try:
            subprocess.Popen(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                              "-WindowStyle", "Hidden", "-Command", script],
                             close_fds=True, creationflags=no_window, env=env, cwd=tempfile.gettempdir())
        except OSError:
            flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            subprocess.Popen([str(dest), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS",
                              "/LOG=%s" % log], close_fds=True, creationflags=flags, env=env,
                             cwd=tempfile.gettempdir())
        threading.Timer(1.5, lambda: os._exit(0)).start()
    except Exception as exc:
        UPDATE.update(state="error", error=str(exc))


# --------------------------------------------------------------------------
# convert to MP3 — WAV, FLAC, M4A, OGG… become MP3s with tags, cover and lyrics
# --------------------------------------------------------------------------

CONV = {"lock": threading.Lock(), "jobs": [], "running": False, "cancel": False}
MP3_RATES = (8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000)


def encode_mp3(src, dest, kbps=320, progress=None):
    """Decode any audio file and encode it as MP3 (LAME through PyAV)."""
    import av
    with av.open(src) as inp:
        stream = next((st for st in inp.streams if st.type == "audio"), None)
        if stream is None:
            raise OSError("no audio in this file")
        dur = float(inp.duration / 1e6) if inp.duration else 0.0
        rate = stream.rate or 44100
        if rate not in MP3_RATES:
            rate = 44100 if rate % 11025 == 0 else 48000
        layout = "mono" if (stream.channels or 2) == 1 else "stereo"
        with av.open(dest, "w", format="mp3") as out:
            o = out.add_stream("libmp3lame", rate=rate)
            o.bit_rate = int(kbps) * 1000
            try:
                o.layout = layout
            except Exception:
                pass
            res = av.AudioResampler(format="fltp", layout=layout, rate=rate)
            for frame in inp.decode(stream):
                for rf in res.resample(frame):
                    for pkt in o.encode(rf):
                        out.mux(pkt)
                if progress and dur and frame.time is not None:
                    progress(min(0.99, frame.time / dur))
            for rf in res.resample(None):
                for pkt in o.encode(rf):
                    out.mux(pkt)
            for pkt in o.encode(None):
                out.mux(pkt)


def originals_dir(src):
    """_originals inside the music folder the song lives in."""
    cfg = load_config()
    src_n = os.path.normcase(os.path.abspath(src))
    for f in cfg.get("folders") or []:
        fn = os.path.normcase(os.path.abspath(f))
        if src_n.startswith(fn + os.sep):
            return Path(f) / "_originals"
    return Path(src).parent / "_originals"


def _copy_metadata(row, src, dest):
    data = {k: row[k] or "" for k in TAG_FIELDS if k in row.keys()}
    data["rating"] = row["rating"] or 0
    write_tags(dest, data)
    art, mime = get_artwork(src)
    if art:
        set_artwork(dest, art, mime)
    lf = read_lyrics_full(src)
    if lf["plain"] or lf["synced"]:
        write_lyrics(dest, lf["plain"] or "\n".join(t for _, t in lf["synced"]), lf["synced"] or None,
                     lf.get("words"), lf.get("ends"))


def convert_one(job, kbps, originals):
    conn = db()
    try:
        row = conn.execute("SELECT * FROM tracks WHERE id=?", (job["track_id"],)).fetchone()
        if not row or not os.path.exists(row["path"]):
            raise OSError("the file is missing")
        src = row["path"]
        if src.lower().endswith(".mp3"):
            job.update(state="skipped", note="already an MP3")
            return
        dest = Path(src).with_suffix(".mp3")
        if dest.exists():
            job.update(state="skipped", note="%s already exists" % dest.name)
            return
        tmp = dest.with_name(dest.stem + ".converting.mp3")
        try:
            encode_mp3(src, str(tmp), kbps, progress=lambda f: job.__setitem__("progress", round(f, 3)))
            if CONV["cancel"]:
                raise InterruptedError()
            _copy_metadata(row, src, str(tmp))
            os.replace(tmp, dest)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
        moved = ""
        if originals == "move":
            od = originals_dir(src)
            od.mkdir(parents=True, exist_ok=True)
            target = od / Path(src).name
            n = 2
            while target.exists():
                target = od / ("%s (%d)%s" % (Path(src).stem, n, Path(src).suffix))
                n += 1
            shutil.move(src, str(target))
            moved = str(target)
        # The song keeps its place in the library: same id, so favorites,
        # playlists, play counts, versions and stats all carry over to the MP3.
        new = analyse(str(dest), False)
        for k in ("_rating", "_notes", "_user_tags", "_era", "gain_db"):
            new.pop(k, None)
        new["fingerprint"] = row["fingerprint"]
        new["last_scan"] = time.time()
        if originals == "move":
            cols = ", ".join("%s=?" % k for k in new)
            conn.execute("UPDATE tracks SET %s WHERE id=?" % cols, (*new.values(), row["id"]))
        else:
            # keep the original as its own entry; the MP3 joins as a new one
            new.update(rating=row["rating"] or 0, notes=row["notes"] or "", user_tags=row["user_tags"] or "",
                       first_seen=time.time())
            conn.execute("INSERT OR IGNORE INTO tracks (%s) VALUES (%s)" % (", ".join(new), ", ".join("?" for _ in new)),
                         tuple(new.values()))
        conn.commit()
        job.update(state="done", progress=1.0, mp3=str(dest), original=moved,
                   saved=max(0, (row["size"] or 0) - (os.path.getsize(dest) or 0)))
    except InterruptedError:
        job.update(state="cancelled")
    except Exception as exc:
        job.update(state="error", note=str(exc) or type(exc).__name__)
    finally:
        conn.close()


def _convert_worker(kbps, originals):
    try:
        todo = [j for j in CONV["jobs"] if j["state"] == "queued"]
        workers = max(1, min(3, (os.cpu_count() or 2) // 2))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            def run(j):
                if CONV["cancel"]:
                    j["state"] = "cancelled"
                    return
                j["state"] = "running"
                convert_one(j, kbps, originals)
            list(ex.map(run, todo))
    finally:
        CONV["running"] = False
        WATCH["sig"] = None      # don't treat our own new files as a reason to rescan


# --------------------------------------------------------------------------
# folder watcher — rescan when music is added, changed or removed
# --------------------------------------------------------------------------

def folder_signature(cfg):
    """A cheap fingerprint of every audio file's name, size and date."""
    h = hashlib.sha1()
    n = 0
    for path in iter_audio_files(cfg.get("folders", []), skip_dirs=[quarantine_dir(cfg)]):
        try:
            st = os.stat(path)
        except OSError:
            continue
        h.update(("%s|%d|%d\n" % (os.path.normcase(path), st.st_size, int(st.st_mtime))).encode("utf-8", "replace"))
        n += 1
    return "%d:%s" % (n, h.hexdigest())


WATCH = {"sig": None}


def folder_watcher(interval=20):
    while True:
        time.sleep(interval)
        try:
            cfg = load_config()
            if not cfg.get("watch_folders", True) or not cfg.get("folders"):
                WATCH["sig"] = None
                continue
            sig = folder_signature(cfg)
            if WATCH["sig"] is None:
                WATCH["sig"] = sig
                continue
            if CONV["running"]:
                continue
            if sig != WATCH["sig"] and not SCAN_STATE["running"]:
                if scan_library(deep=bool(cfg.get("fingerprint", True)), auto=True):
                    WATCH["sig"] = folder_signature(cfg)
        except Exception:
            pass


# --------------------------------------------------------------------------
# search
# --------------------------------------------------------------------------

SEARCH_FIELDS = {
    "title": "title", "artist": "artist", "album": "album",
    "albumartist": "albumartist", "genre": "genre", "year": "year",
    "key": "music_key", "producer": "producer", "prod": "producer",
    "tag": "user_tags", "tags": "user_tags", "note": "notes",
    "notes": "notes", "file": "filename", "folder": "folder",
    "comment": "comment", "ext": "ext", "song": "song", "era": "era",
}
NUM_FIELDS = {"bpm": "bpm", "rating": "rating", "stars": "rating",
              "bitrate": "bitrate", "year": "year"}
CMP = re.compile(r"^(>=|<=|>|<|=)?\s*(\d+(?:\.\d+)?)$")


def _tokenise(q):
    return [t for t in re.findall(r'[^\s"]+|"[^"]*"', q) if t.strip()]


def _num_ok(value, expr):
    m = CMP.match(expr.strip())
    if not m:
        return None
    op, num = m.group(1) or "=", float(m.group(2))
    try:
        v = float(re.sub(r"[^\d.]", "", str(value or "")) or 0)
    except ValueError:
        return False
    return {"=": v == num, ">": v > num, "<": v < num,
            ">=": v >= num, "<=": v <= num}[op]


def match_query(row, q):
    """Supports bare words, field:value, numeric comparisons and a few flags:
    artist:juice  bpm:>140  rating:>=4  noart  untagged  "exact phrase" """
    if not q:
        return True
    for raw in _tokenise(q):
        neg = raw.startswith("-")
        tok = raw[1:] if neg else raw
        tok = tok.strip('"')
        if not tok:
            continue
        ok = None
        low = tok.lower()
        if low in ("noart", "noartwork"):
            ok = not row.get("has_art")
        elif low in ("hasart", "art"):
            ok = bool(row.get("has_art"))
        elif low == "untagged":
            ok = not (row.get("title") and row.get("artist"))
        elif low == "noartist":
            ok = not (row.get("artist") or "").strip()
        elif low == "notitle":
            ok = not (row.get("title") or "").strip()
        elif low == "noalbum":
            ok = not (row.get("album") or "").strip()
        elif low == "noyear":
            ok = not (row.get("year") or "").strip()
        elif low == "noera":
            ok = not (row.get("era") or "").strip()
        elif low == "nolyrics":
            ok = not (row.get("lyrics") or "").strip()
        elif low in ("nosync", "nosynced", "untimed"):
            ok = bool((row.get("lyrics") or "").strip()) and not row.get("has_synced")
        elif low in ("notmp3", "nonmp3", "convertible"):
            ok = (row.get("ext") or "").lower() != "mp3"
        elif low == "unrated":
            ok = not row.get("rating")
        elif low == "versioned":
            ok = bool(row.get("song"))
        elif low == "unversioned":
            ok = not row.get("song")
        elif ":" in tok:
            field, _, value = tok.partition(":")
            field, value = field.lower(), value.strip('"')
            if field in NUM_FIELDS:
                r = _num_ok(row.get(NUM_FIELDS[field]), value)
                ok = r if r is not None else (value.lower() in str(
                    row.get(NUM_FIELDS[field]) or "").lower())
            elif field in SEARCH_FIELDS:
                ok = value.lower() in str(row.get(SEARCH_FIELDS[field]) or "").lower()
        if ok is None:
            hay = " ".join(str(row.get(k) or "") for k in
                           ("filename", "title", "artist", "album", "albumartist",
                            "notes", "user_tags", "genre", "producer", "folder")).lower()
            ok = low in hay
        if ok == neg:
            return False
    return True


# --------------------------------------------------------------------------
# api
# --------------------------------------------------------------------------

def public(row):
    r = dict(row)
    r.pop("fingerprint", None)
    r.pop("_norm", None)
    return r


@app.route("/")
def index():
    html = (resource_dir() / "templates" / "index.html").read_text(encoding="utf-8")
    return Response(html.replace("__TOKEN__", TOKEN), mimetype="text/html")


@app.get("/api/config")
def api_get_config():
    cfg = load_config()
    cfg["fingerprinting"] = CAN_FINGERPRINT
    cfg["decoder"] = "libsndfile" if _sf is not None else ("ffmpeg" if FFMPEG else "none")
    cfg["data_dir"] = str(DATA_DIR)
    cfg["quarantine_effective"] = str(quarantine_dir(cfg) or "")
    cfg["configured"] = bool(cfg.get("folders"))
    return jsonify(cfg)


CONFIG_LOCK = threading.Lock()


@app.post("/api/config")
def api_set_config():
    body = request.get_json(force=True)
    with CONFIG_LOCK:      # two quick changes must not overwrite each other
        return _set_config(body)


def _set_config(body):
    cfg = load_config()
    for key in ("folders", "quarantine", "rename_pattern",
                "write_tags_to_file", "fingerprint", "watch_folders",
                "discord", "discord_client_id", "discord_art_lookup", "discord_art_upload",
                "autosync_model", "detect_bpm_key", "autosync_language", "autosync_precise",
                "transcribe_model", "transcribe_lookup"):
        if key in body:
            cfg[key] = body[key]
    if isinstance(body.get("defaults"), dict):
        cfg["defaults"].update({k: str(v) for k, v in body["defaults"].items()
                                if k in cfg["defaults"]})
    save_config(cfg)
    cfg["quarantine_effective"] = str(quarantine_dir(cfg) or "")
    return jsonify({"ok": True, "config": cfg})


@app.post("/api/browse")
def api_browse():
    body = request.get_json(force=True) or {}
    target = body.get("path") or ""
    if not target:
        if os.name == "nt":
            home = str(Path.home())
            shortcuts = [("Music", Path(home) / "Music"),
                         ("Downloads", Path(home) / "Downloads"),
                         ("Desktop", Path(home) / "Desktop"),
                         ("Documents", Path(home) / "Documents")]
            dirs = [{"name": n, "path": str(p)} for n, p in shortcuts if p.is_dir()]
            dirs += [{"name": "%s:\\" % d, "path": "%s:\\" % d}
                     for d in "CDEFGHIJKLMNOPQRSTUVWXYZ" if os.path.exists("%s:\\" % d)]
            return jsonify({"path": "", "parent": None, "dirs": dirs})
        target = str(Path.home())
    p = Path(target)
    if not p.is_dir():
        return jsonify({"error": "Not a folder"}), 400
    dirs = []
    try:
        for entry in sorted(p.iterdir(), key=lambda e: e.name.lower()):
            try:
                if entry.is_dir() and not entry.name.startswith("."):
                    dirs.append({"name": entry.name, "path": str(entry)})
            except OSError:
                continue
    except PermissionError:
        return jsonify({"error": "No permission to read that folder"}), 403
    return jsonify({"path": str(p),
                    "parent": str(p.parent) if p.parent != p else None, "dirs": dirs})


@app.post("/api/scan")
def api_scan():
    if SCAN_STATE["running"]:
        return jsonify({"ok": False, "message": "Already scanning"}), 409
    deep = bool((request.get_json(silent=True) or {}).get("deep", True))
    threading.Thread(target=scan_library, args=(deep,), daemon=True).start()
    return jsonify({"ok": True})


@app.get("/api/scan/status")
def api_scan_status():
    return jsonify(SCAN_STATE)


@app.get("/api/tracks")
def api_tracks():
    q = (request.args.get("q") or "").strip()
    sort = request.args.get("sort") or "filename"
    folder = request.args.get("folder") or ""

    conn = db()
    sql, args = (
        "SELECT t.*, v.song AS song, v.label AS version_label FROM tracks t "
        "LEFT JOIN versions v ON v.track_id=t.id AND v.status='grouped' "
        "WHERE t.missing=0"), []
    if folder:
        sql += " AND t.folder LIKE ?"
        args.append(folder + "%")
    rows = [public(r) for r in conn.execute(sql, args)]
    stats = conn.execute(
        "SELECT COUNT(*) total, "
        "SUM(CASE WHEN rating>0 THEN 1 ELSE 0 END) rated, "
        "SUM(CASE WHEN has_art=0 THEN 1 ELSE 0 END) noart, "
        "SUM(CASE WHEN title='' OR title IS NULL OR artist='' OR artist IS NULL "
        "         THEN 1 ELSE 0 END) untagged "
        "FROM tracks WHERE missing=0").fetchone()
    missing = conn.execute("SELECT COUNT(*) c FROM tracks WHERE missing=1").fetchone()["c"]
    versioned = conn.execute(
        "SELECT COUNT(*) c FROM versions v JOIN tracks t ON t.id=v.track_id "
        "WHERE v.status='grouped' AND t.missing=0").fetchone()["c"]
    conn.close()

    if q:
        rows = [r for r in rows if match_query(r, q)]

    keyfns = {
        "filename": lambda r: (r.get("filename") or "").lower(),
        "title": lambda r: (r.get("title") or r.get("filename") or "").lower(),
        "artist": lambda r: (r.get("artist") or "").lower(),
        "duration": lambda r: -(r.get("duration") or 0),
        "newest": lambda r: -(r.get("mtime") or 0),
        "oldest": lambda r: (r.get("mtime") or 0),
        "rating": lambda r: (-(r.get("rating") or 0), (r.get("filename") or "").lower()),
        "size": lambda r: -(r.get("size") or 0),
    }
    rows.sort(key=keyfns.get(sort, keyfns["filename"]))
    return jsonify({
        "tracks": rows, "showing": len(rows),
        "stats": {"total": stats["total"] or 0, "rated": stats["rated"] or 0,
                  "noart": stats["noart"] or 0, "untagged": stats["untagged"] or 0,
                  "missing": missing, "versioned": versioned},
    })


@app.get("/api/track/<int:tid>")
def api_track(tid):
    conn = db()
    row = conn.execute(
        "SELECT t.*, v.song AS song, v.label AS version_label FROM tracks t "
        "LEFT JOIN versions v ON v.track_id=t.id AND v.status='grouped' "
        "WHERE t.id=?", (tid,)).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "not found"}), 404
    return jsonify(public(row))


@app.post("/api/track/<int:tid>")
def api_save_track(tid):
    body = request.get_json(force=True)
    cfg = load_config()
    conn = db()
    row = conn.execute("SELECT * FROM tracks WHERE id=?", (tid,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "not found"}), 404

    data = {f: (body.get(f) if body.get(f) is not None else row[f]) or "" for f in TAG_FIELDS}
    data["rating"] = int(body.get("rating", row["rating"] or 0) or 0)

    old = {f: row[f] for f in TAG_FIELDS if str(row[f] or "") != str(data[f] or "")}
    if (row["rating"] or 0) != data["rating"]:
        old["rating"] = row["rating"] or 0
    if old:
        record_undo(conn, "Edited %s" % (row["title"] or row["filename"]), {tid: old})

    warning = ""
    try:
        write_tags(row["path"], data, cfg.get("write_tags_to_file", True))
    except Exception as exc:
        warning = "Saved to the library, but writing tags into the file failed: %s" % exc

    conn.execute("UPDATE tracks SET %s WHERE id=?" % ", ".join("%s=?" % k for k in data),
                 (*data.values(), tid))
    try:
        st = os.stat(row["path"])
        conn.execute("UPDATE tracks SET mtime=?, size=?, file_hash=? WHERE id=?",
                     (st.st_mtime, st.st_size, file_sha256(row["path"]), tid))
    except OSError:
        pass
    conn.commit()
    out = conn.execute("SELECT * FROM tracks WHERE id=?", (tid,)).fetchone()
    conn.close()
    return jsonify({"ok": True, "warning": warning, "track": public(out)})


def apply_changes(conn, cfg, per_track, label):
    """per_track: {track_id: {field: new_value}}. Writes tags, updates the
    database and records one undo batch covering the lot."""
    undo, applied, failed = {}, 0, 0
    for tid, fields in per_track.items():
        row = conn.execute("SELECT * FROM tracks WHERE id=?", (tid,)).fetchone()
        if not row:
            continue
        changed = {f: v for f, v in fields.items() if str(row[f] or "") != str(v or "")}
        if not changed:
            continue
        undo[tid] = {f: row[f] for f in changed}
        merged = {k: row[k] for k in row.keys()}
        merged.update(changed)
        try:
            write_tags(row["path"], merged, cfg.get("write_tags_to_file", True))
        except Exception:
            failed += 1
        conn.execute("UPDATE tracks SET %s WHERE id=?" % ", ".join("%s=?" % f for f in changed),
                     (*changed.values(), tid))
        applied += 1
    batch = record_undo(conn, label, undo)
    conn.commit()
    return applied, failed, batch


@app.post("/api/bulk")
def api_bulk():
    body = request.get_json(force=True)
    ids = [int(i) for i in body.get("ids", [])]
    if not ids:
        return jsonify({"ok": False, "message": "No tracks selected"}), 400
    updates = {}
    if body.get("rating") is not None:
        updates["rating"] = int(body["rating"])
    for f in ("artist", "albumartist", "album", "genre", "year", "user_tags",
              "producer", "comment", "era"):
        if body.get(f) is not None:
            updates[f] = body[f]
    if not updates:
        return jsonify({"ok": False, "message": "Nothing to change"}), 400
    cfg = load_config()
    conn = db()
    applied, failed, batch = apply_changes(
        conn, cfg, {i: dict(updates) for i in ids},
        "Bulk edit of %d tracks" % len(ids))
    conn.close()
    return jsonify({"ok": True, "changed": applied, "failed": failed, "batch": batch})


@app.post("/api/autofill")
def api_autofill():
    """Stamp the library defaults onto tracks and read what can be read out of
    filenames and folder names. Always previewed first."""
    body = request.get_json(silent=True) or {}
    use_defaults = body.get("defaults", True)
    use_filename = body.get("filenames", True)
    overwrite = body.get("overwrite", False)
    want_artist = body.get("artist_from_name", False)
    want_trackno = body.get("track_numbers", False)
    want_quality = body.get("quality_tags", False)
    folder_to = body.get("folder_to") if body.get("folder_to") in ("album", "era") else ""
    only = {int(i) for i in body.get("only") or []}
    cfg = load_config()
    defaults = {k: v for k, v in (cfg.get("defaults") or {}).items() if str(v).strip()}
    roots = {os.path.normcase(os.path.abspath(f)) for f in cfg.get("folders") or []}

    conn = db()
    rows = [dict(r) for r in conn.execute("SELECT * FROM tracks WHERE missing=0")]
    if only:
        rows = [r for r in rows if r["id"] in only]

    def empty_or_over(t, f):
        return overwrite or not str(t.get(f) or "").strip()

    proposals = []
    for t in rows:
        changes = {}
        ex = parse_extras(t["filename"])
        if want_artist and ex["artist"] and empty_or_over(t, "artist") and ex["artist"] != (t.get("artist") or ""):
            changes["artist"] = ex["artist"]
        if want_trackno and ex["track_no"] and empty_or_over(t, "track_no") and ex["track_no"] != (t.get("track_no") or ""):
            changes["track_no"] = ex["track_no"]
        if want_quality and ex["quality"]:
            have = [x.strip() for x in (t.get("user_tags") or "").split(",") if x.strip()]
            add = [q for q in ex["quality"] if q.lower() not in {h.lower() for h in have}]
            if add:
                changes["user_tags"] = ", ".join(have + add)
        if folder_to:
            here = os.path.normcase(os.path.abspath(t.get("folder") or ""))
            name = os.path.basename(t.get("folder") or "").strip()
            if name and here not in roots and not name.startswith("_") and empty_or_over(t, folder_to):
                name = re.sub(r"\s+", " ", name.replace("_", " ")).strip()
                if name != (t.get(folder_to) or ""):
                    changes[folder_to] = name
        if use_defaults:
            for f, v in defaults.items():
                if overwrite or not str(t.get(f) or "").strip():
                    if str(t.get(f) or "") != str(v):
                        changes[f] = v
        if use_filename:
            known = (changes.get("artist") or t.get("artist") or ex["artist"] or defaults.get("artist", ""))
            parsed = parse_filename(t["filename"], known)
            if parsed["title"] and (overwrite or not str(t.get("title") or "").strip()):
                if parsed["title"] != (t.get("title") or ""):
                    changes["title"] = parsed["title"]
            if parsed["producer"] and (overwrite or not str(t.get("producer") or "").strip()):
                if parsed["producer"] != (t.get("producer") or ""):
                    changes["producer"] = parsed["producer"]
        if changes:
            proposals.append({"id": t["id"], "filename": t["filename"],
                              "current_title": t.get("title") or "",
                              "changes": changes})

    if not body.get("apply"):
        conn.close()
        return jsonify({"proposals": proposals, "applied": False,
                        "defaults": defaults})

    keep = set(body.get("ids") or [p["id"] for p in proposals])
    skip = {(int(k.split(":")[0]), k.split(":")[1]) for k in body.get("skip") or [] if ":" in k}
    per_track = {}
    for pr in proposals:
        if pr["id"] in keep:
            ch = {f: v for f, v in pr["changes"].items() if (pr["id"], f) not in skip}
            if ch:
                per_track[pr["id"]] = ch
    applied, failed, batch = apply_changes(
        conn, cfg, per_track, "Auto-filled %d tracks" % len(per_track))
    conn.close()
    return jsonify({"applied": True, "changed": applied, "failed": failed,
                    "batch": batch})


@app.get("/api/undo")
def api_undo_list():
    conn = db()
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM undo_batches ORDER BY id DESC LIMIT 20")]
    conn.close()
    return jsonify({"batches": rows})


@app.post("/api/undo")
def api_undo_apply():
    body = request.get_json(silent=True) or {}
    conn = db()
    if body.get("batch"):
        batch = int(body["batch"])
    else:
        last = conn.execute("SELECT id FROM undo_batches ORDER BY id DESC LIMIT 1").fetchone()
        if not last:
            conn.close()
            return jsonify({"ok": False, "message": "Nothing to undo"}), 400
        batch = last["id"]

    items = [dict(r) for r in conn.execute(
        "SELECT * FROM undo_items WHERE batch_id=?", (batch,))]
    meta = conn.execute("SELECT * FROM undo_batches WHERE id=?", (batch,)).fetchone()
    if not items or not meta:
        conn.close()
        return jsonify({"ok": False, "message": "That change is no longer available"}), 400

    cfg = load_config()
    per_track = {}
    for it in items:
        per_track.setdefault(it["track_id"], {})[it["field"]] = it["old_value"]
    for tid, fields in per_track.items():
        if "rating" in fields:
            try:
                fields["rating"] = int(fields["rating"] or 0)
            except ValueError:
                fields["rating"] = 0

    label = meta["label"]
    restored, failed, _ = apply_changes(conn, cfg, per_track, "Undid: %s" % label)
    conn.execute("DELETE FROM undo_items WHERE batch_id=?", (batch,))
    conn.execute("DELETE FROM undo_batches WHERE id=?", (batch,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "restored": restored, "failed": failed, "label": label})


@app.get("/api/audio/<int:tid>")
def api_audio(tid):
    conn = db()
    row = conn.execute("SELECT path FROM tracks WHERE id=?", (tid,)).fetchone()
    conn.close()
    if not row or not os.path.exists(row["path"]):
        return jsonify({"error": "missing"}), 404
    return send_file(row["path"], conditional=True)


@app.get("/api/art/<int:tid>")
def api_art(tid):
    conn = db()
    row = conn.execute("SELECT path FROM tracks WHERE id=?", (tid,)).fetchone()
    conn.close()
    if not row:
        return "", 404
    data, mime = get_artwork(row["path"])
    if not data:
        return "", 404
    return Response(data, mimetype=mime, headers={"Cache-Control": "no-cache"})


@app.post("/api/art/<int:tid>")
def api_set_art(tid):
    conn = db()
    row = conn.execute("SELECT path FROM tracks WHERE id=?", (tid,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "not found"}), 404
    f = request.files.get("image")
    try:
        if f:
            set_artwork(row["path"], f.read(), f.mimetype)
            conn.execute("UPDATE tracks SET has_art=1 WHERE id=?", (tid,))
            _touch_file(conn, tid, row["path"])
        else:
            set_artwork(row["path"], None, None)
            conn.execute("UPDATE tracks SET has_art=0 WHERE id=?", (tid,))
        conn.commit()
    except Exception as exc:
        conn.close()
        return jsonify({"error": str(exc)}), 500
    conn.close()
    return jsonify({"ok": True})


@app.post("/api/art/bulk")
def api_art_bulk():
    """Apply one cover image to many tracks at once."""
    ids = [int(i) for i in json.loads(request.form.get("ids", "[]"))]
    f = request.files.get("image")
    if not f or not ids:
        return jsonify({"error": "Need an image and at least one track"}), 400
    blob, mime = f.read(), f.mimetype
    conn = db()
    done = failed = 0
    last_err = ""
    for tid in ids:
        row = conn.execute("SELECT path FROM tracks WHERE id=?", (tid,)).fetchone()
        if not row:
            continue
        try:
            set_artwork(row["path"], blob, mime)
            conn.execute("UPDATE tracks SET has_art=1 WHERE id=?", (tid,))
            _touch_file(conn, tid, row["path"])
            done += 1
        except Exception as exc:
            failed += 1
            last_err = str(exc)
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "changed": done, "failed": failed, "error": last_err if failed else ""})


def safe_name(s):
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", s or "")
    return re.sub(r"\s+", " ", s).strip(" .")[:150] or "untitled"


@app.post("/api/rename")
def api_rename():
    """Rename one track or many, from the pattern in Settings."""
    body = request.get_json(force=True) or {}
    cfg = load_config()
    pattern = body.get("pattern") or cfg.get("rename_pattern", "{title}")
    ids = [int(i) for i in body.get("ids", [])]
    conn = db()
    renamed, errors, unchanged = [], [], 0
    for tid in ids:
        row = conn.execute("SELECT * FROM tracks WHERE id=?", (tid,)).fetchone()
        if not row:
            continue
        fields = {k: (row[k] or "") for k in
                  ("title", "artist", "albumartist", "album", "year", "genre",
                   "track_no", "producer")}
        if not fields["title"]:
            fields["title"] = Path(row["filename"]).stem
        try:
            base = safe_name(pattern.format(**fields))
        except KeyError as exc:
            conn.close()
            return jsonify({"error": "Unknown field in pattern: %s" % exc}), 400
        base = re.sub(r"^[\s\-]+|[\s\-]+$", "", base) or Path(row["filename"]).stem
        suffix = Path(row["path"]).suffix.lower()
        dest = Path(row["folder"]) / (base + suffix)
        if os.path.normcase(str(dest)) == os.path.normcase(row["path"]):
            unchanged += 1
            continue
        n = 2
        while dest.exists():
            dest = Path(row["folder"]) / ("%s (%d)%s" % (base, n, suffix))
            n += 1
        try:
            os.rename(row["path"], dest)
            conn.execute("UPDATE tracks SET path=?, filename=? WHERE id=?",
                         (str(dest), dest.name, tid))
            renamed.append(dest.name)
        except OSError as exc:
            errors.append("%s: %s" % (row["filename"], exc))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "renamed": renamed, "unchanged": unchanged,
                    "errors": errors})


@app.get("/api/duplicates")
def api_duplicates():
    thr = float(request.args.get("threshold", 0.78))
    return jsonify({"groups": find_duplicates(thr), "fingerprinting": CAN_FINGERPRINT})


@app.post("/api/duplicates/quarantine")
def api_quarantine():
    """Move the losers of a duplicate group into a holding folder. Nothing is
    ever deleted. `protect` lists every track chosen as a keeper anywhere — a
    file can be the keeper of one group and a loser in another, and without this
    guard a bulk resolve could move every copy of a song."""
    body = request.get_json(force=True)
    protect = {int(i) for i in body.get("protect", [])}
    requested = [int(i) for i in body.get("ids", [])]
    ids = [i for i in requested if i not in protect]
    cfg = load_config()
    qdir = quarantine_dir(cfg)
    if qdir is None:
        return jsonify({"error": "Add a music folder first"}), 400
    try:
        qdir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return jsonify({"error": "Cannot create %s: %s" % (qdir, exc)}), 500

    conn = db()
    moved, errors, skipped = [], [], []
    for tid in ids:
        row = conn.execute("SELECT * FROM tracks WHERE id=?", (tid,)).fetchone()
        if not row:
            continue
        if not os.path.exists(row["path"]):
            conn.execute("UPDATE tracks SET missing=1 WHERE id=?", (tid,))
            skipped.append(row["filename"])
            continue
        dest = qdir / row["filename"]
        n = 2
        while dest.exists():
            stem, suf = Path(row["filename"]).stem, Path(row["filename"]).suffix
            dest = qdir / ("%s (%d)%s" % (stem, n, suf))
            n += 1
        try:
            shutil.move(row["path"], str(dest))
            conn.execute("UPDATE tracks SET missing=1 WHERE id=?", (tid,))
            moved.append(str(dest))
        except Exception as exc:
            errors.append("%s: %s" % (row["filename"], exc))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "moved": moved, "errors": errors,
                    "skipped": skipped, "folder": str(qdir),
                    "protected": len(protect & set(requested))})


# ---- versions api ----

def _canon_song(conn, name, exclude=""):
    """Reuse the existing spelling of a song so 'redline' joins 'Redline'.
    `exclude` is the group being renamed, so it can change its own casing."""
    row = conn.execute(
        "SELECT song FROM versions WHERE status='grouped' AND song=? COLLATE NOCASE "
        "AND song<>? COLLATE NOCASE LIMIT 1", (name, exclude or "\x00")).fetchone()
    return row["song"] if row else name


@app.get("/api/versions")
def api_versions():
    return jsonify(build_versions())


@app.post("/api/versions/guess")
def api_versions_guess():
    """A sensible song name for a hand-picked set of tracks."""
    ids = [int(i) for i in (request.get_json(force=True) or {}).get("ids", [])]
    conn = db()
    names = {}
    for tid in ids:
        row = conn.execute("SELECT * FROM tracks WHERE id=?", (tid,)).fetchone()
        if row:
            base = version_info(dict(row))[0]
            names[base] = names.get(base, 0) + 1
    conn.close()
    song = sorted(names, key=lambda n: (-names[n], len(n), n))[0] if names else ""
    return jsonify({"song": song})


@app.post("/api/versions/save")
def api_versions_save():
    """Group tracks as versions of one song. items: [{id, label?}] — a missing
    label is worked out from the title/filename."""
    body = request.get_json(force=True) or {}
    song = (body.get("song") or "").strip()
    items = body.get("items") or []
    if not song:
        return jsonify({"error": "Give the song a name"}), 400
    if not items:
        return jsonify({"error": "No tracks to group"}), 400
    conn = db()
    song = _canon_song(conn, song)
    n = 0
    for it in items:
        row = conn.execute("SELECT * FROM tracks WHERE id=?", (int(it["id"]),)).fetchone()
        if not row:
            continue
        label = it.get("label")
        if label is None:
            label = version_info(dict(row))[1]
        conn.execute(
            "INSERT INTO versions (track_id, song, label, status) VALUES (?,?,?,'grouped') "
            "ON CONFLICT(track_id) DO UPDATE SET song=excluded.song, "
            "label=excluded.label, status='grouped'",
            (row["id"], song, (label or "").strip()))
        n += 1
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "grouped": n, "song": song})


@app.post("/api/versions/rename")
def api_versions_rename():
    body = request.get_json(force=True) or {}
    old, new = (body.get("song") or "").strip(), (body.get("name") or "").strip()
    if not old or not new:
        return jsonify({"error": "Give the song a name"}), 400
    conn = db()
    new = _canon_song(conn, new, exclude=old)
    conn.execute("UPDATE versions SET song=? WHERE status='grouped' AND song=? COLLATE NOCASE",
                 (new, old))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "song": new})


@app.post("/api/versions/label")
def api_versions_label():
    body = request.get_json(force=True) or {}
    conn = db()
    conn.execute("UPDATE versions SET label=? WHERE track_id=? AND status='grouped'",
                 ((body.get("label") or "").strip(), int(body["id"])))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.post("/api/versions/dismiss")
def api_versions_dismiss():
    """Take tracks out of their group, or reject a suggestion. Either way they
    are remembered as 'not a version' so they are not suggested again."""
    ids = [int(i) for i in (request.get_json(force=True) or {}).get("ids", [])]
    conn = db()
    for tid in ids:
        conn.execute(
            "INSERT INTO versions (track_id, song, label, status) VALUES (?,'','','ignored') "
            "ON CONFLICT(track_id) DO UPDATE SET song='', label='', status='ignored'", (tid,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "n": len(ids)})


@app.post("/api/versions/dissolve")
def api_versions_dissolve():
    song = ((request.get_json(force=True) or {}).get("song") or "").strip()
    conn = db()
    conn.execute("UPDATE versions SET song='', label='', status='ignored' "
                 "WHERE status='grouped' AND song=? COLLATE NOCASE", (song,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.post("/api/versions/titles")
def api_versions_titles():
    """Write 'Song (Label)' into each version's title so they are tellable
    apart in any player. Previewed first; one undo batch when applied."""
    body = request.get_json(force=True) or {}
    wanted = {s_.lower() for s_ in (body.get("songs") or [])}
    set_album = bool(body.get("album"))
    cfg = load_config()
    data = build_versions()
    per_track, preview = {}, []
    for g in data["groups"]:
        if wanted and g["song"].lower() not in wanted:
            continue
        titles = version_titles(g["song"], g["tracks"])
        for t in g["tracks"]:
            changes = {}
            if (t.get("title") or "") != titles[t["id"]]:
                changes["title"] = titles[t["id"]]
            if set_album and (t.get("album") or "") != g["song"]:
                changes["album"] = g["song"]
            if changes:
                per_track[t["id"]] = changes
                preview.append({"id": t["id"], "filename": t["filename"],
                                "from": t.get("title") or "", "to": titles[t["id"]],
                                "album": changes.get("album", "")})
    if not body.get("apply"):
        return jsonify({"preview": preview})
    conn = db()
    applied, failed, batch = apply_changes(
        conn, cfg, per_track, "Version titles on %d tracks" % len(per_track))
    conn.close()
    return jsonify({"ok": True, "changed": applied, "failed": failed, "batch": batch})


# --------------------------------------------------------------------------
# player — favorites and playlists
# --------------------------------------------------------------------------

PLAYER_FIELDS = ("id", "title", "artist", "album", "filename", "folder", "duration",
                 "has_art", "first_seen", "mtime", "rating", "year", "genre")


@app.get("/api/player/library")
def api_player_library():
    """Every playable track with just what the player needs, plus favorites."""
    conn = db()
    rows = conn.execute(
        "SELECT t.id, t.title, t.artist, t.album, t.filename, t.folder, t.duration, "
        "t.has_art, t.first_seen, t.mtime, t.rating, t.year, t.genre, t.track_no, t.albumartist, "
        "t.gain_db, t.has_synced, (COALESCE(t.lyrics,'')<>'') AS has_lyrics, COALESCE(t.era,'') AS era, "
        "t.producer, t.bpm, t.music_key, "
        "f.track_id IS NOT NULL AS favorite, f.added AS fav_added, "
        "v.song AS song, v.label AS version_label, "
        "COALESCE(pc.count, 0) AS plays, pc.last_played AS last_played "
        "FROM tracks t LEFT JOIN favorites f ON f.track_id=t.id "
        "LEFT JOIN plays pc ON pc.track_id=t.id "
        "LEFT JOIN versions v ON v.track_id=t.id AND v.status='grouped' "
        "WHERE t.missing=0").fetchall()
    conn.close()
    return jsonify({"tracks": [dict(r) for r in rows]})


@app.get("/api/player/state")
def api_player_state_get():
    conn = db()
    row = conn.execute("SELECT value FROM kv WHERE key='player_state'").fetchone()
    conn.close()
    try:
        state = json.loads(row["value"]) if row else {}
    except ValueError:
        state = {}
    return jsonify({"state": state})


@app.post("/api/player/state")
def api_player_state_set():
    raw = request.get_data(as_text=True) or "{}"
    if len(raw) > 4_000_000:
        return jsonify({"error": "state too large"}), 413
    try:
        state = json.loads(raw)
    except ValueError:
        return jsonify({"error": "bad json"}), 400
    if not isinstance(state, dict):
        return jsonify({"error": "bad state"}), 400
    conn = db()
    conn.execute("INSERT INTO kv (key, value) VALUES ('player_state', ?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(state),))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.post("/api/player/played/<int:tid>")
def api_player_played(tid):
    now = time.time()
    conn = db()
    if not conn.execute("SELECT 1 FROM tracks WHERE id=?", (tid,)).fetchone():
        conn.close()
        return jsonify({"error": "not found"}), 404
    conn.execute("INSERT INTO plays (track_id, count, last_played) VALUES (?, 1, ?) "
                 "ON CONFLICT(track_id) DO UPDATE SET count=count+1, last_played=excluded.last_played",
                 (tid, now))
    row = conn.execute("SELECT count FROM plays WHERE track_id=?", (tid,)).fetchone()
    conn.execute("INSERT INTO play_log (track_id, ts) VALUES (?,?)", (tid, now))
    conn.execute("DELETE FROM play_log WHERE id < (SELECT MAX(id) - 2000 FROM play_log)")
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "plays": row["count"], "last_played": now})


def _track_row(tid):
    conn = db()
    row = conn.execute("SELECT * FROM tracks WHERE id=?", (tid,)).fetchone()
    conn.close()
    return row


@app.get("/api/lyrics/<int:tid>")
def api_lyrics_get(tid):
    row = _track_row(tid)
    if not row or not os.path.exists(row["path"]):
        return jsonify({"error": "missing"}), 404
    lf = read_lyrics_full(row["path"])
    timed = None
    if lf["synced"]:
        timed = []
        for i, (t, line) in enumerate(lf["synced"]):
            item = {"t": t, "text": line}
            if lf["words"] and lf["words"][i]:
                item["words"] = [{"t": wt, "w": w} for wt, w in lf["words"][i]]
                if lf["ends"][i] is not None:
                    item["end"] = lf["ends"][i]
            timed.append(item)
    return jsonify({"lyrics": lf["plain"],
                    "synced": timed, "by_word": bool(lf["words"]),
                    "source": lf["source"],
                    "editable": (row["ext"] or "").lower() in ("mp3", "m4a", "mp4", "aac", "flac", "ogg", "opus", "wav", "aif", "aiff")})


@app.post("/api/lyrics/<int:tid>")
def api_lyrics_set(tid):
    body = request.get_json(force=True) or {}
    text = body.get("lyrics") or ""
    synced = body.get("synced")
    if len(text) > 200_000:
        return jsonify({"error": "Lyrics are too long"}), 400
    lines = None
    words = ends = None
    if synced:
        try:
            order = sorted(synced, key=lambda x: float(x["t"]))
            lines = [(max(0.0, float(x["t"])), str(x.get("text", ""))[:500]) for x in order]
            words, ends = [], []
            for x in order:
                ws = x.get("words")
                if ws:
                    ws = [(max(0.0, float(w["t"])), str(w["w"])[:80]) for w in ws if str(w.get("w", "")).strip()]
                words.append(ws or None)
                ends.append(float(x["end"]) if x.get("end") is not None and ws else None)
            if not any(words):
                words = ends = None
        except (TypeError, ValueError, KeyError):
            return jsonify({"error": "Bad timed lyrics"}), 400
        if len(lines) > 2000:
            return jsonify({"error": "Too many lines"}), 400
    row = _track_row(tid)
    if not row or not os.path.exists(row["path"]):
        return jsonify({"error": "That file is missing"}), 404
    try:
        ok = write_lyrics(row["path"], text, lines, words, ends)
    except Exception as exc:
        return jsonify({"error": "Couldn't save lyrics: %s" % exc}), 500
    if not ok:
        return jsonify({"error": "This file type can't hold lyrics"}), 400
    lf = read_lyrics_full(row["path"])
    conn = db()
    st = os.stat(row["path"])
    conn.execute("UPDATE tracks SET lyrics=?, has_synced=?, size=?, mtime=? WHERE id=?",
                 (lf["plain"][:20000], 1 if lf["synced"] else 0, st.st_size, st.st_mtime, tid))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "synced": bool(lf["synced"])})


@app.get("/api/lyrics/<int:tid>/lookup")
def api_lyrics_lookup(tid):
    row = _track_row(tid)
    if not row:
        return jsonify({"error": "missing"}), 404
    title = split_version(row["title"] or Path(row["filename"]).stem)[0] or row["title"] or ""
    artist = (row["artist"] or "").split(",")[0].split(" feat")[0].split(" ft.")[0].strip()
    if not title or not artist:
        return jsonify({"found": False, "reason": "This song needs a title and artist tag to look it up."})
    try:
        hit = lrclib_lookup(title, artist, row["album"] or "", row["duration"] or 0)
    except Exception:
        return jsonify({"error": "Couldn't reach the lyrics service — check your internet connection."}), 502
    if not hit:
        return jsonify({"found": False, "reason": "No lyrics found online for %s by %s." % (title, artist)})
    timed = parse_lrc(hit.get("syncedLyrics") or "")
    return jsonify({"found": True, "title": hit.get("trackName") or title, "artist": hit.get("artistName") or artist,
                    "lyrics": (hit.get("plainLyrics") or plain_from_lrc(hit.get("syncedLyrics") or "")).strip(),
                    "synced": [{"t": t, "text": line} for t, line in timed] if timed else None})


@app.get("/api/lyrics/<int:tid>/find")
def api_lyrics_find(tid):
    row = _track_row(tid)
    if not row:
        return jsonify({"error": "missing"}), 404
    res = find_lyrics(row, request.args.get("q"))
    res["default_query"] = ("%s %s" % ((row["artist"] or "").split(",")[0].strip(),
                                       clean_song_title(row["title"] or Path(row["filename"]).stem))).strip()
    return jsonify(res)


@app.get("/api/lyrics/genius")
def api_lyrics_genius():
    url = request.args.get("url") or ""
    try:
        text = genius_lyrics(url)
    except ValueError:
        return jsonify({"error": "That isn't a Genius link"}), 400
    except Exception:
        return jsonify({"error": "Couldn't load the lyrics from Genius — try another result."}), 502
    if not text:
        return jsonify({"error": "That page has no lyrics yet."}), 404
    return jsonify({"lyrics": text})


@app.get("/api/autosync/status")
def api_autosync_status():
    ok, reason = autosync_engine()
    cfg = load_config()
    name = cfg.get("autosync_model") or "small"
    dl = None
    if AS["downloading"]:
        dl = {"name": AS["downloading"], "done": _dir_size(_model_folder(AS["downloading"])),
              "total": AUTOSYNC_MODELS.get(AS["downloading"], 0)}
    with AS["lock"]:
        jobs = [_job_public(j) for j in AS["jobs"][-60:]]
    return jsonify({"available": ok, "reason": reason, "model": name,
                    "language": cfg.get("autosync_language", "en"), "precise": bool(cfg.get("autosync_precise", True)),
                    "transcribe_model": cfg.get("transcribe_model") or "same",
                    "transcribe_lookup": bool(cfg.get("transcribe_lookup", True)),
                    "ready": {k: model_ready(k) for k in AUTOSYNC_MODELS},
                    "sizes": AUTOSYNC_MODELS, "downloading": dl, "jobs": jobs})


@app.post("/api/autosync")
def api_autosync():
    ok, reason = autosync_engine()
    if not ok:
        return jsonify({"error": reason}), 400
    body = request.get_json(force=True) or {}
    j = autosync_enqueue(int(body.get("id") or 0), autosave=bool(body.get("autosave")))
    if not j:
        return jsonify({"error": "missing"}), 404
    return jsonify({"ok": True, "job": _job_public(j)})


@app.post("/api/autosync/all")
def api_autosync_all():
    ok, reason = autosync_engine()
    if not ok:
        return jsonify({"error": reason}), 400
    conn = db()
    rows = conn.execute("SELECT id FROM tracks WHERE missing=0 AND COALESCE(lyrics,'')<>'' "
                        "AND COALESCE(has_synced,0)=0 ORDER BY title").fetchall()
    conn.close()
    n = sum(1 for r in rows if autosync_enqueue(r["id"], autosave=True))
    return jsonify({"ok": True, "queued": n})


@app.post("/api/autosync/cancel")
def api_autosync_cancel():
    body = request.get_json(force=True) or {}
    jid = body.get("job")
    with AS["lock"]:
        for j in AS["jobs"]:
            if (jid is None or j["id"] == jid) and j["state"] in ("queued", "running", "loading", "matching"):
                if j["state"] == "queued":
                    j["state"] = "cancelled"
                else:
                    j["cancel"] = True
    return jsonify({"ok": True})


@app.post("/api/autosync/clear")
def api_autosync_clear():
    with AS["lock"]:
        AS["jobs"] = [j for j in AS["jobs"] if j["state"] in ("queued", "running", "loading", "matching")]
    return jsonify({"ok": True})


@app.get("/api/lyrics/search")
def api_lyrics_search():
    q = (request.args.get("q") or "").strip()
    if len(q) < 3:
        return jsonify({"hits": []})
    conn = db()
    rows = conn.execute("SELECT id, lyrics FROM tracks WHERE missing=0 AND lyrics LIKE ? LIMIT 300",
                        ("%" + q.replace("%", "").replace("_", "") + "%",)).fetchall()
    conn.close()
    ql = q.lower()
    hits = []
    for r in rows:
        line = next((ln.strip() for ln in (r["lyrics"] or "").split("\n") if ql in ln.lower()), "")
        hits.append({"id": r["id"], "line": line[:140]})
    return jsonify({"hits": hits})


# ---- stats ----

@app.post("/api/player/listened")
def api_player_listened():
    body = request.get_json(force=True) or {}
    try:
        tid, secs = int(body.get("id")), float(body.get("seconds") or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "bad request"}), 400
    row = _track_row(tid)
    if not row:
        return jsonify({"error": "not found"}), 404
    secs = min(secs, (row["duration"] or secs) + 5)
    if secs >= 5:
        conn = db()
        conn.execute("INSERT INTO history (track_id, ts, seconds) VALUES (?,?,?)", (tid, time.time(), secs))
        conn.commit()
        conn.close()
    return jsonify({"ok": True})


@app.get("/api/stats")
def api_stats():
    now = time.time()
    lt = time.localtime(now)
    midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    conn = db()
    q = lambda sql, *a: conn.execute(sql, a).fetchone()[0] or 0
    out = {
        "all_seconds": q("SELECT SUM(seconds) FROM history"),
        "week_seconds": q("SELECT SUM(seconds) FROM history WHERE ts>=?", midnight - 6 * 86400),
        "month_seconds": q("SELECT SUM(seconds) FROM history WHERE ts>=?", midnight - 29 * 86400),
        "today_seconds": q("SELECT SUM(seconds) FROM history WHERE ts>=?", midnight),
        "tracks": q("SELECT COUNT(*) FROM tracks WHERE missing=0"),
        "never_played": q("SELECT COUNT(*) FROM tracks t LEFT JOIN plays p ON p.track_id=t.id "
                          "WHERE t.missing=0 AND COALESCE(p.count,0)=0"),
        "favorites": q("SELECT COUNT(*) FROM favorites f JOIN tracks t ON t.id=f.track_id WHERE t.missing=0"),
    }
    days = []
    for k in range(29, -1, -1):
        a = midnight - k * 86400
        d = time.localtime(a)
        days.append({"day": "%04d-%02d-%02d" % (d.tm_year, d.tm_mon, d.tm_mday),
                     "seconds": q("SELECT SUM(seconds) FROM history WHERE ts>=? AND ts<?", a, a + 86400)})
    out["days"] = days
    out["top_songs"] = [dict(r) for r in conn.execute(
        "SELECT t.id, p.count AS plays FROM plays p JOIN tracks t ON t.id=p.track_id "
        "WHERE t.missing=0 AND p.count>0 ORDER BY p.count DESC, p.last_played DESC LIMIT 10")]
    out["top_artists"] = [dict(r) for r in conn.execute(
        "SELECT COALESCE(NULLIF(t.artist,''),'Unknown artist') AS artist, SUM(p.count) AS plays, "
        "COUNT(DISTINCT t.id) AS songs FROM plays p JOIN tracks t ON t.id=p.track_id "
        "WHERE t.missing=0 AND p.count>0 GROUP BY artist ORDER BY plays DESC LIMIT 8")]
    hrs = [0] * 24
    for r in conn.execute("SELECT ts, seconds FROM history WHERE ts>=?", (midnight - 29 * 86400,)):
        hrs[time.localtime(r["ts"]).tm_hour] += r["seconds"]
    out["hours"] = hrs
    conn.close()
    return jsonify(out)


# ---- discord ----

@app.post("/api/discord")
def api_discord():
    body = request.get_json(force=True) or {}
    cfg = load_config()
    cid = str(cfg.get("discord_client_id") or "").strip()
    if not cfg.get("discord"):
        DISCORD.close()
        return jsonify({"ok": False, "message": "off"})
    activity = None
    tid = body.get("id")
    if tid and body.get("playing"):
        row = _track_row(int(tid))
        if row:
            pos = float(body.get("position") or 0)
            dur = float(row["duration"] or 0)
            start_ms = int((time.time() - pos) * 1000)
            title = (row["title"] or Path(row["filename"]).stem)[:120]
            activity = {"type": 2, "details": title or "Music",
                        "state": ("by " + row["artist"])[:120] if row["artist"] else "AURA",
                        "timestamps": {"start": start_ms, **({"end": start_ms + int(dur * 1000)} if dur else {})},
                        "assets": {"large_image": "aura", "large_text": (row["album"] or "AURA")[:120]},
                        "instance": False}
            cached = cover_link(row, cfg, allow_network=False)
            if cached:
                activity["assets"]["large_image"] = cached
                activity["assets"]["small_image"] = "aura"
                activity["assets"]["small_text"] = "AURA"
            elif cfg.get("discord_art_lookup", True) or cfg.get("discord_art_upload"):
                threading.Thread(target=_discord_fill_art, args=(row["id"], cid), daemon=True).start()
    DISCORD_NOW.update(tid=(int(tid) if tid and activity else None), activity=activity, cid=cid)
    ok, msg = DISCORD.update(cid, activity)
    return jsonify({"ok": ok, "message": msg})


# ---- app version & updates ----

@app.get("/api/bg/status")
def api_bg_status():
    conn = db()
    r = conn.execute("SELECT SUM(gain_db IS NULL) AS level_left, SUM(COALESCE(analysed,0)=0) AS bpm_left, "
                     "SUM(TRIM(COALESCE(bpm,''))<>'') AS with_bpm, SUM(TRIM(COALESCE(music_key,''))<>'') AS with_key, "
                     "COUNT(*) AS total FROM tracks WHERE missing=0").fetchone()
    conn.close()
    return jsonify({k: (r[k] or 0) for k in r.keys()})


@app.get("/api/app/version")
def api_app_version():
    info = app_version_info()
    info["can_install"] = bool(FROZEN and os.name == "nt" and info["repo"])
    info["background"] = dict(BG)
    return jsonify(info)


@app.get("/api/app/update")
def api_app_update():
    try:
        return jsonify(check_update(force=request.args.get("force") == "1"))
    except Exception:
        return jsonify({"available": False, "error": "Couldn't check for updates — are you online?"}), 200


@app.get("/api/convert")
def api_convert_status():
    conn = db()
    n = conn.execute("SELECT COUNT(*) FROM tracks WHERE missing=0 AND LOWER(COALESCE(ext,''))<>'mp3'").fetchone()[0]
    by = [dict(r) for r in conn.execute("SELECT LOWER(ext) AS ext, COUNT(*) AS n FROM tracks WHERE missing=0 "
                                        "AND LOWER(COALESCE(ext,''))<>'mp3' GROUP BY LOWER(ext) ORDER BY n DESC")]
    conn.close()
    with CONV["lock"]:
        jobs = [dict(j) for j in CONV["jobs"]]
    return jsonify({"running": CONV["running"], "jobs": jobs, "convertible": n, "by_ext": by})


@app.post("/api/convert")
def api_convert():
    body = request.get_json(force=True) or {}
    kbps = int(body.get("kbps") or 320)
    if kbps not in (128, 192, 256, 320):
        kbps = 320
    originals = "keep" if body.get("originals") == "keep" else "move"
    with CONV["lock"]:
        if CONV["running"]:
            return jsonify({"error": "A conversion is already running"}), 409
        conn = db()
        if body.get("all"):
            rows = conn.execute("SELECT id, title, filename FROM tracks WHERE missing=0 "
                                "AND LOWER(COALESCE(ext,''))<>'mp3' ORDER BY folder, filename").fetchall()
        else:
            ids = [int(i) for i in body.get("ids") or []]
            rows = [r for r in (conn.execute("SELECT id, title, filename, ext FROM tracks WHERE id=?", (i,)).fetchone()
                                for i in ids) if r and (r["ext"] or "").lower() != "mp3"]
        conn.close()
        if not rows:
            return jsonify({"error": "Those are all MP3s already"}), 400
        CONV["jobs"] = [{"track_id": r["id"], "name": r["title"] or r["filename"], "file": r["filename"],
                         "state": "queued", "progress": 0.0, "note": "", "mp3": "", "original": "", "saved": 0}
                        for r in rows]
        CONV.update(running=True, cancel=False)
    threading.Thread(target=_convert_worker, args=(kbps, originals), daemon=True).start()
    return jsonify({"ok": True, "queued": len(rows)})


@app.post("/api/convert/cancel")
def api_convert_cancel():
    CONV["cancel"] = True
    return jsonify({"ok": True})


def _touch_file(conn, tid, path):
    """After writing into a file, remember its new size/date so a rescan doesn't redo it."""
    try:
        st = os.stat(path)
        conn.execute("UPDATE tracks SET size=?, mtime=? WHERE id=?", (st.st_size, st.st_mtime, tid))
    except OSError:
        pass


# ---- cover art finder ----

DEEZER_URL = "https://api.deezer.com"
MUSICBRAINZ_URL = "https://musicbrainz.org"
CAA_URL = "https://coverartarchive.org"
COVER_HOSTS = ("mzstatic.com", "dzcdn.net", "deezer.com", "genius.com", "coverartarchive.org", "archive.org")


def _cov_apple(q):
    out = []
    for ent in ("album", "song"):
        res = http_json(ITUNES_URL + "/search?" + urllib.parse.urlencode(
            {"term": q, "entity": ent, "limit": 12}), timeout=6).get("results") or []
        for r in res:
            u = r.get("artworkUrl100")
            if u:
                out.append({"source": "Apple", "url": u.replace("100x100bb", "1000x1000bb"),
                            "thumb": u.replace("100x100bb", "300x300bb"),
                            "title": r.get("collectionName") if ent == "album" else r.get("trackName"),
                            "artist": r.get("artistName") or ""})
    return out


def _cov_deezer(q):
    res = http_json(DEEZER_URL + "/search/album?" + urllib.parse.urlencode({"q": q, "limit": 15}),
                    timeout=6).get("data") or []
    return [{"source": "Deezer", "url": r["cover_xl"], "thumb": r.get("cover_medium") or r["cover_xl"],
             "title": r.get("title") or "", "artist": (r.get("artist") or {}).get("name") or ""}
            for r in res if r.get("cover_xl")]


def _cov_genius(q):
    """Fans add cover art for a lot of unreleased songs here."""
    data = json.loads(http_text(GENIUS_URL + "/api/search/multi?" + urllib.parse.urlencode({"per_page": 5, "q": q}),
                                timeout=6))
    out = []
    for sec in (data.get("response") or {}).get("sections") or []:
        for hit in sec.get("hits") or []:
            r = hit.get("result") or {}
            if hit.get("type") not in ("song", "album"):
                continue
            u = r.get("song_art_image_url") or r.get("cover_art_url") or ""
            if u and "default_cover" not in u and "default_avatar" not in u:
                out.append({"source": "Genius", "url": u,
                            "thumb": r.get("song_art_image_thumbnail_url") or r.get("cover_art_thumbnail_url") or u,
                            "title": r.get("title") or r.get("name") or "",
                            "artist": (r.get("primary_artist") or r.get("artist") or {}).get("name") or ""})
    return out


def _cov_musicbrainz(q):
    res = http_json(MUSICBRAINZ_URL + "/ws/2/release-group/?" + urllib.parse.urlencode(
        {"query": q, "fmt": "json", "limit": 10}), timeout=6).get("release-groups") or []
    return [{"source": "MusicBrainz", "url": "%s/release-group/%s/front-1200" % (CAA_URL, r["id"]),
             "thumb": "%s/release-group/%s/front-250" % (CAA_URL, r["id"]), "title": r.get("title") or "",
             "artist": ((r.get("artist-credit") or [{}])[0].get("name") or "")} for r in res]


COVER_SOURCES = (("Genius", _cov_genius), ("Apple", _cov_apple), ("Deezer", _cov_deezer), ("MusicBrainz", _cov_musicbrainz))
NET_POOL = ThreadPoolExecutor(max_workers=12)


def run_parallel(jobs, limit=8.0):
    """jobs: [(name, fn, arg)]. Runs them all at once; anything slower than
    `limit` seconds is left out instead of holding everything up."""
    from concurrent.futures import wait
    futs = {NET_POOL.submit(fn, arg): name for name, fn, arg in jobs}
    done, late = wait(futs, timeout=limit)
    results, errors = {}, []
    for f in done:
        try:
            results[futs[f]] = f.result()
        except Exception as exc:
            errors.append("%s: %s" % (futs[f], exc))
    for f in late:
        errors.append("%s: too slow" % futs[f])
    return results, errors


def cover_search(q):
    res, errors = run_parallel([(n, fn, q) for n, fn in COVER_SOURCES])
    lists = [res.get(n) or [] for n, _ in COVER_SOURCES]
    out, seen = [], set()
    for i in range(max([len(l) for l in lists] + [0])):      # mix the sources together
        for l in lists:
            if i < len(l) and l[i]["thumb"] not in seen:
                seen.add(l[i]["thumb"])
                out.append(l[i])
    return out[:60], errors


@app.get("/api/covers/search")
def api_covers_search():
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"results": [], "errors": []})
    res, errors = cover_search(q)
    return jsonify({"results": res, "errors": errors, "offline": bool(errors) and not res})


@app.post("/api/covers/apply")
def api_covers_apply():
    body = request.get_json(force=True) or {}
    url = (body.get("url") or "").strip()
    host = urllib.parse.urlparse(url).hostname or ""
    own_link = bool(body.get("own_link"))      # a link the user pasted themselves
    if not url.startswith(("https://", "http://") if own_link else ("https://",)) or not host or (
            not own_link and not any(host == h or host.endswith("." + h) for h in COVER_HOSTS)):
        return jsonify({"error": "That picture can't be used"}), 400
    try:
        req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA if own_link else HTTP_UA,
                                                   "Accept": "image/*,*/*;q=0.5"})
        with urllib.request.urlopen(req, timeout=20) as r:
            mime = (r.headers.get("Content-Type") or "image/jpeg").split(";")[0]
            data = r.read(12_000_000)
        if mime in ("application/octet-stream", "binary/octet-stream"):
            mime = "image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" else (
                "image/jpeg" if data[:3] == b"\xff\xd8\xff" else "image/webp" if data[8:12] == b"WEBP" else mime)
    except Exception as exc:
        return jsonify({"error": "Couldn't download the picture: %s" % exc}), 502
    if not mime.startswith("image/") or len(data) < 500:
        return jsonify({"error": "That link isn't a picture"}), 400
    conn = db()
    done, failed, err = 0, 0, ""
    for tid in [int(i) for i in body.get("ids") or []]:
        row = conn.execute("SELECT path FROM tracks WHERE id=?", (tid,)).fetchone()
        if not row:
            continue
        try:
            set_artwork(row["path"], data, mime)
            conn.execute("UPDATE tracks SET has_art=1 WHERE id=?", (tid,))
            _touch_file(conn, tid, row["path"])
            done += 1
        except Exception as exc:
            failed += 1
            err = str(exc)
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "changed": done, "failed": failed, "error": err})


@app.post("/api/covers/copy")
def api_covers_copy():
    """Use the cover of one song on others (e.g. every version of a song)."""
    body = request.get_json(force=True) or {}
    src = _track_row(int(body.get("from") or 0))
    if not src:
        return jsonify({"error": "missing"}), 404
    data, mime = get_artwork(src["path"])
    if not data:
        return jsonify({"error": "That song has no cover"}), 400
    conn = db()
    done, failed, err = 0, 0, ""
    for tid in [int(i) for i in body.get("ids") or [] if int(i) != src["id"]]:
        row = conn.execute("SELECT path FROM tracks WHERE id=?", (tid,)).fetchone()
        if not row:
            continue
        try:
            set_artwork(row["path"], data, mime)
            conn.execute("UPDATE tracks SET has_art=1 WHERE id=?", (tid,))
            _touch_file(conn, tid, row["path"])
            done += 1
        except Exception as exc:
            failed += 1
            err = str(exc)
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "changed": done, "failed": failed, "error": err})


# ---- bookmarks ----

@app.get("/api/bookmarks/<int:tid>")
def api_bookmarks(tid):
    conn = db()
    rows = conn.execute("SELECT id, t, label FROM bookmarks WHERE track_id=? ORDER BY t", (tid,)).fetchall()
    conn.close()
    return jsonify({"items": [dict(r) for r in rows]})


@app.post("/api/bookmarks/<int:tid>")
def api_bookmarks_add(tid):
    body = request.get_json(force=True) or {}
    t = max(0.0, float(body.get("t") or 0))
    conn = db()
    if not conn.execute("SELECT 1 FROM tracks WHERE id=?", (tid,)).fetchone():
        conn.close()
        return jsonify({"error": "missing"}), 404
    if conn.execute("SELECT 1 FROM bookmarks WHERE track_id=? AND ABS(t-?)<0.75", (tid, t)).fetchone():
        conn.close()
        return jsonify({"error": "There's already a bookmark there"}), 409
    cur = conn.execute("INSERT INTO bookmarks (track_id, t, label, created) VALUES (?,?,?,?)",
                       (tid, round(t, 2), (body.get("label") or "").strip()[:80], time.time()))
    conn.commit()
    bid = cur.lastrowid
    conn.close()
    return jsonify({"ok": True, "id": bid})


@app.post("/api/bookmarks/edit")
def api_bookmarks_edit():
    body = request.get_json(force=True) or {}
    conn = db()
    if body.get("delete"):
        conn.execute("DELETE FROM bookmarks WHERE id=?", (int(body.get("id") or 0),))
    else:
        conn.execute("UPDATE bookmarks SET label=? WHERE id=?", ((body.get("label") or "").strip()[:80], int(body.get("id") or 0)))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ---- snippet alerts ----

@app.get("/api/alerts")
def api_alerts():
    conn = db()
    out = []
    for r in conn.execute("SELECT * FROM snippet_alerts WHERE dismissed=0 ORDER BY ts DESC").fetchall():
        full = conn.execute("SELECT id, title, filename FROM tracks WHERE id=? AND missing=0", (r["full_id"],)).fetchone()
        sn = [x for x in (conn.execute("SELECT id, title, filename FROM tracks WHERE id=? AND missing=0", (i,)).fetchone()
                          for i in json.loads(r["snippet_ids"] or "[]")) if x]
        if full and sn:
            out.append({"id": r["id"], "song": r["song"], "ts": r["ts"], "full": dict(full), "snippets": [dict(x) for x in sn]})
    conn.close()
    return jsonify({"alerts": out})


@app.post("/api/alerts/check")
def api_alerts_check():
    conn = db()
    n = find_snippet_alerts(conn, None)
    conn.close()
    return jsonify({"ok": True, "found": n})


@app.post("/api/alerts/<int:aid>")
def api_alerts_act(aid):
    body = request.get_json(force=True) or {}
    conn = db()
    r = conn.execute("SELECT * FROM snippet_alerts WHERE id=?", (aid,)).fetchone()
    if not r:
        conn.close()
        return jsonify({"error": "missing"}), 404
    grouped = 0
    if body.get("group"):
        song = _canon_song(conn, (body.get("song") or r["song"] or "").strip() or "Untitled")
        for tid, default_label in [(r["full_id"], None)] + [(i, "Snippet") for i in json.loads(r["snippet_ids"] or "[]")]:
            row = conn.execute("SELECT * FROM tracks WHERE id=?", (tid,)).fetchone()
            if not row:
                continue
            label = version_info(dict(row))[1] or default_label or ""
            if default_label and "snippet" not in label.lower():
                label = (label + " · Snippet").strip(" ·") if label else "Snippet"
            conn.execute("INSERT INTO versions (track_id, song, label, status) VALUES (?,?,?,'grouped') "
                         "ON CONFLICT(track_id) DO UPDATE SET song=excluded.song, label=excluded.label, status='grouped'",
                         (tid, song, label))
            grouped += 1
    conn.execute("UPDATE snippet_alerts SET dismissed=1 WHERE id=?", (aid,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "grouped": grouped})


# ---- compare versions: line two takes up in time ----

@app.get("/api/compare/offset")
def api_compare_offset():
    a, b = _track_row(int(request.args.get("a") or 0)), _track_row(int(request.args.get("b") or 0))
    if not a or not b:
        return jsonify({"error": "missing"}), 404
    sr = 8000
    xa = decode_mono(a["path"], target_sr=sr, max_seconds=150)
    xb = decode_mono(b["path"], target_sr=sr, max_seconds=150)
    if xa is None or xb is None:
        return jsonify({"offset": 0.0, "confidence": 0.0})
    def bands(x):
        S = _stft_mag(x, 1024, 256)
        if S is None:
            return None
        edges = np.unique(np.geomspace(3, S.shape[1] - 1, 9).astype(int))
        B = np.stack([S[:, a:b].sum(axis=1) for a, b in zip(edges[:-1], edges[1:])], axis=1)
        B = np.maximum(0, np.diff(np.log1p(B * 10), axis=0))
        B -= B.mean(axis=0)
        B /= B.std(axis=0) + 1e-6
        return B
    ea, eb = bands(xa), bands(xb)
    if ea is None or eb is None or len(ea) < 50 or len(eb) < 50:
        return jsonify({"offset": 0.0, "confidence": 0.0})
    fps = sr / 256.0
    n = len(ea) + len(eb)
    cc = np.zeros(n)
    for k in range(ea.shape[1]):
        cc += np.fft.irfft(np.fft.rfft(ea[:, k], n) * np.conj(np.fft.rfft(eb[:, k], n)), n)
    cc /= ea.shape[1]
    lags = np.concatenate([np.arange(0, len(ea)), np.arange(-len(eb), 0)])
    cc = np.concatenate([cc[:len(ea)], cc[n - len(eb):]])
    lim = int(45 * fps)
    ok = np.abs(lags) <= lim
    cc, lags = cc[ok], lags[ok]
    # Beats repeat, so several lags look alike at this coarse level. Take the best few
    # and settle it with a fine (1 ms) loudness envelope, where only the true lag lines up.
    def env(x):
        e = np.abs(x)
        e = np.convolve(e, np.ones(8) / 8, mode="same")[::8]      # 1000 per second
        return np.maximum(0, np.diff(e)).astype(np.float32)
    fa, fb = env(xa), env(xb)

    def fine(sec):
        best = (-9.0, sec)
        k0 = int(round(sec * 1000))
        for k in range(k0 - 40, k0 + 41):
            if k >= 0:
                a, b = fa[k:k + len(fb)], fb
            else:
                a, b = fa, fb[-k:]
            m = min(len(a), len(b))
            if m < 2000:
                continue
            a, b = a[:m], b[:m]
            sa, sb = a.std(), b.std()
            if not sa or not sb:
                continue
            r = float(((a - a.mean()) * (b - b.mean())).mean() / (sa * sb))
            if r > best[0]:
                best = (r, k / 1000.0)
        return best

    order = np.argsort(cc)[::-1]
    picks = []
    for j in order:
        if all(abs(int(lags[j]) - int(lags[p_])) > fps * 0.3 for p_ in picks):
            picks.append(j)
        if len(picks) >= 6:
            break
    results = [fine(float(lags[j]) / fps) for j in picks]
    conf, lag_s = max(results)
    # a positive lag means A is later: B time = A time - lag
    return jsonify({"offset": round(-lag_s, 3), "confidence": round(max(0.0, min(1.0, conf)), 3)})


# ---- fan tracker import ----

TRACKER = {}          # token -> {"headers": [...], "rows": [[...]], "name": str}
TRACKER_FIELDS = {
    "name": ("name", "title", "song", "track"),
    "era": ("era", "album", "project", "tape", "section", "sheet"),
    "producer": ("prod", "producer", "producers", "beat"),
    "recorded": ("recorded", "record date", "recording date", "file date", "session date", "date recorded", "made"),
    "leaked": ("leak", "leaked", "leak date", "date leaked", "surfaced", "release date", "released"),
    "notes": ("notes", "note", "description", "info", "details"),
    "quality": ("quality",),
    "type": ("type", "status", "availability", "available"),
    "features": ("feat", "features", "featuring"),
}


def _cell(v):
    if v is None:
        return ""
    if hasattr(v, "strftime"):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def load_tracker_bytes(data, name):
    """CSV or XLSX -> (headers, rows). Every sheet of a workbook is combined,
    with the sheet's name in a 'Sheet' column (trackers often keep one era per tab)."""
    low = (name or "").lower()
    if low.endswith((".xlsx", ".xlsm")) or data[:2] == b"PK":
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        all_rows, headers = [], None
        for ws in wb.worksheets:
            rows = [[_cell(c) for c in r] for r in ws.iter_rows(values_only=True)]
            rows = [r for r in rows if any(r)]
            if not rows:
                continue
            hi = _header_row(rows)
            h = [x or ("Column %d" % (i + 1)) for i, x in enumerate(rows[hi])]
            if headers is None:
                headers = h + ["Sheet"]
            for r in rows[hi + 1:]:
                r = (r + [""] * len(h))[:len(h)]
                # line columns up by header name when tabs differ
                if h != headers[:-1]:
                    m = {hh.lower(): v for hh, v in zip(h, r)}
                    r = [m.get(hh.lower(), "") for hh in headers[:-1]]
                all_rows.append(r + [ws.title])
        return headers or [], all_rows
    import csv
    text = data.decode("utf-8-sig", "replace")
    rows = [[c.strip() for c in r] for r in csv.reader(io.StringIO(text))]
    rows = [r for r in rows if any(r)]
    if not rows:
        return [], []
    hi = _header_row(rows)
    h = [x or ("Column %d" % (i + 1)) for i, x in enumerate(rows[hi])]
    return h, [(r + [""] * len(h))[:len(h)] for r in rows[hi + 1:]]


def _header_row(rows):
    """Trackers often have a title banner above the real header row."""
    best, best_s = 0, -1
    for i, r in enumerate(rows[:15]):
        s = sum(1 for c in r for keys in TRACKER_FIELDS.values() if c and any(k in c.lower() for k in keys))
        if s > best_s:
            best, best_s = i, s
    return best


def guess_mapping(headers):
    m = {}
    for field, keys in TRACKER_FIELDS.items():
        for i, h in enumerate(headers):
            hl = h.lower()
            if i in m.values():
                continue
            if any(hl == k or hl.startswith(k) or (" " + k) in hl for k in keys):
                m[field] = i
                break
    return m


def tracker_names(name):
    """'Bad Energy (Wishing Well) [prod. Nick Mira]' -> main title, alt titles, producer, features."""
    raw = (name or "").replace("\r", "\n")
    lines = [l.strip() for l in raw.split("\n") if l.strip()]
    if not lines:
        return "", [], "", ""
    first = lines[0]
    prod = ""
    m = PROD_RE.search(first)
    if m:
        prod = m.group(1).strip(" -–—_.&,")
        first = (first[:m.start()] + " " + first[m.end():]).strip()
    feats = ""
    m = FEAT_RE.search(first)
    if m:
        feats = m.group(1).strip(" -–—_.&,")
        first = (first[:m.start()] + " " + first[m.end():]).strip()
    alts = []
    for inner in re.findall(r"[\(\[]([^\)\]]+)[\)\]]", first):
        if not re.search(r"\b(v\d|ver|version|snippet|og|cdq|hq|lq|leak|session|remix|edit|demo|ref)\b", inner, re.I):
            alts.extend(a.strip() for a in re.split(r"[,/]| & ", inner) if a.strip())
    main = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]\s*", " ", first)
    parts = [p.strip() for p in re.split(r"\s+/\s+", main) if p.strip()]
    main = parts[0] if parts else main.strip()
    alts = parts[1:] + alts
    for l in lines[1:]:
        l = re.sub(r"^[\(\[]|[\)\]]$", "", l).strip()
        if l and len(l) < 60:
            alts.extend(a.strip() for a in re.split(r"[,/]", l) if a.strip())
    return re.sub(r"\s+", " ", main).strip(" -"), alts, prod, feats


def _year_of(s):
    m = re.search(r"\b(19[6-9]\d|20[0-4]\d)\b", s or "")
    return m.group(1) if m else ""


def match_tracker(entry, mapping, fields, only_ids=None, overwrite=False):
    headers, rows = entry["headers"], entry["rows"]
    get = lambda r, f: (r[mapping[f]] if f in mapping and mapping[f] is not None and mapping[f] < len(r) else "")
    items, cur_era = [], ""
    has_era_col = "era" in mapping and mapping["era"] is not None
    for r in rows:
        name = get(r, "name")
        others = [c for i, c in enumerate(r) if c and i != mapping.get("name") and headers[i] != "Sheet"]
        if name and not others and not has_era_col:
            cur_era = re.sub(r"\s+", " ", name).strip()      # a section heading row
            continue
        if not name:
            continue
        main, alts, prod, feats = tracker_names(name)
        if not main:
            continue
        era = get(r, "era") or cur_era
        items.append({"main": main, "alts": alts, "producer": get(r, "producer") or prod,
                      "features": get(r, "features") or feats, "era": era,
                      "recorded": get(r, "recorded"), "leaked": get(r, "leaked"), "notes": get(r, "notes"),
                      "quality": get(r, "quality"), "type": get(r, "type"), "raw": name})
    index = {}
    for k, it in enumerate(items):
        for nm in [it["main"]] + it["alts"]:
            nn = _norm_words(nm)
            if nn:
                index.setdefault(nn, []).append(k)
    names = list(index)
    conn = db()
    tracks = [dict(t) for t in conn.execute("SELECT * FROM tracks WHERE missing=0")]
    conn.close()
    if only_ids:
        tracks = [t for t in tracks if t["id"] in only_ids]
    from difflib import get_close_matches
    out, unmatched = [], 0
    for t in tracks:
        base = _norm_words(clean_song_title(t["title"] or Path(t["filename"]).stem))
        if not base:
            continue
        score = 1.0
        cands = index.get(base)
        if not cands:
            close = get_close_matches(base, names, n=1, cutoff=0.86)
            if close:
                cands = index[close[0]]
                score = round(SequenceMatcher(None, base, close[0]).ratio(), 3)
        if not cands:
            unmatched += 1
            continue
        # several rows with the same name (versions): prefer the one whose era matches
        it = items[cands[0]]
        if len(cands) > 1 and t.get("era"):
            for k in cands:
                if _norm_words(items[k]["era"]) == _norm_words(t["era"]):
                    it = items[k]
                    break
        ch = {}

        def put(f, v):
            v = (v or "").strip()
            if v and (overwrite or not str(t.get(f) or "").strip()) and v != (t.get(f) or ""):
                ch[f] = v

        if "title" in fields:
            put("title", it["main"] + (" (feat. %s)" % it["features"] if it["features"] else ""))
        if "era" in fields:
            put("era", it["era"])
        if "producer" in fields:
            put("producer", it["producer"])
        if "year" in fields:
            put("year", _year_of(it["recorded"]) or _year_of(it["leaked"]))
        if "notes" in fields:
            bits = [("Recorded: " + it["recorded"]) if it["recorded"] else "", ("Leaked: " + it["leaked"]) if it["leaked"] else "",
                    ("Also known as: " + ", ".join(it["alts"][:4])) if it["alts"] else "", it["notes"]]
            note = " · ".join(b for b in bits if b)
            have = (t.get("notes") or "").strip()
            if note and note not in have:
                ch["notes"] = (have + "\n" + note).strip() if have else note
        if "quality" in fields and it["quality"]:
            tags = [x.strip() for x in (t.get("user_tags") or "").split(",") if x.strip()]
            q = it["quality"].strip()
            if q and q.lower() not in {x.lower() for x in tags} and len(q) <= 24:
                ch["user_tags"] = ", ".join(tags + [q])
        if ch:
            out.append({"id": t["id"], "filename": t["filename"], "changes": ch, "score": score,
                        "matched": it["raw"].split("\n")[0][:120]})
    out.sort(key=lambda p: (p["score"], p["filename"].lower()))
    return {"proposals": out, "rows": len(items), "unmatched": unmatched}


@app.post("/api/tracker/load")
def api_tracker_load():
    data, name = None, ""
    f = request.files.get("file")
    if f:
        data, name = f.read(), f.filename or ""
    else:
        url = ((request.get_json(silent=True) or {}).get("url") or "").strip()
        m = re.search(r"docs\.google\.com/spreadsheets/d/([A-Za-z0-9_-]+)", url)
        if m:
            name = "sheet.xlsx"
            url = "https://docs.google.com/spreadsheets/d/%s/export?format=xlsx" % m.group(1)
        elif not url.startswith("https://"):
            return jsonify({"error": "Paste a Google Sheets link, or pick a .csv or .xlsx file"}), 400
        try:
            req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
            with urllib.request.urlopen(req, timeout=40) as r:
                data = r.read(60_000_000)
                ctype = r.headers.get("Content-Type") or ""
        except urllib.error.HTTPError as exc:
            return jsonify({"error": "Couldn't open that sheet (%s). In Google Sheets, Share → General access → "
                                     "Anyone with the link." % exc.code}), 400
        except Exception as exc:
            return jsonify({"error": "Couldn't download it: %s" % exc}), 502
        if "text/html" in ctype and not name:
            return jsonify({"error": "That link is a web page, not a spreadsheet"}), 400
        name = name or url.rsplit("/", 1)[-1]
    if not data:
        return jsonify({"error": "That file is empty"}), 400
    try:
        headers, rows = load_tracker_bytes(data, name)
    except Exception as exc:
        return jsonify({"error": "Couldn't read that spreadsheet: %s" % exc}), 400
    if not headers or not rows:
        return jsonify({"error": "No rows found in that spreadsheet"}), 400
    token = uuid.uuid4().hex[:12]
    TRACKER.clear()
    TRACKER[token] = {"headers": headers, "rows": rows, "name": name}
    return jsonify({"token": token, "headers": headers, "sample": rows[:8], "count": len(rows),
                    "mapping": guess_mapping(headers)})


@app.post("/api/tracker/match")
def api_tracker_match():
    body = request.get_json(force=True) or {}
    entry = TRACKER.get(body.get("token") or "")
    if not entry:
        return jsonify({"error": "Load the tracker again"}), 400
    mapping = {k: (int(v) if v not in (None, "") else None) for k, v in (body.get("mapping") or {}).items()}
    if mapping.get("name") is None:
        return jsonify({"error": "Pick the column with the song names"}), 400
    res = match_tracker(entry, mapping, set(body.get("fields") or []), set(body.get("only") or []) or None,
                        bool(body.get("overwrite")))
    return jsonify(res)


@app.post("/api/tracker/apply")
def api_tracker_apply():
    body = request.get_json(force=True) or {}
    per = {int(k): {f: v for f, v in ch.items() if f in TAG_FIELDS} for k, ch in (body.get("changes") or {}).items()}
    per = {k: v for k, v in per.items() if v}
    conn = db()
    applied, failed, batch = apply_changes(conn, load_config(), per, "Tracker import: %d songs" % len(per))
    conn.close()
    return jsonify({"ok": True, "changed": applied, "failed": failed})


# ---- waveform ----

WAVE_BINS = 720


def compute_peaks(path, bins=WAVE_BINS):
    x = decode_mono(path, target_sr=4000, max_seconds=1800)
    if x is None or not len(x):
        return None
    x = np.abs(x)
    n = len(x) // bins
    if n < 1:
        return None
    x = x[: n * bins].reshape(bins, n)
    # mix of peak and loudness reads better than either alone
    v = 0.55 * x.max(axis=1) + 0.45 * np.sqrt((x ** 2).mean(axis=1)) * 1.6
    top = float(np.percentile(v, 99)) or 1.0
    v = np.clip(v / top, 0, 1)
    return bytes((v * 255).astype(np.uint8))


@app.get("/api/waveform/<int:tid>")
def api_waveform(tid):
    conn = db()
    row = conn.execute("SELECT path, mtime FROM tracks WHERE id=?", (tid,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "missing"}), 404
    have = conn.execute("SELECT mtime, peaks FROM waveforms WHERE track_id=?", (tid,)).fetchone()
    if have and have["peaks"] and abs((have["mtime"] or 0) - (row["mtime"] or 0)) < 1:
        conn.close()
        return jsonify({"peaks": list(have["peaks"])})
    conn.close()
    peaks = compute_peaks(row["path"])
    if not peaks:
        return jsonify({"peaks": None})
    conn = db()
    conn.execute("INSERT OR REPLACE INTO waveforms (track_id, mtime, peaks) VALUES (?,?,?)",
                 (tid, row["mtime"], peaks))
    conn.commit()
    conn.close()
    return jsonify({"peaks": list(peaks)})


# ---- history ----

@app.get("/api/player/history")
def api_player_history():
    conn = db()
    rows = conn.execute("SELECT l.track_id AS id, l.ts FROM play_log l JOIN tracks t ON t.id=l.track_id "
                        "WHERE t.missing=0 ORDER BY l.ts DESC LIMIT 100").fetchall()
    conn.close()
    return jsonify({"items": [dict(r) for r in rows]})


@app.post("/api/player/history/clear")
def api_player_history_clear():
    conn = db()
    conn.execute("DELETE FROM play_log")
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ---- missing info ----

MISSING_CHECKS = [
    ("noart", "No cover art", "has_art=0"),
    ("noartist", "No artist", "TRIM(COALESCE(artist,''))=''"),
    ("notitle", "No title tag", "TRIM(COALESCE(title,''))=''"),
    ("noalbum", "No album", "TRIM(COALESCE(album,''))=''"),
    ("noyear", "No year", "TRIM(COALESCE(year,''))=''"),
    ("noera", "No era", "TRIM(COALESCE(era,''))=''"),
    ("nolyrics", "No lyrics", "TRIM(COALESCE(lyrics,''))=''"),
    ("nosync", "Lyrics not timed", "TRIM(COALESCE(lyrics,''))<>'' AND COALESCE(has_synced,0)=0"),
    ("unrated", "Not rated", "COALESCE(rating,0)=0"),
    ("notmp3", "Not MP3", "LOWER(COALESCE(ext,''))<>'mp3'"),
]


@app.get("/api/missing")
def api_missing():
    conn = db()
    total = conn.execute("SELECT COUNT(*) FROM tracks WHERE missing=0").fetchone()[0]
    out = []
    for key, label, cond in MISSING_CHECKS:
        n = conn.execute("SELECT COUNT(*) FROM tracks WHERE missing=0 AND (%s)" % cond).fetchone()[0]
        out.append({"key": key, "label": label, "count": n})
    complete = conn.execute("SELECT COUNT(*) FROM tracks WHERE missing=0 AND NOT (%s)" % " OR ".join(
        "(%s)" % c for k, _, c in MISSING_CHECKS if k in ("noart", "noartist", "notitle", "nolyrics"))).fetchone()[0]
    conn.close()
    return jsonify({"total": total, "checks": out, "complete": complete})


# ---- eras ----

@app.get("/api/eras")
def api_eras():
    conn = db()
    rows = conn.execute("SELECT era, COUNT(*) AS n FROM tracks WHERE missing=0 AND TRIM(COALESCE(era,''))<>'' "
                        "GROUP BY era COLLATE NOCASE ORDER BY era COLLATE NOCASE").fetchall()
    conn.close()
    return jsonify({"eras": [dict(r) for r in rows]})


@app.post("/api/eras/rename")
def api_eras_rename():
    body = request.get_json(force=True) or {}
    old, new = (body.get("old") or "").strip(), (body.get("new") or "").strip()
    if not old:
        return jsonify({"error": "missing"}), 400
    conn = db()
    ids = [r["id"] for r in conn.execute("SELECT id FROM tracks WHERE era=? COLLATE NOCASE", (old,))]
    applied, failed, batch = apply_changes(conn, load_config(), {i: {"era": new} for i in ids},
                                           ("Renamed era %s" % old) if new else ("Removed era %s" % old))
    conn.close()
    return jsonify({"ok": True, "changed": applied, "failed": failed})


@app.post("/api/app/update/install")
def api_app_update_install():
    if not (FROZEN and os.name == "nt"):
        return jsonify({"error": "Updates install only in the installed Windows app."}), 400
    if UPDATE["state"] in ("downloading", "installing"):
        return jsonify({"ok": True})
    res = UPDATE.get("result") or {}
    url = res.get("url") or ""
    host = urllib.parse.urlparse(url).hostname or ""
    if not res.get("available") or not url.startswith("https://") or not (
            host == "github.com" or host.endswith(".githubusercontent.com")):
        return jsonify({"error": "No update to install — check again first."}), 400
    threading.Thread(target=_download_and_install, args=(url, res.get("size") or 0), daemon=True).start()
    return jsonify({"ok": True})


@app.get("/api/app/update/status")
def api_app_update_status():
    return jsonify({k: UPDATE[k] for k in ("state", "done", "total", "error")})


@app.post("/api/player/favorite")
def api_player_favorite():
    body = request.get_json(force=True) or {}
    ids = [int(i) for i in (body.get("ids") or [])]
    on = bool(body.get("on", True))
    conn = db()
    for tid in ids:
        if on:
            conn.execute("INSERT OR IGNORE INTO favorites (track_id, added) VALUES (?,?)",
                         (tid, time.time()))
        else:
            conn.execute("DELETE FROM favorites WHERE track_id=?", (tid,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "n": len(ids), "on": on})


def _playlist_rows(conn):
    out = []
    for p in conn.execute("SELECT * FROM playlists ORDER BY updated DESC, id DESC"):
        ids = [r["track_id"] for r in conn.execute(
            "SELECT i.track_id FROM playlist_items i JOIN tracks t ON t.id=i.track_id "
            "WHERE i.playlist_id=? AND t.missing=0 ORDER BY i.pos", (p["id"],))]
        out.append({"id": p["id"], "name": p["name"], "created": p["created"],
                    "updated": p["updated"], "track_ids": ids})
    return out


@app.get("/api/playlists")
def api_playlists():
    conn = db()
    rows = _playlist_rows(conn)
    conn.close()
    return jsonify({"playlists": rows})


def _touch(conn, pid):
    conn.execute("UPDATE playlists SET updated=? WHERE id=?", (time.time(), pid))


def _append(conn, pid, ids):
    have = {r["track_id"] for r in conn.execute(
        "SELECT track_id FROM playlist_items WHERE playlist_id=?", (pid,))}
    pos = conn.execute("SELECT COALESCE(MAX(pos), -1) m FROM playlist_items WHERE playlist_id=?",
                       (pid,)).fetchone()["m"]
    added = 0
    for tid in ids:
        if tid in have:
            continue
        if not conn.execute("SELECT 1 FROM tracks WHERE id=?", (tid,)).fetchone():
            continue
        pos += 1
        conn.execute("INSERT INTO playlist_items (playlist_id, track_id, pos) VALUES (?,?,?)",
                     (pid, tid, pos))
        have.add(tid)
        added += 1
    return added


@app.post("/api/playlists/create")
def api_playlists_create():
    body = request.get_json(force=True) or {}
    name = (body.get("name") or "").strip()[:120]
    if not name:
        return jsonify({"error": "Give the playlist a name"}), 400
    now = time.time()
    conn = db()
    cur = conn.execute("INSERT INTO playlists (name, created, updated) VALUES (?,?,?)",
                       (name, now, now))
    pid = cur.lastrowid
    added = _append(conn, pid, [int(i) for i in (body.get("ids") or [])])
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "id": pid, "name": name, "added": added})


@app.post("/api/playlists/<int:pid>/rename")
def api_playlists_rename(pid):
    name = ((request.get_json(force=True) or {}).get("name") or "").strip()[:120]
    if not name:
        return jsonify({"error": "Give the playlist a name"}), 400
    conn = db()
    conn.execute("UPDATE playlists SET name=? WHERE id=?", (name, pid))
    _touch(conn, pid)
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "name": name})


@app.post("/api/playlists/<int:pid>/delete")
def api_playlists_delete(pid):
    conn = db()
    conn.execute("DELETE FROM playlist_items WHERE playlist_id=?", (pid,))
    conn.execute("DELETE FROM playlists WHERE id=?", (pid,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.post("/api/playlists/<int:pid>/add")
def api_playlists_add(pid):
    ids = [int(i) for i in ((request.get_json(force=True) or {}).get("ids") or [])]
    conn = db()
    if not conn.execute("SELECT 1 FROM playlists WHERE id=?", (pid,)).fetchone():
        conn.close()
        return jsonify({"error": "That playlist no longer exists"}), 404
    added = _append(conn, pid, ids)
    _touch(conn, pid)
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "added": added, "skipped": len(ids) - added})


@app.post("/api/playlists/<int:pid>/remove")
def api_playlists_remove(pid):
    ids = [int(i) for i in ((request.get_json(force=True) or {}).get("ids") or [])]
    conn = db()
    for tid in ids:
        conn.execute("DELETE FROM playlist_items WHERE playlist_id=? AND track_id=?", (pid, tid))
    _touch(conn, pid)
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.post("/api/playlists/<int:pid>/order")
def api_playlists_order(pid):
    """Save a new order. Ids not mentioned keep their place after the ones given."""
    ids = [int(i) for i in ((request.get_json(force=True) or {}).get("ids") or [])]
    conn = db()
    current = [r["track_id"] for r in conn.execute(
        "SELECT track_id FROM playlist_items WHERE playlist_id=? ORDER BY pos", (pid,))]
    present = set(current)
    order = [i for i in ids if i in present]
    seen = set(order)
    order += [i for i in current if i not in seen]
    for pos, tid in enumerate(order):
        conn.execute("UPDATE playlist_items SET pos=? WHERE playlist_id=? AND track_id=?",
                     (pos, pid, tid))
    _touch(conn, pid)
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.post("/api/reveal/<int:tid>")
def api_reveal(tid):
    conn = db()
    row = conn.execute("SELECT path FROM tracks WHERE id=?", (tid,)).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "not found"}), 404
    try:
        if os.name == "nt":
            subprocess.Popen(["explorer", "/select,", os.path.normpath(row["path"])])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", row["path"]])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(row["path"])])
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    return jsonify({"ok": True})


@app.get("/api/export")
def api_export():
    import csv
    conn = db()
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM tracks WHERE missing=0 ORDER BY artist, title")]
    conn.close()
    cols = ["rating", "title", "artist", "albumartist", "album", "year", "genre",
            "producer", "bpm", "music_key", "duration", "user_tags", "notes",
            "filename", "path"]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        r["duration"] = round(r.get("duration") or 0, 1)
        w.writerow(r)
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=library.csv"})


LAST_PING = {"t": time.time()}


@app.post("/api/ping")
def api_ping():
    LAST_PING["t"] = time.time()
    return jsonify({"ok": True})


@app.post("/api/quit")
def api_quit():
    threading.Timer(0.3, lambda: os._exit(0)).start()
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# launching
# --------------------------------------------------------------------------

def watchdog(grace=90):
    while True:
        time.sleep(15)
        if SCAN_STATE["running"]:
            LAST_PING["t"] = time.time()
            continue
        if time.time() - LAST_PING["t"] > grace:
            os._exit(0)


def hide_console():
    if os.name != "nt":
        return
    try:
        import ctypes
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)
    except Exception:
        pass


def free_port(preferred=7788):
    for port in (preferred, 0):
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", port))
            p = s.getsockname()[1]
            s.close()
            return p
        except OSError:
            s.close()
    return preferred


def find_browser():
    if os.name != "nt":
        for name in ("chromium", "google-chrome", "chromium-browser", "microsoft-edge"):
            p = shutil.which(name)
            if p:
                return p
        return None
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    local = os.environ.get("LOCALAPPDATA", "")
    for c in [os.path.join(pf86, r"Microsoft\Edge\Application\msedge.exe"),
              os.path.join(pf, r"Microsoft\Edge\Application\msedge.exe"),
              os.path.join(pf, r"Google\Chrome\Application\chrome.exe"),
              os.path.join(pf86, r"Google\Chrome\Application\chrome.exe"),
              os.path.join(local, r"Google\Chrome\Application\chrome.exe")]:
        if c and os.path.exists(c):
            return c
    return None


def open_window(url):
    exe = find_browser()
    if exe:
        try:
            subprocess.Popen([exe, "--app=%s" % url,
                              "--user-data-dir=%s" % str(DATA_DIR / "window"),
                              "--no-first-run", "--no-default-browser-check",
                              "--window-size=1500,940"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except Exception:
            pass
    try:
        webbrowser.open(url)
    except Exception:
        pass


def existing_instance(port):
    try:
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:%d/api/scan/status" % port,
                                    timeout=1.5) as r:
            json.loads(r.read().decode("utf-8"))
            return True
    except Exception:
        return False


def fatal(msg):
    try:
        (DATA_DIR / "error.log").write_text(msg, encoding="utf-8")
    except Exception:
        pass
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, msg[:1500],
                                             "%s could not start" % APP_NAME, 0x10)
            return
        except Exception:
            pass
    sys.stderr.write(msg + "\n")


def selftest():
    lines = ["%s self-test" % APP_NAME, "-" * 40]
    ok = True
    lines.append("version           : %s" % app_version_info()["version"])
    lines.append("frozen build      : %s" % FROZEN)
    lines.append("python            : %s" % sys.version.split()[0])
    lines.append("data dir          : %s" % DATA_DIR)
    try:
        init_db()
        conn = db()
        n = conn.execute("SELECT COUNT(*) c FROM tracks").fetchone()["c"]
        conn.close()
        lines.append("database          : ok (%d tracks)" % n)
    except Exception as exc:
        ok = False
        lines.append("database          : FAILED %s" % exc)
    tpl = resource_dir() / "templates" / "index.html"
    if tpl.exists():
        lines.append("interface         : ok (%d KB)" % (tpl.stat().st_size // 1024))
    else:
        ok = False
        lines.append("interface         : FAILED missing %s" % tpl)
    lines.append("numpy             : %s" % (np.__version__ if np is not None else "MISSING"))
    if np is None:
        ok = False
    if _sf is not None:
        lines.append("audio decoding    : libsndfile %s, mp3=%s"
                     % (_sf.__libsndfile_version__, "MP3" in _sf.available_formats()))
    elif FFMPEG:
        lines.append("audio decoding    : ffmpeg at %s" % FFMPEG)
    else:
        lines.append("audio decoding    : none - fingerprinting disabled")
    lines.append("fingerprinting    : %s" % ("available" if CAN_FINGERPRINT else "unavailable"))
    try:
        import ctranslate2
        import faster_whisper
        ctranslate2.get_supported_compute_types("cpu")
        lines.append("lyrics auto-sync  : ok (faster-whisper %s, models: %s)" % (
            getattr(faster_whisper, "__version__", "?"),
            ", ".join(k for k in AUTOSYNC_MODELS if model_ready(k)) or "none downloaded yet"))
    except Exception as exc:
        ok = False
        lines.append("lyrics auto-sync  : FAILED %s" % exc)
    try:
        import av
        if "libmp3lame" not in av.codecs_available:
            raise OSError("no MP3 encoder")
        lines.append("mp3 converter     : ok")
    except Exception as exc:
        ok = False
        lines.append("mp3 converter     : FAILED %s" % exc)
    lines.append("cpu workers       : %d" % max(2, min(8, os.cpu_count() or 4)))
    try:
        lines.append("networking        : ok (can bind 127.0.0.1:%d)" % free_port())
    except Exception as exc:
        ok = False
        lines.append("networking        : FAILED %s" % exc)
    b = find_browser()
    lines.append("app window        : %s" % (b if b else "no Edge/Chrome found - will use your default browser"))
    lines.append("-" * 40)
    lines.append("RESULT: %s" % ("OK" if ok else "PROBLEMS FOUND"))
    report = "\n".join(lines)
    print(report)
    try:
        (DATA_DIR / "selftest.txt").write_text(report, encoding="utf-8")
    except Exception:
        pass
    return 0 if ok else 1


def main():
    global ALLOWED_ORIGINS
    init_db()
    if not CONFIG_PATH.exists():
        save_config(load_config())

    fixed = int(os.environ.get("PORT") or 0)
    if not fixed and existing_instance(7788):
        print("%s is already running - opening that window." % APP_NAME)
        open_window("http://127.0.0.1:7788/")
        return
    port = fixed or free_port()
    ALLOWED_ORIGINS = {"http://127.0.0.1:%d" % port, "http://localhost:%d" % port}
    url = "http://127.0.0.1:%d/" % port

    print("\n  %s is running at %s" % (APP_NAME, url))
    print("  Library and settings: %s" % DATA_DIR)
    print("  Close this window to quit.\n")

    if os.environ.get("NO_BROWSER") != "1":
        threading.Timer(1.0, lambda: open_window(url)).start()
    threading.Thread(target=folder_watcher, daemon=True).start()
    threading.Thread(target=background_worker, daemon=True).start()
    if FROZEN and os.environ.get("UNRELEASED_WATCHDOG") != "0":
        LAST_PING["t"] = time.time() + 30
        threading.Thread(target=watchdog, daemon=True).start()
        threading.Timer(3.0, hide_console).start()

    from werkzeug.serving import make_server
    srv = make_server("127.0.0.1", port, app, threaded=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    try:
        if "--selftest" in sys.argv:
            raise SystemExit(selftest())
        main()
    except SystemExit:
        raise
    except Exception:
        import traceback
        fatal("Something went wrong while starting up.\n\n" + traceback.format_exc()
              + "\nSettings folder: %s" % DATA_DIR)
        raise SystemExit(1)
