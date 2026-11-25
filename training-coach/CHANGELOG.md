# Changelog

## 1.11.1

- **Your stored wellness is now actually current.** It was fetched once a day, and
  the first poll after midnight took that slot — hours before last night's sleep
  and HRV are published, so the row stored was empty and nothing tried again until
  the next midnight. The store was permanently a day behind. That went unnoticed
  while the morning check fetched its own data; 1.11.0 moved it to reading the
  store, so on a stale store it would read yesterday's HRV. It now tops the day up
  hourly through the morning until the overnight data has landed, stops as soon as
  it has, and the morning check refreshes it immediately before reading. One
  request each, no tokens.
- **The coach no longer believes the last two days are missing.** Every planning
  and recap prompt ended with a note that the last 48 hours might be incomplete,
  which invited it to re-download rides it already had — they reach the store within
  a couple of minutes of uploading. The note now states what is genuinely pending:
  sessions skipped in the last two days are not yet judged, because an upload can
  land late and calling a session skipped before it arrives would be wrong.

## 1.11.0

A review of everything the add-on sends to OpenAI. Three of these were bugs.

- **Your chat history stopped disappearing.** Every turn, the coach handed back
  only the last three exchanges and the add-on saved exactly that — so anything
  older vanished from the conversation, however far you scrolled. The model still
  sees the recent window; what gets stored is now the whole conversation.
- **A morning alert can no longer go missing.** Whether the wellness check raised
  an alert was decided by the reply starting with the exact text `[ALERT]`. A bold
  marker or a stray quotation mark was enough to turn a poor-readiness morning
  into a silent one. The token is now recognised through formatting, and when
  Garmin readiness reads **poor** the alert is raised regardless of what the model
  wrote — that decision is made in code.
- **The nightly review can no longer change your calendar.** It was being handed
  every tool the add-on has, including create, update and delete workout. It now
  gets only what a review needs: your data, the comment, and the memory note.
- **The morning check no longer downloads a week of raw wellness data.** It was
  asked to average your HRV itself from the raw feed — the same waste removed from
  the Monday recap in 1.8.0, on the one job that runs every day instead of weekly.
  It now reads the figures this add-on already computed, including the 7-day
  average and its range, and says which date they are from so a missing overnight
  sync is visible instead of silent.
- **The Monday recap stopped fetching what it already had.** It was told to call
  for the past week's rides and for a calendar lookup that, by its own
  description, cannot return past events. Both are gone; the week is read from
  memory, and the one call it may still make is for a session from the last 48
  hours.
- **The coach's memo and the "post this review" button are no longer typed out by
  the model.** Both were text markers it had to reproduce character for
  character — and getting an activity id wrong that way is what broke Post review
  in 1.4.2. They are proper actions now, and the id is repaired in code.
- Planning said both "confirm before making changes" and "write every workout in
  one pass". It now says plainly: when you name the period and ask for it to be
  planned, it writes; when you ask an open question, it answers and writes
  nothing. The duplicate-check paragraph that contradicted itself is gone too —
  the add-on has enforced one workout per day server-side all along.
- Planning is now told not to repeat a rep count such as "3 x 12 min" outside the
  interval list, because that is exactly what made some of your sessions
  unmeasurable before 1.10.0.
- A transient server error whose text happened to mention reasoning used to
  disable reasoning for the rest of the day, silently. Only a real rejection does
  that now.
- The static half of each prompt is now stable and the changing half sits at the
  end, so OpenAI's prompt cache can be reused across a request instead of being
  invalidated by the memory block.
- Requests now have an explicit timeout and a cap on reply length, and a tool loop
  that will not converge stops on total tokens rather than on a step count.
- The token counter now also reports how much of the day's total OpenAI served
  from its cache, which is charged at a lower rate.
- One shared copy of the metric reference, the threshold rule and the
  interval-detection caveat. Seven copies had already drifted apart.

## 1.10.0

- **Session reviews were only ever detailed for the last two weeks, and this fixes
  it.** The add-on recorded your planned workouts 14 days back, so any session older
  than a fortnight had nothing to be compared against — which is why 20, 23 and 27
  October showed no interval detail while 3 and 8 November did. It now records them
  six months back, and re-measures your whole back catalogue over about twenty
  minutes after the update.
- 1.9.0 said it would re-measure your stored sessions. It did not reach anything
  older than two weeks: the queue that works through the back catalogue was asking a
  narrower question than the one the re-measure answered. Fixed, and they now share
  a single answer.
- **A session that cannot be measured now says why** — `ex 4 efforts (no plan on
  record)`, `ex 3 efforts (laps did not line up with the plan)` — instead of looking
  identical to an easy spin. Each block also ends with a line like `Execution
  coverage: 12 of 14 measured rep by rep · 2 no plan on record`. Both describe the
  measurement, never your adherence, and the coach is told so explicitly.
- **Over-unders are no longer blank.** A session whose reps each span two power
  targets now reports reps completed and the fade across them — `ex 3x5m 3/3
  275/270/263W fade -4.4% (over-unders: band not judged)`. The band is left unjudged
  because one rep covers two targets; the power is averaged across each rep rather
  than comparing an "over" against the next "under", which would read as a 20%
  collapse inside every rep.
- **Fixed workouts that measured as nothing because of their own fueling line.** Any
  line mentioning something like "3 x 12 min" outside the interval list — the fueling
  note the coach puts first, or the workout's title repeated — made the add-on refuse
  to read the whole workout. A genuinely ambiguous repeat heading is still refused
  rather than guessed.
- Indoor sessions from your history are now recognised as trainer rides when
  re-measured, so hitting the target is credited to the trainer rather than to you.
- The memory block can hold more: 8,000 characters for planning and the Monday recap,
  2,500 for a single-ride review, which is what the richer tags and five long
  auto-review notes actually need. When it still has to be shortened it now drops the
  oldest sessions and says so, instead of discarding the newest ones and the trends.
- Your HRV, resting heart rate and sleep trend was also missing from the Monday recap
  for the same reason as the plans. It is recorded now.
- **Review mode now sees the sessions worth comparing against.** It held your five
  most recent rides of any kind — for six rides a week that is four easy spins and a
  walk — so a conversation about a VO2 session had to fetch its own history from
  Intervals.icu one ride at a time. It now also carries your last ten structured
  sessions with their execution tags, and states its own coverage. The nightly
  auto-review reads the same block, so it can put a session in context instead of
  judging it alone.
- `/debug/storage` reports how many sessions were measured and why the rest were not.

## 1.9.0

- Session reviews now show **what was prescribed and how many reps landed**, not
  just a rep count. A session reads
  `ex 3x15m@88-92% 3/3 2✓ 228/222/209W fade -8.3%`: three 15-minute reps at
  88–92% FTP were planned, three were done, two landed in the band, and the last
  was 8.3% down on the first.
- The watts held in each rep are recorded too, so the coach can discuss a single
  session without fetching its intervals again.
- The auto-review note now opens with the prescribed structure, so it reads as
  "you were down for 3x15 at 88–92% and held two of them".
- Your existing sessions are re-measured automatically over about twenty minutes
  after the update, since the per-rep figures were never stored before.
- Fixed the review instructions claiming the stored tags listed targets hit when
  they did not.

## 1.8.0

- The coach now sees **12 weeks of history** when planning and when writing the
  Monday recap, with the **last 8 weeks session by session** — each one carrying
  how the intervals actually went. Plus 4-week block averages and monthly totals.
- It also sees your **HRV, resting heart rate, sleep and form week by week**.
  The add-on stores those itself now, one request a day.
- **And the recap got cheaper anyway.** It used to be handed the raw wellness
  data from Intervals.icu on every step of its reasoning — roughly 4,000–5,500
  tokens a time. Reading the stored trend instead saves more than the extra
  history costs: about 3,300–4,400 tokens less per recap, for far more context.
- Fixed a week with no riding showing as "– / – · 0 sessions, 0 hard" instead of
  just its note. That happened whenever the recap wrote about a week you were
  ill or away.

## 1.7.1

- Measuring the intervals in your back catalogue now finishes in about twenty
  minutes instead of two to three days. The pacing limit meant to stop a runaway
  loop was set low enough that it throttled the one-off backfill as well.
- The weekly numbers — TSS, hours, RPE, feel, skipped sessions — were never
  affected: those are complete about two minutes after the add-on starts.

## 1.7.0

- The coach now records **whether you actually did the intervals**. TSS could
  never show this: a 5x4 session where you managed three reps and rode tempo
  afterwards lands the same TSS as one you nailed.
- For every structured session it measures reps completed vs. planned, whether
  each rep hit its power target, fade from the first rep to the last, and
  whether any reps were cut short — all from your device's own intervals,
  compared against the workout the coach itself wrote.
- Over time this becomes a pattern the planner can act on: if sweet spot keeps
  getting cut short, it shortens the reps instead of adding more.
- **It will not accuse you wrongly.** If you forgot to press lap, or the workout
  wasn't written by the coach, it says the rep count could not be established
  rather than claiming you missed reps. A session with no execution tag means
  "not measured", never "missed".
- Old sessions are measured too, dripped a few at a time so the first day after
  the update doesn't hammer Intervals.icu. Costs no tokens — none of it is sent
  to the model as raw data.
- Indoor rides are flagged: on a trainer, hitting the target is the trainer's
  doing, so the coach judges completion and fade instead.

## 1.6.0

- The coach now has a **training memory**: a record of how your training actually
  went, kept in the add-on and used when it plans. It means it no longer has to
  re-read months of history from Intervals.icu every time you talk to it, which
  was the bulk of the token cost.
- It remembers, per session: planned vs. actual TSS and time, compliance, RPE,
  feel and decoupling — plus a one-line note from the auto-review, in its own
  words, about what actually happened.
- Per week it keeps hours, TSS, hard sessions, average RPE and feel, and the
  verdict from the Monday recap.
- It also notices **planned sessions you skipped**, and tells the difference
  between skipping one and riding something else instead.
- Used when planning, in the Monday recap, when reviewing a single ride, and in
  the morning wellness check — which previously had no idea what you rode
  yesterday.
- On install it fills itself in from your last six months once. That costs no
  tokens at all: it is one request, read by the add-on, never sent to the model.
- Nothing is ever deleted. The memory lives in `/data` and survives add-on
  updates.

## 1.5.0

- New **Group ride intensity** setting (`easy` / `moderate` / `hard`, default
  `moderate`). The coach no longer asks you to classify every group ride — it
  applies your setting instead.
- Only `hard` spends one of your weekly hard-interval sessions. On `easy` or
  `moderate` the coach keeps planning your full quota of structured sessions and
  keeps the days either side of the group ride genuinely easy.
- The coach can now estimate a group ride's TSS and budget it into your weekly
  cap. The intensity factors it uses are settings, so you can tune them against
  how your own rides turn out: `group_ride_if_<tier>_short` and `_long` for each
  tier, defaulting to 0.65 / 0.60 (easy), 0.72 / 0.70 (moderate) and
  0.80 / 0.77 (hard). `group_ride_long_ride_hours` (default 3, accepts halves)
  sets where a ride switches from the short factor to the long one.
- It still won't put a planned workout on a group-ride day — that would score
  the ride as non-compliant against intervals nobody planned.
- The weekly recap now counts hard sessions against your weekly target, and no
  longer treats a group ride as a failed workout for lacking interval structure.

## 1.4.2

- Fixed **Post review** failing with `404 Not Found`. The coach was dropping the
  leading letter from the activity id (`i172850190` → `172850190`) because the
  prompt asked it for a "numeric id". Posting also repairs a stripped id
  automatically, so reviews already waiting in your chat history can be posted.
- Accepting a review now sets the coach tick, marking the activity as reviewed.
  Previously it didn't, so the nightly auto-review could later overwrite your
  in-depth review with its own shorter note.
- A review that still can't be posted now says which activity id was not found
  instead of showing a raw error.

## 1.4.1

- Fixed runs being analysed against your **cycling** FTP. The coach now reads
  Intervals.icu's per-sport settings and uses each activity's own thresholds.
- The same bug affected **LTHR and max HR**, so run heart-rate zones were wrong
  too — also fixed.
- If a sport has no thresholds of its own, the coach falls back to your
  "other sports" settings when you have them, and otherwise says the thresholds
  aren't configured instead of quietly borrowing another sport's numbers.

## 1.4.0

- Moved to OpenAI's Responses API. Newer reasoning models such as `gpt-5.6` now
  work **with reasoning switched on**. Version 1.3.3 got them working by turning
  reasoning off; that compromise is gone.
- New **Reasoning effort** setting (`none` / `low` / `medium` / `high`, default
  `medium`). Higher means deeper analysis, more tokens and slower replies.
- ⚠️ **Your daily token count will rise.** Reasoning tokens are billed as output
  tokens, and 1.3.3 was suppressing them entirely. If you start hitting the
  daily-budget confirmation sooner than you used to, raise **Daily token budget**
  in the add-on settings, or lower **Reasoning effort**.
- Models that don't support reasoning (such as `gpt-4o`) are detected
  automatically and keep working.

## 1.3.3

- Fixed a 400 error when selecting a newer OpenAI reasoning model such as
  `gpt-5.6`. Those models refuse function tools unless reasoning is switched
  off, so the add-on now detects this and retries with reasoning disabled.
  Older models are unaffected.

## 1.3.2

- Trimmed the Garmin training-readiness output: dropped the band/level label,
  show recovery time in hours, and tidied the feedback wording.
- Docs: added Garmin training-readiness setup instructions and refreshed the
  intro and requirements.

## 1.3.1

- Fixed the add-on build by pinning the Python 3.12 base image (uses the
  prebuilt pydantic-core wheel, so no Rust toolchain is needed).

## 1.3.0

- Added inline accept/reject buttons for proposed workout changes and review
  posting, so you can apply or skip the coach's suggestions in one click.

## 1.2.15

- Quieted the training-readiness startup probe when it succeeds (no more noisy
  log lines on a healthy start).

## 1.2.14

- Fixed the app failing to start with a missing `SUPERVISOR_TOKEN` by launching
  it via `with-contenv`.

## 1.2.13

- Wellness check now diagnoses failing Home Assistant reads and reports why.
- Fixed the container timezone so scheduled checks fire at the right local time.

## 1.2.12

- Health update now reports recovery time and nightly HRV in the Garmin path.

## 1.2.11

- Health update is now driven by the Garmin training-readiness entity, with the
  Intervals.icu data kept as a fallback.

## 1.2.10

- Wellness check now waits until training readiness has been published and is
  capped at 10:00, so it runs once your overnight data is available.

## 1.2.9

- Fixed the wellness check silently failing with a `KeyError` on `hrv_max`.

## 1.2.8

- Version bump finalizing the token-budget UX work from 1.2.7 (no functional
  change).

## 1.2.7

- Token budget no longer hard-stops you. The request that crosses your daily
  limit now finishes and returns its answer, with a heads-up that you're over.
- Later requests while over budget show a single confirmation popup; confirming
  counts for the rest of the day.
- Cancelling the popup is graceful — your prompt rolls back into the input box
  instead of showing a "Stopped" message.

## 1.2.6

- You can now edit the date/time of a chain's last wax event.
- Fixed same-day re-wax counting that day's ride against the freshly waxed chain.

## 1.2.5

- Fixed the wear-edit row overflowing the ride log dialog.

## 1.2.4

- Wellness check now uses Garmin training readiness as its primary signal.

## 1.2.3

- Moved the 0% chain/sealant alert to a badge on the Maintenance tab.

## 1.2.2

- 0% wax/sealant alerts now stay visible until resolved.

## 1.2.1

- Added 25% and 0% life alerts for chain wax and sealant.
- Chain activation date is now stamped, and you can edit the sealant check-in date.
- Fixed a 422 error when editing a wear entry's condition.

## 1.2.0

- Added a sealant tracker, split out from chain wax tracking.

## 1.1.x and earlier

Highlights from the 1.1 series:

- 1.1.64 — Included sleep score in the wellness check and general health check.
- 1.1.63 — Serialized per-session chat and added the daily token budget gate.
- 1.1.62 — Fixed duplicate weekly notes and redundant OpenAI re-fetching.
- 1.1.61 — Stopped Planning mode from entering a verify/adjust loop.
- 1.1.60 — Fixed duplicate workout creation and single-day race mis-classification.
- 1.1.59 — Removed over-permissioned tools from each mode's tool subset.
- 1.1.58 — Added the automated weekly recap and daily wellness check.
- 1.1.57 — Split Training Coach into Review, Health, and Planning modes.
- 1.1.50 — Recover orphaned chat replies after an ingress timeout.
- 1.1.47 — Added weekly max-hours and max-TSS limits.
</content>
