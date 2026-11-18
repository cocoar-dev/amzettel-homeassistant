# Release-Notizen

[English](../release-notes.md) · **Deutsch**

[Dokumentationsübersicht](README.md)

## 0.1.0

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

Die [Installationsanleitung](installation.md) beschreibt das manuelle Kopieren und die
vollständige Anmeldung. Die [Bedienung](configuration.md) zeigt das Verschieben eines Termins,
geteilte Zettel, den Free-/Premium-Unterschied und ganztägige Demomahlzeiten.
