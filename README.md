# Real Exchange Rate Explorer

A Streamlit app that computes the bilateral **real exchange rate (RER)** between any two
IMF economies (including the **Euro area**) from monthly IMF CPI and exchange-rate data,
starting January 1976 where available.

## What it computes

| Symbol | Definition |
|---|---|
| S(t) | Nominal exchange rate: units of **Country 2** currency per 1 unit of **Country 1** currency |
| P1(t), P2(t) | Monthly CPI of Country 1 and Country 2 |
| RER(t) | S(t) × P1(t) / P2(t) |
| RER* | Average of RER(t) over the valid months in the selected sample |
| Real Index(t) | 100 × RER(t) / RER* |

The IMF publishes exchange rates as domestic currency units per USD, so the app converts them:

```
S(t) = (Country 2 LCU per USD) / (Country 1 LCU per USD)
```

A rise in the Real Index is a real appreciation of Country 1's currency against Country 2's.
S(t) is plotted in actual currency units and is never normalized.

## Features

- Choose Country 1 and Country 2 from every economy with IMF monthly CPI and USD exchange-rate
  data, plus the Euro area (`G163`).
- Pick start and end months; RER* and the Real Index are recalculated whenever the range changes.
- Interactive Plotly charts: **Real**, **Nominal**, or **Both** (two y-axes).
- Download a CSV of every month in the selected sample: inputs, S(t), RER(t), a valid-month flag,
  RER*, and the Real Index.
- A data-coverage table shows the first and last month and the observation count of each input series.

## Data source

IMF SDMX 3.0 API: `https://api.imf.org/external/sdmx/3.0`

| Series | Dataflow | Key |
|---|---|---|
| CPI (headline index) | `IMF.STA/CPI` | `{ISO3}.CPI._T.IX.M` |
| Exchange rate (period average, LCU per USD) | `IMF.STA/ER` | `{ISO3}.XDC_USD.PA_RT.M` |

Example request:

```
https://api.imf.org/external/sdmx/3.0/data/dataflow/IMF.STA/CPI/+/USA.CPI._T.IX.M?c[TIME_PERIOD]=ge:1976-01
```

No API key is needed. Responses are cached for 6 hours (country list: 24 hours).

### Data caveats

- **Gaps and coverage.** Series can start late, end early, or have missing months. A month is
  *valid* only when both exchange rates and both CPIs exist. Invalid months appear in the CSV
  (with `Valid month = False`) but are left out of RER* and show as gaps in the charts.
- **Euro area prices.** The IMF publishes the Euro area (`G163`) price index as **HICP**
  (`G163.HICP._T.IX.M`, from about 1999/2000), not CPI. The app uses HICP when no CPI series exists.
- **Euro area member countries.** National exchange-rate series for euro adopters (for example
  Germany or France) stop when the country joined the euro, so they only cover legacy currencies.
  Select **Euro Area (EA)** for later periods. The app shows a notice when this happens.
- **United States.** USD per USD is 1. If the IMF returns no USA series, the app uses a constant 1.
- **Index base years.** CPI base years differ across countries, so the *level* of RER(t) has no
  meaning on its own. The Real Index removes this by normalizing to the sample mean.
- **Redenominations.** The IMF series are expressed in the current currency unit, but very large
  redenominations (for example Venezuela) can make nominal charts hard to read.

## Run locally (Python 3.12)

```bash
python3.12 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

The app opens at http://localhost:8501.

## Deploy to Streamlit Community Cloud

1. Push `app.py`, `requirements.txt` and `README.md` to a GitHub repository:
   ```bash
   git init
   git add app.py requirements.txt README.md
   git commit -m "Real Exchange Rate Explorer"
   git branch -M main
   git remote add origin https://github.com/<your-user>/<your-repo>.git
   git push -u origin main
   ```
2. Sign in at [share.streamlit.io](https://share.streamlit.io) and click **Create app**.
3. Choose the repository, branch `main`, and main file `app.py`.
4. Under **Advanced settings**, set **Python version** to **3.12**, then click **Deploy**.

## Project structure

```
.
├── app.py            # Streamlit app: IMF API client, RER calculations, charts, CSV export
├── requirements.txt  # Python dependencies
└── README.md
```

## Disclaimer

Data © International Monetary Fund, used under the IMF's terms of use. This app is for
educational and analytical purposes. Check figures against the original IMF releases before
relying on them.
