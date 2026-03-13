# app.py
import importlib.util
import json
import html
import os
import random
import re
import shutil
import sys
import time
from datetime import datetime, time as dt_time
import threading
import traceback
from dataclasses import dataclass, asdict
from typing import List, Optional
import requests
import socket
from flask import Flask, Response, jsonify, request, render_template, send_file
from config import DEEZER_API_BASE
import zipfile
from io import BytesIO

import pygame  # make sure it's installed; on Raspberry Pi it's usually available
import subprocess
from mutagen import File as MutagenFile
from mutagen.id3 import ID3, TIT2, TPE1, TALB, TDRC, TPE2, APIC, TXXX
from mutagen.mp3 import MP3

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
PLAYLISTS_FILE = os.path.join(DATA_DIR, "playlists.json")
FAVORITES_FILE = os.path.join(DATA_DIR, "favorites.json")
SEARCH_SETTINGS_FILE = os.path.join(DATA_DIR, "search_settings.json")
APOO_SERVICE_NAME = os.getenv("APOO_SERVICE_NAME", "smart_home_apoo.service")
MAX_SETTINGS_JOURNAL_LINES = 150
SPOTIFY_IMPORT_STATUS_TTL_SECONDS = 1800
LASTFM_API_BASE = os.getenv("LASTFM_API_BASE", "https://ws.audioscrobbler.com/2.0/")
LASTFM_API_KEY = os.getenv("LASTFM_API_KEY", "").strip()
try:
    DEEZER_QUOTA_BACKOFF_SECONDS = int(os.getenv("DEEZER_QUOTA_BACKOFF_SECONDS", "1800"))
except ValueError:
    DEEZER_QUOTA_BACKOFF_SECONDS = 1800
try:
    LASTFM_NEGATIVE_CACHE_TTL_SECONDS = int(os.getenv("LASTFM_NEGATIVE_CACHE_TTL_SECONDS", "900"))
except ValueError:
    LASTFM_NEGATIVE_CACHE_TTL_SECONDS = 900

DEFAULT_SEARCH_SETTINGS = {
    "whitelist_words": ["lyrics", "official audio"],
    "blacklist_words": ["live", "remix", "extended", "acoustic", "karaoke", "cover", "instrumental"],
}

# Semaphore to limit ffmpeg/yt-dlp downloads to 1 at a time (important for Raspberry Pi)
DOWNLOAD_SEMAPHORE = threading.Semaphore(1)
TRACK_METADATA_CACHE = {}
TRACK_METADATA_CACHE_LOCK = threading.Lock()
ARTWORK_EMBED_ATTEMPTED_PATHS = set()
ARTWORK_EMBED_ATTEMPTED_LOCK = threading.Lock()
MP3_METADATA_EMBED_ATTEMPTED_PATHS = set()
MP3_METADATA_EMBED_ATTEMPTED_LOCK = threading.Lock()
SPOTIFY_IMPORT_STATUS = {}
SPOTIFY_IMPORT_STATUS_LOCK = threading.Lock()
DEEZER_QUOTA_COOLDOWN_UNTIL = 0.0
DEEZER_QUOTA_COOLDOWN_LOCK = threading.Lock()
LASTFM_NEGATIVE_CACHE = {}
LASTFM_NEGATIVE_CACHE_LOCK = threading.Lock()


def is_downloading_marker(value: Optional[str]) -> bool:
    return bool(re.search(r"\(downloading\)\s*$", str(value or ""), flags=re.IGNORECASE))


def clean_metadata_lookup_text(value: Optional[str]) -> str:
    cleaned = str(value or "").strip()
    cleaned = re.sub(r"\s*\(downloading\)\s*$", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def prepare_track_metadata_lookup(title: Optional[str], artist: Optional[str]) -> tuple[str, str]:
    """Normalize noisy queue titles/artists so metadata APIs get cleaner lookup terms."""
    clean_title = clean_metadata_lookup_text(title)
    clean_artist = clean_metadata_lookup_text(artist)

    clean_title = re.sub(r"\s*\((?:feat|ft|with)\.?[^)]*\)", "", clean_title, flags=re.IGNORECASE)
    clean_title = re.sub(
        r"\s*-\s*(?:radio\s+edit|radio\s+version|single\s+version|album\s+version|extended\s+mix|remaster(?:ed)?(?:\s+\d{4})?|explicit|clean)\s*$",
        "",
        clean_title,
        flags=re.IGNORECASE,
    )
    clean_title = re.sub(r"\s+", " ", clean_title).strip(" -")

    if clean_artist:
        parts = re.split(
            r"\s*(?:,|&|\bx\b|\bfeat\.?\b|\bft\.?\b|\bwith\b)\s*",
            clean_artist,
            maxsplit=1,
            flags=re.IGNORECASE,
        )
        if parts and parts[0].strip():
            clean_artist = parts[0].strip()

    clean_artist = re.sub(r"\s+", " ", clean_artist).strip()
    return clean_title, clean_artist


def is_downloading_placeholder_track(track: dict) -> bool:
    if not isinstance(track, dict):
        return False
    if track.get("_downloading") or track.get("_lofi_downloading"):
        return True
    return is_downloading_marker(track.get("title"))


def _lastfm_negative_cache_key(title: str, artist: str) -> str:
    key_title = re.sub(r"[^a-z0-9]+", "", (title or "").lower())
    key_artist = re.sub(r"[^a-z0-9]+", "", (artist or "").lower())
    if not key_title or not key_artist:
        return ""
    return f"{key_artist}::{key_title}"


def _is_lastfm_negative_cached(title: str, artist: str) -> bool:
    key = _lastfm_negative_cache_key(title, artist)
    if not key:
        return False

    now = time.time()
    with LASTFM_NEGATIVE_CACHE_LOCK:
        expires_at = LASTFM_NEGATIVE_CACHE.get(key)
        if not expires_at:
            return False
        if expires_at > now:
            return True
        LASTFM_NEGATIVE_CACHE.pop(key, None)
    return False


def _mark_lastfm_negative_cache(title: str, artist: str):
    key = _lastfm_negative_cache_key(title, artist)
    if not key:
        return
    with LASTFM_NEGATIVE_CACHE_LOCK:
        LASTFM_NEGATIVE_CACHE[key] = time.time() + LASTFM_NEGATIVE_CACHE_TTL_SECONDS


def cleanup_spotify_import_statuses():
    cutoff = time.time() - SPOTIFY_IMPORT_STATUS_TTL_SECONDS
    with SPOTIFY_IMPORT_STATUS_LOCK:
        stale = [
            progress_id
            for progress_id, payload in SPOTIFY_IMPORT_STATUS.items()
            if payload.get("updated_ts", 0) < cutoff
        ]
        for progress_id in stale:
            SPOTIFY_IMPORT_STATUS.pop(progress_id, None)


def update_spotify_import_status(progress_id: Optional[str], **updates):
    if not progress_id:
        return

    now = time.time()
    with SPOTIFY_IMPORT_STATUS_LOCK:
        stale = [
            stale_id
            for stale_id, payload in SPOTIFY_IMPORT_STATUS.items()
            if payload.get("updated_ts", 0) < now - SPOTIFY_IMPORT_STATUS_TTL_SECONDS
        ]
        for stale_id in stale:
            SPOTIFY_IMPORT_STATUS.pop(stale_id, None)

        payload = dict(SPOTIFY_IMPORT_STATUS.get(progress_id) or {})
        payload.update(updates)
        payload["progress_id"] = progress_id
        payload["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        payload["updated_ts"] = now
        SPOTIFY_IMPORT_STATUS[progress_id] = payload


def get_spotify_import_status(progress_id: str) -> Optional[dict]:
    cleanup_spotify_import_statuses()
    with SPOTIFY_IMPORT_STATUS_LOCK:
        payload = SPOTIFY_IMPORT_STATUS.get(progress_id)
        return dict(payload) if payload else None


def resolve_yt_dlp_command() -> list[str]:
    env_override = os.getenv("YT_DLP_BIN", "").strip()
    candidates = [
        env_override,
        os.path.join(os.path.dirname(__file__), ".venv", "bin", "yt-dlp"),
        shutil.which("yt-dlp") or "",
    ]

    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            return [candidate]

    if importlib.util.find_spec("yt_dlp"):
        return [sys.executable, "-m", "yt_dlp"]

    raise FileNotFoundError(
        "yt-dlp is not installed. Install it in the Apoo virtualenv or set YT_DLP_BIN."
    )

def ensure_dir_exists(path):
    """Ensure the directory for a file exists."""
    directory = os.path.dirname(path)
    if directory and not os.path.exists(directory):
        os.makedirs(directory, exist_ok=True)

def load_json(path, default):
    """Load JSON safely, creating file/dir if missing, and recover from corruption."""
    ensure_dir_exists(path)

    if not os.path.exists(path):
        # Create file with default content
        save_json(path, default)
        return default

    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        # If file is corrupted, keep a timestamped backup for recovery,
        # then reset to default so the app can continue.
        try:
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            corrupt_backup = f"{path}.corrupt-{ts}.bak"
            shutil.copy2(path, corrupt_backup)
            print(f"Backed up corrupted JSON: {corrupt_backup}")
        except Exception as backup_err:
            print(f"Failed to back up corrupted JSON {path}: {backup_err}")

        save_json(path, default)
        return default

def save_json(path, data):
    """Save JSON safely and atomically, ensuring directory exists."""
    ensure_dir_exists(path)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)


def normalize_word_list(words) -> list[str]:
    """Normalize settings word lists for storage and query building."""
    if not isinstance(words, list):
        return []

    normalized = []
    seen = set()
    for word in words:
        if not isinstance(word, str):
            continue
        cleaned = re.sub(r"\s+", " ", word.strip().lower())
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        normalized.append(cleaned)
    return normalized


def normalize_search_settings(data) -> dict:
    """Merge incoming search settings with defaults."""
    data = data or {}
    return {
        "whitelist_words": normalize_word_list(data.get("whitelist_words", DEFAULT_SEARCH_SETTINGS["whitelist_words"])),
        "blacklist_words": normalize_word_list(data.get("blacklist_words", DEFAULT_SEARCH_SETTINGS["blacklist_words"])),
    }


def load_search_settings() -> dict:
    """Load persisted search settings with sane defaults."""
    settings = load_json(SEARCH_SETTINGS_FILE, DEFAULT_SEARCH_SETTINGS)
    normalized = normalize_search_settings(settings)
    if normalized != settings:
        save_json(SEARCH_SETTINGS_FILE, normalized)
    return normalized


def format_search_term(term: str, exclude: bool = False) -> str:
    """Format a positive or negative search term for yt-dlp queries."""
    cleaned = re.sub(r"\s+", " ", (term or "").strip())
    if not cleaned:
        return ""
    if " " in cleaned:
        cleaned = f'"{cleaned}"'
    return f"-{cleaned}" if exclude else cleaned


def build_download_queries(title: str, artist: str) -> list[str]:
    """Build yt-dlp queries from persisted whitelist/blacklist settings."""
    settings = load_search_settings()
    base_query = f"{title} {artist}".strip()
    whitelist = " ".join(filter(None, [format_search_term(term) for term in settings.get("whitelist_words", [])]))
    blacklist = " ".join(filter(None, [format_search_term(term, exclude=True) for term in settings.get("blacklist_words", [])]))

    queries = []
    with_whitelist = " ".join(part for part in [base_query, whitelist, blacklist] if part).strip()
    if with_whitelist:
        queries.append(with_whitelist)

    fallback = " ".join(part for part in [base_query, blacklist] if part).strip()
    if fallback and fallback not in queries:
        queries.append(fallback)

    return queries or [base_query]


def get_track_cache_key(track: dict) -> str:
    """Return a stable cache key for track metadata."""
    path = track.get("path")
    if path:
        return f"path::{os.path.realpath(path)}"

    deezer_id = track.get("deezer_id")
    if deezer_id:
        return f"deezer::{deezer_id}"

    spotify_id = track.get("spotify_id")
    if spotify_id:
        return f"spotify::{spotify_id}"

    track_id = track.get("id")
    if track_id:
        return f"id::{track_id}"

    title = (track.get("title") or "").strip().lower()
    artist = (track.get("artist") or "").strip().lower()
    return f"name::{title}::{artist}"


def get_cached_track_metadata(cache_key: str) -> dict:
    with TRACK_METADATA_CACHE_LOCK:
        return dict(TRACK_METADATA_CACHE.get(cache_key) or {})


def set_cached_track_metadata(cache_key: str, metadata: dict):
    with TRACK_METADATA_CACHE_LOCK:
        TRACK_METADATA_CACHE[cache_key] = dict(metadata)


def sanitize_filename(name: str) -> str:
    """Remove unsafe characters for filesystem."""
    # Remove filesystem and URL-problematic characters
    name = re.sub(r'[\\/:*?"<>|#%]', '', name)
    name = name.strip()
    name = re.sub(r'\s+', '_', name)
    # Limit filename length to 100 characters for cross-platform safety
    max_len = 100
    if len(name) > max_len:
        name = name[:max_len]
    return name

def run_yt_dlp(command_list):
    cmd = [*resolve_yt_dlp_command(), *command_list]
    print("Running:", " ".join(cmd))
    subprocess.run(cmd, shell=False)

def extract_audio_from_video(video_path: str, audio_path: str) -> bool:
    """Extract audio from video file (MP4, etc) to MP3 using ffmpeg."""
    try:
        if not os.path.exists(video_path):
            return False
        
        print(f"Extracting audio from {os.path.basename(video_path)}...")
        
        # Acquire semaphore to ensure sequential ffmpeg execution
        with DOWNLOAD_SEMAPHORE:
            # Use ffmpeg to extract audio
            cmd = [
                "ffmpeg",
                "-i", video_path,
                "-q:a", "0",  # Highest quality
                "-map", "a",  # Extract audio only
                "-y",  # Overwrite output file
                audio_path
            ]
            
            result = subprocess.run(cmd, shell=False, capture_output=True, text=True)
            
            if result.returncode == 0 and os.path.exists(audio_path):
                print(f"✓ Audio extracted to {os.path.basename(audio_path)}")
                return True
            else:
                print(f"✗ Audio extraction failed: {result.stderr[:200]}")
                return False
    except Exception as e:
        print(f"Error extracting audio: {e}")
        return False

def get_video_resolution(video_path: str) -> str:
    """Get video resolution using ffprobe."""
    try:
        cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "csv=p=0", video_path]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            resolution = result.stdout.strip()
            return resolution
        return "unknown"
    except Exception as e:
        return f"error: {e}"

def parse_deezer_playlist_id(url_or_id: str) -> Optional[str]:
    """Extract Deezer playlist id from URL or return a raw numeric id."""
    if not url_or_id:
        return None
    candidate = str(url_or_id).strip()
    if candidate.isdigit():
        return candidate

    patterns = [
        r"/playlist/(\d+)",
        r"[?&]list=(\d+)",
    ]
    for pattern in patterns:
        m = re.search(pattern, candidate)
        if m:
            return m.group(1)
    return None


def parse_spotify_playlist_id(url_or_id: str) -> Optional[str]:
    """Extract Spotify playlist id from URL/URI or return a raw id."""
    if not url_or_id:
        return None

    candidate = str(url_or_id).strip()
    if re.fullmatch(r"[A-Za-z0-9]{22}", candidate):
        return candidate

    uri_match = re.search(r"spotify:playlist:([A-Za-z0-9]{22})", candidate)
    if uri_match:
        return uri_match.group(1)

    url_match = re.search(r"/playlist/([A-Za-z0-9]{22})", candidate)
    if url_match:
        return url_match.group(1)

    return None


def _spotify_client_credentials_token() -> Optional[str]:
    """Get Spotify app token when SPOTIFY_CLIENT_ID/SECRET are configured."""
    client_id = os.environ.get("SPOTIFY_CLIENT_ID")
    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET")
    if not client_id or not client_secret:
        return None

    try:
        res = requests.post(
            "https://accounts.spotify.com/api/token",
            data={"grant_type": "client_credentials"},
            auth=(client_id, client_secret),
            timeout=20,
        )
        res.raise_for_status()
        data = res.json()
        token = data.get("access_token")
        return token if token else None
    except Exception as e:
        print(f"Spotify token error: {e}")
        return None


def _spotify_anonymous_token() -> Optional[str]:
    """Get a short-lived Spotify web-player token for public resources.

    This uses the same endpoint the Spotify web player calls, so no developer
    account or registered app is required.  The token works for reading any
    public playlist via the standard /v1 API endpoints.
    """
    try:
        res = requests.get(
            "https://open.spotify.com/get_access_token",
            params={"reason": "transport", "productType": "web_player"},
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
                ),
                "Accept": "application/json",
            },
            timeout=15,
        )
        if res.status_code == 200:
            token = res.json().get("accessToken")
            return token if token else None
    except Exception as e:
        print(f"[Spotify] Anonymous token fetch failed: {e}")
    return None


def _spotify_import_via_api(playlist_id: str, token: str, progress_id: Optional[str] = None, stage_label: str = "spotify-api"):
    """Fetch all playlist tracks from Spotify Web API using app token."""
    headers = {"Authorization": f"Bearer {token}"}

    update_spotify_import_status(
        progress_id,
        state="running",
        stage=stage_label,
        message="Fetching playlist metadata from Spotify API...",
    )

    meta_res = requests.get(
        f"https://api.spotify.com/v1/playlists/{playlist_id}",
        headers=headers,
        params={"fields": "name,tracks(total)"},
        timeout=20,
    )
    meta_res.raise_for_status()
    meta = meta_res.json()

    playlist_name = meta.get("name") or "Imported Spotify Playlist"
    total = int(((meta.get("tracks") or {}).get("total") or 0))

    tracks = []
    seen = set()
    offset = 0
    limit = 100

    while True:
        update_spotify_import_status(
            progress_id,
            state="running",
            stage=stage_label,
            message=f"Fetching Spotify API tracks ({len(tracks)}/{total or '?'})...",
            collected_count=len(tracks),
            expected_total=total or None,
            progress=int((len(tracks) / total) * 100) if total else None,
        )
        res = requests.get(
            f"https://api.spotify.com/v1/playlists/{playlist_id}/tracks",
            headers=headers,
            params={
                "offset": offset,
                "limit": limit,
                "fields": "items(track(id,name,artists(name))),next,total",
            },
            timeout=25,
        )
        res.raise_for_status()
        payload = res.json()

        items = payload.get("items") or []
        for item in items:
            track = (item or {}).get("track") or {}
            tid = track.get("id")
            title = (track.get("name") or "Unknown title").strip()
            artists = ", ".join([a.get("name", "") for a in (track.get("artists") or []) if a.get("name")]).strip()
            artist = artists or "Unknown artist"

            if not tid or tid in seen:
                continue
            seen.add(tid)

            tracks.append({
                "id": tid,
                "title": title,
                "artist": artist,
                "spotify_id": tid,
                "path": None,
            })

        if not payload.get("next"):
            break
        offset += limit

    update_spotify_import_status(
        progress_id,
        state="running",
        stage=stage_label,
        message=f"Fetched {len(tracks)} tracks from Spotify API.",
        collected_count=len(tracks),
        expected_total=total or len(tracks),
        progress=100 if tracks else None,
    )

    return playlist_name, tracks, total


def _import_spotify_via_selenium(playlist_id: str, progress_id: Optional[str] = None):
    """
    Use headless Chromium + Selenium to scrape a Spotify playlist,
    scrolling through the virtualised track list to collect all tracks.
    Returns (playlist_name, tracks_list, total_count).
    """
    import time as _time

    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service
        from selenium.webdriver.common.by import By
    except ImportError:
        raise RuntimeError("selenium not installed")

    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-setuid-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1280,900")

    # Resolve chromedriver — prefer explicit env override, then snap, then apt paths.
    # Do NOT set binary_location when using snap chromedriver: the snap wrapper
    # knows where its paired Chromium binary lives and setting a path breaks it.
    _chromedriver_candidates = [
        os.environ.get("CHROMEDRIVER_BINARY", ""),
        "/snap/bin/chromium.chromedriver",
        "/usr/bin/chromedriver",
    ]
    _chromedriver_bin = next((p for p in _chromedriver_candidates if p and os.path.exists(p)), None)
    if not _chromedriver_bin:
        raise RuntimeError("chromedriver not found; install via 'sudo snap install chromium'")

    # Only set an explicit binary_location for non-snap drivers (snap handles it internally)
    if "/snap/" not in _chromedriver_bin:
        _chromium_candidates = [
            os.environ.get("CHROMIUM_BINARY", ""),
            "/usr/bin/chromium-browser",
            "/usr/bin/chromium",
        ]
        _chromium_bin = next((p for p in _chromium_candidates if p and os.path.exists(p)), None)
        if _chromium_bin:
            opts.binary_location = _chromium_bin

    update_spotify_import_status(
        progress_id,
        state="running",
        stage="selenium-launch",
        message="Launching headless Chromium for Spotify import...",
    )

    svc = Service(_chromedriver_bin)
    driver = webdriver.Chrome(service=svc, options=opts)
    try:
        url = f"https://open.spotify.com/playlist/{playlist_id}"
        driver.get(url)
        _time.sleep(6)  # wait for React SPA to mount and render initial tracks

        # Extract playlist name from page title ("Name - playlist by X | Spotify")
        title_raw = driver.title or ""
        playlist_name = title_raw.split(" - ")[0].strip() if " - " in title_raw else "Imported Playlist"
        expected_total = driver.execute_script("""
            const bodyText = document.body?.innerText || "";
            const match = bodyText.match(/(\\d+)\\s+songs?/i);
            return match ? parseInt(match[1], 10) : null;
        """)

        update_spotify_import_status(
            progress_id,
            state="running",
            stage="selenium-scan",
            message=f"Scanning the Spotify page ({0}/{expected_total or '?'})...",
            collected_count=0,
            expected_total=expected_total,
            progress=0,
            playlist_name=playlist_name,
        )

        tracks = {}  # keyed by (title, artist) for deduplication

        def collect_visible():
            """Read currently rendered track rows from the virtual list."""
            added = False
            buttons = driver.find_elements(By.CSS_SELECTOR, "button[aria-label^='Play ']")
            for btn in buttons:
                if expected_total and len(tracks) >= expected_total:
                    break
                label = btn.get_attribute("aria-label") or ""
                if not label.startswith("Play "):
                    continue
                # "Play TITLE by ARTIST" — use rsplit to handle titles containing " by "
                remainder = label[5:]  # strip leading "Play "
                parts = remainder.rsplit(" by ", 1)
                if len(parts) != 2:
                    continue
                title, artist = parts[0].strip(), parts[1].strip()
                if not title or not artist:
                    continue
                key = (title, artist)
                if key in tracks:
                    continue

                # Try to extract Spotify track ID from a nearby link
                spotify_id = None
                try:
                    row = btn.find_element(By.XPATH,
                        "./ancestor::div[@data-testid='tracklist-row']")
                    link = row.find_element(By.CSS_SELECTOR, "a[href*='/track/']")
                    href = link.get_attribute("href") or ""
                    m = re.search(r"/track/([A-Za-z0-9]{22})", href)
                    if m:
                        spotify_id = m.group(1)
                except Exception:
                    pass

                uid = spotify_id or f"sp-{abs(hash(key))}"
                tracks[key] = {
                    "id": uid,
                    "title": title,
                    "artist": artist,
                    "spotify_id": spotify_id,
                    "path": None,
                }
                added = True

            if added:
                update_spotify_import_status(
                    progress_id,
                    state="running",
                    stage="selenium-scan",
                    message=f"Collected {len(tracks)} of {expected_total or '?'} Spotify tracks...",
                    collected_count=len(tracks),
                    expected_total=expected_total,
                    progress=int((len(tracks) / expected_total) * 100) if expected_total else None,
                    playlist_name=playlist_name,
                )

        # Find the single scrollable container that holds the track list
        scroll_el = driver.execute_script("""
            var divs = document.querySelectorAll('div');
            for (var div of divs) {
                var style = window.getComputedStyle(div);
                if (div.scrollHeight > 3000 &&
                    (style.overflowY === 'scroll' || style.overflowY === 'auto')) {
                    return div;
                }
            }
            return null;
        """)

        collect_visible()

        scroll_pos = 0
        step = 400
        last_count = 0
        no_new = 0

        for _ in range(300):  # safety cap — handles playlists with 1000+ tracks
            if expected_total and len(tracks) >= expected_total:
                break
            scroll_pos += step
            if scroll_el:
                driver.execute_script(
                    f"arguments[0].scrollTop = {scroll_pos}", scroll_el)
            _time.sleep(0.6)
            collect_visible()

            if len(tracks) > last_count:
                last_count = len(tracks)
                no_new = 0
            else:
                no_new += 1
                if no_new >= 6:
                    break  # reached end of list

        final_tracks = list(tracks.values())[:expected_total or None]
        update_spotify_import_status(
            progress_id,
            state="running",
            stage="selenium-complete",
            message=f"Collected {len(final_tracks)} Spotify tracks from the page.",
            collected_count=len(final_tracks),
            expected_total=expected_total or len(final_tracks),
            progress=100 if final_tracks else None,
            playlist_name=playlist_name,
        )
        return playlist_name, final_tracks, expected_total or len(final_tracks)
    finally:
        driver.quit()


def _import_spotify_playlist_impl(url_or_id: str, progress_id: Optional[str] = None):
    """
    Import a Spotify playlist. Tries (in order):
      1. Spotify Web API (if SPOTIFY_CLIENT_ID/SECRET configured)
      2. Anonymous Spotify web-player token (public playlists, no credentials needed)
      3. Headless Chromium browser to scroll through the full virtual track list
      4. Static HTML scrape (fallback, returns at most ~30 tracks)
    """
    playlist_id = parse_spotify_playlist_id(url_or_id)
    if not playlist_id:
        update_spotify_import_status(progress_id, state="error", stage="validation", message="Invalid Spotify playlist URL or ID.")
        return jsonify({"error": "Invalid Spotify playlist URL or ID"}), 400

    try:
        # 1. Official API path (requires env vars)
        update_spotify_import_status(progress_id, state="running", stage="credentials-api", message="Trying Spotify API credentials...")
        token = _spotify_client_credentials_token()
        if token:
            try:
                playlist_name, tracks, total = _spotify_import_via_api(playlist_id, token, progress_id=progress_id, stage_label="credentials-api")
                if tracks:
                    update_spotify_import_status(progress_id, state="complete", stage="done", message=f"Imported {len(tracks)} tracks via Spotify API.", collected_count=len(tracks), expected_total=total or len(tracks), progress=100, playlist_name=playlist_name)
                    return jsonify({"name": playlist_name, "tracks": tracks, "total": total})
            except Exception as e:
                print(f"Spotify API import failed, falling back: {e}")
                update_spotify_import_status(progress_id, state="running", stage="credentials-api", message="Spotify API credentials failed, trying the next import method...")

        # 2. Anonymous web-player token (no credentials needed, works for public playlists)
        update_spotify_import_status(progress_id, state="running", stage="anonymous-api", message="Trying anonymous Spotify access token...")
        anon_token = _spotify_anonymous_token()
        if anon_token:
            try:
                playlist_name, tracks, total = _spotify_import_via_api(playlist_id, anon_token, progress_id=progress_id, stage_label="anonymous-api")
                if tracks:
                    update_spotify_import_status(progress_id, state="complete", stage="done", message=f"Imported {len(tracks)} tracks with Spotify's public token.", collected_count=len(tracks), expected_total=total or len(tracks), progress=100, playlist_name=playlist_name)
                    return jsonify({"name": playlist_name, "tracks": tracks, "total": total})
            except Exception as e:
                print(f"Spotify anonymous API import failed, falling back: {e}")
                update_spotify_import_status(progress_id, state="running", stage="anonymous-api", message="Anonymous token failed, falling back to browser import...")

        # 3. Selenium headless-browser path (gets full playlist, no credentials needed)
        try:
            update_spotify_import_status(progress_id, state="running", stage="selenium-launch", message="Starting browser-based Spotify import...")
            playlist_name, tracks, total = _import_spotify_via_selenium(playlist_id, progress_id=progress_id)
            if tracks:
                update_spotify_import_status(progress_id, state="complete", stage="done", message=f"Imported {len(tracks)} tracks from the Spotify page.", collected_count=len(tracks), expected_total=total or len(tracks), progress=100, playlist_name=playlist_name)
                return jsonify({"name": playlist_name, "tracks": tracks, "total": total})
        except Exception as e:
            print(f"Selenium Spotify import failed, falling back to HTML scrape: {e}")
            update_spotify_import_status(progress_id, state="running", stage="html-fallback", message="Browser import failed, falling back to limited HTML scraping...")

        # 4. Static HTML scrape fallback (limited to ~30 tracks)
        playlist_url = f"https://open.spotify.com/playlist/{playlist_id}"
        header_profiles = [
            {},
            {"User-Agent": "Mozilla/5.0"},
            {
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                "Accept-Language": "en-US,en;q=0.9",
            },
        ]
        page = ""
        best_count = -1
        for headers in header_profiles:
            try:
                res = requests.get(playlist_url, headers=headers, timeout=20)
                res.raise_for_status()
                candidate = res.text
                count = len(set(re.findall(r"/track/([A-Za-z0-9]{22})", candidate)))
                if count > best_count:
                    page = candidate
                    best_count = count
            except Exception:
                continue

        if not page:
            update_spotify_import_status(progress_id, state="error", stage="html-fallback", message="Failed to fetch the Spotify playlist page.")
            return jsonify({"error": "Failed to fetch Spotify playlist page"}), 502

        playlist_name = "Imported Spotify Playlist"
        tracks = []
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(page, "html.parser")
            h1 = soup.find("h1")
            if h1:
                playlist_name = h1.get_text(" ", strip=True) or playlist_name
            seen = set()
            for link in soup.select('a[href^="/track/"]'):
                href = link.get("href") or ""
                m = re.search(r"/track/([A-Za-z0-9]{22})", href)
                if not m:
                    continue
                track_id = m.group(1)
                if track_id in seen:
                    continue
                seen.add(track_id)
                title = (link.get_text(" ", strip=True) or "Unknown title").strip()
                artist = "Unknown artist"
                tracks.append({"id": track_id, "title": title, "artist": artist,
                                "spotify_id": track_id, "path": None})
        except Exception:
            pass

        if not tracks:
            update_spotify_import_status(progress_id, state="error", stage="html-fallback", message="Spotify page loaded, but no tracks were found.")
            return jsonify({"error": "No tracks found on Spotify playlist page"}), 400

        warning = (
            "Only the first ~30 tracks could be fetched (static HTML limit). "
            "The anonymous Spotify token also failed. "
            "For the full playlist, configure SPOTIFY_CLIENT_ID/SECRET or install chromium-browser + chromedriver."
        )
        update_spotify_import_status(progress_id, state="complete", stage="done", message=f"Imported {len(tracks)} tracks via limited HTML fallback.", collected_count=len(tracks), expected_total=len(tracks), progress=100, playlist_name=playlist_name, warning=warning)
        return jsonify({"name": playlist_name, "tracks": tracks, "warning": warning})

    except Exception as e:
        print("Spotify import error:", e)
        update_spotify_import_status(progress_id, state="error", stage="error", message=f"Spotify import failed: {e}")
        return jsonify({"error": "Failed to import Spotify playlist"}), 500


def deezer_get(path_or_url: str, params: Optional[dict] = None):
    """Call Deezer API and raise on structured API errors."""
    if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
        url = path_or_url
    else:
        url = f"{DEEZER_API_BASE.rstrip('/')}/{path_or_url.lstrip('/')}"

    res = requests.get(url, params=params, timeout=20)
    data = res.json()
    if isinstance(data, dict) and data.get("error"):
        message = data.get("error", {}).get("message", "Unknown Deezer API error")
        if _is_deezer_quota_error_message(message):
            mark_deezer_quota_backoff(message)
        raise ValueError(message)
    return data


def _is_deezer_quota_error_message(message: str) -> bool:
    text = str(message or "").lower()
    return "quota" in text and "exceeded" in text


def is_deezer_quota_backoff_active() -> bool:
    with DEEZER_QUOTA_COOLDOWN_LOCK:
        return time.time() < DEEZER_QUOTA_COOLDOWN_UNTIL


def mark_deezer_quota_backoff(reason: str = ""):
    global DEEZER_QUOTA_COOLDOWN_UNTIL
    with DEEZER_QUOTA_COOLDOWN_LOCK:
        DEEZER_QUOTA_COOLDOWN_UNTIL = max(
            DEEZER_QUOTA_COOLDOWN_UNTIL,
            time.time() + DEEZER_QUOTA_BACKOFF_SECONDS,
        )
        remaining = int(max(0, DEEZER_QUOTA_COOLDOWN_UNTIL - time.time()))
    if reason:
        print(f"Deezer quota backoff active for {remaining}s: {reason}")


def _extract_year_from_text(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    match = re.search(r"(19|20)\d{2}", str(text))
    return match.group(0) if match else None


def _best_lastfm_image(images) -> Optional[str]:
    if not isinstance(images, list):
        return None

    by_size = {}
    for image in images:
        if not isinstance(image, dict):
            continue
        url = (image.get("#text") or "").strip()
        if not url:
            continue
        by_size[image.get("size") or ""] = url

    for size in ("extralarge", "large", "medium", "small"):
        if by_size.get(size):
            return by_size[size]
    return next(iter(by_size.values()), None)


def _lastfm_track_getinfo(track_title: str, track_artist: str) -> Optional[dict]:
    if not LASTFM_API_KEY:
        return None

    params = {
        "method": "track.getInfo",
        "api_key": LASTFM_API_KEY,
        "artist": track_artist,
        "track": track_title,
        "autocorrect": 1,
        "format": "json",
    }
    response = requests.get(LASTFM_API_BASE, params=params, timeout=15)
    response.raise_for_status()
    payload = response.json()

    if payload.get("error"):
        raise ValueError(payload.get("message") or "Last.fm API error")

    track = payload.get("track")
    if not isinstance(track, dict):
        return None

    album_info = track.get("album") if isinstance(track.get("album"), dict) else {}
    duration_raw = track.get("duration")
    duration = None
    if duration_raw:
        try:
            duration = float(duration_raw) / 1000.0
        except (TypeError, ValueError):
            duration = None

    year = _extract_year_from_text((track.get("wiki") or {}).get("published"))
    if not year:
        year = _extract_year_from_text(album_info.get("title"))

    return {
        "duration": duration,
        "album": album_info.get("title"),
        "year": year,
        "artwork_url": _best_lastfm_image(album_info.get("image")),
        "deezer_id": None,
    }


def search_lastfm_track_metadata(title: str, artist: str) -> Optional[dict]:
    """Best-effort Last.fm metadata lookup by title + artist."""
    if not LASTFM_API_KEY:
        return None

    lookup_title, lookup_artist = prepare_track_metadata_lookup(title, artist)
    if not lookup_title or not lookup_artist:
        return None

    if _is_lastfm_negative_cached(lookup_title, lookup_artist):
        return None

    try:
        direct = _lastfm_track_getinfo(lookup_title, lookup_artist)
        if direct:
            return direct

        search_params = {
            "method": "track.search",
            "api_key": LASTFM_API_KEY,
            "track": f"{lookup_title} {lookup_artist}",
            "limit": 1,
            "format": "json",
        }
        response = requests.get(LASTFM_API_BASE, params=search_params, timeout=15)
        response.raise_for_status()
        payload = response.json()

        if payload.get("error"):
            raise ValueError(payload.get("message") or "Last.fm search error")

        matches = (((payload.get("results") or {}).get("trackmatches") or {}).get("track") or [])
        if isinstance(matches, dict):
            matches = [matches]
        if not matches:
            _mark_lastfm_negative_cache(lookup_title, lookup_artist)
            return None

        first = matches[0]
        candidate_title = (first.get("name") or lookup_title).strip()
        candidate_artist = (first.get("artist") or lookup_artist).strip()
        candidate_title, candidate_artist = prepare_track_metadata_lookup(candidate_title, candidate_artist)
        resolved = _lastfm_track_getinfo(candidate_title or lookup_title, candidate_artist or lookup_artist)
        if not resolved:
            _mark_lastfm_negative_cache(lookup_title, lookup_artist)
        return resolved
    except Exception as e:
        message = str(e or "")
        if "track not found" in message.lower():
            _mark_lastfm_negative_cache(lookup_title, lookup_artist)
            return None
        print(f"Last.fm metadata search failed for {lookup_artist} - {lookup_title}: {e}")
        return None


def search_lastfm_tracks(query: str, limit: int = 10) -> list[dict]:
    """Search tracks on Last.fm for generic UI search fallback."""
    if not LASTFM_API_KEY:
        return []

    try:
        params = {
            "method": "track.search",
            "api_key": LASTFM_API_KEY,
            "track": query,
            "limit": max(1, min(int(limit or 10), 50)),
            "format": "json",
        }
        response = requests.get(LASTFM_API_BASE, params=params, timeout=15)
        response.raise_for_status()
        payload = response.json()

        if payload.get("error"):
            raise ValueError(payload.get("message") or "Last.fm search error")

        matches = (((payload.get("results") or {}).get("trackmatches") or {}).get("track") or [])
        if isinstance(matches, dict):
            matches = [matches]

        results = []
        seen = set()
        for item in matches:
            title = (item.get("name") or "").strip()
            artist = (item.get("artist") or "").strip()
            if not title or not artist:
                continue

            key = (title.lower(), artist.lower())
            if key in seen:
                continue
            seen.add(key)

            synthetic_id = f"lastfm:{sanitize_filename(artist)}:{sanitize_filename(title)}"
            results.append({
                "id": synthetic_id,
                "title": title,
                "artist": artist,
                "deezer_id": None,
                "path": None,
                "duration": None,
                "artwork_url": _best_lastfm_image(item.get("image")),
            })

        return results
    except Exception as e:
        print(f"Last.fm track search failed for query '{query}': {e}")
        return []


def search_lastfm_top_tracks_for_artist(artist: str, limit: int = 10) -> list[dict]:
    """Return lightweight candidate tracks for an artist when Deezer is unavailable."""
    if not LASTFM_API_KEY:
        return []

    try:
        params = {
            "method": "artist.gettoptracks",
            "api_key": LASTFM_API_KEY,
            "artist": artist,
            "limit": max(1, min(int(limit or 10), 50)),
            "autocorrect": 1,
            "format": "json",
        }
        response = requests.get(LASTFM_API_BASE, params=params, timeout=15)
        response.raise_for_status()
        payload = response.json()

        if payload.get("error"):
            raise ValueError(payload.get("message") or "Last.fm artist top tracks error")

        tracks = ((payload.get("toptracks") or {}).get("track") or [])
        if isinstance(tracks, dict):
            tracks = [tracks]

        candidates = []
        seen = set()
        for item in tracks:
            title = (item.get("name") or "").strip()
            artist_info = item.get("artist")
            if isinstance(artist_info, dict):
                artist_name = (artist_info.get("name") or artist).strip()
            else:
                artist_name = (artist_info or artist).strip()

            if not title or not artist_name:
                continue

            key = (title.lower(), artist_name.lower())
            if key in seen:
                continue
            seen.add(key)

            synthetic_id = f"lastfm:{sanitize_filename(artist_name)}:{sanitize_filename(title)}"
            candidates.append({
                "title": title,
                "artist": artist_name,
                "source_id": synthetic_id,
                "album": None,
                "year": None,
                "album_art_url": _best_lastfm_image(item.get("image")),
            })

        return candidates
    except Exception as e:
        print(f"Last.fm top tracks lookup failed for artist '{artist}': {e}")
        return []


def get_track_metadata_for_download(title: str, artist: str, deezer_id: Optional[str]) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Resolve album/year/art for downloads, preferring Deezer and falling back to Last.fm."""
    album = None
    year = None
    album_art_url = None

    if deezer_id and not is_deezer_quota_backoff_active():
        album, year, album_art_url = get_deezer_track_metadata(deezer_id)

    if (not album or not album_art_url or not year) and title and artist:
        lastfm_meta = search_lastfm_track_metadata(title, artist)
        if lastfm_meta:
            album = album or lastfm_meta.get("album")
            year = year or lastfm_meta.get("year")
            album_art_url = album_art_url or lastfm_meta.get("artwork_url")

    return album, year, album_art_url


def get_deezer_track_metadata(track_id: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Return album, year, and album art URL for a Deezer track id."""
    if not track_id:
        return None, None, None

    if is_deezer_quota_backoff_active():
        return None, None, None

    try:
        track_data = deezer_get(f"track/{track_id}")
        album = (track_data.get("album") or {}).get("title")
        release_date = track_data.get("release_date") or ""
        year = release_date[:4] if release_date else None
        album_art_url = (track_data.get("album") or {}).get("cover_xl") or (track_data.get("album") or {}).get("cover_big")
        return album, year, album_art_url
    except Exception as e:
        if _is_deezer_quota_error_message(str(e)):
            mark_deezer_quota_backoff(str(e))
        print(f"Failed to fetch Deezer metadata: {e}")
        return None, None, None


def get_local_track_duration(path: Optional[str]) -> Optional[float]:
    """Return local audio duration in seconds when available."""
    if not path or not os.path.exists(path):
        return None
    try:
        audio = MutagenFile(path)
        if audio and hasattr(audio, "info") and hasattr(audio.info, "length"):
            return float(audio.info.length)
    except Exception:
        pass
    return None


def get_local_track_artwork(path: Optional[str]) -> tuple[Optional[bytes], Optional[str]]:
    """Return embedded artwork bytes and mime type from a local audio file."""
    if not path or not os.path.exists(path):
        return None, None

    try:
        audio = MutagenFile(path)
        if not audio or not getattr(audio, "tags", None):
            return None, None

        tags = audio.tags
        if hasattr(tags, "getall"):
            pictures = tags.getall("APIC")
            if pictures:
                picture = pictures[0]
                return picture.data, picture.mime or "image/jpeg"

            flac_pictures = tags.getall("METADATA_BLOCK_PICTURE")
            if flac_pictures:
                picture = flac_pictures[0]
                return getattr(picture, "data", None), getattr(picture, "mime", None) or "image/jpeg"
    except Exception:
        pass

    return None, None


def get_local_track_full_metadata(path: Optional[str]) -> dict:
    """Read all embedded ID3 metadata from a local MP3 in one efficient pass."""
    result: dict = {}
    if not path or not os.path.exists(path) or not path.lower().endswith(".mp3"):
        return result
    try:
        audio = MP3(path, ID3=ID3)
        tags = audio.tags
        if not tags:
            return result
        if tags.get("TIT2"):
            result["title"] = str(tags["TIT2"])
        if tags.get("TPE1"):
            result["artist"] = str(tags["TPE1"])
        if tags.get("TALB"):
            result["album"] = str(tags["TALB"])
        if tags.get("TDRC"):
            result["year"] = str(tags["TDRC"])
        for txxx in tags.getall("TXXX"):
            if getattr(txxx, "desc", "").upper() == "DEEZER_ID" and txxx.text:
                result["deezer_id"] = str(txxx.text[0])
                break
        if tags.getall("APIC"):
            result["has_artwork"] = True
        if hasattr(audio, "info") and hasattr(audio.info, "length"):
            result["duration"] = float(audio.info.length)
    except Exception:
        pass
    return result


def mp3_has_embedded_metadata(path: Optional[str]) -> bool:
    """Check whether MP3 already has key metadata and cover art embedded."""
    if not path or not os.path.exists(path) or not path.lower().endswith(".mp3"):
        return False

    try:
        audio = MP3(path, ID3=ID3)
        tags = audio.tags
        if not tags:
            return False

        has_title = bool(tags.get("TIT2"))
        has_artist = bool(tags.get("TPE1"))
        has_cover = bool(tags.getall("APIC"))
        return has_title and has_artist and has_cover
    except Exception:
        return False


def mp3_has_basic_metadata(path: Optional[str]) -> bool:
    """Check whether MP3 has at least title + artist tags."""
    if not path or not os.path.exists(path) or not path.lower().endswith(".mp3"):
        return False

    try:
        audio = MP3(path, ID3=ID3)
        tags = audio.tags
        if not tags:
            return False
        return bool(tags.get("TIT2")) and bool(tags.get("TPE1"))
    except Exception:
        return False


def upsert_mp3_metadata(path: Optional[str], title: Optional[str], artist: Optional[str], album: Optional[str], year: Optional[str], artwork_url: Optional[str], deezer_id: Optional[str] = None) -> bool:
    """Write resolved metadata into MP3 once so future UI loads stay local."""
    if not path or not os.path.exists(path) or not path.lower().endswith(".mp3"):
        return False

    artwork_bytes = None
    artwork_mime = None
    if artwork_url:
        try:
            response = requests.get(artwork_url, timeout=12)
            response.raise_for_status()
            artwork_bytes = response.content
            artwork_mime = response.headers.get("Content-Type", "image/jpeg")
        except Exception as e:
            print(f"Failed to fetch artwork for metadata upsert ({path}): {e}")

    try:
        audio = MP3(path, ID3=ID3)
        try:
            audio.add_tags()
        except Exception:
            pass

        if audio.tags is None:
            return False

        if title:
            audio.tags["TIT2"] = TIT2(encoding=3, text=str(title))
        if artist:
            audio.tags["TPE1"] = TPE1(encoding=3, text=str(artist))
            audio.tags["TPE2"] = TPE2(encoding=3, text=str(artist))
        if album:
            audio.tags["TALB"] = TALB(encoding=3, text=str(album))
        if year:
            audio.tags["TDRC"] = TDRC(encoding=3, text=str(year))
        if deezer_id:
            audio.tags.delall("TXXX:DEEZER_ID")
            audio.tags.add(TXXX(encoding=3, desc="DEEZER_ID", text=[str(deezer_id)]))
        if artwork_bytes:
            audio.tags.delall("APIC")
            audio.tags.add(APIC(
                encoding=3,
                mime=artwork_mime or "image/jpeg",
                type=3,
                desc="Cover",
                data=artwork_bytes,
            ))

        audio.save(v2_version=3)
        return True
    except Exception as e:
        print(f"Failed to upsert MP3 metadata ({path}): {e}")
        return False


def embed_artwork_into_mp3(path: Optional[str], artwork_url: Optional[str]) -> bool:
    """Fetch artwork once and store it in the MP3's ID3 tags."""
    if not path or not artwork_url or not os.path.exists(path):
        return False
    if not path.lower().endswith(".mp3"):
        return False

    try:
        response = requests.get(artwork_url, timeout=12)
        response.raise_for_status()
        artwork_bytes = response.content
        mime = response.headers.get("Content-Type", "image/jpeg")
    except Exception as e:
        print(f"Failed to fetch artwork for embedding ({path}): {e}")
        return False

    try:
        audio = MP3(path, ID3=ID3)
        try:
            audio.add_tags()
        except Exception:
            pass

        if audio.tags is None:
            return False

        audio.tags.delall("APIC")
        audio.tags.add(APIC(
            encoding=3,
            mime=mime,
            type=3,
            desc="Cover",
            data=artwork_bytes,
        ))
        audio.save(v2_version=3)
        return True
    except Exception as e:
        print(f"Failed to embed artwork into MP3 ({path}): {e}")
        return False


def search_deezer_track_metadata(title: str, artist: str):
    """Best-effort metadata lookup by title + artist.

    Deezer is primary. When Deezer quota is exceeded, fall back to Last.fm.
    """
    lookup_title, lookup_artist = prepare_track_metadata_lookup(title, artist)
    if not lookup_title or not lookup_artist:
        return None

    if is_deezer_quota_backoff_active():
        return search_lastfm_track_metadata(lookup_title, lookup_artist)

    try:
        query = f'track:"{lookup_title}" artist:"{lookup_artist}"'
        data = deezer_get("search", params={"q": query, "limit": 5})
        items = data.get("data") or []
        if not items:
            fallback = deezer_get("search", params={"q": f"{lookup_title} {lookup_artist}", "limit": 5})
            items = fallback.get("data") or []
        if not items:
            return search_lastfm_track_metadata(lookup_title, lookup_artist)

        best = items[0]
        duration = best.get("duration")
        album = (best.get("album") or {}).get("title")
        release_date = best.get("release_date") or ""
        year = release_date[:4] if release_date else None
        cover = (best.get("album") or {}).get("cover_xl") or (best.get("album") or {}).get("cover_big") or (best.get("album") or {}).get("cover_medium")
        return {
            "duration": float(duration) if duration else None,
            "album": album,
            "year": year,
            "artwork_url": cover,
            "deezer_id": str(best.get("id")) if best.get("id") else None,
        }
    except Exception as e:
        if _is_deezer_quota_error_message(str(e)):
            mark_deezer_quota_backoff(str(e))
            fallback_meta = search_lastfm_track_metadata(lookup_title, lookup_artist)
            if fallback_meta:
                print(f"Using Last.fm fallback metadata for {lookup_artist} - {lookup_title}")
                return fallback_meta
        print(f"Deezer metadata search failed for {lookup_artist} - {lookup_title}: {e}")
        return None


def enrich_track_for_ui(track: dict, allow_remote_lookup: bool = False) -> dict:
    """Return artwork/duration enriched track data for UI lists.

    By default this is local-only to avoid repeated network calls while browsing the UI.
    """
    base = dict(track or {})
    cache_key = get_track_cache_key(base)
    cached = get_cached_track_metadata(cache_key)

    title = base.get("title", "")
    artist = base.get("artist", "")
    lookup_title, lookup_artist = prepare_track_metadata_lookup(title, artist)
    is_downloading_placeholder = is_downloading_placeholder_track(base)
    source_id = base.get("deezer_id") or base.get("spotify_id") or base.get("id")
    path = base.get("path") or cached.get("path")
    deezer_id = base.get("deezer_id") or cached.get("deezer_id")
    spotify_id = base.get("spotify_id") or cached.get("spotify_id")
    album = base.get("album") or cached.get("album")
    year = base.get("year") or cached.get("year")
    duration = base.get("duration") or cached.get("duration")
    artwork_url = base.get("artwork_url") or base.get("album_art") or base.get("thumbnail") or cached.get("artwork_url")

    # Fast path: read all needed data from MP3 tags in one pass (avoids remote calls on restart)
    if path and path.lower().endswith(".mp3"):
        embedded = get_local_track_full_metadata(path)
        if embedded:
            deezer_id = deezer_id or embedded.get("deezer_id")
            album = album or embedded.get("album")
            year = year or embedded.get("year")
            duration = duration or embedded.get("duration")
            if not artwork_url and embedded.get("has_artwork"):
                artwork_url = f"/api/artwork/by-path?path={requests.utils.quote(path)}"

    if not path and source_id:
        path = find_local_track(lookup_title or title, lookup_artist or artist, source_id)

    if is_downloading_placeholder and not path:
        enriched = {
            **base,
            "path": path,
            "album": album,
            "year": year,
            "duration": duration,
            "artwork_url": artwork_url,
            "deezer_id": deezer_id,
            "spotify_id": spotify_id,
        }
        set_cached_track_metadata(cache_key, {
            "path": path,
            "album": album,
            "year": year,
            "duration": duration,
            "artwork_url": artwork_url,
            "deezer_id": deezer_id,
            "spotify_id": spotify_id,
        })
        return enriched

    if path and not duration:
        duration = get_local_track_duration(path)

    if path and not artwork_url:
        artwork_data, _ = get_local_track_artwork(path)
        if artwork_data:
            artwork_url = f"/api/artwork/by-path?path={requests.utils.quote(path)}"

    if allow_remote_lookup and deezer_id and (not artwork_url or not album or not year):
        deezer_album, deezer_year, deezer_art = get_deezer_track_metadata(deezer_id)
        album = album or deezer_album
        year = year or deezer_year
        artwork_url = artwork_url or deezer_art

    if allow_remote_lookup and (not artwork_url or not duration) and lookup_title and lookup_artist:
        deezer_meta = search_deezer_track_metadata(lookup_title, lookup_artist)
        if deezer_meta:
            duration = duration or deezer_meta.get("duration")
            album = album or deezer_meta.get("album")
            year = year or deezer_meta.get("year")
            artwork_url = artwork_url or deezer_meta.get("artwork_url")
            deezer_id = deezer_id or deezer_meta.get("deezer_id")

    # Prefer local embedded artwork: if we only have remote art and local MP3, embed it once.
    if allow_remote_lookup and path and artwork_url and artwork_url.startswith("http"):
        tried_already = False
        with ARTWORK_EMBED_ATTEMPTED_LOCK:
            if path in ARTWORK_EMBED_ATTEMPTED_PATHS:
                tried_already = True
            else:
                ARTWORK_EMBED_ATTEMPTED_PATHS.add(path)

        if not tried_already and embed_artwork_into_mp3(path, artwork_url):
            artwork_data, _ = get_local_track_artwork(path)
            if artwork_data:
                artwork_url = f"/api/artwork/by-path?path={requests.utils.quote(path)}"

    # Persist resolved metadata to local MP3 once, so future loads are local-only.
    if path and path.lower().endswith(".mp3"):
        should_upsert = False
        with MP3_METADATA_EMBED_ATTEMPTED_LOCK:
            if path not in MP3_METADATA_EMBED_ATTEMPTED_PATHS:
                MP3_METADATA_EMBED_ATTEMPTED_PATHS.add(path)
                should_upsert = not mp3_has_embedded_metadata(path)

        if should_upsert:
            remote_artwork = artwork_url if allow_remote_lookup and artwork_url and artwork_url.startswith("http") else None
            upsert_mp3_metadata(path, lookup_title or title, lookup_artist or artist, album, year, remote_artwork, deezer_id)
            artwork_data, _ = get_local_track_artwork(path)
            if artwork_data:
                artwork_url = f"/api/artwork/by-path?path={requests.utils.quote(path)}"

    enriched = {
        **base,
        "path": path,
        "album": album,
        "year": year,
        "duration": duration,
        "artwork_url": artwork_url,
        "deezer_id": deezer_id,
        "spotify_id": spotify_id,
    }
    set_cached_track_metadata(cache_key, {
        "path": path,
        "album": album,
        "year": year,
        "duration": duration,
        "artwork_url": artwork_url,
        "deezer_id": deezer_id,
        "spotify_id": spotify_id,
    })
    return enriched


def enrich_playlist_track(track: dict) -> dict:
    """Return a UI-friendly metadata bundle for a playlist track."""
    return enrich_track_for_ui(track)


def download_missing_song_from_youtube(title: str, artist: str, source_id: Optional[str], album: str = None, year: str = None, album_art_url: str = None) -> Optional[str]:
    """
    Try top 5 YouTube search results.
    Download the first one that produces a valid MP3.
    Add metadata from provider API after download.
    Uses DOWNLOAD_SEMAPHORE to ensure sequential downloads (important for Raspberry Pi).
    """
    print(f"\n{'='*60}")
    print(f"DOWNLOAD START: {artist} - {title}")
    print(f"{'='*60}")
    
    safe_title = sanitize_filename(title)
    safe_artist = sanitize_filename(artist)

    output_template = f"{safe_artist} - {safe_title}.%(ext)s"
    final_mp3 = os.path.join(MUSIC_DIR, f"{safe_artist} - {safe_title}.mp3")

    # If already exists, return it
    if os.path.exists(final_mp3):
        print(f"✓ Already exists: {final_mp3}")
        if not mp3_has_basic_metadata(final_mp3):
            print(f"Existing MP3 is missing metadata, repairing tags: {final_mp3}")
            deezer_tag = str(source_id) if source_id and str(source_id).isdigit() else None
            repaired = upsert_mp3_metadata(final_mp3, title, artist, album, year, album_art_url, deezer_tag)
            if not repaired:
                try:
                    audio = MP3(final_mp3, ID3=ID3)
                    try:
                        audio.add_tags()
                    except Exception:
                        pass
                    if audio.tags is not None:
                        audio.tags["TIT2"] = TIT2(encoding=3, text=title)
                        audio.tags["TPE1"] = TPE1(encoding=3, text=artist)
                        if album:
                            audio.tags["TALB"] = TALB(encoding=3, text=album)
                        if year:
                            audio.tags["TDRC"] = TDRC(encoding=3, text=year)
                        audio.save(v2_version=3)
                except Exception as e:
                    print(f"Failed to repair existing MP3 metadata: {e}")
        return final_mp3

    # Acquire semaphore to ensure only 1 download runs at a time
    with DOWNLOAD_SEMAPHORE:
        # Try top 5 results with multiple strategies
        for i in range(1, 6):
            # Prefer lyric/official audio, and avoid common unwanted variants.
            queries = [f"ytsearch{i}:{query}" for query in build_download_queries(title, artist)]

            # Try different extractor strategies to bypass bot detection
            strategies = [
                ["--extractor-args", "youtube:player_client=android"],
                ["--extractor-args", "youtube:player_client=ios"],
                ["--extractor-args", "youtube:player_client=web"],
                [],  # Fallback: no extra args
            ]
            
            for query in queries:
                print(f"Trying YouTube result {i}: {query}")
                for idx, extra_args in enumerate(strategies):
                    # Download AUDIO ONLY for music (not video like lofi)
                    audio_template = f"{safe_artist} - {safe_title}.%(ext)s"
                    command = [
                        *resolve_yt_dlp_command(),
                        *extra_args,
                        "--extract-audio",
                        "--audio-format", "mp3",
                        "--audio-quality", "0",
                        "--no-playlist",
                        "--max-downloads", "1",
                        "--no-check-certificate",
                        "--output", os.path.join(MUSIC_DIR, audio_template),
                        query
                    ]

                    # Run yt-dlp with timeout to prevent hanging
                    print(f"Running: {' '.join(command)}")
                    try:
                        result = subprocess.run(command, shell=False, capture_output=True, text=True, timeout=60)
                        print(f"yt-dlp returned: {result.returncode}")
                        if result.returncode != 0:
                            print(f"stderr: {result.stderr[:300]}")
                        if result.stdout:
                            print(f"stdout: {result.stdout[:200]}")
                        
                        # Check if MP3 was created
                        if os.path.exists(final_mp3):
                            print(f"✓ Downloaded MP3: {final_mp3}")
                            break  # Success, exit strategy loop
                        else:
                            print(f"✗ MP3 not found at {final_mp3}")
                    except subprocess.TimeoutExpired:
                        print(f"✗ Download timeout after 60 seconds")
                    except Exception as e:
                        print(f"✗ Download error: {e}")

                if os.path.exists(final_mp3):
                    break
            
            # If we successfully downloaded, break out of result loop
            if os.path.exists(final_mp3):
                print("Downloaded:", final_mp3)

                deezer_tag = str(source_id) if source_id and str(source_id).isdigit() else None
                metadata_written = upsert_mp3_metadata(
                    final_mp3,
                    title,
                    artist,
                    album,
                    year,
                    album_art_url,
                    deezer_tag,
                )
                if not metadata_written:
                    print(f"Metadata upsert had issues for {final_mp3}, retrying core tags...")
                    try:
                        audio = MP3(final_mp3, ID3=ID3)
                        try:
                            audio.add_tags()
                        except Exception:
                            pass
                        if audio.tags is not None:
                            audio.tags["TIT2"] = TIT2(encoding=3, text=title)
                            audio.tags["TPE1"] = TPE1(encoding=3, text=artist)
                            if album:
                                audio.tags["TALB"] = TALB(encoding=3, text=album)
                            if year:
                                audio.tags["TDRC"] = TDRC(encoding=3, text=year)
                            audio.save(v2_version=3)
                    except Exception as e:
                        print(f"Failed to write fallback core metadata: {e}")

                if mp3_has_basic_metadata(final_mp3):
                    print(f"✓ MP3 ready with metadata: {final_mp3}")
                else:
                    print(f"⚠ MP3 saved but metadata tags are incomplete: {final_mp3}")
                
                print(f"\n{'='*60}")
                print(f"✓ DOWNLOAD SUCCESS: {artist} - {title}")
                print(f"Saved to: {final_mp3}")
                print(f"{'='*60}\n")
                return final_mp3

            print(f"Result {i} failed, trying next…")

    print(f"\n{'='*60}")
    print(f"✗ DOWNLOAD FAILED: {artist} - {title}")
    print(f"All 5 YouTube search attempts failed")
    print(f"{'='*60}\n")
    return None


# ---------- Config ----------
MUSIC_DIR = os.path.join(os.path.dirname(__file__), "music")
LOFI_DIR = os.path.join(os.path.dirname(__file__), "lofi")
FAV_FILE = os.path.join(os.path.dirname(__file__), "favorites.json")

# Ensure directories exist
os.makedirs(MUSIC_DIR, exist_ok=True)
os.makedirs(LOFI_DIR, exist_ok=True)


# ---------- Data models ----------
@dataclass
class Track:
    id: str            # internal ID (e.g. filename or hash)
    title: str
    artist: str
    path: str          # absolute file path
    deezer_id: Optional[str] = None
    spotify_id: Optional[str] = None
    artwork_url: Optional[str] = None


# ---------- Player class ----------
class Player:
    def __init__(self, music_dir: str):
        self.music_dir = music_dir
        self.queue: List[Track] = []
        self.played: List[Track] = []
        self.failed_source_ids: set[str] = set()
        self._is_paused = False
        self._paused_elapsed = 0.0
        self.shuffle = False  # shuffle mode
        self.auto_fill = False  # auto-add when queue is empty

        # add this in __init__
        self._downloading = set()  # tracks artists currently being downloaded

        self.current: Optional[Track] = None
        self.volume: float = 0.7
        self.repeat: str = "off"  # off, one, all
        self.lock = threading.Lock()
        
        # Playback mode: "host" or "browser"
        self.playback_mode: str = "host"
        # Browser-reported state (when in browser mode)
        self.browser_elapsed: float = 0.0
        self.browser_last_update: float = 0.0

        # Prefer ALSA for real output, but allow a dummy fallback so the API can
        # still boot on systems where sound hardware is temporarily unavailable.
        os.environ.setdefault("SDL_AUDIODRIVER", "alsa")
        self.audio_backend = os.environ.get("SDL_AUDIODRIVER", "alsa")
        try:
            pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=4096)
        except pygame.error as err:
            print(f"[AUDIO] Failed to init backend '{self.audio_backend}': {err}")
            if self.audio_backend == "dummy":
                raise

            os.environ["SDL_AUDIODRIVER"] = "dummy"
            self.audio_backend = "dummy"
            pygame.mixer.quit()
            pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=4096)
            print("[AUDIO] Falling back to SDL_AUDIODRIVER=dummy (no physical audio output).")

        pygame.mixer.music.set_volume(self.volume)

        self.current_start_ts: Optional[float] = None
        self.current_duration: float = 0.0

        # background thread to monitor when tracks end
        self._stop_flag = False
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def prev(self):
        with self.lock:
            # No current or no history → do nothing
            if not self.current or len(self.played) < 2:
                print("No previous track available")
                return

            # The last entry in played is the current track
            current_track = self.current

            # The previous track is the one before it
            previous_track = self.played[-2]

            print(f"Going to previous track: {previous_track.title}")

            # Remove BOTH from played history
            self.played = self.played[:-2]

            # Put current track back at the front of the queue
            self.queue.insert(0, current_track)

            # Make previous track the new current
            self.current = previous_track

            # Load and play it
            try:
                pygame.mixer.music.load(previous_track.path)
                pygame.mixer.music.play()
            except Exception as e:
                print("Error playing previous track:", e)
                # If it fails, skip to next
                self._play_next_locked()
                return

            # Reset timing
            self.current_start_ts = time.time()

            # Detect duration
            try:
                audio = MutagenFile(previous_track.path)
                if audio and hasattr(audio, "info") and hasattr(audio.info, "length"):
                    self.current_duration = float(audio.info.length)
                else:
                    self.current_duration = 0.0
            except:
                self.current_duration = 0.0

            print(f"Now playing previous track: {previous_track.title}")

    # --- queue management ---
    def add_to_queue(self, track: Track):
        with self.lock:
            print("\n--- add_to_queue ---")
            print("Before append: current =", self.current)
            print("Queue before:", [t.title for t in self.queue])

            self.queue.append(track)

            print("After append: current =", self.current)
            print("Queue after:", [t.title for t in self.queue])

            if self.current is None:
                print("Triggering _play_next_locked()")
                self._play_next_locked()
            else:
                print("Not triggering _play_next_locked() because current is NOT None")

    def remove_from_queue(self, track_id: str):
        with self.lock:
            self.queue = [t for t in self.queue if t.id != track_id]

    def clear_queue(self):
        with self.lock:
            self.queue.clear()
            self.played.clear()
            # Stop playback and reset state (inline to avoid nested lock)
            if self.playback_mode == "host":
                pygame.mixer.music.stop()
            self.current = None
            self.current_start_ts = None
            self.current_duration = 0.0
            self.browser_elapsed = 0.0
            self.browser_last_update = 0.0
            self._is_paused = False
            self._paused_elapsed = 0.0
            self._is_paused = False
            self._paused_elapsed = 0.0

    def fetch_new_track_for_artist(self, artist: str) -> bool:
        """
        Attempts to discover + download a new track for the given artist.
        Returns True if something new was downloaded.
        """
        print("Attempting to fetch new track(s) for artist(s):", artist)

        played_titles = {normalize(t.title) for t in self.played}

        candidates = []
        provider = "deezer"
        try:
            if is_deezer_quota_backoff_active():
                raise ValueError("Quota exceeded")

            res = deezer_get("search", params={"q": f'artist:"{artist}"', "limit": 10})
            for item in res.get("data", []):
                title = item.get("title", "")
                artist_name = (item.get("artist") or {}).get("name", artist)
                source_id = str(item.get("id", ""))
                album = (item.get("album") or {}).get("title", "")
                release_date = item.get("release_date") or ""
                year = release_date[:4] if release_date else ""
                album_art_url = (item.get("album") or {}).get("cover_xl") or (item.get("album") or {}).get("cover_big")
                candidates.append({
                    "title": title,
                    "artist": artist_name,
                    "source_id": source_id,
                    "album": album,
                    "year": year,
                    "album_art_url": album_art_url,
                })
        except Exception as e:
            if _is_deezer_quota_error_message(str(e)) or is_deezer_quota_backoff_active():
                provider = "lastfm"
                candidates = search_lastfm_top_tracks_for_artist(artist, limit=10)
                if not candidates and not LASTFM_API_KEY:
                    print("Last.fm fallback unavailable: LASTFM_API_KEY is not configured.")
                print(f"Using Last.fm fallback with {len(candidates)} tracks for artist search '{artist}'")
            else:
                print(f"Artist search failed for '{artist}': {e}")
                return False

        print(f"{provider.capitalize()} returned {len(candidates)} tracks for artist search '{artist}'")
        downloaded_any = False

        for idx, item in enumerate(candidates, start=1):
            title = item.get("title", "")
            artist_name = item.get("artist", artist)
            source_id = item.get("source_id")
            album = item.get("album")
            year = item.get("year")
            album_art_url = item.get("album_art_url")

            if provider == "lastfm" and (not album or not year or not album_art_url):
                meta = search_lastfm_track_metadata(title, artist_name)
                if meta:
                    album = album or meta.get("album")
                    year = year or meta.get("year")
                    album_art_url = album_art_url or meta.get("artwork_url")

            if normalize(title) in played_titles:
                print(f"[{idx}] Candidate: {title} by {artist_name} → already played, skipping")
                continue

            print(f"[{idx}] Candidate: {title} by {artist_name}")
            path = download_missing_song_from_youtube(title, artist_name, source_id, album, year, album_art_url)
            if path:
                print(f"→ downloaded successfully: {path}")
                downloaded_any = True
                # Add to queue and played immediately
                with self.lock:
                    track = Track(id=os.path.basename(path), title=title, artist=artist_name, path=path)
                    self.queue.append(track)
                    self.played.append(track)
                    print("Adding downloaded track to queue and played:", track.title)
                break  # only download one track at a time

        if not downloaded_any:
            print("Artist exhausted, no new tracks available")
        return downloaded_any

    def _pick_random_track_same_artist(self) -> Optional[Track]:
        if not self.played:
            return None

        # Pool of unique played artists
        played_artists = list({normalize(t.artist): t.artist for t in self.played}.values())
        random.shuffle(played_artists)

        # Collect forbidden paths: played + queue + current
        forbidden_paths = {t.path for t in self.played} | {t.path for t in self.queue}
        if self.current:
            forbidden_paths.add(self.current.path)

        for artist in played_artists:
            artist_n = normalize(artist)
            candidates = []

            for root, _, files in os.walk(self.music_dir):
                for f in files:
                    name, ext = os.path.splitext(f)
                    if ext.lower() not in [".mp3", ".wav", ".flac", ".m4a", ".ogg"]:
                        continue
                    if " - " not in name:
                        continue
                    f_artist, f_title = name.split(" - ", 1)
                    path = os.path.join(root, f)

                    # Skip if already in forbidden paths
                    if path in forbidden_paths:
                        continue
                    if normalize(f_artist) != artist_n:
                        continue
                    candidates.append((f_title, path))

            if candidates:
                title, path = random.choice(candidates)
                return Track(
                    id=os.path.basename(path),
                    title=title,
                    artist=artist,
                    path=path
                )

            # No local tracks → download if not already downloading
            if artist_n not in self._downloading:
                print(f"Local tracks exhausted for {artist}, starting background download…")
                self._downloading.add(artist_n)
                threading.Thread(target=self._download_and_queue, args=(artist,), daemon=True).start()

        return None

    def _download_and_queue(self, artist: str):
        try:
            success = self.fetch_new_track_for_artist(artist)
            if not success:
                print("No new tracks found after download")
        finally:
            # Mark download as finished
            self._downloading.discard(normalize(artist))

    def _ensure_queue_not_empty(self):
        if not self.auto_fill:
            return
        if len(self.queue) == 0:
            print("Queue low → auto-picking next track")
            track = self._pick_random_track_same_artist()
            if track:
                self.queue.append(track)
                print("Auto-added:", track.title)

    def fetch_new_tracks_for_artist(self, artist) -> Optional[Track]:
        if not self.current:
            return None

        artist_n = normalize(self.current.artist)

        # Collect forbidden paths: played + queue + current
        forbidden = {t.path for t in self.played} | {t.path for t in self.queue}
        if self.current:
            forbidden.add(self.current.path)

        candidates = []
        for root, _, files in os.walk(self.music_dir):
            for f in files:
                name, ext = os.path.splitext(f)
                if ext.lower() not in [".mp3", ".wav", ".flac", ".m4a", ".ogg"]:
                    continue
                if " - " not in name:
                    continue
                f_artist, f_title = name.split(" - ", 1)
                if normalize(f_artist) != artist_n:
                    continue
                path = os.path.join(root, f)
                if path in forbidden:
                    continue
                candidates.append((f_title, path))

        if not candidates:
            print("Local artist tracks exhausted, starting background download…")
            # start download in background thread
            threading.Thread(target=self._download_and_queue, args=(self.current.artist,), daemon=True).start()
            return None

        title, path = random.choice(candidates)
        return Track(
            id=os.path.basename(path),
            title=title,
            artist=self.current.artist,
            path=path
        )

    # --- playback controls ---
    def _play_next_locked(self):
        print("\n--- _play_next_locked CALLED ---")

        # Handle repeat-one: replay current track
        if self.current and self.repeat == "one":
            print("Repeat mode: replaying current track")
            # Only load/play in host mode
            if self.playback_mode == "host":
                pygame.mixer.music.load(self.current.path)
                pygame.mixer.music.play()
            # Reset timestamps for both modes
            self.current_start_ts = time.time()
            self.browser_elapsed = 0.0
            self.browser_last_update = time.time()
            try:
                audio = MutagenFile(self.current.path)
                if audio and hasattr(audio, "info") and hasattr(audio.info, "length"):
                    self.current_duration = float(audio.info.length)
                else:
                    self.current_duration = 0.0
            except:
                self.current_duration = 0.0
            return

        if self.current:
            print("Moving to played list:", self.current.title)
            self.played.append(self.current)

        # Skip downloading placeholders at the front of the queue
        attempts = 0
        while self.queue and getattr(self.queue[0], "_downloading", False):
            print(f"Skipping downloading track in queue: {self.queue[0].title}")
            self.queue.append(self.queue.pop(0))
            attempts += 1
            if attempts >= len(self.queue):
                print("All tracks in queue are downloading. Stopping playback.")
                self.current = None
                pygame.mixer.music.stop()
                self.current_start_ts = None
                self.current_duration = 0.0
                return

        if not self.queue:
            # Handle repeat-all: replay entire played list
            if self.repeat == "all" and self.played:
                print("Repeat mode: replaying entire queue")
                self.queue = self.played.copy()
                self.played.clear()
            else:
                print("Queue empty → stopping playback")
                self.current = None
                pygame.mixer.music.stop()
                self.current_start_ts = None
                self.current_duration = 0.0
                return

        # Pop next track (shuffle if enabled)
        if self.shuffle and len(self.queue) > 1:
            non_downloading_indices = [i for i, t in enumerate(self.queue) if not getattr(t, "_downloading", False)]
            if non_downloading_indices:
                idx = random.choice(non_downloading_indices)
                self.current = self.queue.pop(idx)
                print(f"Shuffle mode: randomly selected track at index {idx}")
            else:
                print("All tracks in queue are downloading. Stopping playback.")
                self.current = None
                pygame.mixer.music.stop()
                self.current_start_ts = None
                self.current_duration = 0.0
                return
        else:
            self.current = self.queue.pop(0)
        # MARK IT AS PLAYED IMMEDIATELY to prevent duplicate downloads
        print("Adding to played list:", self.current.title)
        self.played.append(self.current)

        print("Now playing:", self.current.title, "-", self.current.artist)
        print("File path:", self.current.path)

        # Load + play (only in host mode)
        if self.playback_mode == "host":
            pygame.mixer.music.load(self.current.path)
            pygame.mixer.music.play()

        # Start timestamp
        self.current_start_ts = time.time()
        self.browser_elapsed = 0.0
        self.browser_last_update = time.time()
        print("Start timestamp:", self.current_start_ts)

        self._ensure_queue_not_empty()

        # Detect duration
        print("Reading duration with mutagen...")
        try:
            audio = MutagenFile(self.current.path)
            if audio and hasattr(audio, "info") and hasattr(audio.info, "length"):
                self.current_duration = float(audio.info.length)
                print("✔ Duration detected:", self.current_duration)
            else:
                print("❌ Mutagen returned no duration")
                self.current_duration = 0.0
        except Exception as e:
            print("❌ Mutagen error:", e)
            self.current_duration = 0.0

        print("--- END _play_next_locked ---\n")

    def next(self):
        with self.lock:
            self._play_next_locked()

    def play(self):
        with self.lock:
            if self.current is None:
                self._play_next_locked()
                return

            # Only control pygame in host mode
            if self.playback_mode == "host":
                pygame.mixer.music.unpause()
            
            self._is_paused = False

            # Adjust start timestamp so elapsed resumes correctly
            if self.playback_mode == "host":
                self.current_start_ts = time.time() - self._paused_elapsed
            else:
                # In browser mode, timestamp based on browser elapsed
                self.current_start_ts = time.time() - self.browser_elapsed

    def pause(self):
        with self.lock:
            if not self.current:
                return

            # Only control pygame in host mode
            if self.playback_mode == "host":
                pygame.mixer.music.pause()
            
            self._is_paused = True

            # Freeze elapsed time
            if self.playback_mode == "host":
                self._paused_elapsed = time.time() - (self.current_start_ts or time.time())
            else:
                # In browser mode, use browser elapsed
                self._paused_elapsed = self.browser_elapsed

    def stop(self):
        with self.lock:
            if self.playback_mode == "host":
                pygame.mixer.music.stop()
            self.current = None
            self.current_start_ts = None
            self.current_duration = 0.0
            self.browser_elapsed = 0.0
            self.browser_last_update = 0.0

    def set_volume(self, vol: float):
        vol = max(0.0, min(1.0, vol))
        with self.lock:
            self.volume = vol
            pygame.mixer.music.set_volume(vol)

    # --- state reporting ---
    def get_state(self):
        def track_to_dict(t):
            d = asdict(t)
            # Include _lofi_downloading if present
            if hasattr(t, "_lofi_downloading"):
                d["_lofi_downloading"] = t._lofi_downloading
            if hasattr(t, "_downloading"):
                d["_downloading"] = t._downloading
            return d
        with self.lock:
            if self.current:
                # Use browser elapsed if in browser mode and recently updated
                if self.playback_mode == "browser" and (time.time() - self.browser_last_update) < 2.0:
                    elapsed = self.browser_elapsed
                else:
                    elapsed = time.time() - (self.current_start_ts or time.time())
                
                duration = self.current_duration
                progress = elapsed / duration if duration > 0 else 0
                progress = max(0.0, min(1.0, progress))

                current_info = track_to_dict(self.current)
                current_info.update({
                    "elapsed": elapsed,
                    "duration": duration,
                    "progress": progress,
                    "remaining": max(0.0, duration - elapsed) if duration > 0 else 0,
                })
            else:
                current_info = None

            return {
                "current": current_info,
                "queue": [track_to_dict(t) for t in self.queue],
                "volume": self.volume,
                "repeat": self.repeat,
                "paused": self._is_paused,
                "playing": pygame.mixer.music.get_busy() if self.playback_mode == "host" else not self._is_paused,
                "shuffle": self.shuffle,
                "auto_fill": self.auto_fill,
                "playback_mode": self.playback_mode,
            }

    # --- background loop ---
    def _loop(self):
        while not self._stop_flag:
            with self.lock:
                is_playing = pygame.mixer.music.get_busy()

                # Detect paused state
                is_paused = False
                if self._is_paused:
                    # Do nothing while paused
                    pass

                elif self.current and not is_playing:
                    # Only handle finished tracks
                    if self.current_duration > 0:
                        elapsed = time.time() - (self.current_start_ts or time.time())
                        if elapsed >= self.current_duration:
                            self._play_next_locked()

                # If playing normally, nothing to do
            time.sleep(0.5)

    def shutdown(self):
        self._stop_flag = True
        self._thread.join()
        pygame.mixer.music.quit()


# ---------- UDP Discovery Server ----------
def start_discovery_server(port=5555):
    """
    Start UDP broadcast discovery server.
    Listens for "DISCOVER_MUSICBOX" and responds with server IP.
    """
    def discovery_loop():
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(('', port))
        print(f"[DISCOVERY] UDP server listening on port {port}")
        
        while True:
            try:
                data, addr = sock.recvfrom(1024)
                message = data.decode('utf-8').strip()
                
                if message == "DISCOVER_MUSICBOX":
                    # Get server's local IP address
                    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    try:
                        s.connect(('8.8.8.8', 80))
                        server_ip = s.getsockname()[0]
                    finally:
                        s.close()
                    
                    response = f"MUSICBOX_HERE:{server_ip}:7000"
                    sock.sendto(response.encode('utf-8'), addr)
                    print(f"[DISCOVERY] Responded to {addr[0]} with {server_ip}:7000")
            except Exception as e:
                print(f"[DISCOVERY] Error: {e}")
    
    thread = threading.Thread(target=discovery_loop, daemon=True)
    thread.start()

# ---------- Flask app ----------
app = Flask(__name__)
player = Player(MUSIC_DIR)

# Start discovery server
start_discovery_server()


@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/playlists", methods=["GET"])
def api_get_playlists():
    playlists = load_json(PLAYLISTS_FILE, {})
    return jsonify(playlists)

@app.route("/api/playlists/save", methods=["POST"])
def api_save_playlists():
    data = request.get_json() or {}
    save_json(PLAYLISTS_FILE, data)
    return jsonify({"status": "ok"})

@app.route("/api/playlists/download", methods=["POST"])
def api_download_playlist():
    """Create and serve a zip file of all tracks in a playlist."""
    data = request.get_json() or {}
    playlist_name = data.get("name")
    
    if not playlist_name:
        return jsonify({"error": "Missing playlist name"}), 400
    
    playlists = load_json(PLAYLISTS_FILE, {})
    
    if playlist_name not in playlists:
        return jsonify({"error": "Playlist not found"}), 404
    
    tracks = playlists[playlist_name]
    
    if not tracks:
        return jsonify({"error": "Playlist is empty"}), 400
    
    # Create zip file in memory
    zip_buffer = BytesIO()
    
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
        for track in tracks:
            # Find the track file
            path = None
            if track.get("path") and os.path.exists(track["path"]):
                path = track["path"]
            else:
                # Try to find it
                path = find_local_track(
                    track.get("title", ""),
                    track.get("artist", ""),
                    track.get("id", "")
                )
            
            if path and os.path.exists(path):
                # Add file to zip with just the filename (not full path)
                filename = os.path.basename(path)
                zip_file.write(path, filename)
    
    # Prepare the zip for download
    zip_buffer.seek(0)
    
    # Sanitize playlist name for filename
    safe_name = sanitize_filename(playlist_name)
    
    return send_file(
        zip_buffer,
        mimetype='application/zip',
        as_attachment=True,
        download_name=f"{safe_name}.zip"
    )


@app.route("/api/playlists/enrich", methods=["POST"])
def api_enrich_playlist_tracks():
    data = request.get_json() or {}
    tracks = data.get("tracks") or []
    if not isinstance(tracks, list):
        return jsonify({"error": "tracks must be a list"}), 400

    enriched = []
    for track in tracks:
        if not isinstance(track, dict):
            continue
        enriched.append(enrich_playlist_track(track))

    return jsonify({"tracks": enriched})


@app.route("/api/tracks/enrich", methods=["POST"])
def api_enrich_tracks():
    data = request.get_json() or {}
    tracks = data.get("tracks") or []
    if not isinstance(tracks, list):
        return jsonify({"error": "tracks must be a list"}), 400

    enriched = []
    for track in tracks:
        if not isinstance(track, dict):
            continue
        enriched.append(enrich_track_for_ui(track))

    return jsonify({"tracks": enriched})

@app.route("/api/favorites", methods=["GET"])
def api_get_favorites():
    favs = load_json(FAVORITES_FILE, [])
    return jsonify(favs)

@app.route("/api/favorites/save", methods=["POST"])
def api_save_favorites():
    data = request.get_json() or []
    save_json(FAVORITES_FILE, data)
    return jsonify({"status": "ok"})


@app.route("/api/settings/search", methods=["GET"])
def api_get_search_settings():
    return jsonify(load_search_settings())


@app.route("/api/settings/search", methods=["POST"])
def api_save_search_settings():
    data = request.get_json() or {}
    normalized = normalize_search_settings(data)
    save_json(SEARCH_SETTINGS_FILE, normalized)
    return jsonify(normalized)


@app.route("/api/settings/journal", methods=["GET"])
def api_get_settings_journal():
    try:
        requested_lines = int(request.args.get("lines", MAX_SETTINGS_JOURNAL_LINES))
    except (TypeError, ValueError):
        requested_lines = MAX_SETTINGS_JOURNAL_LINES

    line_limit = max(1, min(requested_lines, MAX_SETTINGS_JOURNAL_LINES))

    try:
        result = subprocess.run(
            [
                "journalctl",
                "-u",
                APOO_SERVICE_NAME,
                "-n",
                str(line_limit),
                "--no-pager",
                "--output=short-iso",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timed out while reading the service journal."}), 504

    if result.returncode != 0 and not result.stdout.strip():
        return jsonify({
            "error": result.stderr.strip() or "Failed to read the service journal.",
            "service": APOO_SERVICE_NAME,
        }), 500

    lines = result.stdout.splitlines()[-line_limit:]
    return jsonify({
        "service": APOO_SERVICE_NAME,
        "lines": lines,
        "line_count": len(lines),
        "max_lines": MAX_SETTINGS_JOURNAL_LINES,
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })


@app.route("/api/favorites/add", methods=["POST"])
def add_favorite():
    data = request.get_json() or {}
    track_id = data.get("id")
    title = data.get("title")
    artist = data.get("artist")
    if not all([track_id, title, artist]):
        return jsonify({"error": "Missing track info"}), 400

    favorites = []
    if os.path.exists(FAV_FILE):
        with open(FAV_FILE, "r") as f:
            favorites = json.load(f)

    if track_id not in [f["id"] for f in favorites]:
        favorites.append({"id": track_id, "title": title, "artist": artist})
        with open(FAV_FILE, "w") as f:
            json.dump(favorites, f)

    return jsonify({"status": "ok"})

# --- API: queue & state ---
@app.route("/api/queue", methods=["GET"])
def get_queue():
    return jsonify(player.get_state())


def normalize(s: str) -> str:
    """Normalize strings for loose matching."""
    if not s:
        return ""
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s


def find_local_track(title: str, artist: str, track_id: str) -> Optional[str]:
    title_n = normalize(title)
    artist_n = normalize(artist)
    id_n = normalize(track_id)

    # STRICT MATCH FIRST: "Artist - Title.ext"
    for root, _, files in os.walk(MUSIC_DIR):
        for f in files:
            name, ext = os.path.splitext(f)
            if ext.lower() not in [".mp3", ".wav", ".flac", ".m4a", ".ogg"]:
                continue

            # Split "Artist - Title"
            if " - " in name:
                f_artist, f_title = name.split(" - ", 1)
                if normalize(f_artist) == artist_n and normalize(f_title) == title_n:
                    return os.path.join(root, f)

    # SECOND: match both artist + title anywhere in filename
    for root, _, files in os.walk(MUSIC_DIR):
        for f in files:
            fname_n = normalize(f)
            if artist_n in fname_n and title_n in fname_n:
                return os.path.join(root, f)

    # THIRD: match by track_id
    if id_n:
        for root, _, files in os.walk(MUSIC_DIR):
            for f in files:
                if id_n in normalize(f):
                    return os.path.join(root, f)

    return None

@app.route("/media/by-path", methods=["GET"])
def media_by_path():
    """Serve audio files by absolute path, restricted to MUSIC_DIR.
    Uses conditional responses to support Range requests for streaming.
    """
    path = request.args.get("path")
    if not path:
        return jsonify({"error": "missing path"}), 400
    try:
        # Resolve and restrict to MUSIC_DIR
        real = os.path.realpath(path)
        music_root = os.path.realpath(MUSIC_DIR)
        if not real.startswith(music_root):
            return jsonify({"error": "forbidden"}), 403
        if not os.path.exists(real):
            return jsonify({"error": "not found"}), 404
        # conditional=True enables Range (partial content) for audio/video
        return send_file(real, conditional=True)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/artwork/by-path", methods=["GET"])
def artwork_by_path():
    """Serve embedded artwork for local audio files restricted to MUSIC_DIR."""
    path = request.args.get("path")
    if not path:
        return jsonify({"error": "missing path"}), 400

    try:
        real = os.path.realpath(path)
        music_root = os.path.realpath(MUSIC_DIR)
        if not real.startswith(music_root):
            return jsonify({"error": "forbidden"}), 403
        if not os.path.exists(real):
            return jsonify({"error": "not found"}), 404

        artwork_data, mime = get_local_track_artwork(real)
        if not artwork_data:
            return jsonify({"error": "no artwork"}), 404

        return Response(artwork_data, mimetype=mime or "image/jpeg")
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/seek", methods=["POST"])
def api_seek():
    data = request.get_json() or {}
    position = data.get("position")

    print("\n--- /api/seek called ---")
    print("Incoming JSON:", data)
    print("Requested position:", position)

    if not player.current:
        print("Seek failed: No current track")
        return jsonify({"error": "No current track"}), 400

    if position is None:
        print("Seek failed: No position provided")
        return jsonify({"error": "Missing position"}), 400

    try:
        pos = float(position)
    except Exception as e:
        print("Seek failed: Invalid position:", e)
        return jsonify({"error": "Invalid position"}), 400

    with player.lock:
        print(f"Seeking to {pos} seconds...")
        try:
            pygame.mixer.music.play(start=pos)
            player.current_start_ts = time.time() - pos
            print("Seek successful. Updated start timestamp:", player.current_start_ts)
        except Exception as e:
            print("Seek failed during pygame.mixer.music.play:", e)
            return jsonify({"error": "Seek failed"}), 500

    print("--- /api/seek finished ---\n")
    return jsonify({"status": "ok"})


@app.route("/api/queue/add", methods=["POST"])
def add_to_queue():
    data = request.get_json() or {}

    track_id = data.get("id")
    title = data.get("title", "Unknown title")
    artist = data.get("artist", "Unknown artist")
    deezer_id = data.get("deezer_id")
    spotify_id = data.get("spotify_id")
    artwork_url = data.get("artwork_url") or data.get("album_art") or data.get("thumbnail")
    source_id = deezer_id or spotify_id

    # Try to find local file
    path = find_local_track(title, artist, track_id or source_id or "")

    # If not found → download from YouTube
    if not path:
        print("Local file not found, downloading from YouTube…")
        # Add placeholder to queue immediately
        placeholder_id = track_id or sanitize_filename(f"{artist} - {title}")
        placeholder_track = Track(
            id=placeholder_id,
            title=title + " (Downloading)",
            artist=artist,
            path=None,
            deezer_id=deezer_id,
            spotify_id=spotify_id,
            artwork_url=artwork_url,
        )
        placeholder_track._downloading = True
        player.add_to_queue(placeholder_track)

        # Get metadata from provider API if we have a track id
        album = None
        year = None
        album_art_url = None
        album, year, album_art_url = get_track_metadata_for_download(title, artist, deezer_id)

        def download_and_replace():
            real_path = download_missing_song_from_youtube(title, artist, source_id, album, year, album_art_url)
            if real_path:
                with player.lock:
                    for idx, t in enumerate(player.queue):
                        if getattr(t, "_downloading", False) and t.id == placeholder_id:
                            player.queue[idx] = Track(
                                id=placeholder_id,
                                title=title,
                                artist=artist,
                                path=real_path,
                                deezer_id=deezer_id,
                                spotify_id=spotify_id,
                                artwork_url=artwork_url or album_art_url,
                            )
                            break
            else:
                # Remove placeholder if download failed
                with player.lock:
                    player.queue = [t for t in player.queue if not (getattr(t, "_downloading", False) and t.id == placeholder_id)]
        threading.Thread(target=download_and_replace, daemon=True).start()
        return jsonify({"status": "downloading"})

    # If found locally, add to queue as normal
    track = Track(
        id=track_id or os.path.basename(path),
        title=title,
        artist=artist,
        path=path,
        deezer_id=deezer_id,
        spotify_id=spotify_id,
        artwork_url=artwork_url,
    )
    player.add_to_queue(track)
    return jsonify({"status": "ok"})

@app.route("/api/queue/add_next", methods=["POST"])
def add_to_queue_next():
    data = request.get_json() or {}

    track_id = data.get("id")
    title = data.get("title", "Unknown title")
    artist = data.get("artist", "Unknown artist")
    deezer_id = data.get("deezer_id")
    spotify_id = data.get("spotify_id")
    artwork_url = data.get("artwork_url") or data.get("album_art") or data.get("thumbnail")
    source_id = deezer_id or spotify_id

    # Try to find local file
    path = find_local_track(title, artist, track_id or source_id or "")

    # If not found → download from YouTube
    if not path:
        # Add placeholder to queue immediately at the front
        placeholder_id = track_id or sanitize_filename(f"{artist} - {title}")
        placeholder_track = Track(
            id=placeholder_id,
            title=title + " (Downloading)",
            artist=artist,
            path=None,
            deezer_id=deezer_id,
            spotify_id=spotify_id,
            artwork_url=artwork_url,
        )
        placeholder_track._downloading = True
        with player.lock:
            if player.current is None:
                player.queue.insert(0, placeholder_track)
                player._play_next_locked()
            else:
                player.queue.insert(0, placeholder_track)

        # Get metadata from provider API if we have a track id
        album = None
        year = None
        album_art_url = None
        album, year, album_art_url = get_track_metadata_for_download(title, artist, deezer_id)

        def download_and_replace():
            real_path = download_missing_song_from_youtube(title, artist, source_id, album, year, album_art_url)
            if real_path:
                with player.lock:
                    for idx, t in enumerate(player.queue):
                        if getattr(t, "_downloading", False) and t.id == placeholder_id:
                            player.queue[idx] = Track(
                                id=placeholder_id,
                                title=title,
                                artist=artist,
                                path=real_path,
                                deezer_id=deezer_id,
                                spotify_id=spotify_id,
                                artwork_url=artwork_url or album_art_url,
                            )
                            break
            else:
                # Remove placeholder if download failed
                with player.lock:
                    player.queue = [t for t in player.queue if not (getattr(t, "_downloading", False) and t.id == placeholder_id)]
        threading.Thread(target=download_and_replace, daemon=True).start()
        return jsonify({"status": "downloading"})

    # If found locally, add to queue as normal
    track = Track(
        id=track_id or os.path.basename(path),
        title=title,
        artist=artist,
        path=path,
        deezer_id=deezer_id,
        spotify_id=spotify_id,
        artwork_url=artwork_url,
    )
    # Insert at front of queue
    with player.lock:
        if player.current is None:
            player.queue.insert(0, track)
            player._play_next_locked()
        else:
            player.queue.insert(0, track)
    return jsonify({"status": "ok"})

@app.route("/api/queue/reorder", methods=["POST"])
def api_queue_reorder():
    data = request.get_json() or {}
    from_idx = data.get("from")
    to_idx = data.get("to")
    if from_idx is None or to_idx is None:
        return jsonify({"error": "missing indices"}), 400
    with player.lock:
        n = len(player.queue)
        if not (0 <= from_idx < n and 0 <= to_idx < n):
            return jsonify({"error": "index out of range"}), 400
        item = player.queue.pop(from_idx)
        player.queue.insert(to_idx, item)
    return jsonify({"status": "ok"})

@app.route("/api/prev", methods=["POST"])
def api_prev():
    player.prev()  # you’ll need to implement this on Player using player.played
    return jsonify({"status": "ok"})

@app.route("/api/queue/remove", methods=["POST"])
def remove_from_queue():
    data = request.get_json() or {}
    track_id = data.get("id")
    if not track_id:
        return jsonify({"error": "Missing id"}), 400
    player.remove_from_queue(track_id)
    return jsonify({"status": "ok"})

@app.route("/api/track/delete", methods=["POST"])
def api_delete_track():
    """Permanently delete a local track file. Restricted to MUSIC_DIR."""
    data = request.get_json() or {}
    path = data.get("path")
    if not path:
        return jsonify({"error": "path required"}), 400

    real = os.path.realpath(path)
    music_real = os.path.realpath(MUSIC_DIR)
    if not real.startswith(music_real + os.sep) and real != music_real:
        return jsonify({"error": "path not within music directory"}), 403

    if not os.path.isfile(real):
        return jsonify({"error": "file not found"}), 404

    # Remove from player state so it doesn't keep playing
    with player.lock:
        if player.current and os.path.realpath(player.current.path or "") == real:
            player.current = None
            player.current_start_ts = None
            player.current_duration = 0.0
            if player.playback_mode == "host":
                import pygame
                pygame.mixer.music.stop()
        player.queue = [t for t in player.queue if os.path.realpath(t.path or "") != real]
        player.played = [t for t in player.played if os.path.realpath(t.path or "") != real]

    try:
        os.remove(real)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({"status": "ok"})


@app.route("/api/clear", methods=["POST"])
def api_clear():
    """Clear queue and played history, and stop playback."""
    player.clear_queue()
    return jsonify({"status": "ok"})


# --- API: controls ---
@app.route("/api/play", methods=["POST"])
def api_play():
    player.play()
    return jsonify({"status": "ok"})


@app.route("/api/pause", methods=["POST"])
def api_pause():
    player.pause()
    return jsonify({"status": "ok"})


@app.route("/api/next", methods=["POST"])
def api_next():
    player.next()
    return jsonify({"status": "ok"})


@app.route("/api/volume", methods=["POST"])
def api_volume():
    data = request.get_json() or {}
    vol = data.get("volume")
    if vol is None:
        return jsonify({"error": "Missing volume"}), 400
    player.set_volume(float(vol))
    return jsonify({"status": "ok"})

@app.route("/api/shuffle", methods=["POST"])
def api_shuffle():
    """Toggle shuffle mode."""
    data = request.get_json() or {}
    shuffle = data.get("shuffle")
    if shuffle is None:
        return jsonify({"error": "Missing shuffle"}), 400
    with player.lock:
        player.shuffle = bool(shuffle)
    return jsonify({"status": "ok", "shuffle": player.shuffle})

@app.route("/api/auto-fill", methods=["POST"])
def api_auto_fill():
    """Toggle auto-fill mode when queue is empty."""
    data = request.get_json() or {}
    enabled = data.get("enabled")
    if enabled is None:
        return jsonify({"error": "Missing enabled"}), 400
    with player.lock:
        player.auto_fill = bool(enabled)
        if player.auto_fill:
            player._ensure_queue_not_empty()
    return jsonify({"status": "ok", "auto_fill": player.auto_fill})

@app.route("/api/repeat", methods=["POST"])
def api_repeat():
    """Cycle repeat mode: off -> one -> all -> off."""
    with player.lock:
        if player.repeat == "off":
            player.repeat = "one"
        elif player.repeat == "one":
            player.repeat = "all"
        else:
            player.repeat = "off"
    return jsonify({"status": "ok", "repeat": player.repeat})

@app.route("/api/playback-mode", methods=["POST"])
def api_set_playback_mode():
    """Set playback mode: host or browser."""
    data = request.get_json() or {}
    mode = data.get("mode")
    
    if mode not in ["host", "browser"]:
        return jsonify({"error": "Invalid mode. Must be 'host' or 'browser'"}), 400
    
    with player.lock:
        old_mode = player.playback_mode
        player.playback_mode = mode
        
        # When switching modes, handle current playback
        if player.current:
            if mode == "host":
                # Switching to host: start pygame at browser's last position
                try:
                    pygame.mixer.music.load(player.current.path)
                    start_pos = player.browser_elapsed if player.browser_elapsed > 0 else 0
                    pygame.mixer.music.play(start=start_pos)
                    if player._is_paused:
                        pygame.mixer.music.pause()
                    player.current_start_ts = time.time() - start_pos
                except Exception as e:
                    print(f"Error switching to host mode: {e}")
            else:
                # Switching to browser: stop pygame
                try:
                    pygame.mixer.music.stop()
                except Exception as e:
                    print(f"Error stopping pygame: {e}")
        
        print(f"Playback mode changed: {old_mode} -> {mode}")
    
    return jsonify({"status": "ok", "playback_mode": mode})

@app.route("/api/browser/progress", methods=["POST"])
def api_browser_progress():
    """Receive progress updates from browser audio player."""
    data = request.get_json() or {}
    elapsed = data.get("elapsed")
    
    if elapsed is None:
        return jsonify({"error": "Missing elapsed"}), 400
    
    with player.lock:
        if player.playback_mode == "browser" and player.current:
            player.browser_elapsed = float(elapsed)
            player.browser_last_update = time.time()
    
    return jsonify({"status": "ok"})

@app.route("/api/browser/ended", methods=["POST"])
def api_browser_ended():
    """Browser reports that current track has ended."""
    with player.lock:
        if player.playback_mode == "browser" and player.current:
            print("Browser reported track ended, playing next...")
            player._play_next_locked()
    
    return jsonify({"status": "ok"})

@app.route("/api/search", methods=["GET"])
def api_search():
    q = request.args.get("q", "")
    if not q:
        return jsonify({"results": []})

    if is_deezer_quota_backoff_active():
        fallback_results = search_lastfm_tracks(q, limit=10)
        warning = None
        if not LASTFM_API_KEY:
            warning = "Deezer quota is active and LASTFM_API_KEY is not configured."
        elif not fallback_results:
            warning = "Deezer quota is active and Last.fm fallback returned no results."
        return jsonify({"results": fallback_results, "provider": "lastfm", "warning": warning})

    try:
        data = deezer_get("search", params={"q": q, "limit": 10})

        results = []
        for item in data.get("data", []):
            track_id = str(item.get("id"))
            title = item.get("title", "")
            artist = (item.get("artist") or {}).get("name", "")

            # You can later map provider metadata -> local file here
            local_path = None

            results.append({
                "id": track_id,
                "title": title,
                "artist": artist,
                "deezer_id": track_id,
                "path": local_path,
                "duration": item.get("duration"),
                "artwork_url": (item.get("album") or {}).get("cover_xl") or (item.get("album") or {}).get("cover_big") or (item.get("album") or {}).get("cover_medium"),
            })

        return jsonify({"results": results, "provider": "deezer"})
    except Exception as e:
        if _is_deezer_quota_error_message(str(e)) or is_deezer_quota_backoff_active():
            fallback_results = search_lastfm_tracks(q, limit=10)
            if LASTFM_API_KEY:
                warning = "Deezer quota exceeded; using Last.fm fallback."
            else:
                warning = "Deezer quota exceeded and LASTFM_API_KEY is not configured."
            return jsonify({"results": fallback_results, "provider": "lastfm", "warning": warning})

        print(f"Search failed for '{q}': {e}")
        return jsonify({"results": [], "error": "Search failed"}), 500

def _import_deezer_playlist_impl(url_or_id: str):
    """
    Import a Deezer playlist and return:
    {
        "name": "...",
        "tracks": [
            { "id": "...", "title": "...", "artist": "...", "deezer_id": "...", "path": null }
        ]
    }
    """
    playlist_id = parse_deezer_playlist_id(url_or_id)
    if not playlist_id:
        return jsonify({"error": "Invalid Deezer playlist URL or ID"}), 400

    try:
        meta = deezer_get(f"playlist/{playlist_id}")
        playlist_name = meta.get("title", "Imported Playlist")

        tracks = []
        for track in (meta.get("tracks") or {}).get("data", []):
            track_id = str(track.get("id"))
            title = track.get("title", "")
            artist = (track.get("artist") or {}).get("name", "")
            tracks.append({
                "id": track_id,
                "title": title,
                "artist": artist,
                "deezer_id": track_id,
                "path": None,
            })

        return jsonify({"name": playlist_name, "tracks": tracks})
    except Exception as e:
        print("Deezer import error:", e)
        return jsonify({"error": "Failed to import playlist"}), 500


@app.route("/api/spotify/import/status", methods=["GET"])
def api_spotify_import_status():
    progress_id = (request.args.get("progress_id") or "").strip()
    if not progress_id:
        return jsonify({"error": "Missing progress_id"}), 400

    payload = get_spotify_import_status(progress_id)
    if not payload:
        return jsonify({"error": "Import status not found", "progress_id": progress_id}), 404

    payload.pop("updated_ts", None)
    return jsonify(payload)


@app.route("/api/spotify/import", methods=["POST"])
def api_spotify_import():
    data = request.get_json() or {}
    url = data.get("url")
    if not url:
        return jsonify({"error": "Missing URL"}), 400
    progress_id = (data.get("progress_id") or "").strip() or None
    update_spotify_import_status(progress_id, state="running", stage="starting", message="Preparing Spotify import...", progress=0, collected_count=0, expected_total=None)
    return _import_spotify_playlist_impl(url, progress_id=progress_id)


import threading



# Progress tracking dict (persisted to disk)
LOFI_PROGRESS_FILE = os.path.join(DATA_DIR, "lofi_progress.json")
LOFI_IN_PROGRESS_FILE = os.path.join(DATA_DIR, "lofi_in_progress.json")
LOFI_LOG_FILE = os.path.join(DATA_DIR, "lofi_debug.log")
LOFI_TRACKING_LOCK = threading.Lock()

def _node_works(node_path: str) -> bool:
    try:
        result = subprocess.run([node_path, "--version"], capture_output=True, text=True, timeout=2)
        return result.returncode == 0
    except Exception:
        return False

def resolve_node_runtime() -> str:
    """Pick a working node path for yt-dlp EJS."""
    candidates = [
        os.environ.get("NODE_RUNTIME_PATH"),
        "/home/johnara/.nvm/versions/node/v14.21.3/bin/node",
        shutil.which("node"),
        "/usr/local/bin/node",
    ]
    for path in candidates:
        if path and _node_works(path):
            return path
    return "node"

NODE_RUNTIME_PATH = resolve_node_runtime()

def lofi_log(message: str) -> None:
    """Append a timestamped lofi log entry to disk for debugging."""
    try:
        ensure_dir_exists(LOFI_LOG_FILE)
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] {message}\n"
        with open(LOFI_LOG_FILE, "a") as f:
            f.write(line)
    except Exception:
        pass

def load_lofi_progress():
    if os.path.exists(LOFI_PROGRESS_FILE):
        try:
            with open(LOFI_PROGRESS_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_lofi_progress(progress):
    ensure_dir_exists(LOFI_PROGRESS_FILE)
    try:
        with open(LOFI_PROGRESS_FILE, "w") as f:
            json.dump(progress, f)
    except Exception as e:
        print(f"[LOFI] Failed to save progress file: {e}")
        lofi_log(f"save_progress_failed={e}")

def load_lofi_in_progress():
    if os.path.exists(LOFI_IN_PROGRESS_FILE):
        try:
            with open(LOFI_IN_PROGRESS_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_lofi_in_progress(in_progress):
    ensure_dir_exists(LOFI_IN_PROGRESS_FILE)
    try:
        with open(LOFI_IN_PROGRESS_FILE, "w") as f:
            json.dump(in_progress, f)
    except Exception as e:
        print(f"[LOFI] Failed to save in-progress file: {e}")
        lofi_log(f"save_in_progress_failed={e}")

LOFI_PROGRESS = load_lofi_progress()
LOFI_IN_PROGRESS = load_lofi_in_progress()


def _to_percent(value) -> int:
    try:
        return int(float(value))
    except Exception:
        return 0


def _remove_lofi_tracking_entries(progress_ids: list[str], reason: str = "") -> int:
    if not progress_ids:
        return 0

    removed_count = 0
    with LOFI_TRACKING_LOCK:
        for progress_id in progress_ids:
            removed = False
            if progress_id in LOFI_IN_PROGRESS:
                del LOFI_IN_PROGRESS[progress_id]
                removed = True
            if progress_id in LOFI_PROGRESS:
                del LOFI_PROGRESS[progress_id]
                removed = True
            if removed:
                removed_count += 1

        if removed_count:
            save_lofi_in_progress(LOFI_IN_PROGRESS)
            save_lofi_progress(LOFI_PROGRESS)

    if removed_count:
        lofi_log(f"tracking_removed count={removed_count} reason={reason or 'n/a'}")
    return removed_count


def cleanup_completed_lofi_tracking() -> int:
    """Prune completed/orphan tracking entries that can linger after restarts or thread errors."""
    now = time.time()
    completed_ids = []
    orphan_complete_ids = []

    with LOFI_TRACKING_LOCK:
        for progress_id, meta in list(LOFI_IN_PROGRESS.items()):
            meta = meta or {}
            safe_title = str(meta.get("safe_title") or "")
            started_ts = meta.get("started_ts", now)
            try:
                started_ts = float(started_ts)
            except Exception:
                started_ts = now

            percent = _to_percent(LOFI_PROGRESS.get(progress_id, 0))
            audio_path = os.path.join(LOFI_DIR, f"{safe_title}.mp3") if safe_title else ""
            audio_exists = bool(audio_path) and os.path.exists(audio_path)

            # Completed means we have progress=100 and an extracted audio file.
            # As a safety net, also clear very old 100% entries.
            if percent >= 100 and (audio_exists or (now - started_ts) > (30 * 60)):
                completed_ids.append(progress_id)

        for progress_id, percent in list(LOFI_PROGRESS.items()):
            if progress_id not in LOFI_IN_PROGRESS and _to_percent(percent) >= 100:
                orphan_complete_ids.append(progress_id)

        if completed_ids:
            for progress_id in completed_ids:
                LOFI_IN_PROGRESS.pop(progress_id, None)
                LOFI_PROGRESS.pop(progress_id, None)

        if orphan_complete_ids:
            for progress_id in orphan_complete_ids:
                LOFI_PROGRESS.pop(progress_id, None)

        if completed_ids or orphan_complete_ids:
            save_lofi_in_progress(LOFI_IN_PROGRESS)
            save_lofi_progress(LOFI_PROGRESS)

    if completed_ids or orphan_complete_ids:
        lofi_log(
            f"tracking_cleanup completed={len(completed_ids)} orphan_complete={len(orphan_complete_ids)}"
        )

    return len(completed_ids)

# Clean up stale in-progress entries on startup (>30 min old)
def cleanup_stale_lofi_downloads():
    """Remove in-progress entries older than 30 minutes."""
    current_time = time.time()
    timeout_seconds = 30 * 60  # 30 minutes
    stale_ids = []
    with LOFI_TRACKING_LOCK:
        for progress_id, meta in list(LOFI_IN_PROGRESS.items()):
            started_ts = meta.get("started_ts", current_time)
            try:
                started_ts = float(started_ts)
            except Exception:
                started_ts = current_time

            if current_time - started_ts > timeout_seconds:
                stale_ids.append(progress_id)

        if stale_ids:
            print(f"[LOFI] Cleaning up {len(stale_ids)} stale download(s) older than 30 min")
            for progress_id in stale_ids:
                LOFI_IN_PROGRESS.pop(progress_id, None)
                LOFI_PROGRESS.pop(progress_id, None)
            save_lofi_in_progress(LOFI_IN_PROGRESS)
            save_lofi_progress(LOFI_PROGRESS)

cleanup_completed_lofi_tracking()
cleanup_stale_lofi_downloads()

def _lofi_progress_hook(d, progress_id):
    if d['status'] == 'downloading':
        total = d.get('total_bytes') or d.get('total_bytes_estimate') or 1
        downloaded = d.get('downloaded_bytes', 0)
        percent = int(downloaded * 100 / total) if total else 0
        with LOFI_TRACKING_LOCK:
            LOFI_PROGRESS[progress_id] = percent
            save_lofi_progress(LOFI_PROGRESS)
    elif d['status'] == 'finished':
        with LOFI_TRACKING_LOCK:
            LOFI_PROGRESS[progress_id] = 100
            save_lofi_progress(LOFI_PROGRESS)

@app.route("/api/lofi/download", methods=["POST"])
def api_lofi_download():
    """Download a lofi video from YouTube and add to queue as (Downloading) until ready."""
    data = request.get_json() or {}
    url = data.get("url")
    if not url:
        return jsonify({"error": "Missing URL"}), 400
    try:
        print("="*60)
        print("LOFI DOWNLOAD START - URL:", url)
        print("="*60)
        lofi_log(f"download_start url={url}")
        # Get video metadata first
        print("Step 1: Fetching metadata...")
        info_command = [*resolve_yt_dlp_command(), "--js-runtimes", f"node:{NODE_RUNTIME_PATH}", "--dump-json", "--no-playlist", url]
        print("Command:", info_command)
        lofi_log(f"metadata_cmd={' '.join(info_command)}")
        info_result = subprocess.run(info_command, shell=False, capture_output=True, text=True)
        print(f"Return code: {info_result.returncode}")
        if info_result.stderr:
            lofi_log(f"metadata_stderr={info_result.stderr[:200].strip()}")
        video_id = url.split("v=")[-1].split("&")[0] if "v=" in url else url.split("/")[-1]
        title = video_id
        thumbnail = None
        if info_result.returncode == 0:
            try:
                info = json.loads(info_result.stdout)
                title = info.get("title", video_id)
                thumbnail = info.get("thumbnail")
                print(f"✓ Title: {title}")
                lofi_log(f"metadata_title={title}")
            except Exception as e:
                print(f"✗ Parse error: {e}")
                lofi_log(f"metadata_parse_error={e}")
        else:
            print(f"✗ Metadata failed: {info_result.stderr[:200]}")
            lofi_log("metadata_failed")
        safe_title = sanitize_filename(title)  # Sanitize
        output_template = os.path.join(LOFI_DIR, f"{safe_title}.%(ext)s")
        expected_file = os.path.join(LOFI_DIR, f"{safe_title}.mp4")
        print(f"Target file: {expected_file}")
        lofi_log(f"target_file={expected_file}")
        # Check existing
        if os.path.exists(expected_file):
            print("✓ Already exists!")
            print("="*60)
            lofi_log("already_exists")
            # Ensure audio file exists (MP3)
            audio_path = os.path.join(LOFI_DIR, f"{safe_title}.mp3")
            if not os.path.exists(audio_path):
                print(f"MP3 missing, extracting audio...")
                extract_audio_from_video(expected_file, audio_path)
            
            # Add to queue using audio file, not video file
            track = Track(
                id=safe_title,
                title=title,
                artist="Lofi",
                path=audio_path
            )
            player.add_to_queue(track)
            return jsonify({
                "status": "ok",
                "id": safe_title,
                "title": title,
                "path": expected_file,
                "thumbnail": thumbnail
            })
        # Download
        print(f"Step 3: Downloading...")
        lofi_log("download_step_start")
        strategies = [
            ["--extractor-args", "youtube:player_client=default,tv_simply"],
            ["--extractor-args", "youtube:player_client=web"],
            ["--extractor-args", "youtube:player_client=ios"],
            [],
            ["--extractor-args", "youtube:player_client=android"],
        ]
        progress_id = f"{safe_title}_{int(time.time())}"
        with LOFI_TRACKING_LOCK:
            LOFI_PROGRESS[progress_id] = 0
            LOFI_IN_PROGRESS[progress_id] = {
                "title": title,
                "safe_title": safe_title,
                "thumbnail": thumbnail,
                "started_ts": time.time(),
            }
            save_lofi_progress(LOFI_PROGRESS)
            save_lofi_in_progress(LOFI_IN_PROGRESS)
        lofi_log(f"progress_id={progress_id}")
        # Do not add placeholder to queue for lofi downloads
        def run_download():
            success = False
            try:
                # Acquire semaphore to ensure sequential downloads
                with DOWNLOAD_SEMAPHORE:
                    # Format priority: prefer formats that don't require ffmpeg merge to avoid Raspberry Pi timeout issues
                    formats = [
                        "bestvideo[height>=1080]+bestaudio/best[height>=0180]/bestvideo+bestaudio/best",
                    ]

                    for i, strategy in enumerate(strategies):
                        print(f"Strategy {i+1}/{len(strategies)}...")
                        lofi_log(f"strategy_start index={i+1}")
                        for fmt_idx, fmt in enumerate(formats):
                            print(f"  Format {fmt_idx+1}/{len(formats)}: {fmt}")
                            lofi_log(f"format_try index={fmt_idx+1} fmt={fmt}")
                            # Build yt-dlp command
                            yt_dlp_prefix = resolve_yt_dlp_command()
                            cmd = [
                                *yt_dlp_prefix,
                                *strategy,
                                "--js-runtimes", f"node:{NODE_RUNTIME_PATH}",
                                "--newline",
                                "--progress-template",
                                "%(progress._percent_str)s %(progress.downloaded_bytes)s/%(progress.total_bytes)s",
                                "--format", fmt,
                                "--merge-output-format", "mp4",
                                "--no-playlist",
                                "--no-check-certificate",
                                "-o", output_template,
                                url
                            ]
                            print("Running:", " ".join(cmd))
                            lofi_log(f"download_cmd={' '.join(cmd)}")
                            try:
                                proc = subprocess.Popen(cmd, shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
                                stdout_lines = []
                                stderr_lines = []

                                for line in proc.stdout:
                                    stdout_lines.append(line)
                                    # Try to parse percent from line like '  12.3% 123456/1234567'
                                    line = line.strip()
                                    if line.endswith("%") or "%" in line:
                                        try:
                                            percent_str = line.split("%", 1)[0].strip()
                                            percent = float(percent_str)
                                            with LOFI_TRACKING_LOCK:
                                                LOFI_PROGRESS[progress_id] = int(percent)
                                        except Exception:
                                            pass
                                    # Log format selection info
                                    if "Downloading" in line or "Selected" in line or "format" in line.lower():
                                        print(f"[yt-dlp] {line.strip()}")
                                        lofi_log(f"yt_dlp_stdout={line.strip()}")

                                stderr = proc.stderr.read()
                                if stderr:
                                    stderr_lines = stderr.split('\n')
                                    for line in stderr_lines:
                                        if line.strip():
                                            print(f"[yt-dlp stderr] {line}")
                                            lofi_log(f"yt_dlp_stderr={line.strip()}")

                                proc.wait()
                                lofi_log(f"yt_dlp_exit={proc.returncode}")

                                if os.path.exists(expected_file):
                                    success = True
                                    # Use ffprobe to check resolution
                                    try:
                                        probe_cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "csv=p=0", expected_file]
                                        probe_result = subprocess.run(probe_cmd, capture_output=True, text=True)
                                        if probe_result.returncode == 0:
                                            resolution = probe_result.stdout.strip()
                                            print(f"✓ Downloaded! Resolution: {resolution}")
                                            lofi_log(f"downloaded_resolution={resolution}")
                                        else:
                                            print(f"✓ Downloaded!")
                                            lofi_log("downloaded_no_resolution")
                                    except Exception as e:
                                        print(f"✓ Downloaded! (could not check resolution: {e})")
                                        lofi_log(f"resolution_error={e}")
                                    break  # Success! Exit format loop
                                else:
                                    print(f"✗ Format {fmt_idx+1} failed, trying next...")
                                    lofi_log(f"format_failed index={fmt_idx+1}")
                            except Exception as e:
                                print(f"yt-dlp error: {e}")
                                lofi_log(f"yt_dlp_error={e}")

                        if success:
                            break  # Exit strategy loop if successful

                # Extract audio AFTER releasing semaphore to prevent deadlock
                if success:
                    print(f"Extracting audio for {safe_title}...")
                    lofi_log("extract_audio_start")
                    with LOFI_TRACKING_LOCK:
                        LOFI_PROGRESS[progress_id] = 100  # Mark complete
                        save_lofi_progress(LOFI_PROGRESS)
                    audio_path = os.path.join(LOFI_DIR, f"{safe_title}.mp3")
                    extract_audio_from_video(expected_file, audio_path)
                    print(f"✓ Download complete: {safe_title}")
                    lofi_log("download_complete")
                else:
                    print(f"✗ Download failed: {safe_title}")
                    lofi_log("download_failed")
            except Exception as e:
                print(f"[LOFI] Download thread error for {safe_title}: {e}")
                lofi_log(f"download_thread_error={e}")
            finally:
                cleanup_completed_lofi_tracking()
                _remove_lofi_tracking_entries([progress_id], reason="download-thread-exit")
        threading.Thread(target=run_download, daemon=True).start()
        return jsonify({
            "status": "downloading",
            "id": safe_title,
            "title": title,
            "progress_id": progress_id,
            "thumbnail": thumbnail
        })
    except Exception as e:
        print("="*60)
        print("EXCEPTION:")
        print(traceback.format_exc())
        print("="*60)
        return jsonify({"error": str(e)}), 500

# --- API: lofi in-progress downloads ---
@app.route("/api/lofi/in-progress", methods=["GET"])
def api_lofi_in_progress():
    """Return all in-progress lofi downloads with progress."""
    cleanup_completed_lofi_tracking()
    result = []
    with LOFI_TRACKING_LOCK:
        for progress_id, meta in LOFI_IN_PROGRESS.items():
            percent = LOFI_PROGRESS.get(progress_id, 0)
            result.append({
                "progress_id": progress_id,
                "title": meta.get("title", progress_id),
                "safe_title": meta.get("safe_title", progress_id),
                "thumbnail": meta.get("thumbnail"),
                "progress": percent,
                "started_ts": meta.get("started_ts"),
            })
    return jsonify({"in_progress": result})

@app.route("/api/lofi/clear-in-progress", methods=["POST"])
def api_lofi_clear_in_progress():
    """Clear all in-progress lofi downloads and their progress entries."""
    try:
        with LOFI_TRACKING_LOCK:
            cleared_in_progress = len(LOFI_IN_PROGRESS)
            cleared_progress = len(LOFI_PROGRESS)
            LOFI_IN_PROGRESS.clear()
            LOFI_PROGRESS.clear()
            save_lofi_in_progress(LOFI_IN_PROGRESS)
            save_lofi_progress(LOFI_PROGRESS)
        lofi_log("clear_in_progress")
        return jsonify({
            "status": "ok",
            "cleared_in_progress": cleared_in_progress,
            "cleared_progress": cleared_progress,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/lofi/list", methods=["GET"])
def api_lofi_list():
    """Get list of downloaded lofi videos."""
    videos = []
    
    if os.path.exists(LOFI_DIR):
        for filename in os.listdir(LOFI_DIR):
            if filename.endswith(('.mp4', '.webm', '.mkv', '.mov', '.avi')):
                video_id, ext = os.path.splitext(filename)
                path = os.path.join(LOFI_DIR, filename)
                videos.append({
                    "id": video_id,
                    "title": video_id,  # Could be enhanced to store metadata
                    "path": path,
                    "filename": filename,
                    "ext": ext[1:]  # without dot
                })
    
    return jsonify({"videos": videos})


@app.route("/api/lofi/video/<video_id>", methods=["GET"])
def api_lofi_video(video_id):
    """Serve a lofi video file."""
    try:
        video_path = os.path.join(LOFI_DIR, f"{video_id}.mp4")
        
        if not os.path.exists(video_path):
            return jsonify({"error": "Video not found"}), 404
        
        # Serve video with range support for seeking
        return send_file(video_path, mimetype="video/mp4", conditional=True)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/lofi/delete/<video_id>", methods=["DELETE"])
def api_lofi_delete(video_id):
    """Delete a lofi video."""
    try:
        video_path = os.path.join(LOFI_DIR, f"{video_id}.mp4")
        
        if not os.path.exists(video_path):
            return jsonify({"error": "Video not found"}), 404
        
        os.remove(video_path)
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/lofi/play-on-device", methods=["POST"])
def api_lofi_play_on_device():
    """Add a specific lofi video to queue (audio only)."""
    data = request.get_json() or {}
    video_id = data.get("video_id")
    
    if not video_id:
        return jsonify({"error": "video_id required"}), 400
    
    try:
        # Find the video file
        video_path = None
        video_title = None
        
        if os.path.exists(LOFI_DIR):
            for f in os.listdir(LOFI_DIR):
                if f.startswith(video_id) and (f.endswith(".mp4") or f.endswith(".webm") or f.endswith(".mkv")):
                    video_path = os.path.join(LOFI_DIR, f)
                    # Extract title from filename (format: video_id - title.ext)
                    video_title = f.split(" - ", 1)[1].rsplit(".", 1)[0] if " - " in f else video_id
                    break
        
        if not video_path or not os.path.exists(video_path):
            return jsonify({"error": "Video not found"}), 404
        
        # Determine audio path (replace extension with .mp3)
        base_path = os.path.splitext(video_path)[0]
        audio_path = base_path + ".mp3"
        
        # Ensure audio exists; if missing, extract synchronously
        if not os.path.exists(audio_path):
            print(f"[LOFI] Audio missing, extracting: {audio_path}")
            ok = extract_audio_from_video(video_path, audio_path)
            if not ok or not os.path.exists(audio_path):
                return jsonify({"error": f"Could not extract audio for '{video_title}'"}), 500
        
        # Add to queue using extracted audio
        track = Track(
            id=video_id,
            title=video_title,
            artist="Lofi",
            path=audio_path,
        )
        player.add_to_queue(track)
        
        return jsonify({
            "status": "ok",
            "video_id": video_id,
            "title": video_title,
            "message": "Lofi audio queued for playback"
        })
    
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/remote/lofi-random", methods=["POST"])
def api_remote_lofi_random():
    """
    Play a random lofi video with audio output on SERVER ONLY (not browser).
    
    REQUEST FORMAT (for Raspberry Pi or other devices):
    ========================================
    Method: POST
    URL: http://<server-ip>:7000/api/remote/lofi-random
    Headers: Content-Type: application/json
    Body: {} (empty JSON object, or omit body entirely)
    
    Example using curl:
        curl -X POST http://192.168.1.100:7000/api/remote/lofi-random \\
             -H "Content-Type: application/json" \\
             -d '{}'
    
    Example using Python requests:
        import requests
        response = requests.post('http://192.168.1.100:7000/api/remote/lofi-random')
        print(response.json())
    
    Example using Node.js/JavaScript:
        fetch('http://192.168.1.100:7000/api/remote/lofi-random', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' }
        }).then(r => r.json()).then(console.log)
    ========================================
    
    Response:
        - Success: {"status": "ok", "video_id": "xxx", "title": "xxx"}
        - Error: {"error": "No lofi videos available"} or other error message
    """
    try:
        # Get list of all lofi videos
        lofi_files = []
        if os.path.exists(LOFI_DIR):
            lofi_files = [f[:-4] for f in os.listdir(LOFI_DIR) if f.endswith(".mp4")]
        
        if not lofi_files:
            return jsonify({"error": "No lofi videos available"}), 400

        # Pick random lofi
        video_id = random.choice(lofi_files)
        video_path = os.path.join(LOFI_DIR, f"{video_id}.mp4")
        audio_path = os.path.join(LOFI_DIR, f"{video_id}.mp3")

        # Check if file exists
        if not os.path.exists(video_path):
            return jsonify({"error": f"Lofi video '{video_id}' file not found"}), 404

        # Ensure audio exists; if missing, extract synchronously so we only queue when playable
        if not os.path.exists(audio_path):
            print(f"[LOFI] Audio missing, extracting: {audio_path}")
            ok = extract_audio_from_video(video_path, audio_path)
            if not ok or not os.path.exists(audio_path):
                return jsonify({"error": f"Could not extract audio for '{video_id}'"}), 500

        # Use existing audio file for playback
        track = Track(
            id=video_id,
            title=video_id,
            artist="Lofi",
            path=audio_path,
        )
        player.add_to_queue(track)
        return jsonify({
            "status": "ok",
            "video_id": video_id,
            "title": video_id,
            "message": "Random lofi audio queued for playback"
        })
    
    except Exception as e:
        print("Remote lofi error:", e)
        import traceback
        print(traceback.format_exc())
        return jsonify({"error": str(e)}), 500

@app.route("/api/remote/playlist-random", methods=["POST"])
def api_remote_playlist_random():
    """
    Add a random playlist to the queue (excluding "Epic Concentration").
    Plays the entire playlist on SERVER OUTPUT.
    
    REQUEST FORMAT (for Raspberry Pi or other devices):
    ========================================
    Method: POST
    URL: http://<server-ip>:7000/api/remote/playlist-random
    Headers: Content-Type: application/json
    Body: {} (empty JSON object, or omit body entirely)
    
    Example using curl:
        curl -X POST http://192.168.1.100:7000/api/remote/playlist-random \\
             -H "Content-Type: application/json" \\
             -d '{}'
    
    Example using Python requests:
        import requests
        response = requests.post('http://192.168.1.100:7000/api/remote/playlist-random')
        print(response.json())
    
    Example using Node.js/JavaScript:
        fetch('http://192.168.1.100:7000/api/remote/playlist-random', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' }
        }).then(r => r.json()).then(console.log)
    
    Example using Bash script on Raspberry Pi:
        #!/bin/bash
        SERVER_IP="192.168.1.100"
        curl -X POST http://$SERVER_IP:7000/api/remote/playlist-random \\
             -H "Content-Type: application/json" \\
             -d '{}'
    ========================================
    
    Response:
        - Success: {"status": "ok", "playlist_name": "xxx", "tracks_added": N}
        - Error: {"error": "No playlists available"} or other error message
    """
    try:
        # Load all playlists
        playlists = load_json(PLAYLISTS_FILE, {})
        
        if not playlists:
            return jsonify({"error": "No playlists available"}), 400
        
        # Filter out excluded playlists
        EXCLUDED_PLAYLISTS = ["Epic Concentration", "Board Games and Chill"]
        available_playlists = {k: v for k, v in playlists.items() if k not in EXCLUDED_PLAYLISTS}
        
        if not available_playlists:
            return jsonify({"error": "No playlists available (Epic Concentration excluded)"}), 400
        
        # Pick playlist based on time rules, otherwise random
        now = datetime.now()
        now_time = now.time()
        morning_start = dt_time(7, 0)
        morning_end = dt_time(12, 30)

        playlist_name = None
        if now.weekday() == 3 and now_time >= dt_time(20, 0):
            playlist_name = "Board Game Night"
        elif morning_start <= now_time <= morning_end:
            playlist_name = "Wake Up Happy 🥞 Morning Playlist"

        if not playlist_name or playlist_name not in playlists:
            playlist_name = random.choice(list(available_playlists.keys()))
        tracks = available_playlists[playlist_name]
        
        if not tracks:
            return jsonify({"error": f"Playlist '{playlist_name}' is empty"}), 400
        
        # Clear queue before adding this playlist
        player.clear_queue()

        # Add all tracks to queue (download if path invalid)
        added_count = 0
        for track_data in tracks:
            # Check if path exists
            path = track_data.get("path")
            if path and os.path.exists(path):
                # Path is valid, use it directly
                track = Track(
                    id=track_data.get("id", ""),
                    title=track_data.get("title", "Unknown"),
                    artist=track_data.get("artist", "Unknown"),
                    path=path,
                )
                player.add_to_queue(track)
                added_count += 1
            else:
                # Path is invalid, try to find locally or download
                title = track_data.get("title", "Unknown")
                artist = track_data.get("artist", "Unknown")
                source_id = track_data.get("deezer_id") or track_data.get("spotify_id")
                
                # Try to find locally
                local_path = find_local_track(title, artist, track_data.get("id", ""))
                if local_path:
                    track = Track(
                        id=track_data.get("id", ""),
                        title=title,
                        artist=artist,
                        path=local_path,
                    )
                    player.add_to_queue(track)
                    added_count += 1
                else:
                    # Not found locally, add as placeholder and download in background
                    placeholder_id = track_data.get("id", "") or sanitize_filename(f"{artist} - {title}")
                    placeholder_track = Track(
                        id=placeholder_id,
                        title=title + " (Downloading)",
                        artist=artist,
                        path=None
                    )
                    placeholder_track._downloading = True
                    player.add_to_queue(placeholder_track)
                    added_count += 1
                    
                    # Download in background
                    def download_missing(t, a, sid):
                        download_missing_song_from_youtube(t, a, sid)
                    threading.Thread(target=download_missing, args=(title, artist, source_id), daemon=True).start()
        
        if added_count == 0:
            return jsonify({"error": f"Could not load any tracks from playlist '{playlist_name}'"}), 400
        
        # Enable shuffle mode for random playlist
        with player.lock:
            player.shuffle = True
        
        return jsonify({
            "status": "ok",
            "playlist_name": playlist_name,
            "tracks_added": added_count,
            "shuffle_enabled": True,
            "message": f"Added {added_count} tracks from '{playlist_name}' to queue (shuffle enabled)"
        })
    
    except Exception as e:
        print("Remote playlist error:", e)
        import traceback
        print(traceback.format_exc())
        return jsonify({"error": str(e)}), 500

@app.route("/api/lofi/progress", methods=["GET"])
def api_lofi_progress():
    progress_id = request.args.get("progress_id")
    if not progress_id:
        return jsonify({"progress": 0})
    with LOFI_TRACKING_LOCK:
        percent = LOFI_PROGRESS.get(progress_id, 0)
    return jsonify({"progress": percent})

@app.route("/api/lofi/debug/resolutions", methods=["GET"])
def api_lofi_debug_resolutions():
    """DEBUG: Check resolution of all downloaded lofi videos."""
    videos = []
    
    if os.path.exists(LOFI_DIR):
        for filename in os.listdir(LOFI_DIR):
            if filename.endswith((".mp4", ".webm", ".mkv", ".mov", ".avi")):
                video_id, ext = os.path.splitext(filename)
                path = os.path.join(LOFI_DIR, filename)
                resolution = get_video_resolution(path)
                file_size = os.path.getsize(path) / (1024*1024)  # MB
                videos.append({
                    "filename": filename,
                    "resolution": resolution,
                    "size_mb": round(file_size, 2),
                    "path": path
                })
    
    return jsonify({
        "lofi_dir": LOFI_DIR,
        "count": len(videos),
        "videos": videos
    })

if __name__ == "__main__":
    debug_mode = os.getenv("APOO_DEBUG", "0").strip().lower() in {"1", "true", "yes", "on"}
    app.run(host="0.0.0.0", port=7000, debug=debug_mode)
