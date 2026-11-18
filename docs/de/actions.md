# Aktionen und Ereignisse

[English](../actions.md) · **Deutsch**

[Dokumentationsübersicht](README.md) · [Beispiele](examples.md)

Alle eigenen Aktionen findest du unter **Entwicklerwerkzeuge → Aktionen** mit dem Präfix `amzettel`.
Als Ziel eine Entität dieser Integration auswählen. Die Beispiele benutzen fiktive Entitäts-IDs:
Ersetze sie durch die IDs deiner Installation, nicht nur durch deren Anzeigenamen.

## Antworten verwenden

`get_items`, `get_events` und `get_meals` erfordern eine Antwort; in Skripten und Automationen
`response_variable` angeben. `add_item`, `add_event` und `sync_events` können optional antworten.
Bei mehreren Zielen enthält die Antwort einen Eintrag pro Entitäts-ID:

```yaml
action: amzettel.get_items
target:
  entity_id: todo.wocheneinkauf
data:
  include_completed: true
response_variable: einkauf
```

Die Einträge sind anschließend unter `einkauf['todo.wocheneinkauf']['items']` erreichbar.
Eine Antwort wird nicht von selbst als neue HA-Entität angelegt.

## `amzettel.add_item`

Ziel: ein Zettel (`todo`).

| Feld | Bedeutung |
|---|---|
| `name` | Pflicht: Name des Eintrags |
| `quantity` | Positive Menge; Standard 1 |
| `unit` | Optional, z. B. `L` |
| `category` | Optionale Einkaufskategorie als Text |
| `note` | Optionale Notiz |

Die Antwort enthält `item_id`. amZettel kann gleiche Einkaufseinträge mengenmäßig zusammenführen;
die zurückgegebene Id ist dann die des zusammengeführten Eintrags.
Ein erneuter Aufruf ist **nicht idempotent** und kann Menge hinzufügen.

Die Standardaktionen `todo.add_item`, `todo.update_item` und `todo.remove_item` funktionieren ebenfalls.
Menge und Einheit sind Zusatzfelder unserer Aktion.

## `amzettel.add_event`

Ziel: ein normaler amZettel-Kalender oder Kategorie-Kalender (`calendar`), **kein Essensplan**.
Erfordert `calendar.write`, Premium und weiterhin Zugriff auf den Kalender.

| Feld | Bedeutung |
|---|---|
| `summary` | Pflicht: Titel |
| `start_date_time` | Beginn mit Uhrzeit; alternativ `start_date` |
| `end_date_time` | Optionales Ende mit Uhrzeit |
| `time_zone` | Optionale IANA-Zone, z. B. `Europe/Vienna`, `Europe/London` oder `UTC` |
| `start_date` | Erster Tag eines ganztägigen Termins; alternativ `start_date_time` |
| `end_date` | Optional: Tag **nach** dem letzten Tag |
| `description` | Optionale Notiz; ohne Feld bleibt eine vorhandene Notiz unverändert |
| `category` | Optionale Kategorie per Name oder Id; ohne Feld bleibt sie unverändert |
| `external_id` | Optional: deine stabile Kennung dieses Termins innerhalb des Kalenders |

Beginn und Ende müssen beide Datum oder beide Datum mit Uhrzeit sein. Ohne Ende entsteht ein
Zeitpunkt beziehungsweise ein einzelner ganztägiger Termin.
Kategorie-Kalender legen mit ihrer eigenen Kategorie an; eine andere Kategorie wird abgelehnt.

**Wiederholbare Aufrufe:** Mit derselben `external_id` im selben Kalender wird derselbe Termin
aktualisiert, nicht ein zweiter angelegt. Eine Änderung erzeugt `updated`, ein identischer Aufruf
`unchanged`. Ohne Kennung legt jeder Aufruf einen neuen Termin an.
Die Antwort enthält `event_id` und `result` (`created`, `updated`, `unchanged`).

Die Aktion verwaltet keine Serienregel. Zum Anlegen einer Serie kann die unterstützte
Standardaktion `calendar.create_event` mit `rrule` verwendet werden.
Zum Beenden oder gezielten Bearbeiten einer bestehenden Serie die amZettel-App verwenden.

## `amzettel.sync_events`

Ziel: ein normaler amZettel-Kalender oder Kategorie-Kalender.
Erfordert dieselben Schreibrechte wie `add_event`.

| Feld | Bedeutung |
|---|---|
| `source` | Pflicht: stabiler Name der Quelle, z. B. `abfuhrkalender` |
| `start` | Pflicht: Anfang des Abgleichzeitraums |
| `end` | Pflicht: Ende des Abgleichzeitraums |
| `events` | Pflicht: Liste der gelieferten Termine |
| `allow_empty` | Standard `false`; eine leere Liste wird erst mit `true` akzeptiert |

Ein Element von `events` hat die Pflichtfelder `external_id`, `summary`, `start` und optional
`end`, `description`, `category`. `start` und `end` können ISO-Daten oder ISO-Zeitstempel sein,
wie bei `calendar.get_events`. Nicht unterstützte Zusatzfelder, beispielsweise `location`,
werden ignoriert.

**Was der Abgleich macht:**

1. Gelieferte Termine mit stabiler Kennung anlegen oder aktualisieren.
2. Nicht mehr gelieferte Termine entfernen, aber **nur** wenn sie
   - in HAs lokaler Herkunftszuordnung dieser Quelle stehen,
   - die zu Quelle und externer Kennung passende Id haben und
   - laut frischem Serverstand innerhalb des Zeitraums beginnen.
3. Andere Termine, andere Quellen und eigene Termine außerhalb des Zeitraums nicht löschen.

Ein Enddatum als reines Datum schließt bei diesem **Abgleichzeitraum** den ganzen Tag ein.
Ein Endzeitstempel ist eine ausschließende Grenze.
Bei den **einzelnen ganztägigen Terminen** bleibt `end` dagegen der Tag danach.

Die Antwort enthält die Zahlen `created`, `updated`, `removed` und `unchanged`.
Ein Fehler beim erforderlichen frischen Abruf verhindert das Schreiben.
Ein Abgleich besteht aber aus mehreren HTTP-Aufrufen, nicht aus einer serverseitigen Transaktion:
Bei einem Fehler während des Schreibens können bereits einzelne Änderungen angekommen sein.
Mit stabilen Kennungen kann derselbe Abgleich erneut ausgeführt werden.

> [!WARNING]
> `allow_empty: true` bedeutet ausdrücklich: „Die Quelle hat bestätigt keine Termine mehr im
> Zeitraum.“ Nutze es nicht als Ersatz für einen fehlgeschlagenen Quellenabruf.
> Ein unvollständig gelieferter Zeitraum kann auch bei einer nicht leeren Liste eigene Termine
> entfernen. Übergib deshalb immer den vollständigen Bestand des gewählten Zeitraums.

**Herkunft und Backups:** Die Zuordnung liegt in HAs Storage unter `.storage/amzettel.sync_sources`.
Nicht manuell bearbeiten. Sichere den HA-Konfigurationsordner mit.
Beim Entfernen des Integrationseintrags wird die Zuordnung vergessen, Termine bleiben aber in
amZettel. Ohne die alte Zuordnung kann ein späterer Abgleich nicht mehr gelieferte alte Termine
nicht automatisch als seine eigenen erkennen.

`source` und `external_id` sollten nicht bei jedem Lauf wechseln.
Die Kennungen von `add_event` und `sync_events` sind absichtlich getrennt:
Ein von `add_event` erzeugter Termin wird nicht nachträglich von einem gleichnamigen
`sync_events`-Eintrag übernommen.

## Abfragen

### `amzettel.get_items`

Ziel: Zettel. Optional `include_completed: true`, standardmäßig nur offene Einträge.

Antwort: `list_id`, `name`, `type`, `items`.
Einträge enthalten unter anderem `id`, `name`, `quantity`, `unit`, `category`, `note`, `due`,
`completed` und `completed_at`.
Auf Einkaufszetteln sind die erledigten Einträge die **kürzlich gekauften** gemäß der App-Regel,
nicht die vollständige Kaufhistorie.

### `amzettel.get_events`

Ziel: Kalender oder Kategorie-Kalender. Optional `start`, `end`, `category` (Name oder Id).
Ohne Zeitraum: ab jetzt sieben Tage. Ein reines Enddatum schließt den ganzen Tag ein.

Antwort: `calendar_id`, `start`, `end`, `events`.
Serien sind in Vorkommen aufgelöst. Die Einträge enthalten unter anderem `uid`, `recurrence_id`,
`summary`, `start`, `end`, `all_day`, `category_id`, `category`, `note`, `persons`, `recurring`.
Bei ganztägigen Ergebnissen ist `end` der Tag danach.

Die Hauptkalender-Abfrage umfasst alle Kategorien, auch die separat angezeigten.
Auf einem Kategorie-Kalender kommen nur Termine dieser Kategorie.

### `amzettel.get_meals`

Ziel: ein Essensplan-Kalender. Optional `start`, `end`, sonst ab jetzt sieben Tage.

Antwort: `meal_plan_id`, `start`, `end`, `meals`.
Die Mahlzeiten enthalten unter anderem Id, Datum, Zeitangabe, Gerichte, Personenzahl, Notiz
und das Kennzeichen für die Kalenderanzeige.
`time` und `time_zone` enthalten die gespeicherte Snapshot-Uhrzeit und -Zone; ohne Snapshot
ist `time_zone` null und `time` die alte genaue Uhrzeit oder null. Das Datum bleibt das gespeicherte Mahlzeitendatum;
das Abfragefenster berücksichtigt den tatsächlichen Zeitpunkt in der gespeicherten Zone.
Auch im Kalender ausgeblendete Mahlzeiten werden geliefert.
Die Aktion schreibt keine Mahlzeiten und setzt keine Zutaten auf einen Zettel.

## Zeitzonen und Datumsgrenzen

Bei `add_event` ohne `time_zone` gilt die konfigurierte HA-Zone.
Mit `time_zone`:

- `"2026-11-24 10:00:00"` und `Europe/Vienna`: **10:00 in Wien**.
- `"2026-11-24T09:00:00Z"` und `Europe/Vienna`: **derselbe Zeitpunkt**, also 10:00 in Wien.
- `"2026-11-24 09:00:00"` und `UTC`: **09:00 UTC**, nicht 09:00 Wiener Zeit.
- Ganztägige Termine bleiben Datumswerte, auch wenn `time_zone` mitgegeben wird.

`sync_events` hat kein separates `time_zone`-Feld. Zeitstempel dort möglichst mit Offset liefern;
für Uhrzeiten ohne Offset gilt die HA-Zone.

Die API speichert Minuten, keine Sekunden. Bei einer doppelten lokalen Uhrzeit im Herbst verwenden
die Schreibwege UTC, damit der genaue Zeitpunkt erhalten bleibt. Eine Zeit in der übersprungenen
Frühlingsstunde wird auf die reale spätere Ortszeit normalisiert.

Bei Serien ist eine feste UTC-Uhrzeit über eine örtliche Zeitumstellung hinweg nicht dieselbe
Regel wie eine feste Uhrzeit in `Europe/Vienna`. Eine Ortszone lässt sich nicht aus einem Offset
erraten.

## Ereignisse für Automationen und Node-RED

Alle enthalten `config_entry_id`. Objekt- und Eintrags-Ids sind amZettel-Ids, keine HA-Entitäts-IDs.

| Ereignis | Wichtige Daten |
|---|---|
| `amzettel_item_added` | `list_id`, `list_name`, `item_id`, `name`, `quantity`, `unit`, `category`, `note` |
| `amzettel_item_completed` | wie oben; `by_user_id` nur, wenn der Server den Käufer liefert |
| `amzettel_item_removed` | wie `item_added` |
| `amzettel_event_created` | `calendar_id`, `calendar_name`, `event_id`, `summary`, `start`, `all_day`, Kategorie und Notiz |
| `amzettel_event_changed` | wie `event_created` |
| `amzettel_event_removed` | wie `event_created` |
| `amzettel_meal_planned` | `meal_plan_id`, `meal_plan_name`, `meal_id`, `date`, `time`, `time_zone` (Snapshot-Zone, sonst null), `slot`, `dishes`, `people`, `note` |

Beim ersten Laden, einem Reload oder dem ersten Sichtbarwerden eines Objekts werden keine
Ereignisse für dessen gesamten Inhalt ausgesendet. Erst nachfolgende Unterschiede lösen sie aus.
Ein Neuverbinden vergleicht mit dem letzten Stand.
Ein Kauf löst `item_completed` statt `item_removed` aus.
Wird ein Einkaufsartikel mengenmäßig mit einer bestehenden Id zusammengeführt, entsteht kein
`item_added`.

Die Ereignisse entstehen aus Zustandsvergleichen und sind **kein vollständiges Änderungsprotokoll**.
Mehrere Änderungen zwischen zwei Abrufen können zusammenfallen.
Wenn ein Ablauf den vollständigen aktuellen Stand braucht, verwende zusätzlich eine Abfrageaktion.

## Bekannte Grenzen

- Kalender können nur ganze Serien ändern oder löschen, nicht einzelne Vorkommen oder
  „ab diesem späteren Vorkommen“. Eine Serienregel wird beim bloßen Umbenennen erhalten.
- Gekaufte Artikel bleiben in der Kaufhistorie. Löschen von Käufen beziehungsweise
  `todo.remove_completed_items` mit Käufen wird mit einer verständlichen Meldung abgelehnt.
  Über „nicht erledigt“ kann ein Kauf zurückgenommen werden.
- Ein einziger `todo.update_item`-Aufruf zum **Abhaken und gleichzeitigen Leeren aller vorhandenen
  Notiz-/Fälligkeitsfelder** wird als reines Abhaken behandelt. Assist sendet denselben unvollständigen
  Datensatz; Schutz vor unbeabsichtigtem Datenverlust hat Vorrang.
  Gewolltes Leeren deshalb zuerst ausführen, danach in einem zweiten Aufruf abhaken.
- In HA 2026.10.4 kann der `on`/`off`-Zustand eines Kalendertermins während der doppelten Stunde
  am Herbst-Umstellungstag falsch sein. Das ist eine Grenze der HA-Basisklasse; Abfragen und
  die gespeicherten Zeitpunkte sind davon zu unterscheiden.
