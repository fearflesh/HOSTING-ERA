# Changes in this round

## Bugs fixed
- **Hosted bots did not receive their own env vars** when the name matched a panel secret
  (BOT_TOKEN, OPENAI_API_KEY, DATABASE_URL, ...). The owner's vars are now passed through.
  The panel's own inherited secrets are still hidden from bots.
- **Env var names were forced to UPPERCASE.** Names are case-sensitive, so they are now stored
  as typed. Invalid names are rejected with a message.
- **Env "delete" buttons broke the whole menu** when a name was long (Telegram limits button
  data to 64 bytes). Buttons now refer to the variable by position. The menu shows up to 20.
- **Database blob cleanup was inverted** (persistence.py): several files vanishing together
  (likely a disk wipe) could delete the MongoDB copies. Now only single or minority deletions
  propagate.
- **Database key no longer falls back to BOT_TOKEN.** Rotating the token used to lock you out
  of the data. Without PERSIST_SECRET the database stays off and logs why.
- **Scanner false positives:** requirements.txt, README and images were flagged as "unknown
  file type". Non-code files are now approved, and only code files are scanned.
- **Scanner:** .mjs/.cjs/.jsx/.tsx/.sh are scanned; .pyc/.pyo go to manual review (they cannot
  be reviewed); tar extraction is protected against path escape; zip bombs (>300 MB) are refused;
  the partial bot token is no longer shown in findings.
- Bare `except:` clauses changed to `except Exception:`.
- Removed the unused `curl_cffi` package from requirements.

## Buttons
- 126 keyboard buttons whose callbacks had no handler were removed (for example most of the
  Bot Config toggles, Pay Limits/Methods, Coupon Bulk/Analytics, Referral settings, Webhooks,
  Goals, Subscriptions reminders, 2FA setup, Leaderboards, and the App Theme/Emoji editor).
  Their render functions are still in the code and can be wired back if needed.

## Code cleanup
- 111 functions were defined twice in bot.py. The later copy was the one that ran, so the
  earlier copies were dead code and were removed. Behaviour is unchanged.

## Memory limit (new)
- A janitor checks every 5 minutes. When panel RAM or the cleanable disk data reaches
  JANITOR_LIMIT_MB (default 400), it deletes: stopped bots' logs, running bots' logs trimmed
  to 64 KB, panel logs, exports, old backups (keeps the newest), temp backup files, and
  __pycache__/.tmp_run. If RAM is still high, it also deletes stopped bots' .deps (reinstalled
  on next start). It never deletes bot source, the database, encrypted uploads, or settings.
  The owner gets a message (at most once an hour) with the result and the largest bots.
  Set JANITOR_LIMIT_MB=0 to disable.
- It cannot shrink the memory of running bots. Each one is a separate process; to stay under
  512 MB, run fewer bots at once or use a larger plan.

## Overload redirect (new)
When the first panel is busy, new users (and anyone who has no bot on this panel) get a message
with a link to the second panel bot. Owners and admins are never redirected, and users who already
own a bot here stay here. Messages are limited to one redirect every 5 minutes per user.
Set these on the FIRST panel only:
- SECOND_BOT_USERNAME: the second panel's bot username, without @ (no value = feature off)
- OVERLOAD_RSS_MB: panel RAM that counts as busy (default 350)
- OVERLOAD_RUNNING_BOTS: optional count of running hosted bots that counts as busy (0 = off)
Telegram cannot move a user between bots automatically, so the user taps the link and starts the
second bot. Existing hosted bots and their data are not moved.

## Restart only the bots that were live (changed)
- Before, the panel restarted every bot marked "running" and also every crashed bot on plans with
  auto-restart. Now it restarts only the bots that were running when the panel went down.
- The live set is saved to storage (live_bots.json) whenever a bot starts or stops, and frozen
  when Render sends SIGTERM. Crashed and stopped bots stay as they are after a restart.
- A bot that exits cleanly (code 0) is now marked "stopped" instead of staying "running".
- Bots still waiting to be restarted stay in the saved set, so a second restart during boot does
  not lose them.

## Admin redirect button (new)
- Admin menu → 🔀 Redirect: shows status and the current RAM, and lets the admin:
  - Set bot link: send https://t.me/YourBot or @YourBot (validated)
  - Turn ON / OFF
  - Clear the link
- The admin-set link is used by the overload redirect. SECOND_BOT_USERNAME is only a fallback
  when no link is set in the admin panel.

## Two panels sharing one database (changed)
- Each panel has an identity, PANEL_ID. On Render it defaults to RENDER_SERVICE_ID, so two
  services get different ids automatically. Set PANEL_ID yourself to override.
- Every bot records the panel that runs it (field "panel"). On boot a panel starts only its own
  bots, so a second panel never restarts the bots of the first one.
- The live set is stored per panel in the settings (live_bots:<PANEL_ID>), not on local disk.
- The database lease is per panel (lease:<PANEL_ID>). Before, two panels sharing a database
  fenced each other: the second one to boot took the lease and the first one stopped saving.
- Encrypted bot files (enc/<owner>/...) are restored only for owners with a bot on this panel.
- Legacy bots with no panel tag are adopted once, by the first panel that boots, then tagged.
- The redirect target and on/off setting are per panel (redirect_bot:<PANEL_ID>).
- Limitation: the panels merge at the document level. Each panel loads the database at boot and
  pushes only its own changes. If both panels change the same user at the same time (for example a
  plan change on one and a payment on the other), the later write of that user wins.

## Panels talk through the database only (new)
- Users are shared: each panel pulls user records other panels changed (every 20 s). A user this
  panel edited since the last sync is kept as it is, and this panel's copy is pushed on the next flush.
- Panel bus: each panel writes a heartbeat (RAM, running bots, bots on this panel) to the panel_bus
  collection every 30 s, and checks for notices addressed to it.
- Admin menu → 🛰 Panels: shows every panel (online/offline, RAM, running bots) and lets the admin
  send a notice to another panel ("panel-B | message"). The receiving panel forwards it to its owner.
- Limitation: a true conflict (the same user edited on both panels before either sync) is resolved
  by whichever panel pushes last. Use one panel for admin edits to the same user, or give each
  panel its own database name.
- Cost: user records are read from the database every 20 s on each panel. This is fine for thousands
  of users. Tell me if you have far more and I'll switch to incremental reads.

## Before deploying (checklist)
1. Two services, two bot tokens. Each panel needs its own BOT_TOKEN. Do not reuse one token.
2. Same MONGO_URL and MONGO_DB on both panels. Same PERSIST_SECRET on both, or one panel cannot read the other's data.
3. Set SECOND_BOT_USERNAME on the FIRST panel only (the redirect target), or use the admin Redirect screen.
4. Admin settings (plans, prices, payment methods, bot settings) are read at boot and are NOT synced
   between panels. Change them on both panels, or restart the other panel after a change.
5. Each hosted bot runs on the panel that started it. Users are shared; bots are not.
6. Check the first boot logs: "[persist] boot", "[boot] restarting N hosted bot(s)", "[janitor] limit".
7. Test: restart one panel, confirm only its live bots return; set a redirect link and check the
   redirect message; send a notice from Panels to the other panel.
