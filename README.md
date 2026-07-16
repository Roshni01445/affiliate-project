---
title: Lookbook Gemini Bot
emoji: 🤖
colorFrom: blue
colorTo: green
sdk: docker
pinned: false
app_port: 7860
---

This is the background server for the Lookbook Gemini Telegram Bot.

## Railway deployment

Use Docker deployment on Railway and add these environment variables:

- `BOT_TOKEN`
- `GOOGLE_SHEET_WEBHOOK_URL`
- `N8N_TRIGGER_URL`
- `N8N_POSTING_URL`
- `HEADLESS=true`
- `CHROME_PROFILE_DIR=/data/chrome_automation_profile`

Important:

- Mount a Railway volume at `/data` so Gemini login cookies and browser state persist across redeploys.
- Open the mounted profile once, sign in to Gemini manually in that persistent browser profile, then rerun the bot.
- If the profile is not persistent, Gemini will keep opening logged out and the image extraction flow will fail.