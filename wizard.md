---
wizard: 0.2
id: wizard-withings
name: Withings
type: external
category: system
version: 0.1.0
summary: Connect your Withings. Health and your other apps get read-only sleep, activity and workouts (Withings has no recovery score).
tags: [data]
license: GPL-3.0-only
icon: icon.png
source: https://github.com/oscarhenrycollins/wizard-withings
runtime:
  kind: runtipi
  config: runtipi/config.json
  compose: runtipi/docker-compose.yml
api:
  port: 8834
  path: /api
  spec: openapi.json
mcp:
  path: /mcp
  port: 8834
  transport: streamable-http
skill: skill/SKILL.md
view:
  path: /
  port: 8834
  source:
    path: web
    build: python3 app/build_view.py
    dist: web
provides: [health-data, withings-health-data]
system: true
uses: []
needs: []
health: /healthz
---

# Withings

Connects one Withings account to your Wizard. You need your own free Withings developer application (your own developer credentials): the app walks you through creating it on its own screen. You create a "Public API integration" application on the Withings Developer Dashboard with this box's own callback address, paste its Client ID and Client Secret here, then allow access once in your own browser; Withings sends you straight back to this box and the app finishes the sign-in. The keys and sign-in stay inside this app and are never shown again.

After sign-in the app downloads your recent history (a year) of sleep summaries, daily activity and workouts, then checks for new data every 20 minutes. It keeps the copy here and offers it, read-only, to other apps on this box through the `health-data` spot (contract `wizard.health-data/1`, see `docs/health-data-contract.md`). Withings has no recovery score, so Health shows "no score" for recovery instead of a made-up one; activity is intensity minutes. On Withings' free developer plan, sleep score, overnight HRV and breathing rate may be held back; the app notices and syncs everything else.

Disconnect revokes the app's access on Withings and deletes the local copy.
