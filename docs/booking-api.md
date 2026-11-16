# Zeitbuchungs-API

## Freischaltung

Superadmins erstellen unter **Verwaltung > API-Schluessel** einen Schluessel.
Bei **Buchender Benutzer** stehen zwei Zuordnungen zur Auswahl:

- **Mit Benutzerkonto:** aktiver Benutzer mit Buchungsberechtigung.
- **Ohne Benutzerkonto:** Vor- und Nachname als Text. Es wird kein Login-Konto angelegt.

Jeder Schluessel gilt nur fuer die explizit ausgewaehlten Vertraege. Alle damit
erfassten Buchungen werden dem hinterlegten Benutzer bzw. Namen zugeordnet.
Der Name ohne Konto wird an jeder Buchung gespeichert und bleibt nach einem
Widerruf erhalten. Je Mitarbeiter einen eigenen Schluessel verwenden; Benutzer-ID
und Name koennen nicht im API-Body ueberschrieben werden. Bestehende Integrationen
verwenden unveraendert dieselben Endpunkte und Buchungsdaten.

Der geheime Schluessel wird nur nach der Erstellung angezeigt. In der Datenbank
liegt ausschliesslich sein SHA-256-Hash. Schluessel lassen sich widerrufen;
abgelaufene Schluessel und Schluessel deaktivierter Kontobenutzer werden abgewiesen.
Schluessel ohne Konto werden direkt ueber Ablaufdatum und Widerruf gesperrt.
Ein optionales Ablaufdatum gilt einschliesslich des angegebenen Tages in UTC.
Unter **Aktionen > Bearbeiten** koennen Superadmins die Bezeichnung, das Ablaufdatum
und die freigegebenen Vertraege nachtraeglich aendern. Der geheime Schluessel bleibt
gleich und muss im aufrufenden Programm nicht ersetzt werden. Neue Freigaben und
Entziehungen gelten ab dem Speichern fuer nachfolgende API-Anfragen, auch fuer
`/get-services`. Bereits erfasste Buchungen bleiben erhalten. Ohne freigegebene
Vertraege hat der Schluessel keinen Vertragszugriff; Freigaben koennen spaeter
wieder hinzugefuegt werden. Es gibt keine automatische Vertragsfreigabe.

Die Benutzerzuordnung bleibt unveraenderlich. Fuer einen anderen Benutzer einen
neuen Schluessel erstellen und den alten widerrufen. Widerrufene Schluessel
koennen weder bearbeitet noch reaktiviert werden. Diese Erweiterung der
Vertragsfreigaben benoetigt keine weitere Datenmodellmigration.

Alle API-Aufrufe verwenden JSON und den Header:

```http
X-API-Key: osp_DEIN_SCHLUESSEL
Content-Type: application/json
```

Produktiv ausschliesslich ueber HTTPS aufrufen. Schluessel weder in URLs noch
im Frontend einer oeffentlichen Website hinterlegen oder in Logs schreiben.
Browser-Login und Session-Cookies ersetzen den API-Key nicht. Umgekehrt erlaubt
der API-Key keinen Zugriff auf die Weboberflaeche, Rechnungen oder Vertragslisten.
Es gibt keinen API-Endpunkt zum Auflisten oder Suchen von Vertraegen.

## POST /get-services

Der vollstaendige Vertragsname entspricht dem Feld **Titel** des Vertrags.
Es erfolgt ein exakter, gross-/kleinschreibungsabhaengiger Vergleich, keine
Teilwortsuche. Alternativ kann statt `contract_name` die eindeutige
`contract_number` angegeben werden. Genau eines dieser Felder ist erforderlich.

```json
{
  "contract_name": "Muster GmbH - V-2026-001"
}
```

Beispielantwort (HTTP 200):

```json
{
  "contract_number": "V-2026-001",
  "workday_hours": 8.0,
  "services": [
    {
      "service_id": 42,
      "service_name": "Workshop Produktionsstart",
      "status": "active",
      "contracted_hours": 72.0,
      "booked_hours": 12.0,
      "remaining_hours": 60.0,
      "submitted_hours": 4.0,
      "approved_hours": 8.0,
      "billed_hours": 0.0
    }
  ]
}
```

Die Liste enthaelt die dem Vertrag zugeordneten Dienstleistungsinstanzen, auch
solche ohne Zeitbuchungen. `service_name` ist deren vertragsspezifischer Name,
nicht der Katalogname. Inaktive Positionen sind anhand von `status` erkennbar;
auf sie kann nicht neu gebucht werden. Pauschalen, Reisekosten und Lizenzen
werden nicht zurueckgegeben und sind ueber diese API nicht buchbar.

Alle Kontingente sind **Stunden**, unabhaengig von der UI-Einstellung:

- `contracted_hours`: vereinbartes Kontingent, `null` ohne vereinbarte Grenze.
- `booked_hours`: Summe aller bereits erfassten Stunden, ueber alle Benutzer
  und Buchungsstatus hinweg.
- `remaining_hours`: vereinbart minus gebucht; kann bei Ueberschreitung negativ
  sein, ohne Grenze `null`.
- `submitted_hours`, `approved_hours`, `billed_hours`: Aufteilung nach Status.
  Freigegebene Stunden in einem Rechnungsentwurf zaehlen noch als `approved`.
- `workday_hours`: aktuell konfigurierte Stunden je Arbeitstag. Fuer die Anzeige
  in Arbeitstagen das Stundenkontingent durch diesen Wert teilen.

Preise, Kontaktdaten, Mitarbeiterdaten und einzelne Buchungsbeschreibungen
werden nicht ausgegeben. Bei mehrdeutigen Vertragsnamen wird HTTP 409 geliefert;
in diesem Fall `contract_number` verwenden. Auch Vertragsnummern muessen fuer den
Schluessel eindeutig sein; bei Altdaten mit doppelten Nummern diese zuerst in der
Verwaltung bereinigen.

## POST /create-booking

```json
{
  "contract_number": "V-2026-001",
  "service_id": 42,
  "start_date": "2026-11-05",
  "end_date": "2026-11-06",
  "amount": 1.5,
  "unit": "day",
  "description": "Workshop und Nachbereitung"
}
```

Pflichtfelder: `contract_number`, genau eines von `service_id` / `service_name`,
`start_date`, `end_date`, `amount` und `unit`. `description` ist optional.
Statt `service_id` kann der exakte vertragsspezifische Name mitgegeben werden:

```json
{
  "contract_number": "V-2026-001",
  "service_name": "Workshop Produktionsstart",
  "start_date": "2026-11-05",
  "end_date": "2026-11-05",
  "amount": "0,75",
  "unit": "day"
}
```

`service_id` aus `/get-services` ist vorzuziehen: Sie bleibt beim Umbenennen
unveraendert und ist auch bei gleichnamigen Positionen eindeutig. Eine Position
eines anderen Vertrags wird immer abgewiesen.

`amount` ist der **Gesamtaufwand fuer den angegebenen Zeitraum**, nicht pro Tag.
Er muss positiv sein und darf hoechstens zwei Nachkommastellen haben.
JSON-Zahlen mit Dezimalpunkt oder Strings mit Dezimalkomma werden akzeptiert.
`unit` ist `hour` oder `day` und gilt unabhaengig von der UI-Einstellung.
Tage werden mit dem aktuell konfigurierten Wert in Stunden umgerechnet und auf
zwei Nachkommastellen gerundet. Bei acht Stunden pro Tag sind 1,5 Tage = 12 Stunden.
Eine spaetere Aenderung der Einstellung veraendert gespeicherte Stunden nicht.

Datum im Format `YYYY-MM-DD`, Enddatum nicht vor Startdatum. Vertrag und
Dienstleistung muessen aktiv sein. Die Buchung wird als **Eingereicht** angelegt;
die Freigabe und Abrechnung erfolgen weiterhin ueber den bestehenden Workflow.
Das vereinbarte Stundenkontingent ist wie in der Weboberflaeche keine harte
Buchungssperre.

Beispielantwort (HTTP 201):

```json
{
  "booking_id": 123,
  "contract_number": "V-2026-001",
  "service_id": 42,
  "start_date": "2026-11-05",
  "end_date": "2026-11-06",
  "hours": 12.0,
  "workday_hours": 8.0,
  "status": "submitted"
}
```

### Wiederholungen nach Verbindungsfehlern

Empfohlen ist ein zusaetzlicher Header je externer Buchung:

```http
Idempotency-Key: zeiterfassung-2026-000123
```

Maximal 128 Zeichen. Bei erneutem Aufruf mit demselben Schluessel und demselben
validierten Body wird die urspruengliche Antwort mit HTTP 200 zurueckgegeben,
ohne eine zweite Buchung anzulegen. Bei geaenderten Daten oder inzwischen
geloeschter Buchung folgt HTTP 409. Ohne diesen Header erzeugt jeder erfolgreiche
Aufruf eine neue Buchung. Die Wiederholungssperre gilt je API-Key; bei einem
Schluesselwechsel muss der Aufrufer bereits uebertragene Buchungen selbst
beruecksichtigen. Die Replay-Antwort ist die urspruengliche Eingangsquittung,
nicht der aktuelle Workflow-Status einer spaeter freigegebenen Buchung.

## Fehler

### HTTP 400: Invalid host header

Der Hostname aus der API-Basis-URL muss in `ALLOWED_HOSTS` der Vertragsverwaltung
stehen. Wenn ein anderes lokales Docker-Projekt beispielsweise
`http://host.docker.internal:8000` verwendet, `host.docker.internal` zur vorhandenen
Liste in `.env` hinzufuegen (ohne Schema und Port). Die Host-Pruefung nicht durch
eine Wildcard abschalten. Anschliessend den App-Container neu erstellen:

```sh
docker compose up -d --no-deps app
```

Ein blosses `docker compose restart` uebernimmt geaenderte Umgebungsvariablen nicht.
Diese Ablehnung erfolgt vor der API-Key-Pruefung; Schluessel und Vertragsfreigaben
muessen dafuer nicht neu angelegt werden.

### API-Antworten

Die folgenden API-Fehler enthalten ein JSON-Feld `detail`:

| Status | Bedeutung |
| --- | --- |
| 401 | API-Key fehlt, ist ungueltig, abgelaufen, widerrufen oder der Benutzer darf nicht buchen. |
| 404 | Vertrag nicht gefunden/nicht freigegeben oder Dienstleistung gehoert nicht zu diesem Vertrag. |
| 409 | Mehrdeutiger Name, inaktive Position/inaktiver Vertrag oder Idempotency-Konflikt. |
| 422 | Ungueltige oder fehlende Eingaben; unbekannte Felder werden ebenfalls abgewiesen. |

Nicht freigegebene und nicht existierende Vertraege erzeugen dieselbe Antwort.
Die API gibt keine Vorschlagsliste aehnlicher Vertragsnamen zurueck.

## Deployment und Datenmodell

### Herkunft von Zeitbuchungen

`service_time_entries.booking_source` speichert die Herkunft neuer Buchungen:
`api` fuer API-Aufrufe und `web` fuer das Erfassungsformular. Das Feld wird
ausschliesslich serverseitig gesetzt und beim Bearbeiten oder Freigeben nicht
veraendert. API-Buchungen tragen in der Oberflaeche eine Kennzeichnung **API**;
in der Stundenuebersicht kann unter **Filter > Herkunft** danach gefiltert werden.

Die automatische Migration ergaenzt das optionale Textfeld. Vorhandene Buchungen
mit API-Buchungsreferenz oder einem buchenden Namen ohne Benutzerkonto werden
als `api` nachgetragen. Historische Buchungen ohne eindeutigen Nachweis bleiben
ohne Herkunft (`NULL`, im Filter **Nicht erfasst**), auch wenn sie eventuell ueber
die API entstanden sind. Die Herkunft ist im Excel-Datenexport enthalten.

### Weitere Tabellen

Die API verwendet `api_keys`, `api_key_contracts` und `api_booking_requests`.
Fuer Buchende ohne Konto erhalten `api_keys` und `service_time_entries` jeweils
ein optionales Textfeld `booking_name`; `user_id` wird in diesen beiden Tabellen
optional. Bestehende Benutzerzuordnungen und Schluessel bleiben unveraendert.
Diese Postgres-Schemaanpassungen erfolgen gemeinsam innerhalb einer Transaktion
und werden bei einem Fehler zurueckgerollt.
`database.init_db()` legt fehlende Tabellen beim Start automatisch an und ist
auch ueber `scripts/migrate_database.py` aufrufbar. Vor dem Deployment wie
ueblich ein Backup erstellen und das Image neu bauen:

```sh
docker compose up -d --build
```

Ein reines `docker compose restart` aktualisiert den Anwendungscode nicht.
Bestehende Postgres-Backups enthalten die neuen Tabellen automatisch.
Der Excel-Export enthaelt die Verwaltungsmetadaten, aber keine Schluessel-Hashes.

Tests (isolierte In-Memory-Datenbank, keine produktiven Daten):

```sh
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
```

Fuer die Postgres-Variante `TEST_POSTGRES_URL` auf eine separate Testdatenbank
setzen und denselben Testbefehl ausfuehren. Jeder Test verwendet ein neu
angelegtes, zufaellig benanntes Schema, das anschliessend entfernt wird.
Dabei wird auch die gleichzeitige Uebertragung identischer Buchungen getestet.
Nicht gegen eine produktive Datenbank ausfuehren.
