"""
Real Exchange Rate (RER) explorer built on IMF SDMX 3.0 data.

Definitions
-----------
S(t)   : nominal exchange rate, units of Country 2 currency per 1 unit of Country 1 currency
P1(t)  : monthly CPI of Country 1
P2(t)  : monthly CPI of Country 2
RER(t) = S(t) * P1(t) / P2(t)
RER*   = mean of RER(t) over valid months in the user-selected sample
Real Index(t) = 100 * RER(t) / RER*

IMF exchange rates (indicator XDC_USD) are domestic currency units per USD, so
S(t) = [Country 2 LCU per USD] / [Country 1 LCU per USD].
"""

from __future__ import annotations

import re
import time
from datetime import date

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st
from plotly.subplots import make_subplots

# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------
API_BASE = "https://api.imf.org/external/sdmx/3.0"
AGENCY = "IMF.STA"
CPI_FLOW = "CPI"
ER_FLOW = "ER"
START_PERIOD = "1976-01"
EURO_AREA = "G163"
CACHE_TTL = 6 * 60 * 60  # seconds

# Euro area members: their IMF national exchange-rate series stop when they adopted the euro.
EURO_MEMBERS = {
    "AUT", "BEL", "BGR", "CYP", "DEU", "ESP", "EST", "FIN", "FRA", "GRC", "HRV",
    "IRL", "ITA", "LTU", "LUX", "LVA", "MLT", "NLD", "PRT", "SVK", "SVN",
}

HEADERS = {
    "Accept": "application/json",
    "User-Agent": "rer-streamlit-app/1.0 (+https://streamlit.io)",
}

st.set_page_config(page_title="Real Exchange Rate Explorer", page_icon="💱", layout="wide")


# --------------------------------------------------------------------------------------
# IMF API helpers
# --------------------------------------------------------------------------------------
class IMFAPIError(RuntimeError):
    pass


def _get_json(url: str, params: dict | None = None, retries: int = 3, timeout: int = 60) -> dict:
    """GET a JSON document from the IMF API with simple retry/backoff."""
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            resp = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
            if resp.status_code == 404:
                # The IMF API answers 404 when a query matches no data.
                return {}
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as err:
            last_err = err
            time.sleep(1.5 * (attempt + 1))
    raise IMFAPIError(f"IMF API request failed: {url} ({last_err})")


_PERIOD_RE = re.compile(r"^(\d{4})-?M?(\d{1,2})$")


def _to_month(period: str) -> pd.Timestamp | None:
    """Convert IMF period strings such as '1976-M01' or '1976-01' to a month-start Timestamp."""
    m = _PERIOD_RE.match(str(period).strip())
    if not m:
        return None
    return pd.Timestamp(year=int(m.group(1)), month=int(m.group(2)), day=1)


def _parse_sdmx_json(payload: dict) -> pd.DataFrame:
    """Flatten an SDMX-JSON 2.0 data message into a long DataFrame.

    Returns columns: one per series dimension, plus 'date' and 'value'.
    """
    data = (payload or {}).get("data") or {}
    structures = data.get("structures") or []
    datasets = data.get("dataSets") or []
    if not structures or not datasets:
        return pd.DataFrame()

    structure = structures[0]
    series_dims = structure["dimensions"]["series"]
    obs_dims = structure["dimensions"]["observation"]
    time_values = [v.get("value", v.get("id")) for v in obs_dims[0]["values"]]

    # Optional SCALE (unit multiplier) series attribute: value = obs * 10**scale
    series_attrs = (structure.get("attributes") or {}).get("series") or []
    scale_pos = next((i for i, a in enumerate(series_attrs) if a.get("id") == "SCALE"), None)

    rows: list[dict] = []
    for series_key, series in (datasets[0].get("series") or {}).items():
        idx = [int(i) for i in series_key.split(":")]
        labels = {dim["id"]: dim["values"][i]["id"] for dim, i in zip(series_dims, idx)}

        multiplier = 1.0
        if scale_pos is not None:
            attr_vals = series.get("attributes") or []
            if scale_pos < len(attr_vals) and attr_vals[scale_pos] is not None:
                try:
                    code = series_attrs[scale_pos]["values"][int(attr_vals[scale_pos])]["id"]
                    multiplier = 10.0 ** int(code)
                except (KeyError, IndexError, TypeError, ValueError):
                    multiplier = 1.0

        for obs_key, obs in (series.get("observations") or {}).items():
            if not obs or obs[0] in (None, "", "NaN"):
                continue
            try:
                value = float(obs[0]) * multiplier
            except (TypeError, ValueError):
                continue
            month = _to_month(time_values[int(obs_key)])
            if month is None:
                continue
            rows.append({**labels, "date": month, "value": value})

    return pd.DataFrame(rows)


def _fetch_data(flow: str, key: str) -> pd.DataFrame:
    url = f"{API_BASE}/data/dataflow/{AGENCY}/{flow}/+/{key}"
    params = {"c[TIME_PERIOD]": f"ge:{START_PERIOD}"}
    df = _parse_sdmx_json(_get_json(url, params=params))
    if not df.empty:
        df = df[df["date"] >= pd.Timestamp(START_PERIOD + "-01")]
    return df


def _available_countries(flow: str, key: str) -> set[str]:
    url = f"{API_BASE}/availability/dataflow/{AGENCY}/{flow}/+/{key}/COUNTRY"
    payload = _get_json(url)
    out: set[str] = set()
    for constraint in (payload.get("data") or {}).get("dataConstraints") or []:
        for region in constraint.get("cubeRegions") or []:
            for comp in region.get("components") or []:
                if comp.get("id") == "COUNTRY":
                    out.update(v.get("value") for v in comp.get("values") or [] if v.get("value"))
    return out


@st.cache_data(ttl=24 * 60 * 60, show_spinner="Loading IMF country list…")
def load_country_list() -> dict[str, str]:
    """Return {code: name} for economies with both monthly CPI and USD exchange-rate data."""
    payload = _get_json(f"{API_BASE}/structure/codelist/IMF/CL_COUNTRY/+")
    codelists = (payload.get("data") or {}).get("codelists") or []
    names: dict[str, str] = {}
    if codelists:
        for code in codelists[0].get("codes") or []:
            names[code["id"]] = code.get("name") or code["id"]

    try:
        cpi = _available_countries(CPI_FLOW, "*.*._T.IX.M")
        er = _available_countries(ER_FLOW, "*.XDC_USD.PA_RT.M") | {"USA"}  # USD per USD = 1
        codes = cpi & er
    except IMFAPIError:
        codes = set()

    if not codes:
        # Fallback: every 3-letter ISO code in the codelist (data may still be missing).
        codes = {c for c in names if re.fullmatch(r"[A-Z]{3}", c)}
    codes.add(EURO_AREA)

    result = {c: names.get(c, c) for c in codes}
    result[EURO_AREA] = "Euro Area (EA)"
    return dict(sorted(result.items(), key=lambda kv: kv[1].lower()))


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def load_cpi(iso: str) -> tuple[pd.Series, str]:
    """Monthly headline CPI index. Falls back to HICP (used by the IMF for the Euro area)."""
    df = _fetch_data(CPI_FLOW, f"{iso}.CPI+HICP._T.IX.M")
    if df.empty:
        return pd.Series(dtype="float64", name="cpi"), "n/a"
    for index_type in ("CPI", "HICP"):
        sub = df[df["INDEX_TYPE"] == index_type]
        if not sub.empty:
            s = sub.groupby("date")["value"].last().sort_index()
            s.name = "cpi"
            return s, index_type
    return pd.Series(dtype="float64", name="cpi"), "n/a"


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def load_usd_rate(iso: str) -> pd.Series:
    """Monthly average exchange rate, domestic currency units per USD."""
    df = _fetch_data(ER_FLOW, f"{iso}.XDC_USD.PA_RT.M")
    if df.empty:
        if iso == "USA":
            idx = pd.date_range(START_PERIOD + "-01", pd.Timestamp.today().normalize(), freq="MS")
            return pd.Series(1.0, index=idx, name="lcu_per_usd")
        return pd.Series(dtype="float64", name="lcu_per_usd")
    s = df.groupby("date")["value"].last().sort_index()
    s.name = "lcu_per_usd"
    return s


# --------------------------------------------------------------------------------------
# Calculations
# --------------------------------------------------------------------------------------
def build_panel(e1: pd.Series, e2: pd.Series, p1: pd.Series, p2: pd.Series) -> pd.DataFrame:
    """Align the four inputs on a monthly grid and compute S(t) and RER(t)."""
    pieces = [s for s in (e1, e2, p1, p2) if not s.empty]
    if not pieces:
        return pd.DataFrame()
    start = max(pd.Timestamp(START_PERIOD + "-01"), min(s.index.min() for s in pieces))
    end = max(s.index.max() for s in pieces)
    idx = pd.date_range(start, end, freq="MS")

    df = pd.DataFrame(index=idx)
    df.index.name = "month"
    df["E1"] = e1.reindex(idx)
    df["E2"] = e2.reindex(idx)
    df["P1"] = p1.reindex(idx)
    df["P2"] = p2.reindex(idx)

    # Guard against non-positive values (bad data) before dividing.
    for col in ("E1", "E2", "P1", "P2"):
        df[col] = df[col].astype("float64").where(df[col] > 0)

    df["S"] = df["E2"] / df["E1"]
    df["RER"] = df["S"] * df["P1"] / df["P2"]
    df["valid"] = df["RER"].notna()
    return df


def coverage_row(label: str, s: pd.Series) -> dict:
    if s.empty:
        return {"Series": label, "First month": "—", "Last month": "—", "Observations": 0}
    return {
        "Series": label,
        "First month": s.index.min().strftime("%Y-%m"),
        "Last month": s.index.max().strftime("%Y-%m"),
        "Observations": int(s.notna().sum()),
    }


# --------------------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------------------
st.title("💱 Real Exchange Rate Explorer")
st.caption(
    "Bilateral real exchange rates from IMF monthly CPI and exchange-rate data "
    "(SDMX 3.0 API), January 1976 onward where available."
)

try:
    countries = load_country_list()
except IMFAPIError as err:
    st.error(f"Could not load the IMF country list. Please try again later.\n\n{err}")
    st.stop()

codes = list(countries.keys())


def _fmt(code: str) -> str:
    return f"{countries.get(code, code)} ({code})"


with st.sidebar:
    st.header("Countries")
    default_1 = codes.index("USA") if "USA" in codes else 0
    default_2 = codes.index(EURO_AREA) if EURO_AREA in codes else min(1, len(codes) - 1)
    c1 = st.selectbox("Country 1 (base currency)", codes, index=default_1, format_func=_fmt)
    c2 = st.selectbox("Country 2 (quote currency)", codes, index=default_2, format_func=_fmt)
    st.markdown(
        "**S(t)** = units of Country 2 currency per 1 unit of Country 1 currency  \n"
        "**RER(t)** = S(t) × P1(t) / P2(t)  \n"
        "**Real Index(t)** = 100 × RER(t) / RER*"
    )

if c1 == c2:
    st.warning("Please choose two different countries.")
    st.stop()

name1, name2 = countries.get(c1, c1), countries.get(c2, c2)

with st.spinner(f"Fetching IMF data for {name1} and {name2}…"):
    try:
        e1 = load_usd_rate(c1)
        e2 = load_usd_rate(c2)
        p1, p1_type = load_cpi(c1)
        p2, p2_type = load_cpi(c2)
    except IMFAPIError as err:
        st.error(f"The IMF API could not be reached. Please try again later.\n\n{err}")
        st.stop()

coverage = pd.DataFrame(
    [
        coverage_row(f"{name1}: exchange rate (LCU per USD)", e1),
        coverage_row(f"{name2}: exchange rate (LCU per USD)", e2),
        coverage_row(f"{name1}: {p1_type} index", p1),
        coverage_row(f"{name2}: {p2_type} index", p2),
    ]
)

panel = build_panel(e1, e2, p1, p2)

# Helpful hints about structural gaps
latest_any = max((s.index.max() for s in (e1, e2, p1, p2) if not s.empty), default=None)
for code, name, er in ((c1, name1, e1), (c2, name2, e2)):
    if er.empty:
        st.warning(f"No IMF monthly exchange-rate data (XDC_USD) found for {name}.")
    elif latest_any is not None and er.index.max() < latest_any - pd.DateOffset(months=24):
        msg = f"The IMF exchange-rate series for {name} ends in {er.index.max():%Y-%m}."
        if code in EURO_MEMBERS:
            msg += " This economy uses the euro; select **Euro Area (EA)** for later periods."
        st.info(msg)
for name, cpi in ((name1, p1), (name2, p2)):
    if cpi.empty:
        st.warning(f"No IMF monthly CPI data found for {name}.")
if EURO_AREA in (c1, c2):
    st.caption("Euro area prices use the IMF's HICP index (the IMF publishes no separate CPI for G163).")

if panel.empty or not panel["valid"].any():
    st.error(
        "There are no months in which both exchange rates and both price indices are available, "
        "so the RER cannot be computed for this pair."
    )
    with st.expander("Data coverage", expanded=True):
        st.dataframe(coverage, hide_index=True)
    st.stop()

valid_months = panel.index[panel["valid"]]
first_valid, last_valid = valid_months.min(), valid_months.max()
month_options = list(pd.date_range(first_valid, last_valid, freq="MS"))

with st.sidebar:
    st.header("Sample period")
    start_m, end_m = st.select_slider(
        "Start and end months",
        options=month_options,
        value=(month_options[0], month_options[-1]),
        format_func=lambda d: d.strftime("%b %Y"),
        key=f"sample_{c1}_{c2}",  # reset the range when the pair changes
    )
    st.header("Chart")
    view = st.radio("Series to plot", ["Real", "Nominal", "Both"], index=2, horizontal=True)

sample = panel.loc[start_m:end_m].copy()
n_valid = int(sample["valid"].sum())

if n_valid == 0:
    st.error("The selected sample contains no valid months. Please widen the date range.")
    st.stop()

rer_star = float(sample.loc[sample["valid"], "RER"].mean())
sample["RER_star"] = rer_star
sample["Real_Index"] = 100.0 * sample["RER"] / rer_star

# ---- Headline metrics -----------------------------------------------------------------
last_row = sample[sample["valid"]].iloc[-1]
gap_pct = last_row["Real_Index"] - 100.0  # % deviation of RER from RER*
if round(gap_pct, 1) > 0:
    valuation = "Overvalued"
elif round(gap_pct, 1) < 0:
    valuation = "Undervalued"
else:
    valuation = "At average"

m1, m2, m3, m4, m5 = st.columns(5)
m1.metric("RER* (sample mean)", f"{rer_star:,.4g}")
m2.metric(
    f"Real Index, {last_row.name:%b %Y}",
    f"{last_row['Real_Index']:,.1f}",
    f"{last_row['Real_Index'] - 100:+.1f} vs. average",
    delta_color="off",
)
m3.metric(f"S(t), {last_row.name:%b %Y}", f"{last_row['S']:,.4g}")
m4.metric(
    f"{c1} currency vs. {c2}, {last_row.name:%b %Y}",
    valuation,
    f"{gap_pct:+.1f}% vs. RER*",
    delta_color="off",
    help=(
        f"Compares the latest RER with its average (RER*) over the selected sample. "
        f"Above average means the {name1} currency is overvalued in real terms against the "
        f"{name2} currency, relative to its own history. This is a relative-PPP view, "
        f"so the answer depends on the sample period you choose."
    ),
)
m5.metric("Valid months", f"{n_valid} / {len(sample)}")

st.caption(
    f"S(t) is in units of **{name2}** currency per 1 unit of **{name1}** currency. "
    f"A rise in the Real Index is a real appreciation of the {name1} currency against the "
    f"{name2} currency (goods in {name1} become relatively more expensive)."
)

# ---- Chart -----------------------------------------------------------------------------
nominal_label = f"S(t): {c2} currency per 1 {c1} currency"
real_label = "Real Index (sample mean = 100)"
hover_month = "%{x|%b %Y}"
REAL_COLOR, NOMINAL_COLOR = "#1f77b4", "#ff7f0e"

if view == "Both":
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(
        go.Scatter(
            x=sample.index, y=sample["Real_Index"], name="Real Index",
            mode="lines", line=dict(width=2, color=REAL_COLOR),
            hovertemplate=f"{hover_month}<br>Real Index: %{{y:,.2f}}<extra></extra>",
        ),
        secondary_y=False,
    )
    fig.add_trace(
        go.Scatter(
            x=sample.index, y=sample["S"], name="Nominal S(t)",
            mode="lines", line=dict(width=2, color=NOMINAL_COLOR),
            hovertemplate=f"{hover_month}<br>S(t): %{{y:,.6g}}<extra></extra>",
        ),
        secondary_y=True,
    )
    fig.add_hline(y=100, line=dict(color="gray", width=1, dash="dash"), secondary_y=False)
    fig.update_yaxes(title_text=real_label, secondary_y=False)
    fig.update_yaxes(title_text=nominal_label, secondary_y=True, showgrid=False)
    title = f"Real Index and nominal exchange rate — {name1} vs. {name2}"
elif view == "Real":
    fig = go.Figure(
        go.Scatter(
            x=sample.index, y=sample["Real_Index"], name="Real Index",
            mode="lines", line=dict(width=2, color=REAL_COLOR),
            hovertemplate=f"{hover_month}<br>Real Index: %{{y:,.2f}}<extra></extra>",
        )
    )
    fig.add_hline(y=100, line=dict(color="gray", width=1, dash="dash"))
    fig.update_yaxes(title_text=real_label)
    title = f"Real Index — {name1} vs. {name2}"
else:
    fig = go.Figure(
        go.Scatter(
            x=sample.index, y=sample["S"], name="Nominal S(t)",
            mode="lines", line=dict(width=2, color=NOMINAL_COLOR),
            hovertemplate=f"{hover_month}<br>S(t): %{{y:,.6g}}<extra></extra>",
        )
    )
    fig.update_yaxes(title_text=nominal_label)
    title = f"Nominal exchange rate — {name1} vs. {name2}"

fig.update_layout(
    title=title,
    hovermode="x unified",
    height=520,
    margin=dict(l=10, r=10, t=60, b=10),
    legend=dict(orientation="h", yanchor="bottom", y=1.0, xanchor="right", x=1),
)
fig.update_xaxes(title_text="Month", rangeslider=dict(visible=True))
st.plotly_chart(fig, width="stretch")
st.caption(
    "🔍 The small chart under the main one is a range slider for zooming: drag its ends or "
    "the shaded window to look at a shorter stretch, and double-click the main chart to reset. "
    "It changes the view only. RER*, the Real Index and the CSV always use the "
    "**Sample period** set in the sidebar."
)

# ---- Table and CSV download -----------------------------------------------------------
export = pd.DataFrame(
    {
        "Month": sample.index.strftime("%Y-%m"),
        f"{c1} LCU per USD (IMF)": sample["E1"].values,
        f"{c2} LCU per USD (IMF)": sample["E2"].values,
        f"S(t) {c2} per 1 {c1}": sample["S"].values,
        f"P1(t) {c1} {p1_type}": sample["P1"].values,
        f"P2(t) {c2} {p2_type}": sample["P2"].values,
        "RER(t) = S*P1/P2": sample["RER"].values,
        "Valid month": sample["valid"].values,
        "RER* (sample mean)": sample["RER_star"].values,
        "Real Index(t) = 100*RER/RER*": sample["Real_Index"].values,
    }
)

st.download_button(
    "⬇️ Download CSV (selected sample)",
    data=export.to_csv(index=False).encode("utf-8"),
    file_name=f"rer_{c1}_{c2}_{start_m:%Y%m}_{end_m:%Y%m}.csv",
    mime="text/csv",
)

with st.expander("Observations and calculations in the selected sample"):
    st.dataframe(export, hide_index=True)

with st.expander("Data coverage and sources"):
    st.dataframe(coverage, hide_index=True)
    st.markdown(
        f"""
- **Exchange rates:** IMF `ER` dataflow, key `{{ISO3}}.XDC_USD.PA_RT.M`
  (period-average domestic currency per USD). Converted as
  S(t) = ({c2} per USD) / ({c1} per USD).
- **Prices:** IMF `CPI` dataflow, key `{{ISO3}}.CPI._T.IX.M`
  (falls back to `HICP` when no CPI series exists, e.g. Euro area `{EURO_AREA}`).
- Months where any of the four inputs is missing are kept in the CSV but excluded from RER*.
- Data fetched from `{API_BASE}` and cached for up to {CACHE_TTL // 3600} hours.
  Retrieved {date.today():%Y-%m-%d}.
"""
    )
