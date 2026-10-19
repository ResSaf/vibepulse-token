# Grok Pulse (host preview)

An optional, default-off tokenserver source for Grok usage and bot activity.
This part adds the **host endpoints**; the panel page is a separate,
build-time opt-in (see the firmware section once merged). macOS and Windows use the same stdlib implementation. Automated tests
are separate from real-host and physical-panel verification.

Enable with `--grok` (or `VIBEPULSE_GROK=1`) on your existing tokenserver
command. `--grok-source auto` is the default; `VIBEPULSE_GROK_SOURCE` accepts
`auto`, `subscription`, `xai`, `openrouter`, or `bots` (`manual` remains an alias of `subscription`). Restart the existing service after changing
code or its environment: use the restart instructions in
[macOS setup](agent-setup.md) or [Windows setup](windows-setup.md#restarting-the-scheduled-task).
Do not rerun the Windows installer merely to restart the task.

## Sources

### Subscription plan (default)

Most Grok users have a subscription plan. There is **no public usage API for
Grok subscriptions or Grok Bot**. This source never claims a live subscription
reading. Select `--grok-source subscription`, or leave the default `auto`.
Its payload is labelled `source: "plan"`.

Use `state_files.state_dir()` for the service account:
`~/Library/Application Support/VibePulse` on macOS,
`%LOCALAPPDATA%\VibePulse` on Windows. Write exactly one of these three
forms to `grok.json`:

1. **Copied percentage** from grok.com **Settings → Usage**:

   ```json
   {"v":1,"usedPct":42,"resetAt":1790200000,"label":"WEEKLY","plan":"SUPERGROK","window":"weekly"}
   ```

   `usedPct` must be an integer 0–100. Only `v` and `usedPct` are required.
   This is a manual observation with its file age, `estimated:false`.

2. **User-counted usage against your own plan limit**:

   ```json
   {"v":1,"plan":"SUPERGROK","window":"weekly","limit":100,"used":42}
   ```

   `window`, `limit` and `used` are required. The limit is user-set, never
   inferred from a subscription name. `limit` is an integer 1–1,000,000 and
   `used` an integer 0–10,000,000. The percentage is floored and capped at
   100. This carries file age and `estimated:true` (display as **EST**).

3. **Estimate from Grok Bot activity**:

   ```json
   {"v":1,"plan":"SUPERGROK","window":"daily","limit":100,"estimateFrom":"bots"}
   ```

   Uses the bot store's completed tasks today in the computer's local timezone,
   against your user-set limit (the same bound as above). Only `daily` is
   accepted: weekly estimates would need durable history. This is **EST**
   (`estimated:true`), with a live local count, `stale:false` and no file age.
   It does not measure provider usage; service restarts reset the count.

For all forms, `plan` is optional and displayed as uppercase ASCII, at most
12 characters (`planName`). `window` is `daily` or `weekly` and supplies
`windowSeconds` (86400 or 604800) and the default DAILY/WEEKLY label.
The copied-percentage form also accepts a custom `label` up to 16 characters.
Display text folds accents and drops unsupported characters.
`resetAt` is optional epoch seconds from 0 through 2^32 in forms 1 and 2;
otherwise daily windows count down to the next **local midnight** (including
local daylight-saving changes). Weekly windows omit an unknown reset.
Forms 2 and 3 include `planUsed` and `planLimit`.

Unknown keys, mixed forms and invalid numbers are rejected with one sanitized
warning. Files are capped at 64 KiB and read on each check; only changed content is
validated and applied. Replace them atomically to avoid partial reads. Invalid
replacements retain the last valid reading marked stale; a successful parse
clears that invalid state, and deleting the file clears its reading. Bot file
records are applied only after a new valid parse, preventing retained records
from being replayed. Copied observations and user counts become stale after
600 seconds or when their declared resetAt is reached; re-saving unchanged valid plan content refreshes its file age. No automatic grok.com access is implied.

### Source selection

`auto` selects a valid plan file first, then **xAI API**, then **OpenRouter**,
then bot activity once a bot has reported. Without a source it reports `none`.
API sources must be configured and have a fresh or cached reading; until then
they fall through. An API failure retains its last successful values marked
stale; API readings also become stale after 600 seconds. An explicit unavailable
source reports `source: "none"` and omits its numbers. Missing values are
never filled with invented zeroes. Bot activity alone is not a quota reading.

### xAI API (optional secondary source)

This measures console.x.ai API billing, not subscription usage. It uses the
[Management API invoice preview](https://docs.x.ai/developers/rest-api-reference/management/billing).

### xAI Management key

In console.x.ai settings, create a **management key** with billing read access.
This is separate from an inference API key. Set both variables in the
**service environment**, then restart the service:

```sh
export XAI_MANAGEMENT_KEY='<management key>'
export XAI_TEAM_ID='<team ID>'
```

A terminal export alone does not update an already running launchd or Windows
Task Scheduler process. Use your service's environment configuration; keep the
key out of command-line arguments, source control and shared logs. The source
polls immediately at startup, then every 300 seconds after success, with a
15-second timeout. Failures back off exponentially from 300 seconds to one hour.
For HTTP 429/503, a numeric Retry-After can extend that delay up to six hours
(the larger of the backoff and Retry-After, capped at six hours). Success resets
the backoff.
It reads `coreInvoice.totalWithCorr.val` (falling back to `amountAfterVat`),
negates `prepaidCredits.val` for the available credit balance, and uses a
positive postpaid `effectiveSpendingLimit` only. The label is `XAI POSTPAID`:
`spentCents` is the postpaid invoice amount due for the current billing month,
paired with that postpaid spending limit. Usage paid from prepaid credits is
not included, so the amount due stays zero while prepaid credits cover usage.
No limit is inferred from a plan.

### OpenRouter (optional secondary source)

Set `OPENROUTER_API_KEY` in the service environment and restart the service.
Select `--grok-source openrouter` explicitly, or use `auto`. Optionally set
`OPENROUTER_MANAGEMENT_KEY` to enable account credits; an inference API key
cannot read that endpoint. Credentials come only from these environment
variables; this source does not read Keychain or credential files.

The source polls immediately, then every five minutes after success, with the
same failure backoff and HTTP 429/503 Retry-After handling as xAI above, using fixed read-only
[key and credits endpoints](https://openrouter.ai/docs/api-reference/limits),
with redirects refused, bounded responses and an eight-second timeout per
request. The label is `OPENROUTER`. Spend and limit always come from the same window:

- With an `effective_budget` (the budget that blocks first), the panel shows
  that budget's `spend_usd` of `limit_usd` and counts down to its `resets_at`
  (no countdown for a lifetime budget).
- Otherwise, with a key `limit`, spend is `usage_daily`, `usage_weekly` or
  `usage_monthly` to match `limit_reset` (all-time `usage` for a limit that
  never resets), counting down to the next UTC day, Monday or month. When the
  key sets `include_byok_in_limit`, the matching `byok_usage*` is added.
- An unlimited key shows **UTC calendar-month spend for that key**
  (`usage_monthly`), with no limit, counting down to the next month.

Readings with a daily, weekly or monthly window also carry `windowSeconds`, so
the panel can name the window and place its pace marker; lifetime budgets and
never-resetting limits omit it. This is per-key spend, not account-wide spend. Credits are optional
account-wide remaining credits (`total_credits - total_usage`), requiring
non-negative operands and a non-negative balance, shown only with
the management key. A credits failure omits credits and marks the reading
stale. Once a cached accounting window reaches its reset instant, its reading
is stale with resetSeconds zero until a new poll succeeds. Unknown countdowns and missing numbers are omitted. All monetary values
are rounded to USD cents. No limit is inferred from a plan.

## Grok Bot status and local scripts

Grok Bot has no public usage API. These local reports supply activity status
and, only when configured above, a daily plan estimate.

An agent or user can post status from the computer running tokenserver:

```sh
python3 tools/tokenserver/grok_status.py --bot Research --state working --activity searching
curl --fail http://127.0.0.1:8737/api/grok/bots \
  -H 'Content-Type: application/json' \
  --data '{"v":1,"bot":"Research","state":"waiting","activity":"APPROVAL"}'
```

The CLI accepts `--port` (default **8737**) and returns nonzero on failure.
States are `working`, `waiting`, `done`, `idle`, `error`; **waiting means
NEEDS YOU**. This status endpoint cannot approve actions or return answers.
Bot names contain 1–32 characters after control removal; name and activity
keep their case as printable ASCII (accents folded, so `Hårdvara` shows as
`Hardvara`; other characters dropped)
and truncated to 16 characters. A name with no displayable character is
rejected.

Alternatively write `grok-bots.json` in the same state directory:

```json
{"v":1,"bots":[{"bot":"Research","state":"working","activity":"SEARCHING","updatedAt":1790000000}]}
```

Use current epoch seconds for `updatedAt`. Working leases expire after ten
minutes, waiting after two hours, and done/error/idle after thirty minutes.
Expired bots disappear. At most sixteen bots are held (oldest evicted), and
up to four fit in each panel payload, ordered waiting, error, working, done,
idle, then newest first. Escaping-heavy text can reduce the published count
to keep JSON below 480 bytes. Active counts include all held working/waiting
bots. Completions count transitions into done observed today in the computer's
local timezone; repeated done updates do not increment it. Counts are in
memory and restart with the service; file input is not a durable history.

## Privacy and transport

`GET /api/grok` supplies the small flat panel payload on the existing LAN
server. When disabled it returns `{"v":1,"enabled":false}`; posting returns
404. Root diagnostics expose only enabled/source/stale and bot count.
LAN reads of the host preview endpoint `/api/grok` do not count as physical-panel
contact; the route remains listed in diagnostics.
The POST receiver accepts only loopback clients with a valid loopback Host,
no Origin header, JSON content type, known fields and bodies up to 1024 bytes.
It offers no browser CORS access. Local processes can submit statuses.

Grok data is never sent to VibePulse relays. Management keys stay on the host
and are never returned or logged; only the corresponding provider billing requests carry its key.
Bot names/activity and plan labels are visible to LAN clients, so avoid
sensitive task descriptions. Neither prompts nor chat history are collected.
