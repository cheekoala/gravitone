// bgst UI - plain ES2019, no build step, no network beyond this server.
(() => {
  "use strict";

  // The token arrives in the URL fragment. Remember it for this origin so
  // reopening the bare address (a bookmark, a restored tab) still works.
  const remember = (value) => {
    try {
      if (value) localStorage.setItem("bgst-token", value);
    } catch (err) { /* private window, blocked storage - not important */ }
  };
  const recall = () => {
    try {
      return localStorage.getItem("bgst-token") || "";
    } catch (err) {
      return "";
    }
  };
  const TOKEN = location.hash.slice(1) || recall();
  remember(TOKEN);
  // Opened as a file:// page there is no server to talk to, and no token.
  const SERVED = location.protocol === "http:" || location.protocol === "https:";

  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  };

  let state = null;
  let tagsSeen = null;     // the tag-read generation the table was built from
  let panel = "now";
  let section = "music";      // which library the Add panel targets
  let browsePath = null;
  let toastTimer = null;
  const libraries = { music: null, ambient: null };
  const filters = { music: "", ambient: "" };
  const limits = { music: 400, ambient: 400 };   // rows built at once

  // -- server ---------------------------------------------------------
  let inFlight = 0;
  function busy(delta) {
    inFlight = Math.max(0, inFlight + delta);
    $("activity").classList.toggle("on", inFlight > 0);
  }

  // Wrap a button so a click visibly does something, however slow the
  // answer is.
  async function withSpinner(button, work) {
    if (!button) return work();
    const label = button.innerHTML;
    button.classList.add("busy");
    button.innerHTML = `<span class="spinner"></span>${button.textContent.trim()}`;
    try {
      return await work();
    } finally {
      button.classList.remove("busy");
      button.innerHTML = label;
    }
  }

  async function api(route, body, quiet) {
    if (!SERVED) throw new Error("not served by bgst");
    if (!quiet) busy(1);
    try {
      return await request(route, body);
    } finally {
      if (!quiet) busy(-1);
    }
  }

  async function request(route, body) {
    const res = await fetch(`/api/${route}`, {
      method: body === undefined ? "GET" : "POST",
      headers: { "X-BGST-Token": TOKEN, "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const payload = await res.json().catch(() => ({ error: `HTTP ${res.status}` }));
    if (!res.ok) {
      throw new Error(payload.hint ? `${payload.error} — ${payload.hint}` : payload.error
        || `HTTP ${res.status}`);
    }
    return payload;
  }

  function toast(message, bad) {
    const node = $("toast");
    node.textContent = message;
    node.classList.toggle("bad", !!bad);
    node.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { node.hidden = true; }, 3600);
  }

  function offline(detail, lead) {
    $("offline").hidden = false;
    if (lead) $("offline-lead").textContent = lead;
    if (detail) $("offline-detail").textContent = detail;
  }

  async function call(route, body) {
    try {
      const next = await api(route, body);
      if (next && next.config) render(next);
      return next;
    } catch (err) {
      // The usual cause of an unknown setting is a page newer than the
      // server that is serving it.
      toast(/unknown setting/i.test(err.message)
        ? `${err.message} — this page is newer than the bgst running it. `
          + `Restart it: bgst ui --stop, then bgst ui.`
        : err.message, true);
      return null;
    }
  }

  // -- formatting -----------------------------------------------------
  // Paths matter at the tail (the filename), so trim from the left.
  const shortPath = (path, max = 54) =>
    path.length <= max ? path : `…${path.slice(path.length - max + 1)}`;

  const clock = (seconds) => {
    const total = Math.max(0, Math.round(seconds || 0));
    return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
  };
  const humanGap = (config) =>
    config.gap_min === config.gap_max
      ? clock(config.gap_min)
      : `${clock(config.gap_min)}–${clock(config.gap_max)}`;

  // Ban is two steps: the button arms it, a confirm button drops down on its
  // own layer. The ban button itself never moves, so nothing shifts under
  // the cursor between the two clicks.
  let armed = null;        // the track name the confirm would ban
  let armedTimer = null;

  function armBan() {
    if (!state || !state.running || !state.now || state.now.kind === "silence") return;
    armed = state.now.name;
    $("ban-confirm-label").textContent = `Ban ${armed.length > 28 ? "this track" : armed}`;
    $("ban-confirm").classList.add("open");
    $("ban-confirm").removeAttribute("aria-hidden");
    $("ban-confirm").tabIndex = 0;
    $("ban").classList.add("armed");
    clearTimeout(armedTimer);
    armedTimer = setTimeout(disarmBan, 8000);   // forget about it if ignored
  }

  function disarmBan() {
    armed = null;
    clearTimeout(armedTimer);
    $("ban-confirm").classList.remove("open");
    $("ban-confirm").setAttribute("aria-hidden", "true");
    $("ban-confirm").tabIndex = -1;
    $("ban").classList.remove("armed");
  }

  async function confirmBan() {
    const name = armed;
    disarmBan();
    if (!name) return;
    // Name it explicitly: by now the track may have moved on, and banning
    // whatever happens to be playing is not what was asked for.
    const done = await call("ban", { name });
    if (!done) return;
    invalidate();
    toast(done.result.how === "unlinked"
      ? `Banned ${done.result.name} — unlinked and skipped`
      : `Banned ${done.result.name} — out of this playlist, file untouched`);
  }

  function toggleBan() {
    if (armed) confirmBan();
    else armBan();
  }

  // -- render ---------------------------------------------------------
  function render(next) {
    const previous = state;
    state = next;
    // The table shows what the tag reader knows; when a background batch
    // lands, fetch the rows again so guesses give way to the real thing.
    if (tagsSeen !== null && next.tags_version !== tagsSeen) refreshLibraries();
    tagsSeen = next.tags_version;
    const { config, now, running } = next;

    $("root-path").textContent = shortPath(config.root, 44);
    $("transport").dataset.on = String(running);
    $("transport-label").textContent = running ? "Live" : "Play";
    $("skip").disabled = !running;
    $("ban").disabled = !running || !now || now.kind === "silence";
    // The armed confirm belongs to one track; if that track is gone, so is it.
    if (armed && (!now || now.name !== armed)) disarmBan();

    const nowPanel = $("panel-now");
    const inGap = !!now && now.kind !== "track";
    nowPanel.classList.toggle("is-gap", inGap);
    // The server withholds gap times in hidden mode; there is nothing to show.
    nowPanel.classList.toggle("is-hidden", inGap && next.hidden);
    const kinds = { track: "Now playing", ambient: "Ambience", silence: "Silence" };
    $("now-kind").textContent = now ? kinds[now.kind] : (running ? "Starting" : "Idle");
    $("now-title").textContent = now
      ? (now.kind === "silence" ? "Quiet" : (now.label || now.name))
      : (next.error ? "Stopped" : "Nothing playing");
    // Under the title: the album if we know it, otherwise the file it came
    // from. The file name is always in the tooltip.
    const sub = now && now.kind !== "silence"
      ? (now.album || (now.label !== now.name ? now.name : ""))
      : "";
    $("now-sub").textContent = sub;
    $("now-sub").title = now ? now.name : "";
    renderCover(now);
    const hiddenNow = inGap && next.hidden;
    const pct = now && now.duration ? Math.min(100, (now.elapsed / now.duration) * 100) : 0;
    $("now-progress").style.width = hiddenNow ? "100%" : `${pct}%`;
    $("now-elapsed").textContent = hiddenNow ? "— — —" : (now ? clock(now.elapsed) : "0:00");
    $("now-remaining").textContent = hiddenNow
      ? "hidden"
      : (now && now.duration ? `-${clock(now.duration - now.elapsed)}` : "");

    $("stat-gap").textContent = next.hidden ? "hidden" : humanGap(config);
    $("stat-ambient").textContent = `${Math.round(config.ambient_chance * 100)}%`;

    const history = $("history");
    history.replaceChildren();
    const seen = next.history.slice().reverse();
    if (!seen.length) history.append(el("li", "empty", "Nothing yet"));
    seen.forEach((name) => history.append(el("li", null, name)));

    renderSortPickers(next);
    renderPlaylistPicker(next);
    renderPlaylists(next);
    renderRemoved(next);

    $("music-count").textContent = String(next.counts.music);
    $("ambient-count").textContent = String(next.counts.ambient);
    renderLibrary("music");
    renderLibrary("ambient");
    renderSources(next);
    renderServer(next);

    if (!document.activeElement || document.activeElement.type !== "range") syncSettings(config);

    if (next.stale) {
      offline(
        "The version still running is the old one, so this page and it no longer "
        + "agree — restart it: bgst ui --stop, then bgst ui.",
        "bgst was updated."
      );
    } else if (!$("offline").hidden && failures === 0) {
      $("offline").hidden = true;
    }

    const players = next.players.length ? next.players.join(", ") : "none found";
    let note = `Players available: ${players}. ` +
      (next.players.length ? "" : "Install ffmpeg, mpv or vlc to hear anything. ");
    if (!next.picker) note += "No system file chooser here — use the built-in browser or paste a path. ";
    if (next.error) note += `— ${next.error}`;
    $("backend-note").textContent = note;
    $("pick-folder").hidden = !next.picker;
  }

  let coverShown = null;

  function renderCover(now) {
    const holder = $("now-cover");
    const wanted = now && now.art ? now.name : null;
    if (wanted === coverShown) return;
    coverShown = wanted;
    if (!wanted) {
      holder.replaceChildren(document.createTextNode("♪"));
      holder.className = "cover empty";
      return;
    }
    const image = el("img", "cover");
    image.src = coverUrl(now);
    image.alt = "";
    image.addEventListener("error", () => {
      holder.replaceChildren(document.createTextNode("♪"));
      holder.className = "cover empty";
    });
    holder.replaceChildren(image);
    holder.className = "cover-holder";
  }

  const coverUrl = (track) =>
    `/api/cover?track=${encodeURIComponent(track.path || track.target || "")}`
    + `&t=${encodeURIComponent(TOKEN)}`;

  function renderPlaylistPicker(next) {
    const picker = $("playlist-select");
    const signature = next.playlists.map((p) => `${p.id}:${p.name}`).join("|");
    if (picker.dataset.signature !== signature) {
      picker.replaceChildren();
      next.playlists.forEach((item) => {
        const option = document.createElement("option");
        option.value = item.id;
        option.textContent = item.name;
        picker.append(option);
      });
      picker.dataset.signature = signature;
    }
    if (document.activeElement !== picker) picker.value = next.playlist;
  }

  function renderSortPickers(next) {
    const showFiles = next.config.show_filenames;
    document.querySelectorAll("input.filter").forEach((box) => {
    let timer = null;
    box.addEventListener("input", () => {
      clearTimeout(timer);
      timer = setTimeout(() => {
        const name = box.dataset.filterFor;
        filters[name] = box.value;
        limits[name] = 400;
        renderLibrary(name);
      }, 120);
    });
  });
  document.querySelectorAll("input.filenames").forEach((box) => {
      box.checked = showFiles;
    });
    // The first column is whichever of the two you chose to look at, so its
    // header sorts by that same thing.
    document.querySelectorAll("th[data-sort]").forEach((head) => {
      if (head.classList.contains("col-title")) {
        head.dataset.sort = showFiles ? "name" : "title";
        head.querySelector("span").textContent = showFiles ? "File name" : "Title";
      }
      const active = head.dataset.sort === next.sort;
      head.classList.toggle("sorted", active);
      head.classList.toggle("desc", active && next.config.sort_desc);
    });
    ["music", "ambient"].forEach((name) => {
      const note = $(`${name}-tags`);
      // Tags are read in the background; say so rather than showing a guess
      // taken from the path as though it were the real thing.
      note.hidden = !next.tags_pending;
      if (!note.hidden) {
        note.textContent = `Reading tags — ${next.tags_pending} to go. `
          + `Until then, titles and artists are read from the file paths.`;
      }
    });
  }

  function renderPlaylists(next) {
    const list = $("playlists-list");
    if (list.querySelector("input.rename")) return;   // mid-rename, leave it alone
    list.replaceChildren();
    next.playlists.forEach((item) => {
      const row = el("li", item.active ? "active" : null);
      const name = el("div", "s-name", item.name);
      name.title = item.active ? "Playing from this one" : "Switch to this playlist";
      name.addEventListener("click", () => selectPlaylist(item.id));
      const meta = el("span", "s-meta",
        `${item.sources} folder${item.sources === 1 ? "" : "s"}` +
        (item.removed ? `, ${item.removed} removed` : ""));

      const rename = el("button", "s-edit", "✎");
      rename.title = `Rename ${item.name}`;
      rename.addEventListener("click", () => startRename(row, name, item));
      row.append(name, meta, rename);

      if (!item.default) {
        const remove = el("button", "t-remove", "✕");
        remove.title = `Delete the playlist ${item.name} (its links stay on disk)`;
        remove.addEventListener("click", async () => {
          const done = await call("playlist", { action: "remove", id: item.id });
          if (done) { invalidate(); toast(`Deleted ${item.name}`); }
        });
        row.append(remove);
      }
      list.append(row);
    });
  }

  function startRename(row, nameNode, item) {
    const input = el("input", "rename");
    input.value = item.name;
    row.replaceChild(input, nameNode);
    input.focus();
    input.select();
    const finish = async (commit) => {
      const value = input.value.trim();
      input.replaceWith(nameNode);
      if (commit && value && value !== item.name) {
        const done = await call("playlist", { action: "rename", id: item.id, name: value });
        if (done) toast(`Renamed to ${value}`);
      }
    };
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") finish(true);
      if (event.key === "Escape") finish(false);
    });
    input.addEventListener("blur", () => finish(true));
  }

  function renderRemoved(next) {
    const card = $("removed-card");
    const list = $("removed-list");
    card.hidden = !next.removed.length;
    list.replaceChildren();
    next.removed.forEach((item) => {
      const row = el("li");
      const name = el("div", "s-name", item.name);
      name.title = item.target;
      const restore = el("button", "ghost small", "Restore");
      restore.addEventListener("click", async () => {
        const done = await call("restore-track", { target: item.target });
        if (done) { invalidate(); toast(`Restored ${item.name}`); }
      });
      row.append(name, restore);
      list.append(row);
    });
  }

  async function selectPlaylist(id) {
    const done = await call("playlist", { action: "select", id });
    if (done) {
      invalidate();
      const chosen = done.playlists.find((p) => p.id === id);
      toast(`Playing from ${chosen ? chosen.name : id}`);
    }
  }

  function renderLibrary(name) {
    const body = $(`${name}-list`);
    const data = libraries[name];
    if (!data) return;                       // not fetched yet
    const broken = data.broken || [];
    const showFiles = state.config.show_filenames;
    body.replaceChildren();

    if (!data.tracks.length && !broken.length) {
      const row = el("tr", "empty");
      const cell = el("td", null, name === "music"
        ? "No music yet — open Add, then add a folder or link files."
        : "No ambience yet — rain, wind, room tone, a distant tavern.");
      cell.colSpan = 7;
      row.append(cell);
      body.append(row);
      return;
    }

    const playing = state.now && state.now.name;
    const needle = filters[name].trim().toLowerCase();
    const matches = needle
      ? data.tracks.filter((track) =>
          [track.name, track.title, track.artist, track.album]
            .filter(Boolean)
            .some((field) => field.toLowerCase().includes(needle)))
      : data.tracks;
    // A library of thousands would put tens of thousands of nodes in the
    // page and make every interaction sticky, so only a window is built -
    // the filter box is how you reach the rest.
    const shown = matches.slice(0, limits[name]);
    const CHUNK = 150;
    const build = (track) => {
      const row = el("tr", track.name === playing && state.running ? "playing" : null);
      const cover = el("td", "col-cover");
      if (track.art) {
        const thumb = el("img", "thumb");
        thumb.src = coverUrl(track);
        thumb.alt = "";
        thumb.loading = "lazy";
        cover.append(thumb);
      } else if (state.art_pending) {
        cover.append(el("span", "thumb waiting"));   // still being looked for
      }
      row.append(cover, el("td", "col-no", track.track ? String(track.track) : ""));

      const title = el("td", "col-title");
      title.append(el("div", "t-title", showFiles ? track.name : (track.title || track.name)));
      title.title = track.target;
      row.append(title);

      // A guess read off the path is shown, but never dressed up as a tag.
      const artist = el("td", "col-artist", track.artist || "—");
      const album = el("td", "col-album", track.album || "—");
      artist.title = track.artist || "";
      album.title = track.album || "";
      if (track.guessed) {
        [artist, album].forEach((cell) => {
          cell.classList.add("guessed");
          cell.title = "Read from the file path — no tags on this file (yet)";
        });
      }
      row.append(artist, album, el("td", "col-length", track.duration ? clock(track.duration) : "—"));

      const actions = el("td", "col-actions");
      if (track.origin === "source") {
        // An icon, not a word: the column is narrow and the meaning only
        // matters when you are about to press ✕ next to it.
        const mark = el("span", "origin");
        mark.innerHTML = '<svg viewBox="0 0 24 24" class="icon">'
          + '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>';
        mark.title = `Played from the folder ${track.source}`;
        actions.append(mark);
      }
      const remove = el("button", "t-remove", "✕");
      remove.title = track.origin === "source"
        ? "Remove from this playlist (the file stays where it is)"
        : `Unlink ${track.name} from this playlist`;
      remove.addEventListener("click", () => removeTrack(track.name, name));
      actions.append(remove);
      row.append(actions);
      return row;
    };

    const rows = shown;
    const paint = (from) => {
      const fragment = document.createDocumentFragment();
      for (let i = from; i < Math.min(from + CHUNK, rows.length); i += 1) {
        fragment.append(build(rows[i]));
      }
      body.append(fragment);
      if (from + CHUNK < rows.length && libraries[name] === data) {
        requestAnimationFrame(() => paint(from + CHUNK));
      }
    };
    paint(0);

    if (matches.length > shown.length) {
      const more = el("tr", "more-row");
      const cell = el("td");
      cell.colSpan = 7;
      cell.append(
        document.createTextNode(`Showing ${shown.length} of ${matches.length} — filter to narrow it down`)
      );
      const all = el("button", "ghost small", "Show all");
      all.addEventListener("click", () => {
        limits[name] = matches.length;
        renderLibrary(name);
      });
      cell.append(all);
      more.append(cell);
      body.append(more);
    }

    broken.forEach((brokenName) => {
      const row = el("tr", "broken");
      row.append(
        el("td", "col-cover", ""),
        el("td", "col-no", ""),
        el("td", "col-title", brokenName),
        el("td", "col-artist", "—"),
        el("td", "col-album", "target missing"),
        el("td", "col-length", "—"),
        el("td", "col-actions", "")
      );
      body.append(row);
    });
  }

  function renderServer(next) {
    $("srv-url").textContent = location.host;
    $("srv-pid").textContent = next.pid || "—";
    $("srv-version").textContent = next.version || "—";
    const up = next.started ? (Date.now() / 1000) - next.started : 0;
    $("srv-up").textContent = up > 3600
      ? `${Math.floor(up / 3600)}h ${Math.floor((up % 3600) / 60)}m`
      : (up > 60 ? `${Math.floor(up / 60)}m` : `${Math.max(0, Math.floor(up))}s`);

    const note = $("srv-note");
    const jobs = [];
    if (next.indexing) jobs.push(`indexing ${next.indexing} folder(s)`);
    if (next.tags_pending) jobs.push(`reading ${next.tags_pending} tag(s)`);
    if (next.art_pending) jobs.push(`reading ${next.art_pending} record(s) for art`);
    note.hidden = !jobs.length;
    if (jobs.length) note.innerHTML = `<span class="spinner"></span> ${jobs.join(", ")}…`;
  }

  function renderSources(next) {
    const list = $("sources-list");
    list.replaceChildren();
    const all = [
      ...next.sources.music.map((s) => ({ ...s, section: "music" })),
      ...next.sources.ambient.map((s) => ({ ...s, section: "ambient" })),
    ];
    if (!all.length) {
      list.append(el("li", "empty", "No folders — this playlist is all links."));
      return;
    }
    all.forEach((source) => {
      const row = el("li", source.exists ? null : "missing");
      const path = el("div", "s-path", shortPath(source.path, 46));
      path.title = source.path;
      const meta = el("span", "s-meta", source.exists ? `${source.count} audio` : "missing");
      const tag = el("span", `tag${source.section === "ambient" ? " source" : ""}`, source.section);
      const remove = el("button", "t-remove", "✕");
      remove.title = `Take ${source.path} out of this playlist (the folder stays)`;
      remove.addEventListener("click", async () => {
        const done = await call("source", { path: source.path, section: source.section, remove: true });
        if (done) { invalidate(); toast(`Removed source ${source.path}`); }
      });
      row.append(path, meta, tag, remove);
      list.append(row);
    });
  }

  // Gaps run from 0 to an hour. A linear slider would put every useful
  // value in the first centimetre, so the travel is curved: fine control
  // down at 10-60s, coarse up in the tens of minutes.
  const MAX_GAP = 3600;
  const CURVE = 3;
  const gapFromSlider = (pos) => Math.round(MAX_GAP * Math.pow(pos / 1000, CURVE));
  const sliderFromGap = (seconds) =>
    Math.round(1000 * Math.pow(Math.min(MAX_GAP, Math.max(0, seconds)) / MAX_GAP, 1 / CURVE));

  const humanSeconds = (value) => {
    const seconds = Math.round(value);
    if (seconds < 60) return `${seconds}s`;
    const minutes = Math.floor(seconds / 60);
    const rest = seconds % 60;
    return rest ? `${minutes}m ${rest}s` : `${minutes}m`;
  };

  // "90", "90s", "3m", "2m30", "1m 30s", "1:30" all mean what they look like.
  function parseSeconds(text) {
    const value = String(text).trim().toLowerCase();
    if (!value) return null;
    let match = value.match(/^(\d+):(\d{1,2})$/);
    if (match) return Number(match[1]) * 60 + Number(match[2]);
    match = value.match(/^(?:(\d+)\s*m)?\s*(?:(\d+)\s*s?)?$/);
    if (!match || (match[1] === undefined && match[2] === undefined)) return null;
    return Number(match[1] || 0) * 60 + Number(match[2] || 0);
  }

  const GAPS = ["gap_min", "gap_max"];
  const SLIDERS = {
    gap_min: humanSeconds,
    gap_max: humanSeconds,
    ambient_chance: (v) => `${v}%`,
    volume: (v) => `${v}`,
    ambient_volume: (v) => `${v}`,
  };

  const isGap = (key) => GAPS.includes(key);
  const setReadout = (key, value) => {
    const node = $(`${key}-out`);
    if (isGap(key)) node.value = SLIDERS[key](value);
    else node.textContent = SLIDERS[key](value);
  };

  function syncSettings(config) {
    Object.keys(SLIDERS).forEach((key) => {
      if (document.activeElement === $(`${key}-out`)) return;   // mid-typing
      const raw = key === "ambient_chance" ? config[key] * 100 : config[key];
      const value = Math.round(raw);
      $(key).value = isGap(key) ? sliderFromGap(value) : value;
      setReadout(key, value);
    });
    $("shuffle").checked = config.shuffle;
    $("loop").checked = config.loop;
    $("hide_gaps").checked = config.hide_gaps;
    $("ambient_random_start").checked = config.ambient_random_start;
  }

  // Keep min <= max whichever one moved, so the server never rejects a pair.
  function gapPatch(key, seconds) {
    const patch = { [key]: seconds };
    if (!state) return patch;
    if (key === "gap_min" && seconds > state.config.gap_max) patch.gap_max = seconds;
    if (key === "gap_max" && seconds < state.config.gap_min) patch.gap_min = seconds;
    return patch;
  }

  // -- libraries (fetched on demand, not in every poll) ----------------
  function renderSkeleton(name) {
    // How many rows we expect, so the page does not jump when they land.
    const expected = Math.min(state ? state.counts[name] : 8, 14) || 8;
    const body = $(`${name}-list`);
    body.replaceChildren();
    for (let i = 0; i < expected; i += 1) {
      const row = el("tr", "skeleton");
      const cover = el("td", "col-cover");
      cover.append(el("span", "sk sk-thumb"));
      const title = el("td", "col-title");
      title.append(el("span", `sk sk-line ${i % 3 ? "mid" : ""}`));
      const artist = el("td", "col-artist");
      artist.append(el("span", "sk sk-line short"));
      const album = el("td", "col-album");
      album.append(el("span", "sk sk-line mid"));
      const length = el("td", "col-length");
      length.append(el("span", "sk sk-line short"));
      row.append(cover, el("td", "col-no", ""), title, artist, album, length,
                 el("td", "col-actions", ""));
      body.append(row);
    }
  }

  async function loadLibrary(name) {
    if (!libraries[name]) renderSkeleton(name);
    try {
      libraries[name] = await api(`library?section=${name}`, undefined, true);
      renderLibrary(name);
    } catch (err) {
      toast(err.message, true);
      $(`${name}-list`).replaceChildren();
    }
  }

  function refreshLibraries() {
    libraries.music = null;
    libraries.ambient = null;
    if (panel === "music" || panel === "ambient") loadLibrary(panel);
  }

  const invalidate = refreshLibraries;

  async function sortBy(column) {
    if (!state) return;
    // The same column again flips the direction; a new one starts ascending.
    const patch = state.sort === column
      ? { sort_desc: !state.config.sort_desc }
      : { sort_by: column, sort_desc: false };
    const done = await call("config", patch);
    if (done) refreshLibraries();
  }

  async function removeTrack(name, sectionName) {
    const done = await call("remove-track", { name, section: sectionName });
    if (!done) return;
    invalidate();
    toast(done.result.how === "unlinked"
      ? `Unlinked ${name}`
      : `${name} removed from this playlist — restore it in Config`);
  }

  // -- add panel ------------------------------------------------------
  async function browse(path) {
    let data;
    try {
      data = await api(`browse${path ? `?path=${encodeURIComponent(path)}` : ""}`);
    } catch (err) {
      $("browser").replaceChildren(el("li", "empty", err.message));
      return;
    }
    browsePath = data.path;
    $("crumb").textContent = data.path;
    $("crumb").title = data.path;
    $("path-input").value = "";

    const shortcuts = $("shortcuts");
    shortcuts.replaceChildren();
    data.shortcuts.forEach((shortcut) => {
      const chip = el("button", "chip", shortcut.name);
      chip.addEventListener("click", () => browse(shortcut.path));
      shortcuts.append(chip);
    });

    const list = $("browser");
    list.replaceChildren();
    const playlistName = state
      ? (state.playlists.find((p) => p.id === state.playlist) || {}).name
      : "";
    list.append(
      folderRow(`This folder → ${playlistName || "playlist"} · ${section}`, data.path, true)
    );

    if (data.parent) {
      const up = el("li");
      up.append(el("div", "t-body", "↑  Parent folder"));
      up.addEventListener("click", () => browse(data.parent));
      list.append(up);
    }
    data.dirs.forEach((dir) => {
      const row = folderRow(`📁 ${dir.name}`, dir.path, false);
      if (dir.audio) row.insertBefore(el("span", "badge", `${dir.audio} audio`), row.lastChild);
      row.addEventListener("click", () => browse(dir.path));
      list.append(row);
    });
    data.files.forEach((file) => {
      const row = el("li");
      const body = el("div", "t-body");
      body.append(el("div", "t-name", `♪ ${file.name}`));
      const add = el("button", "add", "Link");
      add.addEventListener("click", (event) => { event.stopPropagation(); link(file.path); });
      row.append(body, add);
      list.append(row);
    });
    if (!data.dirs.length && !data.files.length) list.append(el("li", "empty", "No folders or audio here"));
  }

  function folderRow(label, path, showPath) {
    const row = el("li");
    const body = el("div", "t-body");
    body.append(el("div", "t-name", label));
    if (showPath) body.append(el("div", "t-target", shortPath(path)));
    const buttons = el("div", "row-buttons");

    const folderButton = el("button", "add", "Add folder");
    folderButton.title = "Put this whole folder in the playlist, played where it stands";
    folderButton.addEventListener("click", (event) => { event.stopPropagation(); addSource(path); });

    const linkButton = el("button", "source-btn", "Link files");
    linkButton.title = "Link the audio files that are in here now, one by one";
    linkButton.addEventListener("click", (event) => { event.stopPropagation(); link(path); });

    buttons.append(folderButton, linkButton);
    row.append(body, buttons);
    return row;
  }

  async function link(path) {
    const next = await call("link", { path, section });
    if (!next) return;
    invalidate();
    const { linked, skipped } = next.result;
    toast(linked.length
      ? `Linked ${linked.length} into ${section}${skipped.length ? `, ${skipped.length} skipped` : ""}`
      : `Nothing linked${skipped.length ? ` — ${skipped[0][1]}` : ""}`);
  }

  async function addSource(path) {
    const next = await call("source", { path, section });
    if (!next) return;
    invalidate();
    toast(`${section === "ambient" ? "Ambience" : "Music"} now plays from ${next.result}`);
  }

  async function chooseFolder() {
    const button = $("pick-folder");
    button.disabled = true;
    button.textContent = "Waiting for the chooser…";
    try {
      const picked = await api("pick", { kind: "folder", title: `Choose a ${section} folder for bgst` });
      if (!picked.available) {
        toast(`No system chooser here (${picked.reason || "no Tk"}) — browse below instead`, true);
      } else if (picked.paths.length) {
        await browse(picked.paths[0]);
        toast("Folder opened — Add folder to play all of it, or Link files");
      }
    } catch (err) {
      toast(err.message, true);
    } finally {
      button.disabled = false;
      button.textContent = "Choose a folder…";
    }
  }

  async function createPlaylist(name, source) {
    const done = await call("playlist", { action: "new", name, source });
    if (!done) return null;
    if (source) await call("playlist", { action: "select", id: done.result });
    invalidate();
    toast(source ? `Playing from ${name}` : `Created ${name}`);
    return done.result;
  }

  // -- export & import --------------------------------------------------
  const EXPORT_NAMES = { json: "bgst-library.json", csv: "bgst-library.csv", bundle: "bgst-bundle.zip" };

  function transferNote(text, bad) {
    const note = $("transfer-note");
    note.hidden = !text;
    note.textContent = text || "";
    note.style.color = bad ? "#ff9ea1" : "";
  }

  async function askForPath(kind, title, suggested) {
    // The desktop dialog when there is one; otherwise the little path box.
    if (state && state.picker) {
      transferNote("Waiting for the file dialog on the machine running bgst…");
      const picked = await api("pick", { kind, title, suggested }).catch(() => null);
      transferNote("");
      if (picked && picked.available) return picked.paths[0] || null;
    }
    return undefined;   // caller falls back to the inline form
  }

  let pendingTransfer = null;

  function askInline(label, placeholder, done) {
    pendingTransfer = done;
    const form = $("transfer-form");
    form.hidden = false;
    $("transfer-go").textContent = label;
    $("transfer-path").placeholder = placeholder;
    $("transfer-path").focus();
  }

  const kindOf = (path) => (/\.zip$/i.test(path || "") ? "bundle" : "manifest");

  async function doExport(kind, path) {
    transferNote(kind === "bundle" ? "Packing the audio…" : "Working…");
    const next = await call("export", { path, kind });
    if (!next) { transferNote("", true); return; }
    const { tracks, total, bytes, skipped, path: written } = next.result;
    const size = bytes > 1e6 ? `${(bytes / 1e6).toFixed(1)} MB` : `${Math.round(bytes / 1024)} KB`;
    const counted = kindOf(written) === "bundle"
      ? `${tracks} track(s) packed`
      : `${total} track(s) listed`;
    transferNote(`Wrote ${written} — ${counted}, ${size}`
      + (skipped.length ? `, ${skipped.length} skipped` : ""));
    toast(`Exported to ${written}`);
  }

  async function doImport(path) {
    transferNote("Working…");
    const next = await call("import", { path });
    if (!next) { transferNote("", true); return; }
    invalidate();
    const { playlists: added, tracks, skipped } = next.result;
    transferNote(`Added ${added.join(", ")} — ${tracks} track(s)`
      + (skipped.length ? `, ${skipped.length} not found here` : ""));
    toast(`Imported ${added.join(", ")}`);
  }

  document.querySelectorAll("[data-export]").forEach((button) =>
    button.addEventListener("click", () => withSpinner(button, async () => {
      const kind = button.dataset.export;
      const suggested = EXPORT_NAMES[kind];
      const picked = await askForPath("save", `Export ${kind === "bundle" ? "bundle" : "manifest"}`, suggested);
      if (picked === undefined) {
        askInline("Export", `where to write ${suggested}`, (path) => doExport(kind, path));
        return;
      }
      if (picked) await doExport(kind, picked);
    })));

  $("import-file").addEventListener("click", async () => {
    const picked = await askForPath("files", "Choose a bgst export to import", "");
    if (picked === undefined) {
      askInline("Import", "path to a .json or .zip export", doImport);
      return;
    }
    if (picked) doImport(picked);
  });

  $("transfer-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const value = $("transfer-path").value.trim();
    if (!value || !pendingTransfer) return;
    $("transfer-form").hidden = true;
    $("transfer-path").value = "";
    const run = pendingTransfer;
    pendingTransfer = null;
    run(value);
  });

  // -- wiring ---------------------------------------------------------
  function showPanel(name) {
    panel = name;
    document.querySelectorAll(".panel").forEach((p) => p.classList.toggle("active", p.id === `panel-${name}`));
    document.querySelectorAll(".rail-btn").forEach((b) => b.classList.toggle("active", b.dataset.panel === name));
    if (name === "add") browse(browsePath);
    if (name === "music" || name === "ambient") loadLibrary(name);
  }

  function setSection(name) {
    section = name;
    document.querySelectorAll(".seg-btn").forEach((b) => b.classList.toggle("active", b.dataset.section === name));
    if (panel === "add") browse(browsePath);
  }

  document.querySelectorAll(".rail-btn").forEach((button) =>
    button.addEventListener("click", () => showPanel(button.dataset.panel)));
  document.querySelectorAll("[data-goto]").forEach((button) =>
    button.addEventListener("click", () => {
      if (button.closest("#panel-ambient")) setSection("ambient");
      if (button.closest("#panel-music")) setSection("music");
      showPanel("add");
    }));
  document.querySelectorAll(".seg-btn").forEach((button) =>
    button.addEventListener("click", () => setSection(button.dataset.section)));

  $("transport").addEventListener("click", () => call("toggle", {}));
  $("skip").addEventListener("click", () => call("skip", {}));
  $("ban").addEventListener("click", toggleBan);
  $("ban-confirm").addEventListener("click", confirmBan);
  document.addEventListener("click", (event) => {
    if (armed && !event.target.closest(".ban-wrap")) disarmBan();
  });
  $("pick-folder").addEventListener("click", chooseFolder);
  $("path-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const value = $("path-input").value.trim();
    if (value) browse(value);
  });
  $("playlist-select").addEventListener("change", (event) => selectPlaylist(event.target.value));
  document.querySelectorAll("th[data-sort]").forEach((head) =>
    head.addEventListener("click", () => sortBy(head.dataset.sort)));
  document.querySelectorAll("input.filter").forEach((box) => {
    let timer = null;
    box.addEventListener("input", () => {
      clearTimeout(timer);
      timer = setTimeout(() => {
        const name = box.dataset.filterFor;
        filters[name] = box.value;
        limits[name] = 400;
        renderLibrary(name);
      }, 120);
    });
  });
  document.querySelectorAll("input.filenames").forEach((box) =>
    box.addEventListener("change", async () => {
      // Follow the column across: sorting by the first column keeps sorting
      // by whatever it now shows.
      const patch = { show_filenames: box.checked };
      if (state && (state.sort === "name" || state.sort === "title")) {
        patch.sort_by = box.checked ? "name" : "title";
      }
      const done = await call("config", patch);
      if (done) refreshLibraries();
    }));
  $("new-playlist-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const value = $("new-playlist-name").value.trim();
    if (!value) return;
    if (await createPlaylist(value, null)) $("new-playlist-name").value = "";
  });
  $("new-from-folder").addEventListener("click", () => {
    const form = $("folder-playlist-form");
    form.hidden = !form.hidden;
    if (!form.hidden) {
      const parts = (browsePath || "").split(/[\\/]/).filter(Boolean);
      $("folder-playlist-name").value = parts[parts.length - 1] || "New playlist";
      $("folder-playlist-name").focus();
      $("folder-playlist-name").select();
    }
  });
  $("folder-playlist-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const value = $("folder-playlist-name").value.trim();
    if (!value || !browsePath) return;
    if (await createPlaylist(value, browsePath)) $("folder-playlist-form").hidden = true;
  });
  $("srv-restart").addEventListener("click", (event) =>
    withSpinner(event.currentTarget, async () => {
      const done = await call("server", { action: "restart" });
      if (done) {
        toast("Restarting — this page reconnects by itself");
        // The new process keeps the token, so polling picks it back up.
        await new Promise((resolve) => setTimeout(resolve, 1500));
      }
    }));

  $("srv-stop").addEventListener("click", (event) =>
    withSpinner(event.currentTarget, async () => {
      await call("server", { action: "stop" });
      offline("Start it again with: bgst ui", "The server was stopped from here.");
    }));

  $("find-covers").addEventListener("click", (event) =>
    withSpinner(event.currentTarget, async () => {
      const done = await call("covers", {});
      if (done) {
        toast(done.result
          ? `Reading ${done.result} record(s) for art — the table fills in as it goes`
          : "Every record has been asked already — use Look again after tagging files");
      }
    }));

  $("recheck-covers").addEventListener("click", (event) =>
    withSpinner(event.currentTarget, async () => {
      // Files that gained art since we last looked are worth a second ask.
      const done = await call("covers", { again: true });
      if (done) {
        toast(done.result
          ? `Reading ${done.result} record(s) again`
          : "Nothing to read");
      }
    }));

  $("prune").addEventListener("click", async () => {
    const next = await call("prune", {});
    if (next) { invalidate(); toast(`${next.result.length} broken link(s) removed`); }
  });

  Object.keys(SLIDERS).forEach((key) => {
    const input = $(key);
    input.addEventListener("input", () => {
      setReadout(key, isGap(key) ? gapFromSlider(Number(input.value)) : Number(input.value));
    });
    input.addEventListener("change", async () => {
      const raw = Number(input.value);
      if (isGap(key)) {
        await call("config", gapPatch(key, gapFromSlider(raw)));
        return;
      }
      const done = await call("config", { [key]: key === "ambient_chance" ? raw / 100 : raw });
      if (done && done.live === false && key.endsWith("volume") && state.running) {
        toast("Volume applies from the next track — no system mixer here");
      }
    });
  });

  GAPS.forEach((key) => {
    const box = $(`${key}-out`);
    const commit = async () => {
      const seconds = parseSeconds(box.value);
      if (seconds === null || seconds > MAX_GAP) {
        setReadout(key, state ? state.config[key] : 0);
        if (seconds !== null) toast(`The longest gap is ${humanSeconds(MAX_GAP)}`, true);
        return;
      }
      await call("config", gapPatch(key, seconds));
    };
    box.addEventListener("keydown", (event) => {
      if (event.key === "Enter") { event.preventDefault(); box.blur(); }
      if (event.key === "Escape") { setReadout(key, state.config[key]); box.blur(); }
    });
    box.addEventListener("blur", commit);
  });

  ["shuffle", "loop", "hide_gaps", "ambient_random_start"].forEach((key) =>
    $(key).addEventListener("change", () => call("config", { [key]: $(key).checked })));

  document.addEventListener("keydown", (event) => {
    if (event.target.matches("input, textarea")) return;
    if (event.key === " ") { event.preventDefault(); call("toggle", {}); }
    if (event.key === "n") call("skip", {});
    if (event.key === "b") toggleBan();      // once to arm, again to confirm
    if (event.key === "Escape") disarmBan();
  });

  // -- polling --------------------------------------------------------
  let failures = 0;
  async function poll() {
    try {
      render(await api("state", undefined, true));   // quiet: no activity bar
      failures = 0;
      if (!state || !state.stale) $("offline").hidden = true;
    } catch (err) {
      $("now-kind").textContent = "Disconnected";
      if (!SERVED) {
        offline();
      } else if (++failures > 2) {
        offline(
          /token/i.test(err.message)
            ? "This page's key is out of date — open the link bgst printed, or run 'bgst ui' again."
            : `${err.message}. It may have stopped, or been updated while running — `
              + `restart it: bgst ui --stop, then bgst ui.`,
          /token/i.test(err.message) ? "That key is not valid." : "Lost the bgst server."
        );
      }
    }
  }

  if (!SERVED) {
    offline();
  } else {
    poll();
    setInterval(poll, 1000);
  }
})();
