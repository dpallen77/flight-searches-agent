# Flight Search Agent

A small flight-price assistant with a browser-based chat interface. Ask for one-way or round-trip fares, compare options by price, stops, and travel time, and keep paid fare searches under an explicit call budget.

## Overview

The app runs locally and serves a lightweight chat page backed by a Python agent. When configured with an Anthropic API key and the `anthropic` package, the assistant can interpret plain-language requests and choose search filters. Without them, a built-in parser accepts airport codes and ISO-formatted dates. Searches use live Ignav fares when an Ignav API key is configured; otherwise, deterministic mock fares are returned for demonstration.

## Features

- One-way and round-trip fare searches.
- Flexible departure dates, with nearby days searched together.
- Filters for stops, cabin class, and maximum price when using the Claude agent.
- Results sorted by price, with carrier, stops, duration, and fare details.
- Six-hour in-memory result cache and a persistent monthly usage counter.
- Explicit approval before a search would use more than five new calls, plus per-request and monthly limits.
- Responsive chat interface with live usage and provider-mode indicators.

## Files

- `server.py` — local HTTP server, `.env` loading, and JSON API endpoints.
- `agent.py` — Claude tool-use loop, fallback parser, fare providers, caching, and call-budget controls.
- `index.html` — chat interface.
- `.gitignore` — excludes local secrets, usage state, logs, Python cache, and macOS metadata.

## Setup

Python 3 is required. The basic parser and mock fare mode use only the Python standard library.

For plain-language requests, install the Anthropic SDK:

```sh
python3 -m pip install anthropic
```

Create a `.env` file in the project root for whichever services you want to use:

```dotenv
ANTHROPIC_API_KEY=your_anthropic_api_key
IGNAV_API_KEY=your_ignav_api_key
```

Both keys are optional. `ANTHROPIC_API_KEY` enables Claude-powered conversation when the SDK is installed. `IGNAV_API_KEY` enables live fares; without it, searches return mock data. Do not commit `.env` or publish API keys.

Optional settings:

- `CLAUDE_MODEL` — override the default Claude model (`claude-sonnet-5-5`).
- `certifi` — optional certificate bundle for Python installations that have SSL certificate verification issues (`python3 -m pip install certifi`).

## Run

Start the local server from the project directory:

```sh
python3 server.py
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000) in a browser. Stop the server with `Ctrl+C`.

With Claude enabled, you can ask in plain language, for example:

```text
Find the cheapest nonstop round trip from Seattle to Lisbon, departing 2026-11-10 and returning 2026-11-20.
```

Without Claude, use three-letter airport or metro codes and an ISO date:

```text
SEA to LIS on 2026-11-10
SEA to LIS on 2026-11-10 and 2026-11-20 ±2
```

## API

The local server exposes a small JSON API used by the chat page:

- `GET /api/usage` — current provider mode and monthly call usage.
- `POST /api/chat` — send a message; accepts `session` and `message` fields.
- `POST /api/approval` — respond to a pending search prompt with `proceed`, `narrow`, or `cancel`.

## Notes

- Mock fares are generated sample data, not current prices or bookable offers.
- Live fares require a valid Ignav API key and are subject to provider availability and limits.
- The app enforces a maximum of 25 new fare calls per request and a monthly allowance of 1,000 calls. Searches using more than five new calls pause for user approval.
- Usage is stored in `usage.json`; server activity is written to `server.log`. Both are local files excluded from Git.
- Fare prices can change. Verify details with the airline or booking provider before purchasing.