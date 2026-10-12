#!/usr/bin/env python3
"""Unit tests for flight_runner hardening (no network, no live Google).

Covers the contract that killed the 2026-10-12 debugging spiral:

1. Tolerant parse of fast-flights 3.1.0 payload shapes:
   A) price-less "price on request" segments (k[1] == [[], <token>])
      -> skipped, never a crash (seen: Eastar Jet on ICN->KIX 2027-01-17)
   B) payload[3] is None (pages without a results array, seen with
      nonstop-filtered queries)
      -> UnparsedPageError, NEVER an empty result list (no lying)
   C) payload[3] == [None] -> genuinely empty result list
2. max_stops=0 is a client-side nonstop filter (_apply_direct_only).
3. Currency is always labelled: explicit wins, else FLIGHT_CURRENCY env
   (default USD), else "" + label "google-default".
4. The tolerant parser is actually installed on every call path
   (fast_flights.parser.parse_js, fast_flights.parser.parse,
   fast_flights.fetcher.parse).

Run standalone:   python3 tests/test_flight_runner.py
Or with pytest:   python3 -m pytest tests/ -q
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fast_flights.fetcher as ff_fetcher  # noqa: E402
import fast_flights.parser as ff_parser  # noqa: E402
import flight_runner  # noqa: E402  (importing installs the parser patch)
from fast_flights.exceptions import FlightsNotFound  # noqa: E402
from flight_runner import (  # noqa: E402
    UnparsedPageError,
    _apply_direct_only,
    _parse_js_tolerant,
    _parse_tolerant,
    _resolve_currency,
)


# ---------------------------------------------------------------------------
# Synthetic payload builders — index layout pinned to fast-flights 3.1.0
# (fast_flights/parser.py).
# ---------------------------------------------------------------------------

def _single_flight(dep=(9, 5), arr=(10, 55)) -> list:
    sf = [None] * 22
    sf[3] = "ICN"                      # from code
    sf[4] = "Incheon International Airport"   # from name
    sf[5] = "Kansai International Airport"   # to name
    sf[6] = "KIX"                      # to code
    sf[8] = dep                        # departure time
    sf[10] = arr                       # arrival time
    sf[11] = 110                       # duration minutes
    sf[17] = "Boeing 737"              # plane type
    sf[20] = (2027, 1, 17)             # departure date
    sf[21] = (2027, 1, 17)             # arrival date
    return sf


def _segment(price, code="ZE", name="EASTAR JET") -> list:
    extras = [None] * 9
    extras[7] = 4800                   # carbon emission
    extras[8] = 5000                   # typical carbon emission
    flight = [None] * 23
    flight[0] = code
    flight[1] = [name]
    flight[2] = [_single_flight()]
    flight[22] = extras
    price_cell = [None, price] if price is not None else []
    return [flight, [price_cell, "booking-token"]]


def _payload(p3) -> list:
    p = [None] * 8
    p[3] = p3
    p[7] = [None, [
        [["SKYTEAM", "SkyTeam"]],       # alliances
        [["ZE", "EASTAR JET"]],         # airlines
    ]]
    return p


def _js(p3) -> str:
    """Wrap a payload the way script.ds:1 does: prefix + data:<json> + ,suffix."""
    return "headdata:" + json.dumps(_payload(p3)) + ",tail"


# ---------------------------------------------------------------------------
# 1. Tolerant parse
# ---------------------------------------------------------------------------

def test_skips_priceless_segments():
    segs = [
        _segment(111, "MM", "Peach Aviation"),
        _segment(None),                 # shape A: price-less (Eastar Jet)
        _segment(132, "7C", "Jeju Air"),
    ]
    out = _parse_js_tolerant(_js([segs]))
    assert [f.price for f in out] == [111, 132]
    # flight[1] is the airline DISPLAY NAME in the real payload (not IATA codes)
    assert out[0].airlines == ["Peach Aviation"]
    leg = out[0].flights[0]
    assert leg.from_airport.code == "ICN"
    assert leg.to_airport.code == "KIX"
    assert leg.departure.time == (9, 5)
    assert leg.arrival.time == (10, 55)
    assert leg.duration == 110
    assert leg.plane_type == "Boeing 737"
    # metadata survived the skip
    assert [a.code for a in out.metadata.airlines] == ["ZE"]
    assert [a.code for a in out.metadata.alliances] == ["SKYTEAM"]


def test_all_priceless_segments_gives_empty_list():
    out = _parse_js_tolerant(_js([[_segment(None)]]))
    assert len(out) == 0


def test_payload3_none_raises_not_empty():
    try:
        _parse_js_tolerant(_js(None))
    except UnparsedPageError as e:
        assert "payload[3]" in str(e)
    else:
        raise AssertionError("expected UnparsedPageError for payload[3] is None")


def test_payload3_null_list_is_empty_result():
    out = _parse_js_tolerant(_js([None]))
    assert len(out) == 0
    assert out.metadata is not None


def test_error_has_status_raises_flights_not_found():
    js = "headdata:abcerrorHasStatus: true,tail"
    try:
        _parse_js_tolerant(js)
    except FlightsNotFound:
        pass
    else:
        raise AssertionError("expected FlightsNotFound for errorHasStatus")


def test_missing_data_payload_raises_unparsed():
    try:
        _parse_js_tolerant("no data here at all")
    except UnparsedPageError:
        pass
    else:
        raise AssertionError("expected UnparsedPageError for missing 'data:'")


def test_tolerant_html_parse_raises_on_page_without_script():
    # _parse_tolerant handles the HTML level: no script.ds:1 -> clear error
    # instead of AttributeError on None.
    try:
        _parse_tolerant("<html><head><title>consent</title></head><body>x</body></html>")
    except UnparsedPageError as e:
        assert "script.ds:1" in str(e)
    else:
        raise AssertionError("expected UnparsedPageError for page without script.ds:1")


# ---------------------------------------------------------------------------
# 2. Direct-only (max_stops=0) client-side filter
# ---------------------------------------------------------------------------

def test_apply_direct_only_keeps_nonstops():
    flights = [
        {"price": 1, "stops": 1},
        {"price": 2, "stops": 0},
        {"price": 3, "stops": 0},
        {"price": 4, "stops": 2},
    ]
    kept = _apply_direct_only(flights)
    assert [f["price"] for f in kept] == [2, 3]


def test_apply_direct_only_empty():
    assert _apply_direct_only([{"stops": 1}, {"stops": 3}]) == []


# ---------------------------------------------------------------------------
# 3. Currency labelling
# ---------------------------------------------------------------------------

def test_resolve_currency():
    old = os.environ.pop("FLIGHT_CURRENCY", None)
    try:
        # explicit query currency wins (case-insensitive)
        assert _resolve_currency({"currency": "jpy"}) == ("JPY", "JPY")
        assert _resolve_currency({"currency": "  eur  "}) == ("EUR", "EUR")
        # no explicit -> env default USD
        assert _resolve_currency({}) == ("USD", "USD")
        assert _resolve_currency({"currency": ""}) == ("USD", "USD")
        # env override
        os.environ["FLIGHT_CURRENCY"] = "TRY"
        assert _resolve_currency({}) == ("TRY", "TRY")
        # explicit still beats env
        assert _resolve_currency({"currency": "usd"}) == ("USD", "USD")
        # env "" -> let Google decide, with an honest label
        os.environ["FLIGHT_CURRENCY"] = ""
        assert _resolve_currency({}) == ("", "google-default")
    finally:
        os.environ.pop("FLIGHT_CURRENCY", None)
        if old is not None:
            os.environ["FLIGHT_CURRENCY"] = old


# ---------------------------------------------------------------------------
# 4. Patch installation (every call path covered)
# ---------------------------------------------------------------------------

def test_parser_patch_installed_on_all_paths():
    assert ff_parser.parse_js is flight_runner._parse_js_tolerant
    assert ff_parser.parse is flight_runner._parse_tolerant
    assert ff_fetcher.parse is flight_runner._parse_tolerant


# ---------------------------------------------------------------------------
# 4b. examples CLI uses the hardened parser
# ---------------------------------------------------------------------------

def test_examples_cli_installs_tolerant_parser():
    # The documented CLI (examples/search_flights.py) must run with the same
    # tolerant parser as flight_runner — raw fast_flights crashes on
    # price-less "price on request" segments (seen: Eastar Jet ICN->KIX).
    # Verified in a subprocess because the parser patch is a process-global
    # import side effect this suite would already have installed.
    import subprocess
    repo = Path(__file__).resolve().parents[1]
    probe = (
        "import importlib.util, fast_flights.parser as p\n"
        "spec = importlib.util.spec_from_file_location('sf_cli', %r)\n"
        "m = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(m)\n"
        "if p.parse_js.__name__ != '_parse_js_tolerant':\n"
        "    raise SystemExit('examples CLI left the raw fast_flights parser in place')\n"
    ) % (str(repo / "examples" / "search_flights.py"),)
    r = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, cwd=str(repo)
    )
    assert r.returncode == 0, (
        f"examples CLI did not install the tolerant parser:\n{r.stdout}{r.stderr}"
    )


def test_examples_cli_standalone_copy_does_not_crash():
    # The CLI is documented as a ready-made standalone script. A copy placed
    # outside the repo (no flight_runner.py reachable) must import cleanly
    # with a warning instead of dying with ImportError.
    import subprocess
    import tempfile
    src = Path(__file__).resolve().parents[1] / "examples" / "search_flights.py"
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "search_flights.py"
        dest.write_text(src.read_text())
        probe = (
            "import importlib.util\n"
            "spec = importlib.util.spec_from_file_location('sf_cli', %r)\n"
            "m = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(m)\n"
        ) % (str(dest),)
        # cwd MUST be the temp dir: with `python -c`, '' (the cwd) is on
        # sys.path, and a repo cwd would let the import find flight_runner
        # and make this test meaningless.
        r = subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, text=True, cwd=tmp
        )
    assert r.returncode == 0, (
        f"standalone copy of the CLI crashed on import:\n{r.stdout}{r.stderr}"
    )


# ---------------------------------------------------------------------------
# 5. Country metadata integrity (countries.json)
# ---------------------------------------------------------------------------

def test_country_names_are_complete():
    # Regression: 2026-10-12 session showed country_name "South" for Korea —
    # multi-word names in countries.json were shredded into separate key/value
    # pairs ("KR"->"South", "Korea"->"KW"), and pairs after the first shredded
    # name drifted out of alignment ("VN"->"Chile").
    import airports
    countries = airports._countries()
    # every key must be a real 2-letter ISO alpha-2 code
    for key in countries:
        assert len(key) == 2 and key.isalpha() and key.isupper(), (
            f"countries.json has non-ISO key {key!r} (shredded name?)"
        )
    # spot-check the multi-word names the old file got wrong
    expected = {
        "KR": "South Korea",
        "KP": "North Korea",
        "US": "United States",
        "GB": "United Kingdom",
        "AE": "United Arab Emirates",
        "CI": "Côte d'Ivoire",
        "TW": "Taiwan",
        "VN": "Vietnam",
        "TZ": "Tanzania",
    }
    for iso2, name in expected.items():
        got = airports.country_name(iso2)
        assert got == name, f"{iso2}: {got!r} != {name!r}"
    # end-to-end: the exact field the live session printed
    assert airports.resolve("ICN")["country_name"] == "South Korea"
    # every country referenced by airports.json must resolve to a name
    missing = sorted({
        rec[3] for rec in airports._airports().values()
        if rec and len(rec) > 3 and rec[3] and not airports.country_name(rec[3])
    })
    assert not missing, f"airports with unnamed countries: {missing}"


def test_countries_json_matches_pinned_table():
    # Full-table pin: countries.json must match the reviewed snapshot exactly
    # (keys AND values), so any future regeneration is diff-checked instead of
    # trust-checked. The snapshot was generated from CLDR (Node Intl) on
    # 2026-10-12 after an exhaustive cross-check; regenerate it only after
    # re-verifying the new data.
    import airports
    expected = json.loads(
        (Path(__file__).resolve().parent / "countries_expected.json")
        .read_text(encoding="utf-8")
    )
    actual = airports._countries()
    assert actual == expected, (
        "countries.json drifted from the pinned table: "
        f"missing={sorted(set(expected) - set(actual))[:10]} "
        f"extra={sorted(set(actual) - set(expected))[:10]} "
        f"changed={[(k, actual[k], expected[k]) for k in sorted(set(actual) & set(expected)) if actual[k] != expected[k]][:10]}"
    )


# ---------------------------------------------------------------------------
# Standalone runner (pytest-compatible tests are plain functions)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [
        (name, fn)
        for name, fn in sorted(globals().items())
        if name.startswith("test_") and callable(fn)
    ]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {name}: {e!r}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
