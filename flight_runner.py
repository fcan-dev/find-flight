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
  max_stops, max_price: int   (max_stops=0 = nonstop only; applied
     client-side because fast-flights 3.1.0 cannot parse nonstop-filtered pages)
  carry_on_bags, checked_bags, hide_separate_and_self_transfer,
  exclude_basic_economy: int/bool

Currency: explicit `currency` wins; otherwise the FLIGHT_CURRENCY env var
(default "USD"); FLIGHT_CURRENCY="" means "let Google decide" and the output
is labelled "google-default". Prices are never left without a label.

Errors are reported as JSON with {"error": "..."} and a non-zero exit code, so
the caller can distinguish "no flights" (valid empty result) from a failure.
The pi extension surfaces this as "flight search failed: <error>".

Hardening: fast-flights 3.1.0's parser crashes on two real-world payload
shapes — price-less "price on request" segments, and pages without a results
array. This runner installs a tolerant parser (below) that skips the former
and reports the latter as an error instead of a crash.

Output shapes:

  one-way   -> { currency, trip: "one-way", route, flights: [flight...] }
  round-trip-> { currency, trip: "round-trip", outbound_route, return_route,
                 round_trip: [flight...], return_options: [flight...], note }

Each flight: { "price": int, "currency": str, "price_type": str,
                "airlines": [..], "type": str,
                "carbon": {"typical_on_route": int, "emission": int},
                "stops": int, "legs": [leg...] }

ROUND-TRIP PRICING (fast-flights behavior, verified 2026-09-11): a single
native round-trip query (two FlightQuery legs, trip="round-trip") returns
flight.price as the TRUE two-way total fare, and flight.flights with ONLY the
outbound legs — the return direction is not in the data (no booking token
links a specific outbound with a specific return). NEVER estimate a
round-trip price by summing two one-way fares: one-way fares (especially
business) are often far higher than half the RT fare (e.g. IST<->BRU direct
business 2026-11-20/22 was USD 855 RT vs ~USD 1422 as a sum of one-ways).

So for round-trips the runner:
  1. runs the native RT query -> `round_trip[]`: true two-way totals with
     outbound legs (price_type "round-trip-total") — this is the price to
     present;
  2. runs ONE one-way search for the return direction -> `return_options[]`:
     real return-leg schedules priced at one-way fares for reference ONLY
     (price_type "one-way-reference").

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
import os
import sys

from fast_flights import (
    FlightsNotFound,
    FlightQuery,
    Passengers,
    create_query,
    get_flights,
)
from fast_flights.parser import (
    Airline,
    Airport,
    Alliance,
    CarbonEmission,
    Flights,
    JsMetadata,
    ResultList,
    SimpleDatetime,
    SingleFlight,
    _parse_time,
)
from selectolax.lexbor import LexborHTMLParser

import airports as airport_lookup


class UnparsedPageError(RuntimeError):
    """Google returned a page shape fast-flights 3.1.0 cannot parse.

    This is NOT "no results" — it must surface as an error, never as an
    empty result list.
    """


def _parse_js_tolerant(js: str) -> ResultList:
    """Tolerant copy of fast_flights.parser.parse_js (pinned to 3.1.0 layout).

    Upstream crashes on two real-world payload shapes:

    A) segments whose price array is empty, k[1] == [[], <booking token>] —
       "price on request" fares (seen: Eastar Jet on ICN->KIX, 2027-01-17).
       -> skipped: a fare with no price is not a bookable option here.
    B) payload[3] is None — pages without a results array (seen with
       nonstop-filtered queries). -> raised as UnparsedPageError, because
       it must never be reported as an empty result set.

    A script without a 'data:' payload is also UnparsedPageError, not
    IndexError.
    """
    if "data:" not in js:
        raise UnparsedPageError(
            "Google page script contains no 'data:' payload (page shape "
            "not supported by fast-flights 3.1.0)"
        )
    data = js.split("data:", 1)[1].rsplit(",", 1)[0]

    if data.endswith("errorHasStatus: true"):
        raise FlightsNotFound("no flights found; received error")

    payload = json.loads(data)

    alliances = []
    airlines = []

    (alliances_data, airlines_data) = (
        payload[7][1][0],
        payload[7][1][1],
    )

    for code, name in alliances_data:
        alliances.append(Alliance(code=code, name=name))

    for code, name in airlines_data:
        airlines.append(Airline(code=code, name=name))

    meta = JsMetadata(alliances=alliances, airlines=airlines)

    flights = ResultList()
    if payload[3] is None:
        raise UnparsedPageError(
            "Google returned a page without a results array (payload[3] is "
            "None); fast-flights 3.1.0 cannot parse this page shape (seen "
            "with nonstop-filtered queries). No results available."
        )
    if payload[3][0] is None:
        flights.metadata = meta
        return flights

    for k in payload[3][0]:
        flight = k[0]
        try:
            price = k[1][0][1]
        except (IndexError, TypeError):
            continue  # shape A: price-less ("price on request") segment

        typ = flight[0]
        airlines = flight[1]

        sg_flights = []

        for single_flight in flight[2]:
            from_airport = Airport(code=single_flight[3], name=single_flight[4])
            to_airport = Airport(code=single_flight[6], name=single_flight[5])
            departure_time = _parse_time(single_flight[8])
            departure_date = tuple(single_flight[20])
            departure = SimpleDatetime(date=departure_date, time=departure_time)

            arrival_time = _parse_time(single_flight[10])
            arrival_date = tuple(single_flight[21])
            arrival = SimpleDatetime(date=arrival_date, time=arrival_time)

            plane_type = single_flight[17]

            duration = single_flight[11]

            sg_flights.append(
                SingleFlight(
                    from_airport=from_airport,
                    to_airport=to_airport,
                    departure=departure,
                    arrival=arrival,
                    duration=duration,
                    plane_type=plane_type,
                )
            )

        # some additional data
        extras = flight[22]
        carbon_emission = extras[7]
        typical_carbon_emission = extras[8]

        flights.append(
            Flights(
                type=typ,
                price=price,
                airlines=airlines,
                flights=sg_flights,
                carbon=CarbonEmission(
                    typical_on_route=typical_carbon_emission, emission=carbon_emission
                ),
            )
        )

    flights.metadata = meta
    return flights


def _parse_tolerant(html: str) -> ResultList:
    """HTML-level parse with a clear error for pages without flight data."""
    parser = LexborHTMLParser(html)
    script = parser.css_first(r"script.ds\:1")
    if script is None:
        raise UnparsedPageError(
            "Google page contained no flight data script (script.ds:1 "
            "missing) - possibly a consent or redirect page. Retry the query."
        )
    return _parse_js_tolerant(script.text())


# Install the tolerant parser on every call path. get_flights() calls
# fast_flights.fetcher.parse (bound at import time), which in turn calls
# parse_js via the parser module's global — patch all three names so the
# patch holds no matter which path a future fast-flights version takes.
import fast_flights.fetcher as _ff_fetcher  # noqa: E402
import fast_flights.parser as _ff_parser  # noqa: E402

_ff_parser.parse_js = _parse_js_tolerant
_ff_parser.parse = _parse_tolerant
_ff_fetcher.parse = _parse_tolerant


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


def _query_params(q: dict) -> dict:
    """Common fast-flights params shared by both trip directions."""
    return dict(
        seat=q.get("seat", "economy"),
        passengers=Passengers(
            adults=int(q.get("adults", 1)),
            children=int(q.get("children", 0)),
            infants_in_seat=int(q.get("infants_in_seat", 0)),
            infants_on_lap=int(q.get("infants_on_lap", 0)),
        ),
        currency=q.get("currency", ""),
        language=q.get("language", ""),
        # max_stops=0 means "nonstop only". fast-flights 3.1.0 cannot parse
        # nonstop-filtered pages (payload[3] is None), so the filter is
        # applied client-side after the search (_apply_direct_only).
        max_stops=None if q.get("max_stops") == 0 else q.get("max_stops"),
        max_price=q.get("max_price"),
        carry_on_bags=int(q.get("carry_on_bags", 0)),
        checked_bags=int(q.get("checked_bags", 0)),
        hide_separate_and_self_transfer=bool(q.get("hide_separate_and_self_transfer", False)),
        exclude_basic_economy=bool(q.get("exclude_basic_economy", False)),
    )


def _search_direction(date: str, src: str, dst: str, q: dict) -> list:
    """Run a single one-way search src->dst on date and return the flight list.

    One-way fares. Used for trip="one-way", and (for round-trips) as a
    reference search for the RETURN direction's leg schedules — the native RT
    query does not include return legs.
    """
    query = create_query(
        flights=[FlightQuery(date=date, from_airport=src, to_airport=dst)],
        trip="one-way",
        **_query_params(q),
    )
    try:
        result = get_flights(query)
    except FlightsNotFound:
        return []
    currency = q.get("currency") or "google-default"
    flights = [
        {
            "price": f.price,
            "currency": currency,
            "price_type": "one-way",
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
    if q.get("max_stops") == 0:
        flights = _apply_direct_only(flights)
    return flights


def _search_round_trip(out_date: str, ret_date: str, src: str, dst: str, q: dict) -> list:
    """Native fast-flights round-trip query (both legs in ONE query).

    Returns flights whose `price` is the TRUE two-way round-trip total fare.
    `legs` contain only the OUTBOUND direction (fast-flights limitation,
    verified) — return-leg schedules must come from a separate one-way search.
    """
    query = create_query(
        flights=[
            FlightQuery(date=out_date, from_airport=src, to_airport=dst),
            FlightQuery(date=ret_date, from_airport=dst, to_airport=src),
        ],
        trip="round-trip",
        **_query_params(q),
    )
    try:
        result = get_flights(query)
    except FlightsNotFound:
        return []
    currency = q.get("currency") or "google-default"
    flights = [
        {
            "price": f.price,  # true two-way round-trip total
            "price_type": "round-trip-total",
            "currency": currency,
            "airlines": f.airlines,
            "type": f.type,
            "carbon": {
                "typical_on_route": f.carbon.typical_on_route,
                "emission": f.carbon.emission,
            },
            "stops": len(f.flights) - 1,
            "legs": [_leg(x) for x in f.flights],  # outbound legs only
        }
        for f in result
    ]
    if q.get("max_stops") == 0:
        flights = _apply_direct_only(flights)
    return flights


def _route(src: str, dst: str) -> dict:
    """Resolved endpoint metadata + a ready-to-read summary."""
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
    return route


def _apply_direct_only(flights: list) -> list:
    """Keep nonstop itineraries (client-side max_stops=0 filter).

    Google's top results include the direct options on short-haul routes, so
    filtering the normal search is a good approximation of the nonstop
    filter. (A note attached to the result tells the caller this is so.)
    """
    return [f for f in flights if f["stops"] == 0]


def _resolve_currency(q: dict) -> tuple[str, str]:
    """Return (value_for_google, label_for_output).

    Explicit `currency` in the query wins (upper-cased). Otherwise the
    FLIGHT_CURRENCY env var (default "USD"). An empty value means "let
    Google decide"; the output is then labelled "google-default" so prices
    are never shown without a label.
    """
    explicit = (q.get("currency") or "").strip().upper()
    if explicit:
        return explicit, explicit
    env = os.environ.get("FLIGHT_CURRENCY")
    if env is None:
        env = "USD"
    env = env.strip().upper()
    if env:
        return env, env
    return "", "google-default"


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

    effective_currency, currency = _resolve_currency(q)
    q = {**q, "currency": effective_currency}

    if trip == "round-trip":
        return_date = q.get("return_date")
        if not return_date:
            raise ValueError("return_date is required for round-trip")
        # Native RT query for the TRUE two-way price (+outbound legs), plus a
        # one-way reference search so return-leg schedules are still visible.
        payload = {
            "currency": currency,
            "trip": "round-trip",
            "outbound_route": _route(src, dst),
            "return_route": _route(dst, src),
            "round_trip": _search_round_trip(outbound_date, return_date, src, dst, q),
            "return_options": _search_direction(return_date, dst, src, q),
        }
        note = (
            "round_trip[].price is the true two-way round-trip total from a "
            "native fast-flights RT query; its legs cover the outbound "
            "direction only. return_options[] are one-way fares for the "
            "return direction (schedule reference, NOT round-trip prices). "
            "Do not add one-way fares to estimate round-trip prices."
        )
        if q.get("max_stops") == 0:
            note += (
                " max_stops=0: nonstop filter applied client-side to the top "
                "itineraries Google returned."
            )
        payload["note"] = note
        return payload

    # one-way: keep the flat, backward-compatible shape.
    out = {
        "currency": currency,
        "trip": "one-way",
        "route": _route(src, dst),
        "flights": _search_direction(outbound_date, src, dst, q),
    }
    if q.get("max_stops") == 0:
        out["note"] = (
            "max_stops=0: nonstop filter applied client-side to the top "
            "itineraries Google returned."
        )
    return out


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
