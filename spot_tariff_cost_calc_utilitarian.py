#!/usr/bin/env python3
"""
Berechnet historische Energiekosten auf Basis eines Lastprofils und
Day-Ahead-Preisen über Utilitarian Spot (ohne API-Key).

Funktionen:
- Liest österreichische Smart-Meter-Lastprofile im Stil des Beispiels des Nutzers
  (z. B. 'Messzeitpunkt;Messinterval;Abrechnungsmaßeinheit;Verbrauch [kWh];').
- Lädt Day-Ahead-Preise über die öffentliche Utilitarian-Spot-API
  ODER liest alternativ eine lokale Preis-CSV ein.
- Unterstützt stündliche und 15-minütige Preisreihen.
- Kann Stundenpreise automatisch auf Viertelstunden erweitern.
- Berücksichtigt optional einen Lieferanten-Offset in ct/kWh netto
  (z. B. 1.49 ct/kWh exkl. USt.) sowie USt.
- Schreibt Detail-CSV + Summary-JSON.
- Schreibt zusätzlich einen lesbaren Ergebnisbericht als Textdatei.
- Erstellt optional Diagramme für Monatskosten, Tageskosten, Wochentagsdurchschnitte
    und ein durchschnittliches Tages-Lastprofil.

Hinweis:
- Netzgebühren, Abgaben, Grundgebühr, Boni oder andere Tariflogiken sind
  NICHT automatisch enthalten.
- Das Skript berechnet standardmäßig den Spotmarktpreis plus optionalen
  Lieferanten-Offset.

Beispiel-Aufruf:
python3 spot_tariff_cost_calc_utilitarian.py \
  --load-profile 2025-Lastprofil.csv \
  --supplier-offset-ct-per-kwh 1.49 \
  --vat-rate 0.20 \
  --output-prefix 2025_spot

"""

from __future__ import annotations

import argparse
import csv
import importlib
import io
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pandas as pd

VIENNA_TZ = "Europe/Vienna"
UTILITARIAN_BASE_URL = "https://spot.utilitarian.io"
DEFAULT_ZONE = "AT"


@dataclass
class Summary:
    rows_load: int
    rows_price: int
    rows_merged: int
    rows_missing_price: int
    consumption_kwh: float
    priced_consumption_kwh: float
    missing_price_consumption_kwh: float
    priced_consumption_share: float
    market_cost_eur: float
    supplier_offset_cost_eur: float
    total_net_eur: float
    total_gross_eur: float
    average_market_price_eur_mwh_weighted: float
    average_market_price_ct_kwh_weighted: float
    average_price_ct_kwh_with_supplier_net: float
    average_price_ct_kwh_with_supplier_gross: float
    supplier_offset_ct_kwh: float
    vat_rate: float


# ---------- Hilfsfunktionen ----------

def _pick_existing(df: pd.DataFrame, names: list[str]) -> Optional[str]:
    lower_map = {c.lower().strip(): c for c in df.columns}
    for name in names:
        if name.lower().strip() in lower_map:
            return lower_map[name.lower().strip()]
    for c in df.columns:
        normalized = c.lower().strip().replace("_", " ")
        for name in names:
            if normalized == name.lower().strip().replace("_", " "):
                return c
    return None


def _normalize_datetime_series(series: pd.Series, assume_tz: str = VIENNA_TZ) -> pd.Series:
    def parse_one(value: object) -> Any:
        text = str(value).strip()
        if text == "" or text.lower() in {"nan", "none", "nat"}:
            return pd.NaT

        try:
            ts = pd.Timestamp(text)
        except (TypeError, ValueError):
            return pd.NaT

        if ts.tzinfo is None:
            ts = pd.DatetimeIndex([ts]).tz_localize(
                assume_tz,
                ambiguous="infer",
                nonexistent="shift_forward",
            )[0]

        return ts.tz_convert(VIENNA_TZ)

    normalized = [parse_one(value) for value in series]
    return pd.Series(pd.DatetimeIndex(normalized), index=series.index, name=series.name)


def _read_delimited_auto(path: Path) -> pd.DataFrame:
    text = path.read_text(encoding="utf-8-sig")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=";,\t,")
        sep = dialect.delimiter
    except csv.Error:
        sep = ";"
    return pd.read_csv(io.StringIO(text), sep=sep, dtype=str)


def _parse_float_series(series: pd.Series) -> pd.Series:
    def parse_one(value: object) -> float | None:
        s = str(value).strip().replace(" ", "").replace("\u00a0", "")
        if s == "" or s.lower() in {"nan", "none"}:
            return None

        # Beide Separatoren vorhanden -> letzter Separator ist Dezimaltrennzeichen.
        if "," in s and "." in s:
            if s.rfind(",") > s.rfind("."):
                s = s.replace(".", "")
                s = s.replace(",", ".")
            else:
                s = s.replace(",", "")
        elif "," in s:
            s = s.replace(",", ".")

        try:
            return float(s)
        except ValueError:
            return None

    return series.apply(parse_one).astype(float)


# ---------- Lastprofil ----------

def read_load_profile(path: Path, timestamp_is: str = "end") -> pd.DataFrame:
    df = _read_delimited_auto(path)

    time_col = _pick_existing(df, [
        "Ende Ablesezeitraum", "Ende ablesezeitraum", "Messzeitpunkt", "Zeitstempel", "timestamp", "datetime", "Datum", "date"
    ])
    consumption_col = _pick_existing(df, [
        "Verbrauch [kWh]", "Verbrauch[kWh]", "Verbrauch", "consumption_kwh", "kwh"
    ])
    interval_col = _pick_existing(df, [
        "Messintervall", "Messinterval", "resolution", "Intervall"
    ])

    if time_col is None or consumption_col is None:
        raise ValueError(
            "Konnte die benötigten Spalten im Lastprofil nicht finden. "
            "Erwartet werden mindestens Zeitstempel und Verbrauch [kWh]."
        )

    out = pd.DataFrame()
    out["timestamp"] = _normalize_datetime_series(df[time_col])
    out["consumption_kwh"] = _parse_float_series(df[consumption_col]).fillna(0.0)

    if interval_col is not None:
        interval_values = df[interval_col].astype(str).str.strip().str.upper()
    else:
        interval_values = pd.Series(["QH"] * len(df))

    def interval_to_timedelta(code: str) -> pd.Timedelta:
        code = str(code).upper().strip()
        if code in {"QH", "PT15M", "15M", "15MIN"}:
            return pd.Timedelta(minutes=15)
        if code in {"H", "PT60M", "PT1H", "60M", "1H"}:
            return pd.Timedelta(hours=1)
        return pd.Timedelta(minutes=15)

    out["interval"] = interval_values.apply(interval_to_timedelta)

    if timestamp_is == "end":
        out["interval_end"] = out["timestamp"]
        out["interval_start"] = out["timestamp"] - out["interval"]
    elif timestamp_is == "start":
        out["interval_start"] = out["timestamp"]
        out["interval_end"] = out["timestamp"] + out["interval"]
    else:
        raise ValueError("timestamp_is muss 'start' oder 'end' sein")

    out = out[["interval_start", "interval_end", "consumption_kwh"]].sort_values("interval_start")
    out = out.reset_index(drop=True)
    return out


# ---------- Preisdatei ----------

def read_price_csv(path: Path) -> pd.DataFrame:
    df = _read_delimited_auto(path)

    start_col = _pick_existing(df, [
        "interval_start", "start", "timestamp", "datetime", "time", "from", "von"
    ])
    end_col = _pick_existing(df, [
        "interval_end", "end", "to", "bis"
    ])
    price_col = _pick_existing(df, [
        "price_eur_mwh", "price", "marketprice", "market_price", "preis", "spotpreis", "eur/mwh", "value"
    ])
    resolution_col = _pick_existing(df, [
        "resolution", "Messinterval", "Intervall"
    ])

    if start_col is None or price_col is None:
        raise ValueError(
            "Konnte Preisdatei nicht interpretieren. Erwartet werden mindestens "
            "eine Zeitspalte und eine Preisspalte."
        )

    out = pd.DataFrame()
    out["interval_start"] = _normalize_datetime_series(df[start_col])
    out["price_eur_mwh"] = _parse_float_series(df[price_col])

    if end_col is not None:
        out["interval_end"] = _normalize_datetime_series(df[end_col])
    else:
        if resolution_col is not None:
            res = df[resolution_col].astype(str).str.upper().str.strip()
            delta = res.map({
                "PT15M": pd.Timedelta(minutes=15),
                "QH": pd.Timedelta(minutes=15),
                "PT60M": pd.Timedelta(hours=1),
                "PT1H": pd.Timedelta(hours=1),
                "H": pd.Timedelta(hours=1)
            })
            delta = delta.fillna(pd.Timedelta(minutes=15))
            out["interval_end"] = out["interval_start"] + delta
        else:
            diffs = out["interval_start"].sort_values().diff().dropna()
            inferred = diffs.mode().iloc[0] if not diffs.empty else pd.Timedelta(minutes=15)
            out["interval_end"] = out["interval_start"] + inferred

    out = out[["interval_start", "interval_end", "price_eur_mwh"]].dropna(subset=["interval_start", "price_eur_mwh"])
    out = out.sort_values("interval_start").drop_duplicates(subset=["interval_start"], keep="last").reset_index(drop=True)
    return out


# ---------- Utilitarian Spot ----------

def fetch_utilitarian_prices(zone: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    start_utc = start.tz_convert("UTC")
    end_utc = end.tz_convert("UTC")
    years = range(start_utc.year, end_utc.year + 1)

    all_rows: list[dict] = []
    for year in years:
        url = f"{UTILITARIAN_BASE_URL}/electricity/{zone}/{year}/"
        request = Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "historic-flexible-energy-tariff-calculator/1.0",
            },
        )
        try:
            with urlopen(request, timeout=60) as resp:
                data = json.load(resp)
        except HTTPError as exc:
            raise RuntimeError(f"HTTP-Fehler beim Abrufen von Utilitarian Spot: {exc.code} {exc.reason}") from exc
        except URLError as exc:
            raise RuntimeError(f"Netzwerkfehler beim Abrufen von Utilitarian Spot: {exc.reason}") from exc

        if not isinstance(data, list):
            raise RuntimeError(f"Unerwartete Antwort von Utilitarian Spot für Jahr {year}: {data!r}")

        for item in data:
            ts = item.get("timestamp")
            value = item.get("value")
            if ts is None or value is None:
                continue
            all_rows.append(
                {
                    "interval_start": pd.Timestamp(ts).tz_convert(VIENNA_TZ),
                    "price_eur_mwh": float(str(value).replace(",", ".")),
                }
            )

    if not all_rows:
        raise RuntimeError("Keine Preisdaten von Utilitarian Spot erhalten.")

    prices = pd.DataFrame(all_rows)
    prices = prices.sort_values("interval_start").drop_duplicates(subset=["interval_start"], keep="last").reset_index(drop=True)

    # Intervallende aus dem jeweils nächsten Zeitstempel ableiten.
    prices["interval_end"] = prices["interval_start"].shift(-1)
    if len(prices) >= 2:
        inferred = (prices["interval_start"].diff().dropna().mode().iloc[0])
    else:
        inferred = pd.Timedelta(hours=1)
    prices["interval_end"] = prices["interval_end"].fillna(prices["interval_start"] + inferred)

    # Nur tatsächlich benötigten Bereich behalten.
    prices = prices[(prices["interval_start"] < end) & (prices["interval_end"] > start)].copy()
    prices = prices.reset_index(drop=True)
    return prices[["interval_start", "interval_end", "price_eur_mwh"]]


def expand_prices_to_quarter_hour(prices: pd.DataFrame) -> pd.DataFrame:
    prices = prices.copy().sort_values("interval_start").reset_index(drop=True)
    diffs = prices["interval_end"] - prices["interval_start"]
    if diffs.empty:
        return prices

    unique_diffs = set(diffs.dropna().unique())
    if unique_diffs == {pd.Timedelta(minutes=15)}:
        return prices

    rows = []
    for _, row in prices.iterrows():
        step = row["interval_end"] - row["interval_start"]
        if step == pd.Timedelta(minutes=15):
            rows.append(row.to_dict())
        elif step == pd.Timedelta(minutes=30):
            for i in range(2):
                s = row["interval_start"] + i * pd.Timedelta(minutes=15)
                e = s + pd.Timedelta(minutes=15)
                rows.append({"interval_start": s, "interval_end": e, "price_eur_mwh": row["price_eur_mwh"]})
        elif step == pd.Timedelta(hours=1):
            for i in range(4):
                s = row["interval_start"] + i * pd.Timedelta(minutes=15)
                e = s + pd.Timedelta(minutes=15)
                rows.append({"interval_start": s, "interval_end": e, "price_eur_mwh": row["price_eur_mwh"]})
        else:
            raise ValueError(f"Kann Preisintervall {step} nicht auf 15 Minuten abbilden.")

    return pd.DataFrame(rows).sort_values("interval_start").reset_index(drop=True)


def maybe_expand_prices_to_load_resolution(load: pd.DataFrame, prices: pd.DataFrame, disable_auto_expand: bool) -> pd.DataFrame:
    if disable_auto_expand:
        return prices

    if load.empty or prices.empty:
        return prices

    load_step = (load["interval_end"] - load["interval_start"]).mode().iloc[0]
    price_step = (prices["interval_end"] - prices["interval_start"]).mode().iloc[0]

    if load_step == pd.Timedelta(minutes=15) and price_step in {pd.Timedelta(hours=1), pd.Timedelta(minutes=30)}:
        return expand_prices_to_quarter_hour(prices)
    return prices


def _import_matplotlib_pyplot():
    try:
        matplotlib = importlib.import_module("matplotlib")
        matplotlib.use("Agg")
        plt = importlib.import_module("matplotlib.pyplot")
    except ModuleNotFoundError as exc:  # pragma: no cover
        raise RuntimeError(
            "matplotlib ist nicht installiert. Bitte 'pip install matplotlib' ausführen oder --skip-plots verwenden."
        ) from exc

    return plt


def write_plots(detail: pd.DataFrame, output_prefix: Path) -> list[Path]:
    if detail.empty:
        return []

    plt = _import_matplotlib_pyplot()

    plot_df = detail.copy()
    local_start = plot_df["interval_start"].dt.tz_convert(VIENNA_TZ).dt.tz_localize(None)
    plot_df["day"] = local_start.dt.floor("D")
    plot_df["month"] = local_start.dt.to_period("M").astype(str)
    plot_df["weekday"] = local_start.dt.dayofweek
    plot_df["weekday_name"] = local_start.dt.day_name()
    plot_df["priced_consumption_kwh"] = plot_df["consumption_kwh"].where(~plot_df["price_missing"], 0.0)
    plot_df["weighted_market_price"] = plot_df["price_eur_mwh"].fillna(0.0) * plot_df["priced_consumption_kwh"]

    generated_paths: list[Path] = []

    def save_figure(fig, suffix: str) -> None:
        path = output_prefix.with_name(output_prefix.name + suffix + ".png")
        fig.tight_layout()
        fig.savefig(path, dpi=160, bbox_inches="tight")
        plt.close(fig)
        generated_paths.append(path)

    monthly = (
        plot_df.groupby("month", sort=True)
        .agg(
            consumption_kwh=("consumption_kwh", "sum"),
            market_cost_eur=("market_cost_eur", "sum"),
            supplier_offset_cost_eur=("supplier_offset_cost_eur", "sum"),
            total_net_eur=("total_net_eur", "sum"),
            total_gross_eur=("total_gross_eur", "sum"),
            priced_consumption_kwh=("priced_consumption_kwh", "sum"),
            weighted_market_price=("weighted_market_price", "sum"),
        )
        .reset_index()
    )
    monthly["weighted_market_price_eur_mwh"] = (
        monthly["weighted_market_price"] / monthly["priced_consumption_kwh"].where(monthly["priced_consumption_kwh"] > 0)
    )

    fig, ax = plt.subplots(figsize=(11, 5))
    ax.bar(monthly["month"], monthly["market_cost_eur"], label="Marktpreis", color="#3b82f6")
    ax.bar(
        monthly["month"],
        monthly["supplier_offset_cost_eur"],
        bottom=monthly["market_cost_eur"],
        label="Lieferanten-Offset",
        color="#f59e0b",
    )
    ax.plot(monthly["month"], monthly["total_gross_eur"], color="#15803d", marker="o", linewidth=2, label="Gesamt brutto")
    ax.set_title("Monatliche Energiekosten")
    ax.set_xlabel("Monat")
    ax.set_ylabel("Kosten [EUR]")
    ax.tick_params(axis="x", rotation=45)
    ax.grid(axis="y", alpha=0.25)
    ax.legend()
    save_figure(fig, "_monthly_costs")

    daily = (
        plot_df.groupby("day", sort=True)
        .agg(
            consumption_kwh=("consumption_kwh", "sum"),
            total_net_eur=("total_net_eur", "sum"),
            total_gross_eur=("total_gross_eur", "sum"),
        )
        .reset_index()
    )
    daily["weekday"] = daily["day"].dt.dayofweek
    daily["weekday_name"] = daily["day"].dt.day_name()

    avg_daily_net = daily["total_net_eur"].mean()
    avg_daily_gross = daily["total_gross_eur"].mean()

    fig, ax = plt.subplots(figsize=(12, 5))
    ax.plot(daily["day"], daily["total_net_eur"], color="#2563eb", linewidth=1.8, label="Tageskosten netto")
    ax.plot(daily["day"], daily["total_gross_eur"], color="#16a34a", linewidth=1.8, label="Tageskosten brutto")
    ax.axhline(avg_daily_net, color="#1d4ed8", linestyle="--", linewidth=1.4, label=f"Durchschnitt netto: {avg_daily_net:.2f} EUR/Tag")
    ax.axhline(avg_daily_gross, color="#166534", linestyle="--", linewidth=1.4, label=f"Durchschnitt brutto: {avg_daily_gross:.2f} EUR/Tag")
    ax.set_title("Tageskosten und durchschnittliche Kosten pro Tag")
    ax.set_xlabel("Tag")
    ax.set_ylabel("Kosten [EUR]")
    ax.grid(axis="y", alpha=0.25)
    fig.autofmt_xdate()
    ax.legend()
    save_figure(fig, "_daily_costs")

    weekday = (
        daily.groupby(["weekday", "weekday_name"], sort=True)
        .agg(
            avg_net_eur=("total_net_eur", "mean"),
            avg_gross_eur=("total_gross_eur", "mean"),
            avg_consumption_kwh=("consumption_kwh", "mean"),
        )
        .reset_index()
        .sort_values("weekday")
    )

    x_positions = range(len(weekday))
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.bar([x - 0.2 for x in x_positions], weekday["avg_net_eur"], width=0.4, color="#0f766e", label="Ø netto pro Tag")
    ax.bar([x + 0.2 for x in x_positions], weekday["avg_gross_eur"], width=0.4, color="#7c3aed", label="Ø brutto pro Tag")
    ax.set_title("Durchschnittliche Kosten nach Wochentag")
    ax.set_xlabel("Wochentag")
    ax.set_ylabel("Kosten [EUR]")
    ax.set_xticks(list(x_positions), weekday["weekday_name"], rotation=20)
    ax.grid(axis="y", alpha=0.25)
    ax.legend()
    save_figure(fig, "_weekday_average_costs")

    fig, ax1 = plt.subplots(figsize=(11, 5))
    ax1.bar(monthly["month"], monthly["consumption_kwh"], color="#6366f1", alpha=0.8, label="Verbrauch")
    ax1.set_xlabel("Monat")
    ax1.set_ylabel("Verbrauch [kWh]", color="#4338ca")
    ax1.tick_params(axis="y", labelcolor="#4338ca")
    ax1.tick_params(axis="x", rotation=45)
    ax1.grid(axis="y", alpha=0.2)

    ax2 = ax1.twinx()
    ax2.plot(monthly["month"], monthly["weighted_market_price_eur_mwh"], color="#dc2626", marker="o", linewidth=2, label="Gewichteter Marktpreis")
    ax2.set_ylabel("Preis [EUR/MWh]", color="#b91c1c")
    ax2.tick_params(axis="y", labelcolor="#b91c1c")
    ax1.set_title("Monatlicher Verbrauch und gewichteter Marktpreis")

    handles_1, labels_1 = ax1.get_legend_handles_labels()
    handles_2, labels_2 = ax2.get_legend_handles_labels()
    ax1.legend(handles_1 + handles_2, labels_1 + labels_2, loc="upper left")
    save_figure(fig, "_monthly_consumption_price")

    load_profile = (
        plot_df.assign(time_of_day=local_start.dt.strftime("%H:%M"))
        .groupby("time_of_day", sort=True)
        .agg(
            avg_consumption_kwh=("consumption_kwh", "mean"),
            p25_consumption_kwh=("consumption_kwh", lambda values: values.quantile(0.25)),
            p75_consumption_kwh=("consumption_kwh", lambda values: values.quantile(0.75)),
        )
        .reset_index()
    )

    x_positions = list(range(len(load_profile)))
    tick_step = max(1, len(load_profile) // 12)
    fig, ax = plt.subplots(figsize=(12, 5))
    ax.fill_between(
        x_positions,
        load_profile["p25_consumption_kwh"],
        load_profile["p75_consumption_kwh"],
        color="#bfdbfe",
        alpha=0.6,
        label="Mittlere 50 %",
    )
    ax.plot(x_positions, load_profile["avg_consumption_kwh"], color="#1d4ed8", linewidth=2.2, label="Durchschnitt")
    ax.set_title("Durchschnittliches Tages-Lastprofil ueber das Jahr")
    ax.set_xlabel("Uhrzeit")
    ax.set_ylabel("Durchschnittlicher Verbrauch pro 15 Minuten [kWh]")
    ax.set_xticks(x_positions[::tick_step], load_profile["time_of_day"].iloc[::tick_step], rotation=45)
    ax.grid(axis="y", alpha=0.25)
    ax.legend()
    save_figure(fig, "_average_daily_load_profile")

    return generated_paths


def _safe_divide(numerator: float, denominator: float) -> float:
    if denominator == 0:
        return 0.0
    return numerator / denominator


def write_results_report(summary: Summary, path: Path) -> None:
    lines = [
        "Ergebnisbericht",
        "===============",
        "",
        f"Verbrauch gesamt [kWh]:                    {summary.consumption_kwh:,.3f}",
        f"Davon mit Preis [kWh]:                     {summary.priced_consumption_kwh:,.3f}",
        f"Davon ohne Preis [kWh]:                    {summary.missing_price_consumption_kwh:,.3f}",
        f"Preisabdeckung [%]:                        {summary.priced_consumption_share:.2%}",
        f"Marktpreis-Kosten [EUR, ohne Aufschlag/USt]: {summary.market_cost_eur:,.2f}",
        f"Lieferanten-Offset [EUR]:                  {summary.supplier_offset_cost_eur:,.2f}",
        f"Gesamt netto [EUR, inkl. Aufschlag, ohne USt]: {summary.total_net_eur:,.2f}",
        f"Gesamt brutto [EUR, inkl. Aufschlag und USt]: {summary.total_gross_eur:,.2f}",
        "",
        "Verbrauchsgewichtete Preise pro kWh",
        "-----------------------------------",
        f"1) Spotmarktpreis ohne Aufschlag/USt [EUR/MWh]: {summary.average_market_price_eur_mwh_weighted:,.2f}",
        f"1) Spotmarktpreis ohne Aufschlag/USt [ct/kWh]:  {summary.average_market_price_ct_kwh_weighted:,.3f}",
        f"2) Spotmarktpreis inkl. Lieferanten-Offset, ohne USt [ct/kWh]: {summary.average_price_ct_kwh_with_supplier_net:,.3f}",
        f"3) Spotmarktpreis inkl. Lieferanten-Offset und USt [ct/kWh]:   {summary.average_price_ct_kwh_with_supplier_gross:,.3f}",
        "",
        f"Lieferanten-Offset [ct/kWh netto]:         {summary.supplier_offset_ct_kwh:,.3f}",
        f"USt-Satz:                                  {summary.vat_rate:.2%}",
        f"Fehlende Preise [Zeilen]:                  {summary.rows_missing_price}",
    ]

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------- Berechnung ----------

def calculate_costs(
    load: pd.DataFrame,
    prices: pd.DataFrame,
    supplier_offset_ct_per_kwh: float = 0.0,
    vat_rate: float = 0.0,
) -> tuple[pd.DataFrame, Summary]:
    merged = load.merge(
        prices[["interval_start", "price_eur_mwh"]],
        on="interval_start",
        how="left",
        validate="many_to_one",
    )

    merged["price_missing"] = merged["price_eur_mwh"].isna()
    merged["market_cost_eur"] = merged["consumption_kwh"] * (merged["price_eur_mwh"] / 1000.0)
    merged["supplier_offset_cost_eur"] = merged["consumption_kwh"] * (supplier_offset_ct_per_kwh / 100.0)
    merged["total_net_eur"] = merged["market_cost_eur"].fillna(0.0) + merged["supplier_offset_cost_eur"].fillna(0.0)
    merged["total_gross_eur"] = merged["total_net_eur"] * (1.0 + vat_rate)
    merged["market_price_ct_kwh"] = merged["price_eur_mwh"] / 10.0
    merged["effective_price_net_ct_kwh"] = merged["market_price_ct_kwh"] + supplier_offset_ct_per_kwh
    merged["effective_price_gross_ct_kwh"] = merged["effective_price_net_ct_kwh"] * (1.0 + vat_rate)
    merged["interval_hours"] = (
        (merged["interval_end"] - merged["interval_start"]).dt.total_seconds() / 3600.0
    )
    merged["priced_consumption_kwh"] = merged["consumption_kwh"].where(~merged["price_missing"], 0.0)

    weighted_avg_eur_mwh = 0.0
    priced_consumption_kwh = float(merged["priced_consumption_kwh"].sum())
    if priced_consumption_kwh > 0:
        weighted_avg_eur_mwh = (
            (merged.loc[~merged["price_missing"], "price_eur_mwh"] * merged.loc[~merged["price_missing"], "consumption_kwh"]).sum()
            / priced_consumption_kwh
        )

    total_consumption_kwh = float(merged["consumption_kwh"].sum())
    missing_price_consumption_kwh = total_consumption_kwh - priced_consumption_kwh
    average_price_ct_kwh_with_supplier_net = _safe_divide(
        float(merged.loc[~merged["price_missing"], "total_net_eur"].sum()) * 100.0,
        priced_consumption_kwh,
    )
    average_price_ct_kwh_with_supplier_gross = _safe_divide(
        float(merged.loc[~merged["price_missing"], "total_gross_eur"].sum()) * 100.0,
        priced_consumption_kwh,
    )

    summary = Summary(
        rows_load=int(len(load)),
        rows_price=int(len(prices)),
        rows_merged=int(len(merged)),
        rows_missing_price=int(merged["price_missing"].sum()),
        consumption_kwh=total_consumption_kwh,
        priced_consumption_kwh=priced_consumption_kwh,
        missing_price_consumption_kwh=float(missing_price_consumption_kwh),
        priced_consumption_share=float(_safe_divide(priced_consumption_kwh, total_consumption_kwh)),
        market_cost_eur=float(merged["market_cost_eur"].sum(skipna=True)),
        supplier_offset_cost_eur=float(merged["supplier_offset_cost_eur"].sum(skipna=True)),
        total_net_eur=float(merged["total_net_eur"].sum(skipna=True)),
        total_gross_eur=float(merged["total_gross_eur"].sum(skipna=True)),
        average_market_price_eur_mwh_weighted=float(weighted_avg_eur_mwh),
        average_market_price_ct_kwh_weighted=float(weighted_avg_eur_mwh / 10.0),
        average_price_ct_kwh_with_supplier_net=float(average_price_ct_kwh_with_supplier_net),
        average_price_ct_kwh_with_supplier_gross=float(average_price_ct_kwh_with_supplier_gross),
        supplier_offset_ct_kwh=float(supplier_offset_ct_per_kwh),
        vat_rate=float(vat_rate),
    )

    return merged, summary


# ---------- CLI ----------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Berechnet historische Stromkosten aus Lastprofil + Spotpreisen (Utilitarian Spot)."
    )
    p.add_argument("--load-profile", required=True, type=Path, help="Pfad zur Lastprofil-CSV")
    p.add_argument("--price-csv", type=Path, help="Optionale lokale Preis-CSV statt Utilitarian Spot")
    p.add_argument("--zone", default=DEFAULT_ZONE, help=f"Preiszone für Utilitarian Spot, Standard: {DEFAULT_ZONE}")
    p.add_argument("--timestamp-is", choices=["start", "end"], default="end", help="Bedeutung des Messzeitpunkts im Lastprofil")
    p.add_argument("--supplier-offset-ct-per-kwh", "--offset-ct-per-kwh", type=float, default=0.0,
                   help="Lieferantenaufschlag in ct/kWh netto, z. B. 1.49")
    p.add_argument("--vat-rate", type=float, default=0.0, help="USt-Satz, z. B. 0.20")
    p.add_argument("--output-prefix", default="spot_costs", help="Prefix für Ausgabedateien")
    p.add_argument("--expand-hourly-to-quarter-hour", action="store_true",
                   help="Stundenpreise explizit auf Viertelstunden erweitern")
    p.add_argument("--disable-auto-expand", action="store_true",
                   help="Automatische Expansion auf Lastprofil-Auflösung deaktivieren")
    p.add_argument("--drop-missing-prices", action="store_true", help="Zeilen ohne Preis vor Export entfernen")
    p.add_argument("--skip-plots", action="store_true", help="Keine Diagramme erzeugen")
    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    load = read_load_profile(args.load_profile, timestamp_is=args.timestamp_is)

    if args.price_csv:
        prices = read_price_csv(args.price_csv)
    else:
        start = load["interval_start"].min()
        end = load["interval_end"].max()
        prices = fetch_utilitarian_prices(args.zone, start, end)

    prices = maybe_expand_prices_to_load_resolution(load, prices, disable_auto_expand=args.disable_auto_expand)

    if args.expand_hourly_to_quarter_hour:
        prices = expand_prices_to_quarter_hour(prices)

    detail, summary = calculate_costs(
        load=load,
        prices=prices,
        supplier_offset_ct_per_kwh=args.supplier_offset_ct_per_kwh,
        vat_rate=args.vat_rate,
    )

    detail_export = detail

    if args.drop_missing_prices:
        detail_export = detail.loc[~detail["price_missing"]].copy()

    out_prefix = Path(args.output_prefix)
    detail_path = out_prefix.with_name(out_prefix.name + "_detail.csv")
    summary_path = out_prefix.with_name(out_prefix.name + "_summary.json")
    results_path = out_prefix.with_name(out_prefix.name + "_results.txt")

    detail_export = detail_export.copy()
    for col in ["interval_start", "interval_end"]:
        detail_export[col] = detail_export[col].dt.strftime("%Y-%m-%dT%H:%M:%S%z")
    detail_export.to_csv(detail_path, index=False)

    with summary_path.open("w", encoding="utf-8") as f:
        json.dump(summary.__dict__, f, ensure_ascii=False, indent=2)

    write_results_report(summary, results_path)

    plot_paths: list[Path] = []
    if not args.skip_plots:
        plot_paths = write_plots(detail, out_prefix)

    print("Fertig.")
    print(f"Detaildatei:  {detail_path}")
    print(f"Summary-Datei: {summary_path}")
    print(f"Ergebnisdatei: {results_path}")
    for path in plot_paths:
        print(f"Diagramm:     {path}")
    print()
    print("Zusammenfassung:")
    print(f"  Verbrauch gesamt [kWh]:               {summary.consumption_kwh:,.3f}")
    print(f"  Verbrauch mit Preis [kWh]:            {summary.priced_consumption_kwh:,.3f}")
    print(f"  Marktpreis-Kosten [EUR, ohne Aufschlag/USt]: {summary.market_cost_eur:,.2f}")
    print(f"  Lieferanten-Offset [EUR]:             {summary.supplier_offset_cost_eur:,.2f}")
    print(f"  Gesamt netto [EUR, inkl. Aufschlag]:  {summary.total_net_eur:,.2f}")
    print(f"  Gesamt brutto [EUR, inkl. Aufschlag + USt]: {summary.total_gross_eur:,.2f}")
    print(f"  1) Spotmarktpreis [EUR/MWh]:          {summary.average_market_price_eur_mwh_weighted:,.2f}")
    print(f"  1) Spotmarktpreis [ct/kWh]:           {summary.average_market_price_ct_kwh_weighted:,.3f}")
    print(f"  2) Mit Lieferanten-Offset [ct/kWh]:   {summary.average_price_ct_kwh_with_supplier_net:,.3f}")
    print(f"  3) Mit Offset + USt [ct/kWh]:         {summary.average_price_ct_kwh_with_supplier_gross:,.3f}")
    print(f"  Lieferanten-Offset [ct/kWh netto]:    {summary.supplier_offset_ct_kwh:,.3f}")
    print(f"  USt-Satz:                             {summary.vat_rate:.2%}")
    print(f"  Fehlende Preise [Zeilen]:             {summary.rows_missing_price}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
