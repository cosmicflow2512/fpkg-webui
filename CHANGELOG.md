# Changelog

## 1.4.1 – 2026-10-17
- Weitere Fix-/Backport-Ordner (Einstellungen → Watch-Ordner, einer pro Zeile), Standard `/shares/NZB/ps5-backport/zip` (Zip-Ausgabe des PS5-Backport-Containers). Sie erscheinen zusammen mit dem Fix-Ordner in den Vorschlägen bei „FPKG bauen“ und „Backport → PS5“ und gelten für die automatische Fix-Zuordnung im Watch-Ordner. Nicht vorhandene Ordner werden ignoriert

## 1.4.0 – 2026-10-17
- Neuer Modus „Entpacken“: Archiv (auch mehrteilig, verschachtelt wie DUPLEX, mit Passwort), `.exfat`-Image oder FPKG (`.pkg`) nur entpacken, ohne zu bauen. Ergebnis ist ein App-Ordner `<Zielordner>/<TITLE_ID>` (Ordnername frei wählbar), Standard-Ziel `<Ausgabe>/../dumps` bzw. `DEFAULT_EXTRACT`. Entpackt wird direkt im Zielordner, am Ende nur umbenannt; optional Prüfsumme und Quellarchiv danach löschen. Der Ordner taugt als Quelle für spätere Builds und als Ordner-Dump für ShadowMountPlus
- Paketname sichtbar und änderbar: Das Formular zeigt, wie das Paket heißen wird (`<Content-ID>-A…-V…[-backport|-fix].pkg`, bei Ordnern/Images schon vor dem Start, bei Archiven nach dem Entpacken), optional eigener Name. Im Auftrag steht der Name nach „Quelle prüfen“ und lässt sich bis zum Start des Builds ändern (mit „Vor dem Bauen anhalten“ auch bei Archiven)

## 1.3.3 – 2026-10-17
- Gleiches Spiel mehrfach bauen überschreibt nichts mehr: fpkg-cli benennt Pakete nur nach Content-ID und Version. Builds mit Fix heißen jetzt `…-V0400-backport.pkg` (Fix-Name enthält „backport“) bzw. `…-V0400-fix.pkg`; gibt es den Namen schon, wird `-JJJJMMTT-HHMM` angehängt (bei Bedarf `-2`, `-3` …) und im Auftrag gewarnt
- Die Namensprüfung sieht auch Pakete, die der Unraid-Mover vom Pool aufs Array verschoben hat: Liegt die Ausgabe auf einem Pool-Pfad (z. B. `/output` = `/mnt/<pool>/NZB`), wird derselbe Unterordner in den anderen freigegebenen Ordnern (z. B. `/shares/NZB` = `/mnt/user/NZB`) mitgeprüft. Bisher sah der Container dort nur den Pool und gab dem zweiten Build denselben Namen

## 1.3.2 – 2026-10-17
- Backport → PS5 mit ps5-payload-dev/ftpsrv: Der Server wandelt SELF-Dateien beim Lesen in ELF um (pro Verbindung, standardmäßig an) und meldet bei `SIZE` die entschlüsselte Größe – die Größenprüfung schlug deshalb bei `eboot.bin`, `*.sprx` und `*.prx` fehl, obwohl richtig hochgeladen wurde. Die WebUI schaltet das jetzt nach der Anmeldung mit `SITE SELF` ab
- Ordner auflisten/löschen auf der PS5 repariert: ftpsrv kennt kein `OPTS`/`NLST`, `MLSD` listet immer den aktuellen Ordner und `DELE` antwortet mit 226 – halbe Uploads blieben deshalb liegen („502 Command not recognized“), der Verbindungstest zählte die falschen Ordner
- Gegen einen aus dem ftpsrv-Quellcode gebauten Server getestet

## 1.3.1 – 2026-10-17
- Backport → PS5: Ist die PS5 nicht erreichbar (aus, Ruhemodus, FTP-Payload nicht geladen) oder bricht die Verbindung beim Upload ab, wartet der Auftrag im Status „wartet auf PS5“ und versucht es automatisch erneut – Standard alle 5 min, höchstens 24 h (Einstellungen → PS5 / ShadowMountPlus). Vor jedem Versuch nur ein kurzer TCP-Test auf den FTP-Port, entpackt wird erst, wenn die PS5 da ist
- „Jetzt versuchen“ im Auftrag startet den nächsten Versuch sofort, „Abbrechen“ beendet das Warten
- Wartende Backport-Aufträge überstehen einen Container-Neustart (ein Archiv-Passwort aus dem Auftrag nicht, dann gelten die Passwörter aus den Einstellungen)
- Pushover einmal beim ersten Fehlversuch (Schalter „wartet auf Freigabe“), danach wie gewohnt bei fertig/Fehler

## 1.3.0 – 2026-10-17
- Neuer Modus „Backport → PS5“ (Umschalter oben im Formular): Backport-Archiv (zip/rar/7z, auch mehrteilig/passwortgeschützt) oder Ordner wählen, die WebUI entpackt es und kopiert es per FTP nach `<Backport-Ordner>/<TITLE_ID>` auf der PS5 (Standard `/data/homebrew/backports`). ShadowMountPlus (ab 1.7beta2 auch für installierte PKGs) legt es beim Spielstart über das Spiel, das Paket selbst bleibt unverändert
- Title-ID, Spielversion und Firmware-Ziel kommen aus dem Namen (z. B. `[DLPSGAME.COM]-FW_4xx_PPSA09663_…_04_000_000_backport_files.zip`), beide Felder sind editierbar. Die Backport-Wurzel (Ordner mit `eboot.bin`, `sce_module/`, `fakelib/`, `sce_sys/`) wird gesucht, Hüllordner und Readmes bleiben weg; enthält das Archiv mehrere Varianten, wird eine im Formular gewählt
- Optional vor dem Kopieren über die ShadowMountPlus-API prüfen, ob das Spiel installiert ist und ob die Version passt – ohne Haken wird auch für noch nicht installierte Spiele kopiert
- Upload erst in einen Temp-Ordner, Größen werden geprüft; ein vorhandenes Backport derselben Title-ID wird nach `/data/backport-backups/<TITLE_ID>-<Zeit>` verschoben, erst dann wird das neue aktiv. Abbruch/Fehler entfernt den halben Upload
- Hinweise im Auftrag: mehr als 256 Dateien (Redirect-Limit von ShadowMountPlus bei installierten PKGs), `libSceAmpr.sprx`/`libScePlayGo.sprx`/`libkernel.sprx` in `fakelib/` (entfernt ShadowMountPlus bei installierten PKGs), fakelib für den Titel deaktiviert
- Einstellungen → PS5 / ShadowMountPlus: Adresse, FTP-Port/-Benutzer/-Passwort, API-Port, Backport- und Sicherungsordner, „Verbindung testen“
- Backport-Aufträge laufen in einer eigenen Warteschlange neben den FPKG-Builds

## 1.2.5 – 2026-10-10
- Neues Handbuch `docs/ANLEITUNG.md`: alle Bedienelemente mit Ablauf dahinter, Archive/Teile, Kompression, Watch-Ordner, Fehlerbilder, Architektur, API-Referenz, Update-Ablauf
- Image-Build repariert: 1.2.4 hatte die neue fpkg-cli-Version ohne neue SHA-256-Prüfsumme, der Build auf `main` schlug fehl (`:latest` blieb auf 1.2.3)
- Einfache Split-Dateien werden als zusammengehörig erkannt: `game.exfat.001`, `.002` … und `name.001`, `.002` … (7-Zip-Format „Split“). Bisher zählte nur das erste Teil – Größe, Prüfsumme, „Quellarchiv löschen“ und die Watch-Gruppierung haben die übrigen Teile übersehen, ein gewähltes `.002` ergab „Unbekannter Dateityp“

## 1.2.4 – 2026-10-10
- fpkg-cli 2.2.5 → 2.2.6 (Release vom 09.10.): eingebaute Engine auf Drakmors fpkg-gui 0.6.11 – behebt NAPS-Fehler und den DLC-Build, Dateien mit 32 Null-Bytes werden nicht mehr fälschlich als Passcode verworfen, `sce_sys/about/right.sprx` wird automatisch erzeugt
- Achtung, geändertes Verhalten von fpkg-cli: `sce_sys/playgo*` der Quelle bleibt jetzt standardmäßig erhalten (vorher verworfen und neu erzeugt); Dateien bleiben in ihren Original-Chunks

## 1.2.3 – 2026-10-09
- Formular schlägt Fixes und Backports aus dem Fix-Ordner (Einstellungen → Fix-Ordner, Standard `/shares/NZB/fpkg-fixes`) vor, sobald eine Quelle gewählt ist: passende Title-ID zuerst, ein Klick übernimmt den Fix. Die Title-ID kommt aus dem Namen, der Archivliste (auch bei DUPLEX-Namen ohne ID), `inspect` oder `pkg-info`
- Ohne passende Title-ID werden die neuesten Einträge des Fix-Ordners gezeigt

## 1.2.2 – 2026-10-08
- Verschachtelte Archive (z. B. DUPLEX: mehrteiliges RAR, darin ein einzelnes RAR) werden automatisch bis zu 3 Ebenen tief entpackt, statt mit „Archiv enthält nur ein weiteres Archiv“ abzubrechen. Das innere Archiv liegt im Arbeitsordner und wird direkt nach dem Entpacken gelöscht. Neuer Schritt „Inneres Archiv entpacken“
- RAR mit alter Benennung über 100 Teile (`.rar`, `.r00`–`.r99`, `.s00` …) wird vollständig erkannt: Größe, Prüfsumme, „Quellarchiv löschen“ und das Löschen innerer Archive erfassen jetzt auch die `.sNN`-Teile
- Formular: Ausgabe- und Arbeitsordner, die im Browser nur als alter Standardwert gespeichert waren, folgen jetzt einem geänderten `DEFAULT_OUT`/`DEFAULT_WORK`. Selbst gewählte Pfade bleiben erhalten

## 1.2.1 – 2026-10-03
- Passwortgeschützte Archive (7z, RAR/RAR5, ZIP, auch mehrteilig): Erkennung schon bei der Quellauswahl, Feld „Archiv-Passwort“ im Auftrag, Passwortliste unter Einstellungen → Archiv-Passwörter (eine Zeile pro Passwort, wird der Reihe nach probiert)
- Ohne passendes Passwort bricht der Auftrag sofort mit klarer Meldung ab statt mit „Break signaled“ (Exit 255)
- 7-Zip und alle anderen Befehle laufen ohne Eingabekanal, Rückfragen können den Auftrag nicht mehr hängen lassen
- Passwort wird in Oberfläche, API, Auftrags-Log, Server-Log und Diagnose-Paket maskiert und nicht in jobs.json gespeichert
- „Neu starten“ läuft jetzt über den Server und übernimmt das Passwort des Auftrags

## 1.2.0 – 2026-10-03
- Neuer Reiter „Pakete“: alle fertigen Pakete mit Titel, Version, benötigter Firmware, SDK und Content-ID; Schnell- und Vollprüfung (`verify` / `verify --full`) per Klick, Ergebnis wird gespeichert und als Häkchen angezeigt, Prüfungen laufen unabhängig von den Aufträgen
- Option „Vollprüfung nach dem Bauen“
- Aufträge neu: getrennt in „Aktiv“ und „Verlauf“, Name des gewählten Auftrags groß im Kopf, Abbrechen/Löschen nur noch im Menü „⋯ Mehr“ mit Dialog, der die Folgen nennt; vorausgewählt ist „Weiterlaufen lassen“
- „Verlauf leeren“ (löscht keine Pakete, Quellen oder Arbeitsordner)
- Mobile Ansicht: Reiterleiste und Diagnose-Tabellen passen auf schmale Bildschirme

## 1.1.1 – 2026-10-02
- Pushover über Template-Variablen `PUSHOVER_USER`, `PUSHOVER_TOKEN` (maskiert) und `WEBUI_URL`; gesetzte Werte haben Vorrang und sind in der WebUI gesperrt

## 1.1.0 – 2026-10-02
- Watch-Ordner: fertige Archive/Images/.pkg/Ordner im Eingang werden automatisch eingereiht (Stillstand-Erkennung, `.part`/`_UNPACK_` werden übersprungen), Fix wird über die Title-ID im Namen aus dem Fix-Ordner zugeordnet, Quelle danach nach `_erledigt`
- Pushover-Benachrichtigung bei fertig, Fehler (Priorität hoch) und „wartet auf Freigabe“, mit Test-Knopf
- Prüfsumme vor dem Entpacken: SHA-256/MD5 (sha256sum-, BSD- und Einzeilen-Format, z. B. SHA-256.txt) und SFV, auch über mehrteilige Archive
- Überlappende Warteschlange: der nächste Auftrag wird vorbereitet (Prüfsumme, Entpacken, Fix), während der aktuelle baut – max. ein Auftrag Vorlauf, Platz wird für beide eingeplant
- Freigabe blockiert die Warteschlange nicht mehr; neuer Status „bereit“
- Neuer Reiter „Einstellungen“ (gespeichert in /config/settings.json)

## 1.0.1 – 2026-10-02
- Platzprüfung vor dem Entpacken von Archiven und dem Kopieren aus exFAT (entpackte Größe × 1,6 inkl. Build-Temp), klarer Hinweis statt vollgelaufenem Pool
- Template: Arbeitsordner standardmäßig direkt auf dem Cache-Pool (schneller als Array/FUSE), Host-Port 8099

## 1.0.0 – 2026-10-02
- Erste Version: Weboberfläche für fpkg-cli 2.2.5
- Quellen: 7z/zip/rar (mehrteilig), exFAT/ffpfsc/ffpkg, pkg, App-Ordner
- Fix/Backport mit Abgleich und Kontrolle im fertigen Paket
- Eigener exFAT-Leser (ohne Mount, MBR/GPT)
- Fortschrittsbalken mit Restzeit, Schritt-Leiste, Warteschlange
- Option: Quellarchiv nach dem Entpacken löschen (Standard aus)
- Diagnose: Selbsttest, Server-Log, Diagnose-Paket, Log-Download je Auftrag
- Unraid-Template, GitHub Actions (GHCR), install.sh für lokalen Build
