# Withings (Wizard connector)

A Wizard OS connector app for one Withings account. It holds the Withings sign-in, keeps a private synced copy of sleep summaries, daily activity and workouts, and offers that copy read-only to other apps on the same box through the `health-data` spot, contract [`wizard.health-data/1`](docs/health-data-contract.md). The [Health](https://github.com/oscarhenrycollins/wizard-health) app is the screen for the data; this app is only the connection.

**Needs the owner's own developer credentials**: a free "Public API integration" application on the Withings Developer Dashboard. Nothing is shared between boxes.

## Setup (all on the app's own screen)

1. A Withings account the owner's devices sync to. Withings says accounts created on or after 12 October 2026 may need Withings+ to share data with a free-plan developer app.
2. Sign in at https://developer.withings.com/dashboard/, Create an application, choose Public API integration, accept the terms.
3. Fill in the application with the values the screen shows (name, description, callback URL). Each has a Copy button. Withings refuses localhost and IP callbacks, so the callback is this box's own https address (`https://<box>/wizard-withings/callback`).
4. Paste Client ID and Client Secret (stored server-side, never echoed). The app tests them by exchanging a made-up code at `https://wbsapi.withings.net/v2/oauth2` (`action=requesttoken`): a complaint about the client = wrong keys, about the code = keys valid.
5. Connect with Withings: the consent page opens in a new tab of the owner's own browser; after "Allow this app" Withings sends the browser back to the box, where the app exchanges the 30-second code itself. A public placeholder callback with paste-back is offered as a fallback.

## Data and sync

- Tokens: `action=requesttoken` with the client secret (documented variant); access tokens last 3 hours; every refresh returns a new refresh token, saved before use. Revoke on Disconnect is a signed call (Signature v2 - Getnonce, HMAC-SHA256 of the sorted values).
- Backfill of a year after sign-in, then every 20 minutes everything modified since the last sync minus 48 hours (`lastupdate`), paged with `more`/`offset`. Status 601 (rate limit) is retried, then syncing pauses 15 minutes.
- On the free developer plan Withings refuses a whole request that names sleep score, overnight HRV or breathing rate (status 277). The app asks again with the basic fields, leaves those values empty, says so on its screen and retries the full set weekly.
- No readiness score: `readiness` is always `null`. Load is intensity minutes (moderate + 2 x intense), `scale_max` 60, open-ended. Details in `docs/health-data-contract.md` under "Provider mappings".

## Interfaces

- Screen: `/wizard-withings/` (status, Sync now, Disconnect, guided setup).
- Contract (box-only): `GET /api/status`, `GET /api/days?from&to&limit&page&changed_since`.
- Setup API used by the screen: `GET /api/setup`, `POST /api/keys`, `/api/keys/test`, `/api/keys/forget`, `/api/oauth/start`, `/api/oauth/finish`, `/api/sync`, `/api/disconnect`; `GET /callback` (the registered callback URL).
- MCP (`POST /mcp`, box-only): `health_data_status`, `health_data_sync`, `health_data_days`.

## Development

`python3 -m unittest discover -s tests` runs the tests against a fake Withings API built from Withings' published OpenAPI specification (sample objects copied from its examples). No live Withings account was used to build this app. `python3 tools/package.py` refreshes the inline Runtipi payload; `python3 tools/package.py --check` verifies it.
