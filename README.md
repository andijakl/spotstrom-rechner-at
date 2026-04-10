# Spotstrom-Rechner AT

Dieses Projekt enthält zwei Teile:

- das Python-Skript [spot_tariff_cost_calc_utilitarian.py](spot_tariff_cost_calc_utilitarian.py) für lokale Batch-Auswertungen
- die statische Webanwendung in [site/index.html](site/index.html), die komplett im Browser läuft und für GitHub Pages vorbereitet ist

Der Fokus liegt auf der öffentlichen Utilitarian-Spot-API für Österreich (`AT`).

## Webanwendung

Die GitHub-Pages-Version bietet:

- Upload eines österreichischen Lastprofils direkt im Browser
- Eingabe des Lieferanten-Offset; die USt liegt in den erweiterten Einstellungen und ist standardmäßig auf 20 % gesetzt
- Lokale Verarbeitung des Lastprofils ohne Upload an einen eigenen Server
- Laden der öffentlichen Day-Ahead-Preise direkt von Utilitarian Spot
- Verbrauchtgewichtete Preisniveaus für:
  - reinen Spotmarktpreis ohne Aufschlag und ohne USt
  - Spotmarktpreis inklusive Lieferanten-Offset, ohne USt
  - Spotmarktpreis inklusive Lieferanten-Offset und USt
- Diagramme für Monatskosten, Tageskosten, Wochentage, Verbrauch vs. Marktpreis
  und ein durchschnittliches Tages-Lastprofil
- Download von Summary-JSON, Detail-CSV und Ergebnisbericht als Textdatei
- Hinweise direkt auf der Seite, wie das Lastprofil etwa bei Netz NÖ oder über e-control bereitgestellt wird

Die Weboberfläche geht davon aus, dass der Zeitstempel das Ende des Ablesezeitraums beschreibt, wie es im e-control Tarifkalkulator-Format üblich ist. Der Datumsbereich wird automatisch aus der hochgeladenen Datei erkannt.

## Python-Skript

- Einlesen österreichischer Lastprofil-CSV-Dateien mit 15-Minuten- oder Stundenintervallen
- Abruf historischer Day-Ahead-Preise über Utilitarian Spot oder Einlesen einer Preisdatei
- Automatische Anpassung von Stundenpreisen auf Viertelstunden bei Bedarf
- Berechnung klar getrennter, verbrauchsgewichteter Preisniveaus pro kWh:
  - reiner Spotmarktpreis ohne Aufschlag und ohne USt
  - Spotmarktpreis inklusive Lieferanten-Offset, ohne USt
  - Spotmarktpreis inklusive Lieferanten-Offset und USt
- Export von Detaildaten als CSV, Zusammenfassung als JSON und Ergebnisbericht als Textdatei
- Erstellung von Diagrammen für Monatskosten, Tageskosten, Wochentagsdurchschnitte,
  Verbrauch vs. Marktpreis sowie ein durchschnittliches Tages-Lastprofil

## Voraussetzungen für das Python-Skript

- Python 3.14 oder kompatibel
- Eine virtuelle Umgebung, zum Beispiel `.venv`

Installation der Abhängigkeiten:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

## Nutzung des Python-Skripts

Beispiel mit Utilitarian Spot:

```bash
python3 spot_tariff_cost_calc_utilitarian.py \
  --load-profile 2025-Lastprofil.csv \
  --supplier-offset-ct-per-kwh 1.49 \
  --vat-rate 0.20 \
  --output-prefix 2025_spot
```

Wichtige Optionen:

- `--price-csv`: lokale Preisdatei statt API-Abruf verwenden
- `--expand-hourly-to-quarter-hour`: Stundenpreise explizit auf Viertelstunden erweitern
- `--disable-auto-expand`: automatische Erweiterung deaktivieren
- `--drop-missing-prices`: Zeilen ohne Preis vor Export entfernen
- `--skip-plots`: keine Diagramme erzeugen

## Ausgabe des Python-Skripts

Bei `--output-prefix 2025_spot` werden folgende Dateien erzeugt:

- `2025_spot_detail.csv`
- `2025_spot_summary.json`
- `2025_spot_results.txt`
- `2025_spot_monthly_costs.png`
- `2025_spot_daily_costs.png`
- `2025_spot_weekday_average_costs.png`
- `2025_spot_monthly_consumption_price.png`
- `2025_spot_average_daily_load_profile.png`

## GitHub Pages Deployment

Die Weboberfläche liegt in [site/index.html](site/index.html). Der Workflow in [.github/workflows/deploy-pages.yml](.github/workflows/deploy-pages.yml) veröffentlicht genau diesen Ordner auf GitHub Pages.

Empfohlener Repository-Name: `spotstrom-rechner-at`

Typischer Ablauf:

```bash
git init -b main
git add .
git commit -m "Initial commit"
gh repo create andijakl/spotstrom-rechner-at --public --source=. --remote=origin --push
```

Danach kann GitHub Pages über den Workflow bereitgestellt werden.

## Hinweise

- Netzgebühren, Abgaben, Grundpreise, Boni und tarifliche Sonderlogiken sind nicht enthalten.
- Fehlende Preiszeilen werden im Report ausgewiesen.
- Die ausgewiesenen Preise pro kWh sind verbrauchsgewichtet und werden nur über Intervalle mit verfügbarem Preis gebildet; die Preisabdeckung wird separat ausgewiesen.
- Das Skript verarbeitet gemischte Zeitzonen-Offsets im Lastprofil korrekt, etwa bei Sommer-/Winterzeitwechseln.
- Für die Webanwendung werden nur öffentliche Preisdaten von Utilitarian Spot geladen; das hochgeladene Lastprofil bleibt lokal im Browser.

## Lizenz

Dieses Projekt steht unter der MIT-Lizenz. Details stehen in der Datei [LICENSE](LICENSE).