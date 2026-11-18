<img src="custom_components/amzettel/brand/icon.png" alt="amZettel" width="96" height="96">

# amZettel für Home Assistant

[English](README.md) · **Deutsch**

Einkaufszettel, Aufgaben, Kalender und Essenspläne aus **amZettel** in Home Assistant:
im Dashboard, über Assist oder als Bausteine für Automationen und Node-RED.
Die Integration verbindet sich über REST/SSE v1 mit `app.amzettel.at`.
Änderungen kommen per SSE; nach einer Unterbrechung holt HA sie nach.

## Loslegen

1. **Home Assistant ab 2026.10.0** und ein amZettel-Konto bereithalten.
2. Die [Integration installieren](docs/de/installation.md), über HACS/Marketplace oder manuell.
3. Home Assistant neu starten und unter **Einstellungen → Geräte & Dienste → Integration hinzufügen**
   nach **amZettel** suchen.
4. Den angezeigten Link öffnen, den Code mit dem amZettel-Konto bestätigen und auswählen,
   welche Zettel, Kalender und Essenspläne erscheinen sollen.

Kein API-Schlüssel, kein Client-Secret und keine öffentliche Adresse deiner HA-Instanz nötig.
Die **[vollständige bebilderte Dokumentation](docs/de/README.md)** führt von der
amZettel-Anmeldung bis zum HA-Kalender und den Automationen.

## Was du bekommst

| amZettel | Home Assistant | Kostenlos | Premium |
|---|---|---|---|
| Einkauf, Checkliste, Aufgaben | `todo`: lesen, hinzufügen, abhaken, ändern, löschen | Lesen und ändern | ebenso |
| Kalender | `calendar`: lesen, Termine anlegen, ändern und löschen | Lesen | Lesen und ändern |
| Essensplan | `calendar`: lesen, zusätzlich strukturierte Abfrage | Lesen | Lesen |

Freigegebene Rechte und die Mitgliedschaft am jeweiligen Objekt gelten zusätzlich.
Eigene Kalender für einzelne ausgewählte Kategorien sind optional.
Essenspläne sind in dieser Integration auch mit Premium nur lesend.

## Anleitungen

- **[Installation](docs/de/installation.md):** Voraussetzungen, HACS/Marketplace, manuelles Kopieren,
  vollständige Bildfolge von Device-Code über amZettel-Anmeldung und Zugriffsfreigabe bis zur fertigen
  Verbindung, Updates und Entfernen.
- **[Einrichtung und Bedienung](docs/de/configuration.md):** Rechte, Auswahl, Kategorie-Kalender,
  geteilte Zettel, Zeitzonen, Assist und Essenspläne – mit Kalender- und Bearbeitungsbildern.
- **[Aktionen und Ereignisse](docs/de/actions.md):** Felder, Antwortdaten, Synchronisation und Grenzen.
- **[Beispiele](docs/de/examples.md):** Einkaufszettel per Sprache, Kalenderimport, Müll-Erinnerung,
  Essensplan und Node-RED.
- **[Fehlerhilfe](docs/de/troubleshooting.md):** Integration fehlt, Anmeldung abgelaufen, Rechte,
  Verbindungsprobleme und Diagnose.

## Aktionen

| Aktion | Zweck |
|---|---|
| `amzettel.add_item` | Zetteleintrag mit Menge, Einheit, Kategorie und Notiz |
| `amzettel.add_event` | Termin, optional mit eigener Zeitzone und stabiler `external_id` gegen Duplikate |
| `amzettel.sync_events` | Termine einer Quelle abgleichen; entfernt nur nachweislich eigene Termine im Zeitraum |
| `amzettel.get_items` | Einträge einschließlich optional erledigter Einträge abfragen |
| `amzettel.get_events` | Termine eines Zeitraums mit aufgelösten Serien abfragen |
| `amzettel.get_meals` | Geplante Mahlzeiten mit Gerichten, Zeitangabe und Personenzahl abfragen |

Außerdem funktionieren die unterstützten Standardaktionen von `todo` und `calendar`.
Einzelne Vorkommen einer Serie können nicht separat geändert werden, nur die ganze Serie.
Details und weitere Grenzen stehen in der [Aktionsreferenz](docs/de/actions.md).

## Entwicklung

```sh
uv venv --python 3.14 .local/venv
uv pip install --python .local/venv/bin/python -r requirements_test.txt
PYTHONPYCACHEPREFIX="$PWD/.local/cache/pycache" .local/venv/bin/python -m pytest -q
```

`.local/` ist vollständig von Git ausgeschlossen. Dort bleiben lokale Umgebungen,
Caches und interne Arbeitsunterlagen; sie gehören weder ins Repository noch nach HA.

Die CI prüft pytest, hassfest und HACS. Icon und Logo werden lokal mitgeliefert:
[Herkunft und Prüfung der Bilder](docs/de/branding.md).

## Lizenz und Sicherheit

Integrationscode, Tests und Dokumentation stehen unter [MIT](LICENSE).
Du darfst sie nutzen, verändern und weitergeben; Copyright- und Lizenzhinweis müssen erhalten bleiben.
Copyright © 2026 COCOAR e.U. Den Geltungsbereich beschreibt [NOTICE](NOTICE).

Für die mitgelieferten amZettel-Markenbilder gelten [gesonderte Bedingungen](TRADEMARK.md).
Sie dürfen unverändert mit der Integration weitergegeben werden, auch mit angepassten Versionen;
eine allgemeine Markenfreigabe ist das nicht. Die Lizenz der amZettel-App und des Dienstes bleibt unverändert.

Sicherheitslücken bitte vertraulich gemäß [SECURITY.md](SECURITY.md) melden.
