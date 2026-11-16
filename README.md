# Vertragsverwaltung

FastAPI-Webanwendung fuer Vertragsverwaltung mit Unternehmen, Vertraegen, Lizenzen,
Dienstleistungen, Stundenbuchungen, dynamischen Charakteristiken, Benutzerverwaltung
und rudimentaerem Rollen-/Permission-Management.

## Funktionen

- Unternehmen mit dynamischen Charakteristiken
- Vertraege je Unternehmen mit Zahlungsmodalitaeten und Vertrags-PDF-Viewer
- zentraler Katalog fuer Lizenz- und Dienstleistungsarten mit DATEV-Konten
- Lizenzen je Vertrag aus dem Lizenzkatalog mit zeitlicher Lizenzabrechnung pro rata
- Dienstleistungen je Vertrag aus dem Dienstleistungskatalog und Benutzer-Stundenbuchungen
- Freigabe von Dienstleistungsstunden
- Rechnungen als reine Datenaggregation fuer DATEV
- Kombinierte Abrechnung von Lizenzen und Dienstleistungen
- Benutzer, Rollen und Permissions
- Analytics-Seite mit Chart.js-Zeitreihen fuer Umsatz, Lizenzbestand, Rechnungen und Stunden
- Postgres-Datenhaltung per SQLModel
- REST-API fuer Zeitbuchungen mit vertragsbezogenen API-Schluesseln

## Zeitbuchungs-API

Superadmins verwalten Schluessel unter **Verwaltung > API-Schluessel**.
Der buchende Benutzer kann ein vorhandenes Konto oder ein frei eingegebener Name
ohne Login-Konto sein. Vertragsfreigaben und Widerruf gelten fuer beide Varianten.
`POST /create-booking` erfasst Zeiten, `POST /get-services` liefert
Dienstleistungsposten und Stundenkontingente fuer einen bekannten Vertrag.
Es gibt keine Vertragslisten-Abfrage per API-Key.

Request-Beispiele, Authentifizierung und Deployment-Hinweise stehen in
[docs/booking-api.md](docs/booking-api.md).

## Start

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
uvicorn app.main:app --reload
```

Danach ist die Anwendung unter `http://127.0.0.1:8000` erreichbar.

Die wichtigsten Einstellungen liegen in `.env`. Fuer lokale Starts nutzt
`DATABASE_URL` standardmaessig
`postgresql+psycopg://user:password@localhost:5432/contracts`.

Wenn die Datenbank `contracts` noch nicht existiert, versucht die App sie ueber
die Maintenance-Datenbank `postgres` anzulegen. Der Datenbanknutzer braucht
dafuer entsprechende Rechte. Uploads liegen standardmaessig unter `data/`.

## Docker Compose

```powershell
docker compose up -d --build
```

Compose startet die Webanwendung, einen Caddy-Reverse-Proxy und einen
Postgres-16-Container. Nach aussen wird nur der Reverse Proxy veroeffentlicht.
In der mitgelieferten `.env` steht `PROXY_BIND_IP=0.0.0.0` und
`PROXY_HTTP_PORT=8000`, der Webserver ist damit auf Port `8000` erreichbar,
sofern Firewall/Netzwerk das zulassen. Die FastAPI-App ist nur noch intern im
Docker-Netz unter `app:8000` erreichbar. Der Compose-Postgres wird bewusst nur
an `127.0.0.1:5433` gebunden, damit die Datenbank nicht oeffentlich exponiert
wird.

Der App-Container laeuft als non-root User, mit `no-new-privileges`, ohne
Linux-Capabilities und mit read-only Root-Dateisystem. Caddy laeuft ebenfalls
mit read-only Root-Dateisystem; seine offiziellen Binary-Capabilities bleiben
aktiv, damit der Proxy sauber starten kann. Schreibbar bleiben nur die
benoetigten Daten-Volumes und ein temporaeres `/tmp`. Die Anwendung setzt
Security Header inklusive CSP. Caddy uebernimmt Kompression, Request-Body-Limits
und das Forwarding an die App.

Die Proxy-Einstellungen liegen in `.env`:

- `PUBLIC_SITE_ADDRESS=:8000` fuer den aktuellen HTTP-Betrieb auf Port 8000
- `PROXY_HTTP_PORT=8000` fuer den externen Host-Port
- `PROXY_CONTAINER_PORT=8000` fuer den Port, auf dem Caddy im Container lauscht
- `MAX_UPLOAD_SIZE=50MB` fuer Upload-Limits am Proxy

Wenn der Dienst am Host auf Port 80 erreichbar sein soll, reicht fuer den
aktuellen HTTP-Betrieb `PROXY_HTTP_PORT=80`. `PUBLIC_SITE_ADDRESS` und
`PROXY_CONTAINER_PORT` bleiben dabei auf `:8000` bzw. `8000`, weil Caddy intern
weiter auf Port 8000 lauscht.

Fuer produktives HTTPS mit eigener Domain muss Caddy auf die Domain und die
passenden Container-/Host-Ports 80/443 umgestellt werden. Dann sollten auch
`SECURE_COOKIES=1` und `FORCE_HTTPS=1` gesetzt werden.

Vor einem echten produktiven Start sollten mindestens diese Werte angepasst
werden:

- `APP_ENV=production`
- `SESSION_SECRET`
- `ADMIN_PASSWORD`
- `POSTGRES_PASSWORD` und passend dazu `DOCKER_DATABASE_URL`
- `ALLOWED_HOSTS` auf konkrete Hostnamen, keine Wildcard
- `SECURE_COOKIES=1` und `FORCE_HTTPS=1`, wenn TLS am Reverse Proxy terminiert wird

Wenn `APP_ENV=production` gesetzt ist, verweigert die Anwendung den Start mit
bekannten Default-Secrets oder `ALLOWED_HOSTS=*`.

## Automatische Backups

Der Compose-Service `db-backup` sichert alle 24 Stunden die PostgreSQL-Datenbank
sowie das gesamte `app_data`-Volume, einschliesslich Vertrags-PDFs,
Rechnungs-PDFs und Unternehmenslogos. Die erste vollstaendige Sicherung wird
sofort angelegt. Danach zaehlt der Zeitpunkt der letzten erfolgreichen Sicherung;
ein Container-Neustart setzt den Zeitplan nicht zurueck. Es ist kein fester
Uhrzeit-Termin: 86400 Sekunden entsprechen einem 24-Stunden-Intervall.

Unter **Verwaltung > Backups** (nur SuperAdmins) stehen Sicherungsliste,
Download von Datenbank/Dateien/Pruefsummen, Dienststatus und **Jetzt sichern**
zur Verfuegung. Automatik, Intervall, Aufbewahrung, Wiederholungsabstand und
Zeitlimit sind dort bearbeitbar. Der Worker uebernimmt Aenderungen innerhalb
von etwa 10 Sekunden, ohne Neustart. Ein bereits laufender Sicherungslauf
beendet sich noch mit seinen bisherigen Einstellungen. Eine manuelle Sicherung
ist auch bei pausierter Automatik moeglich und verschiebt den naechsten
automatischen Termin entsprechend dem Intervall.

Gespeicherte UI-Einstellungen haben Vorrang vor den Startwerten in `.env`:

- `BACKUP_ENABLED=1`: Automatik aktiv
- `BACKUP_INTERVAL_SECONDS=86400`: einmal pro Tag
- `BACKUP_RETENTION_DAYS=14`: automatische Sicherungen 14 Tage aufbewahren
- `BACKUP_RETRY_SECONDS=300`: bei Fehlern nach 5 Minuten erneut versuchen
- `BACKUP_TIMEOUT_SECONDS=3600`: Zeitlimit je Sicherungs-/Pruefschritt

Die Sicherungen liegen im Docker-Volume `db_backups`, im Container unter
`/backups/snapshot-<UTC-Zeitstempel>-<Kennung>/`. Jeder Sicherungssatz enthaelt
`database.dump`, `uploads.tar.gz`, `SHA256SUMS` und Metadaten. Der Dienst prueft
Dump und Archiv vor der Freigabe. Fehlerhafte Laeufe ersetzen keine erfolgreiche
Sicherung. Nur nach erfolgreicher Sicherung werden automatisch erzeugte Saetze
entfernt, die aelter als 14 Tage sind. Vorhandene einzelne Dumps und manuelle
Dateien bleiben unangetastet. Gleichzeitige Sicherungslaeufe sind gesperrt.

Aktivieren/aktualisieren und Status pruefen:

```powershell
docker compose up -d --build --no-deps app db-backup
docker compose ps db-backup
docker compose logs --tail=30 db-backup
```

Der Healthcheck meldet fehlende bzw. ueberfaellige Sicherungen als `unhealthy`
(mit Zeitpuffer fuer einen laufenden Sicherungslauf und Wiederholungen).
Docker meldet dies lokal; externe Benachrichtigungen sind nicht eingerichtet.
Der Docker-Host muss laufen, damit der Dienst sichern kann. Nach einem Ausfall
wird eine faellige Sicherung nachgeholt.

Die Verwaltung nutzt vorhandene `app_settings`-Eintraege mit Praefix `backup_`;
eine Schema-Migration ist nicht erforderlich. Die Webanwendung hat ausschliesslich
Lesezugriff auf das Backup-Volume. Der separate Worker erhaelt die zusaetzliche
Gruppe 1000 und gibt nur fertige Sicherungen fuer diese App-Gruppe lesbar frei
(Verzeichnisse 0750, Dateien 0640). Ein Docker-Socket wird nicht eingebunden.
Downloads und manuelle Anforderungen sind SuperAdmin-geschuetzt; Formulare
pruefen einen CSRF-Token. Der Worker meldet seinen Status regelmaessig, auch
waehrend eines laufenden Backups. Bei pausierter Automatik prueft der Healthcheck
die Erreichbarkeit des Workers statt das Alter der letzten Sicherung.

Die angezeigte Pruefung bezieht sich auf die Erstellung, nicht auf eine erneute
Pruefsummenpruefung bei jedem Seitenaufruf. Einzelne alte `.dump`-Dateien ausserhalb
der verwalteten Snapshot-Verzeichnisse bleiben erhalten und erscheinen nicht in
der Liste. Die Wiederherstellung bleibt bewusst ein separater Wartungsvorgang;
die laufende Anwendung kann ihren Datenbestand nicht per Klick ueberschreiben.

Zusaetzlich sofort sichern bzw. die letzte Sicherung erneut pruefen:

```powershell
docker compose exec db-backup bash /opt/backup.sh run
docker compose exec db-backup bash /opt/backup.sh verify
```

Sicherungen auf den Host kopieren:

```powershell
docker compose cp db-backup:/backups ./backups-export
```

Wichtig: Ein Volume auf demselben Rechner schuetzt nicht vor Verlust des Rechners
oder Datentraegers. Kopien separat und zugriffsgeschuetzt aufbewahren. `.env`,
Quellcode und Proxy-Zertifikate sind nicht Teil dieser Datensicherung; insbesondere
die `.env` mit den benoetigten Schluesseln separat sicher verwahren. `docker compose
down -v` entfernt auch das Backup-Volume. `app_data` waehrend der Sicherung nicht
manuell veraendern; Uploads der Anwendung nutzen unveraenderliche Dateinamen.

### Wiederherstellung

Vorher eine aktuelle Sicherung extern kopieren. `<snapshot-name>` durch den
gewuenschten Verzeichnisnamen ersetzen. Die Wiederherstellung ueberschreibt den
aktuellen Datenbestand; Anwendung und Backup-Dienst waehrenddessen stoppen.
Die folgenden Befehle sind fuer PowerShell:

```powershell
docker compose exec db-backup bash /opt/backup.sh verify <snapshot-name>
docker compose cp db-backup:/backups/<snapshot-name> ./restore-snapshot
docker compose stop app db-backup
docker compose run --rm --no-deps --entrypoint bash db-backup -c 'pg_restore -h postgres -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --single-transaction /backups/<snapshot-name>/database.dump'
docker compose run --rm --no-deps --user root --cap-add CHOWN --cap-add DAC_OVERRIDE --volume "${PWD}/restore-snapshot:/restore:ro" --entrypoint python app -c "import os, pathlib, pwd, tarfile; tarfile.open('/restore/uploads.tar.gz').extractall('/app/data', filter='data'); owner = pwd.getpwnam('appuser'); [os.chown(p, owner.pw_uid, owner.pw_gid, follow_symlinks=False) for p in [pathlib.Path('/app/data'), *pathlib.Path('/app/data').rglob('*')]]"
docker compose up -d app db-backup
```

Die Datenbank wird transaktional wiederhergestellt. Uploads werden separat
eingespielt; Anwendung erst nach erfolgreichem Abschluss beider Schritte starten.
Der einmalige Upload-Restore nutzt zusaetzliche Rechte, um die Besitzer der
wiederhergestellten Dateien fuer den App-Benutzer zu korrigieren. Das aendert
nicht die Rechte des normalen Webservers. Zusaetzliche, nicht im Archiv
enthaltene Dateien im Daten-Volume werden nicht geloescht.

## Login

Beim Start werden Rollen, Grundeinstellungen und ein Admin-Nutzer angelegt.
Fuer echte Nutzung muessen vor dem ersten Start in `.env` mindestens
`ADMIN_USERNAME`, `ADMIN_PASSWORD` und `SESSION_SECRET` angepasst werden.

Neue Benutzer und Passwort-Resets erzeugen automatisch ein zufaelliges
64-Zeichen-Passwort. Dieses wird nach dem Speichern einmalig angezeigt.

Beim Serverstart werden keine Beispieldaten angelegt.

Superadmins koennen unter `Verwaltung > Einstellungen` einen Excel-Export der
Fach-, Vertrags-, Abrechnungs- und Verwaltungsdaten herunterladen.

## Optionale Beispieldaten

Das alte Beispieldaten-Seeding ist bewusst nicht im Startprozess eingebunden.
Produktive Systeme bleiben dadurch leer, bis Daten ueber die Oberflaeche
angelegt werden.
