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
      <link rel="stylesheet" href="/kim/kim-panel.css">
      <div class="wrap">
        <div id="veil"></div>
        <canvas id="shards"></canvas>
        <div id="grain"></div>
        <div class="page">
          <header class="crest">
            <div class="crown">${CROWN}</div>
            <div class="title">
              <h1>KIM</h1>
              <p>Kuenstliche Intelligenz, die arbeitet. Du regierst, sie liefern.</p>
            </div>
            <div class="crest-right">
              <span class="pill" id="state-pill"><span class="dot" id="state-dot"></span><span id="state-text">verbinde ...</span></span>
              <span class="pill" id="backend-pill" title="Aktives Backend">--</span>
            </div>
          </header>

          <section class="commission">
            <label for="goal">Dein Auftrag</label>
            <div class="row">
              <textarea id="goal" rows="2"
                placeholder="Beschreibe in einem Satz, was erledigt werden soll. KIM zerlegt es selbst und verteilt die Arbeit."></textarea>
              <button class="throne" id="submit">Auftrag erteilen</button>
            </div>
            <div class="hint">Ein Satz genuegt. KIM plant, teilt auf, ueberwacht und faengt Ausfaelle selbst ab.</div>
          </section>

          <section class="stats">
            <div class="stat"><div class="v gold" id="s-running">0</div><div class="k">aktiv</div></div>
            <div class="stat"><div class="v" id="s-done">0</div><div class="k">fertig</div></div>
            <div class="stat"><div class="v" id="s-pending">0</div><div class="k">in Warteschlange</div></div>
            <div class="stat"><div class="v" id="s-agents">0</div><div class="k">Agenten</div></div>
          </section>

          <section class="grid">
            <div>
              <div class="card" style="margin-bottom:18px">
                <h2>Aufgaben <span class="count" id="plan-name"></span></h2>
                <div id="tasks"><div class="empty">Noch kein Auftrag. Oben eintippen und los.</div></div>
              </div>
            </div>
            <div>
              <div class="card" style="margin-bottom:18px">
                <h2>Agenten <span class="count" id="agent-count"></span></h2>
                <div id="agents"><div class="empty">Noch keine Agenten aktiv.</div></div>
              </div>
              <div class="card" style="margin-bottom:18px">
                <h2>Notizzettel <span class="count" id="note-count"></span></h2>
                <div id="notes"><div class="empty">Die Agenten schreiben hier ihre Absprachen.</div></div>
              </div>
              <div class="card">
                <h2>Archiv
                  <span class="archive-head" style="margin-left:auto">
                    <span class="count" id="archive-target"></span>
                    <button class="ghost" id="archive-save">Jetzt speichern</button>
                    <button class="ghost" id="archive-reload">Anzeigen</button>
                  </span>
                </h2>
                <div id="archive"><div class="empty">Laeufe werden ausserhalb von Home Assistant gespeichert.</div></div>
              </div>
            </div>
          </section>

          <footer>KIM &middot; KI-Manager &middot; gefertigt fuer Home Assistant</footer>
        </div>
        <div class="toast" id="toast"></div>
      </div>`;

    root.getElementById("submit").addEventListener("click", () => this._submit());
    root.getElementById("goal").addEventListener("keydown", (e) => {
      if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) this._submit();
    });
    root.getElementById("archive-save").addEventListener("click", () => this._saveArchive());
    root.getElementById("archive-reload").addEventListener("click", () => this._loadArchive());
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
      button.textContent = this._sending ? "Sendet ..." : "Auftrag erteilen";
    }
    let state = "idle";
    if (data.paused) state = "paused";
    else if ((data.running_tasks || 0) > 0) state = "working";
    else if ((data.tasks_pending || 0) > 0) state = "scheduling";
    const map = { idle: "ok", working: "work", scheduling: "warn", paused: "bad" };
    dot.className = `dot ${map[state] || ""}`;
    text.textContent = { idle: "bereit", working: "arbeitet", scheduling: "plant", paused: "pausiert" }[state] || state;

    const backend = data.backend || {};
    const active = backend.active || "--";
    const cooldowns = backend.cooldowns || {};
    const cooling = Object.keys(cooldowns);
    const pill = root.getElementById("backend-pill");
    pill.textContent = cooling.length ? `${active} (${cooling.join(", ")} kuehlt)` : active;
    pill.title = `Kette: ${(backend.chain || []).join(" -> ") || "-"}`;
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
      tasksBox.innerHTML = `<div class="empty">Noch kein Auftrag. Oben eintippen und los.</div>`;
    } else {
      tasksBox.innerHTML = tasks.map((t) => `
        <div class="task" data-state="${esc(t.state)}">
          <div class="t"><span class="role">${esc(t.role || "?")}</span><span>${esc(t.title)}</span></div>
          <div class="meta">${esc(TASK_LABEL[t.state] || t.state)}${t.depends_on && t.depends_on.length ? " &middot; wartet auf Vorgaenger" : ""}${t.attempts ? ` &middot; Versuch ${t.attempts}` : ""}</div>
          ${t.result ? `<div class="res">${esc(String(t.result).slice(0, 400))}</div>` : ""}
          ${t.error ? `<div class="res" style="color:var(--bad)">${esc(String(t.error).slice(0, 240))}</div>` : ""}
        </div>`).join("");
    }

    const workers = data.workers || [];
    root.getElementById("agent-count").textContent = workers.length ? `${workers.length}` : "";
    const agentsBox = root.getElementById("agents");
    agentsBox.innerHTML = workers.length
      ? workers.map((w) => `
        <div class="agent">
          <span class="nm">${esc(w.role)}</span>
          <span style="color:#5d5a6b;font-size:11.5px">${esc(w.worker_id.slice(-6))}</span>
          <span class="st">${esc(STATE_LABEL[w.state] || w.state)}</span>
        </div>`).join("")
      : `<div class="empty">Noch keine Agenten aktiv.</div>`;

    const notes = (data.notes || []).slice(-14).reverse();
    root.getElementById("note-count").textContent = notes.length ? `${notes.length}` : "";
    const notesBox = root.getElementById("notes");
    notesBox.innerHTML = notes.length
      ? notes.map((n) => `
        <div class="note">
          <span class="who">${esc(n.author)}</span>
          <span class="when"> ${esc(ago(n.created_at))}</span>
          <div class="txt">${esc(String(n.text || "").slice(0, 300))}</div>
        </div>`).join("")
      : `<div class="empty">Die Agenten schreiben hier ihre Absprachen.</div>`;
  }

  _paintArchive() {
    const root = this.shadowRoot;
    root.getElementById("archive-target").textContent = this._archiveTarget || "";
    const box = root.getElementById("archive");
    if (!this._archive.length) {
      box.innerHTML = `<div class="empty">Noch keine Laeufe gespeichert.</div>`;
      return;
    }
    box.innerHTML = this._archive.slice(-12).reverse().map((r) => `
      <div class="run">
        <span>${esc(r.name)}</span>
        <span class="sz">${r.size ? `${Math.max(1, Math.round(r.size / 1024))} KB` : ""}</span>
      </div>`).join("");
  }

  /* ---------- Hintergrund: langsam treibende Splitter ---------- */
  _startShards() {
    const canvas = this.shadowRoot.getElementById("shards");
    if (!canvas) return;
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    const ctx = canvas.getContext("2d");
    let w = 0, h = 0, dpr = Math.min(window.devicePixelRatio || 1, 2);
    const shards = [];

    const resize = () => {
      w = canvas.clientWidth; h = canvas.clientHeight;
      canvas.width = w * dpr; canvas.height = h * dpr;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    };
    const make = () => ({
      x: Math.random() * w, y: Math.random() * h,
      r: 12 + Math.random() * 46,
      a: Math.random() * Math.PI * 2,
      spin: (Math.random() - 0.5) * 0.0016,
      vx: (Math.random() - 0.5) * 0.16,
      vy: (Math.random() - 0.5) * 0.16,
      hue: Math.random() < 0.78 ? "232,196,106" : "150,140,255",
      alpha: 0.05 + Math.random() * 0.14,
    });

    resize();
    for (let i = 0; i < 26; i++) shards.push(make());
    window.addEventListener("resize", resize);

    const draw = () => {
      ctx.clearRect(0, 0, w, h);
      for (const s of shards) {
        s.x += s.vx; s.y += s.vy; s.a += s.spin;
        if (s.x < -80) s.x = w + 80; if (s.x > w + 80) s.x = -80;
        if (s.y < -80) s.y = h + 80; if (s.y > h + 80) s.y = -80;
        ctx.save();
        ctx.translate(s.x, s.y);
        ctx.rotate(s.a);
        ctx.strokeStyle = `rgba(${s.hue},${s.alpha})`;
        ctx.lineWidth = 1;
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
      this._raf = requestAnimationFrame(draw);
    };
    this._raf = requestAnimationFrame(draw);
  }
}

if (!customElements.get("kim-panel")) {
  customElements.define("kim-panel", KimPanel);
}
