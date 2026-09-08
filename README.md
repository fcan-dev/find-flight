# find-flight

**Google Flights search for AI agents.** A [pi](https://github.com/earendil-works/pi) package that bundles:

- **a `search_flights` extension tool** — deterministic, structured flight searches the agent calls directly
- **a portable skill** — recipes and reference that work in pi, Claude Code, and Codex

Powered by the [`fast-flights`](https://github.com/AWeirdDev/flights) Google Flights scraper. Read-only — it fetches real availability and prices, but does **not** book anything.

## ✨ What it does

- Search flights between airports on given dates (one-way or round-trip)
- Real prices, airlines, per-leg times, duration, plane type, stop counts, carbon emissions
- Filters: seat class, passengers, currency, language, max stops, max price, exclude basic economy
- **Resolves every airport code to its name, city and ISO-3166 country** before searching (bundled lookup, ~9.7k airports), and returns that metadata per leg plus a ready-to-read route summary — so the extension/caller always knows the country and airport code with certainty
- Rejects unknown airport codes with a clear error instead of silently querying the wrong city
- Returns **structured** results — correct per-direction stop counts, no ad-hoc scripts left behind

## 📦 Install

Requires **Python 3.10+** with fast-flights, and live network access:

```bash
pip install fast-flights
```

### As a pi package (<img src="https://img.shields.io/badge/pi-package-5B8DEF)" width="60px" align="center">)

```bash
pi install git:github.com/<your-user>/find-flight
# or from npm if published:
pi install npm:find-flight
```

Or install only for the current run:

```bash
pi -e git:github.com/<your-user>/find-flight
```

After install, reload pi (`/reload`) — the agent gains a `search_flights` tool automatically.

### Manually (local dev / other harnesses)

```bash
# pi extension
ln -s "$(pwd)/extensions/flight-search.ts" ~/.pi/agent/extensions/flight-search.ts

# skill (pi)
ln -s "$(pwd)/skills/find-flight" ~/.pi/agent/skills/find-flight
# skill (Claude Code / Codex)
ln -s "$(pwd)/skills/find-flight" ~/.agents/skills/find-flight
ln -s "$(pwd)/skills/find-flight" ~/.claude/skills/find-flight
```

## 🚀 Usage

Just ask in natural language:

> *"Find one-way flights from IST to ECN on 12 September, economy, show cheapest 3"*
>
> *"Cheapest round-trip Istanbul to Paris, out 23 Oct return 25 Oct"*

The agent calls `search_flights(from_airport, to_airport, outbound_date, trip, return_date?, seat, currency, ...)` and gets structured JSON.

### Python executor (standalone)

`flight_runner.py` reads a JSON query on stdin and writes structured JSON on stdout — useful for testing or scripting without the agent:

```bash
echo '{"from_airport":"IST","to_airport":"ECN","outbound_date":"2026-09-12","trip":"one-way","currency":"EUR"}' \
  | python3 flight_runner.py
```

### CLI example

```bash
python examples/search_flights.py --from IST --to ECN --date 2026-09-12 --currency EUR --sort price --limit 3
```

## 📁 Layout

```
find-flight/
├── package.json               # pi package manifest (extensions + skills)
├── extensions/
│   └── flight-search.ts       # registerTool: search_flights
├── skills/
│   └── find-flight/SKILL.md   # portable agent skill
├── flight_runner.py           # Python executor (structured JSON)
├── airports.py                # airport/country lookup (code -> name, city, state, country)
├── airports.json              # ~9.7k IATA codes -> [name, city, state, ISO-3166 country]
├── countries.json             # ISO-3166 alpha-2 -> English country name
├── examples/
│   └── search_flights.py      # CLI example
└── README.md
```

## ⚙️ Notes / limitations

- **Airport & country resolution:** every endpoint code is resolved (name/city/ISO-3166 country) from the bundled lookup before searching; unknown codes error out. `airports.py`+`airports.json`/`countries.json` are regenerated from the upstream list (`AWeirdDev/flights` `enums/airports.csv`).
- **Round-trip legs (verified):** raw `fast-flights` returns only **outbound** legs for a single round-trip query. This package runs **two directional one-way searches** (outbound + return) and returns both directions with full legs, each with its own price and resolved country/city metadata.
- `multi-city` trips are not supported by the upstream library.
- Scraping Google is subject to rate limits / IP blocks. For heavy use, plug a proxy (BrightData) or richer source (SearchApi) integration into `fast-flights`.
- Prices are quoted in the `currency` you pass; if omitted, Google picks a default (often the local currency).
- Config: point the runner elsewhere with `FLIGHT_RUNNER=/path/to/flight_runner.py`, or choose a different interpreter with `FLIGHT_PYTHON=...`.

## 🔒 Security

This package executes Python (`flight_runner.py`) that makes live network requests to Google. Review the source before installing.

## License

MIT
