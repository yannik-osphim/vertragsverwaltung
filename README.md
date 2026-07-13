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

Compose enthaelt ausserdem einen `db-backup`-Service. Dieser schreibt per
`pg_dump -Fc` regelmaessig Dumps in das Volume `db_backups`.

- Intervall: `BACKUP_INTERVAL_SECONDS`, Standard 86400 Sekunden
- Aufbewahrung: `BACKUP_RETENTION_DAYS`, Standard 14 Tage

Beispiel fuer ein manuelles Backup:

```powershell
docker compose exec db-backup pg_dump -h postgres -U user -d contracts -Fc -f /backups/manual.dump
```

Beispiel fuer Restore in eine leere Datenbank:

```powershell
docker compose exec db-backup pg_restore -h postgres -U user -d contracts --clean --if-exists /backups/manual.dump
```

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
