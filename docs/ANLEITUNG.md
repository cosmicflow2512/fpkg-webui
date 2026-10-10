# fpkg-webui – Handbuch

Stand: Version 1.2.5 · fpkg-cli 2.2.6 · 7-Zip 25.01

Dieses Handbuch beschreibt jede Funktion der Oberfläche, was dabei im Hintergrund passiert und wie
die Anwendung aufgebaut ist. Kurzfassung und Installation stehen im [README](../README.md).

**Inhalt**

1. [Überblick](#1-überblick)
2. [Installation und Pfade](#2-installation-und-pfade)
3. [Oberfläche: Neuer Auftrag](#3-oberfläche-neuer-auftrag)
4. [Ablauf eines Auftrags](#4-ablauf-eines-auftrags)
5. [Quellen, Archive und Teile](#5-quellen-archive-und-teile)
6. [Fix und Backport](#6-fix-und-backport)
7. [Kompression](#7-kompression)
8. [Prüfsummen](#8-prüfsummen)
9. [Reiter „Aufträge“](#9-reiter-aufträge)
10. [Reiter „Pakete“](#10-reiter-pakete)
11. [Watch-Ordner](#11-watch-ordner)
12. [Reiter „Einstellungen“](#12-reiter-einstellungen)
13. [Reiter „Diagnose“](#13-reiter-diagnose)
14. [Grenzen](#14-grenzen)
15. [Fehlerbilder und Lösungen](#15-fehlerbilder-und-lösungen)
16. [Wie die Anwendung gebaut ist](#16-wie-die-anwendung-gebaut-ist)
17. [API-Referenz](#17-api-referenz)
18. [Entwicklung, Build und Updates](#18-entwicklung-build-und-updates)

---

## 1. Überblick

fpkg-webui ist eine Weboberfläche für Unraid, die aus einem PS5-Spiel ein installierbares
**Debug-FPKG** baut (FIH, `PPRPLAIN-NOAUTH`, installierbar auf einer PS5 im Debug-Modus).
Gebaut wird mit dem [PSVIETHOA FPKG Builder](https://github.com/thanhsondev/PSVIETHOA-FPKG-Builder)
(`fpkg-cli`) und dessen eingebauter Engine (LibProsperoPkg von Drakmor).

Die Oberfläche übernimmt alles um den eigentlichen Build herum:

- Quelle erkennen (Archiv, Image, `.pkg`, Ordner), auch mehrteilig, passwortgeschützt, verschachtelt
- Prüfsumme kontrollieren, entpacken, Platz prüfen
- Fix oder Backport über das Spiel legen und protokollieren, was ersetzt wurde
- bauen, das fertige Paket prüfen, aufräumen
- Warteschlange, Watch-Ordner für automatische Builds, Pushover-Benachrichtigungen
- Paketliste mit Prüfungen, Diagnose

---

## 2. Installation und Pfade

Installation über das Unraid-Template oder lokal mit `install.sh`, siehe README.

### Container-Pfade

| Container-Pfad | Zweck | Hinweis |
|---|---|---|
| `/shares/<Name>` | Quellen | Jeder Ordner direkt unter `/shares/` erscheint im Dateibrowser. Weitere Shares per *Add another Path* mit Container-Pfad `/shares/<Name>` |
| `/output` | fertige Pakete | Standardziel ist `DEFAULT_OUT` (Standard `/output`) |
| `/work` | Entpacken und Build-Temp | braucht etwa das 1,6-fache der entpackten Spielgröße. Am schnellsten direkt auf einem NVMe-Pool (`/mnt/<pool>/…`) |
| `/config` | Aufträge, Logs, Einstellungen | `jobs.json`, `settings.json`, `checks.json`, `watch_state.json`, `logs/` |

### Variablen

| Variable | Standard | Wirkung |
|---|---|---|
| `DEFAULT_OUT` | `/output` | vorgeschlagener Ausgabeordner |
| `DEFAULT_WORK` | `/work` | vorgeschlagener Arbeitsordner |
| `BROWSE_ROOTS` | `/shares/*:/output:/work` | Wurzeln für Dateibrowser und erlaubte Pfade |
| `PUID` / `PGID` | `99` / `100` | Besitzer der fertigen Pakete |
| `PUSHOVER_USER`, `PUSHOVER_TOKEN`, `WEBUI_URL` | leer | haben Vorrang vor der WebUI, die Felder sind dann gesperrt |
| `LOG_LEVEL` | `INFO` | `DEBUG` protokolliert jeden HTTP-Aufruf und Befehl |
| `PORT` | `8095` | Port im Container. Im Template auf Host-Port 8099 gemappt |

### Ausgabe direkt auf dem Pool – worauf achten

Wird `/output` direkt auf ein Pool-Verzeichnis gemappt (z. B. `/mnt/cache_backup/NZB/ps5-fpkg/out/`),
sind Builds schneller als über `/mnt/user` (kein FUSE). Zwei Regeln:

1. **Nie einen Unterordner mappen, den der Mover leeren kann.** Verschiebt der Mover das letzte Paket,
   entfernt er den leeren Ordner, und der Container hängt an einem gelöschten Verzeichnis
   (`… //deleted` in `/proc/self/mountinfo`, „No such file or directory“ beim Bauen).
   Stattdessen das Dataset- bzw. Share-Root mappen, z. B. `/mnt/cache_backup/NZB/` → `/output`,
   und `DEFAULT_OUT=/output/ps5-fpkg/out` setzen. Den Unterordner legt fpkg-webui bei jedem Auftrag an.
2. **Pakete, die der Mover ins Array schiebt, sieht der Container nicht mehr.** Dann ist die
   Paketliste leer, und die Prüfung auf gleichnamige Pakete greift nicht. Abhilfe:
   `NZB/ps5-fpkg/out` in Mover Tuning ausnehmen oder die Ausgabe über `/mnt/user/…` mappen.

---

## 3. Oberfläche: Neuer Auftrag

Die Seite hat links das Formular **Neuer Auftrag**, rechts die Reiter **Aufträge**, **Pakete**,
**Einstellungen** und **Diagnose**. Das Formular merkt sich die Eingaben pro Browser
(`localStorage`). Ausgabe- und Arbeitsordner, die nur als alter Standard gespeichert waren,
folgen einem geänderten `DEFAULT_OUT` / `DEFAULT_WORK`.

| Element | Text in der Oberfläche | Was dahinter passiert |
|---|---|---|
| **Quelle** + „…“ | „Datei oder Ordner wählen“ · Hinweis: „7z / zip / rar, .exfat / .ffpfsc / .ffpkg, .pkg oder App-Ordner (sce_sys + eboot.bin)“ | „…“ öffnet den Dateibrowser (`/api/ls`). Eingabe oder Auswahl prüft nach 400 ms per `/api/probe?fixes=1`: Art (`classify`), alle Teile (`archive_volumes`), Verschlüsselung und Title-ID (`7z l -slt`), bei Ordnern und Images `fpkg-cli inspect`, bei `.pkg` `fpkg-cli pkg-info`. Ergebnis grün unter dem Feld, z. B. „Archiv (185 Teile) · 92 GB“ |
| **Archiv-Passwort** | nur bei verschlüsselten Archiven · „leer = Passwörter aus den Einstellungen probieren“ · „gilt für Quelle und Fix · wird nicht im Log angezeigt“ | wird mit dem Auftrag übergeben, nie in `jobs.json` gespeichert |
| **Fix / Backport** + „…“ + „✕“ | „leer = ohne Fix“ · „Archiv oder Ordner; Dateien ersetzen gleichnamige im App-Ordner“ | `/api/probe` für den Fix. „✕“ leert das Feld |
| Fix-Vorschläge | „Passend zu PPSA…:“ · „Kein Fix für … – im Fix-Ordner:“ · „Title-ID unbekannt – im Fix-Ordner:“ · „N weitere zeigen“ | aus der Probe der Quelle: `list_fixes(Fix-Ordner, Title-ID)`. Ein Klick setzt das Fix-Feld |
| **Ausgabeordner** + „…“ | „frei: … von …“ | `/api/free` |
| ☑ **Vor dem Bauen anhalten** | „zeigt erkannte Version und Fix-Abgleich, Bauen erst nach Klick“ | Auftrag endet nach der Vorbereitung im Status *Freigabe* |
| ☑ **Arbeitsordner nach Erfolg löschen** | „entpackte Daten (oft 50–100 GB) werden entfernt“ | Schritt *Aufräumen* |
| ☑ **Prüfsumme prüfen, wenn vorhanden** | „SHA-256 / MD5 / SFV neben der Quelle (z. B. SHA-256.txt) – kaputte Downloads fallen vor dem Entpacken auf“ | siehe [Prüfsummen](#8-prüfsummen) |
| ☐ **Quellarchiv nach dem Entpacken löschen** | „löscht die .7z/.zip/.rar inkl. aller Teile (part2, .001 …), sobald das Entpacken erfolgreich war“ | löscht alle erkannten Teile. Der Arbeitsordner bleibt danach auch bei Fehlern erhalten. Bei Nicht-Archiven ausgegraut |
| Erweitert → **Kompression** | „Standard (Kraken 4)“ · „Schnell (Kraken 2)“ · „Klein (Kraken 7 Optimal, 5–6× langsamer)“ | `--preset standard / fast / smallest`, siehe [Kompression](#7-kompression) |
| Erweitert → **Arbeitsordner** | „frei: …“ | `/work/job-<id>` entsteht darunter |
| Erweitert → **Vollprüfung nach dem Bauen** | „liest das ganze Paket und dekomprimiert jede Datei – dauert länger“ | `fpkg-cli verify --full` statt `verify` |
| Erweitert → **AMPR-Reste behalten** | „betrifft nur ampr_emu.index; fakelib/libSceAmpr.sprx entfernt der Builder immer“ | `--keep-ampr` |
| Erweitert → **Dump-Reste behalten** | „_DUBLEX_, Unreal Engine/Saved, <Projekt>/Saved“ | `--keep-dump-leftovers` |
| **FPKG erstellen** | — | `POST /api/jobs` → `enqueue()` legt Ausgabe- und Arbeitsordner an und reiht den Auftrag ein, dann Wechsel zu *Aufträge* |

**Dateibrowser:** Ohne Pfad zeigt er die Wurzeln aus `BROWSE_ROOTS` mit freiem Platz. Ein Klick auf
einen Ordner öffnet ihn, ein Klick auf eine Datei wählt sie, **Ordner wählen** bzw. **Diesen Ordner wählen** übernimmt den
aktuellen Ordner. Pfade außerhalb der Wurzeln lehnt der Server ab.

---

## 4. Ablauf eines Auftrags

### Status

```
Warteschlange ─► läuft (Vorbereitung) ─► Freigabe ──[Bauen starten]──► bereit ─► läuft (Build) ─► fertig
                                     └── ohne „anhalten“ direkt ─────────┘
jederzeit:  ─► Fehler  │  abgebrochen
```

Die Vorbereitung (Phase 1) und der Build (Phase 2) laufen in getrennten Threads. Gebaut wird immer
nur ein Paket gleichzeitig. Mit „Überlappend arbeiten“ wird der nächste Auftrag vorbereitet, während
der aktuelle baut. Ein Neustart des Containers setzt laufende Aufträge auf *Fehler: Container wurde
neu gestartet*.

### Phase 1 – Vorbereitung (`Job.prepare`)

| Schritt in der Leiste | Was passiert | Werkzeug |
|---|---|---|
| Prüfsumme | Prüfsummendatei neben der Quelle suchen, jedes Teil hashen | Python |
| Quelle entpacken | Platz prüfen (entpackte Größe × 1,6 minus Reserven anderer Aufträge auf derselben Platte), Passwort finden, entpacken, OS-Müll entfernen (`.DS_Store`, `__MACOSX`, `Thumbs.db` …), Inhalt neu erkennen | `7z l`, `7z x` |
| Inneres Archiv entpacken | nur bei Archiv im Archiv: nach `src2`, `src3` entpacken, inneres Archiv sofort löschen, bis 3 Ebenen | `7z x` |
| App aus exFAT kopieren | nur exFAT + Fix: App-Ordner mit dem eigenen exFAT-Leser herauskopieren | `exfat.py` |
| App-Ordner kopieren | nur Ordner-Quelle + Fix, damit die Quelle unverändert bleibt | Python |
| FPKG entpacken | nur `.pkg` als Quelle | `fpkg-cli pkg-extract` |
| Fix übernehmen | Fix entpacken, Fix-Wurzel bestimmen, jede Datei als ERSETZT/NEU protokollieren, kopieren | `7z x`, Python |
| Quelle prüfen | Content-ID, Version, Größe, PlayGo, AMPR, DLC-Emulator lesen; `sce_sys` und `eboot.bin` müssen vorhanden sein | `fpkg-cli inspect` |
| Freigabe | nur mit „anhalten“: Status *Freigabe*, Pushover „wartet“ | — |

### Phase 2 – Build (`Job.build`)

| Schritt | Was passiert | Werkzeug |
|---|---|---|
| FPKG bauen | baut in `<Ausgabe>/.building-<id>`, benennt danach auf den Namen von fpkg-cli um. Ein halbfertiges Paket trägt nie den echten Namen. Existiert der Name schon, wird `-<Auftrags-ID>` angehängt. Besitzer wird `PUID:PGID` | `fpkg-cli build --source … --output … --temp … --no-sony-sdk --no-sleep-guard --preset …` |
| Paket prüfen | Titel, Version, benötigte Firmware, Content-ID lesen; Dateiliste mit den Fix-Dateien abgleichen (fehlende → Warnung); Schnell- oder Vollprüfung | `fpkg-cli pkg-info`, `pkg-list`, `verify [--full]` |
| Aufräumen | Arbeitsordner löschen | — |

Der Dateiname kommt von fpkg-cli: `<Content-ID>-A<App-Version>-V<Version>.pkg`, z. B.
`EP5291-PPSA34547_00-0210080241603648-A01300-V01300.pkg`.

---

## 5. Quellen, Archive und Teile

| Quelle | Erkennung / Ablauf |
|---|---|
| `.7z`, `.zip`, `.rar`, `.tar`, `.tar.gz`, `.tgz`, `.xz`, `.bz2` | entpacken, dann den Inhalt erkennen |
| 7z mehrteilig: `name.7z.001`, `.002` … | ✓ |
| ZIP mehrteilig: `name.zip.001` … oder `name.zip` + `name.z01`, `.z02` … | ✓ |
| RAR neu: `name.part1.rar` … `name.part10.rar` (auch `part001`, auch Großschreibung) | ✓ |
| RAR alt: `name.rar` + `.r00`–`.r99` + `.s00`–`.s99` … (bis `.y99`) | ✓ |
| einfacher Split: `game.exfat.001`, `.002` … oder `name.001`, `.002` … | ✓ seit 1.2.5 |
| Archiv im Archiv (z. B. DUPLEX) | bis 3 Ebenen automatisch |
| passwortgeschützt, auch mit verschlüsselten Dateinamen | Passwort im Auftrag oder Liste in den Einstellungen |
| `.exfat` (auch mit MBR/GPT) | ohne Fix direkt bauen, mit Fix App herauskopieren |
| `.ffpfsc`, `.ffpkg` | direkt bauen, kein Fix möglich |
| `.pkg` | entpacken und neu bauen, z. B. um einen Fix einzubauen |
| Ordner | App-Ordner (`sce_sys` + `eboot.bin`) darin → bauen; sonst Image, `.pkg` oder Archiv darin suchen |

**Teile:** Es genügt, ein beliebiges Teil zu wählen. fpkg-webui nimmt das erste Teil und erfasst
alle zugehörigen für Größe, Prüfsumme, „Quellarchiv löschen“ und die Gruppierung im Watch-Ordner.
Entpackt wird mit 7-Zip über das erste Teil, 7-Zip findet die übrigen selbst.

**Passwörter:** Zuerst das Passwort aus dem Auftrag, dann die Liste aus den Einstellungen, Zeile für
Zeile. Geprüft wird mit dem billigsten Test (Dateiliste bei verschlüsselten Namen, sonst die kleinste
verschlüsselte Datei). Passt keines, bricht der Auftrag sofort mit klarer Meldung ab. Alle Befehle
laufen ohne Eingabekanal, Rückfragen von 7-Zip können nichts aufhalten.

---

## 6. Fix und Backport

- Der Fix (Archiv oder Ordner) wird **1:1 über den App-Ordner** gelegt: gleiche Pfade werden ersetzt,
  neue Dateien kommen dazu.
- **Fix-Wurzel:** Enthält der Fix einen App-Ordner (`sce_sys`/`eboot.bin`), gilt dieser als Wurzel,
  sonst der oberste Ordner (bei genau einem Unterordner dieser).
- Das Log listet jede Datei als `ERSETZT` oder `NEU`. Sind mehr als die Hälfte neu, warnt die
  Oberfläche, dass die Ordnerebene evtl. nicht passt. Bei Unlockern und Backports, die neue
  `fakelib`-Module mitbringen, ist das normal.
- Nach dem Build prüft fpkg-webui, ob alle Fix-Dateien im Paket sind. Fehlende werden als Warnung
  gemeldet. Bekannt: `fakelib/libSceAmpr.sprx` entfernt der Builder immer; in Tests fehlten außerdem
  `fakelib/libScePlayGo.sprx` und `sce_sys/param.json` aus dem Fix (die Firmware-Anforderung kam
  trotzdem an).
- **Fix-Ordner und Vorschläge:** Im Fix-Ordner (Einstellungen) abgelegte Fixes werden über die
  Title-ID im Dateinamen zugeordnet, z. B. `PPSA34547 Backport FW403.zip`. Das Formular schlägt
  passende Fixes vor, der Watch-Ordner nimmt sie automatisch.

**Wichtig:** Ein Fix gehört immer in das Feld *Fix*, das Spiel immer in *Quelle*. Ein Backport allein
als Quelle ergibt ein winziges Paket mit derselben Content-ID wie das Spiel. Auf der Konsole würde es
das Spiel ersetzen statt ergänzen. Separate Update-Pakete gehen nicht, siehe [Grenzen](#14-grenzen).

---

## 7. Kompression

fpkg-cli komprimiert die Dateien im Paket mit **Kraken** (Oodle-Format) über einen eingebauten
Encoder – laut `fpkg-cli info` „byte-identical to native Oodle“. Die Kompression kostet beim Bauen
CPU-Zeit; das Entpacken hängt an der Platte.

| WebUI | fpkg-cli | laut `fpkg-cli build --help` |
|---|---|---|
| Standard | `--preset standard` (= `balanced`) | „Standard: level 4, the old engine's ‚Kraken 7'“ |
| Schnell | `--preset fast` | keine Stufe angegeben |
| Klein | `--preset smallest` | „level 7 Optimal, slow“ |

Die Beschriftung in der Oberfläche („Kraken 4 / 2 / 7 Optimal, 5–6× langsamer“) ist nicht
gegen die fpkg-cli-Doku abgeglichen; der Faktor ist nicht nachgemessen. fpkg-cli kennt zusätzlich
`maximum`, das die Oberfläche nicht anbietet.

---

## 8. Prüfsummen

Liegt neben der Quelle eine Prüfsummendatei, wird **vor dem Entpacken** jedes Teil geprüft.

- erkannte Dateien: Endung `.sfv`, `.sha256`, `.sha256sum`, `.md5`, `.sha` oder Name beginnend mit
  `sha256sums`, `sha-256`, `sha256`, `md5sums`, `checksum`
- Formate: sha256sum, BSD (`SHA256 (datei) = …`), einzelner Hash für eine Datei, SFV (CRC32)

So fällt ein kaputter Teil-Download auf, bevor 100 GB entpackt sind.

---

## 9. Reiter „Aufträge“

| Element | Text | Dahinter |
|---|---|---|
| Aktiv / Verlauf (N) | Status: Warteschlange, läuft, Freigabe, bereit, fertig, Fehler, abgebrochen | `/api/jobs` alle 3 s. Gespeichert werden die letzten 200 Aufträge |
| Verlauf leeren … | Dialog: „Entfernt alle fertigen, fehlgeschlagenen und abgebrochenen Aufträge aus der Liste. · Laufende und wartende Aufträge bleiben. · Fertige Pakete, Quelldateien und Arbeitsordner werden nicht gelöscht.“ | `POST /api/jobs/clear-history` |
| Kopf | Name, Status, aktueller Schritt, Schrittleiste, Balken mit Restzeit | Fortschritt aus der Ausgabe von 7-Zip und fpkg-cli |
| Ergebnis | „Paket: …“, Titel, Content-ID, Version, Firmware, Größe, „Schnellprüfung OK“, Dauer, „nicht im Paket: …“ | Ergebnis von Phase 2 |
| Warnungen | gelbe Liste | z. B. Namenskonflikt, fehlende Fix-Dateien |
| Live-Log | — | `/api/jobs/<id>/log` alle 1,2 s |
| Bauen starten | nur bei *Freigabe* | `POST …/continue` |
| Paket prüfen | nur bei *fertig* | Vollprüfung im Reiter *Pakete* |
| Neu starten | „Mit denselben Einstellungen neu einreihen“ | `POST …/retry`, „Quellarchiv löschen“ wird abgeschaltet |
| Log ↓ | „Log als Datei herunterladen“ | `GET …/download` |
| ⋯ Mehr → Auftrag abbrechen … | „stoppt nur diesen Auftrag“ · Dialog mit Folgen, „Ja, Auftrag abbrechen“ / „Weiterlaufen lassen“ | eingereiht → sofort abgebrochen; Freigabe/bereit → beendet; läuft → laufender 7-Zip-/fpkg-cli-Prozess wird beendet |
| ⋯ Mehr → Arbeitsordner löschen … | „entpackte Daten dieses Auftrags“ · „Das fertige Paket und deine Quelldateien bleiben erhalten.“ | `POST …/cleanup` |
| ⋯ Mehr → Aus Liste entfernen | „Paket und Quelle bleiben erhalten“ | `POST …/delete`, löscht auch das Auftrags-Log |

---

## 10. Reiter „Pakete“

- Liste aller `.pkg` im gewählten Ordner (Standard: Ausgabeordner) mit Titel, Version, benötigter
  Firmware, SDK, Content-ID, Größe und Datum (`/api/packages`, Daten aus `fpkg-cli pkg-info`).
- **Info** klappt die Details auf.
- **Schnell** (`fpkg-cli verify`): Segmente, CNT-Struktur, Digests, PlayGo, PFS-Metadaten – Sekunden.
- **Voll** (`fpkg-cli verify --full`): liest und dekomprimiert jede Datei – bei 80 GB entsprechend lang.
- Ergebnisse werden in `/config/checks.json` gespeichert und als Häkchen angezeigt, solange Größe und
  Änderungszeit gleich sind. Prüfungen laufen unabhängig von den Aufträgen.

Eine bestandene Prüfung heißt: das Paket ist in sich stimmig. Ob eine Datei schon beim Bauen
weggelassen wurde, erkennt sie nicht.

---

## 11. Watch-Ordner

„Ordner rein, FPKG raus“: Was fertig im Eingang landet, wird automatisch gebaut.

**Einrichten:** Einstellungen → *Watch-Ordner aktiv*, Eingang (Standard `/shares/NZB/fpkg-inbox`)
und Fix-Ordner (Standard `/shares/NZB/fpkg-fixes`) festlegen. SABnzbd oder JDownloader direkt in
den Eingang laden bzw. entpacken lassen.

**Ablauf:**

1. Alle *Prüfintervall* Sekunden (Standard 30) werden die Einträge im Eingang gelesen.
   Archivteile werden zu einer Gruppe zusammengefasst.
2. Übersprungen wird alles, was mit `.` oder `_` beginnt (also auch `_UNPACK_…` und `_erledigt`)
   oder Download-Reste enthält: `.part`, `.tmp`, `.crdownload`, `.!qb`, `.jdtmp`, `.download`, `.partial`.
3. Ein Eintrag gilt als fertig, wenn Größe, Änderungszeit und Dateianzahl über die
   *Stillstandszeit* (Standard 120 s) gleich bleiben.
4. Title-ID aus dem Namen, sonst aus `inspect` bzw. `pkg-info`. Ein Fix im Fix-Ordner, dessen Name
   die ID enthält, wird genommen (bei mehreren der neueste).
5. Der Auftrag läuft mit den Watch-Einstellungen (Kompression, Anhalten, Aufräumen, Archiv löschen).
6. Nach Erfolg wird die Quelle nach `_erledigt` verschoben (oder bleibt liegen).
7. Verarbeitete Einträge stehen in `/config/watch_state.json`. **↻** verarbeitet einen Eintrag neu,
   **Jetzt prüfen** scannt sofort.

---

## 12. Reiter „Einstellungen“

Gespeichert in `/config/settings.json` über **Speichern**.

| Bereich | Option | Standard |
|---|---|---|
| Warteschlange | Überlappend arbeiten – „Während ein Auftrag baut (CPU), wird der nächste schon geprüft/entpackt (Platte). Platz im Arbeitsordner wird dabei für beide eingeplant.“ | an |
| | Prüfsumme standardmäßig prüfen – „gilt für neue Aufträge und den Watch-Ordner“ | an |
| Watch-Ordner | aktiv | aus |
| | Eingang / Fix-Ordner | `/shares/NZB/fpkg-inbox` / `/shares/NZB/fpkg-fixes` |
| | Stillstand vor Start / Prüfintervall | 120 s / 30 s (mindestens 5) |
| | Kompression | Standard |
| | Nach Erfolg | Quelle nach `_erledigt` verschieben |
| | Vor dem Bauen anhalten – „sonst baut der Watch-Ordner ohne Rückfrage“ | aus |
| | Arbeitsordner nach Erfolg löschen / Quellarchiv löschen | an / aus |
| Archiv-Passwörter | ein Passwort pro Zeile, der Reihe nach probiert | leer |
| Pushover | aktiv, User Key, API Token, Link in der Nachricht, Ereignisse fertig / Fehler (Priorität hoch) / wartet auf Freigabe, **Pushover testen** | aus |

Passwörter stehen im Klartext in `settings.json`. In Oberfläche, API, Logs und Diagnose-Paket werden
sie maskiert.

---

## 13. Reiter „Diagnose“

| Element | Inhalt |
|---|---|
| Übersicht | Version, Laufzeit, 7-Zip, Python, System, CPUs/RAM, Anzahl Aufträge, Konfiguration |
| Freigegebene Ordner | Pfad, freier Platz, Schreibtest |
| **Selbsttest** | fpkg-cli vorhanden, Engine und Debug-Keys (`fpkg-cli info`), 7-Zip mit RAR, Schreibtest je Ordner, Ausgabe- und Arbeitsordner gemappt, mindestens 100 GB frei im Arbeitsordner |
| fpkg-cli info | Ausgabe von `fpkg-cli info` |
| Server-Log + **neu laden** | letzte 400 Zeilen von `/config/logs/server.log` |
| **Diagnose-Paket ↓** | ZIP mit `diagnose.json`, Server-Log, Auftragsliste, den letzten 15 Auftrags-Logs; Passwörter maskiert |

---

## 14. Grenzen

- **Keine separaten Update-, Backport- oder Fix-Pakete.** fpkg-cli baut Update-Pakete nur über das
  Sony SDK (`--sdk-reference`, Publishing Tools 2.79). Das ist nicht im Container enthalten; die
  eingebaute Engine kennt nur `--kind app|homebrew|dlc`. Ein Fix geht nur ins Spiel-Paket.
- Kein Fix auf `.ffpfsc` / `.ffpkg`.
- `fakelib/libSceAmpr.sprx` landet nie im Paket.
- Ergebnis sind immer Debug-FPKGs.
- **Kein Login.** Nur im LAN betreiben, nicht ungeschützt über einen Reverse-Proxy freigeben.

---

## 15. Fehlerbilder und Lösungen

| Symptom | Ursache | Lösung |
|---|---|---|
| `FileNotFoundError: … /output/.building-…` | Mover hat den gemappten Ausgabe-Unterordner gelöscht; `grep " /output " /proc/self/mountinfo` zeigt `//deleted` | Dataset-Root mappen + `DEFAULT_OUT` (siehe [Abschnitt 2](#ausgabe-direkt-auf-dem-pool--worauf-achten)), Container neu starten |
| „Archiv enthält nur ein weiteres Archiv“ | Version vor 1.2.2 | auf aktuelle Version aktualisieren |
| Paket nur ~100 MB groß, gleiche Content-ID wie das Spiel | Backport/Fix als *Quelle* gewählt | Spiel als Quelle, Backport als Fix |
| Neues Paket verdeckt ein älteres gleichen Namens unter `/mnt/user` | älteres liegt nach dem Mover im Array, Container sieht es nicht | Mover-Ausnahme für `ps5-fpkg/out` oder Ausgabe über `/mnt/user` |
| Paketliste leer, obwohl Pakete da sind | wie oben | wie oben |
| „Kein App-Ordner … in …/src“ | Quelle ist kein Spiel (z. B. nur ein DLC-Unlocker) | als Fix verwenden |
| „Kein passendes Passwort“ | weder Auftrags-Passwort noch Liste passt | Passwort eintragen |
| „Zu wenig Platz im Arbeitsordner“ | entpackt × 1,6 passt nicht | unter *Erweitert* größeren Arbeitsordner wählen |
| Version bleibt nach Update alt | `docker restart` startet das alte Image | im Docker-Tab **Force Update** bzw. Edit → Apply (siehe [Updates](#updates)) |

---

## 16. Wie die Anwendung gebaut ist

### Aufbau des Repositorys

| Datei | Inhalt |
|---|---|
| `app/server.py` | gesamter Server: HTTP, API, Auftrags-Pipeline, Watch-Ordner, Prüfungen, Diagnose (~2 200 Zeilen) |
| `app/index.html` | gesamte Oberfläche: HTML, CSS und JavaScript in einer Datei (~650 Zeilen) |
| `app/exfat.py` | exFAT-Leser in reinem Python (~230 Zeilen) |
| `tests/test_basic.py` | Tests ohne fpkg-cli: Archivteile, Klassifizierung, Fortschritt, Prüfsummen, verschachtelte Archive, Fix-Vorschläge, exFAT |
| `Dockerfile` | Image auf Basis `debian:bookworm-slim` |
| `unraid/fpkg-webui.xml` | Unraid-Template |
| `install.sh` | lokaler Build ohne GitHub |
| `.github/workflows/docker.yml` | CI: Tests, Image-Build, Push nach GHCR, Smoke-Test |

### Server

- **Nur Python-Standardbibliothek**, keine Pakete: `http.server.ThreadingHTTPServer` mit einem
  eigenen `BaseHTTPRequestHandler` (`class H`), `subprocess` für 7-Zip und fpkg-cli, `threading`,
  `json`, `hashlib`/`zlib` für Prüfsummen, `zipfile` für das Diagnose-Paket, `urllib` für Pushover.
- `GET /` liefert `index.html`, alles unter `/api/…` ist JSON.
- **Hintergrund-Threads**, beim Start gestartet:

  | Thread | Aufgabe |
  |---|---|
  | `prep` | Phase 1 (Vorbereitung); mit „Überlappend“ maximal ein Auftrag Vorlauf |
  | `build` | Phase 2 (Build und Paketprüfung), immer nur ein Build |
  | `watch` | Watch-Ordner-Scan |
  | `checks` | Schnell-/Vollprüfungen aus dem Reiter *Pakete* |

  Abgestimmt werden sie über eine gemeinsame Auftragsliste (`JOBS`, `ORDER`) und eine
  `threading.Condition`.
- **Befehle** laufen als Unterprozesse ohne Eingabekanal. Ihre Ausgabe wird zeilenweise ins
  Auftrags-Log geschrieben und für Fortschritt und Restzeit geparst (`parse_7z`, `parse_fpkg`).
  Abbrechen beendet den laufenden Prozess.
- **Pfadschutz:** Jeder Pfad aus der Oberfläche geht durch `safe_path()` und muss unter einer der
  Wurzeln aus `BROWSE_ROOTS` liegen.
- **Platzplanung:** Jeder Auftrag reserviert seinen geschätzten Bedarf; `free_for()` zieht die
  Reserven anderer aktiver Aufträge auf derselben Platte vom freien Platz ab.
- **Persistenz** in `/config`: `jobs.json` (letzte 200 Aufträge, ohne Passwörter), `settings.json`,
  `checks.json`, `watch_state.json`, `logs/server.log` (rotierend), `logs/jobs/<id>.log`.
  Geschrieben wird atomar (temporäre Datei + `os.replace`).
- **exFAT ohne Mount:** `exfat.py` liest Bootsektor, FAT und Verzeichniseinträge direkt aus der
  Image-Datei (auch hinter MBR/GPT) und kopiert den App-Ordner heraus. Der Container braucht dafür
  keine Privilegien und kein Loop-Device.

### Oberfläche

- Eine einzige Datei `index.html` ohne Framework und ohne Build-Schritt: HTML, ein `<style>`-Block
  mit CSS-Variablen für helles und dunkles Design, ein `<script>`-Block mit Vanilla-JavaScript.
- Kleine Helfer: `$()` für `querySelector`, `api()` / `post()` für `fetch` gegen `/api/…`,
  `human()` für Größen.
- **Polling statt WebSocket:** Auftragsliste alle 3 s, Log des gewählten Auftrags alle 1,2 s,
  Prüfungen alle 1,5 s, Watch-Liste alle 5 s (nur im jeweiligen Reiter).
- Dateibrowser und Bestätigungsdialoge sind native `<dialog>`-Elemente.
- Formularwerte liegen im `localStorage` des Browsers.
- Responsiv: Reiterleiste und Tabellen passen auf schmale Bildschirme.

### Docker-Image

1. Basis `debian:bookworm-slim` + `python3`, `ca-certificates`, `libicu72`, `libssl3`
   (für die .NET-basierte fpkg-cli).
2. **fpkg-cli** aus dem offiziellen Linux-Release, per SHA-256 gepinnt; nur die CLI wird behalten,
   die Desktop-GUI (`app/`) entfernt.
3. **7-Zip** als offizieller statischer Build (`7zzs`, mit RAR-Unterstützung), per SHA-256 gepinnt,
   installiert als `/usr/local/bin/7z`.
4. `app/` nach `/app`, Ordner `/shares`, `/output`, `/work`, `/config` anlegen.
5. Healthcheck: alle 60 s `GET /api/config`.
6. Start: `python3 /app/server.py`.

fpkg-cli und 7-Zip werden nicht im Repository verteilt, sondern beim Build von den offiziellen
Releases geladen.

### CI (GitHub Actions)

Bei jedem Push und Pull Request:

1. **test:** `py_compile` von `server.py` und `exfat.py`, dann `tests/test_basic.py`
   (mit `exfatprogs` für den exFAT-Test).
2. **build:** Image bauen. Nur auf `main` und bei Tags `v*` wird nach
   `ghcr.io/cosmicflow2512/fpkg-webui` gepusht (`latest`, Version, kurzer Commit-Hash).
3. **Smoke-Test** des gepushten Images: `fpkg-cli info` muss „[OK] LibProsperoPkg“ melden,
   `7z i` muss RAR5 kennen.

---

## 17. API-Referenz

Alle Antworten JSON, Fehler als `{"error": "…"}` mit HTTP 4xx/5xx.

| Methode | Pfad | Zweck |
|---|---|---|
| GET | `/api/config` | Version, Standard-Ausgabe/-Arbeitsordner |
| GET | `/api/ls?path=` | Ordnerinhalt für den Dateibrowser; ohne Pfad die Wurzeln |
| GET | `/api/probe?path=&fixes=1` | Quelle erkennen: Art, Teile, Größe, Verschlüsselung, Title-ID, `param.json`, optional Fix-Vorschläge |
| GET | `/api/free?path=` | freier und gesamter Platz |
| GET | `/api/jobs` | alle Aufträge |
| POST | `/api/jobs` | Auftrag anlegen (`source`, `fix`, `out`, `work`, `preset`, `confirm`, `cleanup`, `delete_archive`, `checksum`, `full_verify`, `keep_ampr`, `keep_dump`, `password`) |
| GET | `/api/jobs/<id>/log` | Log-Ausschnitt |
| GET | `/api/jobs/<id>/download` | Log als Datei |
| POST | `/api/jobs/<id>/continue` | Freigabe: bauen |
| POST | `/api/jobs/<id>/cancel` | abbrechen |
| POST | `/api/jobs/<id>/cleanup` | Arbeitsordner löschen |
| POST | `/api/jobs/<id>/delete` | aus der Liste entfernen |
| POST | `/api/jobs/<id>/retry` | neu einreihen |
| POST | `/api/jobs/clear-history` | Verlauf leeren |
| GET | `/api/packages?dir=` | Paketliste |
| GET | `/api/packages/info?path=` | `pkg-info` eines Pakets |
| GET | `/api/checks` | laufende und gespeicherte Prüfungen |
| POST | `/api/checks` | Prüfung starten (`path`, `mode`: `quick`/`full`) |
| POST | `/api/checks/<id>/cancel` | Prüfung abbrechen |
| GET / POST | `/api/settings` | Einstellungen lesen / speichern |
| POST | `/api/settings/pushover-test` | Testnachricht |
| GET | `/api/watch` | Watch-Status und -Liste |
| POST | `/api/watch/scan` | sofort scannen |
| POST | `/api/watch/forget` | Eintrag erneut verarbeiten (`key`) |
| GET | `/api/diag` | Diagnose-Übersicht |
| GET | `/api/diag/selftest` | Selbsttest |
| GET | `/api/diag/log?lines=` | Server-Log |
| GET | `/api/diag/bundle` | Diagnose-Paket (ZIP) |

---

## 18. Entwicklung, Build und Updates

### Lokal starten

```bash
python3 tests/test_basic.py
cd app && DATA_DIR=/tmp/fpkg BROWSE_ROOTS=/pfad/zu/test FPKG_CLI=/pfad/fpkg-cli SEVENZ=7zz python3 server.py
```

Voraussetzungen: Python 3.11+, `fpkg-cli`, 7-Zip.

### fpkg-cli aktualisieren

Im `Dockerfile` **beide** Werte anpassen, sonst bricht der Build an `sha256sum -c` ab:

```dockerfile
ARG FPKG_VERSION=2.2.6
ARG FPKG_SHA256=<sha256 von PSVIETHOA-FPKG-Builder-<version>-Linux-x64.tar.gz>
```

Den Hash aus dem Release berechnen:

```bash
v=2.2.6; curl -fsSL "https://github.com/thanhsondev/PSVIETHOA-FPKG-Builder/releases/download/v$v/PSVIETHOA-FPKG-Builder-$v-Linux-x64.tar.gz" | sha256sum
```

Release-Notes auf geändertes Standardverhalten prüfen (z. B. 2.2.6: PlayGo der Quelle bleibt jetzt
erhalten).

### Updates

Ein Merge auf `main` baut `ghcr.io/cosmicflow2512/fpkg-webui:latest`. Auf Unraid danach im
Docker-Tab beim Container **Force Update** (oder Edit → Apply). `docker pull` + `docker restart`
reicht **nicht**: Ein Container bleibt an das Image gebunden, aus dem er erstellt wurde.

Prüfen:

```bash
docker inspect fpkg-webui --format '{{.Image}}'; docker image inspect ghcr.io/cosmicflow2512/fpkg-webui:latest --format '{{.Id}}'; curl -s http://127.0.0.1:8099/api/config | grep -o '"version": *"[^"]*"'
```

Die beiden Image-IDs müssen gleich sein.
