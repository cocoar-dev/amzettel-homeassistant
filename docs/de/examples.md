# Beispiele

[English](../examples.md) · **Deutsch**

[Dokumentationsübersicht](README.md) · [Aktionsreferenz](actions.md)

Die folgenden Entitäts-IDs sind **Beispiele**. Ersetze sie durch die tatsächlichen IDs aus
**Einstellungen → Geräte & Dienste → Entitäten**.
Eine einzelne Aktion lässt sich in **Entwicklerwerkzeuge → Aktionen → YAML-Modus** ausprobieren.
Ganze Automationen gehören in den YAML-Editor einer neuen Automation.

## 1. Einkauf mit Menge hinzufügen

```yaml
action: amzettel.add_item
target:
  entity_id: todo.wocheneinkauf
data:
  name: Milch
  quantity: 2
  unit: L
  category: Milchprodukte
  note: Für die Schuljause
```

Das ist nicht wiederholbar ohne Nebenwirkung: Ein zweiter Aufruf kann die Menge erhöhen.

Für den Assist-Test genügt der getippte Satz:

> Setze Milch auf die Liste Wocheneinkauf.

Zum Abhaken:

> Hake Milch auf der Liste Wocheneinkauf ab.

## 2. Termin ohne Duplikate, mit eigener Zeitzone

```yaml
action: amzettel.add_event
target:
  entity_id: calendar.haushalt
data:
  summary: Zahnarzt
  start_date_time: "2026-11-24 10:00:00"
  time_zone: Europe/Vienna
  description: Karte mitnehmen
  external_id: zahnarzt-termin-4711
```

Ohne Ende ist das ein Zeitpunkt. Mit derselben `external_id` kann die Uhrzeit später geändert
werden; ein identischer Aufruf legt nichts doppelt an. Für eine Kategorie zusätzlich
`category` mit deren tatsächlichem Namen oder ihrer Id angeben.

Ein ganztägiger Termin:

```yaml
action: amzettel.add_event
target:
  entity_id: calendar.haushalt
data:
  summary: Gelbe Tonne
  start_date: "2026-11-26"
  end_date: "2026-11-27"
  external_id: gelb-2026-11-26
```

`end_date` ist der **Tag danach**, nicht der letzte Tag selbst.

## 3. Müll-Termine aus einem anderen HA-Kalender übernehmen

Voraussetzung: Eine andere Integration stellt einen Kalender mit Abfuhrterminen bereit.
Dieses Beispiel benutzt `calendar.abfuhrquelle` als **Quelle** und
`calendar.haushalt` als **amZettel-Ziel**. Die Installation oder Konfiguration einer bestimmten
Müll-Integration ist nicht Bestandteil von amZettel.

Die Automation fragt täglich die nächsten 60 Tage ab.
Sie läuft nicht weiter, wenn der Quellenabruf fehlschlägt oder keine Termine liefert.

```yaml
alias: Abfuhrtermine nach amZettel
triggers:
  - trigger: time
    at: "06:00:00"
actions:
  - action: calendar.get_events
    target:
      entity_id: calendar.abfuhrquelle
    data:
      start_date_time: "{{ today_at().isoformat() }}"
      end_date_time: "{{ (today_at() + timedelta(days=60)).isoformat() }}"
    response_variable: quelle
  - condition: template
    value_template: "{{ quelle['calendar.abfuhrquelle']['events'] | count > 0 }}"
  - action: amzettel.sync_events
    target:
      entity_id: calendar.haushalt
    data:
      source: abfuhrkalender
      start: "{{ today_at().isoformat() }}"
      end: "{{ (today_at() + timedelta(days=60)).isoformat() }}"
      events: >-
        {% set ns = namespace(events=[]) %}
        {% for e in quelle['calendar.abfuhrquelle']['events'] %}
          {% set key = (e.get('uid') or e.summary) ~ '|' ~ e.start %}
          {% set item = dict(external_id=key, summary=e.summary, start=e.start) %}
          {% if e.get('end') %}
            {% set item = dict(item, end=e.end) %}
          {% endif %}
          {% set ns.events = ns.events + [item] %}
        {% endfor %}
        {{ ns.events }}
mode: single
```

Erst mit wenigen Testterminen ausprobieren und das Ergebnis kontrollieren.
Der Abgleich darf nur dann löschen, wenn die Quelle den Zeitraum vollständig liefert.
Keine `continue_on_error`-Regel für den Quellenabruf hinzufügen.
Die leere Liste wird hier bewusst **nicht** mit `allow_empty: true` freigegeben.

Viele Kalender liefern keine `uid`. Dann verwendet das Beispiel Titel und Beginn als Kennung.
Zwei Termine mit identischem Titel und Beginn wären damit doppelte Kennungen und würden abgelehnt.
Für Quellen mit einer eigenen stabilen Abfuhr-Id diese bevorzugen und das Mapping entsprechend
anpassen. Ändert sich der Beginn als Teil der Kennung, wird der neue Termin angelegt und der alte
innerhalb des Abgleichzeitraums entfernt.

## 4. Müll-Erinnerung am Vorabend

Für diese Automation wurde die Kategorie **Müll** in der Integration als eigener Kalender
`calendar.haushalt_mull` ausgewählt. Das Beispiel sendet eine Meldung innerhalb von HA.

```yaml
alias: Müll am Vorabend
triggers:
  - trigger: calendar
    entity_id: calendar.haushalt_mull
    event: start
    offset: "-05:00:00"
actions:
  - action: persistent_notification.create
    data:
      title: Müllabfuhr morgen
      message: "Morgen: {{ trigger.calendar_event.summary }}"
mode: queued
```

Das passt zu **ganztägigen** Abfuhrterminen: Mitternacht minus fünf Stunden ist 19 Uhr am Vortag.
Bei einem Termin mit Uhrzeit gilt der Versatz relativ zu dieser Uhrzeit.
Kalender-Auslöser testen, indem ein neuer Termin ausreichend in der Zukunft angelegt wird,
nicht indem man einen bereits vergangenen Start erwartet.

Du kannst die letzte Aktion gegen deine Handy-Benachrichtigung oder TTS-/Lautsprecher-Aktion
tauschen. Deren Ziel und Einrichtung hängen von deiner HA-Installation ab.

## 5. Essensplan für heute abfragen

Dieses Skript erzeugt eine HA-Meldung aus den strukturierten Mahlzeiten:

```yaml
alias: Was gibt es heute
sequence:
  - action: amzettel.get_meals
    target:
      entity_id: calendar.mein_essensplan
    data:
      start: "{{ now().date().isoformat() }}"
      end: "{{ now().date().isoformat() }}"
    response_variable: plan
  - action: persistent_notification.create
    data:
      title: Essensplan heute
      message: >-
        {% set meals = plan['calendar.mein_essensplan']['meals'] %}
        {% set ns = namespace(lines=[]) %}
        {% for meal in meals %}
          {% set names = meal.dishes | map(attribute='name') | join(', ') %}
          {% set ns.lines = ns.lines + [names] %}
        {% endfor %}
        {{ ns.lines | join('; ') if ns.lines else 'Noch nichts geplant.' }}
```

Auch im Kalender ausgeblendete Mahlzeiten können in dieser Antwort stehen.
`time` und `time_zone` geben einen vorhandenen Mahlzeiten-Snapshot wieder.
Ohne Snapshot ist eine benannte Essenszeit keine verbindliche Uhrzeit; die Automation legt ihren Auslösezeitpunkt selbst fest.

## 6. Node-RED

Mit einer eingerichteten Verbindung von Node-RED zu HA kann der Flow die normalen
HA-Entitäten, Aktionen und Ereignisse verwenden. Ein zusätzlicher **amZettel-Token** ist nicht nötig.
Die Anmeldung von Node-RED an HA ist davon unabhängig.

### Checkliste für HA-Automationen und Node-RED

- **Doppelte Auslösung vermeiden:** Sensorflattern entprellen, Rückkopplungen verhindern
  und Schreibaktionen nicht blind wiederholen. Jeder Aufruf von
  [`amzettel.add_item`](actions.md#amzetteladd_item) kann die Menge erneut erhöhen.
- **Kalendertermine wiederholbar anlegen:** Bei
  [`amzettel.add_event`](actions.md#amzetteladd_event) eine stabile `external_id` verwenden,
  damit ein erneuter Aufruf denselben Termin aktualisiert statt einen weiteren anzulegen.
- **Rechte bleiben gleich:** Kalender schreiben benötigt Premium und Schreibfreigabe;
  Essenspläne bleiben nur lesbar. Node-RED umgeht keine
  [amZettel-Berechtigungen](configuration.md#welche-rechte-braucht-ha).
- **Abfrageantworten auswerten:** Für `get_items`, `get_events` und `get_meals` in
  HA-Skripten und -Automationen `response_variable` angeben. In Node-RED die Antwortausgabe
  des Aktionsknotens konfigurieren, etwa den Ausgabetyp `results`.
  Siehe [Antworten verwenden](actions.md#antworten-verwenden).
- **Ereignisse sind kein vollständiges Änderungsprotokoll:** Wenn der Ablauf den aktuellen
  Bestand benötigt, diesen nach einem Neustart oder Neuverbinden zusätzlich abfragen.
  Die [Ereignisreferenz](actions.md#ereignisse-für-automationen-und-node-red) erklärt,
  welche Änderungen Ereignisse auslösen und welche zusammenfallen können.
- **Nur vollständige Kalenderquellen abgleichen:** Bei
  [`sync_events`](actions.md#amzettelsync_events) immer den vollständigen Bestand des
  Zeitraums übergeben. Bei einem fehlgeschlagenen Quellenabruf abbrechen, statt eine
  leere Liste einzusetzen oder sie mit `allow_empty: true` freizugeben.

Beispiel „Kaffee ist fast leer“:

1. Den bestehenden Sensor-/Schwellwert-Flow verwenden.
2. In einem HA-Aktionsknoten (in älteren Versionen „Call service“) **`amzettel.add_item`** auswählen.
3. Als Ziel `todo.wocheneinkauf` und als Daten beispielsweise
   `{"name":"Kaffee","quantity":1,"unit":"Packung"}` verwenden.

Beispiel „Auf einen neuen Eintrag reagieren“:

1. Im Ereignisknoten (oft „Events: all“) `amzettel_item_added` abonnieren.
2. Zuerst mit einem Debug-Knoten die tatsächliche Nachrichtenausgabe ansehen.
3. Auf `list_id` oder `list_name` in den Ereignisdaten filtern.
4. `name`, `quantity` und `unit` an den gewünschten nächsten Schritt übergeben.

Die genaue Position der Ereignisdaten innerhalb der Node-RED-Nachricht hängt von der
Knotenversion und deren Ausgabekonfiguration ab. Deshalb gibt es hier keine pauschale
Behauptung wie `msg.payload.event.name`.
Die vollständigen Ereignisfelder stehen in der [Referenz](actions.md#ereignisse-für-automationen-und-node-red).
