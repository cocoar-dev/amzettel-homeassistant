# Fehlerhilfe

[English](../troubleshooting.md) · **Deutsch**

[Dokumentationsübersicht](README.md)

## amZettel erscheint nicht unter „Integration hinzufügen“

- Liegt `manifest.json` unter `/config/custom_components/amzettel/manifest.json`?
- Wurde der ganze Ordner einschließlich `translations` und `brand` kopiert?
- Wurde **HA vollständig neu gestartet**, nicht nur die Browserseite?
- Läuft mindestens HA 2026.10.0?
- Nach dem Neustart die Seite vollständig neu laden und das Log unter
  **Einstellungen → System → Protokolle** prüfen.

Ein fehlendes Logo allein ist kein Grund, die Integration zu entfernen oder neu anzumelden.
Die Warnung, dass eine Custom-Integration nicht von Home Assistant geprüft wurde, ist bei einer
Custom-Integration grundsätzlich erwartbar und kein Beleg für einen Verbindungsfehler.

## Der Code ist abgelaufen oder wurde abgelehnt

Den Einrichtungsdialog neu starten und den neuen Link verwenden.
Der Link und Code gehören genau zu diesem Versuch; ein älterer Browser-Tab hilft dann nicht.
Eine Ablehnung ist kein Fehler in HA: Ohne Freigabe wird keine Verbindung aufgebaut.

## Statt amZettel erscheint eine allgemeine Loginmaske

Verwende eine aktuelle Integrationsversion mit dem voreingestellten Server
`https://app.amzettel.at` und starte die Einrichtung neu. Öffne ausschließlich
den neuen Link aus HA: Er führt zur amZettel-Anmeldeseite auf
`amzettel.auth.cocoar.dev`. Verwende keinen alten Tab und keine selbst
zusammengesetzte Loginadresse; schreibe den Host eines Device-Links nicht um.
Die [Bildfolge der Anmeldung](installation.md#4-bei-amzettel-anmelden) zeigt
E-Mail-Code und Passkey, kein allgemeines Benutzername-/Passwortformular.

Die anschließende Gerätefreigabe **Home Assistant verbinden?** hat ein anderes
Layout. Das ist keine zweite Loginmaske.

## „amZettel hat die Anmeldung nicht angenommen“

Die Bestätigung beim Anmeldedienst war nicht gleichbedeutend mit einem akzeptierten API-Token.
Prüfe, dass `https://app.amzettel.at` eingestellt ist. Andere Server werden nicht unterstützt.
Wird die Anmeldung dort weiterhin abgelehnt, wende dich an den amZettel-Support.
Keine Tokens oder Passwörter aus dem Log herauskopieren und öffentlich posten.

## Nach Widerruf verlangt HA eine Neuanmeldung

Das ist beabsichtigt. Unter **Einstellungen → Geräte & Dienste** dem Hinweis zur
Neuanmeldung folgen und das **gleiche amZettel-Konto** verwenden.
Die bestehende Auswahl bleibt bei diesem Reauth-Weg erhalten.
Wenn die Verbindung nicht mehr erwünscht ist, stattdessen den Eintrag aus HA entfernen.

Eine zeitweise nicht erreichbare Gegenstelle sollte dagegen keine dauerhafte Neuanmeldung
erfordern. Bei wiederholtem Auftreten Diagnose und Log prüfen.

## Zettel oder Kalender fehlen

- Ist das Objekt beim verbundenen amZettel-Konto sichtbar oder mit ihm geteilt?
- Ist der passende Bereich beim Verbinden freigegeben worden?
- Ist das Objekt unter **Konfigurieren** ausgewählt?
- Ist **Neue automatisch übernehmen** ausgeschaltet?
- Wurde ein Termin in einen eigenen Kategorie-Kalender herausgelöst?

Der Hauptkalender zeigt herausgelöste Kategorien nicht zusätzlich an.
Essensplan-Einträge mit ausgeschalteter Kalenderanzeige fehlen in der Kalender-Entität bewusst.

## Lesen klappt, Schreiben nicht

Prüfe die drei unabhängigen Grenzen:

1. Das **bei der Anmeldung gewährte Schreibrecht**.
2. Die Freigabe in amZettel **Profil → Verbundene Systeme**.
3. **Tarif und Objektzugriff**: Kalender schreiben braucht Premium; geteilte oder eingefrorene
   Objekte können weitere Einschränkungen haben.

Eine Reparaturmeldung zu Premium oder fehlender Freigabe verschwindet nach der nächsten
erfolgreichen Änderung im betroffenen Bereich.
Neue Scopes lassen sich derzeit nicht in **Konfigurieren** nachfordern:
Siehe [Rechte ändern](installation.md#rechte-ändern), einschließlich der Hinweise
zur lokalen Synchronisationszuordnung.

## Assist findet die Liste nicht

- Zunächst mit einem **getippten** Satz und dem lokalen HA-Sprachassistenten auf Deutsch testen.
- Den tatsächlichen Anzeigenamen verwenden, beispielsweise
  „Setze Milch auf die Liste Wocheneinkauf“.
- Bei zwei Listen mit gleichem Namen eindeutig umbenennen oder in HA passende Aliasse vergeben.
  Ein mehrdeutiger Name kann abgelehnt werden.
- Falls HA danach fragt, die Entität für Assist freigeben.
- Ist ein Artikel in HAs eigener Einkaufsliste gelandet, wurde der Satz möglicherweise als
  allgemeiner Einkaufslistenbefehl verstanden. Den Zielnamen ausdrücklich nennen.

Alexa, Mikrofon und Spracherkennung sind keine Bestandteile dieser Integration.
Ein erfolgreicher getippter Assist-Test beweist noch nicht die Einrichtung einer Alexa-Verbindung.

## Termine sind um eine oder zwei Stunden verschoben

Die in **HA selbst** konfigurierte Zeitzone prüfen.
`TZ=Europe/Vienna` am Container ist nicht die HA-Konfiguration.

Mit `amzettel.add_event` kann `time_zone: Europe/Vienna` ausdrücklich gewählt werden.
Ein Zeitstempel mit Offset bezeichnet bereits einen Zeitpunkt, der beim Umrechnen erhalten bleibt.
Ein Datum ohne Uhrzeit bleibt ganztägig. Ein ganztägiges Ende ist bei den Kalenderaktionen der
**Tag danach**.
Der amZettel-Editor zeigt die gespeicherte Zone, UTC wird nicht still in Wien umgewandelt.

Beim Herbst-Zeitwechsel sind konkrete Zeitpunkte und der `on`/`off`-Zustand der HA-Kalenderentität
zu unterscheiden; siehe [bekannte Grenzen](actions.md#bekannte-grenzen).

## `sync_events` lehnt eine leere Liste ab

Das schützt vor Löschungen bei einer vorübergehend ausgefallenen Quelle.
Zuerst deren Abruf prüfen. `allow_empty: true` nur bewusst einsetzen, wenn der vollständige Zeitraum
tatsächlich leer sein soll. Nicht als allgemeine Fehlerbehandlung einschalten.

Fehlen beim späteren Abgleich Löschungen alter Termine, kann die lokale Herkunftszuordnung verloren
gegangen sein, beispielsweise durch Entfernen und erneutes Hinzufügen des Integrationseintrags.
Bestehende Termine werden dann nicht einfach als eigene beansprucht.

## „Gekauftes kann nicht gelöscht werden“

Einkaufszettel haben eine Kaufhistorie. Gekaufte Einträge werden durch „Erledigte entfernen“ nicht
aus dieser Historie gelöscht.
Mit „nicht erledigt“ den Kauf zurücknehmen; danach kann der offene Eintrag bei Bedarf gelöscht werden.
Nicht mit „Einkauf abschließen“ in der amZettel-App verwechseln.

## Änderungen kommen nicht sofort an

- Verbindung von HA zum amZettel-Server prüfen.
- HA verwendet `/api/integrations/v1/changes` per SSE.
  Der Proxy darf die Antwort nicht puffern. HA holt nach einer Unterbrechung Änderungen nach
  und lädt bei abgelaufenem Cursor oder geänderten Zugriffsrechten einen vollständigen Snapshot.
  Wenn der Server dabei `429` meldet, wartet HA vor dem nächsten Versuch. Bis ein vollständiger
  Abgleich die Rechte bestätigt, bleiben die betroffenen Daten ausgeblendet.
  Ein Schreibkonflikt bedeutet, dass sich der Stand inzwischen geändert hat: zuerst den aktuellen
  Stand prüfen, statt den Schreibzugriff blind zu wiederholen.

`calendar` mit Zustand `off` bedeutet nur, dass aktuell kein Termin läuft.
`unavailable` weist dagegen auf fehlende Daten beziehungsweise Verbindung hin.

## Server wird nicht unterstützt

Verwende `https://app.amzettel.at`. Falls HA dort meldet, dass REST/SSE v1 nicht
verfügbar ist, wende dich an den amZettel-Support. Eine Neuanmeldung behebt das nicht.

## Diagnose und Support

Am Integrationseintrag unter **Einstellungen → Geräte & Dienste** die Diagnose herunterladen,
sofern der Eintrag geladen ist. Zusätzlich das HA-Protokoll nach `amzettel` durchsuchen.

Die Integration bereinigt ihre Diagnose um Tokens und persönliche Inhalte.
Prüfe die Datei vor dem Weitergeben trotzdem selbst.
**Niemals** die Dateien `.storage/core.config_entries`, Zugangscodes, Tokens, Passwörter
oder den vollständigen HA-Konfigurationsordner öffentlich anhängen.

Für einen hilfreichen Fehlerbericht:

- HA- und Integrationsversion,
- verwendete Serveradresse (unterstützt wird nur `https://app.amzettel.at`),
- betroffene Aktion und genaue Fehlermeldung,
- erwartetes und beobachtetes Verhalten,
- bereinigte Diagnose und ein kurzes relevantes Logstück.
