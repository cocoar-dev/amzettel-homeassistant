import test from "node:test";
import assert from "node:assert/strict";
import { dateKey, shiftDay } from "../amzettel-household-model.mjs";

const elements = new Map();
globalThis.HTMLElement = class {};
globalThis.window = { clearInterval() {}, setInterval() { return 1; } };
globalThis.customElements = {
  get: name => elements.get(name),
  define: (name, type) => elements.set(name, type),
};
await import("../amzettel-household-card.mjs");
const Card = elements.get("amzettel-household-card");

function card() {
  const instance = Object.create(Card.prototype);
  instance.config = { calendar: "calendar.family", meals: "calendar.meals", shopping: "todo.shopping", demo: true };
  instance.data = { events: [], meals: [], items: [] };
  instance.errors = {};
  instance.generation = 0;
  instance.isConnected = true;
  instance._hass = { config: { time_zone: "Europe/Vienna" } };
  return instance;
}

test("an overlapping write refresh waits for the queued fresh data", async () => {
  const instance = card();
  let release;
  const oldRead = new Promise(resolve => { release = resolve; });
  let reads = 0;
  instance._hass.callWS = async request => {
    const entity = request.target.entity_id;
    if (request.domain === "calendar") return { response: { [entity]: { events: [] } } };
    reads++;
    if (reads === 1) await oldRead;
    return { response: { [entity]: { items: reads === 1 ? [] : [{ uid: "new", summary: "Test" }] } } };
  };
  let rendered = [];
  instance.render = () => { rendered = [...instance.data.items]; };
  const background = instance.load();
  const afterWrite = instance.load();
  release();
  await Promise.all([background, afterWrite]);
  assert.equal(reads, 2);
  assert.equal(instance.data.items[0].uid, "new");
  assert.equal(rendered[0].uid, "new");
  assert.equal(instance.loading, null);
});

test("a failed section is explicit without losing the other sections", async () => {
  const instance = card();
  instance.render = () => {};
  instance._hass.callWS = async request => {
    const entity = request.target.entity_id;
    if (entity === "calendar.family") throw new Error("offline");
    return { response: { [entity]: request.domain === "calendar" ? { events: [] } : { items: [] } } };
  };
  await instance.load();
  assert.equal(instance.errors.events, true);
  assert.equal(instance.errors.items, undefined);
  assert.equal(instance.errors.meals, undefined);
  assert.equal(instance.ready, true);
});

test("refreshes update the view without erasing a focused draft or caret", () => {
  const instance = card();
  const previous = { value: "Hafer", selectionStart: 2, selectionEnd: 4 };
  const replacement = {
    value: "", disabled: false,
    focus() { this.focused = true; },
    setSelectionRange(a, b) { this.selection = [a, b]; },
  };
  let input = previous;
  instance.shadowRoot = {
    activeElement: previous,
    querySelector: () => input,
    set innerHTML(value) { this.html = value; input = replacement; },
  };
  instance.render();
  assert.equal(replacement.value, "Hafer");
  assert.equal(replacement.focused, true);
  assert.deepEqual(replacement.selection, [2, 4]);
  assert.match(instance.shadowRoot.html, /Fictional example data/);
});

test("configuration requires appropriate entity types and supported UI languages", () => {
  const instance = card();
  instance._hass = null;
  instance.render = () => {};
  const valid = { calendar: "calendar.family", meals: "calendar.meals", shopping: "todo.shopping" };
  instance.setConfig(valid);
  assert.equal(instance.config.language, "en");
  assert.equal(instance.config.demo, false);
  assert.equal(instance.config.household, "");
  for (const invalid of [
    { shopping: "calendar.family" }, { calendar: "todo.shopping" },
    { meals: "calendar.BAD" }, { language: "xx" }, { demo: "false" }, { household: {} },
  ]) {
    assert.throws(() => instance.setConfig({ ...valid, ...invalid }));
  }
});

test("configuration changes discard an in-flight response from the old entities", async () => {
  const instance = card();
  let release;
  const pending = new Promise(resolve => { release = resolve; });
  const rendered = [];
  instance.render = () => rendered.push(instance.data.items.map(row => row.uid));
  instance._hass.callWS = async request => {
    const entity = request.target.entity_id;
    if (request.domain === "calendar") return { response: { [entity]: { events: [] } } };
    if (entity === "todo.shopping") await pending;
    return { response: { [entity]: { items: [{ uid: entity, summary: entity }] } } };
  };
  const loading = instance.load();
  instance.setConfig({ calendar: "calendar.other", meals: "calendar.other_meals", shopping: "todo.other" });
  release();
  await loading;
  assert.equal(instance.data.items[0].uid, "todo.other");
  assert.ok(!rendered.some(ids => ids.includes("todo.shopping")));
});

test("removing the card does not apply or render an old response", async () => {
  const instance = card();
  let release;
  const pending = new Promise(resolve => { release = resolve; });
  instance.render = () => assert.fail("Disconnected card must not render");
  instance._hass.callWS = async request => {
    await pending;
    const entity = request.target.entity_id;
    return { response: { [entity]: request.domain === "calendar" ? { events: [] } : { items: [] } } };
  };
  const loading = instance.load();
  instance.isConnected = false;
  instance.disconnectedCallback();
  release();
  await loading;
  assert.equal(instance.ready, undefined);
});

test("English/German UI and untrusted HA text remain separate", () => {
  const instance = card();
  instance.ready = true;
  const today = dateKey(new Date(), instance.zone);
  instance.data.events = [{
    summary: 'Zahnarzt <img src=x onerror="alert(1)">', description: "Karte mitnehmen",
    start: today, end: shiftDay(today, 1),
  }];
  instance.data.meals = [{ summary: "Ofengemüse mit Feta", start: today, end: shiftDay(today, 1) }];
  instance.data.items = [{ uid: '" onclick="alert(2)', summary: "Äpfel", status: "needs_action" }];
  for (const language of ["en", "de"]) {
    instance.config.language = language;
    instance.config.household = "<script>private</script>";
    instance.shadowRoot = { querySelector: () => null, innerHTML: "" };
    instance.render();
    const html = instance.shadowRoot.innerHTML;
    assert.match(html, language === "en" ? /The next three days/ : /Die nächsten drei Tage/);
    assert.match(html, /Ofengemüse mit Feta/);
    assert.match(html, /Karte mitnehmen/);
    assert.match(html, /Äpfel/);
    assert.match(html, /&lt;img/);
    assert.match(html, /&quot; onclick=&quot;/);
    assert.ok(!html.includes('<script>private</script>'));
    assert.ok(!html.includes('src=x onerror="'));
  }
});
