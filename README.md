# Spotstrom-Rechner AT

Dieses Projekt enthaelt zwei Teile:

- das Python-Skript [spot_tariff_cost_calc_utilitarian.py](spot_tariff_cost_calc_utilitarian.py) fuer lokale Batch-Auswertungen
- die statische Webanwendung in [site/index.html](site/index.html), die komplett im Browser laeuft und fuer GitHub Pages vorbereitet ist

Der Fokus liegt auf der oeffentlichen Utilitarian-Spot-API fuer Oesterreich (`AT`).

## Webanwendung

Die GitHub-Pages-Version bietet:

- Upload eines oesterreichischen Lastprofils direkt im Browser
- Eingabe von Lieferanten-Offset und USt, standardmaessig 20 %
- Lokale Verarbeitung des Lastprofils ohne Upload an einen eigenen Server
- Laden der oeffentlichen Day-Ahead-Preise direkt von Utilitarian Spot
- Verbrauchtgewichtete Preisniveaus fuer:
  - reinen Spotmarktpreis ohne Aufschlag und ohne USt
  - Spotmarktpreis inklusive Lieferanten-Offset, ohne USt
  - Spotmarktpreis inklusive Lieferanten-Offset und USt
- Diagramme fuer Monatskosten, Tageskosten, Wochentage, Verbrauch vs. Marktpreis
  und ein durchschnittliches Tages-Lastprofil
- Download von Summary-JSON, Detail-CSV und Ergebnisbericht als Textdatei

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

## Voraussetzungen fuer das Python-Skript

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
- `--timestamp-is start|end`: Interpretation des Messzeitpunkts im Lastprofil
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

Die Weboberflaeche liegt in [site/index.html](site/index.html). Der Workflow in [.github/workflows/deploy-pages.yml](.github/workflows/deploy-pages.yml) veroeffentlicht genau diesen Ordner auf GitHub Pages.

Empfohlener Repository-Name: `spotstrom-rechner-at`

Typischer Ablauf:

```bash
git init -b main
git add .
git commit -m "Initial commit"
gh repo create andijakl/spotstrom-rechner-at --public --source=. --remote=origin --push
```

Danach kann GitHub Pages ueber den Workflow bereitgestellt werden.

## Hinweise

- Netzgebühren, Abgaben, Grundpreise, Boni und tarifliche Sonderlogiken sind nicht enthalten.
- Fehlende Preiszeilen werden im Report ausgewiesen.
- Die ausgewiesenen Preise pro kWh sind verbrauchsgewichtet und werden nur ueber Intervalle mit verfuegbarem Preis gebildet; die Preisabdeckung wird separat ausgewiesen.
- Das Skript verarbeitet gemischte Zeitzonen-Offsets im Lastprofil korrekt, etwa bei Sommer-/Winterzeitwechseln.
- Fuer die Webanwendung werden nur oeffentliche Preisdaten von Utilitarian Spot geladen; das hochgeladene Lastprofil bleibt lokal im Browser.