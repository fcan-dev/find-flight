#!/usr/bin/env python3
"""Search Google Flights via fast-flights and emit structured JSON.

This is the deterministic executor behind the pi `search_flights` extension
tool (and usable standalone). It reads a JSON query object on stdin and writes
a JSON result object on stdout.

Query keys (all optional unless noted):
  from_airport (required), to_airport (required)
  outbound_date (required): "YYYY-MM-DD"
  return_date: "YYYY-MM-DD" for round-trip
  trip: "one-way" | "round-trip"
  seat: economy | premium-economy | business | first
  adults, children, infants_in_seat, infants_on_lap: int
  currency: ISO code or "" (Google default)
  language: IETF tag or ""
  max_stops, max_price: int
  carry_on_bags, checked_bags, hide_separate_and_self_transfer,
  exclude_basic_economy: int/bool

Errors are reported as JSON with {"error": "..."} and a non-zero exit code, so
the caller can distinguish "no flights" (valid empty result) from a failure.

Output shape (ResultList -> list):
  [{ "price": int, "currency": str, "is_round_trip": bool, "airlines": [..],
     "type": str, "carbon": {"typical_on_route": int, "emission": int},
     "stops": int, "legs": [leg...] }]

IMPORTANT (fast-flights behavior, verified): `flight.flights` contains ONLY the
OUTBOUND legs. For a round-trip, `price` is the TOTAL round-trip fare, but no
return-leg detail is present in the parsed data. So the output describes the
outbound itinerary; set `is_round_trip` when return was requested so the caller
knows `price` is the two-way total.

Each leg: { "from": code, "from_name": str, "to": code, "to_name": str,
            "from_city": str, "to_city": str,
            "from_country": str, "to_country": str,
            "from_country_name": str, "to_country_name": str,
            "date": "YYYY-MM-DD", "departure": "HH:MM", "arrival": "HH:MM",
            "duration_minutes": int, "plane": str }

Metadata: airport names/cities and ISO-3166 country codes are resolved from
the bundled lookup (airports.py + airports.json/countries.json) so the caller
knows exactly which city and country each code refers to (disambiguation).
"""
from __future__ import annotations

import json
import sys

from fast_flights import (
    FlightsNotFound,
    FlightQuery,
    Passengers,
    create_query,
    get_flights,
)

import airports as airport_lookup

SEATS = ("economy", "premium-economy", "business", "first")
TRIPS = ("one-way", "round-trip")


def _hhmm(t) -> str:
    return f"{t[0]:02d}:{t[1]:02d}"


def _leg(seg) -> dict:
    f = airport_lookup.resolve(seg.from_airport.code)
    t = airport_lookup.resolve(seg.to_airport.code)
    return {
        "from": seg.from_airport.code,
        "from_name": f["name"] or seg.from_airport.name,
        "from_city": f["city"],
        "from_state": f["state"],
        "from_country": f["country"],
        "from_country_name": f["country_name"],
        "to": seg.to_airport.code,
        "to_name": t["name"] or seg.to_airport.name,
        "to_city": t["city"],
        "to_state": t["state"],
        "to_country": t["country"],
        "to_country_name": t["country_name"],
        "date": "%04d-%02d-%02d" % seg.departure.date,
        "departure": _hhmm(seg.departure.time),
        "arrival": _hhmm(seg.arrival.time),
        "duration_minutes": seg.duration,
        "plane": seg.plane_type,
    }


def run(q: dict) -> dict:
    seat = q.get("seat", "economy")
    trip = q.get("trip", "one-way")
    if seat not in SEATS:
        raise ValueError(f"seat must be one of {SEATS}")
    if trip not in TRIPS:
        raise ValueError(f"trip must be one of {TRIPS}")

    outbound_date = q["outbound_date"]
    src = q["from_airport"].strip().upper()
    dst = q["to_airport"].strip().upper()
    for code in (src, dst):
        if not (code.isalpha() and len(code) == 3):
            raise ValueError(f"airport code must be 3 letters, got {code!r}")
        if not airport_lookup.is_known(code):
            raise ValueError(
                f"unknown airport code {code!r}: not in the airport database "
                f"(resolve it to a valid IATA code first)"
            )

    legs = [FlightQuery(date=outbound_date, from_airport=src, to_airport=dst)]
    return_date = q.get("return_date")
    if trip == "round-trip":
        if not return_date:
            raise ValueError("return_date is required for round-trip")
        legs.append(FlightQuery(date=return_date, from_airport=dst, to_airport=src))

    # Resolved endpoint metadata (disambiguation): which city & country each
    # airport code belongs to. This is known before any network call.
    route = {
        "from": airport_lookup.resolve(src),
        "to": airport_lookup.resolve(dst),
    }
    route["summary"] = (
        f"{route['from']['code']} {route['from']['city']}, "
        f"{route['from']['country_name'] or route['from']['country']} -> "
        f"{route['to']['code']} {route['to']['city']}, "
        f"{route['to']['country_name'] or route['to']['country']}"
    )

    query = create_query(
        flights=legs,
        trip=trip,
        seat=seat,
        passengers=Passengers(
            adults=int(q.get("adults", 1)),
            children=int(q.get("children", 0)),
            infants_in_seat=int(q.get("infants_in_seat", 0)),
            infants_on_lap=int(q.get("infants_on_lap", 0)),
        ),
        currency=q.get("currency", ""),
        language=q.get("language", ""),
        max_stops=q.get("max_stops"),
        max_price=q.get("max_price"),
        carry_on_bags=int(q.get("carry_on_bags", 0)),
        checked_bags=int(q.get("checked_bags", 0)),
        hide_separate_and_self_transfer=bool(q.get("hide_separate_and_self_transfer", False)),
        exclude_basic_economy=bool(q.get("exclude_basic_economy", False)),
    )

    try:
        result = get_flights(query)
    except FlightsNotFound:
        return {
            "currency": q.get("currency", "") or "default",
            "route": route,
            "flights": [],
        }

    currency = q.get("currency", "") or "default"
    out = [
        {
            "price": f.price,
            "currency": currency,
            "is_round_trip": trip == "round-trip",
            "airlines": f.airlines,
            "type": f.type,
            "carbon": {
                "typical_on_route": f.carbon.typical_on_route,
                "emission": f.carbon.emission,
            },
            "stops": len(f.flights) - 1,
            "legs": [_leg(x) for x in f.flights],
        }
        for f in result
    ]
    return {"currency": currency, "route": route, "flights": out}


def main() -> int:
    try:
        q = json.load(sys.stdin)
        payload = run(q)
    except Exception as e:  # noqa: BLE001 - report any failure to the caller
        print(json.dumps({"error": str(e)}))
        return 1
    print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
