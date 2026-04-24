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

// ── Browser audio queue ───────────────────────────────────────────
const browserAudio=document.getElementById("browser-audio");
let browserQueue=[],browserIdx=0;
let npTrack = null;   // track object currently shown in now-playing panel

function setNpTrack(t){
  npTrack = t || null;
  const panel = $("np-actions");
  if(npTrack && npTrack.url){
    panel.style.display = "flex";
  } else {
    panel.style.display = "none";
  }
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
  ["browse","search","playlists","favourites"].forEach(t=>{
    $("tab-"+t).classList.toggle("active",t===tab);
  });
  const isBrowse = tab==="browse";
  $("browse-modes").style.display = (isBrowse && !drillArtist && !drillAlbum) ? "" : "none";
  $("letter-bar").style.display   = (isBrowse && !drillArtist && !drillAlbum) ? "" : "none";
  if(tab==="browse"){
    if(curServer) { buildLetterBar(); loadBrowsePage(); }
  } else if(tab==="playlists"){
    loadPlaylists();
  } else if(tab==="favourites"){
    showFavourites();
  }
}

// ── Servers (source) ─────────────────────────────────────────────
let renderers = {};   // udn → MediaRenderer
async function refreshServers(){
  const r=await api("/api/servers");if(!r)return;
  const data=await r.json();
  const sel=$("server-sel"),prev=sel.value||curServer?.udn;
  servers={};data.forEach(s=>servers[s.udn]=s);
  const dot=$("disc-dot"),lbl=$("disc-label");
  const online=data.filter(s=>s.online);
  if(online.length){dot.className="disc-dot on";lbl.textContent=`${online.length} server${online.length>1?"s":""} online`;}
  else if(data.length){dot.className="disc-dot";lbl.textContent="Server offline — reconnecting…";}
  else{dot.className="disc-dot";lbl.textContent="Scanning…";}
  // Populate selector — offline servers shown dimmed but selectable
  if(data.length){
    sel.innerHTML=data.map(s=>`<option value="${esc(s.udn)}" ${s.online?"":"style='color:var(--ink-dim)'"}>
      ${esc(s.name)}${s.online?"":" ⚠"}${s.tracks?" · "+s.tracks.toLocaleString()+" tracks":""}
    </option>`).join("");
  } else {
    sel.innerHTML='<option value="">— scanning… —</option>';
  }
  // Restore previous server selection (keep curServer even when offline)
  if(prev&&servers[prev]){sel.value=prev;curServer=servers[prev];}
  else if(data.length&&!curServer){curServer=data[0];sel.value=data[0].udn;if(curTab==="browse")showArtists();}
}

// ── Renderers (output) ────────────────────────────────────────────
async function refreshRenderers(){
  const r=await api("/api/renderers");if(!r)return;
  const data=await r.json();
  renderers={};data.forEach(rd=>renderers[rd.udn]=rd);
  rebuildOutputSel(data);
}
$("server-sel").addEventListener("change",e=>{
  const s=servers[e.target.value];if(!s)return;
  curServer=s;navStack=[{id:"0",title:"Root"}];
  if(curTab==="browse")showArtists();
});

// ── Browse ────────────────────────────────────────────────────────
let browsing=false;
// ── Artists view (SQLite — default startup view) ──────────────────
// ── SQLite Browse system ─────────────────────────────────────────
let browseMode   = "artists";   // artists | albums | tracks
let browseLetter = "A";
let browseOffset = 0;
const BROWSE_LIMIT = 100;
const LETTERS = ["#","0","A","B","C","D","E","F","G","H","I","J","K","L","M",
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
  ["artists","albums","tracks","genres"].forEach(m=>{
    $("bmode-"+m).classList.toggle("active", m===mode);
  });
  // Genres browse uses its own list (no letter picker needed — usually short)
  const hasLetters = mode !== "genres";
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
      div.querySelector(".icon-btn").addEventListener("click", e=>{e.stopPropagation(); playAlbumFromDB(_artist, item.album);});
      div.addEventListener("click", ()=>showAlbumTracks(_artist, item.album));
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
    div.querySelector(".icon-btn").addEventListener("click", e=>{e.stopPropagation(); playAlbumFromDB(a.artist==="Various Artists"?"":a.artist, a.album);});
    div.addEventListener("click", ()=>showAlbumTracks(a.artist==="Various Artists"?"":a.artist, a.album, {artist:a.artist, album_count:null}));
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
  $("browse-play-all").onclick = async ()=>{
    const r=await api(`/api/artist_albums?udn=${enc(curServer.udn)}&artist=${enc(artistItem.artist)}`);
    if(!r)return; const d=await r.json();
    const tracks_r=await api(`/api/search?udn=${enc(curServer.udn)}&q=${enc(artistItem.artist)}`);
    if(!tracks_r)return; const td=await tracks_r.json();
    const tracks=(td.tracks||[]).filter(t=>t.artist&&t.artist.toLowerCase()===artistItem.artist.toLowerCase());
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
    div.querySelector(".icon-btn").addEventListener("click", e=>{e.stopPropagation(); playAlbumFromDB(a.artist, a.album);});
    div.addEventListener("click", ()=>showAlbumTracks(a.artist, a.album, artistItem));
    list.appendChild(div);
  });
}

// ── Drill-down: Album → Tracks ────────────────────────────────────
async function showAlbumTracks(artist, album, artistItem=null){
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
  $("browse-play-all").onclick = ()=>playAlbumFromDB(artist, album);
  const r = await api(`/api/album_tracks?udn=${enc(curServer.udn)}&artist=${enc(artist)}&album=${enc(album)}`);
  if(!r){ $("item-list").innerHTML='<div class="msg">Could not load tracks.</div>'; return; }
  const data = await r.json();
  renderListAppend({containers:[], items: data.tracks||[]});
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
          if(btn.dataset.action==="play") playAlbumFromDB(a.artist,a.album);
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
                        {artist: a.artist, album_count: null});
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

async function playAlbumFromDB(artist,album){
  if(!curServer)return;
  toast("Loading album…",3000);
  const r=await api(`/api/album_tracks?udn=${enc(curServer.udn)}&artist=${enc(artist)}&album=${enc(album)}`);
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
  if(!playlists.length){list.innerHTML='<div class="msg" style="padding:20px">No playlists yet</div>';return;}
  playlists.forEach(pl=>{
    const div=document.createElement("div");
    div.className="pl-item"+(curPlId===pl.id?" active":"");
    div.innerHTML=`<div class="pl-item-name">${esc(pl.name)}</div><div class="pl-item-count">${pl.count} tracks</div>`;
    div.addEventListener("click",()=>openPlaylist(pl.id));
    list.appendChild(div);
  });
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
    div.innerHTML=`${t.art?`<img src="/art?url=${encodeURIComponent(t.art)}" style="width:28px;height:28px;object-fit:cover;border-radius:3px;flex-shrink:0" onerror="this.style.display='none'">`:""}<div class="pl-track-body"><div class="pl-track-title">${esc(t.title)}</div><div class="pl-track-sub">${esc([t.artist,t.album].filter(Boolean).join(" · "))}</div></div><button class="pl-remove" title="Remove from playlist" onclick="event.stopPropagation();removeFromPlaylist('${plId}',${JSON.stringify(t.url)})">✕</button>`;
    div.querySelector(".pl-track-body").addEventListener("click",()=>startPlayTrack(t));
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
$("btn-prev").addEventListener("click",()=>control({action:"prev"}));
$("btn-next").addEventListener("click",()=>control({action:"next"}));
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
$("vol").addEventListener("input",()=>{const v=parseInt($("vol").value);$("vol-label").textContent=v;control({action:"volume",value:v});});
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
      case "volume":
        browserAudio.volume=Math.max(0,Math.min(1,(cmd.value||80)/100));break;
    }
    return;
  }
  await api("/api/control",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({...cmd,device:activeDevice})});
}

function resetPlayer(){
  $("np-title").textContent="Nothing playing";$("np-artist").textContent="";$("np-album").textContent="";
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
    if(ps.media_title||ps.title){
      $("np-title").textContent=ps.media_title||ps.title||"";
      $("hdr-status").textContent=ps.media_title||ps.title||"";
    }
    if(ps.artist) $("np-artist").textContent=ps.artist;
    if(ps.album)  $("np-album").textContent=ps.album;
    if(ps.queue_len>1)
      $("np-meta").textContent=`Track ${ps.queue_pos} of ${ps.queue_len}`;
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
  if(s.status==="running"){
    bar.style.display="";
    const pct=s.total>0?Math.round((s.progress/s.total)*100):0;
    lbl.textContent=`Indexing… ${s.progress}/${s.total} albums · ${s.tracks} tracks`;
    pb.style.width=pct+"%";
  } else if(s.status==="done"){
    bar.style.display="";
    lbl.textContent=`Library: ${s.db_tracks.toLocaleString()} tracks indexed ✓`;
    pb.style.width="100%";
  } else if(s.status==="error"){
    bar.style.display="";
    lbl.textContent=`Index error: ${s.error}`;
    pb.style.background="var(--red)";pb.style.width="100%";
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

function startPolling(){
  stopPolling();
  _t_servers   = setInterval(refreshServers,   8000);
  _t_renderers = setInterval(refreshRenderers, 10000);
  _t_state     = setInterval(pollState,        1000);
  _t_index     = setInterval(pollIndex,        2000);
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

function renderPlayerSettings(){
  const list=$("settings-player-list");
  if(list) list.innerHTML="";
}

function togglePlayer(id,enabled){
  rebuildOutputSel(Object.values(renderers));
}

// ── Edit metadata modal ───────────────────────────────────────────
let _editTrack = null;   // track object currently being edited

function openEditModal(track){
  if(!track || !track.url){ toast("No track to edit"); return; }
  _editTrack = track;
  $("edit-title").value  = track.title  || "";
  $("edit-artist").value = track.artist || "";
  $("edit-album").value  = track.album  || "";
  $("edit-genre").value  = track.genre  || "";
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
["edit-title","edit-artist","edit-album","edit-genre"].forEach(id=>{
  $(id).addEventListener("keydown", e=>{ if(e.key==="Enter") $("edit-save").click(); });
});

$("edit-save").addEventListener("click", async ()=>{
  if(!_editTrack) return;
  const body = {
    url:    _editTrack.url,
    title:  $("edit-title").value.trim()  || null,
    artist: $("edit-artist").value.trim() || null,
    album:  $("edit-album").value.trim()  || null,
    genre:  $("edit-genre").value.trim()  || null,
  };
  // Only send changed fields
  const changed = {};
  if(body.title  !== (_editTrack.title  ||null)) changed.title  = body.title;
  if(body.artist !== (_editTrack.artist ||null)) changed.artist = body.artist;
  if(body.album  !== (_editTrack.album  ||null)) changed.album  = body.album;
  if(body.genre  !== (_editTrack.genre  ||null)) changed.genre  = body.genre;
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

$("btn-settings").addEventListener("click",()=>{
  renderPlayerSettings();
  $("settings-modal").classList.add("open");
});
$("settings-close").addEventListener("click",()=>$("settings-modal").classList.remove("open"));
$("settings-modal").addEventListener("click",e=>{if(e.target===$("settings-modal"))$("settings-modal").classList.remove("open");});

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