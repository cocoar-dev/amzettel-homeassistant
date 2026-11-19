// MIT · COCOAR e.U. View helpers; dates follow the HA installation's time zone.
export const COPY = {
  en: {
    locale: "en-GB", home: "HOME", household: "Our home", demo: "EXAMPLE",
    heroFirst: "Less on your mind.", heroSecond: "More from your day.",
    introFirst: "Your plans, your meals, your shopping.", introSecond: "Everything together. Everyone in the loop.",
    glance: "Today at home", appointments: "plans", shoppingStat: "on the list", mealStat: "meal plan",
    together: "Everyday life is better together.", agendaEyebrow: "A LITTLE PLANNING",
    agenda: "The next three days", eventFallback: "Planned together",
    openDay: "Room for spontaneous plans.", loadingEvents: "Loading your plans…",
    event: "plan", events: "plans", todayFooter: "Have a lovely day!",
    tomorrowFooter: "Already in view.", futureFooter: "Something to look forward to.",
    shoppingEyebrow: "SHOP TOGETHER", shopping: "On the list",
    shoppingSubtitle: "If it’s missing, add it here.", check: "mark as done", reopen: "reopen",
    allDone: "All sorted. Time for something nice.", addLabel: "Add to shopping list",
    addPlaceholder: "Anything else?", add: "Add item",
    writeError: "Could not save. Please try again.", progress: "{done} of {total} picked up",
    showDone: "Already picked up ({count})", hideDone: "Hide completed items",
    mealEyebrow: "THE BEST QUESTION OF THE DAY", meal: "What’s on the menu",
    todayMeal: "TODAY", mealEmpty: "Nothing planned yet", mealLoading: "One moment…",
    mealTogether: "Everything tastes better together.", mealNote: "Saved in your meal plan",
    mealIdea: "Still room for your ideas", mealOpen: "Still open",
    footer: "Running in Home Assistant", footerDemo: "Fictional example data · ",
    tagline: "More together. Less to organise.", readError: "Data is currently unavailable.",
    retry: "Try again", planned: "Planned", allDay: "All day", today: "Today", tomorrow: "Tomorrow",
  },
  de: {
    locale: "de-AT", home: "ZUHAUSE", household: "Unser Zuhause", demo: "BEISPIEL",
    heroFirst: "Weniger im Kopf.", heroSecond: "Mehr vom Tag.",
    introFirst: "Eure Termine, euer Essen, euer Einkauf.", introSecond: "Alles da. Für alle im Blick.",
    glance: "Heute bei euch", appointments: "Termine", shoppingStat: "auf dem Zettel", mealStat: "Essensplan",
    together: "Zusammen wird Alltag leichter.", agendaEyebrow: "GUT VORBEREITET",
    agenda: "Die nächsten drei Tage", eventFallback: "Gemeinsam geplant",
    openDay: "Platz für spontane Pläne.", loadingEvents: "Termine werden geladen …",
    event: "Termin", events: "Termine", todayFooter: "Einen schönen Tag!",
    tomorrowFooter: "Schon im Blick.", futureFooter: "Vorfreude inklusive.",
    shoppingEyebrow: "GEMEINSAM EINKAUFEN", shopping: "Auf dem Zettel",
    shoppingSubtitle: "Was fehlt, kommt hier drauf.", check: "abhaken", reopen: "wieder öffnen",
    allDone: "Alles besorgt. Zeit für etwas Schönes.", addLabel: "Zum Einkaufszettel hinzufügen",
    addPlaceholder: "Noch etwas vergessen?", add: "Eintrag hinzufügen",
    writeError: "Nicht gespeichert. Bitte erneut versuchen.", progress: "{done} von {total} besorgt",
    showDone: "Schon besorgt ({count})", hideDone: "Erledigte ausblenden",
    mealEyebrow: "DIE SCHÖNSTE FRAGE DES TAGES", meal: "Was gibt’s zu essen",
    todayMeal: "HEUTE", mealEmpty: "Noch nichts geplant", mealLoading: "Einen Moment …",
    mealTogether: "Gemeinsam schmeckt’s am besten.", mealNote: "Im Essensplan vorgemerkt",
    mealIdea: "Noch Platz für eure Ideen", mealOpen: "Noch offen",
    footer: "Läuft in Home Assistant", footerDemo: "Fiktive Beispieldaten · ",
    tagline: "Mehr gemeinsam. Weniger organisieren.", readError: "Daten gerade nicht erreichbar.",
    retry: "Erneut laden", planned: "Geplant", allDay: "Ganztägig", today: "Heute", tomorrow: "Morgen",
  },
};

export function text(language, key, values = {}) {
  return COPY[language][key].replace(/\{(\w+)\}/g, (_, name) => String(values[name] ?? ""));
}

export const escapeHTML = value => String(value ?? "").replace(
  /[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char],
);

const eventStart = event => event?.start?.dateTime ?? event?.start?.date ?? event?.start;

export function dateKey(value, zone) {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: zone, year: "numeric", month: "2-digit", day: "2-digit",
  }).formatToParts(new Date(value));
  const part = type => parts.find(p => p.type === type).value;
  return `${part("year")}-${part("month")}-${part("day")}`;
}

export function shiftDay(key, amount) {
  const date = new Date(`${key}T12:00:00Z`);
  date.setUTCDate(date.getUTCDate() + amount);
  return date.toISOString().slice(0, 10);
}

export function midnight(key, zone) {
  const target = Date.parse(`${key}T00:00:00Z`);
  if (!Number.isFinite(target)) throw new Error("Invalid calendar date.");
  const formatter = new Intl.DateTimeFormat("en-CA", {
    timeZone: zone, year: "numeric", month: "2-digit", day: "2-digit",
  });
  const keyAt = value => {
    const parts = formatter.formatToParts(value);
    const get = type => parts.find(p => p.type === type).value;
    return `${get("year")}-${get("month")}-${get("day")}`;
  };
  // Find the first real instant on the date, not a potentially nonexistent
  // wall-clock 00:00. This also chooses the first occurrence of a folded midnight.
  // A completely skipped date resolves to the following date's boundary.
  let lower = target - 36 * 3600000;
  let upper = target + 36 * 3600000;
  if (keyAt(lower) >= key || keyAt(upper) < key) throw new Error("Cannot resolve calendar day.");
  while (lower < upper) {
    const middle = Math.floor((lower + upper) / 2);
    if (keyAt(middle) < key) lower = middle + 1;
    else upper = middle;
  }
  return new Date(lower);
}

export function threeDays(now, zone) {
  const today = dateKey(now, zone);
  return [0, 1, 2].map(offset => shiftDay(today, offset));
}

export function eventsForDay(events, key, zone) {
  const from = midnight(key, zone).getTime();
  const until = midnight(shiftDay(key, 1), zone).getTime();
  return events.filter(event => {
    const start = eventStart(event);
    const end = event?.end?.dateTime ?? event?.end?.date ?? event?.end ?? start;
    if (typeof start !== "string") return false;
    if (/^\d{4}-\d\d-\d\d$/.test(start)) {
      return start <= key && (end > key || (start === end && start === key));
    }
    const a = Date.parse(start), b = Date.parse(end);
    return a < until && (b > from || (a === b && a >= from));
  }).sort((a, b) => {
    const instant = event => {
      const start = eventStart(event);
      return /^\d{4}-\d\d-\d\d$/.test(start) ? midnight(start, zone).getTime() : Date.parse(start);
    };
    return instant(a) - instant(b);
  });
}

export function timeLabel(event, zone, language = "en") {
  const value = eventStart(event);
  if (!value || /^\d{4}-\d\d-\d\d$/.test(value)) return COPY[language].allDay;
  return new Intl.DateTimeFormat(COPY[language].locale, {
    timeZone: zone, hour: "2-digit", minute: "2-digit",
  }).format(new Date(value));
}

export function dayLabel(key, index, language = "en") {
  return index === 0 ? COPY[language].today : index === 1 ? COPY[language].tomorrow :
    new Intl.DateTimeFormat(COPY[language].locale, { weekday: "long", timeZone: "UTC" }).format(new Date(`${key}T12:00:00Z`));
}

export function mealPresentation(event, zone, language = "en") {
  if (!event) return { title: COPY[language].mealEmpty, time: null };
  const start = eventStart(event);
  return {
    title: String(event.summary ?? ""),
    time: !start || /^\d{4}-\d\d-\d\d$/.test(start) ? null : timeLabel(event, zone, language),
  };
}
