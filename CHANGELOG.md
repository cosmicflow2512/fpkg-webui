# Changelog

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
