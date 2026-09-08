#!/usr/bin/env python3
"""Lightweight airport & country lookup for the find-flight runner.

Given a 3-letter IATA airport code, resolve the authoritative name, city,
state/region and ISO-3166-1 alpha-2 country code (and its English country
name). This is the disambiguation source that prevents mis-mapping a code to
the wrong city — e.g. ECN is Ercan (North Cyprus), not Edinburgh; MYJ is
Matsuyama (Japan), not Mysore.

Data comes from the bundled JSON files generated from the upstream airport
list (`enums/airports.csv` in AWeirdDev/flights, ~9.7k rows):
  * airports.json   { code: [name, city, state, country_id] }
  * countries.json  { ISO2: "English country name" }

The files resolve relative to THIS module, so the lookup works regardless of
the process CWD (important because the extension runs the runner from an
arbitrary directory). If a file is missing or a code is unknown, the lookup
returns a minimal record built from the code itself rather than raising, so
the search never hard-fails on metadata.
"""
from __future__ import annotations

import json
import os

_THIS = os.path.dirname(os.path.abspath(__file__))

_AIRPORTS = None      # cached dict: code -> [name, city, state, country]
_COUNTRIES = None     # cached dict: ISO2 -> "English name"


def _load(path: str):
    try:
        with open(os.path.join(_THIS, path), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _airports() -> dict:
    global _AIRPORTS
    if _AIRPORTS is None:
        _AIRPORTS = _load("airports.json") or {}
    return _AIRPORTS


def _countries() -> dict:
    global _COUNTRIES
    if _COUNTRIES is None:
        _COUNTRIES = _load("countries.json") or {}
    return _COUNTRIES


def country_name(iso2: str) -> str:
    """ISO-3166-1 alpha-2 code -> English country name ('' if unknown)."""
    return _countries().get((iso2 or "").upper(), "")


def resolve(code: str) -> dict:
    """Resolve a 3-letter IATA code to name/city/state/country metadata.

    Never raises: unknown/empty codes return a record keyed on the code with
    empty fields, so callers can still proceed (or choose to reject on 3-char
    validation elsewhere).
    """
    code = (code or "").strip().upper()
    rec = _airports().get(code, [])
    name, city, state, iso2 = (rec + ["", "", "", ""])[:4]
    return {
        "code": code,
        "name": name,
        "city": city,
        "state": state,
        "country": iso2.upper(),
        "country_name": country_name(iso2),
    }


def is_known(code: str) -> bool:
    return (code or "").strip().upper() in _airports()
