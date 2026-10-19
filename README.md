# HOSTING-ERA
# Deploying on Render

1. Push this folder to a **private** GitHub repo.
2. Render → **New → Blueprint** → pick the repo (it reads `render.yaml`).
   Or **New → Web Service**: build `pip install -r requirements.txt`, start `python bot.py`, health check `/healthz`.
3. Set the environment variables:

| Variable | Required | Notes |
|---|---|---|
| `BOT_TOKEN` | yes | main panel bot from @BotFather |
| `OWNER_ID` | yes | your numeric Telegram id. The app refuses to boot without it |
| `SUPPORT_GROUP` | optional | support group invite link. Or set it later from Telegram: `/setsupportgroup <link>` (owner only, `-` clears) |
| `UPDATE_CHANNEL` | optional | updates channel link (defaults to https://t.me/Fsh_bots) |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `GROQ_API_KEY` / `OPENROUTER_API_KEY` | recommended (any one) | enables the AI code scanner. Optional: `AI_SCAN_PROVIDER` (which to try first), `AI_SCAN_MODEL` (model override) |
| `MONGO_URL` | **required for persistence** | MongoDB Atlas connection string. Stores every user, bot, setting, encrypted upload, key and bot data so nothing is lost on restart/crash |
| `PERSIST_SECRET` | **required with MONGO_URL** | long random text that encrypts the stored data. Keep a copy: without it the data cannot be read |
| `MONGO_DB` | optional | database name (default `fsh_panel`) |
| `BOOT_START_CONCURRENCY` | optional | how many bots start at once after a restart (default 3) |
| `GITHUB_TOKEN`, `GITHUB_REPO` | strongly advised on free plan | private repo; panel state is backed up and restored on boot |
| `GITHUB_KEY_REPO` | advised | a *separate* private repo for per-file encryption keys |
| `DATA_DIR` | paid plan only | mount path of a Render disk, e.g. `/var/data` |
| `SELF_PING` / `SELF_PING_SECONDS` | optional | self-ping keeps free instances awake (default on, 600 s) |

`PORT` is set by Render; the app binds it immediately at start-up.

## Free plan notes
* Disk is ephemeral: without the GitHub backup, users/bots vanish on every redeploy.
* During a redeploy the old and new instance briefly overlap, so Telegram may log a few "409 Conflict" polling errors; they clear on their own.

## Privacy model (what changed)
* Only the user who uploaded a bot can download/clone it or open its logs, env vars and error traces.
* Admins and the owner can still see user info (name, plan, join date, wallet, ban status), **number of bots hosted**, running count, and run moderation (stop/restart/delete/ban).
* GitHub backups no longer contain decrypted user source from `sandbox/`.
* Removed: a hidden loader (`_init_locale_cache`) that silently copied every uploaded file to two external Telegram chats.
* `OWNER_ID` no longer defaults to a hard-coded id.
* Anyone with full infrastructure access (Render shell + the GitHub key repo) can still technically decrypt, because the server must decrypt code to run it. This change removes every in-app path.

After upgrading, delete old `backups/*.tar.gz` from your backup repo — earlier backups may contain plaintext source.

## Payments (manual only)
* Owner: **@ADITYA_HNU_YAR** · Appeals / help / support: **@Fsh_bots**.
* Prices are in INR (₹). There is no auto-pay and no auto-approval: users talk to the owner first, pay, send proof, and the owner approves it in the bot.
* Methods: UPI (`yourpaymetid@app`, QR generated for the plan price), Crypto, Telegram Stars, NFT. Crypto / Stars / NFT details are given by the owner in chat.
* UPI QR needs the `qrcode[pil]` package (already in `requirements.txt`). Without it the bot still shows the UPI ID.

## AI code scanner
* Every upload (zips included) is checked by the pattern scanner and then by one AI review of the whole project, using a threat model written for a shared hosting panel.
* The AI can only make the verdict stricter, never more lenient than the pattern scanner. A hard block needs concrete evidence; otherwise the upload goes to manual review.
* Compiled `.pyc` / binaries, heavy obfuscation and oversized projects are flagged for manual review instead of being passed as safe.
* With no AI key set, the bot falls back to the pattern scanner alone. Admin panel → "Scan pending bots" re-runs the full scan on queued uploads.
## Hosted bot username
* If the uploaded code (or its env vars) contains the bot's token, the bot's public @username is looked up via Telegram and shown to the uploader in the approval message. The token itself is never stored or shown.

## Persistent database (MongoDB) — survives restarts and crashes
Render's free disk is erased whenever the service restarts, crashes or redeploys. The panel therefore mirrors its data into MongoDB and restores it on boot.

**Set up (5 minutes, free):**
1. Create a free cluster on MongoDB Atlas (M0). Database Access → add a user with a password. Network Access → allow `0.0.0.0/0` (Render's IPs change).
2. Connect → Drivers → copy the `mongodb+srv://…` string, put your password in it.
3. On Render add `MONGO_URL` and `PERSIST_SECRET` (any long random text, and keep a copy). Redeploy.
4. Send `/dbstatus` to the bot (owner only). It should show *Connected: yes*. The first boot copies the existing data up automatically.

**What is stored:** one record per user, bot, coupon, ticket, payment and setting; the encrypted files each user uploaded (bot source, or the files from their zip, including `requirements.txt`) and their key cache; and admin banners. **Data a hosted bot creates while running (its own databases, json files, downloads) is NOT stored.**

**What happens after a crash or restart:**
1. SIGTERM (normal restarts/redeploys): everything is flushed to MongoDB first. A hard crash can lose at most the last ~2 seconds of changes.
2. Boot: database, settings, files and keys are restored, paths are re-pointed to the new disk.
3. Every bot that was running (and crashed bots of plans with auto-restart) is started again, staggered and retried once; bots waiting for approval, rejected, or belonging to banned users are skipped. The bot's uploaded files are decrypted fresh and its requirements are reinstalled.
4. You get a summary, and each user is told which of their bots are back (turn off per-user messages with the setting `boot_notify_users`).

**Safety:** data in MongoDB is encrypted with `PERSIST_SECRET`; a wrong secret puts the panel in degraded mode and never overwrites the stored data. Only one panel instance writes at a time (a lease), so an overlapping old instance during a deploy cannot overwrite newer data. If MongoDB is unreachable at boot the panel still runs on local data and warns you; fix it and use `/dbsync push` (keep this server's data) or `/dbsync pull` (take the database's data).

**Owner commands:** `/dbstatus` (health, counts, storage used) · `/dbsync push|pull`.
**Limits:** Atlas free tier is 512 MB total (uploads count). Keep `SELF_PING=1` (or an external pinger) so the free web service does not sleep and stop your bots.
