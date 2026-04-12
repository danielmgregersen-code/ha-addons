# Training Coach — Home Assistant Add-on

An AI-powered cycling coach that lives inside Home Assistant and connects directly to your Intervals.icu training data. Chat with it like a real coach — ask questions, request feedback, plan workouts, and have it automatically review rides as they sync from your device.

---

## What it does

### Conversational coaching
The coach understands natural language. You don't fill in forms or click buttons — you just talk to it. Ask broad questions like *"How has my training looked this month?"* or specific ones like *"Did I hit my targets on Tuesday's intervals?"* and it will fetch your actual data, analyse it, and respond with real coaching feedback.

### Multiple sessions
The sidebar lets you create named sessions with separate histories — for example one for calendar planning, one for workout feedback, and one for general questions. Sessions are stored on your Raspberry Pi and shared across all devices (phone, tablet, PC) that connect to the same Home Assistant instance. Double-click any session name to rename it.

### Automatic workout review
When a new ride syncs to Intervals.icu, the coach detects it within a few minutes and automatically writes a short review — covering training load, zone distribution, decoupling, key efforts, and recovery recommendations. The review is posted as a description on the activity in Intervals.icu and marked with a coach tick to indicate it has been reviewed.

A notification card appears in the chat UI showing the activity name and a preview of the comment. From the notification you can request a more detailed analysis with one tap. Notifications persist across restarts so you never miss an auto-review.

All auto-reviews are also logged chronologically in a dedicated **⚡ Auto-reviews** session, giving you a permanent record of every automated comment the coach has posted.

### Deep interval analysis
For structured workouts the coach can break down individual intervals — comparing power, heart rate, and cadence across each rep, identifying drift or fatigue within a set, checking whether targets were hit, and commenting on work-to-rest ratios. It uses Intervals.icu's detected interval data, so any ride with structured efforts can be analysed this way. For matched workouts (rides that completed a planned session) the coach focuses primarily on interval execution; for unstructured rides it gives equal weight to both intervals and zone distribution.

### Calendar management
The coach can read your upcoming planned workouts and make changes to them on your behalf. You can ask it to add a new session, modify an existing one, reschedule something, or remove a workout entirely. Workouts are created in the native Intervals.icu structured format — the description is parsed into a visual bar graph with automatic TSS calculation, and the workout can be pushed to a Garmin or other device. Descriptions use raw percentages or watt values (e.g. `80-90%`, `250w`) rather than zone notation.

### Weekly planning note
The coach reads and writes a planning note on Monday of each week. If no note exists it writes one automatically, outlining the week's focus, planned sessions with their type and duration, and expected total TSS. The note is concise — it describes the character of each session without detailing specific interval structures. It is updated (not duplicated) whenever the plan changes.

### Block periodization with race awareness
Set a `block_start_date` once and the coach tracks your position in a repeating 4-week cycle automatically — Foundation, Build, Peak, and Recovery. You never need to update the date; it cycles indefinitely from the date you set.

When an A-priority race is within 8 weeks, the coach overrides the normal block cycle to ensure optimal preparation:

- **Race days** — activation only, minimal TSS
- **1–3 weeks out** — taper, cut volume 40–60%, maintain sharpness
- **4–5 weeks out** — forced sharpening/peak phase
- **6–8 weeks out** — no recovery weeks allowed, build extended if needed
- **Beyond 8 weeks** — normal 1-2-3-4 block cycle
- **Post-race** — recovery week regardless of block position

Stage races (multiple consecutive RACE_A events) are automatically detected and treated as a single block, with post-race recovery counting from the last stage.

### Group ride handling
Configure your regular group ride days and the coach will reserve those days in the plan (no structured work), deduct estimated TSS from the weekly load budget, and auto-detect group rides by keywords in activity names or tags. Weekday and weekend rides have separate TSS estimates. If a group ride turns out harder than expected (decoupling >8% or RPE ≥8) the following day's session is softened automatically. If no group ride days are configured, the coach will ask when planning a week whether any group rides are planned and on which days.

### Coach ticks
Whenever the coach posts a comment — whether automatically or when asked — it also sets a coach tick on the activity in Intervals.icu. The tick reflects the session quality: 1 = Really bad through 5 = Amazing, chosen based on TSS, RPE, feel score, interval execution, and decoupling. This marks the workout as reviewed and, if you have coached activity notifications enabled in Intervals.icu, triggers a notification to you as the athlete.

### Wellness-aware feedback
The coach reads your wellness data alongside your training — HRV, resting heart rate, sleep, fatigue, and form scores. When you set your personal HRV and resting HR ranges in the add-on configuration, the coach interprets your daily wellness values in context rather than in isolation.

### Training metrics
For each activity the coach receives a comprehensive set of metrics including zone times (power Z1–Z7 and HR Z1–Z5), aerobic decoupling, efficiency factor, variability index, RPE, feel (1=Strong through 5=Weak), polarisation index, strain score, and compliance (whether the ride matched a planned workout).

### Race calendar awareness
The coach reads your race calendar including event category (A, B, or C priority) and sport type (Road, Gravel, etc.). Only A-priority races influence periodization. B and C races are visible for context but do not override the block cycle.

---

## Getting your API keys

### OpenAI API key

The coach uses OpenAI's `gpt-5.4-mini` model. Usage is pay-per-use — a typical coaching conversation costs a few cents, and each auto-review generates one API call.

1. Go to [platform.openai.com](https://platform.openai.com) and sign in or create an account
2. Click your profile icon (top right) → **API keys**
3. Click **Create new secret key**, give it a name like "Training Coach", and click **Create**
4. Copy the key immediately — it is only shown once
5. Add a payment method under **Settings → Billing**. OpenAI requires a minimum top-up (typically $5) to activate API access

The key looks like `sk-proj-...` and goes into the `openai_api_key` field in the add-on configuration.

---

### Intervals.icu API key and athlete ID

Intervals.icu API access is completely free for personal use.

**Finding your athlete ID:**
1. Log in to [intervals.icu](https://intervals.icu)
2. Look at the URL — it contains your athlete ID, for example:
   `https://intervals.icu/athletes/i12345/calendar`
3. Your athlete ID is the part starting with `i` — in this example `i12345`

**Generating an API key:**
1. In Intervals.icu, click your profile icon → **Settings**
2. Scroll to **Developer Settings** near the bottom
3. Click **Generate** next to API Key and copy the result

Paste your athlete ID into `intervals_athlete_id` and your API key into `intervals_api_key`. Keep your API key private — anyone with it can read and modify your training data.

---

## Configuration

| Setting | Description |
|---|---|
| `openai_api_key` | Your OpenAI API key |
| `intervals_athlete_id` | Your Intervals.icu athlete ID (e.g. `i12345`) |
| `intervals_api_key` | Your Intervals.icu API key |
| `days_back` | Maximum days of past activities the agent can fetch. The agent uses the minimum needed per question — 3–5 for a single workout, 7–10 for weekly review, up to this cap for trend analysis. Default: 28 |
| `days_ahead` | How many days ahead to fetch planned workouts for calendar management. Race detection uses a separate call and is unaffected by this setting. Default: 21 |
| `hrv_min` | Lower end of your normal HRV range in ms (0 = not configured) |
| `hrv_max` | Upper end of your normal HRV range in ms (0 = not configured) |
| `rhr_min` | Lower end of your normal resting HR in bpm (0 = not configured) |
| `rhr_max` | Upper end of your normal resting HR in bpm (0 = not configured) |
| `hard_intervals_per_week` | Number of hard interval sessions to plan per week (default: 2) |
| `block_start_date` | Monday that started your current training season in `YYYY-MM-DD` format. Set once and leave it — the 4-week Foundation/Build/Peak/Recovery cycle repeats automatically forever |
| `group_ride_days` | Comma-separated days with regular group rides, e.g. `Saturday` or `Wednesday,Saturday` |
| `group_ride_weekday_tss` | Estimated TSS for a weekday group ride (default: 120) |
| `group_ride_weekend_tss` | Estimated TSS for a weekend group ride (default: 180) |
| `group_ride_keywords` | Keywords to auto-detect group rides in activity names/tags (default: `group,club,fondo,race`) |

---

## Data and privacy

All conversation history and notifications are stored locally on your Raspberry Pi in the add-on's `/data` directory. This survives version updates and restarts — it is only cleared on a full manual uninstall. Nothing is stored externally except API calls to OpenAI (for AI responses) and Intervals.icu (for training data). Your API keys never leave your Home Assistant instance.

---

## Requirements

- Home Assistant OS or Supervised
- Raspberry Pi 4 (or any aarch64/amd64 device running HA)
- An OpenAI account with API access and a funded balance
- An Intervals.icu account (free)
