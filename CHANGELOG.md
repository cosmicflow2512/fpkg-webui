# Changelog

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
