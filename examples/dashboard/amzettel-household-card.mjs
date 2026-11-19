// MIT · COCOAR e.U. Optional Lovelace card; no separate application or backend.
import { COPY, text, escapeHTML as esc, shiftDay, midnight, threeDays, eventsForDay, timeLabel, dayLabel, mealPresentation } from "./amzettel-household-model.mjs?v=1";

const icon = name => `<ha-icon icon="mdi:${name}" aria-hidden="true"></ha-icon>`;
const bowl = `<svg class="bowl" viewBox="0 0 220 160" fill="none" aria-hidden="true">
  <ellipse cx="110" cy="142" rx="72" ry="8" fill="#123d3020"/>
  <path d="M30 75h160c-5 39-29 63-80 63S35 114 30 75Z" fill="#fffdf5"/>
  <ellipse cx="110" cy="75" rx="80" ry="22" fill="#dce8c5"/>
  <path d="M38 73c18-31 34-39 58-17 13-29 40-30 54-3 24-8 35 5 37 22" fill="#66a66b"/>
  <path d="M58 70c0-22 23-29 32-8M119 63c0-23 27-27 33-7" stroke="#235a3e" stroke-width="5" stroke-linecap="round"/>
  <path d="m89 49 24 9-7 24-24-9zM138 63l21 8-7 20-21-8z" fill="#fff9dd"/>
  <circle cx="65" cy="80" r="10" fill="#d98253"/><circle cx="117" cy="78" r="9" fill="#e6a369"/>
  <path d="m96 24 13 8m29-17-3 12M63 32l-6-8" stroke="#82a58b" stroke-width="3" stroke-linecap="round"/>
  <path d="M48 101c11 17 33 26 61 26" stroke="#e2e5d9" stroke-width="4" stroke-linecap="round"/>
</svg>`;

class AmzettelHouseholdCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this.data = { events: [], meals: [], items: [] };
    this.errors = {};
    this.ready = false;
    this.showDone = false;
    this.busy = false;
    this.generation = 0;
    this.shadowRoot.addEventListener("click", event => this.onClick(event));
    this.shadowRoot.addEventListener("submit", event => this.onSubmit(event));
  }

  setConfig(config) {
    for (const key of ["calendar", "meals", "shopping"]) {
      if (typeof config[key] !== "string" || !/^(calendar|todo)\.[a-z0-9_]+$/.test(config[key])) {
        throw new Error(`A valid entity ID is required for ${key}.`);
      }
    }
    if (!config.calendar.startsWith("calendar.") || !config.meals.startsWith("calendar.") || !config.shopping.startsWith("todo.")) {
      throw new Error("Use calendar entities for calendar/meals, and a todo entity for shopping.");
    }
    if (config.language !== undefined && !["en", "de"].includes(config.language))
      throw new Error("language must be en or de.");
    if (config.household !== undefined && typeof config.household !== "string")
      throw new Error("household must be text.");
    if (config.demo !== undefined && typeof config.demo !== "boolean")
      throw new Error("demo must be true or false.");
    this.config = { household: "", language: "en", demo: false, ...config };
    this.generation++;
    this.data = { events: [], meals: [], items: [] };
    this.errors = {};
    this.ready = false;
    this.signature = null;
    this.render();
    if (this._hass) this.load();
  }

  set hass(hass) {
    this._hass = hass;
    if (!this.config) return;
    const signature = [this.config.calendar, this.config.meals, this.config.shopping]
      .map(id => `${id}:${hass.states[id]?.last_updated}`).join("|");
    if (signature !== this.signature) {
      this.signature = signature;
      this.load();
    }
  }

  connectedCallback() {
    this.generation++;
    window.clearInterval(this.timer);
    this.timer = window.setInterval(() => this.load(), 60000);
    if (this._hass) this.load();
  }
  disconnectedCallback() { this.generation++; window.clearInterval(this.timer); }
  getCardSize() { return 12; }
  get zone() { return this._hass?.config?.time_zone || "UTC"; }
  get language() { return this.config?.language || "en"; }
  t(key, values) { return text(this.language, key, values); }

  async load() {
    if (!this._hass || !this.config || !this.isConnected) return;
    if (this.loading) { this.loadAgain = true; return this.loading; }
    this.loading = (async () => {
      do {
        this.loadAgain = false;
        const generation = this.generation;
        const config = this.config;
        const keys = threeDays(new Date(), this.zone);
        const range = { start_date_time: midnight(keys[0], this.zone).toISOString(),
          end_date_time: midnight(shiftDay(keys[2], 1), this.zone).toISOString() };
        const tasks = [
          ["events", "calendar", "get_events", config.calendar, range, "events"],
          ["meals", "calendar", "get_events", config.meals, range, "events"],
          ["items", "todo", "get_items", config.shopping, {}, "items"],
        ];
        const results = await Promise.allSettled(tasks.map(async ([, domain, service, entity, serviceData, field]) => {
          const result = await this._hass.callWS({
            type: "call_service", domain, service, target: { entity_id: entity },
            service_data: serviceData, return_response: true,
          });
          const data = result.response?.[entity]?.[field];
          if (!Array.isArray(data)) throw new Error("Expected an array of entity data.");
          return data;
        }));
        if (generation !== this.generation || !this.isConnected) continue;
        results.forEach((result, index) => {
          const key = tasks[index][0];
          if (result.status === "fulfilled") { this.data[key] = result.value; delete this.errors[key]; }
          else this.errors[key] = true;
        });
        this.ready = true;
        if (!this.busy) this.render();
      } while (this.loadAgain && this.isConnected);
    })();
    try { await this.loading; }
    finally { this.loading = null; }
  }

  async onClick(event) {
    const button = event.target.closest("button");
    if (!button || this.busy) return;
    if (button.dataset.action === "done") {
      this.showDone = !this.showDone;
      this.render();
      this.shadowRoot.querySelector('[data-action="done"]')?.focus();
      return;
    }
    if (button.dataset.action === "refresh") { await this.load(); return; }
    if (button.dataset.action !== "check") return;
    const item = this.data.items.find(item => item.uid === button.dataset.uid);
    if (!item) return;
    this.busy = true;
    button.disabled = true;
    try {
      await this._hass.callService("todo", "update_item", {
        entity_id: this.config.shopping, item: item.uid,
        status: item.status === "completed" ? "needs_action" : "completed",
      });
      delete this.errors.write;
    } catch { this.errors.write = true; }
    finally {
      this.busy = false;
      await this.load();
      this.render();
      this.shadowRoot.querySelector('[data-action="done"]')?.focus();
    }
  }

  async onSubmit(event) {
    event.preventDefault();
    if (this.busy || this.errors.items) return;
    const input = this.shadowRoot.querySelector("input");
    const value = input.value.trim();
    if (!value) return;
    this.busy = true;
    const submit = this.shadowRoot.querySelector('[type="submit"]');
    submit.disabled = true;
    input.disabled = true;
    let failed = false;
    try {
      await this._hass.callService("todo", "add_item", {
        entity_id: this.config.shopping, item: value,
      });
      delete this.errors.write;
      input.value = "";
    } catch { this.errors.write = true; failed = true; }
    finally {
      this.busy = false;
      await this.load();
      this.render();
      const next = this.shadowRoot.querySelector("input");
      if (next) {
        if (failed) next.value = value;
        next.focus();
      }
    }
  }

  warning(key) {
    return this.errors[key] ? `<p class="error" role="alert">${icon("cloud-alert-outline")} ${esc(this.t("readError"))} <button data-action="refresh">${esc(this.t("retry"))}</button></p>` : "";
  }

  render() {
    if (!this.config) return;
    const previousInput = this.shadowRoot.querySelector("input");
    const draft = previousInput?.value || "";
    const inputFocused = previousInput && this.shadowRoot.activeElement === previousInput;
    const selection = inputFocused ? [previousInput.selectionStart, previousInput.selectionEnd] : null;
    const now = new Date(), keys = threeDays(now, this.zone);
    const open = this.data.items.filter(item => item.status !== "completed");
    const done = this.data.items.filter(item => item.status === "completed");
    const todayEvents = eventsForDay(this.data.events, keys[0], this.zone);
    const todayMeal = eventsForDay(this.data.meals, keys[0], this.zone)[0];
    const meal = mealPresentation(todayMeal, this.zone, this.language);
    const household = this.config.household || this.t("household");
    const dates = new Intl.DateTimeFormat(COPY[this.language].locale, {
      weekday: "long", day: "numeric", month: "long", timeZone: this.zone,
    }).format(now);
    const dayCards = keys.map((key, index) => {
      const events = eventsForDay(this.data.events, key, this.zone);
      const stamp = new Date(`${key}T12:00:00Z`);
      const weekday = new Intl.DateTimeFormat(COPY[this.language].locale, { weekday: "short", timeZone: "UTC" }).format(stamp);
      return `<article class="day ${index === 0 ? "today" : ""}">
        <div class="day-heading"><div><span class="eyebrow">${esc(weekday)}</span><h3>${esc(dayLabel(key, index, this.language))}</h3></div>
          <span class="date-number">${Number(key.slice(-2))}<i></i></span></div>
        <div class="timeline">${events.length ? events.map((item, i) => {
          const tone = ["mint", "lilac", "sand"][i % 3];
          return `<div class="event ${tone}"><span class="event-dot"></span><div>
            <time>${esc(timeLabel(item, this.zone, this.language))}</time><h4>${esc(item.summary)}</h4>
            <p>${esc(item.description || item.location || this.t("eventFallback"))}</p></div></div>`;
        }).join("") : `<p class="empty">${esc(this.t(this.ready ? "openDay" : "loadingEvents"))}</p>`}</div>
        <div class="day-footer">${events.length} ${esc(this.t(events.length === 1 ? "event" : "events"))}<span>${esc(this.t(index === 0 ? "todayFooter" : index === 1 ? "tomorrowFooter" : "futureFooter"))}</span></div>
      </article>`;
    }).join("");
    const itemRows = (items, completed = false) => items.map(item => `<div class="shopping-row ${completed ? "completed" : ""}">
      <button class="check" data-action="check" data-uid="${esc(item.uid)}"
        aria-label="${esc(item.summary)}: ${esc(this.t(completed ? "reopen" : "check"))}" ${this.errors.items ? "disabled" : ""}>
        ${completed ? icon("check") : ""}</button>
      <div><span class="item-name">${esc(item.summary)}</span>${item.description ? `<span class="item-detail">${esc(item.description)}</span>` : ""}</div>
    </div>`).join("");
    const meals = keys.slice(1).map((key, index) => {
      const nextMeal = eventsForDay(this.data.meals, key, this.zone)[0];
      return `<div class="next-meal"><span>${esc(dayLabel(key, index + 1, this.language))}</span><strong>${esc(nextMeal ? mealPresentation(nextMeal, this.zone, this.language).title : this.t("mealOpen"))}</strong>${icon("silverware-fork-knife")}</div>`;
    }).join("");
    this.shadowRoot.innerHTML = `<style>${styles}</style>
    <div class="app" role="region" aria-label="${esc(household)}" lang="${this.language}">
      <header class="brandbar"><div class="brand"><img src="/local/amzettel-household/icon.png" alt="" width="38" height="38"><strong>amZettel<span>${esc(this.t("home"))}</span></strong></div>
        <div class="household">${icon("home-heart")}<span>${esc(household)}</span>${this.config.demo ? `<span class="demo-tag">${esc(this.t("demo"))}</span>` : ""}</div>
      </header>
      <section class="hero">
        <div><p class="eyebrow date-line"><span></span>${esc(dates)}</p>
          <h1>${esc(this.t("heroFirst"))}<br><span>${esc(this.t("heroSecond"))}</span></h1>
          <p class="intro">${esc(this.t("introFirst"))}<br>${esc(this.t("introSecond"))}</p></div>
        <aside class="glance"><div class="glance-heading">${icon("white-balance-sunny")}<span>${esc(this.t("glance"))}</span><span class="mini-dot"></span></div>
          <div class="stats"><div><strong>${!this.ready || this.errors.events ? "–" : todayEvents.length}</strong><span>${esc(this.t("appointments"))}</span></div>
          <div><strong>${!this.ready || this.errors.items ? "–" : open.length}</strong><span>${esc(this.t("shoppingStat"))}</span></div>
          <div><strong class="dinner-time ${todayMeal && !meal.time ? "planned" : ""}">${this.errors.meals ? "–" : todayMeal ? esc(meal.time || this.t("planned")) : "–"}</strong><span>${esc(this.t("mealStat"))}</span></div></div>
          <p>${icon("heart-outline")} ${esc(this.t("together"))}</p>
        </aside>
      </section>
      <main class="dashboard">
        <section class="agenda-section"><div class="section-heading"><div><span class="eyebrow">${esc(this.t("agendaEyebrow"))}</span><h2>${esc(this.t("agenda"))}<span>.</span></h2></div><span class="section-icon">${icon("calendar-outline")}</span></div>
          ${this.warning("events")}<div class="days">${dayCards}</div></section>
        <section class="shopping panel"><div class="shopping-heading"><span class="tile-icon">${icon("basket-outline")}</span><div><span class="eyebrow">${esc(this.t("shoppingEyebrow"))}</span><h2>${esc(this.t("shopping"))}<span>.</span></h2></div><span class="count">${!this.ready || this.errors.items ? "–" : open.length}</span></div>
          <p class="shopping-subtitle">${esc(this.t("shoppingSubtitle"))}</p>${this.warning("items")}
          <div class="shopping-items">${itemRows(open)}${this.ready && !open.length && !this.errors.items ? `<p class="empty">${esc(this.t("allDone"))}</p>` : ""}</div>
          <form><label class="sr-only" for="new-item">${esc(this.t("addLabel"))}</label><input id="new-item" placeholder="${esc(this.t("addPlaceholder"))}" maxlength="150" autocomplete="off" ${this.errors.items ? "disabled" : ""}><button type="submit" aria-label="${esc(this.t("add"))}" ${this.errors.items ? "disabled" : ""}>${icon("plus")}</button></form>
          ${this.errors.write ? `<p class="error" role="alert">${esc(this.t("writeError"))}</p>` : ""}
          <div class="shopping-progress"><span><i style="width:${this.data.items.length ? done.length / this.data.items.length * 100 : 0}%"></i></span><small>${esc(this.t("progress", { done: done.length, total: this.data.items.length }))}</small></div>
          <button class="show-done" data-action="done" aria-expanded="${this.showDone}">${icon(this.showDone ? "chevron-up" : "check-all")}${esc(this.showDone ? this.t("hideDone") : this.t("showDone", { count: done.length }))}</button>
          ${this.showDone ? `<div class="done-items">${itemRows(done, true)}</div>` : ""}
        </section>
        <section class="meals-section"><div class="section-heading"><div><span class="eyebrow">${esc(this.t("mealEyebrow"))}</span><h2>${esc(this.t("meal"))}<span>?</span></h2></div>${icon("silverware-fork-knife")}</div>
          ${this.warning("meals")}<div class="meal-feature"><div class="meal-copy"><span class="meal-label">${esc(this.t("todayMeal"))}${meal.time ? ` · ${esc(meal.time)}` : ""}</span><h3>${esc(todayMeal ? meal.title : this.t(this.ready ? "mealEmpty" : "mealLoading"))}</h3><p>${esc(todayMeal?.description || this.t("mealTogether"))}</p><span class="meal-note">${icon("calendar-check-outline")} ${esc(this.t(todayMeal ? "mealNote" : "mealIdea"))}</span></div>${bowl}</div>
          <div class="next-meals">${meals}</div>
        </section>
      </main>
      <footer><span>${icon("home-assistant")} ${esc(this.t("footer"))}</span><span>${this.config.demo ? esc(this.t("footerDemo")) : ""}${esc(this.t("tagline"))}</span></footer>
    </div>`;
    const input = this.shadowRoot.querySelector("input");
    if (input) {
      input.value = draft;
      if (inputFocused && !input.disabled) {
        input.focus({ preventScroll: true });
        if (selection) input.setSelectionRange(...selection);
      }
    }
  }
}

const styles = `
  :host {
    display:block;
    color:#243d35;
    font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
    font-size:15px;
    --green:#238361;
    --muted:#748178;
    --line:#e7ebe4
  }
  * {
    box-sizing:border-box
  }
  button,input {
    font:inherit
  }
  button {
    cursor:pointer
  }
  button:disabled {
    cursor:wait;
    opacity:.5
  }
  button:focus-visible,input:focus-visible {
    outline:3px solid #399c77;
    outline-offset:4px
  }
  ha-icon {
    --mdc-icon-size:21px;
    display:inline-flex;
    vertical-align:middle;
    flex-shrink:0
  }
  .app {
    max-width:1550px;
    margin:0 auto;
    background:#f7f8f3;
    padding:26px 44px 18px;
    border-radius:0
  }
  .brandbar {
    display:flex;
    justify-content:space-between;
    align-items:center;
    padding-bottom:24px;
    border-bottom:1px solid var(--line)
  }
  .brand {
    display:flex;
    align-items:center;
    gap:11px
  }
  .brand img {
    border-radius:11px
  }
  .brand strong {
    font-size:23px;
    font-weight:740;
    letter-spacing:-.8px;
    display:flex;
    align-items:center;
    gap:14px
  }
  .brand strong span {
    font-size:10px;
    letter-spacing:2.3px;
    font-weight:600;
    color:#8a948a;
    border-left:1px solid #d8dfd5;
    padding-left:14px
  }
  .household {
    display:flex;
    align-items:center;
    gap:9px;
    font-size:13px;
    color:#5b6e62
  }
  .household ha-icon {
    color:var(--green)
  }
  .demo-tag {
    font-size:8px;
    letter-spacing:1.4px;
    background:#e8eee3;
    padding:5px 7px;
    border-radius:5px;
    color:#69786a
  }
  .hero {
    display:grid;
    grid-template-columns:1.3fr 1fr;
    gap:60px;
    align-items:center;
    padding:32px 0 34px
  }
  .eyebrow {
    font-size:9px;
    letter-spacing:1.9px;
    font-weight:750;
    color:#7b867b;
    margin:0
  }
  .date-line {
    display:flex;
    gap:8px;
    align-items:center;
    text-transform:uppercase
  }
  .date-line>span {
    width:6px;
    height:6px;
    border-radius:50%;
    background:#43936f
  }
  h1 {
    font-size:clamp(34px,3.4vw,51px);
    line-height:1.09;
    letter-spacing:-2px;
    font-weight:720;
    margin:15px 0 14px
  }
  h1>span {
    color:#318866
  }
  .intro {
    font-size:14px;
    line-height:1.6;
    color:#7b857c;
    margin:0
  }
  .glance {
    position:relative;
    overflow:hidden;
    background:#224d3d;
    color:#f6f8ed;
    border-radius:24px;
    padding:24px 27px;
    box-shadow:0 12px 28px #244b3b10
  }
  .glance:after {
    content:"";
    position:absolute;
    right:-43px;
    top:-65px;
    width:190px;
    height:190px;
    border:1px solid #ffffff0b;
    border-radius:50%;
    box-shadow:0 0 0 23px #ffffff04,0 0 0 46px #ffffff03;
    pointer-events:none
  }
  .glance-heading {
    display:flex;
    align-items:center;
    gap:9px;
    font-size:12px;
    font-weight:500;
    color:#d4e4d4
  }
  .glance-heading ha-icon {
    color:#bed397;
    --mdc-icon-size:19px
  }
  .mini-dot {
    margin-left:auto;
    width:6px;
    height:6px;
    background:#9ac998;
    border-radius:50%
  }
  .stats {
    display:grid;
    grid-template-columns:1fr 1fr 1.15fr;
    margin:24px 0 21px
  }
  .stats>div {
    display:flex;
    flex-direction:column;
    gap:8px;
    border-right:1px solid #ffffff20;
    padding-left:20px
  }
  .stats>div:first-child {
    padding-left:0
  }
  .stats>div:last-child {
    border:0
  }
  .stats strong {
    font-size:39px;
    line-height:1;
    letter-spacing:-1.6px;
    font-weight:520
  }
  .stats .dinner-time {
    font-size:31px;
    line-height:39px;
    letter-spacing:-1px
  }
  .stats span {
    font-size:10px;
    color:#bdd0bd
  }
  .glance>p {
    border-top:1px solid #ffffff1c;
    padding-top:15px;
    margin:0;
    font-size:10px;
    color:#aec5b0;
    display:flex;
    align-items:center;
    gap:7px
  }
  .glance>p ha-icon {
    --mdc-icon-size:14px
  }
  .dashboard {
    display:grid;
    grid-template-columns:minmax(0,1.7fr) minmax(290px,1fr);
    gap:24px 26px;
    align-items:start
  }
  .section-heading {
    display:flex;
    justify-content:space-between;
    align-items:center;
    margin:1px 0 18px
  }
  .section-heading h2 {
    font-size:22px;
    font-weight:650;
    letter-spacing:-.7px;
    margin:6px 0 0
  }
  h2>span {
    color:#3b926c
  }
  .section-heading>ha-icon,.section-icon {
    color:#93a18f
  }
  .days {
    display:grid;
    grid-template-columns:repeat(3,minmax(0,1fr));
    gap:12px
  }
  .day {
    border-radius:18px;
    background:#fffefa;
    border:1px solid #e8ece3;
    overflow:hidden;
    min-height:266px;
    display:flex;
    flex-direction:column
  }
  .day.today {
    border-color:#a7c8ad;
    box-shadow:0 4px 13px #2d785008
  }
  .day-heading {
    display:flex;
    justify-content:space-between;
    padding:20px 18px 16px;
    align-items:center
  }
  .day-heading .eyebrow {
    font-size:9px;
    letter-spacing:1.5px;
    text-transform:uppercase
  }
  .day h3 {
    font-size:17px;
    letter-spacing:-.4px;
    margin:5px 0 0;
    font-weight:620
  }
  .date-number {
    font-size:30px;
    letter-spacing:-1.7px;
    position:relative;
    color:#b3bcae;
    font-weight:500
  }
  .today .date-number {
    color:#2b825a
  }
  .today .date-number i {
    display:block;
    position:absolute;
    right:10px;
    bottom:-8px;
    width:4px;
    height:4px;
    border-radius:50%;
    background:#2b825a
  }
  .timeline {
    padding:4px 15px 15px;
    flex:1
  }
  .event {
    display:flex;
    position:relative;
    gap:10px;
    padding:0 0 20px
  }
  .event:last-child {
    padding-bottom:3px
  }
  .event:before {
    content:"";
    position:absolute;
    left:3px;
    top:9px;
    bottom:3px;
    border-left:1px solid #e6e9e0
  }
  .event:last-child:before {
    display:none
  }
  .event-dot {
    margin-top:5px;
    position:relative;
    z-index:1;
    width:7px;
    height:7px;
    border-radius:50%;
    background:#c3a46b;
    flex-shrink:0
  }
  .event.mint .event-dot {
    background:#57a68d
  }
  .event.lilac .event-dot {
    background:#a38ab8
  }
  .event time {
    font-size:10px;
    color:#84907f;
    letter-spacing:.4px
  }
  .event h4 {
    font-size:12px;
    line-height:1.35;
    font-weight:620;
    margin:5px 0 4px;
    overflow-wrap:anywhere
  }
  .event p {
    font-size:10px;
    line-height:1.4;
    color:#92998d;
    margin:0;
    overflow-wrap:anywhere
  }
  .day-footer {
    background:#f4f6ee;
    border-top:1px solid #edf0e8;
    padding:11px 15px;
    font-size:9px;
    color:#9bA28f;
    display:flex;
    justify-content:space-between;
    gap:5px
  }
  .day-footer>span {
    font-size:8px
  }
  .today .day-footer {
    background:#edf4e9;
    color:#61906b
  }
  .shopping {
    grid-column:2;
    grid-row:1 / span 2
  }
  .panel {
    border-radius:23px;
    background:#fffefa;
    border:1px solid #e5eadf;
    padding:23px 24px;
    box-shadow:0 5px 18px #42553404
  }
  .shopping-heading {
    display:flex;
    align-items:center;
    gap:12px
  }
  .tile-icon {
    display:flex;
    align-items:center;
    justify-content:center;
    width:38px;
    height:38px;
    background:#edf3e5;
    border-radius:12px;
    color:#6c9561
  }
  .shopping-heading .eyebrow {
    font-size:8px;
    letter-spacing:1.4px
  }
  .shopping h2 {
    font-size:21px;
    margin:6px 0 0;
    letter-spacing:-.7px;
    font-weight:650
  }
  .count {
    margin-left:auto;
    font-size:12px;
    display:flex;
    align-items:center;
    justify-content:center;
    min-width:27px;
    height:27px;
    border-radius:50%;
    background:#edf3e5;
    color:#6b8a59
  }
  .shopping-subtitle {
    font-size:11px;
    color:#8e9888;
    margin:17px 0 13px
  }
  .shopping-items {
    border-top:1px solid var(--line)
  }
  .shopping-row {
    display:flex;
    gap:12px;
    align-items:center;
    padding:13px 0;
    border-bottom:1px solid #f0f1eb;
    min-height:56px
  }
  .check {
    display:flex;
    align-items:center;
    justify-content:center;
    width:23px;
    height:23px;
    flex-shrink:0;
    border:1.5px solid #d0daca;
    background:#fffefa;
    border-radius:8px;
    padding:0;
    color:#fff
  }
  .check:hover {
    border-color:#4f9d73;
    background:#edf5e9
  }
  .shopping-row>div {
    display:flex;
    flex-direction:column;
    gap:3px;
    min-width:0
  }
  .item-name {
    font-size:13px;
    font-weight:550;
    overflow-wrap:anywhere
  }
  .item-detail {
    font-size:10px;
    color:#949b8e
  }
  .completed .item-name {
    text-decoration:line-through;
    color:#9ca392
  }
  .completed .check {
    background:#5b9a73;
    border-color:#5b9a73
  }
  .completed .check ha-icon {
    --mdc-icon-size:15px
  }
  form {
    display:flex;
    align-items:center;
    gap:8px;
    margin:16px 0;
    background:#f4f6ee;
    border:1px solid #e9edde;
    border-radius:11px;
    padding:5px 6px 5px 13px
  }
  input {
    min-width:0;
    width:100%;
    border:0;
    background:transparent;
    color:#3c5b46;
    outline:none;
    font-size:11px;
    padding:8px 0
  }
  input::placeholder {
    color:#9aA18f
  }
  form button {
    display:flex;
    align-items:center;
    justify-content:center;
    width:31px;
    height:31px;
    flex-shrink:0;
    border:0;
    border-radius:8px;
    background:#2f805b;
    color:white
  }
  form button ha-icon {
    --mdc-icon-size:18px
  }
  .shopping-progress {
    display:flex;
    align-items:center;
    gap:12px;
    margin:18px 0 12px
  }
  .shopping-progress>span {
    height:4px;
    flex:1;
    background:#eef1e7;
    border-radius:5px;
    overflow:hidden
  }
  .shopping-progress i {
    display:block;
    height:100%;
    border-radius:5px;
    background:#94b38b
  }
  .shopping-progress small {
    font-size:9px;
    color:#929b87
  }
  .show-done {
    width:100%;
    border:0;
    background:none;
    color:#7a8f72;
    font-size:10px;
    display:flex;
    align-items:center;
    justify-content:center;
    gap:6px;
    padding:9px
  }
  .show-done ha-icon {
    --mdc-icon-size:16px
  }
  .meals-section {
    min-width:0
  }
  .meals-section .section-heading {
    margin-bottom:14px
  }
  .meal-feature {
    position:relative;
    overflow:hidden;
    min-height:155px;
    background:#e9efdc;
    border-radius:19px;
    padding:24px 25px;
    display:flex;
    align-items:center;
    justify-content:space-between;
    gap:12px
  }
  .meal-copy {
    position:relative;
    z-index:1;
    max-width:68%
  }
  .meal-label {
    font-size:9px;
    letter-spacing:1.1px;
    font-weight:700;
    color:#6e8759
  }
  .meal-feature h3 {
    font-size:25px;
    letter-spacing:-.8px;
    line-height:1.17;
    max-width:310px;
    font-weight:650;
    margin:10px 0
  }
  .meal-feature p {
    font-size:11px;
    color:#819174;
    margin:0
  }
  .meal-note {
    display:flex;
    align-items:center;
    gap:5px;
    font-size:9px;
    color:#7b906c;
    margin-top:20px
  }
  .meal-note ha-icon {
    --mdc-icon-size:13px
  }
  .bowl {
    width:180px;
    max-width:37%;
    height:auto;
    flex-shrink:0
  }
  .next-meals {
    display:grid;
    grid-template-columns:1fr 1fr;
    gap:12px;
    margin-top:12px
  }
  .next-meal {
    display:grid;
    grid-template-columns:1fr auto;
    gap:5px;
    background:#fffefa;
    border:1px solid #e7ebdf;
    padding:13px 16px;
    border-radius:13px
  }
  .next-meal>span {
    font-size:9px;
    color:#9bA28f
  }
  .next-meal>strong {
    font-size:11px;
    font-weight:550;
    grid-column:1;
    line-height:1.4
  }
  .next-meal>ha-icon {
    grid-row:1 / span 2;
    grid-column:2;
    align-self:center;
    color:#adba98;
    --mdc-icon-size:23px
  }
  footer {
    display:flex;
    justify-content:space-between;
    gap:15px;
    border-top:1px solid #e8ecdf;
    padding-top:17px;
    margin-top:27px;
    font-size:9px;
    color:#9da58f
  }
  footer>span:first-child {
    display:flex;
    align-items:center;
    gap:6px
  }
  footer ha-icon {
    --mdc-icon-size:15px;
    color:#8ca37f
  }
  .error {
    font-size:11px;
    color:#925a38;
    line-height:1.5
  }
  .error button {
    border:0;
    background:transparent;
    color:inherit;
    text-decoration:underline;
    font-size:inherit
  }
  .empty {
    font-size:12px;
    line-height:1.6;
    color:#97a18e;
    padding:8px 2px
  }
  .sr-only {
    position:absolute;
    width:1px;
    height:1px;
    overflow:hidden;
    clip:rect(0,0,0,0);
    white-space:nowrap
  }
  @media(min-width:1600px) {
    .app {
      padding-left:55px;
      padding-right:55px
    }
    .event h4 {
      font-size:14px
    }
    .event p {
      font-size:11px
    }
    .day {
      min-height:280px
    }
  }
  @media(max-width:1100px) {
    .app {
      padding:24px
    }
    .hero {
      gap:28px
    }
    .dashboard {
      gap:22px 18px;
      grid-template-columns:minmax(0,1.6fr) minmax(280px,1fr)
    }
    .day-heading {
      padding:16px 12px
    }
    .timeline {
      padding-left:11px;
      padding-right:10px
    }
    .day-footer>span {
      display:none
    }
    .shopping {
      padding:21px 18px
    }
    .shopping h2 {
      font-size:19px
    }
    .day h3 {
      font-size:15px
    }
    .meal-feature {
      padding:22px
    }
    .bowl {
      max-width:33%
    }
    .meal-feature h3 {
      font-size:23px
    }
  }
  @media(max-width:850px) {
    .hero {
      grid-template-columns:1fr 1fr;
      gap:22px
    }
    .glance {
      padding:21px
    }
    .stats>div {
      padding-left:12px
    }
    .stats strong {
      font-size:32px
    }
    .stats .dinner-time {
      font-size:25px;
      line-height:32px
    }
    .stats span {
      font-size:9px
    }
    .dashboard {
      grid-template-columns:1fr
    }
    .shopping {
      grid-column:1;
      grid-row:3
    }
    .meals-section {
      grid-row:2
    }
    .day-footer>span {
      display:block
    }
    .shopping-row {
      padding:12px 0
    }
    .shopping-items {
      display:grid;
      grid-template-columns:1fr 1fr;
      gap:0 22px
    }
    .shopping h2 {
      font-size:22px
    }
    .shopping-heading .eyebrow {
      font-size:9px
    }
    .meal-copy {
      max-width:65%
    }
    .meal-feature {
      min-height:175px
    }
    .bowl {
      width:220px;
      max-width:30%
    }
  }
  @media(max-width:560px) {
    .app {
      padding:20px 18px
    }
    .brandbar {
      padding-bottom:19px
    }
    .brand strong {
      font-size:22px
    }
    .brand strong span {
      display:none
    }
    .household {
      font-size:11px;
      gap:6px
    }
    .household>ha-icon {
      display:none
    }
    .hero {
      grid-template-columns:1fr;
      padding:27px 0;
      gap:24px
    }
    h1 {
      font-size:43px;
      margin-top:14px
    }
    .intro {
      font-size:13px
    }
    .glance {
      padding:20px 23px;
      border-radius:20px
    }
    .stats {
      margin:20px 0
    }
    .stats strong {
      font-size:35px
    }
    .stats .dinner-time {
      font-size:29px;
      line-height:35px
    }
    .stats>div {
      padding-left:24px
    }
    .stats span {
      font-size:10px
    }
    .section-heading h2 {
      font-size:22px
    }
    .days {
      grid-template-columns:1fr;
      gap:11px
    }
    .day {
      min-height:0
    }
    .day-heading {
      padding:17px 18px 13px
    }
    .day-heading>div {
      display:flex;
      align-items:center;
      gap:12px
    }
    .day h3 {
      font-size:17px;
      margin:0
    }
    .date-number {
      font-size:25px
    }
    .timeline {
      padding:0 18px 16px
    }
    .event {
      padding-bottom:16px;
      gap:13px
    }
    .event h4 {
      font-size:14px;
      margin:3px 0
    }
    .event p {
      font-size:11px
    }
    .event time {
      font-size:11px
    }
    .event>div {
      display:grid;
      grid-template-columns:62px 1fr;
      gap:2px 8px;
      width:100%
    }
    .event time {
      grid-row:1 / span 2;
      align-self:center
    }
    .event p {
      grid-column:2
    }
    .event h4 {
      grid-column:2
    }
    .event-dot {
      margin-top:8px
    }
    .day-footer {
      padding:9px 18px
    }
    .day-footer>span {
      display:block
    }
    .shopping-items {
      display:block
    }
    .meal-feature {
      min-height:185px;
      padding:21px
    }
    .meal-feature h3 {
      font-size:25px
    }
    .meal-copy {
      max-width:72%
    }
    .bowl {
      width:125px;
      max-width:33%;
      margin-right:-8px
    }
    .meal-note {
      font-size:8px
    }
    .next-meals {
      gap:10px
    }
    .next-meal {
      padding:12px
    }
    .next-meal>ha-icon {
      display:none
    }
    .next-meal>strong {
      font-size:11px
    }
    .shopping {
      padding:22px
    }
    .shopping-row {
      min-height:60px
    }
    .item-name {
      font-size:14px
    }
    .item-detail {
      font-size:11px
    }
    .check {
      width:27px;
      height:27px
    }
    .eyebrow {
      font-size:8px
    }
    .shopping-heading .eyebrow {
      font-size:8px
    }
    footer {
      font-size:8px;
      align-items:flex-start
    }
    footer>span:last-child {
      max-width:48%;
      text-align:right;
      line-height:1.7
    }
  }
  @media(prefers-reduced-motion:no-preference) {
    button {
      transition:background .15s,border-color .15s
    }
  }
  .stats .dinner-time.planned {
    font-size:24px;
    letter-spacing:-.5px
  }
  .shopping-items {
    max-height:388px;
    overflow-y:auto;
    scrollbar-width:thin;
    scrollbar-color:#c8d7bf transparent
  }
  @media(max-width:560px) {
    .shopping-items {
      max-height:none
    }
    .stats .dinner-time.planned {
      font-size:25px
    }
  }
`;

if (!customElements.get("amzettel-household-card")) customElements.define("amzettel-household-card", AmzettelHouseholdCard);
