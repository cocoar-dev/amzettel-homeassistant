import test from "node:test";
import assert from "node:assert/strict";
import { COPY, text, escapeHTML, dateKey, shiftDay, midnight, threeDays, eventsForDay, timeLabel, dayLabel, mealPresentation } from "../amzettel-household-model.mjs";

test("untrusted HA strings are HTML escaped, including attribute delimiters", () => {
  assert.equal(escapeHTML('<img src=x onerror="alert(1)"> & \''), "&lt;img src=x onerror=&quot;alert(1)&quot;&gt; &amp; &#39;");
  assert.equal(escapeHTML(null), "");
});
test("three days use the installation time zone, not UTC or browser time", () => {
  assert.deepEqual(threeDays("2026-11-19T23:30:00Z", "Europe/Vienna"), ["2026-11-20", "2026-11-21", "2026-11-22"]);
  assert.equal(dateKey("2026-11-19T01:00:00Z", "America/New_York"), "2026-11-18");
});
test("date rollover and leap days", () => {
  assert.equal(shiftDay("2026-12-31", 1), "2027-01-01");
  assert.equal(shiftDay("2028-02-28", 1), "2028-02-29");
});
test("query windows follow both daylight saving changes", () => {
  for (const [key, hours] of [["2026-03-29", 23], ["2026-10-25", 25], ["2026-11-19", 24]]) {
    assert.equal((midnight(shiftDay(key, 1), "Europe/Vienna") - midnight(key, "Europe/Vienna")) / 3600000, hours);
  }
  assert.equal(midnight("2026-11-19", "America/New_York").toISOString(), "2026-11-19T05:00:00.000Z");
});
test("all-day events have exclusive ends, multi-day entries occur on each day", () => {
  const event = { summary: "Urlaub", start: "2026-11-19", end: "2026-11-21" };
  assert.equal(eventsForDay([event], "2026-11-19", "Europe/Vienna").length, 1);
  assert.equal(eventsForDay([event], "2026-11-20", "Europe/Vienna").length, 1);
  assert.equal(eventsForDay([event], "2026-11-21", "Europe/Vienna").length, 0);
});
test("timed, zero-duration and midnight boundaries are correct", () => {
  const events = [
    { summary: "End", start: "2026-11-18T23:00:00+01:00", end: "2026-11-19T00:00:00+01:00" },
    { summary: "Point", start: "2026-11-19T18:00:00+01:00", end: "2026-11-19T18:00:00+01:00" },
    { summary: "Span", start: "2026-11-18T23:00:00+01:00", end: "2026-11-19T01:00:00+01:00" },
    { summary: "Next", start: "2026-11-20T00:00:00+01:00", end: "2026-11-20T01:00:00+01:00" },
  ];
  assert.deepEqual(eventsForDay(events, "2026-11-19", "Europe/Vienna").map(e => e.summary), ["Span", "Point"]);
});
test("calendar REST-style values and service-response strings both work", () => {
  const event = { start: { dateTime: "2026-11-19T18:30:00+01:00" }, end: { dateTime: "2026-11-19T19:30:00+01:00" } };
  assert.equal(timeLabel(event, "Europe/Vienna"), "18:30");
  assert.equal(timeLabel({ start: { date: "2026-11-19" } }, "Europe/Vienna", "de"), "Ganztägig");
  assert.equal(eventsForDay([event], "2026-11-19", "Europe/Vienna").length, 1);
  assert.equal(dayLabel("2026-11-21", 2, "de"), "Samstag");
});

test("real amZettel meals retain complete titles and do not invent a time", () => {
  assert.deepEqual(mealPresentation({ summary: "Abendessen: Gemüse-Bowl", start: "2026-11-19" }, "Europe/Vienna"),
    { title: "Abendessen: Gemüse-Bowl", time: null });
  assert.deepEqual(mealPresentation({ summary: "Dinner: Pizza", start: "2026-11-19T18:30:00+01:00" }, "Europe/Vienna"),
    { title: "Dinner: Pizza", time: "18:30" });
  assert.deepEqual(mealPresentation(null, "Europe/Vienna"), { title: "Nothing planned yet", time: null });
});

test("both UI dictionaries contain exactly the same keys and localize controls/dates", () => {
  assert.deepEqual(Object.keys(COPY.en).sort(), Object.keys(COPY.de).sort());
  assert.equal(text("en", "showDone", { count: 2 }), "Already picked up (2)");
  assert.equal(text("de", "progress", { done: 2, total: 8 }), "2 von 8 besorgt");
  assert.equal(dayLabel("2026-11-21", 2), "Saturday");
  assert.equal(dayLabel("2026-11-19", 0, "de"), "Heute");
  assert.equal(timeLabel({ start: "2026-11-19" }, "UTC"), "All day");
});

test("timed events sort by instant rather than their offset-bearing strings", () => {
  const events = [
    { summary: "Later", start: "2026-11-19T09:00:00-05:00" },
    { summary: "Earlier", start: "2026-11-19T13:00:00+01:00" },
    null,
  ];
  assert.deepEqual(eventsForDay(events, "2026-11-19", "Europe/Vienna").map(e => e.summary), ["Earlier", "Later"]);
});

test("meal content keeps its original language under either UI language", () => {
  const event = { start: "2026-11-19", summary: "Abendessen: Ofengemüse mit Feta" };
  for (const language of ["de", "en"]) {
    assert.equal(mealPresentation(event, "Europe/Vienna", language).title, event.summary);
    assert.equal(mealPresentation(event, "Europe/Vienna", language).time, null);
  }
});

test("day boundaries work when the DST jump skips midnight", () => {
  for (const [zone, day] of [
    ["America/Havana", "2026-03-08"], ["Africa/Cairo", "2026-04-24"],
    ["Asia/Beirut", "2026-03-29"], ["America/Santiago", "2026-09-06"],
    ["Atlantic/Azores", "2026-03-29"],
  ]) {
    const first = midnight(day, zone);
    assert.equal(dateKey(first, zone), day);
    assert.notEqual(dateKey(new Date(first.getTime() - 1), zone), day);
    const next = midnight(shiftDay(day, 1), zone);
    assert.equal((next - first) / 3600000, 23);
    assert.equal(eventsForDay([{ start: day, end: shiftDay(day, 1) }], day, zone).length, 1);
  }
});

test("folded midnights choose the first instant and a skipped date stays usable", () => {
  const first = midnight("2026-11-01", "America/Havana");
  assert.equal(first.toISOString(), "2026-11-01T04:00:00.000Z");
  assert.notEqual(dateKey(new Date(first.getTime() - 1), "America/Havana"), "2026-11-01");
  assert.equal(midnight("2011-12-30", "Pacific/Apia").getTime(), midnight("2011-12-31", "Pacific/Apia").getTime());
  assert.throws(() => midnight("invalid", "UTC"), /Invalid calendar date/);
});
