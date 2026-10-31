---
name: wizard-withings
description: Use when the owner asks about his Withings connection, Withings setup or sync. Health questions go to Health.
version: 0.1.1
author: Humanity Labs
license: GPL-3.0-only
metadata:
  hermes:
    tags: [withings, health-data, connector]
    related_skills: [wizard-health]
---
# Withings (connector)

The box's Withings connection (`health-data` spot, contract `wizard.health-data/1`). It holds the Withings sign-in and a synced copy; the **Health** app (`/wizard-health/`) shows the data and answers health questions. Tools: `mcp__wizard__wizard_withings__<tool>`.

- Questions about sleep, activity, workouts or trends: use the Health app's tools, not these. Withings has no recovery/readiness score: say so, never estimate one.
- "Is my Withings connected / when did it last sync": `health_data_status`. `connected`, `syncing` or `rate_limited` = data is flowing; `not_configured` or `needs_login` = the owner must finish or redo setup. `setup.plan_limited` lists data his free Withings developer plan holds back (sleep score, overnight HRV, breathing rate).
- Setup, reconnecting or replacing keys: send the owner the app link `[Withings](/wizard-withings/)` and say its screen walks him through it (five steps: Withings account, developer dashboard, fill in the application with the copy buttons, paste Client ID and Secret, Connect). It needs his own free Withings developer application. Never walk him through the dashboard in chat, and never ask for or accept a Client ID, Client Secret, password, sign-in code or callback address in chat.
- `health_data_sync` asks for fresh Withings data now (the app also syncs every 20 minutes); it returns at once.
- `health_data_days` is the raw contract read (load = intensity minutes, moderate + 2 x intense). Use it only to check what the connector holds.
- Scripts on the box can read the same contract at `http://127.0.0.1:8834/api/status` and `/api/days`.
