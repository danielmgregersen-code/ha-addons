# Changelog

## 1.4.1

- Fixed runs being analysed against your **cycling** FTP. The coach now reads
  Intervals.icu's per-sport settings and uses each activity's own thresholds.
- The same bug affected **LTHR and max HR**, so run heart-rate zones were wrong
  too — also fixed.
- If a sport has no thresholds configured, the coach now says so instead of
  quietly borrowing another sport's numbers.

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
