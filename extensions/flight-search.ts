/**
 * Flight Search — a pi extension that registers a `search_flights` tool.
 *
 * The tool delegates to `flight_runner.py` (a small Python wrapper around the
 * `fast-flights` Google Flights scraper) for deterministic, structured results:
 * correct per-direction stop counts, no scratch scripts, no ad-hoc formatting.
 *
 * Output contract (see `description`): COMPACT JSON, at most 10 itineraries
 * per section (cheapest first) with total_* full counts. The full untruncated
 * result is kept in `details` for the transcript.
 *
 * Error contract: the runner reports failures as {"error": "..."} on stdout
 * with a non-zero exit code. This extension surfaces that message as
 * `flight search failed: <error>` so the agent sees the real cause in one
 * call instead of re-running the runner by hand.
 *
 * Install:
 *   - python3 with `fast-flights` installed (the runner reports a clear error if not)
 *   - symlink this file into your pi extensions dir so it auto-loads on /reload:
 *       ln -s "$(pwd)/extensions/flight-search.ts" ~/.pi/agent/extensions/flight-search.ts
 *
 * The runner path is resolved in this order:
 *   1. `FLIGHT_RUNNER` env var
 *   2. `flight_runner.py` next to this file
 *   3. default OSS folder: <repo>/flight_runner.py
 */

import { execFile } from "node:child_process";
import { accessSync, realpathSync } from "node:fs";
import { dirname, join } from "node:path";

import { StringEnum } from "@earendil-works/pi-ai";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

export function findRunner(): string {
	const env = process.env.FLIGHT_RUNNER;
	if (env) return env;

	// __dirname is the dir the extension was LOADED from. If the extension is
	// symlinked into ~/.pi/agent/extensions/, __dirname is that symlink dir, so
	// resolve the real path of this module before looking for its neighbours.
	let base = __dirname;
	try {
		base = dirname(realpathSync(__filename));
	} catch {
		/* fall back to __dirname */
	}

	// 1) flight_runner.py next to this file
	const local = join(base, "flight_runner.py");
	if (fileExists(local)) return local;
	// 2) OSS project layout: <project>/flight_runner.py
	const project = join(dirname(base), "flight_runner.py");
	if (fileExists(project)) return project;
	// 3) last resort: current working dir
	return join(process.cwd(), "flight_runner.py");
}

function fileExists(p: string): boolean {
	try {
		accessSync(p);
		return true;
	} catch {
		return false;
	}
}

function runPython(runner: string, python: string, payload: unknown, signal?: AbortSignal): Promise<string> {
	return new Promise((resolvePromise, reject) => {
		const child = execFile(
			python,
			[runner],
			{
				timeout: 120_000,
				maxBuffer: 10 * 1024 * 1024,
				signal,
			},
			(error, stdout, stderr) => {
				if (error) {
					// Runner contract: failures print {"error": "..."} to stdout
					// and exit 1. Surface that real reason instead of the
					// generic "Command failed" shell message.
					let detail = "";
					try {
						const parsed = JSON.parse(stdout) as { error?: unknown };
						if (parsed && typeof parsed.error === "string") detail = parsed.error;
					} catch {
						detail = `${stdout ?? ""} ${stderr ?? ""}`.trim().slice(0, 500);
					}
					reject(
						new Error(
							detail
								? `flight search failed: ${detail}`
								: `flight search failed (${error.message})`,
						),
					);
					return;
				}
				resolvePromise(stdout);
			},
		);
		child.stdin?.on("error", () => {}); // EPIPE if python exits early
		child.stdin?.end(JSON.stringify(payload));
	});
}

/** How many itineraries per section are returned in the tool output. */
const LIMIT = 10;

const search_flights = {
	name: "search_flights",
	label: "Search Flights",
	description:
		"Search real Google Flights availability between two airports on given dates via fast-flights. Pass 3-letter IATA airport codes (from_airport/to_airport); the tool resolves each code to airport name, city and ISO-3166 country before searching. Returns COMPACT JSON (single line). one-way: {trip, currency, route, flights[], total_flights}. round-trip: {trip, currency, outbound_route, return_route, round_trip[], return_options[], total_round_trip, total_return_options, note}. Each array holds at most 10 itineraries, cheapest first; total_* give the full result count. Each itinerary: {price (in `currency`), price_type ('round-trip-total' or 'one-way'), airlines, stops, legs[]}; each leg: {from, to, from_city, to_city, from_country, to_country, date, departure 'HH:MM', arrival 'HH:MM', duration_minutes, plane}. Round-trip semantics: round_trip[].price is the TRUE two-way total and its legs cover the OUTBOUND direction only; return_options[] are ONE-WAY reference fares for the return direction (schedule reference) - never add them to the round-trip price. Failures raise an error 'flight search failed: <reason>' where <reason> is the runner's real error (e.g. an unparseable Google page) - report it to the user or retry once; do not re-implement the search ad hoc. Always pass an explicit currency; when omitted the runner defaults to USD (env FLIGHT_CURRENCY, '' = Google default). max_stops=0 = nonstop only, applied client-side to Google's top results.",
	promptSnippet: "Search live Google Flights for price/availability between airports",
	promptGuidelines: [
		"Use search_flights for real flight prices and availability instead of guessing or writing ad-hoc scraping scripts.",
		"Pass 3-letter IATA airport codes (from_airport/to_airport); the tool resolves each code to its airport name, city and ISO-3166 country so you can confirm a code really means the city the user wants (e.g. ECN = Ercan/Cyprus). Resolve dates to YYYY-MM-DD; ask the user if the code, trip type, or dates are unclear.",
		"Always pass an explicit currency to search_flights (e.g. 'EUR', 'TRY', 'USD') so prices are labelled; the runner defaults to USD when omitted.",
		"search_flights output is compact JSON: at most 10 itineraries per section (cheapest first), total_* = full counts. For round-trips, only round_trip[].price is the real two-way price; return_options[] are one-way reference fares — never sum them into the round-trip price.",
	],
	parameters: Type.Object({
		from_airport: Type.String({ description: "3-letter IATA departure airport code (e.g. 'IST')" }),
		to_airport: Type.String({ description: "3-letter IATA arrival airport code (e.g. 'ECN')" }),
		outbound_date: Type.String({ description: "Outbound departure date, YYYY-MM-DD" }),
		trip: StringEnum(["one-way", "round-trip"] as const),
		return_date: Type.Optional(
			Type.String({ description: "Return date, YYYY-MM-DD — required when trip is round-trip" }),
		),
		seat: StringEnum(["economy", "premium-economy", "business", "first"] as const),
		currency: Type.Optional(
			Type.String({
				description:
					"ISO currency code, e.g. EUR, USD, TRY. Always pass one so prices are labelled; when omitted the runner defaults to USD (override: env FLIGHT_CURRENCY, '' = Google default).",
			}),
		),
		language: Type.Optional(
			Type.String({ description: "IETF language tag, e.g. en-US. Omit for Google default." }),
		),
		adults: Type.Optional(Type.Integer({ minimum: 0, maximum: 9 })),
		children: Type.Optional(Type.Integer({ minimum: 0, maximum: 9 })),
		max_stops: Type.Optional(
			Type.Integer({
				description:
					"Max number of stops. 0 = nonstop only (applied client-side to Google's top results); omit for no limit.",
			}),
		),
		max_price: Type.Optional(Type.Integer({ minimum: 0 })),
		exclude_basic_economy: Type.Optional(Type.Boolean()),
	}),

	async execute(_toolCallId, params, signal, onUpdate, _ctx) {
		onUpdate?.({ content: [{ type: "text", text: "Searching flights…" }] });

		if (params.trip === "round-trip" && !params.return_date) {
			throw new Error("return_date is required when trip is 'round-trip'");
		}

		const runner = findRunner();
		const python = process.env.FLIGHT_PYTHON ?? "python3";
		const stdout = await runPython(runner, python, params, signal);

		let data: unknown;
		try {
			data = JSON.parse(stdout);
		} catch {
			throw new Error(`flight search returned invalid JSON: ${stdout.slice(0, 300)}`);
		}
		const obj = data as Record<string, unknown>;
		if (obj.error) throw new Error(`flight search failed: ${obj.error}`);

		const byPrice = (list: Array<Record<string, unknown>>) =>
			[...list].sort((a, b) => Number(a.price) - Number(b.price));

		// ---- round-trip: native RT query (true two-way price) + return reference ----
		if (obj.trip === "round-trip") {
			const rtAll = byPrice((obj.round_trip ?? []) as Array<Record<string, unknown>>);
			const retAll = byPrice((obj.return_options ?? []) as Array<Record<string, unknown>>);
			const out = {
				trip: "round-trip",
				currency: (obj.currency as string | undefined) ?? "",
				outbound_route: obj.outbound_route,
				return_route: obj.return_route,
				round_trip: rtAll.slice(0, LIMIT),
				return_options: retAll.slice(0, LIMIT),
				total_round_trip: rtAll.length,
				total_return_options: retAll.length,
				note: obj.note,
			};
			return {
				content: [{ type: "text", text: JSON.stringify(out) }],
				details: { ...out, round_trip: rtAll, return_options: retAll },
			};
		}

		// ---- one-way (flat shape) ----
		const all = byPrice((obj.flights ?? []) as Array<Record<string, unknown>>);
		const out: Record<string, unknown> = {
			trip: "one-way",
			currency: (obj.currency as string | undefined) ?? "",
			route: obj.route,
			flights: all.slice(0, LIMIT),
			total_flights: all.length,
		};
		if (obj.note) out.note = obj.note;
		return {
			content: [{ type: "text", text: JSON.stringify(out) }],
			details: { ...out, flights: all },
		};
	},
};

export default function (pi: ExtensionAPI) {
	pi.registerTool(search_flights);
}
