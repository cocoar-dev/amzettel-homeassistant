# Installation und Verbindung

[English](../installation.md) · **Deutsch**

[Dokumentationsübersicht](README.md)

**Ablauf:** [Voraussetzungen](#voraussetzungen) → [Installation](#manuell-installieren) →
[Mit amZettel verbinden](#verbindung-einrichten) → [Funktion prüfen](#nach-dem-verbinden-praktisch-prüfen).

## Voraussetzungen

- **Home Assistant ab 2026.10.0**, wie in `hacs.json` angegeben.
- Ein **amZettel-Konto**. Zum Lesen und zum Bearbeiten von Zetteln reicht der kostenlose Tarif;
  zum Ändern von Kalenderterminen ist Premium nötig.
- Home Assistant und dein Browser erreichen den amZettel-Server und dessen Anmeldedienst.
- Administratorrechte in Home Assistant, um eine Integration zu installieren.
- Für die Installation über HACS/Marketplace: Zugriff auf dessen Repository-Verwaltung.

Die Integration ist eine **Custom-Integration**, kein Add-on. Sie läuft innerhalb von Home Assistant;
ein zusätzlicher amZettel-Container ist nicht nötig. Für Home Assistant Container ist der manuelle
Installationsweg genauso möglich wie für Home Assistant OS.

## Über HACS installieren

Für HA 2026.10 mit eingerichteten HACS:

1. **HACS** öffnen.
2. Im Menü **⋮ → Benutzerdefinierte Repositories / Custom repositories** die Adresse
   `https://github.com/cocoar-dev/amzettel-homeassistant` eintragen.
3. Als Typ **Integration** wählen und hinzufügen.
4. **amZettel** suchen, die Version auswählen und herunterladen.
5. **Home Assistant neu starten**, nicht nur die Seite im Browser.
6. Anschließend [die Verbindung einrichten](#verbindung-einrichten).

Ab HA 2026.11 ist HACS als **Marketplace** eingebaut. Die Bezeichnungen und der Zugang zum
Repository-Menü können deshalb von der oben beschriebenen HACS-Oberfläche abweichen.
Folge in diesem Fall den [offiziellen Marketplace-/HACS-Hinweisen][hacs].
Die Integration muss nach der Installation weiterhin in **Geräte & Dienste** eingerichtet werden.

Ein benutzerdefiniertes Repository ist nicht dasselbe wie ein Eintrag im offiziellen Katalog.
Dieser Installationsweg setzt keine Aufnahme in den offiziellen Katalog voraus.

## Manuell installieren

1. Ein Home-Assistant-Backup erstellen.
2. Den Ordner **`custom_components/amzettel`** aus diesem Repository nehmen.
3. Den **gesamten Ordner** in das Konfigurationsverzeichnis von Home Assistant kopieren:
   unter `custom_components/amzettel`.
4. **Home Assistant neu starten.**
5. [Die Verbindung einrichten](#verbindung-einrichten).

Das Konfigurationsverzeichnis ist der Ordner, in dem `configuration.yaml` liegt.
Bei HA OS ist das typischerweise `/config`. In einem Container entspricht `/config`
dem vom Betreiber eingebundenen Host-Verzeichnis.

Die Datei liegt danach beispielsweise unter:

```text
/config/custom_components/amzettel/manifest.json
```

**Nicht** unter `/config/amzettel` und auch nicht unter
`/config/custom_components/amzettel/custom_components/amzettel`.
Kopiere weder `.local` noch alte `.venv`-Ordner, Tests oder Git-Dateien nach `/config`.
Die Unterordner `brand` und `translations` gehören dagegen dazu.

Für eine **Erstinstallation mit Dateizugriff auf den HA-Host** kannst du aus dem entpackten
Quell-/Installationsordner beispielsweise so kopieren. Ersetze den Beispielpfad durch deinen
tatsächlichen Konfigurationsordner:

```sh
HA_CONFIG="/pfad/zur/ha-konfiguration"
mkdir -p "$HA_CONFIG/custom_components"
cp -R custom_components/amzettel "$HA_CONFIG/custom_components/"
```

Über einen Dateimanager oder eine bestehende Freigabe entsprechend den ganzen Ordner kopieren.
Bei einem Update stattdessen den Abschnitt [Aktualisieren](#aktualisieren) beachten.

### Bei einem bestehenden HA-Container

Kopiere den Integrationsordner in den Konfigurationsordner, den dein vorhandener Container
bereits eingebunden hat. Alternativ kann ein zusätzlicher Bind-Mount auf
`/config/custom_components/amzettel` verwendet werden.

Die Integration braucht keine Änderungen an `configuration.yaml`. Eine Neuinstallation von HA,
ein Zurücksetzen des Konfigurationsordners oder ein Löschen bestehender Container ist nicht nötig.

## Verbindung einrichten

Die folgenden **echten Aufnahmen** entstanden mit HA 2026.10.4 und einem eigens
angelegten Demokonto auf `app.amzettel.at`. Die Beispielobjekte heißen
`WG-Einkauf`, `WG-Kalender` und `Unser Wochenplan`.
Konto-Adressen, Zugangscodes und vollständige Device-Links sind geschwärzt.

### 1. Integration suchen

**Einstellungen → Geräte & Dienste → Integration hinzufügen** öffnen, nach **amZettel**
suchen und den Treffer auswählen. Fehlt er, zuerst den [Installationspfad](#manuell-installieren)
und den vollständigen HA-Neustart prüfen.

![amZettel in der Liste der Integrationen finden](screenshots/add-integration.png)

### 2. Server und Rechte wählen

Lass den offiziellen Server `https://app.amzettel.at` voreingestellt und wähle die Rechte,
die Home Assistant braucht. HA nutzt ausschließlich **REST/SSE v1**.
Für das spätere Verschieben von Terminen muss **Kalender ändern** aktiviert sein;
zusätzlich gelten Premium, Objektmitgliedschaft und die in amZettel freigegebenen Rechte.
Die [Rechteübersicht](configuration.md#welche-rechte-braucht-ha) erklärt den Umfang.

![Server und angeforderte Rechte im deutschen Einrichtungsdialog](screenshots/connect.png)

Mit **OK** fortfahren. Noch wurde keine bestehende Anmeldung verändert.

### 3. Den Device-Link öffnen

HA zeigt einen **Device-Code und einen Link**. Lass dieses HA-Fenster geöffnet und öffne
den Link in einem weiteren Tab oder auf deinem Telefon. Der Link enthält den Device-Code
bereits. Falls der Anmeldedienst stattdessen danach fragt, übertrage genau den von HA gezeigten Code.

![HA wartet auf die Bestätigung; der Device-Code ist geschwärzt](screenshots/device-code.png)

> [!IMPORTANT]
> **Device-Code und E-Mail-Anmeldecode sind zwei verschiedene Codes.** Der Device-Code ordnet
> die neue HA-Verbindung zu. Der gegebenenfalls anschließend per E-Mail angeforderte Code
> meldet dich an deinem Konto an. Trage nicht den Device-Code in das E-Mail-Codefeld ein.

Der Device-Code ist zeitlich begrenzt. Läuft er ab, den Vorgang in HA neu starten und den
neuen Link verwenden. Codes und vollständige Device-Links nicht an andere weitergeben.

### 4. Bei amZettel anmelden

Der Link führt zur **amZettel-Anmeldeseite**, bereitgestellt von Modgud auf
`amzettel.auth.cocoar.dev`.
Du gibst deinen E-Mail-Anmeldecode **dort** ein oder verwendest einen Passkey,
niemals im HA-Integrationsdialog.
Eine bestehende Modgud-Sitzung kann diesen Anmeldeschritt überspringen.

![amZettel-Anmeldeseite auf Deutsch](screenshots/amzettel-login.png)

Die amZettel-Seite zeigt direkt **Mit Code anmelden**. Gib für den gezeigten
E-Mail-Weg deine eigene Kontoadresse ein und drücke **Code senden**.
Alternativ steht **Mit Passkey anmelden** zur Verfügung.

![amZettel-E-Mail-Anmeldung mit geschwärzter Konto-Adresse](screenshots/amzettel-email.png)

Den erhaltenen **E-Mail-Anmeldecode** in die sechs Felder eintragen und **Anmelden** wählen.
Die Aufnahme zeigt bewusst leere Codefelder. Verwende deine eigenen Zugangsdaten,
nicht das Beispielkonto aus den Bildern.

![Leere Felder für den E-Mail-Anmeldecode, nicht für den HA-Device-Code](screenshots/amzettel-code.png)

### 5. Zugriff prüfen und erlauben

Nach der Anmeldung folgt die **Gerätefreigabe** mit **Home Assistant verbinden?**
und den angefragten Rechten. Diese Seite sieht anders aus als die amZettel-Anmeldemaske;
sie fragt keine weiteren Zugangsdaten ab.
Prüfe, ob du diese Verbindung gerade selbst gestartet hast und ob die Rechte zu deiner
Auswahl in HA passen. **Erlauben** bestätigt die Verbindung; **Ablehnen** gibt keinen Zugriff frei.

![Gerätefreigabe mit den tatsächlich angefragten Bereichen](screenshots/device-consent.png)

`openid` ist die OAuth-Anmeldung. `offline_access` erlaubt HA, Zugangstokens selbst
zu erneuern; es verlangt keine dauerhaft geöffnete Browserseite. Diese beiden Rechte
werden im gezeigten Consent auf Englisch beschrieben. Die übrigen Rechte betreffen
die ausgewählten amZettel-Bereiche.
Eine Bestätigung hebt weder Tarifgrenzen noch Objektberechtigungen auf.

![Bestätigung der Gerätefreigabe](screenshots/device-approved.png)

Nach **Gerät verbunden** zu HA zurückkehren. Kein Token muss kopiert werden.
HA braucht weder API-Schlüssel noch OAuth-Client-Secret oder eine öffentliche Internetadresse.
Die Verbindung erscheint in amZettel unter **Profil → Verbundene Systeme**.

### 6. Sichtbare Objekte auswählen

HA fährt automatisch mit **Was soll erscheinen?** fort. Wähle die gewünschten Zettel,
Kalender und Essenspläne. Optional einzelne benannte Kategorien als eigene Kalender auswählen.
**Neue Zettel/Kalender/Essenspläne automatisch übernehmen** bestimmt, ob spätere neue Objekte
von selbst hinzukommen. Die Auswahl ändert keine Daten in amZettel.

![Nur die drei eigens angelegten Dokumentationsobjekte sind ausgewählt](screenshots/select.png)

Mit **OK** speichern. Die Einrichtung bestätigt den Erfolg; die Entitäten erscheinen ohne
weiteren Neustart.

![Erfolgreicher Abschluss der Einrichtung](screenshots/connected.png)

### 7. Konto und Verbindung kontrollieren

Unter **Einstellungen → Geräte & Dienste → amZettel** stehen nun das angemeldete Konto
und die ausgewählten Entitäten. Ein selbst vergebener Eintragstitel
bleibt erhalten; **Konfigurieren** zeigt das Konto trotzdem an.

![Fertige Prod-Verbindung mit geschwärztem Kontotitel und drei Entitäten](screenshots/integration.png)

Es wird bewusst kein separates amZettel-Gerät angelegt. Ein Kalenderzustand `off` bedeutet
„gerade kein Termin aktiv“, nicht „Verbindung kaputt“.

## Nach dem Verbinden praktisch prüfen

1. Unter **To-do-Listen** einen ausgewählten Zettel öffnen und einen eigenen Testeintrag ergänzen.
2. Unter **Kalender** die gewünschten amZettel-Kalender einblenden.
3. In der amZettel-App mit **demselben Konto und derselben Umgebung** einen eigenen Testtermin
   anlegen. Er sollte in HA ohne manuelles Neuladen erscheinen.
4. Den Termin in HA öffnen, **Termin bearbeiten** wählen und Beginn/Ende ändern.
   Anschließend die gespeicherte Zeit in amZettel kontrollieren.
5. Eine geplante Mahlzeit öffnen: Ohne gespeicherte Uhrzeit erscheint sie ganztägig,
   wie in den aktuellen Prod-Aufnahmen. Falls Mahlzeiten-Uhrzeiten in amZettel verfügbar sind,
   muss eine mit Uhrzeit gespeicherte Mahlzeit zeitgebunden erscheinen.

Den bebilderten Ablauf zum [Verschieben von Terminen](configuration.md#termine-verschieben-und-abgleichen)
und den [Essensplan](configuration.md#essensplan) findest du in der Bedienung.
Weitere Beispiele stehen unter [Automationen](examples.md) und [Fehlerhilfe](troubleshooting.md).

## Aktualisieren

- **HACS/Marketplace:** angebotene Aktualisierung installieren und anschließend HA neu starten.
- **Manuell:** Backup erstellen, den Integrationsordner durch die neue Version ersetzen und HA
  neu starten. Nicht den restlichen HA-Konfigurationsordner ersetzen.
- Auswahl und Anmeldung bleiben bei normalen Updates erhalten. Fordert HA eine Neuanmeldung,
  folge dem angezeigten Dialog.
- Keine Updates gleichzeitig über HACS und manuelles Kopieren verwalten.

## Rechte ändern

**Konfigurieren** ändert die sichtbaren Objekte, nicht die bei der Anmeldung angeforderten Rechte.
Neue Bereiche oder zusätzliche Schreibrechte benötigen derzeit ein Entfernen und erneutes
Hinzufügen der Integration.

Vorher die Auswahl, Kategorie-Kalender und Entitäts-IDs notieren.
Prüfe anschließend die Ziele in Dashboards, Skripten, Automationen und Node-RED:
Beim Entfernen und erneuten Hinzufügen können Entitätszuordnungen verloren gehen.
Eine bloße Neuanmeldung nach einem Widerruf ist dagegen kein Entfernen der Integration.

> [!WARNING]
> Wer `sync_events` verwendet, sollte die Integration nicht als normalen Update-Schritt entfernen:
> Beim Entfernen geht deren lokale Herkunftszuordnung verloren. Vorhandene Termine bleiben in
> amZettel, ein späterer Abgleich kann alte, nicht mehr gelieferte Termine dann nicht mehr als seine
> eigenen erkennen. Siehe [Synchronisation](actions.md#amzettelsync_events).

## Verbindung sperren oder entfernen

- **Zugriff sofort entziehen:** In amZettel **Profil → Verbundene Systeme** die Verbindung sperren
  bzw. widerrufen. HA verlangt daraufhin eine Neuanmeldung. Nicht erneut bestätigen, wenn du die
  Verbindung nicht mehr möchtest.
- **Aus HA entfernen:** Bei **Einstellungen → Geräte & Dienste → amZettel** den betreffenden
  Eintrag löschen. Die Integration versucht dabei, ihren Refresh-Token zu widerrufen.
- **Vollständig deinstallieren:** Zuerst den Eintrag entfernen, danach über HACS deinstallieren
  oder den Ordner `/config/custom_components/amzettel` löschen und HA neu starten.

Zettel, Termine und Essenspläne in amZettel werden dadurch **nicht** gelöscht. Automationen mit
entfernten HA-Entitäten musst du selbst anpassen.

## Offizielle Hinweise

- [Benutzerdefinierte HACS-Repositories][custom]
- [Marketplace und HACS][hacs]
- [Dateistruktur von HA-Integrationen][structure]

[custom]: https://www.hacs.xyz/docs/faq/custom_repositories/
[hacs]: https://www.hacs.xyz/
[structure]: https://developers.home-assistant.io/docs/creating_integration_file_structure/
