# Release-Notizen

[English](../release-notes.md) · **Deutsch**

[Dokumentationsübersicht](README.md)

Die Release-Notizen sind nach Version geordnet; der neueste Release steht oben.
Jeder Eintrag beschreibt sichtbare Funktionen, Fehlerkorrekturen, inkompatible Änderungen
und bekannte Grenzen. Die Versionierung folgt Semantic Versioning: Patch-Versionen
beheben Fehler, Minor-Versionen ergänzen kompatible Funktionen und Major-Versionen
kennzeichnen inkompatible Änderungen.

## 1.0.0

Erster stabiler Release der amZettel-Integration für Home Assistant.

### Funktionsumfang

- Zettel als HA-To-do-Entitäten: lesen, ergänzen, bearbeiten, abhaken und löschen.
- Kalender lesen und mit Premium Termine anlegen, bearbeiten und löschen;
  ausgewählte Kategorien können eigene Kalender erhalten.
- Essenspläne als nur lesende Kalender und strukturierte Abfragen.
  Gespeicherte Mahlzeitenzeiten samt Zeitzone bleiben maßgeblich, auch nach
  Änderungen an Vorlagen oder der Plan-Zone.
- Realtime-Aktualisierung, eigene HA-Aktionen und Ereignisse für Automationen.
- Device-Anmeldung über die amZettel-gebrandete Modgud-Seite ohne Client-Secret in HA;
  auswählbare Rechte und Objekte.
- Erkennbares angemeldetes Konto, unterscheidbare geteilte Objekte und
  geschwärzte Kontobezeichnung in Diagnosen.
- Deutsche und englische Anleitungen mit echten Installations-, amZettel-Anmelde-,
  Gerätefreigabe- und Kalenderaufnahmen mit eigens angelegten Demokonten auf Produktion.
- Installation über Marketplace ab HA 2026.11, HACS bei HA 2026.10 oder manuelles Kopieren.
  Vorhandene HACS-Installationen wechseln mit ihrem Custom Repository zu Marketplace.

### Bekannte Grenzen

- Essenspläne bleiben nur lesend, auch mit Premium.
- Ein gespeicherter Mahlzeitenzeitpunkt hat keine erfundene Dauer; HA kann etwa
  **18:00–18:00** anzeigen. Alte benannte Mahlzeiten ohne Snapshot bleiben ganztägig.
- In der wiederholten Herbststunde kann HAs Kalender-`on`/`off`-Anzeige wegen
  lokaler Zeitvergleiche im HA-Framework abweichen. Die abgefragten Zeitpunkte sind korrekt;
  ein erwarteter Regressionstest dokumentiert diese Grenze.
- Die englische Modgud-Oberfläche zeigt amZettels Bereichsbeschreibungen derzeit
  noch auf Deutsch. Die englische Anleitung erklärt die tatsächlich sichtbaren Begriffe.
- Keine automatische Alexa-Anbindung. Sprachbedienung erfolgt über passend eingerichtetes HA Assist.

### Installation und Bedienung

Die [Installationsanleitung](installation.md) beschreibt Marketplace, HACS, manuelles Kopieren und die
vollständige Anmeldung. Die [Bedienung](configuration.md) zeigt das Verschieben eines Termins,
geteilte Zettel, den Free-/Premium-Unterschied und ganztägige Demomahlzeiten.
