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
		"Search real Google Flights availability between two airports on given dates via fast-flights. Pass 3-letter IATA airport codes (from_airport/to_airport); the tool resolves each code to its airport name, city and ISO-3166 country so you know exactly which city the code refers to before reading results. Returns structured JSON: the resolved route (airport/city/country), price (currency), airline(s), number of stops, and each flight leg with airports, city/country, times, duration and plane type. For round-trips it runs a native round-trip query and returns the TRUE two-way total price with the outbound legs, plus one-way reference fares showing return-leg schedules (return_options). Use for finding flights, checking prices/availability, comparing routes, dates, seats, or airlines.",
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

		const currency = (obj.currency as string | undefined) ?? "";

		// Human-friendly duration: show minutes too when under 2h (e.g. 1h 35m).
		const fmtDuration = (min: number) => {
			const m = Math.round(min || 0);
			const h = Math.floor(m / 60);
			const r = m % 60;
			return h > 0 ? `${h}h${r ? ` ${r}m` : ""}` : `${r}m`;
		};

		// Render one direction of flights (outbound or return).
		const renderDirection = (
			label: string,
			flights: Array<Record<string, unknown>>,
			routeSummary?: string,
		) => {
			const sorted = [...flights].sort((a, b) => Number(a.price) - Number(b.price));
			const top = sorted.slice(0, 10);
			const header = `## ${label}${routeSummary ? ` — ${routeSummary}` : ""}`;
			if (top.length === 0) return `${header}\nNo ${label.toLowerCase()} flights found.`;
			const lines = top.map((f, i) => {
				const legs = (f.legs as Array<Record<string, string>>) ?? [];
				const where = (l: Record<string, string>, prefix: "from" | "to") => {
					const code = l[prefix];
					const city = l[`${prefix}_city`] ?? "";
					const country = l[`${prefix}_country`] ?? "";
					const ctx = city || country ? ` (${city}, ${country})` : "";
					return `${code}${ctx}`;
				};
				const seg = legs
					.map(
						(l) =>
							`${where(l, "from")} ${l.departure} → ${where(l, "to")} ${l.arrival} ${l.date} ` +
						`(${fmtDuration(Number(l.duration_minutes) || 0)}, ${l.plane})`,
					)
					.join(", ");
				const airline = (f.airlines as string[] | undefined)?.join("/") ?? "";
				const stops = Number(f.stops) || 0;
				return `${i + 1}. ${f.currency ?? currency} ${f.price} · ${airline} · ${stops === 0 ? "direct" : `${stops} stop${stops > 1 ? "s" : ""}`}\n   ${seg}`;
			});
			return `${header}\n${lines.join("\n")}\n\n${sorted.length} itinerary/ies (showing cheapest ${top.length})`;
		};

		// ---- round-trip: native RT query (true two-way price) + return reference ----
		if (obj.trip === "round-trip") {
			const rt = (obj.round_trip ?? []) as Array<Record<string, unknown>>;
			const retOpts = (obj.return_options ?? []) as Array<Record<string, unknown>>;
			const or = obj.outbound_route as Record<string, unknown> | undefined;
			const rr = obj.return_route as Record<string, unknown> | undefined;
			const outboundSummary = (or?.summary as string | undefined) ?? "";
			const returnSummary = (rr?.summary as string | undefined) ?? "";
			const parts: string[] = [];
			parts.push(renderDirection("Round-trip (true two-way total, outbound legs shown)", rt, outboundSummary));
			parts.push(renderDirection("Return leg options (one-way reference fares — NOT round-trip prices)", retOpts, returnSummary));
			const text = parts.join("\n\n");
			return {
				content: [{ type: "text", text }],
				details: {
					currency,
					trip: "round-trip",
					outbound_route: or,
					return_route: rr,
					round_trip: rt,
					return_options: retOpts,
					note: obj.note,
				},
			};
		}

		// ---- one-way (and legacy flat shape) ----
		const route = obj.route as Record<string, unknown> | undefined;
		const routeSummary = (route?.summary as string | undefined) ?? "";
		const flights = (obj.flights ?? []) as Array<Record<string, unknown>>;
		const rendered = renderDirection("Flights", flights, routeSummary);
		const text = `${rendered}\n\nRoute: ${routeSummary}`;
		return {
			content: [{ type: "text", text }],
			details: { currency, trip: "one-way", route, flights },
		};
	},
};

export default function (pi: ExtensionAPI) {
	pi.registerTool(search_flights);
}
