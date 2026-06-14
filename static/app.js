const $=id=>document.getElementById(id);
const esc=s=>String(s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
const enc=s=>encodeURIComponent(s||"");
// Every track/album art URL is routed through the gateway's /art proxy so
// it's always same-origin as the PWA. Without this, an HTTPS-served PWA
// loading raw http://media-server/...cover.jpg may be blocked as mixed
// content on some mobile browsers (iOS Safari PWA standalone is the
// strictest) — the thumbnail and the now-playing art then load
// inconsistently depending on whether the browser's cache has a prior
// successful fetch. Same-origin via /art removes that whole class.
const artUrl=raw=>raw?`/art?url=${encodeURIComponent(raw)}`:"";
const fmtSec=s=>{if(s==null||isNaN(s))return"0:00";s=Math.max(0,Math.floor(s));const h=Math.floor(s/3600),m=Math.floor((s%3600)/60),sc=s%60,p=n=>String(n).padStart(2,"0");return h?`${h}:${p(m)}:${p(sc)}`:`${m}:${p(sc)}`;};
const fmtDur=d=>{if(!d)return"";const p=d.split(":").map(Number);return p.length===3?fmtSec(p[0]*3600+p[1]*60+p[2]):d;};
async function api(url,opts){try{return await fetch(url,opts);}catch{return null;}}
function toast(msg,ms=2400){const t=$("toast");t.textContent=msg;t.classList.add("show");setTimeout(()=>t.classList.remove("show"),ms);}

// Fisher-Yates shuffle — cryptographically seeded, truly uniform distribution.
// .sort(()=>Math.random()-0.5) is biased and produces near-identical sequences.
function shuffle(arr){
  const a=[...arr];
  // Use crypto.getRandomValues for better entropy when available
  const rnd = (typeof crypto!=="undefined" && crypto.getRandomValues)
    ? (n)=>{ const buf=new Uint32Array(1); crypto.getRandomValues(buf); return buf[0]/0x100000000*n|0; }
    : (n)=>Math.random()*n|0;
  for(let i=a.length-1;i>0;i--){
    const j=rnd(i+1);
    [a[i],a[j]]=[a[j],a[i]];
  }
  return a;
}

let servers={},curServer=null,navStack=[{id:"0",title:"Root"}],curItemId=null,ps={state:"stopped"};
let seeking=false,seekTarget=0;
let curTab="browse";
let playlists=[],curPlId=null;
// Album favourites: cached list (null = stale, refetch on next view).

// Internet radio ("📡 Stations"): favourites cache (null = stale), the
// cap, and the station currently playing — when non-null the
// now-playing panel is in its radio variant.
let radioFavCache=null, radioFavLimit=25, currentRadioStation=null;
let _radioSearchTimer=null, _radioActiveTag="", _icyPollTimer=null;

// ── Browser audio queue ───────────────────────────────────────────
const browserAudio=document.getElementById("browser-audio");
let browserQueue=[],browserIdx=0;
let npTrack = null;   // track object currently shown in now-playing panel

function setNpTrack(t){
  npTrack = t || null;
  const panel = $("np-actions");
  if(npTrack && npTrack.url){
    panel.style.display = "flex";
    _renderNpYear(npTrack.url);
  } else {
    panel.style.display = "none";
    $("np-year").textContent = "";
  }
}

// Fetch and render the year line under album in the now-playing panel.
// Display rule: when both years are present, use MIN(file, mb) — the
// earlier one is most likely the "original recording" year, since
// AcoustID sometimes fingerprints to a later anniversary-edition
// recording on MusicBrainz (its first-release-date is then the
// reissue date, not the original). If the file year is meaningfully
// later than the earlier year, annotate as "1987 (remastered)" —
// typical sign of a remaster reissue you own. Same MIN logic as the
// decade browse (LibraryDB._EFFECTIVE_YEAR).
let _npYearReqUrl = null;
async function _renderNpYear(url){
  if(!url){ $("np-year").textContent = ""; return; }
  // Guard against races where successive setNpTrack() calls fire in
  // close order — only the most-recent URL gets to write the field.
  _npYearReqUrl = url;
  $("np-year").textContent = "";  // clear instantly
  try{
    const r = await fetch(`/api/track_meta?url=${enc(url)}`);
    if(!r.ok) return;
    const m = await r.json();
    if(_npYearReqUrl !== url) return;   // a newer fetch superseded us
    const origYear = m.year_original;
    const fileYear = m.year;
    let display = "";
    if(origYear && fileYear){
      const earlier = Math.min(origYear, fileYear);
      display = String(earlier);
      if(fileYear - earlier >= 3){
        display += " (remastered)";
      }
    } else if(origYear){
      display = String(origYear);
    } else if(fileYear){
      display = String(fileYear);
    }
    $("np-year").textContent = display;
  }catch(e){ /* best effort — silently fail */ }
}
// Persist shuffle preference across reloads
let shuffleEnabled=(()=>{const v=localStorage.getItem("dlna_shuffle");return v===null?true:v==="1";})();

browserAudio.addEventListener("ended",()=>{
  if(browserIdx<browserQueue.length-1){browserIdx++;_browserPlayIdx(browserIdx);}
  else{activeDevice="browser";} // playlist done
});
// ── <audio> error handling ───────────────────────────────────────
// MediaError.code mapping:
//   1 MEDIA_ERR_ABORTED        — user/script aborted (not our concern, ignore)
//   2 MEDIA_ERR_NETWORK        — transient network failure — retry ONCE before skipping
//   3 MEDIA_ERR_DECODE         — decode failure mid-stream — retry ONCE (bad chunk can heal)
//   4 MEDIA_ERR_SRC_NOT_SUPPORTED — genuinely unsupported format — skip immediately
//
// Prior to this refactor every "error" event treated the track as unsupported
// and skipped, causing false-positive skips whenever the network hiccupped or
// an upstream Range returned garbage. Discriminating by code fixes that.
//
// Tracks a per-URL retry counter so a persistently-broken track can't loop.
const _audioRetryCount = new Map();   // src URL → retry count
const _MEDIA_ERR = {1:"aborted", 2:"network", 3:"decode", 4:"unsupported"};

async function _reportClientError(payload){
  try{
    await fetch("/api/client_log",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify(payload)});
  }catch(e){ /* best effort — don't recurse */ }
}

browserAudio.addEventListener("error", e=>{
  if(activeDevice!=="browser") return;
  const err = browserAudio.error;
  const code = err ? err.code : 0;
  const codeName = _MEDIA_ERR[code] || `unknown(${code})`;
  const t = browserQueue[browserIdx];
  const src = browserAudio.currentSrc || "";
  const retries = _audioRetryCount.get(src) || 0;

  // Always surface the event to the gateway so we can see what's really happening.
  _reportClientError({
    kind:   "audio_error",
    code,           codeName,
    message:       err?.message || "",
    title:         t?.title  || "",
    artist:        t?.artist || "",
    src:           src.slice(0, 200),
    retries,
    queue_len:     browserQueue.length,
    ready_state:   browserAudio.readyState,
    network_state: browserAudio.networkState,
    user_agent:    navigator.userAgent.slice(0, 120),
  });

  // Code 1 (aborted): we told it to stop, or a new src was set. Ignore.
  if(code === 1) return;

  // Codes 2 (network) / 3 (decode): transient. Retry the same track once
  // before giving up.
  if((code === 2 || code === 3) && retries < 1 && src){
    _audioRetryCount.set(src, retries + 1);
    toast(`⟳ Retrying "${t?.title||'track'}" (${codeName})`);
    setTimeout(()=>{
      // Re-arm the same src (forces a fresh request)
      browserAudio.src = src;
      _playBrowserAudio("retry_"+codeName);
    }, 800);
    return;
  }

  // Reached here: code 4, or we've already retried once. Treat as unplayable.
  const why = code === 4 ? "unsupported format" : `${codeName} error`;
  if(browserQueue.length > 1){
    toast(`⚠ Skipping "${t?.title||'track'}" — ${why}`);
    setTimeout(()=>{
      if(browserIdx < browserQueue.length - 1){
        browserIdx++; _browserPlayIdx(browserIdx);
      } else {
        $("btn-pp").textContent="▶ Play"; $("mini-pp").textContent="▶";
      }
    }, 1500);
  } else {
    toast(`⚠ Can't play "${t?.title||'track'}" — ${why}`);
    $("btn-pp").textContent="▶ Play"; $("mini-pp").textContent="▶";
  }
});

// Clear the retry counter on a successful play-start
browserAudio.addEventListener("playing", ()=>{
  _audioRetryCount.clear();
});

// Single helper for every call to browserAudio.play() — surfaces the
// NotAllowedError that browsers throw when autoplay is blocked
// (first-play without a user gesture; backgrounded tab resume on iOS)
// instead of swallowing it silently. The old `.catch(()=>{})` form is
// what made "tapped play but nothing happened" invisible.
function _playBrowserAudio(reason){
  return browserAudio.play().catch(err=>{
    const name = err?.name || "Error";
    if(name === "NotAllowedError" || name === "AbortError"){
      // Update UI so the user can re-trigger manually.
      $("btn-pp").textContent="▶ Play"; $("mini-pp").textContent="▶";
      toast("⚠ Browser blocked playback — tap ▶ Play to start");
    } else {
      // Something unexpected. Don't swallow — log it.
      toast(`⚠ Playback error: ${name}`);
    }
    _reportClientError({
      kind:    "play_rejected",
      reason:  reason || "",
      err:     name,
      message: err?.message || "",
      ua:      navigator.userAgent.slice(0, 120),
    });
  });
}

// ── MediaSession — lock screen / CarPlay controls + metadata ──
function _updateMediaSession(t, idx){
  if(!("mediaSession" in navigator)) return;
  // Artwork: proxy through gateway so iOS can load it (cross-origin art URLs fail on lock screen)
  const artSrc = t.art ? `/art?url=${encodeURIComponent(t.art)}` : null;
  const artwork = artSrc ? [{src: artSrc, sizes: "512x512", type: "image/jpeg"}] : [];
  navigator.mediaSession.metadata = new MediaMetadata({
    title:  t.title  || "Unknown track",
    artist: t.artist || "",
    album:  t.album  || "",
    artwork,
  });
  // Transport handlers — registered every track so iOS keeps them active
  navigator.mediaSession.setActionHandler("play", ()=>{
    _playBrowserAudio("mediasession_play");
    $("btn-pp").textContent="⏸ Pause";$("mini-pp").textContent="⏸";
  });
  navigator.mediaSession.setActionHandler("pause", ()=>{
    browserAudio.pause();
    $("btn-pp").textContent="▶ Play";$("mini-pp").textContent="▶";
  });
  navigator.mediaSession.setActionHandler("previoustrack", ()=>{
    if(browserIdx>0){browserIdx--;_browserPlayIdx(browserIdx);}
    else{browserAudio.currentTime=0;}
  });
  navigator.mediaSession.setActionHandler("nexttrack", ()=>{
    if(browserIdx<browserQueue.length-1){browserIdx++;_browserPlayIdx(browserIdx);}
  });
  // Position state — enables lock screen / CarPlay scrubber
  const dur = browserAudio.duration;
  if(dur && !isNaN(dur) && dur > 0){
    try{
      navigator.mediaSession.setPositionState({
        duration:     dur,
        playbackRate: browserAudio.playbackRate || 1,
        position:     Math.min(browserAudio.currentTime, dur),
      });
    }catch(e){}
  }
}

// Keep position state in sync as playback progresses
browserAudio.addEventListener("timeupdate",()=>{
  if(!("mediaSession" in navigator)) return;
  const dur=browserAudio.duration;
  if(!dur||isNaN(dur)) return;
  try{
    navigator.mediaSession.setPositionState({
      duration:     dur,
      playbackRate: browserAudio.playbackRate,
      position:     Math.min(browserAudio.currentTime, dur),
    });
  }catch(e){}
});

function _browserPlayIdx(idx){
  const t=browserQueue[idx];if(!t)return;
  browserAudio.src=`/stream?url=${enc(t.url)}`;
  _playBrowserAudio("queue_advance");
  $("np-title").textContent=t.title||"";
  $("np-artist").textContent=t.artist||"";
  $("np-album").textContent=t.album||"";
  $("np-meta").textContent=`Track ${idx+1} of ${browserQueue.length}`;
  if(t.art){$("art").innerHTML=`<img src="${artUrl(t.art)}" style="width:100%;height:100%;object-fit:cover;border-radius:12px" onerror="this.parentElement.textContent='💿'">`;}
  else{$("art").textContent="💿";}
  $("player").className="playing is-audio";
  $("btn-pp").textContent="⏸ Pause";
  $("hdr-status").textContent=t.title||"";
  setNpTrack(t);
  _updateMiniPlayer(t);
  // Update lock screen / CarPlay metadata
  // "playing" fires after iOS activates the audio session — the right moment for MediaSession
  browserAudio.addEventListener("playing",     ()=>_updateMediaSession(t,idx), {once:true});
  browserAudio.addEventListener("loadedmetadata", ()=>_updateMediaSession(t,idx), {once:true});
  _updateMediaSession(t, idx);  // also call immediately as a best-effort
}

function _updateMiniPlayer(t){
  $("mini-title").textContent=t?.title||"Nothing playing";
  $("mini-artist").textContent=t?.artist||"";
  if(t?.art){$("mini-art").innerHTML=`<img src="${artUrl(t.art)}" style="width:100%;height:100%;object-fit:cover;border-radius:6px" onerror="this.parentElement.textContent='🎵'">`;}
  else{$("mini-art").textContent=t?"💿":"🎵";}
  // Pulse the now-playing nav icon when something is playing
  const icon=$("bnav-np-icon");
  if(icon) icon.textContent=t?"▶":"🎵";
}

// ── Mobile navigation ─────────────────────────────────────────────
const _mobileClasses=["m-browse","m-search","m-pl","m-fav","m-np"];
function mobileTab(tab){
  // Only applies on mobile (bottom nav visible)
  if(!$("bottom-nav").offsetParent&&$("bottom-nav").style.display==="none")return;
  document.body.classList.remove(..._mobileClasses);
  ["browse","search","playlists","favourites","nowplaying"].forEach(t=>
    $("bnav-"+t)?.classList.toggle("active",t===tab));
  // Show/hide back button on Now Playing
  const nb=$("np-back");if(nb)nb.style.display=tab==="nowplaying"?"":"none";
  if(tab==="nowplaying"){document.body.classList.add("m-np");}
  else if(tab==="playlists"){document.body.classList.add("m-pl");showTab("playlists");}
  else if(tab==="favourites"){document.body.classList.add("m-fav");showTab("favourites");}
  else if(tab==="search"){document.body.classList.add("m-search");showTab("search");}
  else{document.body.classList.add("m-browse");showTab("browse");}
}


// ── Tabs ──────────────────────────────────────────────────────────
function showTab(tab){
  curTab=tab;
  // Only browse/search exist as desktop tab-bar buttons; playlists/favourites
  // are mobile-only (#bnav-*). Optional-chain so calling showTab("playlists")
  // from mobileTab() doesn't crash on the desktop layout.
  ["browse","search","playlists","favourites"].forEach(t=>{
    $("tab-"+t)?.classList.toggle("active",t===tab);
  });
  const isBrowse = tab==="browse";
  $("browse-modes").style.display = (isBrowse && !drillArtist && !drillAlbum) ? "" : "none";
  $("letter-bar").style.display   = (isBrowse && !drillArtist && !drillAlbum) ? "" : "none";
  if(tab==="browse"){
    // Entering Browse always returns to the ROOT (artist list + letter bar).
    // Without resetting the drill state, re-tapping Browse after drilling into
    // an album (e.g. on returning from Now Playing) left drillArtist/drillAlbum
    // set: loadBrowsePage hid the back button while letter-bar/browse-modes
    // stayed hidden too — no nav chrome at all, stuck on the album.
    if(curServer) { buildLetterBar(); exitDrillDown(true); }
  } else if(tab==="playlists"){
    loadPlaylists();
  } else if(tab==="favourites"){
    showFavourites();
  }
}

// ── Servers (source) ─────────────────────────────────────────────
// Multiple music servers can now coexist (AssetUPnP + the in-process
// LocalFs backend, plus any future Plex/Jellyfin provider). The header
// carries a SRC dropdown (#source-sel) to switch the active source; the
// disc-dot still shows the active source's online status.
let renderers = {};   // udn → MediaRenderer
async function refreshServers(){
  const r=await api("/api/servers");if(!r)return;
  const data=await r.json();
  servers={};data.forEach(s=>servers[s.udn]=s);
  // Adopt the first known server on first sight; otherwise keep the
  // user's chosen source and just refresh its online flag etc.
  if(!curServer && data.length){
    curServer=data[0];
    if(curTab==="browse") showArtists();
  } else if(curServer && servers[curServer.udn]){
    curServer=servers[curServer.udn];   // refresh online flag etc.
  }
  rebuildSourceSel(data);
  updateDiscStatus();
}

// Server status now lives entirely in the SRC dropdown (name + online
// state per option), so the standalone disc-dot/label was removed from
// the header. Kept as a guarded no-op so callers don't need to change
// and a future status indicator can slot back in here.
function updateDiscStatus(){
  const dot=$("disc-dot"),lbl=$("disc-label");
  if(!dot||!lbl) return;
  const s=curServer && servers[curServer.udn];
  if(!s){
    dot.className="disc-dot";
    lbl.textContent="Scanning…";
    return;
  }
  dot.className = s.online ? "disc-dot on" : "disc-dot";
  const status = s.online ? "online" : "offline";
  lbl.textContent=`${s.name} · ${status}${s.tracks?" · "+s.tracks.toLocaleString()+" tracks":""}`;
}

// Populate the SRC dropdown with every known music server.
function rebuildSourceSel(data){
  const sel=$("source-sel");
  if(!sel) return;
  if(!data.length){
    sel.innerHTML=`<option value="">Scanning…</option>`;
    return;
  }
  sel.innerHTML=data.map(s=>{
    const icon=s.udn.startsWith("uuid:localfs-")?"💾":"🗄";
    const dim=s.online?"":" (offline)";
    const cnt=s.tracks?` · ${s.tracks.toLocaleString()} tracks`:"";
    const acnt=s.albums?` · ${s.albums.toLocaleString()} albums`:"";
    return `<option value="${esc(s.udn)}">${icon} ${esc(s.name)}${cnt}${acnt}${dim}</option>`;
  }).join("");
  if(curServer) sel.value=curServer.udn;
}

// Switch the active library source from the SRC dropdown.
function selectSource(udn){
  const s=servers[udn];
  if(!s || (curServer && curServer.udn===udn)) return;
  curServer=s;
  updateDiscStatus();
  // Reset browse navigation and reload the new source's library.
  if(typeof exitDrillDown==="function") exitDrillDown(false);
  browseLetter="A"; browseOffset=0;
  if(curTab==="search"){
    const q=$("search-input").value.trim();
    if(q){ doSearch(q); return; }
  }
  showTab("browse");
  showArtists();
}

// ── Renderers (output) ────────────────────────────────────────────
async function refreshRenderers(){
  const r=await api("/api/renderers");if(!r)return;
  const data=await r.json();
  renderers={};data.forEach(rd=>renderers[rd.udn]=rd);
  rebuildOutputSel(data);
}

// ── Browse ────────────────────────────────────────────────────────
let browsing=false;
// ── Artists view (SQLite — default startup view) ──────────────────
// ── SQLite Browse system ─────────────────────────────────────────
let browseMode   = "artists";   // artists | albums | tracks
let browseLetter = "A";
let browseOffset = 0;
const BROWSE_LIMIT = 100;
// "⭐" (first) shows favourited albums; the rest are the A–Z initials.
const LETTERS = ["⭐","#","0","A","B","C","D","E","F","G","H","I","J","K","L","M",
                 "N","O","P","Q","R","S","T","U","V","W","X","Y","Z"];

// drill-down navigation stack
// Each entry: {type, label, fn}  where fn() re-renders that level
// Level 0 is always the letter/mode view (not pushed — it's the base)
// Level 1: artist albums   {type:'artist', artist, label}
// Level 2: album tracks    {type:'album',  artist, album, label}
let browseNavStack = [];
// Legacy aliases — kept so showTab() / showArtists() still work
let drillArtist = null;
let drillAlbum  = null;

function showArtists(){
  if(!curServer) return;
  exitDrillDown(false);
  $("browse-modes").style.display = "";
  $("letter-bar").style.display   = "";
  setBrowseMode("artists", false);
}

function setBrowseMode(mode, resetLetter=true){
  browseMode = mode;
  ["artists","albums","tracks","genres","decades"].forEach(m=>{
    $("bmode-"+m).classList.toggle("active", m===mode);
  });
  // Genres + decades use their own short lists — no letter picker.
  const hasLetters = mode !== "genres" && mode !== "decades";
  $("letter-bar").style.display = hasLetters ? "" : "none";
  if(!hasLetters){ buildLetterBar(); }  // still build for when switching back
  if(resetLetter){ browseLetter="A"; browseOffset=0; }
  buildLetterBar();
  loadBrowsePage();
}

function setLetter(l){
  browseLetter = l;
  browseOffset = 0;
  buildLetterBar();
  loadBrowsePage();
}

function buildLetterBar(){
  const bar = $("letter-bar");
  bar.innerHTML = "";
  LETTERS.forEach(l=>{
    const b = document.createElement("button");
    b.className = "letter-btn" + (l===browseLetter?" active":"");
    b.textContent = l;
    b.onclick = ()=>setLetter(l);
    bar.appendChild(b);
  });
}

async function loadBrowsePage(){
  if(!curServer) return;
  $("item-list").innerHTML = '<div class="spinner-wrap"><div class="spinner"></div></div>';
  $("browse-back").style.display        = "none";
  $("browse-section-hdr").style.display = "none";
  $("browse-pager").classList.add("hidden");

  // Genres: load full list without letter filter
  if(browseMode === "genres"){
    const r = await api(`/api/genres?udn=${enc(curServer.udn)}`);
    if(!r){ $("item-list").innerHTML='<div class="msg">Could not load genres.</div>'; return; }
    const genres = await r.json();
    if(!genres.length){ $("item-list").innerHTML='<div class="msg">No genres in library.<br><small>Rebuild index to pick up genre tags.</small></div>'; return; }
    renderGenreList(genres);
    return;
  }

  // Decades: same shape as genres. Year comes from MB original year
  // (preferred) falling back to file-tag year; tracks with no year at
  // all are excluded server-side.
  if(browseMode === "decades"){
    const r = await api(`/api/decades?udn=${enc(curServer.udn)}`);
    if(!r){ $("item-list").innerHTML='<div class="msg">Could not load decades.</div>'; return; }
    const decades = await r.json();
    if(!decades.length){
      $("item-list").innerHTML='<div class="msg">No year metadata yet.<br><small>Run a rebuild-index (for file-tag year) and the AcoustID year backfill (for MusicBrainz original year).</small></div>';
      return;
    }
    renderDecadeList(decades);
    return;
  }

  // ⭐ — favourited albums (the star at the front of the letter bar).
  // Replaces the removed right-column Favourite Albums view; folder-keyed.
  if(browseLetter==="⭐"){
    const r = await api("/api/album_favourites");
    if(!r){ $("item-list").innerHTML='<div class="msg">Could not load favourites.</div>'; return; }
    renderFavouriteAlbums(await r.json());
    return;
  }

  const url = `/api/browse_letter?udn=${enc(curServer.udn)}&mode=${browseMode}&letter=${enc(browseLetter)}&offset=${browseOffset}&limit=${BROWSE_LIMIT}`;
  const r = await api(url);
  if(!r){ $("item-list").innerHTML='<div class="msg">Could not load library.</div>'; return; }
  const data = await r.json();
  if(!data.items||!data.items.length){
    $("item-list").innerHTML = `<div class="msg">No ${browseMode} starting with "${browseLetter}"</div>`;
    return;
  }
  renderBrowseItems(data.items);
  updatePager(data.total, data.offset, data.limit);
}

function renderFavouriteAlbums(favs){
  const list = $("item-list");
  list.innerHTML = "";
  if(!favs.length){
    list.innerHTML='<div class="msg">No favourite albums yet — open an album and tap the ☆ in its header.</div>';
    return;
  }
  favs.forEach(a=>{
    const div = document.createElement("div");
    div.className = "row";
    const artEl = a.art
      ? `<img src="/art?url=${encodeURIComponent(a.art)}" style="width:36px;height:36px;object-fit:cover;border-radius:4px;flex-shrink:0" onerror="this.style.display='none'">`
      : `<div class="row-icon">💿</div>`;
    const _artist = a.artist==="Various Artists"?"":a.artist;
    const _ak = a.album_key||"";
    div.innerHTML = `${artEl}<div class="row-body"><div class="row-title">${esc(a.album)}</div><div class="row-sub">${esc(a.artist||"")}${a.track_count?` · ${a.track_count} tracks`:""}</div></div><div class="row-actions"><button class="icon-btn" title="Play album">▶</button></div>`;
    div.querySelector(".icon-btn").addEventListener("click", e=>{e.stopPropagation(); playAlbumFromDB(_artist, a.album, _ak);});
    div.addEventListener("click", ()=>showAlbumTracks(_artist, a.album, null, _ak));
    list.appendChild(div);
  });
}

function renderBrowseItems(items){
  const list = $("item-list");
  list.innerHTML = "";
  items.forEach(item=>{
    const div = document.createElement("div");
    div.className = "row";
    const artEl = item.art
      ? `<img src="/art?url=${encodeURIComponent(item.art)}" style="width:36px;height:36px;object-fit:cover;border-radius:4px;flex-shrink:0" onerror="this.style.display='none'">`
      : `<div class="row-icon">${browseMode==="tracks"?"🎵":browseMode==="albums"?"💿":"👤"}</div>`;

    if(browseMode==="artists"){
      div.innerHTML = `${artEl}<div class="row-body"><div class="row-title">${esc(item.artist)}</div><div class="row-sub">${item.album_count} album${item.album_count!==1?"s":""} · ${item.track_count} tracks</div></div>`;
      div.addEventListener("click", ()=>showArtistAlbums(item));
    } else if(browseMode==="albums"){
      div.innerHTML = `${artEl}<div class="row-body"><div class="row-title">${esc(item.album)}</div><div class="row-sub">${esc(item.artist)} · ${item.track_count} tracks</div></div><div class="row-actions"><button class="icon-btn" title="Play album">▶</button></div>`;
      const _artist = item.artist==="Various Artists"?"":item.artist;
      const _ak = item.album_key||"";
      div.querySelector(".icon-btn").addEventListener("click", e=>{e.stopPropagation(); playAlbumFromDB(_artist, item.album, _ak);});
      div.addEventListener("click", ()=>showAlbumTracks(_artist, item.album, null, _ak));
    } else {
      // tracks
      const k = regItem(item);
      div.innerHTML = `${artEl}<div class="row-body"><div class="row-title">${esc(item.title)}</div><div class="row-sub">${esc([item.artist,item.album].filter(Boolean).join(" · "))}</div></div>${item.duration?`<div class="row-dur">${fmtDur(item.duration)}</div>`:""}<div class="row-actions"><button class="icon-btn" title="Add to Favourites" data-k="${k}" data-action="fav">⭐</button><button class="icon-btn" title="Add to playlist…" data-k="${k}" data-action="add">＋</button><button class="icon-btn" title="Edit metadata" data-k="${k}" data-action="edit">✏️</button><button class="icon-btn" data-k="${k}" data-action="play">▶</button></div>`;
      div.addEventListener("click", e=>{
        const btn=e.target.closest("[data-action]");
        if(btn){ e.stopPropagation(); const it=itemRegistry.get(Number(btn.dataset.k)); if(!it)return;
          if(btn.dataset.action==="fav") addToPlaylist("__favourites__",it);
          else if(btn.dataset.action==="add") showAddToPlaylistForItem(e,it);
          else if(btn.dataset.action==="edit") openEditModal(it);
          else startPlay(it, div); return; }
        startPlay(item, div);
      });
    }
    list.appendChild(div);
  });
}

function renderGenreList(genres){
  const list = $("item-list");
  list.innerHTML = "";
  genres.forEach(g=>{
    const div = document.createElement("div");
    div.className = "row";
    div.innerHTML = `<div class="row-icon">🎼</div><div class="row-body"><div class="row-title">${esc(g.genre)}</div><div class="row-sub">${g.album_count} album${g.album_count!==1?"s":""} · ${g.track_count} tracks</div></div>`;
    div.addEventListener("click", ()=>showGenreAlbums(g));
    list.appendChild(div);
  });
}

function renderDecadeList(decades){
  const list = $("item-list");
  list.innerHTML = "";
  decades.forEach(d=>{
    const div = document.createElement("div");
    div.className = "row";
    const label = `${d.decade}s`;
    div.innerHTML = `<div class="row-icon">📅</div><div class="row-body"><div class="row-title">${label}</div><div class="row-sub">${d.album_count} album${d.album_count!==1?"s":""} · ${d.track_count} tracks</div></div>`;
    div.addEventListener("click", ()=>showDecadeAlbums(d));
    list.appendChild(div);
  });
}

async function showDecadeAlbums(decadeItem){
  if(!curServer) return;
  browseNavStack.push({type:"root", label:"Decades"});
  _showDecadeAlbumsInner(decadeItem);
}

async function _showDecadeAlbumsInner(decadeItem){
  if(!curServer) return;
  $("item-list").innerHTML = '<div class="spinner-wrap"><div class="spinner"></div></div>';
  _drillShowChrome(true);
  $("browse-pager").classList.add("hidden");
  $("browse-back-title").textContent = browseNavStack.length>0
    ? browseNavStack[browseNavStack.length-1].label : "Decades";
  $("browse-section-hdr").style.display = "";
  $("browse-section-title").textContent = `${decadeItem.decade}s · ${decadeItem.album_count} album${decadeItem.album_count!==1?"s":""}`;
  $("browse-play-all").onclick = async ()=>{
    const r=await api(`/api/decade_tracks?udn=${enc(curServer.udn)}&decade=${decadeItem.decade}`);
    if(!r)return; const data=await r.json();
    if(data.tracks && data.tracks.length) await playTracklist(data.tracks, `${decadeItem.decade}s`, "");
  };
  const r = await api(`/api/decade_albums?udn=${enc(curServer.udn)}&decade=${decadeItem.decade}`);
  if(!r){ $("item-list").innerHTML='<div class="msg">Could not load decade albums.</div>'; return; }
  const albums = await r.json();
  const list = $("item-list");
  list.innerHTML = "";
  albums.forEach(a=>{
    const div = document.createElement("div");
    div.className = "row";
    const artEl = a.art
      ? `<img src="/art?url=${encodeURIComponent(a.art)}" style="width:36px;height:36px;object-fit:cover;border-radius:4px;flex-shrink:0" onerror="this.style.display='none'">`
      : `<div class="row-icon">💿</div>`;
    div.innerHTML = `${artEl}<div class="row-body"><div class="row-title">${esc(a.album)}</div><div class="row-sub">${esc(a.artist)} · ${a.track_count} tracks</div></div><div class="row-actions"><button class="icon-btn" title="Play album">▶</button></div>`;
    div.querySelector(".icon-btn").addEventListener("click", e=>{e.stopPropagation(); playAlbumFromDB(a.artist==="Various Artists"?"":a.artist, a.album, a.album_key||"");});
    div.addEventListener("click", ()=>showAlbumTracks(a.artist==="Various Artists"?"":a.artist, a.album, {artist:a.artist, album_count:null}, a.album_key||""));
    list.appendChild(div);
  });
}

async function showGenreAlbums(genreItem){
  if(!curServer) return;
  browseNavStack.push({type:"root", label:"Genres"});
  _showGenreAlbumsInner(genreItem);
}

async function _showGenreAlbumsInner(genreItem){
  if(!curServer) return;
  $("item-list").innerHTML = '<div class="spinner-wrap"><div class="spinner"></div></div>';
  _drillShowChrome(true);
  $("browse-pager").classList.add("hidden");
  $("browse-back-title").textContent = browseNavStack.length>0
    ? browseNavStack[browseNavStack.length-1].label : "Genres";
  $("browse-section-hdr").style.display = "";
  $("browse-section-title").textContent = `${genreItem.album_count} album${genreItem.album_count!==1?"s":""}`;
  $("browse-play-all").onclick = async ()=>{
    const r=await api(`/api/genre_tracks?udn=${enc(curServer.udn)}&genre=${enc(genreItem.genre)}`);
    if(!r)return; const tracks=await r.json();
    if(tracks.length) await playTracklist(tracks, genreItem.genre, "");
  };
  const r = await api(`/api/genre_albums?udn=${enc(curServer.udn)}&genre=${enc(genreItem.genre)}`);
  if(!r){ $("item-list").innerHTML='<div class="msg">Could not load genre albums.</div>'; return; }
  const albums = await r.json();
  const list = $("item-list");
  list.innerHTML = "";
  albums.forEach(a=>{
    const div = document.createElement("div");
    div.className = "row";
    const artEl = a.art
      ? `<img src="/art?url=${encodeURIComponent(a.art)}" style="width:36px;height:36px;object-fit:cover;border-radius:4px;flex-shrink:0" onerror="this.style.display='none'">`
      : `<div class="row-icon">💿</div>`;
    div.innerHTML = `${artEl}<div class="row-body"><div class="row-title">${esc(a.album)}</div><div class="row-sub">${esc(a.artist)} · ${a.track_count} tracks</div></div><div class="row-actions"><button class="icon-btn" title="Play album">▶</button></div>`;
    div.querySelector(".icon-btn").addEventListener("click", e=>{e.stopPropagation(); playAlbumFromDB(a.artist==="Various Artists"?"":a.artist, a.album, a.album_key||"");});
    div.addEventListener("click", ()=>showAlbumTracks(a.artist==="Various Artists"?"":a.artist, a.album, {artist:a.artist, album_count:null}, a.album_key||""));
    list.appendChild(div);
  });
}

function updatePager(total, offset, limit){
  const pager = $("browse-pager");
  if(total<=limit){ pager.classList.add("hidden"); return; }
  pager.classList.remove("hidden");
  const from = offset+1, to = Math.min(offset+limit, total);
  $("pager-info").textContent = `${from}–${to} of ${total}`;
  $("pager-prev").disabled = offset===0;
  $("pager-next").disabled = offset+limit>=total;
  $("pager-prev").onclick = ()=>{ browseOffset=Math.max(0,offset-limit); loadBrowsePage(); };
  $("pager-next").onclick = ()=>{ browseOffset=offset+limit; loadBrowsePage(); };
}

// ── Drill-down: Artist → Albums ───────────────────────────────────
async function showArtistAlbums(artistItem){
  if(!curServer) return;
  // Push the root level onto the stack before drilling in
  // (only push if we're coming from the letter view, not from drillBack)
  if(browseNavStack.length===0 || browseNavStack[browseNavStack.length-1].type!=="artist"){
    browseNavStack.push({type:"root", label:`Artists ${browseLetter}`});
  }
  drillArtist = artistItem.artist;
  drillAlbum  = null;
  _showArtistAlbumsInner(artistItem);
}

async function _showArtistAlbumsInner(artistItem){
  if(!curServer) return;
  $("item-list").innerHTML = '<div class="spinner-wrap"><div class="spinner"></div></div>';
  _drillShowChrome(true);
  $("browse-pager").classList.add("hidden");
  // Back label = one level up in the stack
  $("browse-back-title").textContent = browseNavStack.length>0
    ? browseNavStack[browseNavStack.length-1].label : "";
  $("browse-section-hdr").style.display = "";
  $("browse-section-title").textContent = `${artistItem.album_count||"?"} album${artistItem.album_count!==1?"s":""}`;
  // Play all by this artist — clean endpoint, dedup applied, ordered
  // by album then title.
  $("browse-play-all").onclick = async ()=>{
    const r=await api(`/api/artist_tracks?udn=${enc(curServer.udn)}&artist=${enc(artistItem.artist)}`);
    if(!r) return;
    const d = await r.json();
    const tracks = d.tracks || [];
    if(tracks.length) await playTracklist(tracks, artistItem.artist, artistItem.artist);
  };
  const r = await api(`/api/artist_albums?udn=${enc(curServer.udn)}&artist=${enc(artistItem.artist)}`);
  if(!r){ $("item-list").innerHTML='<div class="msg">Could not load albums.</div>'; return; }
  const albums = await r.json();
  const list = $("item-list");
  list.innerHTML = "";
  albums.forEach(a=>{
    const div = document.createElement("div");
    div.className = "row";
    const artEl = a.art
      ? `<img src="/art?url=${encodeURIComponent(a.art)}" style="width:36px;height:36px;object-fit:cover;border-radius:4px;flex-shrink:0" onerror="this.style.display='none'">`
      : `<div class="row-icon">💿</div>`;
    div.innerHTML = `${artEl}<div class="row-body"><div class="row-title">${esc(a.album)}</div><div class="row-sub">${a.track_count} tracks</div></div><div class="row-actions"><button class="icon-btn" title="Play album">▶</button></div>`;
    div.querySelector(".icon-btn").addEventListener("click", e=>{e.stopPropagation(); playAlbumFromDB(a.artist, a.album, a.album_key||"");});
    div.addEventListener("click", ()=>showAlbumTracks(a.artist, a.album, artistItem, a.album_key||""));
    list.appendChild(div);
  });
}

// ── Drill-down: Album → Tracks ────────────────────────────────────
async function showAlbumTracks(artist, album, artistItem=null, albumKey=""){
  if(!curServer) return;
  // Push the artist-albums level so back returns there
  const backLabel = artistItem ? artistItem.artist : (drillArtist||album);
  const stackEntry = {
    type:"artist",
    artist: artistItem ? artistItem.artist : drillArtist,
    label: browseNavStack.length>0 ? browseNavStack[browseNavStack.length-1].label : `Artists ${browseLetter}`,
    albumCount: artistItem ? artistItem.album_count : null,
    artistItem: artistItem || {artist: drillArtist||artist, album_count: null}
  };
  browseNavStack.push(stackEntry);
  drillAlbum = album;
  $("item-list").innerHTML = '<div class="spinner-wrap"><div class="spinner"></div></div>';
  _drillShowChrome(true);
  $("browse-pager").classList.add("hidden");
  // Back label = artist name (the level we're returning to)
  $("browse-back-title").textContent = backLabel;
  $("browse-section-hdr").style.display = "";
  $("browse-section-title").textContent = esc(artist||"Various Artists");
  $("browse-play-all").onclick = ()=>playAlbumFromDB(artist, album, albumKey);
  // Hide the star until the track count is known so single-track
  // "albums" (orphan metadata-less tracks) never expose it.
  _setAlbumFavStar(false, false);
  const kq = albumKey?`&album_key=${enc(albumKey)}`:"";
  const r = await api(`/api/album_tracks?udn=${enc(curServer.udn)}&artist=${enc(artist)}&album=${enc(album)}${kq}`);
  if(!r){ $("item-list").innerHTML='<div class="msg">Could not load tracks.</div>'; return; }
  const data = await r.json();
  const tracks = data.tracks||[];
  renderListAppend({containers:[], items: tracks});
  if(tracks.length > 1){ _wireAlbumFavStar(artist, album, albumKey); }
}

// ── Album favourite star (album header) ──────────────────────────
function _setAlbumFavStar(visible, isFav){
  const btn = $("browse-fav-album");
  if(!btn) return;
  btn.style.display = visible ? "" : "none";
  btn.dataset.fav = isFav ? "1" : "0";
  btn.textContent = isFav ? "★" : "☆";
  btn.title = isFav ? "Remove from Favourite Albums" : "Add to Favourite Albums";
}

async function _wireAlbumFavStar(artist, album, albumKey=""){
  const btn = $("browse-fav-album");
  if(!btn) return;
  // album_key (LocalFs folder identity) is preferred so a compilation is
  // favourited as one album; falls back to (artist, album).
  const kq = albumKey?`&album_key=${enc(albumKey)}`:"";
  // Show empty by default, then upgrade to filled after the check
  // round-trip — UI is responsive even if the call is slow.
  _setAlbumFavStar(true, false);
  try {
    const r = await api(`/api/album_favourites/check?artist=${enc(artist)}&album=${enc(album)}${kq}`);
    if(!r) return;
    const j = await r.json();
    _setAlbumFavStar(true, !!j.is_favourite);
  } catch(e){ /* leave as ☆ */ }
  btn.onclick = async ()=>{
    const wasFav = btn.dataset.fav === "1";
    // Optimistic flip — feels snappy, reverts on failure.
    _setAlbumFavStar(true, !wasFav);
    const path = wasFav ? "/api/album_favourites/remove" : "/api/album_favourites/add";
    try {
      const r = await api(`${path}?artist=${enc(artist)}&album=${enc(album)}${kq}`);
      if(!r){ _setAlbumFavStar(true, wasFav); return; }
    } catch(e){ _setAlbumFavStar(true, wasFav); }
  };
}

// ── Browse navigation stack helpers ──────────────────────────────

function _drillShowChrome(hasBack){
  $("browse-back").style.display        = hasBack ? "" : "none";
  $("browse-modes").style.display       = hasBack ? "none" : "";
  $("letter-bar").style.display         = (hasBack || browseMode==="genres") ? "none" : "";
}

function _drillUpdateBackLabel(){
  if(browseNavStack.length===0){
    $("browse-back-title").textContent = "";
    return;
  }
  // Label on the back button = the level we are going BACK TO
  const prev = browseNavStack[browseNavStack.length-1];
  $("browse-back-title").textContent = prev.label;
}

// Go back one level in the navigation stack
function drillBack(){
  if(browseNavStack.length===0){
    // Already at root — reload letter view
    drillArtist=null; drillAlbum=null;
    _drillShowChrome(false);
    $("browse-section-hdr").style.display="none";
    loadBrowsePage();
    return;
  }
  const prev = browseNavStack.pop();
  drillAlbum = null;
  if(prev.type==="root"){
    drillArtist=null;
    $("browse-section-hdr").style.display="none";
    _drillShowChrome(false);
    loadBrowsePage();
  } else if(prev.type==="artist"){
    drillArtist=prev.artist;
    drillAlbum=null;
    // Re-render artist albums without pushing to stack again
    _showArtistAlbumsInner(prev);
  } else if(prev.type==="genre"){
    _showGenreAlbumsInner(prev);
  }
}

// Legacy alias — called by showArtists() and showTab()
function exitDrillDown(reload=true){
  browseNavStack=[];
  drillArtist=null; drillAlbum=null;
  $("browse-back").style.display        = "none";
  $("browse-section-hdr").style.display = "none";
  _drillShowChrome(false);
  if(reload) loadBrowsePage();
}

let _browseRetryTimer=null;
let _browseRetries=0;
const _BROWSE_MAX_RETRIES=5;
async function browse(id){
  if(!curServer)return;
  if(browsing)return;  // drop if already in flight
  browsing=true;
  clearTimeout(_browseRetryTimer);
  $("item-list").innerHTML='<div class="spinner-wrap"><div class="spinner"></div></div>';
  try{
    const r=await api(`/api/browse?udn=${enc(curServer.udn)}&id=${enc(id)}`);
    if(!r){
      $("item-list").innerHTML='<div class="msg">Browse failed — check server connection.</div>';
      return;
    }
    const data=await r.json();
    if(data.error){
      if(_browseRetries<_BROWSE_MAX_RETRIES){
        _browseRetries++;
        const delay=_browseRetries*3000;
        $("item-list").innerHTML=`<div class="msg">Library loading… <span style="color:var(--ink-dim);font-size:11px">(attempt ${_browseRetries}/${_BROWSE_MAX_RETRIES})</span></div>`;
        _browseRetryTimer=setTimeout(()=>browse(id),delay);
      } else {
        // Give up — show error with manual retry button
        $("item-list").innerHTML=`<div class="msg">Browse unavailable.<br><span style="color:var(--ink-dim);font-size:11px">${esc(data.error)}</span><br><br><button class="btn" style="font-size:11px;padding:5px 12px" onclick="_browseRetries=0;browse('${id}')">↺ Retry</button></div>`;
      }
      return;
    }
    _browseRetries=0;
    renderBreadcrumb();renderList(data);
  }finally{browsing=false;}
}

function renderBreadcrumb(){
  // breadcrumb element removed — no-op (kept for browse() compatibility)
}

// Item registry — avoids encoding items as JSON in onclick attributes
// which breaks on apostrophes and special characters in track titles.
const itemRegistry = new Map();
let itemRegSeq = 0;
function regItem(item){ const k=itemRegSeq++;itemRegistry.set(k,item);return k; }

function renderList(data,context="browse"){
  const list=$("item-list"),all=[...(data.containers||[]),...(data.items||[])];
  if(!all.length){list.innerHTML='<div class="msg">Empty folder</div>';return;}
  list.innerHTML="";
  all.forEach(item=>{
    const k = regItem(item);
    const div=document.createElement("div");
    const icon=item.type==="container"?"📁":item.type==="video"?"🎬":"🎵";
    const sub=[item.artist,item.album].filter(Boolean).join(" · ");
    div.className="row"+(curItemId===item.id?" active":"");
    const artEl=item.art
      ?`<img src="${artUrl(item.art)}" style="width:36px;height:36px;object-fit:cover;border-radius:4px;flex-shrink:0" onerror="this.style.display='none'">`
      :`<div class="row-icon">${icon}</div>`;

    const isItem = item.type !== "container";
    div.innerHTML=`${artEl}<div class="row-body"><div class="row-title">${esc(item.title)}</div>${sub?`<div class="row-sub">${esc(sub)}</div>`:""}</div>${item.duration?`<div class="row-dur">${fmtDur(item.duration)}</div>`:""}<div class="row-actions">${isItem?`<button class="icon-btn" title="Add to Favourites" data-k="${k}" data-action="fav">⭐</button><button class="icon-btn" title="Add to playlist…" data-k="${k}" data-action="add">＋</button>`:""}<button class="icon-btn" title="${isItem?"Play":"Play album"}" data-k="${k}" data-action="play">▶</button></div>`;

    div.addEventListener("click",e=>{
      const btn=e.target.closest("[data-action]");
      if(btn){
        e.stopPropagation();
        const it=itemRegistry.get(Number(btn.dataset.k));
        if(!it)return;
        if(btn.dataset.action==="fav") addToPlaylist("__favourites__",it);
        else if(btn.dataset.action==="add") showAddToPlaylistForItem(e,it);
        else if(btn.dataset.action==="play"){
          if(it.type==="container") playAlbum(it);
          else startPlay(it,div);
        }
        return;
      }
      if(item.type==="container"){navStack.push({id:item.id,title:item.title});browse(item.id);}
      else startPlay(item,div);
    });
    list.appendChild(div);
  });
}

// ── Search ────────────────────────────────────────────────────────
let searchTimer=null;
$("search-input").addEventListener("input",e=>{
  const q=e.target.value.trim();
  clearTimeout(searchTimer);
  if(!q){if(curTab==="search")$("item-list").innerHTML='<div class="msg">Type to search…</div>';return;}
  // Switch to search tab automatically
  if(curTab!=="search"){showTab("search");}
  searchTimer=setTimeout(()=>doSearch(q),400);
});
$("search-input").addEventListener("keydown",e=>{
  if(e.key==="Enter"){clearTimeout(searchTimer);doSearch(e.target.value.trim());}
});

async function doSearch(q){
  if(!curServer||!q)return;
  $("item-list").innerHTML='<div class="spinner-wrap"><div class="spinner"></div></div>';
  const r=await api(`/api/search?udn=${enc(curServer.udn)}&q=${enc(q)}`);
  if(!r){$("item-list").innerHTML='<div class="msg">Search failed.</div>';return;}
  const data=await r.json();
  if(data.error){$("item-list").innerHTML=`<div class="msg">${esc(data.error)}</div>`;return;}

  const total=(data.tracks||[]).length+(data.albums||[]).length+(data.artists||[]).length;
  if(!total){$("item-list").innerHTML=`<div class="msg">No results for <b>${esc(q)}</b></div>`;return;}

  $("item-list").innerHTML="";

  // Artists section
  if((data.artists||[]).length){
    addSearchSection(`Artists (${data.artists.length})`);
    data.artists.forEach(a=>{
      const k=regItem({id:"artist:"+a.artist,title:a.artist,type:"container",art:a.art||""});
      const div=document.createElement("div");
      div.className="row";
      div.innerHTML=`${a.art?`<img src="${artUrl(a.art)}" style="width:36px;height:36px;object-fit:cover;border-radius:4px;flex-shrink:0" onerror="this.style.display='none'">`:
        `<div class="row-icon">👤</div>`}<div class="row-body"><div class="row-title">${esc(a.artist)}</div><div class="row-sub">${a.album_count} albums · ${a.track_count} tracks</div></div>`;
      div.addEventListener("click",()=>{ showTab('browse'); showArtistAlbums({artist:a.artist, album_count:a.album_count||0}); });
      $("item-list").appendChild(div);
    });
  }

  // Albums section
  if((data.albums||[]).length){
    addSearchSection(`Albums (${data.albums.length})`);
    data.albums.forEach(a=>{
      const pseudo={id:"album:"+a.artist+"/"+a.album,title:a.album,artist:a.artist,type:"album",art:a.art||""};
      const k=regItem(pseudo);
      const div=document.createElement("div");
      div.className="row";
      div.innerHTML=`${a.art?`<img src="${artUrl(a.art)}" style="width:36px;height:36px;object-fit:cover;border-radius:4px;flex-shrink:0" onerror="this.style.display='none'">`:
        `<div class="row-icon">💿</div>`}<div class="row-body"><div class="row-title">${esc(a.album)}</div><div class="row-sub">${esc(a.artist)} · ${a.track_count} tracks</div></div><div class="row-actions"><button class="icon-btn" data-k="${k}" data-action="fav">⭐</button><button class="icon-btn" data-k="${k}" data-action="play">▶</button></div>`;
      div.addEventListener("click",e=>{
        const btn=e.target.closest("[data-action]");
        if(btn){e.stopPropagation();const it=itemRegistry.get(Number(btn.dataset.k));if(!it)return;
          if(btn.dataset.action==="play") playAlbumFromDB(a.artist,a.album,a.album_key||"");
          else if(btn.dataset.action==="fav") addAlbumToPlaylist("__favourites__",a.artist,a.album);
          return;}
        // Row click → drill into tracks (same pattern as the artist's
        // album view). Jump to the browse tab first so the back button
        // and section header chrome are visible for the drill-down.
        // Pass an explicit artistItem so `back` from the track list
        // returns to THIS album's artist, not a stale drillArtist from
        // an earlier browse session.
        showTab('browse');
        showAlbumTracks(a.artist, a.album,
                        {artist: a.artist, album_count: null}, a.album_key||"");
      });
      $("item-list").appendChild(div);
    });
  }

  // Tracks section
  if((data.tracks||[]).length){
    addSearchSection(`Tracks (${data.tracks.length})`);
    renderListAppend({containers:[],items:data.tracks});
  }
}

function addSearchSection(label){
  const h=document.createElement("div");
  h.style.cssText="padding:6px 14px 4px;font-size:10px;color:var(--ink-dim);letter-spacing:.08em;text-transform:uppercase;border-bottom:1px solid var(--border)";
  h.textContent=label;
  $("item-list").appendChild(h);
}

async function playAlbumFromDB(artist,album,albumKey=""){
  if(!curServer)return;
  toast("Loading album…",3000);
  // album_key (folder identity) is preferred for LocalFs so a Various-
  // Artists compilation plays as one album; falls back to (artist,album).
  const kq=albumKey?`&album_key=${enc(albumKey)}`:"";
  const r=await api(`/api/album_tracks?udn=${enc(curServer.udn)}&artist=${enc(artist)}&album=${enc(album)}${kq}`);
  if(!r){toast("Failed to load album");return;}
  const data=await r.json();
  if(!data.tracks||!data.tracks.length){toast("No tracks found for this album");return;}
  await playTracklist(data.tracks, album, artist);
}

// POST a queue to a UPnP renderer. Handles the server's 409 "renderer busy"
// response by prompting the user to take over an existing session.
// Returns true on success, false if user declined or request failed.
async function sendRenderQueue(udn, tracks){
  const post=(force)=>api("/api/render_queue",{method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({udn, tracks, force})});
  let r=await post(false);
  if(!r){toast("Failed to reach renderer");return false;}
  if(r.status===409){
    const d=await r.json().catch(()=>({}));
    const bw=d.busy_with||{};
    const what=bw.title?`"${bw.title}"${bw.artist?" — "+bw.artist:""}`:"another session";
    const rname=renderers[udn]?.name||udn;
    if(!confirm(`${rname} is already playing ${what}.\n\nTake over and replace the current queue?`)){
      return false;
    }
    r=await post(true);
    if(!r){toast("Failed to reach renderer");return false;}
  }
  const d=await r.json().catch(()=>({}));
  if(d.error){toast("Error: "+d.error);return false;}
  return true;
}

// Central function: play a list of track objects via whatever output is selected
async function playTracklist(tracks, title, artist){
  _exitRadioMode();   // a normal queue replaces any radio session
  // Apply shuffle before anything else so art/title reflect actual first track
  if(shuffleEnabled) tracks=shuffle(tracks);
  const out=$("output-sel").value;
  const first=tracks[0];

  // Update now-playing panel
  $("np-title").textContent=title||first?.title||"";
  $("np-artist").textContent=artist||first?.artist||"";
  $("np-album").textContent=first?.album||"";
  $("np-meta").textContent=`${tracks.length} tracks`;
  setNpTrack(first||null);
  if(first?.art){$("art").innerHTML=`<img src="${artUrl(first.art)}" style="width:100%;height:100%;object-fit:cover;border-radius:12px" onerror="this.parentElement.textContent='💿'">`;}
  else{$("art").textContent="💿";}
  $("player").className="playing is-audio";
  $("btn-pp").textContent="⏸ Pause";
  $("hdr-status").textContent=title||"";
  activeDevice=out;

  if(out==="browser"){
    browserQueue=tracks;browserIdx=0;
    _browserPlayIdx(0);
    toast(`▶ Playing ${tracks.length} tracks in browser`);
  } else {
    // UPnP renderer
    const rendUdn=out.replace("upnp:","");
    if(!await sendRenderQueue(rendUdn, tracks)) return;
    const rname=renderers[rendUdn]?.name||rendUdn;
    toast(`▶ Playing ${tracks.length} tracks on ${rname}`);
  }
}

async function addAlbumToPlaylist(plId,artist,album){
  if(!curServer)return;
  const r=await api(`/api/album_tracks?udn=${enc(curServer.udn)}&artist=${enc(artist)}&album=${enc(album)}`);
  if(!r)return;
  const data=await r.json();
  if(!data.tracks)return;
  let added=0;
  for(const t of data.tracks){
    const params=new URLSearchParams({pl:plId,url:t.url,title:t.title||"",artist:t.artist||"",album:t.album||"",duration:t.duration||"",art:t.art||""});
    const res=await api("/api/playlist/add?"+params.toString());
    if(res){const d=await res.json();if(d.ok&&!d.duplicate)added++;}
  }
  await loadPlaylists();
  const plName=playlists.find(p=>p.id===plId)?.name||plId;
  toast(`✓ Added ${added} tracks to ${plName}`);
}

function renderListAppend(data){
  const list=$("item-list"),all=[...(data.containers||[]),...(data.items||[])];
  // Clear spinner before appending — spinner is set by the caller before the fetch
  if(list.querySelector(".spinner-wrap")) list.innerHTML="";
  all.forEach(item=>{
    const k=regItem(item);
    const div=document.createElement("div");
    const icon=item.type==="container"?"📁":item.type==="video"?"🎬":"🎵";
    const sub=[item.artist,item.album].filter(Boolean).join(" · ");
    div.className="row"+(curItemId===item.id?" active":"");
    const artEl=item.art
      ?`<img src="${artUrl(item.art)}" style="width:36px;height:36px;object-fit:cover;border-radius:4px;flex-shrink:0" onerror="this.style.display='none'">`
      :`<div class="row-icon">${icon}</div>`;
    const isItem=item.type!=="container";
    div.innerHTML=`${artEl}<div class="row-body"><div class="row-title">${esc(item.title)}</div>${sub?`<div class="row-sub">${esc(sub)}</div>`:""}</div>${item.duration?`<div class="row-dur">${fmtDur(item.duration)}</div>`:""}<div class="row-actions">${isItem?`<button class="icon-btn" data-k="${k}" data-action="fav" title="Add to Favourites">⭐</button><button class="icon-btn" data-k="${k}" data-action="add" title="Add to playlist">＋</button><button class="icon-btn" data-k="${k}" data-action="edit" title="Edit metadata">✏️</button>`:""}<button class="icon-btn" data-k="${k}" data-action="play">▶</button></div>`;
    div.addEventListener("click",e=>{
      const btn=e.target.closest("[data-action]");
      if(btn){
        e.stopPropagation();
        const it=itemRegistry.get(Number(btn.dataset.k));
        if(!it)return;
        if(btn.dataset.action==="fav") addToPlaylist("__favourites__",it);
        else if(btn.dataset.action==="add") showAddToPlaylistForItem(e,it);
        else if(btn.dataset.action==="edit") openEditModal(it);
        else if(btn.dataset.action==="play") startPlay(it,div);
        return;
      }
      if(isItem) startPlay(item,div);
    });
    list.appendChild(div);
  });
}

// ── Playlists panel ───────────────────────────────────────────────
async function loadPlaylists(){
  const r=await api("/api/playlists");if(!r)return;
  playlists=await r.json();
}

async function showPlaylists(){
  await loadPlaylists();
  $("pl-panel-title").textContent="Playlists";
  $("pl-back-btn").style.display="none";
  $("pl-list").style.display="";
  $("pl-tracks").classList.remove("visible");
  $("pl-actions").innerHTML=`<button class="btn" style="font-size:11px;padding:5px 10px" onclick="newPlaylist()">+ New playlist</button>`;
  const list=$("pl-list");
  list.innerHTML="";
  // NOTE: the "⭐ Favourite Albums" browse view was removed (2026-06-01) —
  // its (artist, album) entries didn't survive the LocalFs folder-album
  // migration. The ⭐ star on the album header still adds/removes
  // favourites (folder-keyed); they're exposed via UPnP/Subsonic.
  // Synthetic row: internet-radio stations ("📡 Stations").
  const favRadio=document.createElement("div");
  favRadio.id="radio-pl-item";
  favRadio.className="pl-item";
  favRadio.innerHTML=`<div class="pl-item-name">📡 Radio Stations</div><div class="pl-item-count">internet radio</div>`;
  favRadio.addEventListener("click",showRadioStations);
  list.appendChild(favRadio);
  // Synthetic row: videos ("📹 Videos") — the GWMovies library.
  const vidRow=document.createElement("div");
  vidRow.id="videos-pl-item";
  vidRow.className="pl-item";
  vidRow.innerHTML=`<div class="pl-item-name">📹 Videos</div><div class="pl-item-count">movies</div>`;
  vidRow.addEventListener("click",showVideos);
  list.appendChild(vidRow);
  if(!playlists.length){
    const msg=document.createElement("div");
    msg.className="msg";msg.style.padding="12px 20px 20px";
    msg.textContent="No playlists yet";
    list.appendChild(msg);
    return;
  }
  playlists.forEach(pl=>{
    const div=document.createElement("div");
    div.className="pl-item"+(curPlId===pl.id?" active":"");
    div.innerHTML=`<div class="pl-item-name">${esc(pl.name)}</div><div class="pl-item-count">${pl.count} tracks</div>`;
    div.addEventListener("click",()=>openPlaylist(pl.id));
    list.appendChild(div);
  });
}

// (The "⭐ Favourite Albums" right-column browse view + its drill-in
// were removed 2026-06-01 — their (artist, album) entries didn't survive
// the LocalFs folder-album migration. The ⭐ star on the album header
// still toggles favourites; UPnP/Subsonic still expose them.)

// ── Videos ("📹 Videos") ──────────────────────────────────────────
// Browse the GWMovies library + play in a <video> modal. Uses the SAME-ORIGIN
// /video/<id> (mixed-content-safe over HTTPS); the LG TV uses DLNA instead.
function _vidDur(sec){
  sec=parseInt(sec||0,10); if(!sec||sec<0) return "";
  const h=Math.floor(sec/3600), m=Math.floor(sec%3600/60), s=sec%60;
  return h?`${h}:${String(m).padStart(2,"0")}:${String(s).padStart(2,"0")}`
          :`${m}:${String(s).padStart(2,"0")}`;
}

async function showVideos(){
  curPlId="__videos__";
  $("pl-panel-title").textContent="📹 Videos";
  $("pl-back-btn").style.display="";
  $("pl-list").style.display="none";
  $("pl-tracks").classList.add("visible");
  $("pl-actions").innerHTML="";
  $("pl-tracks").innerHTML=`<div id="video-list" style="padding:6px"></div>`;
  let vids=[];
  const r=await api("/api/videos");
  if(r&&r.ok){ try{ vids=await r.json(); }catch{} }
  renderVideos(vids);
}

function renderVideos(vids){
  const box=$("video-list"); if(!box) return;
  if(!vids||!vids.length){
    box.innerHTML=`<div class="msg" style="padding:16px">No videos found.</div>`;
    return;
  }
  box.innerHTML="";
  vids.forEach(v=>{
    const row=document.createElement("div");
    row.className="video-row"; row.dataset.id=v.id;
    row.style.cssText="display:flex;gap:10px;align-items:center;padding:8px;"
      +"border-bottom:1px solid var(--border);cursor:pointer";
    const thumb=v.posterUrl
      ? `<img src="${esc(v.posterUrl)}" loading="lazy" style="width:96px;height:54px;`
        +`object-fit:cover;background:#000;border-radius:4px;flex:none">`
      : `<div style="width:96px;height:54px;display:flex;align-items:center;`
        +`justify-content:center;background:var(--raised);border-radius:4px;flex:none">📹</div>`;
    const sub=[v.folder,_vidDur(v.duration),
               (v.width&&v.height?`${v.width}×${v.height}`:"")].filter(Boolean).join(" · ");
    row.innerHTML=`${thumb}<div style="min-width:0">`
      +`<div style="font-size:13px;color:var(--ink);overflow:hidden;`
      +`text-overflow:ellipsis;white-space:nowrap">${esc(v.title||"")}</div>`
      +`<div style="font-size:11px;color:var(--ink-dim)">${esc(sub)}</div></div>`;
    row.addEventListener("click",()=>playVideo(v));
    box.appendChild(row);
  });
}

let _curVid=null;
function playVideo(v){
  const modal=$("video-modal"), player=$("video-player");
  if(!modal||!player) return;
  _curVid=v;
  $("video-modal-title").textContent="📹 "+(v.title||"Video");
  player.dataset.triedTranscode="0";
  modal.classList.add("open");
  // Containers NO browser <video> supports → transcode immediately (canPlayType
  // is useless — Chromium says "maybe" even for MKV). For mp4/mov/webm, play
  // native first (Safari does HEVC; H.264 plays everywhere); the 'error' handler
  // below falls back to the (seekable) transcode for codecs it can't decode.
  const FORCE_TRANSCODE=["video/x-matroska","video/x-msvideo","video/mp2t"];
  if(FORCE_TRANSCODE.includes((v.mime||"").toLowerCase())){
    player.dataset.triedTranscode="1";
    playTranscoded(v);
  } else {
    _detachHls();
    player.dataset.mode="native";
    player.src=v.playUrl;
    player.play().catch(()=>{});
  }
}

// Seekable transcode via on-demand HLS: Safari plays the .m3u8 natively;
// Chrome/FF use hls.js (MSE). Each ~6s segment is transcoded on demand, so
// seeking just fetches that segment. Falls back to the progressive (non-
// seekable) /video_transcode stream if HLS is unavailable.
function playTranscoded(v){
  const player=$("video-player");
  _detachHls();
  if(v.hlsUrl && player.canPlayType("application/vnd.apple.mpegurl")){
    player.dataset.mode="native-hls";
    player.src=v.hlsUrl;
    player.play().catch(()=>{});
  } else if(v.hlsUrl && window.Hls && window.Hls.isSupported()){
    player.dataset.mode="hls";
    const hls=new Hls({enableWorker:true});
    player._hls=hls;
    hls.loadSource(v.hlsUrl);
    hls.attachMedia(player);
    hls.on(Hls.Events.MANIFEST_PARSED, ()=>player.play().catch(()=>{}));
  } else if(v.transcodeUrl){
    player.dataset.mode="progressive";   // plays, but not seekable
    player.src=v.transcodeUrl;
    player.play().catch(()=>{});
  }
}

function _detachHls(){
  const player=$("video-player");
  if(player && player._hls){ try{ player._hls.destroy(); }catch{} player._hls=null; }
}

function closeVideo(){
  const modal=$("video-modal"), player=$("video-player");
  _detachHls();
  if(player){ player.pause(); player.removeAttribute("src"); player.load(); }
  if(modal) modal.classList.remove("open");
}

// ── Internet radio ("📡 Stations") ───────────────────────────────
const RADIO_GENRES=["Prog","Prog-rock","Jazz","Pop","Rock","Classical"];

// Open the right-column radio view: search box + genre chips + list.
async function showRadioStations(){
  curPlId="__radio__";
  $("pl-panel-title").textContent="📡 Radio Stations";
  $("pl-back-btn").style.display="";
  $("pl-list").style.display="none";
  $("pl-tracks").classList.add("visible");
  $("pl-actions").innerHTML="";
  $("pl-tracks").innerHTML=`
    <div style="padding:8px 10px;display:flex;flex-direction:column;gap:6px;border-bottom:1px solid var(--border)">
      <div style="display:flex;align-items:center;gap:6px">
        <input id="radio-search" type="text" placeholder="🔍 Search stations…" autocomplete="off"
          style="flex:1;background:var(--raised);border:1px solid var(--border);color:var(--ink);font-family:var(--fm);font-size:12px;padding:6px 8px;border-radius:var(--r);outline:none">
        <span id="radio-cap" style="font-size:11px;color:var(--ink-dim);white-space:nowrap"></span>
      </div>
      <div id="radio-genre-chips" style="display:flex;flex-wrap:wrap;gap:4px"></div>
    </div>
    <div id="radio-list"></div>`;
  $("radio-search").addEventListener("input",e=>{
    clearTimeout(_radioSearchTimer);
    const q=e.target.value.trim();
    _radioSearchTimer=setTimeout(()=>radioSearch({q}),350);
  });
  renderGenreChips();
  await renderRadioFavourites();
}

function renderGenreChips(){
  const box=$("radio-genre-chips");
  if(!box) return;
  box.innerHTML="";
  RADIO_GENRES.forEach(g=>{
    const tag=g.toLowerCase();
    const chip=document.createElement("button");
    chip.className="radio-chip";
    chip.dataset.tag=tag;
    chip.dataset.active="0";
    chip.textContent=g;
    chip.style.cssText="font-size:11px;padding:3px 9px;border-radius:11px;cursor:pointer;"
      +"font-family:var(--fm);background:var(--raised);border:1px solid var(--border);color:var(--ink-dim)";
    chip.addEventListener("click",()=>{
      _radioActiveTag=(_radioActiveTag===tag)?"":tag;
      _markActiveChip();
      const inp=$("radio-search"); if(inp) inp.value="";
      if(_radioActiveTag) radioSearch({tag:_radioActiveTag});
      else renderRadioFavourites();
    });
    box.appendChild(chip);
  });
  _markActiveChip();
}

function _markActiveChip(){
  document.querySelectorAll(".radio-chip").forEach(c=>{
    const on=c.dataset.tag===_radioActiveTag;
    c.dataset.active=on?"1":"0";
    c.style.background=on?"var(--amber)":"var(--raised)";
    c.style.color=on?"#000":"var(--ink-dim)";
  });
}

// Search the radio-browser catalogue. q (name) and tag (genre chip)
// combine; with neither set, fall back to the favourites list.
async function radioSearch({q="",tag=""}={}){
  const inp=$("radio-search");
  q   = q   || (inp?inp.value.trim():"");
  tag = tag || _radioActiveTag;
  if(!q && !tag){ renderRadioFavourites(); return; }
  const list=$("radio-list");
  if(list) list.innerHTML='<div class="msg" style="padding:16px">Searching…</div>';
  const params=new URLSearchParams();
  if(q)   params.set("q",q);
  if(tag) params.set("tag",tag);
  const r=await api("/api/radio/search?"+params.toString());
  if(!r){ if(list) list.innerHTML='<div class="msg" style="padding:16px">Search failed.</div>'; return; }
  let stations=[];
  try{ stations=await r.json(); }catch(e){ stations=[]; }
  renderRadioResults(Array.isArray(stations)?stations:[]);
}

async function renderRadioFavourites(){
  const list=$("radio-list");
  if(!list) return;
  list.innerHTML='<div class="spinner-wrap"><div class="spinner"></div></div>';
  if(radioFavCache===null){
    const r=await api("/api/radio/favourites");
    if(!r){ list.innerHTML='<div class="msg" style="padding:16px">Could not load.</div>'; return; }
    const d=await r.json();
    radioFavCache=d.stations||[];
    if(typeof d.limit==="number") radioFavLimit=d.limit;
  }
  _updateRadioCap();
  list.innerHTML="";
  if(!radioFavCache.length){
    list.innerHTML='<div class="msg" style="padding:16px">No stations yet — search above to add one.</div>';
    return;
  }
  radioFavCache.forEach(st=>list.appendChild(_radioRow(st,true)));
}

function renderRadioResults(stations){
  const list=$("radio-list");
  if(!list) return;
  _updateRadioCap();
  list.innerHTML="";
  if(!stations.length){
    list.innerHTML='<div class="msg" style="padding:16px">No stations found.</div>';
    return;
  }
  stations.forEach(st=>list.appendChild(_radioRow(st,false)));
}

function _radioIsFav(uuid){
  return !!(radioFavCache||[]).find(s=>s.station_uuid===uuid);
}

function _updateRadioCap(){
  const cap=$("radio-cap");
  if(!cap) return;
  const n=(radioFavCache||[]).length;
  cap.textContent=`${n}/${radioFavLimit}`;
  cap.style.color=n>=radioFavLimit?"var(--amber)":"var(--ink-dim)";
}

// One station row. isFav rows carry a ✕ remove; search-result rows
// carry a ☆/★ favourite toggle. The genre tags lead the sub-line.
function _radioRow(st,isFav){
  const div=document.createElement("div");
  div.className="pl-track radio-row";
  div.dataset.uuid=st.station_uuid||"";
  const art=st.favicon
    ? `<img src="${artUrl(st.favicon)}" style="width:32px;height:32px;object-fit:cover;border-radius:3px;flex-shrink:0" onerror="this.style.display='none'">`
    : `<div class="row-icon">📻</div>`;
  const genre=(st.tags||"").split(",").map(s=>s.trim()).filter(Boolean).slice(0,2).join(", ");
  const tech=[st.codec,st.bitrate?st.bitrate+"k":"",st.country].filter(Boolean).join(" ");
  const sub=[genre,tech].filter(Boolean).join(" · ");
  const fav=isFav || _radioIsFav(st.station_uuid);
  const ctrl=isFav
    ? `<button class="pl-remove radio-remove" title="Remove station">✕</button>`
    : `<button class="radio-star" data-fav="${fav?1:0}" title="${fav?"Favourited":"Add to favourites"}" style="background:none;border:none;color:var(--amber);font-size:16px;cursor:pointer;padding:0 4px">${fav?"★":"☆"}</button>`;
  div.innerHTML=`${art}<div class="pl-track-body"><div class="pl-track-title">${esc(st.name||"")}</div><div class="pl-track-sub">${esc(sub)}</div></div>${ctrl}`;
  div.querySelector(".pl-track-body").addEventListener("click",()=>playStation(st));
  const star=div.querySelector(".radio-star");
  if(star) star.addEventListener("click",e=>{e.stopPropagation();toggleRadioFav(st,star);});
  const rm=div.querySelector(".radio-remove");
  if(rm) rm.addEventListener("click",e=>{e.stopPropagation();removeRadioFav(st,div);});
  return div;
}

async function toggleRadioFav(st,star){
  if(star.dataset.fav==="1"){
    star.dataset.fav="0";star.textContent="☆";star.title="Add to favourites";
    await removeRadioFav(st,null);
    return;
  }
  // Optimistic flip to ★ — reverts on failure or a full-cap 409.
  star.dataset.fav="1";star.textContent="★";star.title="Favourited";
  const r=await api("/api/radio/favourites/add",{method:"POST",
    headers:{"Content-Type":"application/json"},body:JSON.stringify(st)});
  if(!r||r.status>=500){
    star.dataset.fav="0";star.textContent="☆";toast("Add failed");return;
  }
  if(r.status===409){
    star.dataset.fav="0";star.textContent="☆";
    toast(`Radio favourites full (${radioFavLimit}) — remove one first`);
    return;
  }
  if(Array.isArray(radioFavCache) && !_radioIsFav(st.station_uuid))
    radioFavCache.push(st);
  _updateRadioCap();
  toast(`📡 Added ${st.name}`);
}

async function removeRadioFav(st,rowEl){
  const r=await api("/api/radio/favourites/remove",{method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({station_uuid:st.station_uuid})});
  if(!r){ toast("Remove failed"); return; }
  if(Array.isArray(radioFavCache))
    radioFavCache=radioFavCache.filter(s=>s.station_uuid!==st.station_uuid);
  _updateRadioCap();
  if(rowEl) rowEl.remove();
  toast(`Removed ${st.name}`);
}

// Play an internet-radio station. Browser output → the /radio_stream
// ICY-de-interleaving proxy; UPnP output → an is_stream track posted
// to the renderer queue (which never auto-advances it).
async function playStation(st){
  currentRadioStation=st;
  const out=$("output-sel").value;
  activeDevice=out;
  $("np-title").textContent=st.name||"Radio";
  $("np-artist").textContent="";   // ICY metadata fills this
  $("np-album").textContent="";
  $("np-meta").textContent="";
  if(st.favicon){
    $("art").innerHTML=`<img src="${artUrl(st.favicon)}" style="width:100%;height:100%;object-fit:cover;border-radius:12px" onerror="this.parentElement.textContent='📻'">`;
  } else { $("art").textContent="📻"; }
  $("player").className="playing is-audio is-radio";
  $("btn-pp").textContent="⏸ Pause";
  $("hdr-status").textContent=st.name||"";
  setNpTrack(null);          // hide track-level actions
  _applyRadioLayout(true);
  _updateMiniPlayer({title:st.name,artist:"📻 Radio",art:st.favicon||""});

  if(out==="browser"){
    browserQueue=[];browserIdx=0;
    browserAudio.src=`/radio_stream?url=${enc(st.stream_url)}`;
    _playBrowserAudio("radio_station");
    toast(`📡 ${st.name}`);
  } else {
    const rendUdn=out.replace("upnp:","");
    const track={url:st.stream_url,title:st.name,artist:"Radio",
                 album:"",art:st.favicon||"",duration:"",is_stream:true};
    if(await sendRenderQueue(rendUdn,[track]))
      toast(`📡 ${st.name} → ${renderers[rendUdn]?.name||rendUdn}`);
  }
  _startIcyPoll();
}

// Show/hide now-playing chrome for the radio variant. on=false
// restores the normal track layout (#np-actions stays governed by
// setNpTrack()).
function _applyRadioLayout(on){
  const sk=$("seek-section"); if(sk) sk.style.display=on?"none":"";
  const lv=$("np-live");      if(lv) lv.style.display=on?"":"none";
  $("btn-rew").style.display=on?"none":"";
  $("btn-fwd").style.display=on?"none":"";
  if(on) $("np-actions").style.display="none";
}

// Leave radio mode — called by every non-radio play path.
function _exitRadioMode(){
  if(!currentRadioStation && !_icyPollTimer) return;
  currentRadioStation=null;
  _stopIcyPoll();
  _applyRadioLayout(false);
}

function _startIcyPoll(){
  _stopIcyPoll();
  pollIcy();
  _icyPollTimer=setInterval(pollIcy,10000);
}
function _stopIcyPoll(){
  if(_icyPollTimer){ clearInterval(_icyPollTimer); _icyPollTimer=null; }
}
// Poll the current station's "now playing" text into #np-artist.
async function pollIcy(){
  if(!currentRadioStation) return;
  const out=$("output-sel").value;
  const url=(out==="browser")
    ? `/api/radio/nowplaying?stream=${enc(currentRadioStation.stream_url)}`
    : `/api/radio/nowplaying?udn=${enc(out.replace("upnp:",""))}`;
  const r=await api(url);
  if(!r) return;
  let d={};
  try{ d=await r.json(); }catch(e){ return; }
  if(d.title) $("np-artist").textContent=d.title;
}

// ⏮ / ⏭ in radio mode step through the favourites like presets.
async function radioPreset(dir){
  if(!radioFavCache || !radioFavCache.length){
    const r=await api("/api/radio/favourites");
    if(r){ const d=await r.json(); radioFavCache=d.stations||[]; }
  }
  const favs=radioFavCache||[];
  if(!favs.length){ toast("No favourite stations"); return; }
  let idx=favs.findIndex(s=>s.station_uuid===(currentRadioStation||{}).station_uuid);
  idx=((idx<0?0:idx)+dir+favs.length)%favs.length;
  playStation(favs[idx]);
}

async function openPlaylist(plId){
  curPlId=plId;
  const r=await api(`/api/playlist?id=${enc(plId)}`);if(!r)return;
  const pl=await r.json();
  $("pl-panel-title").textContent=pl.name;
  $("pl-back-btn").style.display="";
  $("pl-list").style.display="none";
  $("pl-tracks").classList.add("visible");

  const isFav=plId==="__favourites__";
  $("pl-actions").innerHTML=`
    <button class="btn" style="font-size:11px;padding:5px 10px" onclick="playPlaylist('${plId}')">▶ Play</button>
    ${!isFav?`<button class="btn" style="font-size:11px;padding:5px 10px;color:var(--red)" onclick="deletePlaylist('${plId}')">🗑 Delete</button>`:""}
  `;

  const tracks=$("pl-tracks");
  tracks.innerHTML="";
  if(!pl.tracks||!pl.tracks.length){tracks.innerHTML='<div class="msg" style="padding:20px">Empty playlist</div>';return;}
  pl.tracks.forEach(t=>{
    const div=document.createElement("div");
    div.className="pl-track";
    const k2=regItem(t);
    div.innerHTML=`${t.art?`<img src="/art?url=${encodeURIComponent(t.art)}" style="width:28px;height:28px;object-fit:cover;border-radius:3px;flex-shrink:0" onerror="this.style.display='none'">`:""}<div class="pl-track-body"><div class="pl-track-title">${esc(t.title)}</div><div class="pl-track-sub">${esc([t.artist,t.album].filter(Boolean).join(" · "))}</div></div><button class="pl-remove" title="Remove from playlist">✕</button>`;
    div.querySelector(".pl-track-body").addEventListener("click",()=>startPlayTrack(t));
    // Wire the remove button from JS — using inline onclick with JSON.stringify(url)
    // produced HTML like onclick="…removeFromPlaylist('pl-1',"http://…")" which
    // breaks the attribute parser on the embedded double-quote, so the click
    // silently did nothing.
    div.querySelector(".pl-remove").addEventListener("click",e=>{
      e.stopPropagation();
      removeFromPlaylist(plId, t.url);
    });
    tracks.appendChild(div);
  });
}

async function playPlaylist(plId){
  const r=await api(`/api/playlist?id=${enc(plId)}`);
  if(!r)return;
  const pl=await r.json();
  let tracks=pl.tracks||[];
  if(!tracks.length){toast("Playlist is empty");return;}
  await playTracklist(tracks, pl.name, "");
}

async function newPlaylist(){
  const name=prompt("Playlist name:");
  if(!name)return;
  const r=await api(`/api/playlist/create?name=${enc(name)}`);
  if(!r)return;
  await showPlaylists();
  toast(`Created: ${name}`);
}

async function deletePlaylist(plId){
  if(!confirm("Delete this playlist?"))return;
  await api(`/api/playlist/delete?id=${enc(plId)}`);
  curPlId=null;showPlaylists();toast("Playlist deleted");
}

async function removeFromPlaylist(plId,url){
  await api(`/api/playlist/remove?pl=${enc(plId)}&url=${enc(url)}`);
  openPlaylist(plId);
}

function showFavourites(){
  openPlaylist("__favourites__");
  $("pl-list").style.display="none";
  $("pl-tracks").classList.add("visible");
}

// ── Add-to-playlist dropdown ──────────────────────────────────────
let dropdownEl=null;
function hideDropdown(){if(dropdownEl){dropdownEl.remove();dropdownEl=null;}}
document.addEventListener("click",hideDropdown);

async function showAddToPlaylistForItem(event,item){
  hideDropdown();
  await loadPlaylists(); // always refresh before showing
  const custom=playlists.filter(p=>p.id!=="__favourites__");
  if(!custom.length){toast("No playlists yet — create one in the Playlists tab");return;}
  const el=document.createElement("div");
  el.className="pl-dropdown";
  el.style.top=(event.clientY+4)+"px";
  el.style.left=Math.min(event.clientX,window.innerWidth-200)+"px";
  custom.forEach(pl=>{
    const d=document.createElement("div");
    d.className="pl-dropdown-item";d.textContent=pl.name;
    d.addEventListener("click",e=>{e.stopPropagation();hideDropdown();addToPlaylist(pl.id,item);});
    el.appendChild(d);
  });
  document.body.appendChild(el);dropdownEl=el;
}

async function addToPlaylist(plId,item){
  if(!item||!item.url){toast("Error: no track URL");return;}
  const params=new URLSearchParams({
    pl: plId,
    url: item.url,
    title: item.title||"",
    artist: item.artist||"",
    album: item.album||"",
    duration: item.duration||"",
    art: item.art||""
  });
  const r=await api("/api/playlist/add?"+params.toString());
  if(!r){toast("❌ Network error");return;}
  const data=await r.json();
  await loadPlaylists();
  if(data.ok===false && r.status===404){
    toast("❌ Playlist not found — try reloading");
  } else if(data.duplicate){
    toast("Already in playlist");
  } else {
    const plName=playlists.find(p=>p.id===plId)?.name||plId;
    toast(`✓ Added to ${plName}`);
  }
}

// ── Playback ──────────────────────────────────────────────────────
async function playAlbum(item){
  // Browse-pane album play — fetch track list via UPnP then route through output selector
  toast("Loading album…",3000);
  const r=await api(`/api/browse?udn=${enc(curServer.udn)}&id=${enc(item.id)}`);
  if(!r){toast("Failed to load album");return;}
  const data=await r.json();
  if(data.error){toast("Error: "+data.error);return;}
  const tracks=(data.items||[]).filter(t=>t.type!=="container");
  if(!tracks.length){toast("No tracks found in album");return;}
  await playTracklist(tracks, item.title, item.artist||"");
}

function startPlayTrack(item){
  PLAYER_play(item.url,item.title,item.art,item.type||"audio",item.artist||"",item.album||"");
}

async function startPlay(item,rowEl){
  curItemId=item.id;
  document.querySelectorAll(".row").forEach(r=>r.classList.remove("active"));
  if(rowEl)rowEl.classList.add("active");
  PLAYER_play(item.url,item.title,item.art,item.type||"audio",item.artist||"",item.album||"");
}

async function PLAYER_play(url,title,art,mtype,artist,album){
  _exitRadioMode();   // a normal track replaces any radio session
  $("np-title").textContent=title||"";
  $("np-artist").textContent=artist||"";
  $("np-album").textContent=album||"";
  $("np-meta").textContent="";
  if(art){$("art").innerHTML=`<img src="${artUrl(art)}" style="width:100%;height:100%;object-fit:cover;border-radius:12px" onerror="this.parentElement.textContent='🎵'">`;}
  else{$("art").textContent=mtype==="video"?"🎬":"🎵";}
  $("btn-pp").textContent="⏸ Pause";
  $("hdr-status").textContent=title||"";
  $("player").className="playing is-"+(mtype);
  setNpTrack({url,title,artist,album,art,mime:""});

  const out=$("output-sel").value;
  activeDevice=out;
  _updateMiniPlayer({title,artist,art});

  if(out==="browser"){
    browserQueue=[{url,title,artist,album,art,mime:""}];browserIdx=0;
    browserAudio.src=`/stream?url=${enc(url)}`;
    _playBrowserAudio("single_track_send");
    toast("▶ Streaming in browser…");
  } else {
    const rendUdn=out.replace("upnp:","");
    const ok=await sendRenderQueue(rendUdn,[{url,title,artist,art,mime:""}]);
    if(ok) toast(`▶ Sending to ${renderers[rendUdn]?.name||rendUdn}…`);
  }
}

// activeDevice tracks the current output: "browser" or "upnp:<udn>"
let activeDevice="browser";  // default — always available

// Sync activeDevice when user changes output selector
$("output-sel").addEventListener("change",()=>{activeDevice=$("output-sel").value;});

$("btn-pp").addEventListener("click",()=>control({action:"pause"}));
$("btn-stop").addEventListener("click",()=>{control({action:"stop"});resetPlayer();});
$("btn-rew").addEventListener("click",()=>control({action:"seek",value:-30}));
$("btn-fwd").addEventListener("click",()=>control({action:"seek",value:30}));
$("btn-prev").addEventListener("click",()=>{
  if(currentRadioStation) radioPreset(-1); else control({action:"prev"});
});
$("btn-next").addEventListener("click",()=>{
  if(currentRadioStation) radioPreset(1);  else control({action:"next"});
});
$("btn-shuffle").addEventListener("click",()=>{
  shuffleEnabled=!shuffleEnabled;
  localStorage.setItem("dlna_shuffle", shuffleEnabled?"1":"0");
  updateShuffleBtn();
  toast(shuffleEnabled?"🔀 Shuffle ON — next play will be random":"▶ Shuffle OFF — next play will be in order");
});
function updateShuffleBtn(){
  $("btn-shuffle").style.color=shuffleEnabled?"var(--amber)":"";
  $("btn-shuffle").style.borderColor=shuffleEnabled?"var(--amber)":"";
}
updateShuffleBtn();  // initialise visual state (ON by default)

// Play a shuffled grab-bag of tracks from the current server. Exposed on
// the global scope so both the desktop toolbar (#btn-radio) and the
// mobile bottom-nav (#bnav-radio) can call it directly.
async function startRadio(){
  if(!curServer){toast("No server selected");return;}
  toast("📻 Loading radio…", 3000);
  const r=await api(`/api/radio?udn=${enc(curServer.udn)}&limit=100`);
  if(!r){toast("Radio failed");return;}
  const d=await r.json();
  if(!d.tracks||!d.tracks.length){toast("No tracks in library");return;}
  // Radio always shuffles regardless of the shuffle toggle
  const tracks=shuffle(d.tracks);
  await playTracklist(tracks,"📻 Radio","");
  toast(`📻 Radio — ${tracks.length} random tracks`);
}
$("btn-radio").addEventListener("click", startRadio);

// ── Volume slider — RELATIVE TRIM (-5..+5 dB), default 0 ─────────
// Each slider step = 0.5 dB (range -10..+10 in slider units → -5..+5 dB).
// Default 0 means "no change" — tapping the slider can't blast the room.
//
// Slider drags fire `input` ~30+ times/sec, which used to flood the
// renderer's single-threaded SOAP server with SetVolume calls and
// produce audible stair-stepping. We update the on-screen label
// instantly (no perceived lag) but coalesce the actual control() call
// to the LAST value after the user pauses for ≥120 ms — so a drag
// produces one SetVolume, not thirty. Buttons fire immediately because
// each click is one event.
const _TRIM_DEBOUNCE_MS = 120;
let _trimDebounceTimer = null;
function _trimDbFromSlider(){ return parseInt($("vol").value, 10) / 2; }
function _formatTrim(db){ return (db >= 0 ? "+" : "") + db.toFixed(1) + " dB"; }
function _updateTrimLabel(){
  $("vol-label").textContent = _formatTrim(_trimDbFromSlider());
}
function _sendTrimNow(){
  control({action: "trim_db", value: _trimDbFromSlider()});
}
function _sendTrimDebounced(){
  _updateTrimLabel();
  clearTimeout(_trimDebounceTimer);
  _trimDebounceTimer = setTimeout(_sendTrimNow, _TRIM_DEBOUNCE_MS);
}
$("vol").addEventListener("input", _sendTrimDebounced);
// Pointerup / change fires when the user releases the slider — flush
// any pending debounced call so the final value lands without delay.
$("vol").addEventListener("change", () => {
  clearTimeout(_trimDebounceTimer); _sendTrimNow();
});
$("btn-vol-up").addEventListener("click", () => {
  const v = $("vol");
  v.value = Math.min(parseInt(v.max, 10), parseInt(v.value, 10) + 1);
  _updateTrimLabel(); _sendTrimNow();
});
$("btn-vol-down").addEventListener("click", () => {
  const v = $("vol");
  v.value = Math.max(parseInt(v.min, 10), parseInt(v.value, 10) - 1);
  _updateTrimLabel(); _sendTrimNow();
});
async function control(cmd){
  if(activeDevice==="browser"){
    // Handle browser audio locally — no server round-trip needed
    switch(cmd.action){
      case "pause":
        if(browserAudio.paused){
          $("btn-pp").textContent="⏸ Pause";$("mini-pp").textContent="⏸";
          _playBrowserAudio("user_pause_toggle");
        }else{browserAudio.pause();$("btn-pp").textContent="▶ Play";$("mini-pp").textContent="▶";}
        break;
      case "stop":
        browserAudio.pause();browserAudio.src="";browserAudio.load();
        browserQueue=[];browserIdx=0;
        $("btn-pp").textContent="▶ Play";$("mini-pp").textContent="▶";
        break;
      case "next":
        if(browserIdx<browserQueue.length-1){browserIdx++;_browserPlayIdx(browserIdx);}
        break;
      case "prev":
        if(browserIdx>0){browserIdx--;_browserPlayIdx(browserIdx);}
        else{browserAudio.currentTime=0;}
        break;
      case "seek":
        browserAudio.currentTime=Math.max(0,browserAudio.currentTime+(cmd.value||0));break;
      case "seek_abs":
        browserAudio.currentTime=Math.max(0,cmd.value||0);break;
      case "trim_db": {
        // Browser audio: linear gain = 10^(trim_db / 20), clamped to
        // [0, 1] (HTML5 audio.volume can't exceed 1, so positive trim
        // just means "no attenuation"). Phone master volume is the
        // user's real ceiling.
        const db = Number(cmd.value) || 0;
        browserAudio.volume = Math.max(0, Math.min(1, Math.pow(10, db / 20)));
        break;
      }
    }
    return;
  }
  await api("/api/control",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({...cmd,device:activeDevice})});
}

function resetPlayer(){
  _exitRadioMode();
  $("np-title").textContent="Nothing playing";$("np-artist").textContent="";$("np-album").textContent="";$("np-year").textContent="";
  $("np-meta").textContent="Browse or search your library";
  $("art").textContent="🎵";$("btn-pp").textContent="▶ Play";$("player").className="";
  $("seek-fill").style.width="0%";$("seek-thumb").style.left="0%";
  $("t-pos").textContent="0:00";$("t-dur").textContent="0:00";curItemId=null;
}

const seekTrack=$("seek-track");
function pct(e){const r=seekTrack.getBoundingClientRect();return Math.max(0,Math.min(1,(e.clientX-r.left)/r.width));}
function applyPct(p){$("seek-fill").style.width=(p*100)+"%";$("seek-thumb").style.left=(p*100)+"%";$("t-pos").textContent=fmtSec(p*(ps.duration||0));}
seekTrack.addEventListener("mousedown",e=>{seeking=true;seekTrack.classList.add("dragging");const p=pct(e);applyPct(p);seekTarget=p*(ps.duration||0);});
document.addEventListener("mousemove",e=>{if(!seeking)return;const p=pct(e);applyPct(p);seekTarget=p*(ps.duration||0);});
document.addEventListener("mouseup",()=>{if(!seeking)return;seeking=false;seekTrack.classList.remove("dragging");control({action:"seek_abs",value:seekTarget});});

async function pollState(){
  // Poll the right player depending on active output
  if(activeDevice==="browser"){
    // ── Browser <audio> state (no server poll needed) ─────────
    const paused=browserAudio.paused||browserAudio.ended;
    const pos=browserAudio.currentTime||0;
    const dur=isNaN(browserAudio.duration)?0:browserAudio.duration;
    $("sb-dot").className="sb-dot "+(paused?"paused":"playing");
    $("sb-state").textContent=paused?"paused":"playing";
    $("btn-pp").textContent=paused?"▶ Play":"⏸ Pause";
    if(!seeking&&dur>0){
      const p=(pos/dur)*100;
      $("seek-fill").style.width=p+"%";$("seek-thumb").style.left=p+"%";
      $("t-pos").textContent=fmtSec(pos);$("t-dur").textContent=fmtSec(dur);
    }
    const t=browserQueue[browserIdx];
    if(t) $("sb-uri").textContent=t.title||"—";
    if(dur>0) $("mini-progress").style.width=((pos/dur)*100)+"%";
    return;
  }
  // ── UPnP renderer state (per-renderer; pass UDN so we poll the
  // right queue when multiple renderers have active sessions) ──
  const udn=activeDevice.startsWith("upnp:")?activeDevice.slice(5):"";
  const url="/api/renderer_state"+(udn?"?udn="+encodeURIComponent(udn):"");
  const r=await api(url);if(!r)return;
  const ps=await r.json();
  $("sb-dot").className="sb-dot "+(ps.state||"stopped");
  $("sb-state").textContent=ps.state||"stopped";
  $("sb-uri").textContent=ps.media_title||ps.title||"—";
  if(ps.alive){
    $("btn-pp").textContent=ps.paused?"▶ Play":"⏸ Pause";
    // In radio mode the station name + ICY title own the panel — don't
    // let the renderer snapshot clobber them.
    if(!currentRadioStation){
      if(ps.media_title||ps.title){
        $("np-title").textContent=ps.media_title||ps.title||"";
        $("hdr-status").textContent=ps.media_title||ps.title||"";
      }
      if(ps.artist) $("np-artist").textContent=ps.artist;
      if(ps.album)  $("np-album").textContent=ps.album;
      if(ps.queue_len>1)
        $("np-meta").textContent=`Track ${ps.queue_pos} of ${ps.queue_len}`;
    }
    if(!seeking&&ps.duration&&ps.position!=null){
      const p=(ps.position/ps.duration)*100;
      $("seek-fill").style.width=p+"%";$("seek-thumb").style.left=p+"%";
      $("t-pos").textContent=fmtSec(ps.position);$("t-dur").textContent=fmtSec(ps.duration);
    }
  } else {
    if($("player").classList.contains("playing")){
      $("player").classList.remove("playing");$("btn-pp").textContent="▶ Play";
    }
  }
}

async function pollIndex(){
  if(!curServer)return;
  const r=await api(`/api/index/status?udn=${enc(curServer.udn)}`);
  if(!r)return;
  const s=await r.json();
  const bar=$("index-bar"),lbl=$("index-label"),pb=$("index-progress-bar");
  // Indexing takes priority — it's a foreground rebuild.
  if(s.status==="running"){
    bar.style.display="";
    const pct=s.total>0?Math.round((s.progress/s.total)*100):0;
    lbl.textContent=`Indexing… ${s.progress}/${s.total} albums · ${s.tracks} tracks`;
    pb.style.background="var(--amber)";pb.style.width=pct+"%";
    return;
  }
  if(s.status==="error"){
    bar.style.display="";
    lbl.textContent=`Index error: ${s.error}`;
    pb.style.background="var(--red)";pb.style.width="100%";
    return;
  }
  // Done, OR idle but a library exists (the LocalFs case: the UPnP-only
  // INDEXER never runs, so status stays 'idle' even though db_tracks>0).
  // Show the library line.
  if(s.status==="done" || (s.db_tracks||0)>0){
    bar.style.display="";
    lbl.textContent=`Library: ${(s.db_tracks||0).toLocaleString()} tracks indexed ✓`;
    pb.style.background="var(--amber)";pb.style.width="100%";
  }
}


async function reindex(){
  if(!curServer)return;
  if(!confirm("Rebuild the full library index? This takes a few minutes."))return;
  await api(`/api/index/rebuild?udn=${enc(curServer.udn)}`);
  toast("Rebuilding index…",3000);
}

// ── Polling — paused when tab hidden to save iOS battery ─────────
let _t_servers, _t_renderers, _t_state, _t_index;

// ── SSE (R2) — instant pushes on top of the polls ───────────────
// The 2.0 ASGI gateway pushes server-sent events on state/index/device
// changes (dlna_events). We treat them as an ACCELERATOR: an event just
// fires the same refresh the interval would, so updates feel instant.
// Polling stays as the fallback — if SSE never connects (stdlib server,
// older browser), nothing is lost. EventSource auto-reconnects on drop.
let _es=null;
function initEventSource(){
  if(_es) return;                                   // open once
  if(typeof EventSource==="undefined") return;      // unsupported → polling
  try{
    _es=new EventSource("/api/events");
    _es.addEventListener("state",   ()=>pollState());
    _es.addEventListener("index",   ()=>pollIndex());
    _es.addEventListener("devices", ()=>{refreshServers();refreshRenderers();});
    _es.onerror=()=>{};   // transient drop — EventSource retries on its own
  }catch(e){ /* SSE is optional; polling carries on */ }
}

function startPolling(){
  stopPolling();
  _t_servers   = setInterval(refreshServers,   8000);
  _t_renderers = setInterval(refreshRenderers, 10000);
  _t_state     = setInterval(pollState,        1000);
  _t_index     = setInterval(pollIndex,        2000);
  initEventSource();
}
function stopPolling(){
  clearInterval(_t_servers);
  clearInterval(_t_renderers);
  clearInterval(_t_state);
  clearInterval(_t_index);
}

// Page Visibility API — stop polling when screen locks or tab goes background.
// Cuts iPhone radio wake-ups from ~3600/hr to zero while hidden.
document.addEventListener("visibilitychange", ()=>{
  if(document.hidden){
    stopPolling();
  } else {
    startPolling();
    pollState();
    refreshRenderers();
  }
});

// ── Audio interruption recovery (phone calls, Siri, other apps) ──
// When iOS interrupts audio (call, Siri), it may resume a different
// audio session on end (podcast app, etc). Reassert our MediaSession
// when audio pauses unexpectedly so iOS knows we own the session.
browserAudio.addEventListener("pause", ()=>{
  setTimeout(()=>{
    if(browserAudio.paused && activeDevice==="browser" &&
       browserQueue.length && !browserAudio.ended){
      const t=browserQueue[browserIdx];
      if(t) _updateMediaSession(t, browserIdx);
    }
  }, 800);
});

// Reassert on resume too — locks in our session after interruption ends
browserAudio.addEventListener("play", ()=>{
  const t=browserQueue[browserIdx];
  if(t && activeDevice==="browser") _updateMediaSession(t, browserIdx);
});

// ── Init ──────────────────────────────────────────────────────────
// Version badge — lets a side-by-side 1.x / 2.0 instance be told apart.
// Defensive: any failure (older 1.x with no /api/version) just leaves it blank.
api("/api/version").then(async r=>{try{if(r&&r.ok){const j=await r.json();const el=$("app-version");if(el&&j&&j.version)el.textContent="v"+j.version;}}catch{}});
refreshServers();
refreshRenderers();
loadPlaylists().then(showPlaylists);
startPolling();

// Rebuild the output selector with browser + UPnP renderers
function rebuildOutputSel(upnpData){
  const out=$("output-sel");
  const prev=out.value;
  let html=`<option value="browser">📱 Browser</option>`;
  if(upnpData){
    upnpData.forEach(rd=>{
      html+=`<option value="upnp:${esc(rd.udn)}">📡 ${esc(rd.name)}</option>`;
    });
  }
  out.innerHTML=html;
  if(prev&&out.querySelector(`option[value="${prev}"]`)) out.value=prev;
  else out.value="browser";
  activeDevice=out.value;
}

// ── Edit metadata modal ───────────────────────────────────────────
let _editTrack = null;   // track object currently being edited

async function openEditModal(track){
  if(!track || !track.url){ toast("No track to edit"); return; }
  _editTrack = track;
  $("edit-title").value  = track.title  || "";
  $("edit-artist").value = track.artist || "";
  $("edit-album").value  = track.album  || "";
  $("edit-genre").value  = track.genre  || "";
  // Year isn't carried on the track object — fetch the current
  // override from /api/track_meta. Prefill with the MB-original
  // year if present, else file-tag year, else blank. Cache the
  // prefill on the track so the save handler can tell whether the
  // user actually changed it.
  $("edit-year").value = "";
  track._editPrefillYear = null;
  try{
    const r = await fetch(`/api/track_meta?url=${enc(track.url)}`);
    if(r.ok){
      const m = await r.json();
      const y = m.year_original || m.year || null;
      $("edit-year").value = y ? String(y) : "";
      track._editPrefillYear = y;
    }
  }catch(e){ /* best effort prefill */ }
  $("edit-modal-sub").textContent =
    "Changes saved to library immediately. File tags written if path is known.";
  $("edit-modal").classList.add("open");
  // Focus title field
  setTimeout(()=>$("edit-title").focus(), 120);
}

$("edit-cancel").addEventListener("click", ()=>$("edit-modal").classList.remove("open"));
$("edit-modal").addEventListener("click", e=>{
  if(e.target===$("edit-modal")) $("edit-modal").classList.remove("open");
});
// Save on Enter in any field
["edit-title","edit-artist","edit-album","edit-genre","edit-year"].forEach(id=>{
  $(id).addEventListener("keydown", e=>{ if(e.key==="Enter") $("edit-save").click(); });
});

$("edit-save").addEventListener("click", async ()=>{
  if(!_editTrack) return;
  // Parse year — must be a 4-digit int in [1900, 2100] if present.
  const yearRaw = $("edit-year").value.trim();
  let yearVal = null;
  if(yearRaw){
    const n = parseInt(yearRaw, 10);
    if(!Number.isFinite(n) || n < 1900 || n > 2100){
      toast("Year must be between 1900 and 2100");
      return;
    }
    yearVal = n;
  }
  const body = {
    url:    _editTrack.url,
    title:  $("edit-title").value.trim()  || null,
    artist: $("edit-artist").value.trim() || null,
    album:  $("edit-album").value.trim()  || null,
    genre:  $("edit-genre").value.trim()  || null,
    year:   yearVal,
  };
  // Only send changed fields (only year is compared against the
  // form's prefilled value; everything else compares against the
  // in-memory track object).
  const changed = {};
  if(body.title  !== (_editTrack.title  ||null)) changed.title  = body.title;
  if(body.artist !== (_editTrack.artist ||null)) changed.artist = body.artist;
  if(body.album  !== (_editTrack.album  ||null)) changed.album  = body.album;
  if(body.genre  !== (_editTrack.genre  ||null)) changed.genre  = body.genre;
  // Year: send when the user typed a value different from the
  // prefilled one (or cleared a previously-set value).
  const prefilledYear = _editTrack._editPrefillYear ?? null;
  if(body.year !== prefilledYear) changed.year = body.year;
  if(!Object.keys(changed).length){ $("edit-modal").classList.remove("open"); return; }
  changed.url = _editTrack.url;
  const r = await api("/api/edit_track", {method:"POST",
    headers:{"Content-Type":"application/json"},
    body: JSON.stringify(changed)});
  if(!r){ toast("Save failed"); return; }
  const d = await r.json();
  if(d.error){ toast("Error: "+d.error); return; }
  // Update in-memory track object so UI reflects change immediately
  Object.assign(_editTrack, changed);
  $("edit-modal").classList.remove("open");
  toast("✓ Saved to library", 2400);
  // Update now-playing panel if this is the current track
  if(npTrack && npTrack.url===_editTrack.url){
    Object.assign(npTrack, changed);
    if(changed.title)  $("np-title").textContent  = changed.title;
    if(changed.artist) $("np-artist").textContent = changed.artist;
    if(changed.album)  $("np-album").textContent  = changed.album;
    if("year" in changed) _renderNpYear(npTrack.url);   // refetch + redraw
  }
});

// ── Now-playing ⭐ and ＋ buttons ─────────────────────────────────
$("np-btn-fav").addEventListener("click",()=>{
  if(!npTrack){toast("Nothing playing");return;}
  addToPlaylist("__favourites__", npTrack);
});
$("np-btn-edit").addEventListener("click",()=>{
  if(!npTrack){toast("Nothing playing");return;}
  openEditModal(npTrack);
});
$("np-btn-add").addEventListener("click",(e)=>{
  if(!npTrack){toast("Nothing playing");return;}
  showAddToPlaylistForItem(e, npTrack);
});

// ── Lyrics modal ──────────────────────────────────────────────────
$("np-btn-lyrics").addEventListener("click", async ()=>{
  if(!npTrack || !npTrack.url){ toast("Nothing playing"); return; }
  const overlay = $("lyrics-modal");
  const body    = $("lyrics-body");
  const sub     = $("lyrics-modal-sub");
  sub.textContent = `${npTrack.title || ""}${npTrack.artist ? " — " + npTrack.artist : ""}`;
  body.textContent = "Loading…";
  overlay.classList.add("open");
  let r;
  try { r = await api("/api/lyrics?url=" + encodeURIComponent(npTrack.url)); }
  catch(e){ body.textContent = "Error contacting gateway."; return; }
  if(!r){ body.textContent = "Error contacting gateway."; return; }
  let d;
  try { d = await r.json(); } catch(e){ body.textContent = "Bad response."; return; }
  if(d.error && d.source !== "notfound"){
    body.textContent = "Could not fetch lyrics: " + d.error;
    return;
  }
  if(d.source === "notfound" || (!d.plain && !d.synced)){
    body.textContent = "No lyrics found for this track.";
    return;
  }
  // Prefer plain text for v1; if only synced (LRC) is available, strip
  // the [mm:ss.xx] timestamps for readable display.
  let text = d.plain;
  if(!text && d.synced){
    text = d.synced.replace(/\[\d+:\d+(?:\.\d+)?\]\s?/g, "");
  }
  body.textContent = text || "(empty)";
  body.scrollTop = 0;
});
$("lyrics-close").addEventListener("click", ()=>$("lyrics-modal").classList.remove("open"));
$("lyrics-modal").addEventListener("click", e=>{
  if(e.target===$("lyrics-modal")) $("lyrics-modal").classList.remove("open");
});
$("video-close").addEventListener("click", closeVideo);
$("video-modal").addEventListener("click", e=>{ if(e.target===$("video-modal")) closeVideo(); });
// Native playback failed (e.g. HEVC in Chrome/FF that canPlayType mis-claimed)
// → fall back once to the same-origin transcode stream (H.264/AAC).
$("video-player").addEventListener("error", ()=>{
  const p=$("video-player");
  if(_curVid && p.dataset.triedTranscode!=="1"){
    p.dataset.triedTranscode="1";
    playTranscoded(_curVid);
    toast("Converting for your browser…");
  }
});

// Initialise output selector on first load
rebuildOutputSel(null);

// ── Service Worker registration ──────────────────────────────────
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js')
    .then(reg => {
      setInterval(() => reg.update(), 30 * 60 * 1000);
    })
    .catch(err => console.warn('SW registration failed:', err));
}