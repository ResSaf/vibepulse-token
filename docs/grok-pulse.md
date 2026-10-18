# Grok Pulse (host preview)

An optional, default-off tokenserver source for Grok usage and bot activity.
This part adds the **host endpoints**; the panel page is a separate,
build-time opt-in (see the firmware section once merged). macOS and Windows use the same stdlib implementation. Automated tests
are separate from real-host and physical-panel verification.

Enable with `--grok` (or `VIBEPULSE_GROK=1`) on your existing tokenserver
command. `--grok-source auto` is the default; `VIBEPULSE_GROK_SOURCE` accepts
`auto`, `xai`, `openrouter`, `manual`, or `bots`. Restart the existing service after changing
code or its environment: use the restart instructions in
[macOS setup](agent-setup.md) or [Windows setup](windows-setup.md#restarting-the-scheduled-task).
Do not rerun the Windows installer merely to restart the task.

## Four sources

- **xai:** xAI API spend from **console.x.ai**, using the
  [documented Management API invoice preview](https://docs.x.ai/developers/rest-api-reference/management/billing).
  It reports month-to-date invoice spend, optional prepaid balance and an
  explicitly supplied positive spending limit, all in USD cents. This is
  **not the X Subscription weekly pool**, which has no documented usage API.
- **openrouter:** per-key UTC month spend, effective budget or key limit, and
  optional account credits from the OpenRouter API.
- **manual:** the percentage you copy from grok.com **Settings → Usage**.
  This remains a manual observation, with age measured from file modification.
- **bots:** current working/waiting count and locally observed completions
  today. Activity is not a quota measurement.

`auto` selects xai, then openrouter, then live bot activity (once any bot
has reported or completed today), then a valid manual file. API sources must
be configured and have a fresh or cached reading; until then they fall through.
An API failure keeps the last successful API values marked stale. An explicit
unavailable source reports `source: "none"` and omits its numbers.
Missing values are omitted, never filled with invented zeroes. API/manual
readings become stale after ten minutes; API failures mark stale immediately.

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
polls immediately at startup, then every 300 seconds, with a 15-second timeout.
It reads `coreInvoice.totalWithCorr.val` (falling back to `amountAfterVat`),
negates `prepaidCredits.val` for the available credit balance, and uses a
positive `effectiveSpendingLimit` only. No limit is inferred from a plan.

### OpenRouter API key

Set `OPENROUTER_API_KEY` in the service environment and restart the service.
Select `--grok-source openrouter` explicitly, or use `auto`. Optionally set
`OPENROUTER_MANAGEMENT_KEY` to enable account credits; an inference API key
cannot read that endpoint. Credentials come only from these environment
variables; this source does not read Keychain or credential files.

The source polls immediately, then every five minutes, using fixed read-only
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

This is per-key spend, not account-wide spend. Credits are optional
account-wide remaining credits (`total_credits - total_usage`), shown only with
the management key. A credits failure omits credits and marks the reading
stale. Unknown countdowns and missing numbers are omitted. All monetary values
are rounded to USD cents. No limit is inferred from a plan.

### Manual quota file

Use `state_files.state_dir()` for the service account:
`~/Library/Application Support/VibePulse` on macOS,
`%LOCALAPPDATA%\VibePulse` on Windows. Write `grok.json` there:

```json
{"v":1,"usedPct":42,"resetAt":1790200000,"label":"WEEKLY"}
```

`usedPct` is a number from 0 to 100. `resetAt` is optional epoch seconds;
`label` is optional, up to 16 characters (displayed uppercase ASCII; accents
are folded and other characters dropped, because the panel fonts are ASCII). The host reports integer percentages and remaining reset seconds.
Files are reread when their modification time changes. Invalid files are
ignored with one sanitized warning; replacing a file atomically avoids partial
reads. This manual snapshot never claims automatic grok.com access.

## Bot agents and local scripts

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
The POST receiver accepts only loopback clients with a valid loopback Host,
no Origin header, JSON content type, known fields and bodies up to 1024 bytes.
It offers no browser CORS access. Local processes can submit statuses.

Grok data is never sent to VibePulse relays. Management keys stay on the host
and are never returned or logged; only the corresponding provider billing requests carry its key.
Bot names/activity and manual labels are visible to LAN clients, so avoid
sensitive task descriptions. Neither prompts nor chat history are collected.
