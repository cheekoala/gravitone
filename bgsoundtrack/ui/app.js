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
  let panel = "now";
  let section = "music";      // which library the Add panel targets
  let browsePath = null;
  let toastTimer = null;
  const libraries = { music: null, ambient: null };

  // -- server ---------------------------------------------------------
  async function api(route, body) {
    if (!SERVED) throw new Error("not served by bgst");
    const res = await fetch(`/api/${route}`, {
      method: body === undefined ? "GET" : "POST",
      headers: { "X-BGST-Token": TOKEN, "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const payload = await res.json().catch(() => ({ error: `HTTP ${res.status}` }));
    if (!res.ok) throw new Error(payload.error || `HTTP ${res.status}`);
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

  function offline(detail) {
    $("offline").hidden = false;
    if (detail) $("offline-detail").textContent = detail;
  }

  async function call(route, body) {
    try {
      const next = await api(route, body);
      if (next && next.config) render(next);
      return next;
    } catch (err) {
      toast(err.message, true);
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

  async function ban() {
    const done = await call("ban", {});
    if (!done) return;
    invalidate();
    toast(done.result.how === "unlinked"
      ? `Banned ${done.result.name} — unlinked and skipped`
      : `Banned ${done.result.name} — out of this playlist, file untouched`);
  }

  // -- render ---------------------------------------------------------
  function render(next) {
    state = next;
    const { config, now, running } = next;

    $("root-path").textContent = shortPath(config.root, 44);
    $("transport").dataset.on = String(running);
    $("transport-label").textContent = running ? "Live" : "Play";
    $("skip").disabled = !running;
    $("ban").disabled = !running || !now || now.kind === "silence";

    const nowPanel = $("panel-now");
    const inGap = !!now && now.kind !== "track";
    nowPanel.classList.toggle("is-gap", inGap);
    // The server withholds gap times in hidden mode; there is nothing to show.
    nowPanel.classList.toggle("is-hidden", inGap && next.hidden);
    const kinds = { track: "Now playing", ambient: "Ambience", silence: "Silence" };
    $("now-kind").textContent = now ? kinds[now.kind] : (running ? "Starting" : "Idle");
    $("now-title").textContent = now
      ? (now.kind === "silence" ? "Quiet" : now.name)
      : (next.error ? "Stopped" : "Nothing playing");
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

    renderPlaylistPicker(next);
    renderPlaylists(next);
    renderRemoved(next);

    $("music-count").textContent = String(next.counts.music);
    $("ambient-count").textContent = String(next.counts.ambient);
    renderLibrary("music");
    renderLibrary("ambient");
    renderSources(next);

    if (!document.activeElement || document.activeElement.type !== "range") syncSettings(config);

    const players = next.players.length ? next.players.join(", ") : "none found";
    let note = `Players available: ${players}. ` +
      (next.players.length ? "" : "Install ffmpeg, mpv or vlc to hear anything. ");
    if (!next.picker) note += "No system file chooser here — use the built-in browser or paste a path. ";
    if (next.error) note += `— ${next.error}`;
    $("backend-note").textContent = note;
    $("pick-folder").hidden = !next.picker;
  }

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
        `${item.sources} source${item.sources === 1 ? "" : "s"}` +
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
    const list = $(`${name}-list`);
    const data = libraries[name];
    if (!data) return;                       // not fetched yet
    const broken = data.broken || [];
    list.replaceChildren();

    if (!data.tracks.length && !broken.length) {
      list.append(el("li", "empty", name === "music"
        ? "No music yet — open Add, then link files or add a source folder."
        : "No ambience yet — rain, wind, room tone, a distant tavern."));
      return;
    }

    const playing = state && state.now && state.now.name;
    data.tracks.forEach((track) => {
      const row = el("li", track.name === playing && state.running ? "playing" : null);
      const body = el("div", "t-body");
      body.append(el("div", "t-name", track.name), el("div", "t-target", shortPath(track.target)));
      row.append(body);
      if (track.origin === "source") {
        const tag = el("span", "tag source", "source");
        tag.title = `From the source folder ${track.source}`;
        row.append(tag);
      }
      const remove = el("button", "t-remove", "✕");
      remove.title = track.origin === "source"
        ? `Remove from this playlist (the file stays where it is)`
        : `Unlink ${track.name} from this playlist`;
      remove.addEventListener("click", () => removeTrack(track.name, name));
      row.append(remove);
      list.append(row);
    });
    broken.forEach((brokenName) => {
      const row = el("li", "broken");
      const body = el("div", "t-body");
      body.append(el("div", "t-name", brokenName), el("div", "t-target", "target missing"));
      row.append(body);
      list.append(row);
    });
  }

  function renderSources(next) {
    const list = $("sources-list");
    list.replaceChildren();
    const all = [
      ...next.sources.music.map((s) => ({ ...s, section: "music" })),
      ...next.sources.ambient.map((s) => ({ ...s, section: "ambient" })),
    ];
    if (!all.length) {
      list.append(el("li", "empty", "No source folders — everything is linked."));
      return;
    }
    all.forEach((source) => {
      const row = el("li", source.exists ? null : "missing");
      const path = el("div", "s-path", shortPath(source.path, 46));
      path.title = source.path;
      const meta = el("span", "s-meta", source.exists ? `${source.count} audio` : "missing");
      const tag = el("span", `tag${source.section === "ambient" ? " source" : ""}`, source.section);
      const remove = el("button", "t-remove", "✕");
      remove.title = `Stop playing ${source.path}`;
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
  async function loadLibrary(name) {
    try {
      libraries[name] = await api(`library?section=${name}`);
      renderLibrary(name);
    } catch (err) {
      toast(err.message, true);
    }
  }

  function invalidate() {
    libraries.music = null;
    libraries.ambient = null;
    if (panel === "music" || panel === "ambient") loadLibrary(panel);
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
    list.append(folderRow(`This folder → ${section}`, data.path, true));

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

    const linkButton = el("button", "add", "Link all");
    linkButton.title = "Symlink every audio file in here into the library";
    linkButton.addEventListener("click", (event) => { event.stopPropagation(); link(path); });

    const sourceButton = el("button", "source-btn", "Source");
    sourceButton.title = "Play this folder in place, including anything added later";
    sourceButton.addEventListener("click", (event) => { event.stopPropagation(); addSource(path); });

    buttons.append(linkButton, sourceButton);
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
    toast(`Playing ${section} from ${next.result}`);
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
        toast("Folder opened — Link all, or Source to play it in place");
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
  $("ban").addEventListener("click", ban);
  $("pick-folder").addEventListener("click", chooseFolder);
  $("path-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const value = $("path-input").value.trim();
    if (value) browse(value);
  });
  $("playlist-select").addEventListener("change", (event) => selectPlaylist(event.target.value));
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
    if (event.key === "b") ban();
  });

  // -- polling --------------------------------------------------------
  let failures = 0;
  async function poll() {
    try {
      render(await api("state"));
      failures = 0;
      $("offline").hidden = true;
    } catch (err) {
      $("now-kind").textContent = "Disconnected";
      if (!SERVED) {
        offline();
      } else if (++failures > 2) {
        offline(
          /token/i.test(err.message)
            ? "This page's key is out of date — open the link bgst printed, or run 'bgst ui' again."
            : `Cannot reach the bgst server (${err.message}). Is it still running?`
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
