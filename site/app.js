const VIENNA_TZ = "Europe/Vienna";
const DEFAULT_ZONE = "AT";
const UTILITARIAN_BASE_URL = "https://spot.utilitarian.io";
const WEEKDAY_LABELS = [
    "Montag",
    "Dienstag",
    "Mittwoch",
    "Donnerstag",
    "Freitag",
    "Samstag",
    "Sonntag",
];

const number3 = new Intl.NumberFormat("de-AT", {
    minimumFractionDigits: 3,
    maximumFractionDigits: 3,
});
const number2 = new Intl.NumberFormat("de-AT", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
});
const currency = new Intl.NumberFormat("de-AT", {
    style: "currency",
    currency: "EUR",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
});
const percent2 = new Intl.NumberFormat("de-AT", {
    style: "percent",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
});
const monthLabelFormatter = new Intl.DateTimeFormat("de-AT", {
    timeZone: VIENNA_TZ,
    month: "short",
    year: "numeric",
});
const dayLabelFormatter = new Intl.DateTimeFormat("de-AT", {
    timeZone: VIENNA_TZ,
    day: "2-digit",
    month: "2-digit",
});
const zonedPartsFormatter = new Intl.DateTimeFormat("en-CA", {
    timeZone: VIENNA_TZ,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23",
    timeZoneName: "longOffset",
});

const form = document.querySelector("#calculator-form");
const loadProfileInput = document.querySelector("#load-profile");
const supplierOffsetInput = document.querySelector("#supplier-offset");
const vatRateInput = document.querySelector("#vat-rate");
const calculateButton = document.querySelector("#calculate-button");
const statusNode = document.querySelector("#status");
const resultsNode = document.querySelector("#results");
const kpiGridNode = document.querySelector("#kpi-grid");
const resultSummaryNode = document.querySelector("#result-summary");
const exportActionsNode = document.querySelector("#export-actions");

const chartMounts = {
    monthlyCosts: document.querySelector("#chart-monthly-costs"),
    dailyCosts: document.querySelector("#chart-daily-costs"),
    weekdayCosts: document.querySelector("#chart-weekday-costs"),
    monthlyConsumptionPrice: document.querySelector("#chart-monthly-consumption-price"),
    dailyLoadProfile: document.querySelector("#chart-daily-load-profile"),
};

form.addEventListener("submit", async (event) => {
    event.preventDefault();
    await runCalculation();
});

async function runCalculation() {
    const file = loadProfileInput.files?.[0];
    if (!file) {
        setStatus("Bitte zuerst eine Lastprofil-CSV auswählen.", true);
        return;
    }

    const supplierOffsetCtPerKwh = Number.parseFloat(supplierOffsetInput.value || "0");
    const vatRate = Number.parseFloat(vatRateInput.value || "0") / 100;

    if (Number.isNaN(supplierOffsetCtPerKwh) || Number.isNaN(vatRate)) {
        setStatus("Offset und USt müssen gültige Zahlen sein.", true);
        return;
    }

    setBusy(true);
    clearResults();

    try {
        setStatus("Lastprofil wird eingelesen ...");
        const loadText = await file.text();
        const load = readLoadProfile(loadText);
        if (!load.length) {
            throw new Error("Das Lastprofil enthält keine auswertbaren Zeilen.");
        }

        setStatus("Spotpreise werden direkt von Utilitarian Spot geladen ...");
        let prices = await fetchUtilitarianPrices(DEFAULT_ZONE, load[0].intervalStart, load[load.length - 1].intervalEnd);
        prices = maybeExpandPricesToLoadResolution(load, prices);

        setStatus("Kosten und Diagramme werden berechnet ...");
        const { detail, summary } = calculateCosts(load, prices, supplierOffsetCtPerKwh, vatRate);

        renderResults(detail, summary);
        setStatus("Fertig. Die Berechnung lief komplett lokal im Browser.");
    } catch (error) {
        console.error(error);
        setStatus(error instanceof Error ? error.message : String(error), true);
    } finally {
        setBusy(false);
    }
}

function setBusy(isBusy) {
    calculateButton.disabled = isBusy;
    calculateButton.textContent = isBusy ? "Berechnung läuft ..." : "Berechnung starten";
}

function setStatus(message, isError = false) {
    statusNode.textContent = message;
    statusNode.classList.toggle("error", isError);
}

function clearResults() {
    resultsNode.hidden = true;
    kpiGridNode.innerHTML = "";
    resultSummaryNode.innerHTML = "";
    exportActionsNode.innerHTML = "";
    Object.values(chartMounts).forEach((mount) => {
        mount.innerHTML = "";
    });
}

function detectDelimiter(text) {
    const sample = text.slice(0, 4096);
    const candidates = [";", ",", "\t"];
    let bestDelimiter = ";";
    let bestScore = -1;

    for (const delimiter of candidates) {
        const lines = sample.split(/\r?\n/).filter(Boolean).slice(0, 6);
        const score = lines.reduce((sum, line) => sum + splitCsvLine(line, delimiter).length, 0);
        if (score > bestScore) {
            bestScore = score;
            bestDelimiter = delimiter;
        }
    }

    return bestDelimiter;
}

function splitCsvLine(line, delimiter) {
    const cells = [];
    let current = "";
    let insideQuotes = false;

    for (let index = 0; index < line.length; index += 1) {
        const char = line[index];
        const next = line[index + 1];

        if (char === '"') {
            if (insideQuotes && next === '"') {
                current += '"';
                index += 1;
            } else {
                insideQuotes = !insideQuotes;
            }
            continue;
        }

        if (char === delimiter && !insideQuotes) {
            cells.push(current);
            current = "";
            continue;
        }

        current += char;
    }

    cells.push(current);
    return cells;
}

function parseDelimitedText(text) {
    const delimiter = detectDelimiter(text);
    const lines = text.split(/\r?\n/).filter((line) => line.trim() !== "");
    if (!lines.length) {
        return { columns: [], rows: [] };
    }

    const columns = splitCsvLine(lines[0], delimiter).map((cell) => cell.trim());
    const rows = lines.slice(1).map((line) => {
        const cells = splitCsvLine(line, delimiter);
        const row = {};
        columns.forEach((column, index) => {
            row[column] = (cells[index] ?? "").trim();
        });
        return row;
    });

    return { columns, rows };
}

function normalizeHeader(value) {
    return value.toLowerCase().trim().replaceAll("_", " ");
}

function pickExisting(columns, names) {
    const direct = new Map(columns.map((column) => [normalizeHeader(column), column]));
    for (const name of names) {
        const match = direct.get(normalizeHeader(name));
        if (match) {
            return match;
        }
    }
    return null;
}

function parseFloatLocalized(value) {
    const normalized = String(value ?? "")
        .trim()
        .replaceAll(" ", "")
        .replaceAll("\u00A0", "");

    if (!normalized || ["nan", "none"].includes(normalized.toLowerCase())) {
        return null;
    }

    let text = normalized;
    if (text.includes(",") && text.includes(".")) {
        if (text.lastIndexOf(",") > text.lastIndexOf(".")) {
            text = text.replaceAll(".", "").replace(",", ".");
        } else {
            text = text.replaceAll(",", "");
        }
    } else if (text.includes(",")) {
        text = text.replace(",", ".");
    }

    const parsed = Number.parseFloat(text);
    return Number.isFinite(parsed) ? parsed : null;
}

function intervalToMs(code) {
    const normalized = String(code ?? "QH").trim().toUpperCase();
    if (["QH", "PT15M", "15M", "15MIN"].includes(normalized)) {
        return 15 * 60 * 1000;
    }
    if (["H", "PT60M", "PT1H", "60M", "1H"].includes(normalized)) {
        return 60 * 60 * 1000;
    }
    return 15 * 60 * 1000;
}

function readLoadProfile(text) {
    const { columns, rows } = parseDelimitedText(text);
    const timeColumn = pickExisting(columns, [
        "Ende Ablesezeitraum",
        "Ende ablesezeitraum",
        "Messzeitpunkt",
        "Zeitstempel",
        "timestamp",
        "datetime",
        "Datum",
        "date",
    ]);
    const consumptionColumn = pickExisting(columns, ["Verbrauch [kWh]", "Verbrauch[kWh]", "Verbrauch", "consumption_kwh", "kwh"]);
    const intervalColumn = pickExisting(columns, ["Messintervall", "Messinterval", "resolution", "Intervall"]);

    if (!timeColumn || !consumptionColumn) {
        throw new Error("Zeitstempel und Verbrauch [kWh] konnten im Lastprofil nicht gefunden werden.");
    }

    return rows
        .map((row) => {
            const timestamp = new Date(row[timeColumn]);
            const consumptionKwh = parseFloatLocalized(row[consumptionColumn]) ?? 0;
            const intervalMs = intervalToMs(intervalColumn ? row[intervalColumn] : "QH");

            if (Number.isNaN(timestamp.getTime())) {
                return null;
            }

            const intervalStart = new Date(timestamp.getTime() - intervalMs);
            const intervalEnd = new Date(timestamp.getTime());

            return { intervalStart, intervalEnd, consumptionKwh };
        })
        .filter(Boolean)
        .sort((left, right) => left.intervalStart - right.intervalStart);
}

async function fetchUtilitarianPrices(zone, start, end) {
    const startYear = start.getUTCFullYear();
    const endYear = end.getUTCFullYear();
    const years = [];
    for (let year = startYear; year <= endYear; year += 1) {
        years.push(year);
    }

    const responses = await Promise.all(
        years.map(async (year) => {
            const response = await fetch(`${UTILITARIAN_BASE_URL}/electricity/${zone}/${year}/`, {
                headers: { Accept: "application/json" },
            });
            if (!response.ok) {
                throw new Error(`Preisdaten konnten nicht geladen werden (${response.status} ${response.statusText}).`);
            }
            const data = await response.json();
            if (!Array.isArray(data)) {
                throw new Error(`Unerwartete Antwort von Utilitarian Spot für ${year}.`);
            }
            return data;
        }),
    );

    const deduped = new Map();
    responses.flat().forEach((item) => {
        if (!item?.timestamp || item?.value == null) {
            return;
        }
        const intervalStart = new Date(item.timestamp);
        const priceEurMWh = parseFloatLocalized(item.value);
        if (Number.isNaN(intervalStart.getTime()) || priceEurMWh == null) {
            return;
        }
        deduped.set(intervalStart.getTime(), { intervalStart, priceEurMWh });
    });

    const prices = Array.from(deduped.values()).sort((left, right) => left.intervalStart - right.intervalStart);
    if (!prices.length) {
        throw new Error("Keine Preisdaten von Utilitarian Spot erhalten.");
    }

    const diffs = [];
    for (let index = 1; index < prices.length; index += 1) {
        diffs.push(prices[index].intervalStart.getTime() - prices[index - 1].intervalStart.getTime());
    }
    const inferred = modeNumber(diffs) ?? 60 * 60 * 1000;

    for (let index = 0; index < prices.length; index += 1) {
        prices[index].intervalEnd = new Date(
            index < prices.length - 1
                ? prices[index + 1].intervalStart.getTime()
                : prices[index].intervalStart.getTime() + inferred,
        );
    }

    return prices.filter((price) => price.intervalStart < end && price.intervalEnd > start);
}

function modeNumber(values) {
    if (!values.length) {
        return null;
    }
    const counts = new Map();
    values.forEach((value) => counts.set(value, (counts.get(value) ?? 0) + 1));
    return Array.from(counts.entries()).sort((left, right) => right[1] - left[1])[0][0];
}

function expandPricesToQuarterHour(prices) {
    const expanded = [];

    prices.forEach((price) => {
        const stepMs = price.intervalEnd.getTime() - price.intervalStart.getTime();
        if (stepMs === 15 * 60 * 1000) {
            expanded.push(price);
            return;
        }
        if (![30 * 60 * 1000, 60 * 60 * 1000].includes(stepMs)) {
            throw new Error(`Preisintervall ${stepMs} ms kann nicht auf 15 Minuten abgebildet werden.`);
        }

        const steps = stepMs / (15 * 60 * 1000);
        for (let index = 0; index < steps; index += 1) {
            const intervalStart = new Date(price.intervalStart.getTime() + index * 15 * 60 * 1000);
            const intervalEnd = new Date(intervalStart.getTime() + 15 * 60 * 1000);
            expanded.push({ intervalStart, intervalEnd, priceEurMWh: price.priceEurMWh });
        }
    });

    return expanded;
}

function maybeExpandPricesToLoadResolution(load, prices) {
    if (!load.length || !prices.length) {
        return prices;
    }

    const loadStep = modeNumber(load.map((row) => row.intervalEnd.getTime() - row.intervalStart.getTime()));
    const priceStep = modeNumber(prices.map((row) => row.intervalEnd.getTime() - row.intervalStart.getTime()));
    if (loadStep === 15 * 60 * 1000 && [30 * 60 * 1000, 60 * 60 * 1000].includes(priceStep)) {
        return expandPricesToQuarterHour(prices);
    }
    return prices;
}

function calculateCosts(load, prices, supplierOffsetCtPerKwh, vatRate) {
    const priceMap = new Map(prices.map((price) => [price.intervalStart.getTime(), price.priceEurMWh]));

    const detail = load.map((row) => {
        const priceEurMWh = priceMap.get(row.intervalStart.getTime());
        const priceMissing = priceEurMWh == null;
        const marketCostEur = priceMissing ? 0 : row.consumptionKwh * (priceEurMWh / 1000);
        const supplierOffsetCostEur = row.consumptionKwh * (supplierOffsetCtPerKwh / 100);
        const totalNetEur = marketCostEur + supplierOffsetCostEur;
        const totalGrossEur = totalNetEur * (1 + vatRate);
        const marketPriceCtKwh = priceMissing ? null : priceEurMWh / 10;
        const effectivePriceNetCtKwh = priceMissing ? null : marketPriceCtKwh + supplierOffsetCtPerKwh;
        const effectivePriceGrossCtKwh = priceMissing ? null : effectivePriceNetCtKwh * (1 + vatRate);

        return {
            intervalStart: row.intervalStart,
            intervalEnd: row.intervalEnd,
            consumptionKwh: row.consumptionKwh,
            priceEurMWh,
            priceMissing,
            marketCostEur,
            supplierOffsetCostEur,
            totalNetEur,
            totalGrossEur,
            marketPriceCtKwh,
            effectivePriceNetCtKwh,
            effectivePriceGrossCtKwh,
            pricedConsumptionKwh: priceMissing ? 0 : row.consumptionKwh,
        };
    });

    const consumptionKwh = sum(detail.map((row) => row.consumptionKwh));
    const pricedConsumptionKwh = sum(detail.map((row) => row.pricedConsumptionKwh));
    const weightedAvgEurMWh = pricedConsumptionKwh > 0
        ? sum(detail.filter((row) => !row.priceMissing).map((row) => row.priceEurMWh * row.consumptionKwh)) / pricedConsumptionKwh
        : 0;
    const totalNetOnPricedRows = sum(detail.filter((row) => !row.priceMissing).map((row) => row.totalNetEur));
    const totalGrossOnPricedRows = sum(detail.filter((row) => !row.priceMissing).map((row) => row.totalGrossEur));

    const summary = {
        rows_load: load.length,
        rows_price: prices.length,
        rows_merged: detail.length,
        rows_missing_price: detail.filter((row) => row.priceMissing).length,
        consumption_kwh: consumptionKwh,
        priced_consumption_kwh: pricedConsumptionKwh,
        missing_price_consumption_kwh: consumptionKwh - pricedConsumptionKwh,
        priced_consumption_share: safeDivide(pricedConsumptionKwh, consumptionKwh),
        market_cost_eur: sum(detail.map((row) => row.marketCostEur)),
        supplier_offset_cost_eur: sum(detail.map((row) => row.supplierOffsetCostEur)),
        total_net_eur: sum(detail.map((row) => row.totalNetEur)),
        total_gross_eur: sum(detail.map((row) => row.totalGrossEur)),
        average_market_price_eur_mwh_weighted: weightedAvgEurMWh,
        average_market_price_ct_kwh_weighted: weightedAvgEurMWh / 10,
        average_price_ct_kwh_with_supplier_net: safeDivide(totalNetOnPricedRows * 100, pricedConsumptionKwh),
        average_price_ct_kwh_with_supplier_gross: safeDivide(totalGrossOnPricedRows * 100, pricedConsumptionKwh),
        supplier_offset_ct_kwh: supplierOffsetCtPerKwh,
        vat_rate: vatRate,
    };

    return { detail, summary };
}

function sum(values) {
    return values.reduce((accumulator, value) => accumulator + value, 0);
}

function safeDivide(numerator, denominator) {
    return denominator === 0 ? 0 : numerator / denominator;
}

function renderResults(detail, summary) {
    resultsNode.hidden = false;
    renderKpis(summary);
    renderSummaryTable(summary);
    renderExports(detail, summary);
    renderCharts(detail);
}

function renderKpis(summary) {
    const cards = [
        {
            label: "1) Spotmarktpreis ohne Aufschlag und ohne USt",
            value: `${number3.format(summary.average_market_price_ct_kwh_weighted)} ct/kWh`,
            note: `${number2.format(summary.average_market_price_eur_mwh_weighted)} EUR/MWh`,
        },
        {
            label: "2) Spotmarktpreis plus Lieferanten-Offset, ohne USt",
            value: `${number3.format(summary.average_price_ct_kwh_with_supplier_net)} ct/kWh`,
            note: `inkl. ${number3.format(summary.supplier_offset_ct_kwh)} ct/kWh Aufschlag`,
        },
        {
            label: "3) Spotmarktpreis plus Lieferanten-Offset und USt",
            value: `${number3.format(summary.average_price_ct_kwh_with_supplier_gross)} ct/kWh`,
            note: `inkl. ${(summary.vat_rate * 100).toFixed(1).replace('.', ',')} % USt`,
        },
        {
            label: "Marktpreis-Kosten",
            value: currency.format(summary.market_cost_eur),
            note: "Reiner Spotmarktanteil ohne Lieferanten-Offset und ohne USt",
        },
        {
            label: "Gesamtkosten netto",
            value: currency.format(summary.total_net_eur),
            note: "inkl. Lieferanten-Offset, exkl. USt",
        },
        {
            label: "Gesamtkosten brutto",
            value: currency.format(summary.total_gross_eur),
            note: "inkl. Lieferanten-Offset und USt",
        },
    ];

    kpiGridNode.innerHTML = cards
        .map(
            (card) => `
        <article class="kpi-card">
          <p class="kpi-label">${card.label}</p>
          <p class="kpi-value">${card.value}</p>
          <p class="kpi-note">${card.note}</p>
        </article>
      `,
        )
        .join("");
}

function renderSummaryTable(summary) {
    const rows = [
        {
            label: "Lieferanten-Offset",
            value: currency.format(summary.supplier_offset_cost_eur),
            note: "Aufschlag in ct/kWh netto auf den tatsächlichen Verbrauch und bereits in den Netto-/Brutto-Werten enthalten.",
        },
        {
            label: "Verbrauch gesamt",
            value: `${number3.format(summary.consumption_kwh)} kWh`,
            note: `${number3.format(summary.priced_consumption_kwh)} kWh mit Preis, ${summary.rows_missing_price} Intervalle ohne Preis.`,
        },
        {
            label: "Preisabdeckung",
            value: percent2.format(summary.priced_consumption_share),
            note: `${number3.format(summary.missing_price_consumption_kwh)} kWh liegen in Intervallen ohne geladenen Spotpreis. Diese kWh zählen beim Verbrauch mit, aber nicht in die verbrauchsgewichteten Preiskennzahlen hinein.`,
        },
    ];

    resultSummaryNode.innerHTML = `
    <div class="stat-table">
      ${rows
            .map(
                (row) => `
            <div class="stat-row">
              <strong>${row.label}</strong>
              <span>${row.value}</span>
              <span>${row.note}</span>
            </div>
          `,
            )
            .join("")}
    </div>
  `;
}

function buildReport(summary) {
    return [
        "Ergebnisbericht",
        "===============",
        "",
        `Verbrauch gesamt [kWh]:                    ${number3.format(summary.consumption_kwh)}`,
        `Davon mit Preis [kWh]:                     ${number3.format(summary.priced_consumption_kwh)}`,
        `Davon ohne Preis [kWh]:                    ${number3.format(summary.missing_price_consumption_kwh)}`,
        `Preisabdeckung [%]:                        ${percent2.format(summary.priced_consumption_share)}`,
        `Marktpreis-Kosten [EUR, ohne Aufschlag/USt]: ${number2.format(summary.market_cost_eur)}`,
        `Lieferanten-Offset [EUR]:                  ${number2.format(summary.supplier_offset_cost_eur)}`,
        `Gesamt netto [EUR, inkl. Aufschlag, ohne USt]: ${number2.format(summary.total_net_eur)}`,
        `Gesamt brutto [EUR, inkl. Aufschlag und USt]: ${number2.format(summary.total_gross_eur)}`,
        "",
        "Verbrauchsgewichtete Preise pro kWh",
        "-----------------------------------",
        `1) Spotmarktpreis ohne Aufschlag/USt [EUR/MWh]: ${number2.format(summary.average_market_price_eur_mwh_weighted)}`,
        `1) Spotmarktpreis ohne Aufschlag/USt [ct/kWh]:  ${number3.format(summary.average_market_price_ct_kwh_weighted)}`,
        `2) Spotmarktpreis inkl. Lieferanten-Offset, ohne USt [ct/kWh]: ${number3.format(summary.average_price_ct_kwh_with_supplier_net)}`,
        `3) Spotmarktpreis inkl. Lieferanten-Offset und USt [ct/kWh]:   ${number3.format(summary.average_price_ct_kwh_with_supplier_gross)}`,
        "",
        `Lieferanten-Offset [ct/kWh netto]:         ${number3.format(summary.supplier_offset_ct_kwh)}`,
        `USt-Satz:                                  ${percent2.format(summary.vat_rate)}`,
        `Fehlende Preise [Zeilen]:                  ${summary.rows_missing_price}`,
    ].join("\n");
}

function renderExports(detail, summary) {
    const downloads = [
        {
            label: "Summary JSON laden",
            fileName: "spotstrom_summary.json",
            content: JSON.stringify(summary, null, 2),
            type: "application/json",
        },
        {
            label: "Detail-CSV laden",
            fileName: "spotstrom_detail.csv",
            content: buildDetailCsv(detail),
            type: "text/csv",
        },
        {
            label: "Ergebnisbericht laden",
            fileName: "spotstrom_ergebnisbericht.txt",
            content: buildReport(summary),
            type: "text/plain",
        },
    ];

    exportActionsNode.innerHTML = "";
    downloads.forEach((download) => {
        const blob = new Blob([download.content], { type: `${download.type};charset=utf-8` });
        const url = URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.className = "download-button";
        link.href = url;
        link.download = download.fileName;
        link.textContent = download.label;
        exportActionsNode.append(link);
    });
}

function buildDetailCsv(detail) {
    const headers = [
        "interval_start",
        "interval_end",
        "consumption_kwh",
        "price_eur_mwh",
        "price_missing",
        "market_cost_eur",
        "supplier_offset_cost_eur",
        "total_net_eur",
        "total_gross_eur",
        "market_price_ct_kwh",
        "effective_price_net_ct_kwh",
        "effective_price_gross_ct_kwh",
    ];

    const rows = detail.map((row) => [
        formatViennaTimestamp(row.intervalStart),
        formatViennaTimestamp(row.intervalEnd),
        row.consumptionKwh,
        row.priceEurMWh ?? "",
        row.priceMissing,
        row.marketCostEur,
        row.supplierOffsetCostEur,
        row.totalNetEur,
        row.totalGrossEur,
        row.marketPriceCtKwh ?? "",
        row.effectivePriceNetCtKwh ?? "",
        row.effectivePriceGrossCtKwh ?? "",
    ]);

    return [headers, ...rows]
        .map((row) => row.map((cell) => csvEscape(cell)).join(";"))
        .join("\n");
}

function csvEscape(value) {
    const text = String(value ?? "");
    if (!/[;"\n]/.test(text)) {
        return text;
    }
    return `"${text.replaceAll('"', '""')}"`;
}

function renderCharts(detail) {
    renderMonthlyCostsChart(detail);
    renderDailyCostsChart(detail);
    renderWeekdayCostsChart(detail);
    renderMonthlyConsumptionPriceChart(detail);
    renderDailyLoadProfileChart(detail);
}

function buildMonthlyStats(detail) {
    const grouped = new Map();
    detail.forEach((row) => {
        const key = monthKey(row.intervalStart);
        if (!grouped.has(key)) {
            grouped.set(key, {
                key,
                label: monthLabelFormatter.format(row.intervalStart),
                consumptionKwh: 0,
                marketCostEur: 0,
                supplierOffsetCostEur: 0,
                totalGrossEur: 0,
                totalNetEur: 0,
                weightedMarketPrice: 0,
                pricedConsumptionKwh: 0,
                sortValue: key,
            });
        }
        const item = grouped.get(key);
        item.consumptionKwh += row.consumptionKwh;
        item.marketCostEur += row.marketCostEur;
        item.supplierOffsetCostEur += row.supplierOffsetCostEur;
        item.totalGrossEur += row.totalGrossEur;
        item.totalNetEur += row.totalNetEur;
        item.weightedMarketPrice += (row.priceEurMWh ?? 0) * row.pricedConsumptionKwh;
        item.pricedConsumptionKwh += row.pricedConsumptionKwh;
    });

    return Array.from(grouped.values())
        .sort((left, right) => left.sortValue.localeCompare(right.sortValue))
        .map((item) => ({
            ...item,
            weightedMarketPriceEurMWh: safeDivide(item.weightedMarketPrice, item.pricedConsumptionKwh),
        }));
}

function buildDailyStats(detail) {
    const grouped = new Map();
    detail.forEach((row) => {
        const key = dayKey(row.intervalStart);
        if (!grouped.has(key)) {
            const fields = getViennaFields(row.intervalStart);
            const weekdayIndex = mondayBasedWeekday(fields.year, fields.month, fields.day);
            grouped.set(key, {
                key,
                label: dayLabelFormatter.format(row.intervalStart),
                weekdayIndex,
                weekdayLabel: WEEKDAY_LABELS[weekdayIndex],
                consumptionKwh: 0,
                totalNetEur: 0,
                totalGrossEur: 0,
            });
        }
        const item = grouped.get(key);
        item.consumptionKwh += row.consumptionKwh;
        item.totalNetEur += row.totalNetEur;
        item.totalGrossEur += row.totalGrossEur;
    });
    return Array.from(grouped.values()).sort((left, right) => left.key.localeCompare(right.key));
}

function buildWeekdayStats(detail) {
    const daily = buildDailyStats(detail);
    const grouped = new Map();
    daily.forEach((day) => {
        if (!grouped.has(day.weekdayIndex)) {
            grouped.set(day.weekdayIndex, {
                weekdayIndex: day.weekdayIndex,
                label: day.weekdayLabel,
                avgNetEur: 0,
                avgGrossEur: 0,
                avgConsumptionKwh: 0,
                count: 0,
            });
        }
        const item = grouped.get(day.weekdayIndex);
        item.avgNetEur += day.totalNetEur;
        item.avgGrossEur += day.totalGrossEur;
        item.avgConsumptionKwh += day.consumptionKwh;
        item.count += 1;
    });
    return Array.from(grouped.values())
        .sort((left, right) => left.weekdayIndex - right.weekdayIndex)
        .map((item) => ({
            ...item,
            avgNetEur: safeDivide(item.avgNetEur, item.count),
            avgGrossEur: safeDivide(item.avgGrossEur, item.count),
            avgConsumptionKwh: safeDivide(item.avgConsumptionKwh, item.count),
        }));
}

function buildLoadProfileStats(detail) {
    const grouped = new Map();
    detail.forEach((row) => {
        const fields = getViennaFields(row.intervalStart);
        const key = `${String(fields.hour).padStart(2, "0")}:${String(fields.minute).padStart(2, "0")}`;
        if (!grouped.has(key)) {
            grouped.set(key, []);
        }
        grouped.get(key).push(row.consumptionKwh);
    });

    return Array.from(grouped.entries())
        .sort((left, right) => left[0].localeCompare(right[0]))
        .map(([timeOfDay, values]) => ({
            timeOfDay,
            avgConsumptionKwh: safeDivide(sum(values), values.length),
            p25ConsumptionKwh: quantile(values, 0.25),
            p75ConsumptionKwh: quantile(values, 0.75),
        }));
}

function quantile(values, quantileValue) {
    if (!values.length) {
        return 0;
    }
    const sorted = [...values].sort((left, right) => left - right);
    const position = (sorted.length - 1) * quantileValue;
    const base = Math.floor(position);
    const rest = position - base;
    if (sorted[base + 1] == null) {
        return sorted[base];
    }
    return sorted[base] + rest * (sorted[base + 1] - sorted[base]);
}

function renderMonthlyCostsChart(detail) {
    const monthly = buildMonthlyStats(detail);
    if (!monthly.length) {
        renderEmptyChart(chartMounts.monthlyCosts, "Keine Monatsdaten verfügbar.");
        return;
    }

    renderStackedBarLineChart(chartMounts.monthlyCosts, {
        labels: monthly.map((item) => item.label),
        bottomSeries: monthly.map((item) => item.marketCostEur),
        topSeries: monthly.map((item) => item.supplierOffsetCostEur),
        lineSeries: monthly.map((item) => item.totalGrossEur),
        yLabel: "Kosten [EUR]",
        legend: [
            { label: "Marktpreis", color: "#3b82f6" },
            { label: "Lieferanten-Offset", color: "#f59e0b" },
            { label: "Gesamt brutto", color: "#15803d" },
        ],
    });
}

function renderDailyCostsChart(detail) {
    const daily = buildDailyStats(detail);
    if (!daily.length) {
        renderEmptyChart(chartMounts.dailyCosts, "Keine Tagesdaten verfügbar.");
        return;
    }

    const avgNet = safeDivide(sum(daily.map((item) => item.totalNetEur)), daily.length);
    const avgGross = safeDivide(sum(daily.map((item) => item.totalGrossEur)), daily.length);

    renderLineChart(chartMounts.dailyCosts, {
        labels: daily.map((item) => item.label),
        series: [
            { values: daily.map((item) => item.totalNetEur), color: "#2563eb", label: "Tageskosten netto" },
            { values: daily.map((item) => item.totalGrossEur), color: "#16a34a", label: "Tageskosten brutto" },
            { values: daily.map(() => avgNet), color: "#1d4ed8", label: `Durchschnitt netto: ${number2.format(avgNet)} EUR/Tag`, dashed: true },
            { values: daily.map(() => avgGross), color: "#166534", label: `Durchschnitt brutto: ${number2.format(avgGross)} EUR/Tag`, dashed: true },
        ],
        yLabel: "Kosten [EUR]",
    });
}

function renderWeekdayCostsChart(detail) {
    const weekday = buildWeekdayStats(detail);
    if (!weekday.length) {
        renderEmptyChart(chartMounts.weekdayCosts, "Keine Wochentagsdaten verfügbar.");
        return;
    }

    renderGroupedBarChart(chartMounts.weekdayCosts, {
        labels: weekday.map((item) => item.label),
        series: [
            { values: weekday.map((item) => item.avgNetEur), color: "#0f766e", label: "Ø netto pro Tag" },
            { values: weekday.map((item) => item.avgGrossEur), color: "#7c3aed", label: "Ø brutto pro Tag" },
        ],
        yLabel: "Kosten [EUR]",
    });
}

function renderMonthlyConsumptionPriceChart(detail) {
    const monthly = buildMonthlyStats(detail);
    if (!monthly.length) {
        renderEmptyChart(chartMounts.monthlyConsumptionPrice, "Keine Monatsdaten verfügbar.");
        return;
    }

    renderBarLineDualAxisChart(chartMounts.monthlyConsumptionPrice, {
        labels: monthly.map((item) => item.label),
        barValues: monthly.map((item) => item.consumptionKwh),
        lineValues: monthly.map((item) => item.weightedMarketPriceEurMWh),
        barLabel: "Verbrauch [kWh]",
        lineLabel: "Preis [EUR/MWh]",
        barColor: "#6366f1",
        lineColor: "#dc2626",
        legend: [
            { label: "Verbrauch", color: "#6366f1" },
            { label: "Gewichteter Marktpreis", color: "#dc2626" },
        ],
    });
}

function renderDailyLoadProfileChart(detail) {
    const profile = buildLoadProfileStats(detail);
    if (!profile.length) {
        renderEmptyChart(chartMounts.dailyLoadProfile, "Kein Lastprofil verfügbar.");
        return;
    }

    renderBandLineChart(chartMounts.dailyLoadProfile, {
        labels: profile.map((item) => item.timeOfDay),
        avgValues: profile.map((item) => item.avgConsumptionKwh),
        lowValues: profile.map((item) => item.p25ConsumptionKwh),
        highValues: profile.map((item) => item.p75ConsumptionKwh),
        yLabel: "Verbrauch [kWh pro 15 Min]",
        legend: [
            { label: "Mittlere 50 %", color: "#bfdbfe" },
            { label: "Durchschnitt", color: "#1d4ed8" },
        ],
    });
}

function renderEmptyChart(container, message) {
    container.innerHTML = `<div class="chart-empty">${message}</div>`;
}

function renderStackedBarLineChart(container, config) {
    const maxValue = Math.max(...config.bottomSeries.map((value, index) => value + config.topSeries[index]), ...config.lineSeries, 1);
    const bounds = createBounds();
    const svg = createSvg(bounds.width, bounds.height);
    const yMax = niceMax(maxValue);
    drawYAxis(svg, bounds, yMax, config.yLabel);

    const stepX = bounds.innerWidth / config.labels.length;
    const barWidth = stepX * 0.55;
    const linePoints = [];

    config.labels.forEach((label, index) => {
        const x = bounds.left + index * stepX + stepX / 2;
        const bottomHeight = scaleValue(config.bottomSeries[index], yMax, bounds.innerHeight);
        const topHeight = scaleValue(config.topSeries[index], yMax, bounds.innerHeight);
        const totalY = bounds.top + bounds.innerHeight - bottomHeight - topHeight;

        svg.append(
            rect(x - barWidth / 2, bounds.top + bounds.innerHeight - bottomHeight, barWidth, bottomHeight, { fill: "#3b82f6", rx: 8 }),
            rect(x - barWidth / 2, totalY, barWidth, topHeight, { fill: "#f59e0b", rx: 8 }),
        );

        linePoints.push([x, bounds.top + bounds.innerHeight - scaleValue(config.lineSeries[index], yMax, bounds.innerHeight)]);
    });

    svg.append(polyline(linePoints, { fill: "none", stroke: "#15803d", "stroke-width": 3, "stroke-linejoin": "round", "stroke-linecap": "round" }));
    linePoints.forEach(([x, y]) => svg.append(circle(x, y, 4, { fill: "#15803d" })));
    drawCategoryXAxis(svg, bounds, config.labels);
    mountChart(container, svg, config.legend);
}

function renderLineChart(container, config) {
    const maxValue = Math.max(...config.series.flatMap((series) => series.values), 1);
    const bounds = createBounds();
    const svg = createSvg(bounds.width, bounds.height);
    const yMax = niceMax(maxValue);
    drawYAxis(svg, bounds, yMax, config.yLabel);

    config.series.forEach((series) => {
        const points = series.values.map((value, index) => [
            bounds.left + (index / Math.max(series.values.length - 1, 1)) * bounds.innerWidth,
            bounds.top + bounds.innerHeight - scaleValue(value, yMax, bounds.innerHeight),
        ]);
        svg.append(polyline(points, {
            fill: "none",
            stroke: series.color,
            "stroke-width": series.dashed ? 2 : 2.6,
            "stroke-dasharray": series.dashed ? "8 8" : undefined,
            "stroke-linejoin": "round",
            "stroke-linecap": "round",
        }));
    });

    const tickIndices = makeTickIndices(config.labels.length, 10);
    drawIndexedXAxis(svg, bounds, tickIndices.map((index) => ({ index, label: config.labels[index] })));
    mountChart(container, svg, config.series.map((series) => ({ label: series.label, color: series.color, dashed: series.dashed })));
}

function renderGroupedBarChart(container, config) {
    const maxValue = Math.max(...config.series.flatMap((series) => series.values), 1);
    const bounds = createBounds();
    const svg = createSvg(bounds.width, bounds.height);
    const yMax = niceMax(maxValue);
    drawYAxis(svg, bounds, yMax, config.yLabel);

    const stepX = bounds.innerWidth / config.labels.length;
    const barGroupWidth = stepX * 0.7;
    const singleBarWidth = barGroupWidth / config.series.length;

    config.labels.forEach((label, index) => {
        const groupStart = bounds.left + index * stepX + (stepX - barGroupWidth) / 2;
        config.series.forEach((series, seriesIndex) => {
            const value = series.values[index];
            const barHeight = scaleValue(value, yMax, bounds.innerHeight);
            svg.append(rect(
                groupStart + seriesIndex * singleBarWidth,
                bounds.top + bounds.innerHeight - barHeight,
                singleBarWidth - 4,
                barHeight,
                { fill: series.color, rx: 8 },
            ));
        });
    });

    drawCategoryXAxis(svg, bounds, config.labels);
    mountChart(container, svg, config.series.map((series) => ({ label: series.label, color: series.color })));
}

function renderBarLineDualAxisChart(container, config) {
    const bounds = createBounds();
    const svg = createSvg(bounds.width, bounds.height);
    const leftMax = niceMax(Math.max(...config.barValues, 1));
    const rightMax = niceMax(Math.max(...config.lineValues, 1));

    drawYAxis(svg, bounds, leftMax, config.barLabel, "#4338ca", "left");
    drawYAxis(svg, bounds, rightMax, config.lineLabel, "#b91c1c", "right");

    const stepX = bounds.innerWidth / config.labels.length;
    const barWidth = stepX * 0.58;
    const linePoints = [];

    config.labels.forEach((label, index) => {
        const x = bounds.left + index * stepX + stepX / 2;
        const barHeight = scaleValue(config.barValues[index], leftMax, bounds.innerHeight);
        svg.append(rect(x - barWidth / 2, bounds.top + bounds.innerHeight - barHeight, barWidth, barHeight, { fill: config.barColor, rx: 8, opacity: 0.85 }));
        linePoints.push([x, bounds.top + bounds.innerHeight - scaleValue(config.lineValues[index], rightMax, bounds.innerHeight)]);
    });

    svg.append(polyline(linePoints, { fill: "none", stroke: config.lineColor, "stroke-width": 3, "stroke-linejoin": "round", "stroke-linecap": "round" }));
    linePoints.forEach(([x, y]) => svg.append(circle(x, y, 4, { fill: config.lineColor })));
    drawCategoryXAxis(svg, bounds, config.labels);
    mountChart(container, svg, config.legend);
}

function renderBandLineChart(container, config) {
    const maxValue = Math.max(...config.highValues, ...config.avgValues, 1);
    const bounds = createBounds();
    const svg = createSvg(bounds.width, bounds.height);
    const yMax = niceMax(maxValue);
    drawYAxis(svg, bounds, yMax, config.yLabel);

    const pointsAvg = config.avgValues.map((value, index) => [
        bounds.left + (index / Math.max(config.labels.length - 1, 1)) * bounds.innerWidth,
        bounds.top + bounds.innerHeight - scaleValue(value, yMax, bounds.innerHeight),
    ]);
    const pointsLow = config.lowValues.map((value, index) => [
        bounds.left + (index / Math.max(config.labels.length - 1, 1)) * bounds.innerWidth,
        bounds.top + bounds.innerHeight - scaleValue(value, yMax, bounds.innerHeight),
    ]);
    const pointsHigh = config.highValues.map((value, index) => [
        bounds.left + (index / Math.max(config.labels.length - 1, 1)) * bounds.innerWidth,
        bounds.top + bounds.innerHeight - scaleValue(value, yMax, bounds.innerHeight),
    ]);

    svg.append(path([...pointsHigh, ...[...pointsLow].reverse()], { fill: "#bfdbfe", opacity: 0.75 }));
    svg.append(polyline(pointsAvg, { fill: "none", stroke: "#1d4ed8", "stroke-width": 3, "stroke-linejoin": "round", "stroke-linecap": "round" }));

    const tickIndices = makeTickIndices(config.labels.length, 12);
    drawIndexedXAxis(svg, bounds, tickIndices.map((index) => ({ index, label: config.labels[index] })));
    mountChart(container, svg, config.legend);
}

function mountChart(container, svg, legendItems) {
    container.innerHTML = "";
    svg.classList.add("chart-svg");
    container.append(svg);
    if (legendItems?.length) {
        container.append(buildLegend(legendItems));
    }
}

function buildLegend(items) {
    const legend = document.createElement("div");
    legend.className = "chart-legend";
    items.forEach((item) => {
        const node = document.createElement("span");
        node.className = "legend-item";
        const swatch = document.createElement("span");
        swatch.className = "legend-swatch";
        swatch.style.background = item.dashed ? `linear-gradient(90deg, ${item.color} 55%, transparent 55%)` : item.color;
        node.append(swatch, document.createTextNode(item.label));
        legend.append(node);
    });
    return legend;
}

function createBounds() {
    const width = 960;
    const height = 360;
    const left = 74;
    const right = 28;
    const top = 34;
    const bottom = 64;
    return {
        width,
        height,
        left,
        right,
        top,
        bottom,
        innerWidth: width - left - right,
        innerHeight: height - top - bottom,
    };
}

function createSvg(width, height) {
    return element("svg", { viewBox: `0 0 ${width} ${height}`, xmlns: "http://www.w3.org/2000/svg" });
}

function drawYAxis(svg, bounds, yMax, label, color = "#455a57", side = "left") {
    const ticks = 5;
    for (let tick = 0; tick <= ticks; tick += 1) {
        const value = (yMax / ticks) * tick;
        const y = bounds.top + bounds.innerHeight - (tick / ticks) * bounds.innerHeight;
        svg.append(line(bounds.left, y, bounds.width - bounds.right, y, { stroke: "rgba(31,42,42,0.12)", "stroke-width": 1 }));
        const x = side === "left" ? bounds.left - 10 : bounds.width - bounds.right + 10;
        const anchor = side === "left" ? "end" : "start";
        svg.append(textNode(x, y + 4, number2.format(value), { fill: color, "font-size": 12, "text-anchor": anchor }));
    }

    const axisX = side === "left" ? bounds.left : bounds.width - bounds.right;
    svg.append(line(axisX, bounds.top, axisX, bounds.top + bounds.innerHeight, { stroke: "rgba(31,42,42,0.18)", "stroke-width": 1.2 }));
    svg.append(textNode(side === "left" ? 18 : bounds.width - 18, bounds.top - 16, label, {
        fill: color,
        "font-size": 12,
        "text-anchor": side === "left" ? "start" : "end",
        "font-weight": 700,
    }));
}

function drawCategoryXAxis(svg, bounds, labels) {
    const stepX = bounds.innerWidth / labels.length;
    labels.forEach((label, index) => {
        const x = bounds.left + index * stepX + stepX / 2;
        svg.append(textNode(x, bounds.top + bounds.innerHeight + 24, label, {
            fill: "#526460",
            "font-size": 11,
            "text-anchor": "middle",
        }));
    });
}

function drawIndexedXAxis(svg, bounds, ticks) {
    ticks.forEach((tick) => {
        const x = bounds.left + (tick.index / Math.max(ticks[ticks.length - 1].index || 1, 1)) * bounds.innerWidth;
        svg.append(textNode(x, bounds.top + bounds.innerHeight + 24, tick.label, {
            fill: "#526460",
            "font-size": 11,
            "text-anchor": "middle",
        }));
    });
}

function makeTickIndices(length, desiredCount) {
    const count = Math.min(desiredCount, length);
    const step = Math.max(1, Math.floor(length / count));
    const ticks = [];
    for (let index = 0; index < length; index += step) {
        ticks.push(index);
    }
    if (ticks[ticks.length - 1] !== length - 1) {
        ticks.push(length - 1);
    }
    return ticks;
}

function scaleValue(value, yMax, height) {
    return (Math.max(value, 0) / yMax) * height;
}

function niceMax(value) {
    if (value <= 0) {
        return 1;
    }
    const exponent = 10 ** Math.floor(Math.log10(value));
    const fraction = value / exponent;
    if (fraction <= 1) {
        return exponent;
    }
    if (fraction <= 2) {
        return 2 * exponent;
    }
    if (fraction <= 5) {
        return 5 * exponent;
    }
    return 10 * exponent;
}

function element(tagName, attributes = {}) {
    const node = document.createElementNS("http://www.w3.org/2000/svg", tagName);
    Object.entries(attributes).forEach(([key, value]) => {
        if (value != null) {
            node.setAttribute(key, String(value));
        }
    });
    return node;
}

function textNode(x, y, value, attributes = {}) {
    const node = element("text", { x, y, ...attributes });
    node.textContent = value;
    return node;
}

function line(x1, y1, x2, y2, attributes = {}) {
    return element("line", { x1, y1, x2, y2, ...attributes });
}

function rect(x, y, width, height, attributes = {}) {
    return element("rect", { x, y, width, height, ...attributes });
}

function circle(cx, cy, r, attributes = {}) {
    return element("circle", { cx, cy, r, ...attributes });
}

function polyline(points, attributes = {}) {
    return element("polyline", { points: points.map((point) => point.join(",")).join(" "), ...attributes });
}

function path(points, attributes = {}) {
    const d = points.map((point, index) => `${index === 0 ? "M" : "L"} ${point[0]} ${point[1]}`).join(" ") + " Z";
    return element("path", { d, ...attributes });
}

function monthKey(date) {
    const fields = getViennaFields(date);
    return `${fields.year}-${String(fields.month).padStart(2, "0")}`;
}

function dayKey(date) {
    const fields = getViennaFields(date);
    return `${fields.year}-${String(fields.month).padStart(2, "0")}-${String(fields.day).padStart(2, "0")}`;
}

function getViennaFields(date) {
    const parts = Object.fromEntries(
        zonedPartsFormatter
            .formatToParts(date)
            .filter((part) => part.type !== "literal")
            .map((part) => [part.type, part.value]),
    );
    return {
        year: Number.parseInt(parts.year, 10),
        month: Number.parseInt(parts.month, 10),
        day: Number.parseInt(parts.day, 10),
        hour: Number.parseInt(parts.hour, 10),
        minute: Number.parseInt(parts.minute, 10),
        second: Number.parseInt(parts.second, 10),
        offset: normalizeOffset(parts.timeZoneName),
    };
}

function formatViennaTimestamp(date) {
    const fields = getViennaFields(date);
    return `${fields.year}-${String(fields.month).padStart(2, "0")}-${String(fields.day).padStart(2, "0")}T${String(fields.hour).padStart(2, "0")}:${String(fields.minute).padStart(2, "0")}:${String(fields.second).padStart(2, "0")}${fields.offset}`;
}

function normalizeOffset(offsetValue) {
    if (!offsetValue || offsetValue === "GMT") {
        return "+00:00";
    }
    const raw = offsetValue.replace("GMT", "");
    const match = raw.match(/^([+-])(\d{1,2})(?::?(\d{2}))?$/);
    if (!match) {
        return raw;
    }
    const [, sign, hours, minutes = "00"] = match;
    return `${sign}${hours.padStart(2, "0")}:${minutes}`;
}

function mondayBasedWeekday(year, month, day) {
    const jsWeekday = new Date(Date.UTC(year, month - 1, day)).getUTCDay();
    return (jsWeekday + 6) % 7;
}