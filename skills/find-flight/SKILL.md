---
name: find-flight
description: Use when searching for flights, checking flight availability or prices, building flight queries, or booking research. Wraps the fast-flights Python library (Google Flights scraper) — structured queries via create_query/FlightQuery, natural-language queries, or returning ResultList flight data through get_flights.
---

# Flight Skill

> Search Google Flights from Python using the **fast-flights** library. Build a query (structured or natural language), fetch results, and read `ResultList` flight data.

## Overview

`fast-flights` is a Python Google Flights scraper. An agent uses it by writing small Python programs: construct a search via `create_query` (or a plain sentence), call `get_flights`, and iterate the returned `ResultList`. Everything is strongly typed.

```bash
pip install fast-flights
# deps pulled automatically: primp, protobuf>=5.27, selectolax  (Python >= 3.10)
```

## When to Use

- Find flights/times/prices between airports for a date
- Compare prices across dates, seats, or filter by stops/airlines/layovers
- Travel-planning or trip-recommendation tasks that need real availability
- Getting per-segment legs, durations, plane types, or carbon emissions

**When NOT:** for booking/reservation actual purchase (this is read-only search), or non-flight transport.

## Before you query (REQUIRED gate)

**Never guess the date, the trip type, or the airport codes.** All three are common model failure points — resolve them *before* building any query:

1. **Get the real current date from the system** — run `date` in a shell (or read the runtime clock), not from memory:
   ```bash
   date "+%Y-%m-%d"
   ```
   Then resolve relative/ambiguous dates ("next Friday", "11-13 September") against this real year. Format every date as `YYYY-MM-DD`.
2. **Confirm one-way vs round-trip.** If the user didn't explicitly say, ASK: one-way (1 leg, needs only `date`+`from`+`to`) vs round-trip (2+ legs, needs a `return_date` too). Do not assume. If a stay duration or "until" date is given, that implies round-trip — but still confirm the dates.
3. **Verify the airport codes — never guess what a 3-letter IATA code means.** Models routinely map codes to the wrong city (e.g. misreading `ECN` as Edinburgh when it is really Ercan, or inventing a code). Before searching, confirm what `from_airport`/`to_airport` actually are: look the code up in the upstream airport list (`enums/airports.csv` in `AWeirdDev/flights`, ~9.7k rows, by code + name + country) or against a known route. If you can't verify a code, ask the user instead of assuming.

   **Country & airport awareness is built into the tools in this package.** The `search_flights` tool and `flight_runner.py` bundle a code → (airport name, city, state, ISO-3166 country) lookup (`airports.py` + `airports.json`/`countries.json`). Every search resolves both endpoint codes to their city and country **before** calling Google Flights, rejects an unknown code with a clear error, and returns per-leg `*_city`, `*_state`, `*_country` and `*_country_name` fields plus a human-readable `route.summary` (e.g. `IST Istanbul, Turkey -> ECN Nicosia, Cyprus`). So the agent never has to guess a code's meaning — the tool surfaces it.

   Still treat output resolution as a *cross-check*, not a replacement for intent: an ambiguous request (e.g. user says "St. John's" but a code resolves elsewhere) should still be confirmed against the user's intent.

## Core Recipe — structured query

```python
from fast_flights import (
    FlightQuery, Passengers, ResultList,
    create_query, get_flights, FlightsNotFound,
)

query = create_query(
    flights=[
        FlightQuery(                       # one FlightQuery per leg
            date="2025-01-01",             # "YYYY-MM-DD" or a datetime
            from_airport="TPE",            # 3-letter IATA code
            to_airport="MYJ",
            max_stops=1,
            airlines=["JL", "ONEWORLD"],   # IATA codes, or SKYTEAM/STAR_ALLIANCE/ONEWORLD
            earliest_departure_hour=7,     # local time, 0-23
            latest_departure_hour=18,
            earliest_arrival_hour=10,
            latest_arrival_hour=23,
            max_duration_minutes=720,      # minutes
            connecting_airports=["HND", "NRT"],
            min_layover_minutes=60,
            max_layover_minutes=240,
            less_emissions_only=True,
        ),
    ],
    seat="economy",                        # economy | premium-economy | business | first
    trip="one-way",                        # one-way | round-trip (multi-city unimplemented)
    passengers=Passengers(adults=1),       # adults/children/infants_in_seat/infants_on_lap
    language="en-US",                      # "" lets Google decide
    currency="USD",                        # "" lets Google decide
    max_price=1500,                        # whole-search filters ↓
    carry_on_bags=0,
    checked_bags=0,
    hide_separate_and_self_transfer=False,
    exclude_basic_economy=False,
)

try:
    result: ResultList = get_flights(query)
    for flight in result:
        print(flight.price, flight.airlines, flight.type)
except FlightsNotFound:
    print("no flights found")
```

### Round-trip example

A round-trip is two `FlightQuery` legs in one `create_query` call — outbound then return (reverse airports, return date):

```python
query = create_query(
    flights=[
        FlightQuery(date="2025-01-01", from_airport="IST", to_airport="ECN"),
        FlightQuery(date="2025-01-08", from_airport="ECN", to_airport="IST"),
    ],
    trip="round-trip",
    seat="economy",
    passengers=Passengers(adults=1),
    currency="EUR",
)
```

## Natural-language path (agent-friendly)

`get_flights` also accepts a plain sentence instead of a `Query`:

```python
from fast_flights import get_flights
result = get_flights(
    "Flights from TPE to MYJ on 2025-01-01 one way economy class"
)
```

Preferred when the request arrives as English text; use the structured recipe when the user gives discrete filters.

## Reading results — the data model

`ResultList` is a list of `Flights`, with metadata attached.

```python
result.metadata            # JsMetadata: .airlines[], .alliances[] (code+name)
for flight in result:
    # flight: Flights
    flight.price           # int, in selected currency
    flight.airlines        # list[str] of IATA codes
    flight.type            # str | "multi"
    flight.carbon          # CarbonEmission: .typical_on_route, .emission (grams)
    for leg in flight.flights:     # SingleFlight
        leg.from_airport   # Airport: .code, .name
        leg.to_airport     # Airport: .code, .name
        leg.departure      # SimpleDatetime: .date=(Y,M,D), .time=(H,M)
        leg.arrival        # SimpleDatetime: .date=(Y,M,D), .time=(H,M)
        leg.duration       # int, minutes
        leg.plane_type     # str
```

**Country & airport codes (the bundled tools add these).** When you call `search_flights` or `flight_runner.py` (not raw fast-flights), each leg is enriched with the resolved airport metadata from the bundled lookup — `from_city`, `from_state`, `from_country` (ISO-3166 alpha-2), `from_country_name`, and the matching `to_*` fields — plus `from_name`/`to_name`. A top-level `route` object gives both endpoints' resolved metadata and a ready-to-read `route.summary` like `IST Istanbul, Turkey -> ECN Nicosia, Cyprus`. This is how the agent knows the country and airport code with certainty instead of guessing.

**Currency is NOT attached to prices.** `flight.price` is a bare `int` with no currency symbol or code. The currency that the price is quoted in is whatever you passed to `create_query(currency=...)` (or Google's default if you passed `""` — often the local currency, e.g. Turkish Lira for a Turkey-originating route). When presenting prices, surface the currency explicitly: track the `currency` you passed (or the user's expected currency) and label every price with it. Never hardcode a currency symbol in your output format — it may not match the price's actual currency.

**Round-trip legs — raw `fast-flights` gives only OUTBOUND (verified).** A single `create_query(..., trip="round-trip")` returns `flight.flights` with **only the outbound legs** — no return-leg detail — and `flight.price` as the **two-way total fare**. So on raw fast-flights, count stops/display legs as the outbound itinerary; do NOT invent return segments that aren't in the data.

**Round-trip legs — the bundled tools show BOTH directions.** `search_flights` / `flight_runner.py` do not use the raw round-trip query. Instead they run **two separate one-way searches** (outbound `src->dst`, return `dst->src`) and return them as `outbound` and `return` arrays, each with its **own price and full legs**. Each direction gets its own `outbound_route` / `return_route` metadata. They do NOT manufacture a pairing between a specific outbound and a specific return, because the API exposes no booking token linking them.

`FlightsNotFound` is raised when nothing matches (or Google returns an error status).

### Sorting / narrowing results

`ResultList` is a plain list.sort()-able list — sort by price or total duration before presenting to the user:

```python
def total_duration(flight):
    return sum(seg.duration for seg in flight.flights)

result.sort(key=lambda f: f.price)      # cheapest first
result.sort(key=total_duration)          # shortest trip first
best = result[0]                         # or result[:3] for top-N
```

## Options reference

| Option | Where | Values / notes |
|---|---|---|
| `seat` | create_query | `economy`, `premium-economy`, `business`, `first` |
| `trip` | create_query | `one-way`, `round-trip`. **round-trip needs 2+ `FlightQuery` legs**; `multi-city` unimplemented |
| `passengers` | create_query | `Passengers(adults, children, infants_in_seat, infants_on_lap)` |
| `language` / `currency` | create_query | any supported IETF tag / ISO code; `""` = Google default |
| `max_price`, `carry_on_bags`, `checked_bags`, `hide_separate_and_self_transfer`, `exclude_basic_economy` | create_query | search-wide |
| Per-leg filters | FlightQuery | `max_stops`, `airlines`, departure/arrival hour windows, `max_duration_minutes`, `connecting_airports`, `min/max_layover_minutes`, `less_emissions_only` |

## Gotchas

- **Hours use local airport time** on a 0–23 clock; durations/layovers are in minutes.
- **`airlines` on non-first legs is ignored** — Google applies only the first leg's airline filter to the whole search. (The filter also accepts an alliance name.)
- **`max_stops` can be passed once on `create_query`** as a global, and/or per-leg via `FlightQuery` (per-leg wins).
- **Round-trip = two+ `FlightQuery` objects**; one way = one.
- **Passengers rules:** total ≤ 9, and `infants_on_lap ≤ adults` (one lap infant needs one adult). Violations raise `AssertionError`.
- **Read-only:** this scrapes availability; it cannot book. No guarantee against rate limits / IP blocks — proxy via an integration if scraping at scale.
- **Live network dependency:** `get_flights` hits Google's servers at call time. For repeatable/offline testing, use `fetch_flights_html` or cached HTML, or an integration (BrightData / SearchApi).

## Integrations (optional)

- **BrightData** — proxy the default fetcher to protect your IP (pass `integration=BrightData(api_key=...)`). Only needed if scraping at scale.
- **SearchApi** — richer data source. Returns `SearchApiResult` (booking options, price insights, amenities) instead of `ResultList`:
  ```python
  from fast_flights.integrations import SearchApi
  result = get_flights(..., integration=SearchApi())
  ```

## Common Mistakes

| Mistake | Fix |
|---|---|
| Using `get_flights` without try/except | Wrap in `try/except FlightsNotFound` |
| Round-trip with one leg | Add a second `FlightQuery` (e.g. return leg) |
| Expecting `multi-city` to work | Unimplemented — treat as not supported today |
| Passing `airlines` to multiple legs expecting per-leg filtering | Only first leg's `airlines` is applied |
| Assuming 24h timestamps | Times are `(hour, minute)` on a 0–23 clock |
| Guessing what a 3-letter airport code means | Verify it against the upstream `enums/airports.csv` (or a known route) before searching; the bundled `search_flights` tool resolves codes to city + country for you |
| Hardcoding a currency symbol in output | `price` has no currency — track/pass `currency` and label prices with it |
| Assuming return legs are in raw round-trip data | Raw `fast-flights` returns only outbound legs; but the bundled `search_flights` tool runs two directional searches and returns `outbound` + `return`, so both legs ARE shown there |

## Real-world notes

- Version: `fast-flights` 3.x. See upstream repo `AWeirdDev/flights` and docs `aweirddev.github.io/flights`.
- Run a ready-made CLI: `python examples/search_flights.py --from TPE --to MYJ --date 2025-01-01 --seat economy`.
- **Round-trip legs (verified):** the bundled `search_flights` tool runs two one-way searches (outbound + return) and returns both directions with full legs, each with its own price. Raw `fast-flights` still returns only outbound legs for a single round-trip query.
- **pi extension:** if the `search_flights` custom tool is active (via `extensions/flight-search.ts`), prefer calling it over writing ad-hoc Python — it returns structured, correctly-labelled results and leaves no scratch files. `flight_runner.py` is the equivalent standalone executor.
