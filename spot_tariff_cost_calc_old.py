#!/usr/bin/env python3
"""
Berechnet historische Energiekosten auf Basis eines Lastprofils und
Day-Ahead-Preisen (EPEX Spot / ENTSO-E).

Funktionen:
- Liest österreichische Smart-Meter-Lastprofile im Stil des Beispiels des Nutzers
  (z. B. 'Messzeitpunkt;Messinterval;Abrechnungsmaßeinheit;Verbrauch [kWh];').
- Liest alternativ eine lokale Preis-CSV ein ODER lädt Preise per ENTSO-E API.
- Unterstützt PT15M- und PT60M-Preise.
- Wandelt Stundenpreise optional automatisch auf Viertelstunden auf.
- Berechnet Marktpreisanteil, optionalen Lieferaufschlag und USt.
- Schreibt Detail-CSV + Summary-JSON.

Hinweis:
- Netzgebühren, Abgaben, Grundgebühr, Rabattlogiken etc. sind NICHT automatisch enthalten.
  Das Skript berechnet standardmäßig nur den Energiepreis aus dem Spotmarkt.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pandas as pd

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

VIENNA_TZ = "Europe/Vienna"
ENTSOE_API_URL = "https://web-api.tp.entsoe.eu/api"
AT_DOMAIN = "10YAT-APG------L"


@dataclass
class Summary:
    rows_load: int
    rows_price: int
    rows_merged: int
    rows_missing_price: int
    consumption_kwh: float
    market_cost_eur: float
    adder_cost_eur: float
    total_net_eur: float
    total_gross_eur: float
    average_market_price_eur_mwh_weighted: float
    average_market_price_ct_kwh_weighted: float


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
    ts = pd.to_datetime(series, errors="coerce")
    if ts.dt.tz is None:
        ts = ts.dt.tz_localize(assume_tz, ambiguous="infer", nonexistent="shift_forward")
    return ts.dt.tz_convert(VIENNA_TZ)


def _read_delimited_auto(path: Path) -> pd.DataFrame:
    text = path.read_text(encoding="utf-8-sig")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=";,\t,")
        sep = dialect.delimiter
    except csv.Error:
        sep = ";"

    # decimal=',' ist für das Lastprofil wichtig; für Preisdateien kann auch '.' vorkommen.
    # Daher erst als Text einlesen, danach gezielt numerische Spalten konvertieren.
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
        "Messzeitpunkt", "Zeitstempel", "timestamp", "datetime", "Datum", "date"
    ])
    consumption_col = _pick_existing(df, [
        "Verbrauch [kWh]", "Verbrauch[kWh]", "Verbrauch", "consumption_kwh", "kwh"
    ])
    interval_col = _pick_existing(df, [
        "Messinterval", "Messintervall", "resolution", "Intervall"
    ])

    if time_col is None or consumption_col is None:
        raise ValueError(
            "Konnte die benötigten Spalten im Lastprofil nicht finden. "
            "Erwartet werden mindestens Zeitstempel und Verbrauch [kWh]."
        )

    out = pd.DataFrame()
    out["timestamp_raw"] = df[time_col]
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
        # Fallback: bei unbekanntem Format 15 Minuten annehmen
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

    # Mögliche Zeitspalten
    start_col = _pick_existing(df, [
        "interval_start", "start", "timestamp", "datetime", "time", "from", "von"
    ])
    end_col = _pick_existing(df, [
        "interval_end", "end", "to", "bis"
    ])
    price_col = _pick_existing(df, [
        "price_eur_mwh", "price", "marketprice", "market_price", "preis", "spotpreis", "eur/mwh"
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
        # Falls keine Endzeit vorhanden ist, aus der Auflösung ableiten.
        if resolution_col is not None:
            res = df[resolution_col].astype(str).str.upper().str.strip()
            delta = res.map({"PT15M": pd.Timedelta(minutes=15), "QH": pd.Timedelta(minutes=15), "PT60M": pd.Timedelta(hours=1), "PT1H": pd.Timedelta(hours=1), "H": pd.Timedelta(hours=1)})
            delta = delta.fillna(pd.Timedelta(minutes=15))
            out["interval_end"] = out["interval_start"] + delta
        else:
            # Fallback: Zeitabstand der Daten ermitteln
            diffs = out["interval_start"].sort_values().diff().dropna()
            inferred = diffs.mode().iloc[0] if not diffs.empty else pd.Timedelta(minutes=15)
            out["interval_end"] = out["interval_start"] + inferred

    out = out[["interval_start", "interval_end", "price_eur_mwh"]].dropna(subset=["interval_start", "price_eur_mwh"])
    out = out.sort_values("interval_start").drop_duplicates(subset=["interval_start"], keep="last").reset_index(drop=True)
    return out


# ---------- ENTSO-E Fetch ----------

def _entsoe_period_fmt(ts: pd.Timestamp) -> str:
    # ENTSO-E erwartet UTC im Format yyyyMMddHHmm
    return ts.tz_convert("UTC").strftime("%Y%m%d%H%M")


def fetch_entsoe_day_ahead_prices(token: str, start: pd.Timestamp, end: pd.Timestamp, domain: str = AT_DOMAIN) -> pd.DataFrame:
    if requests is None:
        raise RuntimeError("requests ist nicht installiert. Bitte 'pip install requests' ausführen.")

    # ENTSO-E arbeitet robust mit Abfragen in UTC; wir geben den kompletten Bereich an.
    params = {
        "securityToken": token,
        "documentType": "A44",  # Day-ahead prices
        "in_Domain": domain,
        "out_Domain": domain,
        "periodStart": _entsoe_period_fmt(start),
        "periodEnd": _entsoe_period_fmt(end),
    }

    resp = requests.get(ENTSOE_API_URL, params=params, timeout=60)
    resp.raise_for_status()

    if "<Acknowledgement_MarketDocument" in resp.text:
        raise RuntimeError(f"ENTSO-E API Fehler:\n{resp.text[:2000]}")

    root = ET.fromstring(resp.content)
    ns = {"ns": root.tag.split("}")[0].strip("{")}

    rows = []
    for ts in root.findall("ns:TimeSeries", ns):
        period = ts.find("ns:Period", ns)
        if period is None:
            continue

        ti = period.find("ns:timeInterval", ns)
        if ti is None:
            continue

        start_text = ti.findtext("ns:start", default=None, namespaces=ns)
        resolution_text = period.findtext("ns:resolution", default="PT60M", namespaces=ns)
        if start_text is None:
            continue

        period_start = pd.Timestamp(start_text, tz="UTC").tz_convert(VIENNA_TZ)

        if resolution_text == "PT15M":
            step = pd.Timedelta(minutes=15)
        elif resolution_text in {"PT60M", "PT1H"}:
            step = pd.Timedelta(hours=1)
        elif resolution_text == "PT30M":
            step = pd.Timedelta(minutes=30)
        else:
            # konservativer Fallback
            raise ValueError(f"Unbekannte ENTSO-E Auflösung: {resolution_text}")

        for point in period.findall("ns:Point", ns):
            pos_text = point.findtext("ns:position", default=None, namespaces=ns)
            price_text = point.findtext("ns:price.amount", default=None, namespaces=ns)
            if pos_text is None or price_text is None:
                continue
            position = int(pos_text)
            start_dt = period_start + (position - 1) * step
            end_dt = start_dt + step
            rows.append(
                {
                    "interval_start": start_dt,
                    "interval_end": end_dt,
                    "price_eur_mwh": float(price_text),
                    "resolution": resolution_text,
                }
            )

    if not rows:
        raise RuntimeError("Keine Preisdaten von ENTSO-E erhalten.")

    prices = pd.DataFrame(rows).sort_values("interval_start")
    prices = prices.drop_duplicates(subset=["interval_start"], keep="last").reset_index(drop=True)
    return prices


def expand_prices_to_quarter_hour(prices: pd.DataFrame) -> pd.DataFrame:
    """
    Expandiert PT60M / PT30M auf Viertelstunden. PT15M bleibt unverändert.
    Das ist für Lastprofile in QH-Auflösung praktisch.
    """
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


# ---------- Berechnung ----------

def calculate_costs(
    load: pd.DataFrame,
    prices: pd.DataFrame,
    fixed_adder_ct_per_kwh: float = 0.0,
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
    merged["adder_cost_eur"] = merged["consumption_kwh"] * (fixed_adder_ct_per_kwh / 100.0)
    merged["total_net_eur"] = merged["market_cost_eur"].fillna(0.0) + merged["adder_cost_eur"].fillna(0.0)
    merged["total_gross_eur"] = merged["total_net_eur"] * (1.0 + vat_rate)
    merged["market_price_ct_kwh"] = merged["price_eur_mwh"] / 10.0  # 1 EUR/MWh = 0.1 ct/kWh

    weighted_avg_eur_mwh = 0.0
    denom = merged.loc[~merged["price_missing"], "consumption_kwh"].sum()
    if denom > 0:
        weighted_avg_eur_mwh = (
            (merged.loc[~merged["price_missing"], "price_eur_mwh"] * merged.loc[~merged["price_missing"], "consumption_kwh"]).sum()
            / denom
        )

    summary = Summary(
        rows_load=int(len(load)),
        rows_price=int(len(prices)),
        rows_merged=int(len(merged)),
        rows_missing_price=int(merged["price_missing"].sum()),
        consumption_kwh=float(merged["consumption_kwh"].sum()),
        market_cost_eur=float(merged["market_cost_eur"].sum(skipna=True)),
        adder_cost_eur=float(merged["adder_cost_eur"].sum(skipna=True)),
        total_net_eur=float(merged["total_net_eur"].sum(skipna=True)),
        total_gross_eur=float(merged["total_gross_eur"].sum(skipna=True)),
        average_market_price_eur_mwh_weighted=float(weighted_avg_eur_mwh),
        average_market_price_ct_kwh_weighted=float(weighted_avg_eur_mwh / 10.0),
    )

    return merged, summary


# ---------- CLI ----------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Berechnet historische Stromkosten aus Lastprofil + Spotpreisen."
    )
    p.add_argument("--load-profile", required=True, type=Path, help="Pfad zur Lastprofil-CSV")
    p.add_argument("--price-csv", type=Path, help="Pfad zu einer Preis-CSV (alternativ zu --entsoe-token)")
    p.add_argument("--entsoe-token", type=str, help="ENTSO-E API Token (alternativ zu --price-csv)")
    p.add_argument("--domain", default=AT_DOMAIN, help=f"ENTSO-E Domain, Standard: {AT_DOMAIN}")
    p.add_argument("--timestamp-is", choices=["start", "end"], default="end", help="Bedeutung des Messzeitpunkts im Lastprofil")
    p.add_argument("--fixed-adder-ct-per-kwh", type=float, default=0.0, help="Optionaler fixer Aufschlag in ct/kWh (netto)")
    p.add_argument("--vat-rate", type=float, default=0.0, help="Optionaler USt-Satz, z. B. 0.20")
    p.add_argument("--output-prefix", default="spot_costs", help="Prefix für Ausgabedateien")
    p.add_argument("--expand-hourly-to-quarter-hour", action="store_true", help="Stundenpreise auf Viertelstunden erweitern")
    p.add_argument("--drop-missing-prices", action="store_true", help="Zeilen ohne Preis vor Export entfernen")
    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if bool(args.price_csv) == bool(args.entsoe_token):
        parser.error("Bitte genau EINE Quelle angeben: entweder --price-csv oder --entsoe-token")

    load = read_load_profile(args.load_profile, timestamp_is=args.timestamp_is)

    if args.price_csv:
        prices = read_price_csv(args.price_csv)
    else:
        start = load["interval_start"].min()
        end = load["interval_end"].max()
        prices = fetch_entsoe_day_ahead_prices(args.entsoe_token, start, end, domain=args.domain)

    if args.expand_hourly_to_quarter_hour:
        prices = expand_prices_to_quarter_hour(prices)

    detail, summary = calculate_costs(
        load=load,
        prices=prices,
        fixed_adder_ct_per_kwh=args.fixed_adder_ct_per_kwh,
        vat_rate=args.vat_rate,
    )

    if args.drop_missing_prices:
        detail = detail.loc[~detail["price_missing"]].copy()

    out_prefix = Path(args.output_prefix)
    detail_path = out_prefix.with_name(out_prefix.name + "_detail.csv")
    summary_path = out_prefix.with_name(out_prefix.name + "_summary.json")

    detail_export = detail.copy()
    for col in ["interval_start", "interval_end"]:
        detail_export[col] = detail_export[col].dt.strftime("%Y-%m-%dT%H:%M:%S%z")
    detail_export.to_csv(detail_path, index=False)

    with summary_path.open("w", encoding="utf-8") as f:
        json.dump(summary.__dict__, f, ensure_ascii=False, indent=2)

    print("Fertig.")
    print(f"Detaildatei:  {detail_path}")
    print(f"Summary-Datei: {summary_path}")
    print()
    print("Zusammenfassung:")
    print(f"  Verbrauch gesamt [kWh]:            {summary.consumption_kwh:,.3f}")
    print(f"  Marktpreis-Kosten [EUR]:           {summary.market_cost_eur:,.2f}")
    print(f"  Aufschlag-Kosten [EUR]:            {summary.adder_cost_eur:,.2f}")
    print(f"  Gesamt netto [EUR]:                {summary.total_net_eur:,.2f}")
    print(f"  Gesamt brutto [EUR]:               {summary.total_gross_eur:,.2f}")
    print(f"  Gewichteter Marktpreis [EUR/MWh]:  {summary.average_market_price_eur_mwh_weighted:,.2f}")
    print(f"  Gewichteter Marktpreis [ct/kWh]:   {summary.average_market_price_ct_kwh_weighted:,.3f}")
    print(f"  Fehlende Preise [Zeilen]:          {summary.rows_missing_price}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
