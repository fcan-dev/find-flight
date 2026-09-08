/**
 * Flight Search — a pi extension that registers a `search_flights` tool.
 *
 * The tool delegates to `flight_runner.py` (a small Python wrapper around the
 * `fast-flights` Google Flights scraper) for deterministic, structured results:
 * correct per-direction stop counts, no scratch scripts, no ad-hoc formatting.
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
					reject(
						new Error(
							`flight search failed (${error.message})${stderr ? `: ${stderr.trim()}` : ""}`,
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

const search_flights = {
	name: "search_flights",
	label: "Search Flights",
	description:
		"Search real Google Flights availability between two airports on given dates via fast-flights. Pass 3-letter IATA airport codes (from_airport/to_airport); the tool resolves each code to its airport name, city and ISO-3166 country so you know exactly which city the code refers to before reading results. Returns structured JSON: the resolved route (airport/city/country), price (currency), airline(s), number of stops, and each flight leg with airports, city/country, times, duration and plane type. For round-trips, price is the two-way total and only the outbound legs are returned. Use for finding flights, checking prices/availability, comparing routes, dates, seats, or airlines.",
	promptSnippet: "Search live Google Flights for price/availability between airports",
	promptGuidelines: [
		"Use search_flights for real flight prices and availability instead of guessing or writing ad-hoc scraping scripts.",
		"Pass 3-letter IATA airport codes (from_airport/to_airport); the tool resolves each code to its airport name, city and ISO-3166 country so you can confirm a code really means the city the user wants (e.g. ECN = Ercan/Cyprus). Resolve dates to YYYY-MM-DD; ask the user if the code, trip type, or dates are unclear.",
		"search_flights prices carry the currency you pass; pass a currency (e.g. 'EUR', 'TRY', 'USD') unless the user prefers Google's default.",
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
			Type.String({ description: "ISO currency code, e.g. EUR, USD, TRY. Omit for Google default." }),
		),
		language: Type.Optional(
			Type.String({ description: "IETF language tag, e.g. en-US. Omit for Google default." }),
		),
		adults: Type.Optional(Type.Integer({ minimum: 0, maximum: 9 })),
		children: Type.Optional(Type.Integer({ minimum: 0, maximum: 9 })),
		max_stops: Type.Optional(Type.Integer({ minimum: 0 })),
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
		if (obj.error) throw new Error(`flight search error: ${obj.error}`);

		const route = obj.route as Record<string, unknown> | undefined;
		const routeSummary = (route?.summary as string | undefined) ?? "";
		const routeLine = routeSummary ? `\nRoute: ${routeSummary}` : "";

		const flights = (obj.flights ?? []) as Array<Record<string, unknown>>;
		if (flights.length === 0) {
			return {
				content: [{ type: "text", text: `No flights found for this search.${routeLine}` }],
				details: { currency: obj.currency, route, count: 0 },
			};
		}
		flights.sort((a, b) => Number(a.price) - Number(b.price));
		const top = flights.slice(0, 10);

		const lines = top.map((f, i) => {
			const legs = (f.legs as Array<Record<string, string>>) ?? [];
			const seg = legs.map(
				(l) =>
					`${l.from} (${l.from_city ?? ""}, ${l.from_country ?? ""}) ${l.departure} → ` +
					`${l.to} (${l.to_city ?? ""}, ${l.to_country ?? ""}) ${l.arrival} ${l.date} ` +
					`(${Math.round((Number(l.duration_minutes) || 0) / 60)}h, ${l.plane})`,
			).join(", ");
			const rt = f.is_round_trip ? " (round-trip total)" : "";
			const airline = (f.airlines as string[] | undefined)?.join("/") ?? "";
			const stops = Number(f.stops) || 0;
			return `${i + 1}. ${f.currency} ${f.price}${rt} · ${airline} · ${stops === 0 ? "direct" : `${stops} stop`}\n   ${seg}`;
		});
		const summary = `${lines.join("\n")}\n\n${flights.length} itinerary/ies (showing cheapest ${top.length})${routeLine}`;

		return {
			content: [{ type: "text", text: summary }],
			details: { currency: obj.currency, route, flights },
		};
	},
};

export default function (pi: ExtensionAPI) {
	pi.registerTool(search_flights);
}
