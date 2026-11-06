/* KIM Panel — ein Panel, ein Ziel, ein Knopf.
   Vanilla-Web-Component, kein Build-Schritt. Nutzt die HA-WebSocket-API. */

const CROWN = `<svg viewBox="0 0 24 24" fill="none" stroke="#e8c46a" stroke-width="1.4"
  stroke-linecap="round" stroke-linejoin="round">
  <path d="M3 8l4 3 5-6 5 6 4-3-2 11H5L3 8z"/><circle cx="12" cy="19" r="0.6" fill="#e8c46a"/>
</svg>`;

const STATE_LABEL = {
  idle: "bereit", working: "arbeitet", done: "fertig", failed: "fehler",
  stalled: "haengt", offline: "offline",
};
const TASK_LABEL = {
  pending: "wartet", assigned: "zugewiesen", in_progress: "laeuft", blocked: "blockiert",
  done: "fertig", failed: "fehler", cancelled: "abgebrochen",
};

function esc(text) {
  return String(text ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function ago(ts) {
  if (!ts) return "";
  const seconds = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (seconds < 60) return `vor ${seconds}s`;
  if (seconds < 3600) return `vor ${Math.floor(seconds / 60)}min`;
  return `vor ${Math.floor(seconds / 3600)}h`;
}

function fmtBytes(n) {
  const size = Number(n) || 0;
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
}

class KimPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._entryId = null;
    this._timer = null;
    this._data = null;
    this._sending = false;
    this._archive = [];
    this._built = false;
    this._detail = false;
  }

  set panel(panel) {
    this._panel = panel;
    this._entryId = (panel && panel.config && panel.config.entry_id) || null;
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first && hass) {
      if (!this._entryId && hass.panels && hass.panels.kim) {
        this._entryId = (hass.panels.kim.config || {}).entry_id || null;
      }
      this._build();
      this._refresh();
      this._timer = window.setInterval(() => this._refresh(), 4000);
    }
  }

  disconnectedCallback() {
    if (this._timer) window.clearInterval(this._timer);
    if (this._raf) cancelAnimationFrame(this._raf);
  }

  async _send(type, extra = {}) {
    if (!this._hass || !this._hass.connection) return null;
    try {
      return await this._hass.connection.sendMessagePromise({
        type, entry_id: this._entryId || undefined, ...extra,
      });
    } catch (err) {
      this._toast(`Fehler: ${err.message || err}`);
      return null;
    }
  }

  async _refresh() {
    const snap = await this._send("kim/snapshot");
    if (snap) {
      this._data = snap;
      this._paint();
    }
  }

  async _submit() {
    const box = this.shadowRoot.getElementById("goal");
    const goal = (box.value || "").trim();
    if (!goal) { this._toast("Bitte ein Ziel eingeben."); return; }
    this._sending = true;
    this._paintHeader();
    const res = await this._send("kim/submit", { goal, title: goal.slice(0, 60) });
    this._sending = false;
    if (res && res.ok) {
      box.value = "";
      this._toast("Auftrag erteilt. Die Agenten beginnen.");
      this._refresh();
    }
  }

  async _loadArchive() {
    const res = await this._send("kim/archive/list");
    if (res) {
      this._archive = res.runs || [];
      this._archiveTarget = res.target || "?";
      this._paintArchive();
    }
  }

  async _saveArchive() {
    const res = await this._send("kim/archive/save");
    if (res) this._toast(`Lauf gespeichert (${res.target}).`);
    this._loadArchive();
  }

  async _collectArtifacts() {
    this._toast("Sammle Dateien der Agenten ...");
    const res = await this._send("kim/artifacts/collect");
    if (res) {
      const ok = (res.collected || []).filter((c) => !c.error).length;
      this._toast(`${ok} Aufgabe(n) gesichert.`);
      this._refresh();
    }
  }

  _toast(text) {
    const el = this.shadowRoot.getElementById("toast");
    if (!el) return;
    el.textContent = text;
    el.classList.add("show");
    window.clearTimeout(this._toastTimer);
    this._toastTimer = window.setTimeout(() => el.classList.remove("show"), 3200);
  }

  _build() {
    if (this._built) return;
    this._built = true;
    const root = this.shadowRoot;
    root.innerHTML = `
      <link rel="stylesheet" href="/kim_frontend/kim-panel.css">
      <div class="wrap">
        <div id="veil"></div>
        <canvas id="shards"></canvas>
        <div id="grain"></div>
        <div class="page">
          <header class="crest">
            <div class="crown">${CROWN}</div>
            <div class="title">
              <h1>KIM</h1>
              <p>Sag was zu tun ist. KIM verteilt die Arbeit.</p>
            </div>
            <div class="crest-right">
              <span class="pill" id="state-pill"><span class="dot" id="state-dot"></span><span id="state-text">verbinde ...</span></span>
            </div>
          </header>

          <div class="alert" id="alert" style="display:none"></div>

          <section class="commission">
            <div class="row">
              <textarea id="goal" rows="2"
                placeholder="Was soll erledigt werden? Ein Satz genuegt."></textarea>
              <button class="throne" id="submit">Los</button>
            </div>
          </section>

          <section class="stats">
            <div class="stat"><div class="v gold" id="s-running">0</div><div class="k">laeuft</div></div>
            <div class="stat"><div class="v" id="s-done">0</div><div class="k">fertig</div></div>
            <div class="stat"><div class="v" id="s-pending">0</div><div class="k">wartet</div></div>
            <div class="stat"><div class="v" id="s-agents">0</div><div class="k">Agenten</div></div>
          </section>

          <section class="card">
            <h2>Aufgaben <span class="count" id="plan-name"></span></h2>
            <div id="tasks"><div class="empty">Noch kein Auftrag.</div></div>
          </section>

          <section class="card">
            <h2>Ergebnisse
              <button class="ghost" id="collect" style="margin-left:auto">Dateien einsammeln</button>
            </h2>
            <div id="artifacts"><div class="empty">Noch keine Dateien.</div></div>
          </section>

          <section class="card">
            <h2>Verlauf
              <button class="ghost" id="archive-reload" style="margin-left:auto">Anzeigen</button>
            </h2>
            <div id="archive"><div class="empty">Noch nichts gespeichert.</div></div>
          </section>

          <section class="card">
            <button class="ghost wide" id="detail-toggle">Details anzeigen</button>
            <div id="detail" style="display:none">
              <div class="sub">
                <h3>Agenten</h3>
                <div id="agents"><div class="empty">Keine Agenten aktiv.</div></div>
              </div>
              <div class="sub">
                <h3>Notizen</h3>
                <div id="notes"><div class="empty">Noch keine Notizen.</div></div>
              </div>
            </div>
          </section>

          <footer>KIM &middot; KI-Manager</footer>
        </div>
        <div class="toast" id="toast"></div>
      </div>`;

    root.getElementById("submit").addEventListener("click", () => this._submit());
    root.getElementById("goal").addEventListener("keydown", (e) => {
      if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) this._submit();
    });
    root.getElementById("archive-reload").addEventListener("click", () => this._loadArchive());
    root.getElementById("collect").addEventListener("click", () => this._collectArtifacts());
    root.getElementById("detail-toggle").addEventListener("click", () => {
      this._detail = !this._detail;
      const box = root.getElementById("detail");
      box.style.display = this._detail ? "" : "none";
      root.getElementById("detail-toggle").textContent =
        this._detail ? "Details verbergen" : "Details anzeigen";
    });
    this._startShards();
  }

  _paintHeader() {
    const root = this.shadowRoot;
    const data = this._data || {};
    const dot = root.getElementById("state-dot");
    const text = root.getElementById("state-text");
    const button = root.getElementById("submit");
    if (button) {
      button.disabled = this._sending;
      button.textContent = this._sending ? "..." : "Los";
    }
    let state = "idle";
    if (data.config_error) state = "error";
    else if (data.paused) state = "paused";
    else if ((data.running_tasks || 0) > 0) state = "working";
    else if ((data.tasks_pending || 0) > 0) state = "scheduling";
    const map = { idle: "ok", working: "work", scheduling: "warn", paused: "bad", error: "bad" };
    dot.className = `dot ${map[state] || ""}`;
    text.textContent = {
      idle: "bereit", working: "arbeitet", scheduling: "plant", paused: "pausiert", error: "Fehler",
    }[state] || state;

    const alert = root.getElementById("alert");
    const backend = data.backend || {};
    const degraded = backend.degraded || backend.active === "mock";
    if (data.config_error) {
      alert.style.display = "";
      alert.className = "alert error";
      alert.textContent = data.config_error;
    } else if (degraded) {
      alert.style.display = "";
      alert.className = "alert warn";
      const why = backend.reason ? ` Grund: ${backend.reason}` : "";
      alert.textContent =
        `Achtung: Es laufen keine echten Agenten - aktiv ist '${backend.active}'.` +
        `${why} Die Ergebnisse sind Simulationen. Cloud-Kontingent/Modell pruefen.`;
    } else {
      alert.style.display = "none";
    }
  }

  _paint() {
    const data = this._data || {};
    const root = this.shadowRoot;
    this._paintHeader();

    root.getElementById("s-running").textContent = data.running_tasks || 0;
    root.getElementById("s-done").textContent = data.tasks_completed || 0;
    root.getElementById("s-pending").textContent = data.tasks_pending || 0;
    root.getElementById("s-agents").textContent = data.worker_count || 0;

    const plans = data.plans || [];
    const plan = plans[plans.length - 1];
    const tasks = plan ? plan.tasks || [] : [];
    root.getElementById("plan-name").textContent = plan ? plan.title : "";
    const tasksBox = root.getElementById("tasks");
    if (!tasks.length) {
      tasksBox.innerHTML = `<div class="empty">Noch kein Auftrag.</div>`;
    } else {
      tasksBox.innerHTML = tasks.map((t) => {
        const waiting = t.state === "pending" && (t.depends_on || []).length;
        return `
        <div class="task" data-state="${esc(t.state)}">
          <div class="t"><span class="role">${esc(t.role || "?")}</span><span>${esc(t.title)}</span></div>
          <div class="meta">${esc(TASK_LABEL[t.state] || t.state)}${waiting ? " &middot; wartet auf Vorgaenger" : ""}</div>
          ${t.result ? `<div class="res">${esc(String(t.result).slice(0, 400))}</div>` : ""}
          ${t.error ? `<div class="res" style="color:var(--bad)">${esc(String(t.error).slice(0, 240))}</div>` : ""}
        </div>`;
      }).join("");
    }

    const workers = data.workers || [];
    const agentsBox = root.getElementById("agents");
    agentsBox.innerHTML = workers.length
      ? workers.map((w) => `
        <div class="agent">
          <span class="nm">${esc(w.role)}</span>
          <span class="st">${esc(STATE_LABEL[w.state] || w.state)}</span>
        </div>`).join("")
      : `<div class="empty">Keine Agenten aktiv.</div>`;

    const arts = data.artifacts || [];
    const artBox = root.getElementById("artifacts");
    if (!arts.length) {
      artBox.innerHTML = `<div class="empty">Noch keine Dateien. Nach dem Lauf "Dateien einsammeln".</div>`;
    } else {
      artBox.innerHTML = arts.map((a) => {
        const files = (a.files || []);
        const shown = files.slice(0, 12);
        const more = files.length > shown.length ? `<div class="meta">+${files.length - shown.length} weitere</div>` : "";
        return `
        <div class="artifact">
          <div class="t"><span class="role">${esc(a.role)}</span><span>${esc(a.title)}</span></div>
          <div class="meta">${files.length} Datei(en) &middot; <a href="${esc(a.dir)}" target="_blank">Ordner oeffnen</a></div>
          ${shown.map((f) => `<div class="file"><a href="${esc(f.url)}" target="_blank">${esc(f.path)}</a> <span class="sz">${fmtBytes(f.bytes)}</span></div>`).join("")}
          ${more}
        </div>`;
      }).join("");
    }

    const notes = (data.notes || []).slice(-14).reverse();
    const notesBox = root.getElementById("notes");
    notesBox.innerHTML = notes.length
      ? notes.map((n) => `
        <div class="note">
          <span class="who">${esc(n.author)}</span>
          <span class="when"> ${esc(ago(n.created_at))}</span>
          <div class="txt">${esc(String(n.text || "").slice(0, 300))}</div>
        </div>`).join("")
      : `<div class="empty">Noch keine Notizen.</div>`;
  }

  _paintArchive() {
    const root = this.shadowRoot;
    const box = root.getElementById("archive");
    if (!this._archive.length) {
      box.innerHTML = `<div class="empty">Noch nichts gespeichert.</div>`;
      return;
    }
    box.innerHTML = this._archive.slice(-12).reverse().map((r) => `
      <div class="run">
        <span>${esc(r.name)}</span>
        <span class="sz">${r.size ? `${Math.max(1, Math.round(r.size / 1024))} KB` : ""}</span>
        <button class="ghost load" data-name="${esc(r.name)}">Laden</button>
      </div>`).join("");
    box.querySelectorAll("button.load").forEach((btn) => {
      btn.addEventListener("click", () => this._loadRun(btn.dataset.name));
    });
  }

  async _loadRun(name) {
    const res = await this._send("kim/archive/load", { name });
    if (res && res.ok && res.run) {
      this._toast(`Lauf '${name}' geladen.`);
      this._refresh();
    } else {
      this._toast(`Lauf '${name}' konnte nicht geladen werden.`);
    }
  }

  /* ---------- Hintergrund: System-of-a-Down-Energie ---------- */
  _startShards() {
    const canvas = this.shadowRoot.getElementById("shards");
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    let w = 0, h = 0, dpr = Math.min(window.devicePixelRatio || 1, 2);
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    const resize = () => {
      w = canvas.clientWidth; h = canvas.clientHeight;
      canvas.width = w * dpr; canvas.height = h * dpr;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    };
    resize();
    window.addEventListener("resize", resize);

    const make = () => ({
      x: Math.random() * w, y: Math.random() * h,
      r: 14 + Math.random() * 60,
      a: Math.random() * Math.PI * 2,
      spin: (Math.random() - 0.5) * 0.01,
      vx: (Math.random() - 0.5) * 0.9,
      vy: (Math.random() - 0.5) * 0.9,
      hue: Math.random() < 0.6 ? "232,196,106" : "150,140,255",
      alpha: 0.05 + Math.random() * 0.16,
    });
    const shards = [];
    for (let i = 0; i < 34; i++) shards.push(make());

    let t = 0;
    let raf = null;
    const draw = () => {
      t += reduced ? 0.004 : 0.016;
      ctx.clearRect(0, 0, w, h);

      // 1. wogende Energiewellen (wie ein Bass-Puls)
      const bands = 3;
      for (let b = 0; b < bands; b++) {
        ctx.beginPath();
        const amp = 26 + b * 20 + Math.sin(t * 1.6 + b) * 14;
        const speed = 0.9 + b * 0.5;
        for (let x = 0; x <= w; x += 12) {
          const y = h * (0.32 + b * 0.22)
            + Math.sin(x * 0.006 * speed + t * (1.5 + b)) * amp
            + Math.sin(x * 0.02 + t * 3) * (amp * 0.28);
          x === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
        }
        ctx.strokeStyle = b === 1
          ? `rgba(232,196,106,${0.16 - b * 0.03})`
          : `rgba(150,140,255,${0.14 - b * 0.03})`;
        ctx.lineWidth = 1.4;
        ctx.stroke();
      }

      // 2. pulsierendes Gitter
      const gap = 46;
      const glow = 0.03 + (Math.sin(t * 2.2) + 1) * 0.03;
      ctx.strokeStyle = `rgba(232,196,106,${glow})`;
      ctx.lineWidth = 1;
      ctx.beginPath();
      for (let x = 0; x <= w; x += gap) { ctx.moveTo(x, 0); ctx.lineTo(x, h); }
      for (let y = 0; y <= h; y += gap) { ctx.moveTo(0, y); ctx.lineTo(w, y); }
      ctx.stroke();

      // 3. rotierende Splitter mit Leuchtspur
      for (const s of shards) {
        s.x += s.vx; s.y += s.vy; s.a += s.spin;
        if (s.x < -90) s.x = w + 90; if (s.x > w + 90) s.x = -90;
        if (s.y < -90) s.y = h + 90; if (s.y > h + 90) s.y = -90;
        ctx.save();
        ctx.translate(s.x, s.y);
        ctx.rotate(s.a);
        ctx.strokeStyle = `rgba(${s.hue},${s.alpha})`;
        ctx.shadowColor = `rgba(${s.hue},0.5)`;
        ctx.shadowBlur = 12;
        ctx.lineWidth = 1.2;
        ctx.beginPath();
        for (let i = 0; i < 3; i++) {
          const ang = (i / 3) * Math.PI * 2;
          const px = Math.cos(ang) * s.r, py = Math.sin(ang) * s.r;
          i === 0 ? ctx.moveTo(px, py) : ctx.lineTo(px, py);
        }
        ctx.closePath();
        ctx.stroke();
        ctx.restore();
      }

      // 4. gelegentlicher Glitch-Blitz
      if (!reduced && Math.random() < 0.02) {
        ctx.fillStyle = `rgba(255,${Math.floor(Math.random() * 120)},120,0.06)`;
        ctx.fillRect(0, Math.random() * h, w, 2 + Math.random() * 6);
      }
      raf = requestAnimationFrame(draw);
    };
    raf = requestAnimationFrame(draw);
    this._raf = raf;
  }
}

if (!customElements.get("kim-panel")) {
  customElements.define("kim-panel", KimPanel);
}
