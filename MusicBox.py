# app.py
import json
import os
import random
import re
import shutil
import time
from datetime import datetime, time as dt_time
import threading
import traceback
from dataclasses import dataclass, asdict
from typing import List, Optional
import requests
import socket
from flask import Flask, jsonify, request, render_template, send_file
from config import DEEZER_API_BASE
import zipfile
from io import BytesIO

import pygame  # make sure it's installed; on Raspberry Pi it's usually available
import subprocess
from mutagen import File as MutagenFile
from mutagen.id3 import ID3, TIT2, TPE1, TALB, TDRC, TPE2, APIC
from mutagen.mp3 import MP3

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
PLAYLISTS_FILE = os.path.join(DATA_DIR, "playlists.json")
FAVORITES_FILE = os.path.join(DATA_DIR, "favorites.json")

# Semaphore to limit ffmpeg/yt-dlp downloads to 1 at a time (important for Raspberry Pi)
DOWNLOAD_SEMAPHORE = threading.Semaphore(1)

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
    cmd = " ".join(command_list)
    print("Running:", cmd)
    subprocess.run(cmd, shell=True)

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
        raise ValueError(message)
    return data


def get_deezer_track_metadata(track_id: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Return album, year, and album art URL for a Deezer track id."""
    if not track_id:
        return None, None, None

    try:
        track_data = deezer_get(f"track/{track_id}")
        album = (track_data.get("album") or {}).get("title")
        release_date = track_data.get("release_date") or ""
        year = release_date[:4] if release_date else None
        album_art_url = (track_data.get("album") or {}).get("cover_xl") or (track_data.get("album") or {}).get("cover_big")
        return album, year, album_art_url
    except Exception as e:
        print(f"Failed to fetch Deezer metadata: {e}")
        return None, None, None


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
        return final_mp3

    # Acquire semaphore to ensure only 1 download runs at a time
    with DOWNLOAD_SEMAPHORE:
        # Try top 5 results with multiple strategies
        for i in range(1, 6):
            query = f"ytsearch{i}:{title} {artist}"
            print(f"Trying YouTube result {i}: {query}")

            # Try different extractor strategies to bypass bot detection
            strategies = [
                ["--extractor-args", "youtube:player_client=android"],
                ["--extractor-args", "youtube:player_client=ios"],
                ["--extractor-args", "youtube:player_client=web"],
                [],  # Fallback: no extra args
            ]
            
            for idx, extra_args in enumerate(strategies):
                # Download AUDIO ONLY for music (not video like lofi)
                audio_template = f"{safe_artist} - {safe_title}.%(ext)s"
                command = [
                    "yt-dlp",
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
            
            # If we successfully downloaded, break out of result loop
            if os.path.exists(final_mp3):
                print("Downloaded:", final_mp3)
                
                # Add metadata to the MP3 file
                try:
                    audio = MP3(final_mp3, ID3=ID3)
                    
                    # Add ID3 tag if it doesn't exist
                    try:
                        audio.add_tags()
                    except:
                        pass
                    
                    # Set metadata
                    audio.tags["TIT2"] = TIT2(encoding=3, text=title)  # Title
                    audio.tags["TPE1"] = TPE1(encoding=3, text=artist)  # Artist
                    
                    if album:
                        audio.tags["TALB"] = TALB(encoding=3, text=album)  # Album
                        audio.tags["TPE2"] = TPE2(encoding=3, text=artist)  # Album Artist
                    
                    if year:
                        audio.tags["TDRC"] = TDRC(encoding=3, text=year)  # Year
                    
                    # Download and embed album art if available
                    if album_art_url:
                        try:
                            art_response = requests.get(album_art_url, timeout=10)
                            if art_response.status_code == 200:
                                audio.tags["APIC"] = APIC(
                                    encoding=3,
                                    mime='image/jpeg',
                                    type=3,  # Cover (front)
                                    desc='Cover',
                                    data=art_response.content
                                )
                        except Exception as e:
                            print(f"Failed to download album art: {e}")
                    
                    audio.save()
                    print(f"✓ Added metadata to {final_mp3}")
                except Exception as e:
                    print(f"Failed to add metadata: {e}")
                
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

        # Set SDL audio driver for Linux compatibility
        os.environ["SDL_AUDIODRIVER"] = "alsa"  # Try "pulse" or "dsp" if needed
        # Explicitly set mixer parameters for best quality
        pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=4096)
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

        res = deezer_get("search", params={"q": f'artist:"{artist}"', "limit": 10})
        items = res.get("data", [])

        print(f"Deezer returned {len(items)} tracks for artist search '{artist}'")
        downloaded_any = False

        for idx, item in enumerate(items, start=1):
            title = item.get("title", "")
            artist_name = (item.get("artist") or {}).get("name", artist)
            source_id = str(item.get("id", ""))
            album = (item.get("album") or {}).get("title", "")
            release_date = item.get("release_date") or ""
            year = release_date[:4] if release_date else ""
            album_art_url = (item.get("album") or {}).get("cover_xl") or (item.get("album") or {}).get("cover_big")

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

@app.route("/api/favorites", methods=["GET"])
def api_get_favorites():
    favs = load_json(FAVORITES_FILE, [])
    return jsonify(favs)

@app.route("/api/favorites/save", methods=["POST"])
def api_save_favorites():
    data = request.get_json() or []
    save_json(FAVORITES_FILE, data)
    return jsonify({"status": "ok"})


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
    source_id = data.get("deezer_id") or data.get("spotify_id")

    # Try to find local file
    path = find_local_track(title, artist, track_id)

    # If not found → download from YouTube
    if not path:
        print("Local file not found, downloading from YouTube…")
        # Add placeholder to queue immediately
        placeholder_id = track_id or sanitize_filename(f"{artist} - {title}")
        placeholder_track = Track(
            id=placeholder_id,
            title=title + " (Downloading)",
            artist=artist,
            path=None
        )
        placeholder_track._downloading = True
        player.add_to_queue(placeholder_track)

        # Get metadata from provider API if we have a track id
        album = None
        year = None
        album_art_url = None
        if source_id:
            album, year, album_art_url = get_deezer_track_metadata(source_id)

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
                                path=real_path
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
    )
    player.add_to_queue(track)
    return jsonify({"status": "ok"})

@app.route("/api/queue/add_next", methods=["POST"])
def add_to_queue_next():
    data = request.get_json() or {}

    track_id = data.get("id")
    title = data.get("title", "Unknown title")
    artist = data.get("artist", "Unknown artist")
    source_id = data.get("deezer_id") or data.get("spotify_id")

    # Try to find local file
    path = find_local_track(title, artist, track_id)

    # If not found → download from YouTube
    if not path:
        # Add placeholder to queue immediately at the front
        placeholder_id = track_id or sanitize_filename(f"{artist} - {title}")
        placeholder_track = Track(
            id=placeholder_id,
            title=title + " (Downloading)",
            artist=artist,
            path=None
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
        if source_id:
            album, year, album_art_url = get_deezer_track_metadata(source_id)

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
                                path=real_path
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
        })

    return jsonify({"results": results})

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


@app.route("/api/deezer/import", methods=["POST"])
def api_deezer_import():
    data = request.get_json() or {}
    url = data.get("url")
    if not url:
        return jsonify({"error": "Missing URL"}), 400
    return _import_deezer_playlist_impl(url)


@app.route("/api/spotify/import", methods=["POST"])
def api_spotify_import_alias():
    """Legacy route kept for backward compatibility. Uses Deezer now."""
    data = request.get_json() or {}
    url = data.get("url")
    if not url:
        return jsonify({"error": "Missing URL"}), 400
    return _import_deezer_playlist_impl(url)


import threading



# Progress tracking dict (persisted to disk)
LOFI_PROGRESS_FILE = os.path.join(DATA_DIR, "lofi_progress.json")
LOFI_IN_PROGRESS_FILE = os.path.join(DATA_DIR, "lofi_in_progress.json")
LOFI_LOG_FILE = os.path.join(DATA_DIR, "lofi_debug.log")

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
    except Exception:
        pass

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
    except Exception:
        pass

LOFI_PROGRESS = load_lofi_progress()
LOFI_IN_PROGRESS = load_lofi_in_progress()

# Clean up stale in-progress entries on startup (>30 min old)
def cleanup_stale_lofi_downloads():
    """Remove in-progress entries older than 30 minutes."""
    current_time = time.time()
    timeout_seconds = 30 * 60  # 30 minutes
    stale_ids = []
    
    for progress_id, meta in LOFI_IN_PROGRESS.items():
        started_ts = meta.get("started_ts", current_time)
        if current_time - started_ts > timeout_seconds:
            stale_ids.append(progress_id)
    
    if stale_ids:
        print(f"[LOFI] Cleaning up {len(stale_ids)} stale download(s) older than 30 min")
        for progress_id in stale_ids:
            del LOFI_IN_PROGRESS[progress_id]
            if progress_id in LOFI_PROGRESS:
                del LOFI_PROGRESS[progress_id]
        save_lofi_in_progress(LOFI_IN_PROGRESS)
        save_lofi_progress(LOFI_PROGRESS)

cleanup_stale_lofi_downloads()

def _lofi_progress_hook(d, progress_id):
    if d['status'] == 'downloading':
        total = d.get('total_bytes') or d.get('total_bytes_estimate') or 1
        downloaded = d.get('downloaded_bytes', 0)
        percent = int(downloaded * 100 / total) if total else 0
        LOFI_PROGRESS[progress_id] = percent
        save_lofi_progress(LOFI_PROGRESS)
    elif d['status'] == 'finished':
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
        info_command = ["yt-dlp", "--js-runtimes", f"node:{NODE_RUNTIME_PATH}", "--dump-json", "--no-playlist", url]
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
        LOFI_PROGRESS[progress_id] = 0
        save_lofi_progress(LOFI_PROGRESS)
        LOFI_IN_PROGRESS[progress_id] = {
            "title": title,
            "safe_title": safe_title,
            "thumbnail": thumbnail,
            "started_ts": time.time(),
        }
        save_lofi_in_progress(LOFI_IN_PROGRESS)
        lofi_log(f"progress_id={progress_id}")
        # Do not add placeholder to queue for lofi downloads
        def run_download():
            # Acquire semaphore to ensure sequential downloads
            with DOWNLOAD_SEMAPHORE:
                success = False
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
                        cmd = [
                            "yt-dlp",
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
                        if strategy:
                            cmd = ["yt-dlp", "--js-runtimes", f"node:{NODE_RUNTIME_PATH}"] + strategy + cmd[3:]
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
                LOFI_PROGRESS[progress_id] = 100  # Mark complete
                save_lofi_progress(LOFI_PROGRESS)
                audio_path = os.path.join(LOFI_DIR, f"{safe_title}.mp3")
                extract_audio_from_video(expected_file, audio_path)
                print(f"✓ Download complete: {safe_title}")
                lofi_log("download_complete")
                # Immediately remove from in-progress after completion
                if progress_id in LOFI_IN_PROGRESS:
                    del LOFI_IN_PROGRESS[progress_id]
                    save_lofi_in_progress(LOFI_IN_PROGRESS)
                if progress_id in LOFI_PROGRESS:
                    del LOFI_PROGRESS[progress_id]
                    save_lofi_progress(LOFI_PROGRESS)
            else:
                print(f"✗ Download failed: {safe_title}")
                lofi_log("download_failed")
                # Remove failed downloads from tracking
                if progress_id in LOFI_IN_PROGRESS:
                    del LOFI_IN_PROGRESS[progress_id]
                    save_lofi_in_progress(LOFI_IN_PROGRESS)
                if progress_id in LOFI_PROGRESS:
                    del LOFI_PROGRESS[progress_id]
                    save_lofi_progress(LOFI_PROGRESS)
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
    result = []
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
    app.run(host="0.0.0.0", port=7000, debug=True)
