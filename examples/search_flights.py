#!/usr/bin/env python3
"""Search Google Flights with fast-flights from the command line.

Requires: pip install fast-flights

Examples:
    python search_flights.py --from TPE --to MYJ --date 2025-01-01
    python search_flights.py --from TPE --to MYJ --date 2025-01-01 --trip round-trip --return-date 2025-01-08 --seat business
    python search_flights.py --from TPE --to MYJ --date 2025-01-01 --max-stops 1 --max-price 500 --currency USD --sort price --limit 5
    echo "Flights from TPE to MYJ on 2025-01-01 one way economy" | python search_flights.py --text
"""
from __future__ import annotations

import argparse
import sys
from datetime import date as Date
from pathlib import Path

# Install the tolerant fast-flights parser before any search: raw fast_flights
# 3.1.0 crashes on price-less "price on request" segments (e.g. Eastar Jet on
# ICN->KIX). flight_runner lives in the repo root, not next to this script.
# Standalone copies (no repo root) keep working with the raw parser + a warning.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if (_REPO_ROOT / "flight_runner.py").is_file():
    sys.path.insert(0, str(_REPO_ROOT))
    try:
        import flight_runner  # noqa: E402,F401  (import installs the parser patch)
    except Exception as _e:  # noqa: BLE001
        print(f"warning: tolerant parser unavailable ({_e!r}); "
              "raw fast-flights may crash on some pages", file=sys.stderr)
else:
    print("note: standalone mode (no flight_runner.py found); using raw "
          "fast-flights parser — some pages may crash", file=sys.stderr)

from fast_flights import (
    FlightsNotFound,
    FlightQuery,
    Passengers,
    ResultList,
    create_query,
    get_flights,
)

TRIPS = ("one-way", "round-trip")
SEATS = ("economy", "premium-economy", "business", "first")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Search Google Flights with fast-flights.")
    p.add_argument("--text", action="store_true",
                   help="Read a natural-language query from stdin instead of structured flags.")
    p.add_argument("--from", dest="from_airport", required=False,
                   help="3-letter IATA departure airport.")
    p.add_argument("--to", dest="to_airport", required=False,
                   help="3-letter IATA arrival airport.")
    p.add_argument("--date", type=Date.fromisoformat,
                   help="Departure date, YYYY-MM-DD.")
    p.add_argument("--return-date", type=Date.fromisoformat,
                   help="Return date for round-trip, YYYY-MM-DD.")
    p.add_argument("--trip", choices=TRIPS, default="one-way")
    p.add_argument("--seat", choices=SEATS, default="economy")
    p.add_argument("--adults", type=int, default=1)
    p.add_argument("--children", type=int, default=0)
    p.add_argument("--currency", default="")
    p.add_argument("--language", default="")
    p.add_argument("--max-stops", type=int)
    p.add_argument("--max-price", type=int)
    p.add_argument("--sort", choices=("price", "duration"), default=None,
                   help="Sort results by price (ascending) or total duration (ascending).")
    p.add_argument("--limit", type=int, default=0,
                   help="Show only the top N itineraries after sorting (0 = all).")
    return p


def leg(date: Date, src: str, dst: str, max_stops: int | None) -> FlightQuery:
    src, dst = src.strip().upper(), dst.strip().upper()
    for code in (src, dst):
        if not code.isalpha() or len(code) != 3:
            sys.exit(f"airport codes must be 3 letters, got {code!r}")
    return FlightQuery(
        date=date.strftime("%Y-%m-%d"),
        from_airport=src,
        to_airport=dst,
        max_stops=max_stops,
    )


def build_query(args: argparse.Namespace):
    flights = [leg(args.date, args.from_airport, args.to_airport, args.max_stops)]
    if args.trip == "round-trip":
        if not args.return_date:
            sys.exit("--return-date is required for --trip round-trip")
        flights.append(leg(args.return_date, args.to_airport, args.from_airport, args.max_stops))

    return create_query(
        flights=flights,
        trip=args.trip,
        seat=args.seat,
        passengers=Passengers(adults=args.adults, children=args.children),
        currency=args.currency,
        language=args.language,
        max_price=args.max_price,
    )


def total_duration(flight) -> int:
    return sum(seg.duration for seg in flight.flights)


def load_airports():
    """Try to ""import airports"" for city/country metadata; fall back to a no-op."""
    try:
        import airports as a
        return a
    except Exception:
        return None


AP = load_airports()


def place(code: str) -> str:
    if AP:
        r = AP.resolve(code)
        if r["country"]:
            return f"{code} {r['city']}, {r['country']}".strip()
        if r["name"]:
            return f"{code} ({r['name']})"
    return code


def print_result(result: ResultList, currency: str = "") -> None:
    if not result:
        print("No flights found.")
        return
    sym = f"{currency} " if currency else "$"
    for flight in result:
        print(f"{sym}{flight.price}  {', '.join(flight.airlines)}  ({flight.type})")
        for seg in flight.flights:
            dep = f"{seg.departure.date[0]}-{seg.departure.date[1]:02d}-{seg.departure.date[2]:02d} {seg.departure.time[0]:02d}:{seg.departure.time[1]:02d}"
            arr = f"{seg.arrival.date[0]}-{seg.arrival.date[1]:02d}-{seg.arrival.date[2]:02d} {seg.arrival.time[0]:02d}:{seg.arrival.time[1]:02d}"
            print(f"  {place(seg.from_airport.code)} {dep} -> {place(seg.to_airport.code)} {arr} "
                  f"({seg.duration} min, {seg.plane_type})")
    print(f"\n{len(result)} itinerary/ies")


def main() -> int:
    args = build_parser().parse_args()

    if args.text:
        query = sys.stdin.read().strip()
        if not query:
            sys.exit("no text on stdin")
        result = get_flights(query)
    else:
        query = build_query(args)
        result = get_flights(query)

    if args.sort == "price":
        result.sort(key=lambda f: f.price)
    elif args.sort == "duration":
        result.sort(key=total_duration)
    if args.limit:
        result = result[: args.limit]

    print_result(result, currency=args.currency)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except FlightsNotFound:
        print("No flights found (Google returned an error/no results).")
        raise SystemExit(1)
