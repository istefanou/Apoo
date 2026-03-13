// ===================== GOOGLE CAST =====================
const btnCastLofi = document.getElementById("btn-cast-lofi");
if (btnCastLofi && window.cast && window.cast.framework) {
  btnCastLofi.style.display = "inline-block";
} else if (btnCastLofi) {
  btnCastLofi.style.display = "none";
}

function castLofiVideo() {
  if (!window.cast || !window.cast.framework) {
    alert("Google Cast is not available in this browser.");
    return;
  }
  const video = document.getElementById("lofi-video");
  if (!video || !video.src) {
    alert("No lofi video loaded.");
    return;
  }
  const context = cast.framework.CastContext.getInstance();
  context.setOptions({
    receiverApplicationId: chrome.cast.media.DEFAULT_MEDIA_RECEIVER_APP_ID,
    autoJoinPolicy: chrome.cast.AutoJoinPolicy.ORIGIN_SCOPED
  });
  const mediaInfo = new chrome.cast.media.MediaInfo(video.src, "video/mp4");
  const request = new chrome.cast.media.LoadRequest(mediaInfo);
  context.requestSession().then(() => {
    context.getCurrentSession().loadMedia(request).then(
      () => {},
      (err) => { alert("Failed to cast: " + err); }
    );
  });
}

if (btnCastLofi) {
  btnCastLofi.addEventListener("click", castLofiVideo);
}
// ======================================================
//  GLOBAL IN-MEMORY STATE (Option B)
// ======================================================

let FAVORITES = [];
let PLAYLISTS = {};
let LAST_SEARCH_RESULTS = [];
let ACTIVE_PLAYLIST_NAME = null;
let PLAYLIST_ENRICHED = {};
let PLAYLIST_COVER_PICK = {};
let SEARCH_SETTINGS = { whitelist_words: [], blacklist_words: [] };
let TRACK_METADATA_CACHE = {};
let TRACK_METADATA_PENDING = new Set();
let TRACK_METADATA_ATTEMPTED = new Map();
const TRACK_METADATA_RETRY_MS = 5 * 60 * 1000;
let LAST_CURRENT = null; // cache current track state for audio sync
let LAST_AUDIO_SRC = ""; // track last src set on browser audio
let DID_INITIAL_SYNC = false; // one-time sync per track for browser mode
let JOURNAL_REQUEST = null;
let ACTIVE_SPOTIFY_IMPORT_ID = null;
let SPOTIFY_IMPORT_POLL_TIMER = null;

// ======================================================
//  API HELPERS
// ======================================================

async function apiGet(path) {
  const res = await fetch(path);
  return res.json();
}

async function apiPost(path, body) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : null,
  });
  return res.json();
}


// ======================================================
//  INITIAL LOAD
// ======================================================

async function loadInitialData() {
  try {
    const favoritesData = await apiGet("/api/favorites");
    const playlistsData = await apiGet("/api/playlists");
    const searchSettings = await apiGet("/api/settings/search");

    FAVORITES = Array.isArray(favoritesData) ? favoritesData : [];
    PLAYLISTS = (playlistsData && typeof playlistsData === "object" && !Array.isArray(playlistsData)) ? playlistsData : {};
    SEARCH_SETTINGS = normalizeSearchSettings(searchSettings);
  } catch (e) {
    console.error("Failed to load initial data:", e);
    FAVORITES = [];
    PLAYLISTS = {};
    SEARCH_SETTINGS = normalizeSearchSettings();
  }

  hydrateSearchSettingsForm();
  renderFavoritesTab();
  renderPlaylistsTab();
}

loadInitialData();


// ======================================================
//  TAB SWITCHING
// ======================================================

const tabs = document.querySelectorAll(".tab");
const views = document.querySelectorAll(".view");

tabs.forEach(tab => {
  tab.addEventListener("click", () => {
    tabs.forEach(t => t.classList.remove("active"));
    tab.classList.add("active");

    views.forEach(v => v.classList.remove("active"));

    const targetId = tab.id.replace("tab-", "view-");
    const targetView = document.getElementById(targetId);
    if (targetView) targetView.classList.add("active");

    if (targetId === "view-favorites") {
      renderFavoritesTab();
    } else if (targetId === "view-playlists") {
      renderPlaylistsTab();
    } else if (targetId === "view-settings") {
      refreshServiceJournal();
    }
  });
});


// ======================================================
//  FAVORITES (SERVER STORAGE + IN-MEMORY)
// ======================================================

function isFavorite(id) {
  return FAVORITES.some(t => t.id === id);
}

function toggleFavorite(track) {
  if (isFavorite(track.id)) {
    FAVORITES = FAVORITES.filter(t => t.id !== track.id);
  } else {
    FAVORITES.push(cleanTrack(track));
  }

  // Instant UI
  renderFavoritesTab();
  renderPlaylistsTab();

  // Background save
  saveFavorites();
}

function saveFavorites() {
  apiPost("/api/favorites/save", FAVORITES);
}


// ======================================================
//  PLAYLISTS (SERVER STORAGE + IN-MEMORY)
// ======================================================

function createPlaylist(name) {
  if (!name || name.trim() === "") return { error: "empty" };
  if (PLAYLISTS[name]) return { error: "exists" };

  PLAYLISTS[name] = [];

  // Instant UI
  renderPlaylistsTab();

  // Background save
  savePlaylists();
  return { success: true };
}

function deletePlaylist(name) {
  delete PLAYLISTS[name];

  // Instant UI
  renderPlaylistsTab();

  // Background save
  savePlaylists();
}

function addTrackToPlaylist(name, track) {
  if (!PLAYLISTS[name]) return;

  const cleaned = cleanTrack(track);

  if (!PLAYLISTS[name].some(t => t.id === cleaned.id)) {
    PLAYLISTS[name].push(cleaned);
  }

  // Instant UI
  renderPlaylistsTab();

  // Background save
  savePlaylists();
}

function savePlaylists() {
  apiPost("/api/playlists/save", PLAYLISTS);
}


// ======================================================
//  CLEAN TRACK OBJECT
// ======================================================

function cleanTrack(track) {
  return {
    id: track.id,
    title: track.title,
    artist: track.artist,
    deezer_id: track.deezer_id || null,
    spotify_id: track.spotify_id || null,
    path: track.path || null,
    duration: track.duration || null,
    artwork_url: track.artwork_url || track.album_art || track.thumbnail || null,
  };
}

function normalizeSearchSettings(data = {}) {
  return {
    whitelist_words: Array.isArray(data.whitelist_words) ? data.whitelist_words : [],
    blacklist_words: Array.isArray(data.blacklist_words) ? data.blacklist_words : [],
  };
}

function hydrateSearchSettingsForm() {
  if (searchWhitelistInput) {
    searchWhitelistInput.value = (SEARCH_SETTINGS.whitelist_words || []).join("\n");
  }
  if (searchBlacklistInput) {
    searchBlacklistInput.value = (SEARCH_SETTINGS.blacklist_words || []).join("\n");
  }
}

function parseSettingsWords(text) {
  return String(text || "")
    .split(/\n|,/)
    .map(word => word.trim().toLowerCase())
    .filter(Boolean)
    .filter((word, index, array) => array.indexOf(word) === index);
}

function getTrackCacheKey(track) {
  if (!track) return "";
  if (track.path) return `path::${track.path}`;
  if (track.deezer_id) return `deezer::${track.deezer_id}`;
  if (track.spotify_id) return `spotify::${track.spotify_id}`;
  if (track.id) return `id::${track.id}`;
  return `name::${String(track.title || "").toLowerCase()}::${String(track.artist || "").toLowerCase()}`;
}

function isTrackDownloading(track) {
  if (!track) return false;
  if (track._downloading || track._lofi_downloading) return true;
  return /\(downloading\)\s*$/i.test(String(track.title || ""));
}

function mergeTrackMetadata(track) {
  if (!track) return track;
  const cacheKey = getTrackCacheKey(track);
  const cached = cacheKey ? TRACK_METADATA_CACHE[cacheKey] : null;
  const artworkUrl = track.artwork_url || track.album_art || track.thumbnail || cached?.artwork_url || null;
  return {
    ...cached,
    ...track,
    artwork_url: artworkUrl,
    duration: track.duration ?? cached?.duration ?? null,
    deezer_id: track.deezer_id || cached?.deezer_id || null,
    spotify_id: track.spotify_id || cached?.spotify_id || null,
    path: track.path || cached?.path || null,
  };
}

async function ensureTracksEnriched(tracks, onDone) {
  const missing = [];
  const seen = new Set();

  (tracks || []).forEach((track) => {
    if (isTrackDownloading(track) && !track?.path) {
      return;
    }

    const cacheKey = getTrackCacheKey(track);
    if (!cacheKey || seen.has(cacheKey) || TRACK_METADATA_PENDING.has(cacheKey)) {
      return;
    }

    const attemptedAt = TRACK_METADATA_ATTEMPTED.get(cacheKey) || 0;
    if (attemptedAt && (Date.now() - attemptedAt) < TRACK_METADATA_RETRY_MS) {
      return;
    }

    seen.add(cacheKey);
    if (mergeTrackMetadata(track)?.artwork_url) {
      TRACK_METADATA_ATTEMPTED.set(cacheKey, Date.now());
      return;
    }

    TRACK_METADATA_ATTEMPTED.set(cacheKey, Date.now());
    TRACK_METADATA_PENDING.add(cacheKey);
    missing.push(track);
  });

  if (!missing.length) {
    return;
  }

  try {
    const data = await apiPost("/api/tracks/enrich", { tracks: missing });
    const enrichedTracks = Array.isArray(data?.tracks) ? data.tracks : [];
    enrichedTracks.forEach((track) => {
      const cacheKey = getTrackCacheKey(track);
      if (cacheKey) {
        TRACK_METADATA_CACHE[cacheKey] = track;
      }
    });
  } catch (e) {
    console.error("Failed to enrich tracks:", e);
  } finally {
    missing.forEach((track) => {
      const cacheKey = getTrackCacheKey(track);
      if (cacheKey) {
        TRACK_METADATA_PENDING.delete(cacheKey);
      }
    });
  }

  if (onDone) {
    onDone();
  }
}

function createTrackArtwork(track, className = "track-art") {
  const resolved = mergeTrackMetadata(track);
  const artwork = document.createElement("div");
  artwork.className = className;

  if (resolved?.artwork_url) {
    const img = document.createElement("img");
    img.src = resolved.artwork_url;
    img.alt = `${resolved.title || "Track"} cover`;
    artwork.appendChild(img);
  } else {
    artwork.classList.add("placeholder");
    artwork.textContent = (resolved?.title || "?").trim().charAt(0).toUpperCase() || "?";
  }

  return artwork;
}


// ======================================================
//  PLAYLIST PICKER POPUP
// ======================================================

const picker = document.getElementById("playlist-picker");
const pickerList = document.getElementById("playlist-picker-list");
const pickerCancel = document.getElementById("picker-cancel");

let trackToAdd = null;

function openPlaylistPicker(track) {
  trackToAdd = track;

  pickerList.innerHTML = "";

  const names = Object.keys(PLAYLISTS);

  if (names.length === 0) {
    const li = document.createElement("li");
    li.textContent = "No playlists yet. Create one first.";
    pickerList.appendChild(li);
  } else {
    names.forEach(name => {
      const li = document.createElement("li");
      li.className = "track-item";

      const btn = document.createElement("button");
      btn.textContent = name;
      btn.addEventListener("click", () => {
        addTrackToPlaylist(name, trackToAdd);
        closePlaylistPicker();
      });

      li.appendChild(btn);
      pickerList.appendChild(li);
    });
  }

  picker.classList.remove("hidden");
}

function closePlaylistPicker() {
  picker.classList.add("hidden");
  trackToAdd = null;
}

if (pickerCancel) {
  pickerCancel.addEventListener("click", closePlaylistPicker);
}


// ======================================================
//  DOM REFERENCES
// ======================================================

const currentTrackEl = document.getElementById("current-track");
const queueListEl = document.getElementById("queue-list");
const volumeSlider = document.getElementById("volume-slider");
const browserAudio = document.getElementById("browser-audio");

const outputHostRadio = document.getElementById("output-host");
const outputBrowserRadio = document.getElementById("output-browser");
const btnClear = document.getElementById("btn-clear");

const btnToggle = document.getElementById("btn-toggle");
const btnNext = document.getElementById("btn-next");
const btnPrev = document.getElementById("btn-prev"); // you need this in HTML
const btnShuffle = document.getElementById("btn-shuffle");
const autoFillToggle = document.getElementById("auto-fill-toggle");

const searchInput = document.getElementById("search-input");
const btnSearch = document.getElementById("btn-search");
const searchResultsEl = document.getElementById("search-results");

const favoritesListEl = document.getElementById("favorites-list");
const playlistsListEl = document.getElementById("playlists-list");
const newPlaylistNameInput = document.getElementById("new-playlist-name");
const btnCreatePlaylist = document.getElementById("btn-create-playlist");
const btnImportSpotify = document.getElementById("btn-import-spotify");
const searchWhitelistInput = document.getElementById("search-whitelist");
const searchBlacklistInput = document.getElementById("search-blacklist");
const btnSaveSearchSettings = document.getElementById("btn-save-search-settings");
const btnRefreshJournal = document.getElementById("btn-refresh-journal");
const journalMetaEl = document.getElementById("journal-meta");
const journalOutputEl = document.getElementById("journal-output");
const playlistImportStatusEl = document.getElementById("playlist-import-status");
const playlistImportTitleEl = document.getElementById("playlist-import-title");
const playlistImportMetaEl = document.getElementById("playlist-import-meta");
const playlistImportMessageEl = document.getElementById("playlist-import-message");
const playlistImportProgressBarEl = document.getElementById("playlist-import-progress-bar");

function stopSpotifyImportPolling() {
  if (SPOTIFY_IMPORT_POLL_TIMER) {
    clearInterval(SPOTIFY_IMPORT_POLL_TIMER);
    SPOTIFY_IMPORT_POLL_TIMER = null;
  }
}

function setPlaylistImportStatus({ title = "Spotify Import", meta = "", message = "", progress = null, state = "running" } = {}) {
  if (!playlistImportStatusEl) {
    return;
  }

  playlistImportStatusEl.classList.remove("hidden", "is-running", "is-complete", "is-error");
  playlistImportStatusEl.classList.add(`is-${state}`);

  if (playlistImportTitleEl) {
    playlistImportTitleEl.textContent = title;
  }
  if (playlistImportMetaEl) {
    playlistImportMetaEl.textContent = meta;
  }
  if (playlistImportMessageEl) {
    playlistImportMessageEl.textContent = message;
  }
  if (playlistImportProgressBarEl) {
    const safeProgress = Number.isFinite(progress) ? Math.max(0, Math.min(100, progress)) : 8;
    playlistImportProgressBarEl.style.width = `${safeProgress}%`;
  }
}

function schedulePlaylistImportStatusHide(delay = 5000) {
  window.setTimeout(() => {
    if (ACTIVE_SPOTIFY_IMPORT_ID || !playlistImportStatusEl || !playlistImportStatusEl.classList.contains("is-complete")) {
      return;
    }
    playlistImportStatusEl.classList.add("hidden");
  }, delay);
}

async function pollSpotifyImportStatus(progressId) {
  if (!progressId || ACTIVE_SPOTIFY_IMPORT_ID !== progressId) {
    return;
  }

  try {
    const data = await apiGet(`/api/spotify/import/status?progress_id=${encodeURIComponent(progressId)}&t=${Date.now()}`);
    if (data?.error) {
      return;
    }

    const expected = Number.isFinite(data?.expected_total) ? data.expected_total : null;
    const collected = Number.isFinite(data?.collected_count) ? data.collected_count : null;
    const metaParts = [];
    if (collected !== null) {
      metaParts.push(expected ? `${collected}/${expected} tracks` : `${collected} tracks`);
    }
    if (data?.updated_at) {
      metaParts.push(data.updated_at);
    }

    setPlaylistImportStatus({
      title: data?.playlist_name ? `Spotify Import: ${data.playlist_name}` : "Spotify Import",
      meta: metaParts.join(" • "),
      message: data?.message || "Import in progress...",
      progress: Number.isFinite(data?.progress) ? data.progress : null,
      state: data?.state || "running",
    });
  } catch (e) {
    console.error("Failed to poll Spotify import status:", e);
  }
}

async function refreshServiceJournal() {
  if (!journalOutputEl) {
    return;
  }

  if (JOURNAL_REQUEST) {
    return JOURNAL_REQUEST;
  }

  journalOutputEl.textContent = "Loading recent service log...";
  if (journalMetaEl) {
    journalMetaEl.textContent = "Fetching smart_home_apoo.service...";
  }

  JOURNAL_REQUEST = (async () => {
    try {
      const data = await apiGet(`/api/settings/journal?lines=150&t=${Date.now()}`);
      if (data?.error) {
        throw new Error(data.error);
      }

      const lines = Array.isArray(data?.lines) ? data.lines : [];
      journalOutputEl.textContent = lines.length ? lines.join("\n") : "No journal lines available yet.";
      journalOutputEl.scrollTop = journalOutputEl.scrollHeight;

      if (journalMetaEl) {
        const serviceName = data?.service || "smart_home_apoo.service";
        const lineCount = typeof data?.line_count === "number" ? data.line_count : lines.length;
        const fetchedAt = data?.fetched_at || "just now";
        journalMetaEl.textContent = `${serviceName} • ${lineCount} lines • refreshed ${fetchedAt}`;
      }
    } catch (e) {
      journalOutputEl.textContent = `Failed to load the service log.\n${e.message}`;
      if (journalMetaEl) {
        journalMetaEl.textContent = "Service log unavailable.";
      }
    } finally {
      JOURNAL_REQUEST = null;
    }
  })();

  return JOURNAL_REQUEST;
}


// ======================================================
//  QUEUE + PLAYER STATE
// ======================================================

async function refreshState() {
  const state = await apiGet("/api/queue");
  renderState(state);
}

function formatTime(seconds) {
  if (!seconds || seconds < 0) return "0:00";
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
}

async function addToQueue(track) {
  await apiPost("/api/queue/add", cleanTrack(track));
}

async function addToQueueNext(track) {
  await apiPost("/api/queue/add_next", cleanTrack(track));
}

function formatPlaylistDuration(seconds) {
  if (!seconds || seconds <= 0) return "0 min";
  const rounded = Math.round(seconds);
  const hours = Math.floor(rounded / 3600);
  const minutes = Math.floor((rounded % 3600) / 60);
  const parts = [];
  if (hours) parts.push(`${hours} hr` + (hours === 1 ? "" : "s"));
  if (minutes) parts.push(`${minutes} min`);
  if (!parts.length) parts.push("< 1 min");
  return parts.join(" ");
}

async function ensurePlaylistEnriched(name, tracks) {
  if (!name || PLAYLIST_ENRICHED[name]) return PLAYLIST_ENRICHED[name] || [];
  try {
    const data = await apiPost("/api/playlists/enrich", { tracks });
    PLAYLIST_ENRICHED[name] = Array.isArray(data?.tracks) ? data.tracks : tracks;
  } catch (e) {
    console.error("Failed to enrich playlist:", e);
    PLAYLIST_ENRICHED[name] = tracks;
  }

  const artTracks = (PLAYLIST_ENRICHED[name] || []).filter(t => t?.artwork_url);
  if (artTracks.length) {
    const pool = [...artTracks].sort(() => Math.random() - 0.5).slice(0, 4);
    PLAYLIST_COVER_PICK[name] = pool.map(t => t.artwork_url);
  } else {
    PLAYLIST_COVER_PICK[name] = [];
  }

  return PLAYLIST_ENRICHED[name];
}

function getPlaylistCoverUrls(name, tracks) {
  const artUrls = (tracks || [])
    .map(t => mergeTrackMetadata(t)?.artwork_url)
    .filter(Boolean);

  if (!artUrls.length) {
    PLAYLIST_COVER_PICK[name] = [];
    return [];
  }

  const orderedFirstFour = [...new Set(artUrls)].slice(0, 4);
  PLAYLIST_COVER_PICK[name] = orderedFirstFour;
  return orderedFirstFour;
}


// Progress cache to stop bar when paused
let LAST_ELAPSED = 0;
let LAST_DURATION = 0;
let LAST_PROGRESS = 0;


// ======================================================
//  TRACK ITEM RENDERER (with optional remove-from-queue)
// ======================================================

function renderTrackItem(container, track, isQueue = false) {
  const resolvedTrack = mergeTrackMetadata(track);
  const li = document.createElement("li");
  li.className = "track-item";

  if (isQueue) {
    li.setAttribute("draggable", "true");
    li.setAttribute("data-track-id", resolvedTrack.id);
  }

  const main = document.createElement("div");
  main.className = "track-main";
  main.appendChild(createTrackArtwork(resolvedTrack));

  const info = document.createElement("div");
  info.className = "track-info";

  const titleEl = document.createElement("div");
  titleEl.className = "track-title";
  titleEl.textContent = resolvedTrack.title || "Unknown";

  const artistEl = document.createElement("div");
  artistEl.className = "track-artist";
  artistEl.textContent = resolvedTrack.artist || "Unknown artist";

  info.appendChild(titleEl);
  info.appendChild(artistEl);
  main.appendChild(info);
  li.appendChild(main);

  const actions = document.createElement("div");
  actions.className = "track-actions";

  // Duration
  if (resolvedTrack.duration) {
    const durEl = document.createElement("span");
    durEl.className = "track-duration";
    durEl.textContent = formatTime(resolvedTrack.duration);
    actions.appendChild(durEl);
  }

  // Queue Next
  const queueAddNextBtn = document.createElement("button");
  queueAddNextBtn.className = "icon-btn";
  queueAddNextBtn.title = "Queue Next";
  queueAddNextBtn.textContent = "Next";
  queueAddNextBtn.addEventListener("click", async () => {
    await addToQueueNext(resolvedTrack);
    await refreshState();
  });

  // Queue Last
  const queueAddLastBtn = document.createElement("button");
  queueAddLastBtn.className = "icon-btn";
  queueAddLastBtn.title = "Queue Last";
  queueAddLastBtn.textContent = "Last";
  queueAddLastBtn.addEventListener("click", async () => {
    await addToQueue(resolvedTrack);
    await refreshState();
  });

  // Favorite
  const favBtn = document.createElement("button");
  favBtn.className = "icon-btn";
  favBtn.textContent = isFavorite(resolvedTrack.id) ? "♥" : "♡";
  favBtn.addEventListener("click", () => {
    toggleFavorite(resolvedTrack);
    favBtn.textContent = isFavorite(resolvedTrack.id) ? "♥" : "♡";
    if (LAST_SEARCH_RESULTS.length > 0 && searchResultsEl.contains(container)) {
      renderSearchResults(LAST_SEARCH_RESULTS);
    }
    renderFavoritesTab();
    renderPlaylistsTab();
  });

  // Add to playlist
  const playlistBtn = document.createElement("button");
  playlistBtn.className = "icon-btn";
  playlistBtn.textContent = "📂";
  playlistBtn.addEventListener("click", () => {
    openPlaylistPicker(resolvedTrack);
  });

  // Delete track from disk
  const deleteBtn = document.createElement("button");
  deleteBtn.className = "icon-btn icon-btn-danger";
  deleteBtn.title = "Delete file";
  deleteBtn.textContent = "🗑";
  deleteBtn.addEventListener("click", async () => {
    const path = resolvedTrack.path;
    if (!path) {
      alert("Cannot delete: no local file path known for this track.");
      return;
    }
    if (!confirm(`Delete "${resolvedTrack.title}" permanently from disk?`)) return;
    const res = await apiPost("/api/track/delete", { path });
    if (res?.error) {
      alert("Delete failed: " + res.error);
      return;
    }
    // Remove from local caches
    const cacheKey = getTrackCacheKey(resolvedTrack);
    if (cacheKey) delete TRACK_METADATA_CACHE[cacheKey];
    li.remove();
    await refreshState();
  });

  actions.appendChild(queueAddNextBtn);
  actions.appendChild(queueAddLastBtn);
  actions.appendChild(favBtn);
  actions.appendChild(playlistBtn);
  actions.appendChild(deleteBtn);

  // Remove from queue (only in queue view)
  if (isQueue) {
    const removeBtn = document.createElement("button");
    removeBtn.className = "icon-btn";
    removeBtn.title = "Remove from queue";
    removeBtn.textContent = "✕";
    removeBtn.addEventListener("click", async () => {
      await apiPost("/api/queue/remove", { id: resolvedTrack.id });
      await refreshState();
    });
    actions.appendChild(removeBtn);
  }

  li.appendChild(actions);

  container.appendChild(li);
}


// ======================================================
//  RENDER STATE (with pause-progress fix)
// ======================================================

let isSeeking = false;
const seekBar = document.getElementById("seek-bar");
const elapsedEl = document.getElementById("time-elapsed");
const totalEl = document.getElementById("time-total");

function renderState(state) {
  const resolvedCurrent = state.current ? mergeTrackMetadata(state.current) : null;
  const resolvedQueue = Array.isArray(state.queue) ? state.queue.map(mergeTrackMetadata) : [];

  ensureTracksEnriched(
    [state.current, ...(state.queue || [])].filter(Boolean),
    () => renderState({
      ...state,
      current: state.current ? mergeTrackMetadata(state.current) : null,
      queue: (state.queue || []).map(mergeTrackMetadata),
    })
  );

  // Update output mode radio buttons based on server state
  if (state.playback_mode) {
    if (state.playback_mode === "host") {
      outputHostRadio.checked = true;
      outputBrowserRadio.checked = false;
    } else if (state.playback_mode === "browser") {
      outputHostRadio.checked = false;
      outputBrowserRadio.checked = true;
    }
  }
  
  if (outputBrowserRadio && outputBrowserRadio.checked) {
    volumeSlider.value = browserAudio.muted ? 0 : (browserAudio.volume || BROWSER_VOLUME_CACHE);
  } else {
    volumeSlider.value = state.volume;
  }
  btnToggle.textContent = state.paused ? "▶️" : "⏸";
  
  // Update shuffle button appearance
  if (btnShuffle) {
    btnShuffle.style.opacity = state.shuffle ? "1" : "0.5";
    btnShuffle.style.fontWeight = state.shuffle ? "bold" : "normal";
  }

  if (autoFillToggle) {
    autoFillToggle.checked = !!state.auto_fill;
  }

  // Update repeat button appearance
  const btnRepeat = document.getElementById("btn-repeat");
  if (btnRepeat && state.repeat) {
    const repeatModes = {
      "off": { icon: "🔁", title: "Repeat: Off", opacity: "0.5" },
      "one": { icon: "🔂", title: "Repeat: One", opacity: "1" },
      "all": { icon: "🔁", title: "Repeat: All", opacity: "1" }
    };
    const mode = repeatModes[state.repeat] || repeatModes["off"];
    btnRepeat.textContent = mode.icon;
    btnRepeat.title = mode.title;
    btnRepeat.style.opacity = mode.opacity;
    btnRepeat.style.fontWeight = state.repeat !== "off" ? "bold" : "normal";
  }


  currentTrackEl.innerHTML = "";

  if (resolvedCurrent) {
    const t = resolvedCurrent;
    LAST_CURRENT = t;

    // Prepare browser audio source once per track
    const src = `/media/by-path?path=${encodeURIComponent(t.path)}`;
    if (LAST_AUDIO_SRC !== src) {
      LAST_AUDIO_SRC = src;
      try {
        browserAudio.src = src;
        browserAudio.load();
        DID_INITIAL_SYNC = false; // will sync once on metadata
      } catch (_) {}
    }

    // Handle browser audio playback based on mode and state
    if (outputBrowserRadio && outputBrowserRadio.checked && state.playback_mode === "browser") {
      // Browser mode: mirror play/pause state
      if (state.paused) {
        if (!browserAudio.paused) {
          try { browserAudio.pause(); } catch (_) {}
        }
      } else {
        if (browserAudio.paused) {
          browserAudio.play().catch(() => {});
        }
      }
    } else {
      // Host mode: ensure browser audio is paused
      if (!browserAudio.paused) {
        try { browserAudio.pause(); } catch (_) {}
      }
    }

    const container = document.createElement("div");
    container.className = "current-track-main";
    container.appendChild(createTrackArtwork(t, "track-art current-track-art"));
    
    const title = document.createElement("div");
    title.className = "track-meta";
    title.textContent = `${t.title} — ${t.artist}`;
    title.style.flex = "1";
    
    const favBtn = document.createElement("button");
    favBtn.className = "icon-btn";
    favBtn.textContent = isFavorite(t.id) ? "♥" : "♡";
    favBtn.style.fontSize = "1.5em";
    favBtn.addEventListener("click", () => {
      toggleFavorite(t);
      // Update button immediately
      favBtn.textContent = isFavorite(t.id) ? "♥" : "♡";
    });
    
    container.appendChild(title);
    container.appendChild(favBtn);
    currentTrackEl.appendChild(container);

    let elapsed = t.elapsed;
    let duration = t.duration;
    let progress = t.progress || 0;

    // If not paused, trust backend and update cache
    if (!state.paused) {
      LAST_ELAPSED = elapsed;
      LAST_DURATION = duration;
      LAST_PROGRESS = progress;
    } else {
      // If paused, freeze at last known values
      elapsed = LAST_ELAPSED;
      duration = LAST_DURATION;
      progress = LAST_PROGRESS;
    }

    elapsedEl.textContent = formatTime(elapsed);
    totalEl.textContent = formatTime(duration);

    if (!isSeeking) {
      seekBar.value = Math.floor((progress || 0) * 1000);
    }
    seekBar.disabled = false;
  } else {
    seekBar.value = 0;
    seekBar.disabled = true;
    elapsedEl.textContent = "0:00";
    totalEl.textContent = "0:00";
  }

  // Render queue
  queueListEl.innerHTML = "";
  resolvedQueue.forEach(t => renderTrackItem(queueListEl, t, true));
  
  // Re-attach drag and drop handlers after rendering
  attachQueueDragHandlers();
}

// ======================================================
//  DRAG AND DROP FOR QUEUE REORDERING
// ======================================================

let draggedItem = null;
let draggedFromIndex = null;

function attachQueueDragHandlers() {
  const queueItems = document.querySelectorAll("#queue-list .track-item[draggable='true']");
  
  queueItems.forEach((item, index) => {
    // Handle drag start
    item.addEventListener("dragstart", (e) => {
      draggedItem = item;
      draggedFromIndex = index;
      item.classList.add("dragging");
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData("text/html", item.innerHTML);
    });
    
    // Handle drag over
    item.addEventListener("dragover", (e) => {
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
      
      if (item !== draggedItem) {
        item.classList.add("drag-over");
      }
    });
    
    // Handle drag leave
    item.addEventListener("dragleave", () => {
      item.classList.remove("drag-over");
    });
    
    // Handle drop
    item.addEventListener("drop", async (e) => {
      e.preventDefault();
      e.stopPropagation();
      
      if (item !== draggedItem && draggedFromIndex !== null) {
        const allItems = Array.from(queueListEl.querySelectorAll(".track-item[draggable='true']"));
        const draggedToIndex = allItems.indexOf(item);
        
        // Reorder via API
        await apiPost("/api/queue/reorder", { 
          from: draggedFromIndex, 
          to: draggedToIndex 
        });
        
        await refreshState();
      }
      
      item.classList.remove("drag-over");
    });
    
    // Handle drag end
    item.addEventListener("dragend", () => {
      item.classList.remove("dragging");
      queueItems.forEach(qi => qi.classList.remove("drag-over"));
      draggedItem = null;
      draggedFromIndex = null;
    });
  });
}


// ======================================================
//  CONTROLS (Play/Pause/Next/Prev)
// ======================================================

btnToggle.addEventListener("click", async () => {
  const state = await apiGet("/api/queue");

  if (state.paused) {
    await apiPost("/api/play");
  } else {
    await apiPost("/api/pause");
  }

  await refreshState();
});

btnNext.addEventListener("click", async () => {
  await apiPost("/api/next");
  await refreshState();
});

btnPrev.addEventListener("click", async () => {
  await apiPost("/api/prev");
  await refreshState();
});

if (btnShuffle) {
  btnShuffle.addEventListener("click", async () => {
    const state = await apiGet("/api/queue");
    const newShuffle = !state.shuffle;
    await apiPost("/api/shuffle", { shuffle: newShuffle });
    await refreshState();
  });
}

if (autoFillToggle) {
  autoFillToggle.addEventListener("change", async () => {
    await apiPost("/api/auto-fill", { enabled: autoFillToggle.checked });
    await refreshState();
  });
}

// Repeat button
const btnRepeat = document.getElementById("btn-repeat");
if (btnRepeat) {
  btnRepeat.addEventListener("click", async () => {
    const result = await apiPost("/api/repeat", {});
    await refreshState();
  });
}

volumeSlider.addEventListener("input", async e => {
  const vol = parseFloat(e.target.value);
  if (outputBrowserRadio && outputBrowserRadio.checked) {
    browserAudio.volume = vol;
    BROWSER_VOLUME_CACHE = vol;
    // Ensure host muted while in browser mode
    await apiPost("/api/volume", { volume: 0 });
  } else {
    await apiPost("/api/volume", { volume: vol });
    HOST_VOLUME_CACHE = vol;
    // Keep browser muted while in host mode
    browserAudio.muted = true;
  }
});


// ======================================================
//  SEEK BAR
// ======================================================

seekBar.addEventListener("input", () => {
  isSeeking = true;
});

seekBar.addEventListener("change", async () => {
  const state = await apiGet("/api/queue");
  if (!state.current) return;

  const newProgress = seekBar.value / 1000;
  const newPosition = newProgress * state.current.duration;

  // In browser mode, seek the browser audio directly
  if (outputBrowserRadio && outputBrowserRadio.checked && state.playback_mode === "browser") {
    try { 
      browserAudio.currentTime = newPosition;
      // Also update server so it knows where we are
      await apiPost("/api/browser/progress", { elapsed: newPosition });
    } catch (_) {}
  } else {
    // In host mode, tell server to seek
    await apiPost("/api/seek", { position: newPosition });
  }

  isSeeking = false;
  await refreshState();
});


// ======================================================
//  SEARCH
// ======================================================

btnSearch.addEventListener("click", search);
searchInput.addEventListener("keydown", e => {
  if (e.key === "Enter") search();
});

async function search() {
  const q = searchInput.value.trim();
  if (!q) return;

  const data = await apiGet(`/api/search?q=${encodeURIComponent(q)}&type=track`);
  renderSearchResults(data.results || []);
}

function renderSearchResults(results) {
  LAST_SEARCH_RESULTS = results; // store for re-rendering
  searchResultsEl.innerHTML = "";
  results.map(mergeTrackMetadata).forEach(r => renderTrackItem(searchResultsEl, r, false));
  ensureTracksEnriched(results, () => {
    if (LAST_SEARCH_RESULTS === results) {
      renderSearchResults(results);
    }
  });
}

// ======================================================
//  FAVORITES TAB
// ======================================================

function renderFavoritesTab() {
  if (!favoritesListEl) return;
  favoritesListEl.innerHTML = "";
  FAVORITES.map(mergeTrackMetadata).forEach(t => renderTrackItem(favoritesListEl, t, false));
  ensureTracksEnriched(FAVORITES, () => {
    FAVORITES = FAVORITES.map(track => mergeTrackMetadata(track));
    renderFavoritesTab();
  });
}


// ======================================================
//  PLAYLISTS TAB (EXPANDABLE)
// ======================================================

function renderPlaylistsTab() {
  if (!playlistsListEl) return;
  const names = Object.keys(PLAYLISTS);
  if (!names.length) {
    playlistsListEl.innerHTML = '<li class="playlist-empty">No playlists yet. Create or import one.</li>';
    ACTIVE_PLAYLIST_NAME = null;
    return;
  }

  if (!ACTIVE_PLAYLIST_NAME || !PLAYLISTS[ACTIVE_PLAYLIST_NAME]) {
    ACTIVE_PLAYLIST_NAME = names[0];
  }

  const activeName = ACTIVE_PLAYLIST_NAME;
  const baseTracks = Array.isArray(PLAYLISTS[activeName]) ? PLAYLISTS[activeName] : [];
  const activeTracks = (Array.isArray(PLAYLIST_ENRICHED[activeName]) ? PLAYLIST_ENRICHED[activeName] : baseTracks).map(mergeTrackMetadata);

  ensureTracksEnriched(activeTracks, () => {
    if (ACTIVE_PLAYLIST_NAME === activeName) {
      renderPlaylistsTab();
    }
  });

  if (!PLAYLIST_ENRICHED[activeName]) {
    ensurePlaylistEnriched(activeName, baseTracks).then(() => {
      if (ACTIVE_PLAYLIST_NAME === activeName) {
        renderPlaylistsTab();
      }
    });
  }

  const totalDuration = activeTracks.reduce((sum, track) => sum + (Number(track?.duration) || 0), 0);

  playlistsListEl.innerHTML = "";

  const shell = document.createElement("li");
  shell.className = "playlist-spotify-shell";

  const strip = document.createElement("div");
  strip.className = "playlist-selector-strip";
  names.forEach((name) => {
    const btn = document.createElement("button");
    btn.className = "playlist-chip" + (name === activeName ? " active" : "");
    btn.textContent = `${name} (${Array.isArray(PLAYLISTS[name]) ? PLAYLISTS[name].length : 0})`;
    btn.addEventListener("click", () => {
      ACTIVE_PLAYLIST_NAME = name;
      renderPlaylistsTab();
    });
    strip.appendChild(btn);
  });

  const hero = document.createElement("div");
  hero.className = "playlist-hero";

  const heroCover = document.createElement("div");
  heroCover.className = "playlist-hero-cover";
  const coverUrls = getPlaylistCoverUrls(activeName, activeTracks);
  if (coverUrls.length) {
    heroCover.classList.add("playlist-hero-cover-grid");
    for (let index = 0; index < 4; index += 1) {
      const tile = document.createElement("div");
      tile.className = "playlist-cover-tile";
      const url = coverUrls[index];
      if (url) {
        const img = document.createElement("img");
        img.src = url;
        img.alt = "Playlist artwork";
        img.loading = "lazy";
        img.decoding = "async";
        img.addEventListener("error", () => {
          tile.classList.add("empty");
        });
        tile.appendChild(img);
      } else {
        tile.classList.add("empty");
      }
      heroCover.appendChild(tile);
    }
  } else {
    heroCover.textContent = (activeName || "?").trim().charAt(0).toUpperCase() || "?";
  }

  const heroMeta = document.createElement("div");
  heroMeta.className = "playlist-hero-meta";
  const heroType = document.createElement("div");
  heroType.className = "playlist-hero-type";
  heroType.textContent = "Public Playlist";

  const heroTitle = document.createElement("div");
  heroTitle.className = "playlist-hero-title";
  heroTitle.textContent = activeName;

  const heroSub = document.createElement("div");
  heroSub.className = "playlist-hero-sub";
  heroSub.textContent = `${activeTracks.length} songs${totalDuration ? `, ${formatPlaylistDuration(totalDuration)}` : ""}`;

  heroMeta.appendChild(heroType);
  heroMeta.appendChild(heroTitle);
  heroMeta.appendChild(heroSub);

  hero.appendChild(heroCover);
  hero.appendChild(heroMeta);

  const actions = document.createElement("div");
  actions.className = "playlist-hero-actions";

  const playBtn = document.createElement("button");
  playBtn.className = "playlist-action-primary";
  playBtn.textContent = "Play";
  playBtn.addEventListener("click", async () => {
    await apiPost("/api/clear", {});
    for (const t of activeTracks) {
      await addToQueue(t);
    }
    await refreshState();
  });

  const downloadBtn = document.createElement("button");
  downloadBtn.className = "playlist-action-secondary";
  downloadBtn.textContent = "Download ZIP";
  downloadBtn.addEventListener("click", async () => {
    try {
      const response = await fetch("/api/playlists/download", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: activeName })
      });
      if (!response.ok) {
        alert("Failed to download playlist");
        return;
      }
      const blob = await response.blob();
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${activeName}.zip`;
      document.body.appendChild(a);
      a.click();
      window.URL.revokeObjectURL(url);
      document.body.removeChild(a);
    } catch (e) {
      alert("Error downloading playlist: " + e.message);
    }
  });

  const deleteBtn = document.createElement("button");
  deleteBtn.className = "playlist-action-danger";
  deleteBtn.textContent = "Delete Playlist";
  deleteBtn.addEventListener("click", () => {
    if (confirm(`Delete playlist "${activeName}"?`)) {
      deletePlaylist(activeName);
      if (ACTIVE_PLAYLIST_NAME === activeName) {
        ACTIVE_PLAYLIST_NAME = null;
      }
    }
  });

  actions.appendChild(playBtn);
  actions.appendChild(downloadBtn);
  actions.appendChild(deleteBtn);

  const tableWrap = document.createElement("div");
  tableWrap.className = "playlist-table-wrap";

  const table = document.createElement("table");
  table.className = "playlist-table";
  table.innerHTML = `
    <thead>
      <tr>
        <th>#</th>
        <th>Title</th>
        <th>Artist</th>
        <th>Time</th>
        <th>Action</th>
      </tr>
    </thead>
  `;

  const tbody = document.createElement("tbody");
  activeTracks.forEach((t, idx) => {
    const row = document.createElement("tr");

    const c1 = document.createElement("td");
    c1.textContent = String(idx + 1);

    const c2 = document.createElement("td");
    c2.className = "playlist-track-title";

    const titleWrap = document.createElement("div");
    titleWrap.className = "playlist-track-main";
    titleWrap.appendChild(createTrackArtwork(t, "playlist-track-art"));

    const titleText = document.createElement("div");
    titleText.textContent = t?.title || "Unknown title";
    titleWrap.appendChild(titleText);
    c2.appendChild(titleWrap);

    const c3 = document.createElement("td");
    c3.textContent = t?.artist || "Unknown artist";

    const c4 = document.createElement("td");
    c4.className = "playlist-track-duration";
    c4.textContent = t?.duration ? formatTime(t.duration) : "--:--";

    const c5 = document.createElement("td");
    c5.className = "playlist-row-actions-cell";

    const nextBtn = document.createElement("button");
    nextBtn.className = "playlist-row-add";
    nextBtn.textContent = "Next";
    nextBtn.addEventListener("click", async () => {
      await addToQueueNext(t);
      await refreshState();
    });

    const lastBtn = document.createElement("button");
    lastBtn.className = "playlist-row-add secondary";
    lastBtn.textContent = "Last";
    lastBtn.addEventListener("click", async () => {
      await addToQueue(t);
      await refreshState();
    });

    const removeFromPlaylistBtn = document.createElement("button");
    removeFromPlaylistBtn.className = "playlist-row-add danger";
    removeFromPlaylistBtn.textContent = "✕";
    removeFromPlaylistBtn.title = "Remove from playlist";
    removeFromPlaylistBtn.addEventListener("click", () => {
      PLAYLISTS[activeName] = (PLAYLISTS[activeName] || []).filter((_, i) => i !== idx);
      delete PLAYLIST_ENRICHED[activeName];
      delete PLAYLIST_COVER_PICK[activeName];
      savePlaylists();
      renderPlaylistsTab();
    });

    c5.appendChild(nextBtn);
    c5.appendChild(lastBtn);
    c5.appendChild(removeFromPlaylistBtn);

    row.appendChild(c1);
    row.appendChild(c2);
    row.appendChild(c3);
    row.appendChild(c4);
    row.appendChild(c5);
    tbody.appendChild(row);
  });

  table.appendChild(tbody);
  tableWrap.appendChild(table);

  shell.appendChild(strip);
  shell.appendChild(hero);
  shell.appendChild(actions);
  shell.appendChild(tableWrap);

  playlistsListEl.appendChild(shell);
}


// ======================================================
//  PLAYLIST CREATION + SPOTIFY/DEEZER IMPORT
// ======================================================

if (btnCreatePlaylist) {
  btnCreatePlaylist.addEventListener("click", () => {
    const name = newPlaylistNameInput.value.trim();
    const result = createPlaylist(name);

    if (result.error === "empty") {
      alert("Playlist name cannot be empty");
      return;
    }
    if (result.error === "exists") {
      alert("A playlist with that name already exists");
      return;
    }

    newPlaylistNameInput.value = "";
  });
}

if (btnImportSpotify) {
  btnImportSpotify.addEventListener("click", async () => {
    const url = prompt("Enter Spotify playlist URL or ID:");
    if (!url) return;

    const progressId = `spotify-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    ACTIVE_SPOTIFY_IMPORT_ID = progressId;
    stopSpotifyImportPolling();
    setPlaylistImportStatus({
      title: "Spotify Import",
      meta: "Starting...",
      message: "Preparing Spotify playlist import.",
      progress: 4,
      state: "running",
    });

    SPOTIFY_IMPORT_POLL_TIMER = window.setInterval(() => {
      pollSpotifyImportStatus(progressId);
    }, 900);
    pollSpotifyImportStatus(progressId);

    try {
      const data = await apiPost("/api/spotify/import", { url, progress_id: progressId });
      stopSpotifyImportPolling();

      if (!data || !data.name || !Array.isArray(data.tracks)) {
        ACTIVE_SPOTIFY_IMPORT_ID = null;
        setPlaylistImportStatus({
          title: "Spotify Import",
          meta: "Failed",
          message: data?.error ? `Failed to import Spotify playlist: ${data.error}` : "Failed to import Spotify playlist.",
          progress: 100,
          state: "error",
        });
        alert(data?.error ? `Failed to import Spotify playlist: ${data.error}` : "Failed to import Spotify playlist.");
        return;
      }

      const name = data.name;
      const wasExisting = Object.prototype.hasOwnProperty.call(PLAYLISTS, name);
      PLAYLISTS[name] = data.tracks.map(cleanTrack);
      delete PLAYLIST_ENRICHED[name];
      delete PLAYLIST_COVER_PICK[name];
      ACTIVE_PLAYLIST_NAME = name;

      renderPlaylistsTab();
      savePlaylists();

      ACTIVE_SPOTIFY_IMPORT_ID = null;
      setPlaylistImportStatus({
        title: `Spotify Import: ${name}`,
        meta: wasExisting ? "Playlist updated" : "Playlist imported",
        message: `Stored ${data.tracks.length} tracks and synced the playlist with Spotify's latest version.`,
        progress: 100,
        state: "complete",
      });
      schedulePlaylistImportStatusHide();

      if (data.warning) {
        alert(data.warning);
      }
    } catch (e) {
      stopSpotifyImportPolling();
      ACTIVE_SPOTIFY_IMPORT_ID = null;
      setPlaylistImportStatus({
        title: "Spotify Import",
        meta: "Failed",
        message: `Failed to import Spotify playlist: ${e.message}`,
        progress: 100,
        state: "error",
      });
      alert(`Failed to import Spotify playlist: ${e.message}`);
    }
  });
}


// ======================================================
//  POLLING
// ======================================================

setInterval(refreshState, 1000);
refreshState();

if (btnSaveSearchSettings) {
  btnSaveSearchSettings.addEventListener("click", async () => {
    const payload = {
      whitelist_words: parseSettingsWords(searchWhitelistInput?.value),
      blacklist_words: parseSettingsWords(searchBlacklistInput?.value),
    };

    try {
      const data = await apiPost("/api/settings/search", payload);
      SEARCH_SETTINGS = normalizeSearchSettings(data);
      hydrateSearchSettingsForm();
      alert("Search settings saved.");
    } catch (e) {
      alert(`Failed to save settings: ${e.message}`);
    }
  });
}

if (btnRefreshJournal) {
  btnRefreshJournal.addEventListener("click", refreshServiceJournal);
}

// ======================================================
//  OUTPUT MODE SWITCHING + CLEAR
// ======================================================

let HOST_VOLUME_CACHE = 0.7;
let BROWSER_VOLUME_CACHE = 0.7;

if (outputHostRadio) {
  outputHostRadio.addEventListener("change", async () => {
    if (outputHostRadio.checked) {
      // Switch to host mode
      await apiPost("/api/playback-mode", { mode: "host" });
      browserAudio.pause();
      browserAudio.currentTime = 0;
      browserAudio.muted = true;
      await apiPost("/api/volume", { volume: HOST_VOLUME_CACHE });
      volumeSlider.value = HOST_VOLUME_CACHE;
      await refreshState();
    }
  });
}

if (outputBrowserRadio) {
  outputBrowserRadio.addEventListener("change", async () => {
    if (outputBrowserRadio.checked) {
      // Switch to browser mode
      await apiPost("/api/playback-mode", { mode: "browser" });
      browserAudio.muted = false;
      // Cache current host volume before muting host
      HOST_VOLUME_CACHE = parseFloat(volumeSlider.value || HOST_VOLUME_CACHE);
      // Restore previous browser volume to slider and audio
      browserAudio.volume = BROWSER_VOLUME_CACHE;
      volumeSlider.value = BROWSER_VOLUME_CACHE;
      // Mute host by setting volume to 0 (pygame will be stopped by server)
      await apiPost("/api/volume", { volume: 0 });
      
      // Sync browser audio with current track
      const state = await apiGet("/api/queue");
      if (state.current && LAST_CURRENT) {
        try {
          if (LAST_AUDIO_SRC) {
            const target = Math.max(0, Math.min(browserAudio.duration || 0, state.current.elapsed || 0));
            browserAudio.currentTime = target;
            if (!state.paused) {
              await browserAudio.play();
            }
          }
        } catch(e) {
          console.error("Error syncing browser audio:", e);
        }
      }
      
      await refreshState();
    }
  });
}

if (btnClear) {
  btnClear.addEventListener("click", async () => {
    // Reset browser audio immediately
    try {
      browserAudio.pause();
      browserAudio.currentTime = 0;
      browserAudio.src = "";
      browserAudio.load();
      LAST_AUDIO_SRC = "";
      LAST_CURRENT = null;
      DID_INITIAL_SYNC = false;
    } catch (_) {}
    
    // Call clear endpoint
    await apiPost("/api/clear", {});
    
    // Force immediate UI refresh
    await refreshState();
  });
}

// Ensure currentTime sync once metadata loads
if (browserAudio) {
  browserAudio.addEventListener("loadedmetadata", async () => {
    // Only sync elapsed time in browser mode
    if (outputBrowserRadio && outputBrowserRadio.checked) {
      const state = await apiGet("/api/queue");
      if (state.current && state.playback_mode === "browser") {
        const target = Math.max(0, Math.min(browserAudio.duration || 0, state.current.elapsed || 0));
        try { 
          browserAudio.currentTime = target;
          // If not paused, start playing
          if (!state.paused && browserAudio.paused) {
            await browserAudio.play();
          }
        } catch(e) {
          console.error("Error syncing on loadedmetadata:", e);
        }
      }
    }
    DID_INITIAL_SYNC = true;
  });
  
  // Report progress to server when in browser mode
  let lastProgressReport = 0;
  browserAudio.addEventListener("timeupdate", () => {
    if (outputBrowserRadio && outputBrowserRadio.checked) {
      const now = Date.now();
      // Report every 500ms to avoid flooding
      if (now - lastProgressReport > 500) {
        lastProgressReport = now;
        apiPost("/api/browser/progress", { elapsed: browserAudio.currentTime }).catch(() => {});
      }
    }
  });
  
  // Report when track ends in browser mode
  browserAudio.addEventListener("ended", async () => {
    if (outputBrowserRadio && outputBrowserRadio.checked) {
      console.log("Browser audio ended, notifying server...");
      await apiPost("/api/browser/ended", {});
      // After server processes next track, refresh state
      setTimeout(async () => {
        const state = await apiGet("/api/queue");
        // If repeat-one mode, the track stays the same, so replay browser audio
        if (state.current && state.current.id === LAST_CURRENT?.id) {
          try {
            browserAudio.currentTime = 0;
            await browserAudio.play();
          } catch (e) {
            console.error("Error replaying in repeat-one mode:", e);
          }
        }
        await refreshState();
      }, 100);
    }
  });
  
  // Ensure normal playback rate
  try { browserAudio.playbackRate = 1.0; } catch (_) {}
}

// ======================================================
//  LOFI TAB FUNCTIONALITY
// ======================================================


const lofiUrlInput = document.getElementById("lofi-url-input");
const btnDownloadLofi = document.getElementById("btn-download-lofi");
const btnClearLofi = document.getElementById("btn-clear-lofi");
const lofiLibrary = document.getElementById("lofi-library");
const lofiPlayerContainer = document.getElementById("lofi-player-container");
const lofiVideo = document.getElementById("lofi-video");
const btnClosePlayer = document.getElementById("btn-close-player");
const btnFullscreen = document.getElementById("btn-fullscreen");
const lofiProgressContainer = document.getElementById("lofi-progress-container");
const lofiProgressBar = document.getElementById("lofi-progress-bar");
const lofiProgressValue = document.getElementById("lofi-progress-value");

// Load lofi library when tab is clicked
const lofiTab = document.getElementById("tab-lofi");
let lofiLibraryPollInterval = null;
let lofiDownloadProgressInterval = null;

lofiTab?.addEventListener("click", () => {
  loadLofiLibrary();
  startLofiPoll();  // Start polling for downloads
});

// Poll for active downloads; auto-stop when none remain
function startLofiPoll() {
  if (lofiLibraryPollInterval) return;  // Already polling
  
  lofiLibraryPollInterval = setInterval(async () => {
    try {
      const inProgressRes = await fetch("/api/lofi/in-progress");
      const inProgressData = await inProgressRes.json();
      const activeDownloads = (inProgressData.in_progress || []).filter(item => Number(item?.progress || 0) < 100);
      const hasActiveDownloads = activeDownloads.length > 0;
      
      if (!hasActiveDownloads) {
        // No active downloads, stop polling and refresh once more
        stopLofiPoll();
        loadLofiLibrary();  // Final refresh to remove completed downloads
        return;
      }
      
      // Active downloads remain, refresh library
      loadLofiLibrary();
    } catch (e) {
      console.error("Poll error:", e);
    }
  }, 500);  // Poll every 500ms while downloading
}

function stopLofiPoll() {
  if (lofiLibraryPollInterval) {
    clearInterval(lofiLibraryPollInterval);
    lofiLibraryPollInterval = null;
  }
}

function resetLofiProgressUi() {
  lofiProgressContainer.style.display = "none";
  lofiProgressBar.style.width = "0%";
  lofiProgressValue.textContent = "0%";
}

// Download lofi video
btnDownloadLofi?.addEventListener("click", async () => {
  const url = lofiUrlInput.value.trim();
  
  if (!url) {
    alert("Please enter a YouTube URL");
    return;
  }
  
  btnDownloadLofi.disabled = true;
  btnDownloadLofi.textContent = "Downloading...";
  lofiProgressBar.style.width = "0%";
  lofiProgressValue.textContent = "Download Starting Soon";
  lofiProgressContainer.style.display = "block";

  let progressInterval = null;
  try {
    const res = await apiPost("/api/lofi/download", { url });
    console.log("/api/lofi/download response:", res);
    let progressId = res.progress_id;
    
    // Load library immediately to show thumbnail + progress bar
    loadLofiLibrary();
    // Start library polling to refresh as download progresses
    startLofiPoll();
    
    if (res.error) {
      alert(`Error: ${res.error}`);
      lofiProgressContainer.style.display = "none";
    } else if (progressId) {
      // Poll progress
      await new Promise((resolve) => {
        progressInterval = setInterval(async () => {
          try {
            const progRes = await fetch(`/api/lofi/progress?progress_id=${encodeURIComponent(progressId)}`);
            const progData = await progRes.json();
            let percent = progData.progress || 0;
            console.log("Polling progress:", { progressId, percent, progData });
            lofiProgressBar.style.width = percent + "%";
            if (percent < 1) {
              lofiProgressValue.textContent = "Download Starting Soon";
            } else {
              lofiProgressValue.textContent = `Downloading ${percent}%`;
            }
            if (percent >= 100) {
              clearInterval(progressInterval);
              lofiDownloadProgressInterval = null;
              setTimeout(() => {
                resetLofiProgressUi();
              }, 1000);
              lofiUrlInput.value = "";
              loadLofiLibrary();
              resolve();
            }
          } catch (e) {
            console.log("Progress polling error:", e);
          }
        }, 500);
        lofiDownloadProgressInterval = progressInterval;
      });
    } else {
      resetLofiProgressUi();
      lofiUrlInput.value = "";
      loadLofiLibrary();
    }
  } catch (err) {
    alert(`Error: ${err.message}`);
    resetLofiProgressUi();
  } finally {
    btnDownloadLofi.disabled = false;
    btnDownloadLofi.textContent = "Download";
    if (progressInterval) clearInterval(progressInterval);
    if (progressInterval && lofiDownloadProgressInterval === progressInterval) {
      lofiDownloadProgressInterval = null;
    }
  }
});

btnClearLofi?.addEventListener("click", async () => {
  if (btnClearLofi.disabled) return;
  btnClearLofi.disabled = true;
  const prevText = btnClearLofi.textContent;
  btnClearLofi.textContent = "Clearing...";
  try {
    const res = await apiPost("/api/lofi/clear-in-progress", {});
    if (res.error) {
      alert(`Error: ${res.error}`);
    }
  } catch (err) {
    alert(`Error: ${err.message}`);
  } finally {
    stopLofiPoll();
    if (lofiDownloadProgressInterval) {
      clearInterval(lofiDownloadProgressInterval);
      lofiDownloadProgressInterval = null;
    }
    resetLofiProgressUi();
    await loadLofiLibrary();
    btnClearLofi.disabled = false;
    btnClearLofi.textContent = prevText;
  }
});

// Load and render lofi library, including in-progress downloads
async function loadLofiLibrary() {
  try {
    // Fetch both finished and in-progress
    const [listData, inProgressData] = await Promise.all([
      apiGet("/api/lofi/list"),
      apiGet("/api/lofi/in-progress")
    ]);
    const videos = listData.videos || [];
    const inProgress = (inProgressData.in_progress || [])
      .filter(item => Number(item?.progress || 0) < 100)
      .map(item => ({
        id: item.safe_title,
        title: item.title + ' (Downloading)',
        progress: item.progress,
        thumbnail: item.thumbnail,
        inProgress: true,
        progress_id: item.progress_id
      }));
    // Show in-progress first, then finished
    renderLofiLibrary([...inProgress, ...videos]);
  } catch (err) {
    console.error("Failed to load lofi library:", err);
  }
}

// Render lofi library grid
function renderLofiLibrary(videos) {
  lofiLibrary.innerHTML = "";
  if (videos.length === 0) {
    lofiLibrary.innerHTML = "<p>No lofi videos yet. Add one using the YouTube URL above.</p>";
    return;
  }
  videos.forEach(video => {
    const card = document.createElement("div");
    card.className = "lofi-card";

    // Thumbnail or video preview
    let thumbEl;
    if (video.inProgress && video.thumbnail) {
      thumbEl = document.createElement("img");
      thumbEl.className = "lofi-thumbnail";
      thumbEl.src = video.thumbnail;
    } else {
      thumbEl = document.createElement("video");
      thumbEl.className = "lofi-thumbnail";
      // Use correct extension for preview
      const ext = video.ext || "mp4";
      thumbEl.src = `/api/lofi/video/${video.id}`;
      thumbEl.type = `video/${ext}`;
      thumbEl.muted = true;
      thumbEl.loop = true;
      // Only enable preview if browser can play the format
      let mime = "";
      if (ext === "mp4") mime = "video/mp4";
      else if (ext === "webm") mime = "video/webm";
      else if (ext === "ogg") mime = "video/ogg";
      else if (ext === "mov") mime = "video/quicktime";
      else if (ext === "mkv") mime = "video/x-matroska";
      if (mime && thumbEl.canPlayType(mime) !== "") {
        card.addEventListener("mouseenter", () => { thumbEl.play().catch(() => {}); });
        card.addEventListener("mouseleave", () => { thumbEl.pause(); thumbEl.currentTime = 0; });
      }
    }
    card.appendChild(thumbEl);

    // Title
    const title = document.createElement("div");
    title.className = "lofi-title";
    title.textContent = video.title;
    card.appendChild(title);

    // Progress bar for in-progress
    if (video.inProgress) {
      const progBarWrap = document.createElement("div");
      progBarWrap.style.background = "#222";
      progBarWrap.style.borderRadius = "4px";
      progBarWrap.style.overflow = "hidden";
      progBarWrap.style.height = "14px";
      progBarWrap.style.margin = "8px 0";
      const progBar = document.createElement("div");
      progBar.style.background = "#1db954";
      progBar.style.height = "14px";
      progBar.style.width = (video.progress || 0) + "%";
      progBar.style.transition = "width 0.2s";
      progBarWrap.appendChild(progBar);
      card.appendChild(progBarWrap);
      // Progress label
      const progLabel = document.createElement("div");
      progLabel.style.fontSize = "0.95em";
      progLabel.style.marginBottom = "2px";
      progLabel.textContent = `Downloading ${video.progress || 0}%`;
      card.appendChild(progLabel);
    } else {
      // Actions
      const actions = document.createElement("div");
      actions.className = "lofi-actions";
      
      const playBtn = document.createElement("button");
      playBtn.textContent = "▶ Play Video";
      
      const playOnDeviceBtn = document.createElement("button");
      playOnDeviceBtn.textContent = "🔊 Play on Server";
      playOnDeviceBtn.style.background = "#1db954";
      
      const deleteBtn = document.createElement("button");
      deleteBtn.textContent = "🗑 Delete";
      deleteBtn.className = "delete-btn";
      
      if (!video.path) {
        playBtn.disabled = true;
        playOnDeviceBtn.disabled = true;
        deleteBtn.disabled = true;
        playBtn.textContent = "(Downloading)";
      } else {
        playBtn.addEventListener("click", () => { playLofiVideo(video); });
        playOnDeviceBtn.addEventListener("click", async () => { await playLofiOnDevice(video); });
        deleteBtn.addEventListener("click", async () => {
          if (confirm(`Delete ${video.title}?`)) {
            await deleteLofiVideo(video.id);
          }
        });
      }
      
      actions.appendChild(playBtn);
      actions.appendChild(playOnDeviceBtn);
      actions.appendChild(deleteBtn);
      card.appendChild(actions);
    }
    lofiLibrary.appendChild(card);
  });
}

// Play lofi video in player
function playLofiVideo(video) {
  lofiVideo.src = `/api/lofi/video/${video.id}`;
  lofiPlayerContainer.classList.remove("hidden");
  lofiVideo.play().catch(err => {
    console.error("Failed to play video:", err);
  });
}

// Close video player
btnClosePlayer?.addEventListener("click", () => {
  lofiVideo.pause();
  lofiVideo.src = "";
  lofiPlayerContainer.classList.add("hidden");
});

// Fullscreen toggle
btnFullscreen?.addEventListener("click", () => {
  if (!document.fullscreenElement) {
    lofiPlayerContainer.requestFullscreen().catch(err => {
      console.error("Failed to enter fullscreen:", err);
    });
  } else {
    document.exitFullscreen();
  }
});

// Delete lofi video
async function deleteLofiVideo(videoId) {
  try {
    const res = await fetch(`/api/lofi/delete/${videoId}`, {
      method: "DELETE"
    });
    
    const data = await res.json();
    
    if (data.error) {
      alert(`Error: ${data.error}`);
    } else {
      loadLofiLibrary();
    }
  } catch (err) {
    alert(`Error: ${err.message}`);
  }
}

// Play lofi on device (server audio only)
async function playLofiOnDevice(video) {
  try {
    const statusDiv = document.querySelector(".lofi-playback-info");
    const statusText = document.getElementById("lofi-playback-status");
    
    if (statusDiv) {
      statusDiv.style.display = "block";
      statusText.textContent = `⏳ Adding "${video.title}" to queue...`;
    }
    
    const res = await apiPost("/api/lofi/play-on-device", {
      video_id: video.id
    });
    
    if (res.error) {
      alert(`Error: ${res.error}`);
      if (statusDiv) statusDiv.style.display = "none";
    } else {
      if (statusDiv) {
        statusText.textContent = `✓ Added "${res.title}" to queue`;
        // Hide message after 3 seconds
        setTimeout(() => {
          statusDiv.style.display = "none";
        }, 3000);
      }
      // Refresh queue to show the new track
      await refreshState();
    }
  } catch (err) {
    alert(`Error: ${err.message}`);
    const statusDiv = document.querySelector(".lofi-playback-info");
    if (statusDiv) statusDiv.style.display = "none";
  }
}

