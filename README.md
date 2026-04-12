# Training Coach — Home Assistant Add-on

An AI-powered cycling coach that lives inside Home Assistant and connects directly to your Intervals.icu training data. Chat with it like a real coach — ask questions, request feedback, plan workouts, and have it automatically review rides as they sync from your device.

---

## What it does

### Conversational coaching
The coach understands natural language. You don't fill in forms or click buttons — you just talk to it. Ask broad questions like *"How has my training looked this month?"* or specific ones like *"Did I hit my targets on Tuesday's intervals?"* and it will fetch your actual data, analyse it, and respond with real coaching feedback.

### Multiple sessions
The sidebar lets you create named sessions with separate histories — for example one for calendar planning, one for workout feedback, and one for general questions. Sessions are stored on your Raspberry Pi and shared across all devices (phone, tablet, PC) that connect to the same Home Assistant instance.

### Automatic workout review
When a new ride syncs to Intervals.icu, the coach detects it within a few minutes and automatically writes a short review — covering training load, zone distribution, decoupling, key efforts, and recovery recommendations. The review is posted as a description on the activity in Intervals.icu and marked with a coach tick to indicate it has been reviewed.

A notification card appears in the chat UI showing the activity name and a preview of the comment. From the notification you can request a more detailed analysis with one tap. Notifications persist across restarts so you never miss an auto-review.

All auto-reviews are also logged chronologically in a dedicated **⚡ Auto-reviews** session, giving you a permanent record of every automated comment the coach has posted.

### Deep interval analysis
For structured workouts the coach can break down individual intervals — comparing power, heart rate, and cadence across each rep, identifying drift or fatigue within a set, checking whether targets were hit, and commenting on work-to-rest ratios. It uses Intervals.icu's detected interval data, so any ride with structured efforts can be analysed this way.

### Calendar management
The coach can read your upcoming planned workouts and make changes to them on your behalf. You can ask it to add a new session, modify an existing one, reschedule something, or remove a workout entirely. When creating workouts it writes full descriptions including warm-up, main set with power or HR targets, and cool-down, and calculates approximate TSS.

### Weekly planning
The coach reads and writes a planning note on Monday of each week. If no note exists it writes one automatically, outlining the week's focus, planned sessions with targets, and expected total TSS. The plan respects the current block periodization phase and your configured hard interval count.

### Block periodization
Set a block start date once and the coach tracks your position in a repeating 4-week cycle automatically — Foundation, Build, Peak, and Recovery — adjusting session intensity and volume recommendations accordingly. You never need to update the date; it cycles indefinitely.

### Group ride handling
Configure your regular group ride days and the coach will reserve those days in the plan (no structured work), deduct estimated TSS from the weekly load budget, and auto-detect group rides by keywords in activity names or tags. If a group ride turns out harder than expected the following day's session is softened automatically.

### Coach ticks
Whenever the coach posts a comment — whether automatically or when asked — it also sets a coach tick on the activity in Intervals.icu. This marks the workout as reviewed and, if you have coached activity notifications enabled in Intervals.icu, triggers a notification to you as the athlete. Ticks are rated: 1 = Really bad through 5 = Amazing.

### Wellness-aware feedback
The coach reads your wellness data alongside your training — HRV, resting heart rate, sleep, fatigue, and form scores. When you set your personal HRV and resting HR ranges in the add-on configuration, the coach interprets your daily wellness values in context rather than in isolation.

### Training metrics
For each activity the coach receives a comprehensive set of metrics including zone times (power Z1–Z7 and HR Z1–Z5), aerobic decoupling, efficiency factor, variability index, RPE, feel, polarisation index, and strain score.

### Race calendar awareness
The coach reads your race calendar including event category (A, B, or C priority) and sport type (Road, Gravel, etc.), so it can factor upcoming races into training load recommendations and taper planning.

---

## Getting your API keys

### OpenAI API key

The coach uses OpenAI's `gpt-5.4-mini` model. Usage is pay-per-use — a typical coaching conversation costs a few cents, and each auto-review generates one API call.

1. Go to [platform.openai.com](https://platform.openai.com) and sign in or create an account
2. Click your profile icon (top right) → **API keys**
3. Click **Create new secret key**, give it a name like "Training Coach", and click **Create**
4. Copy the key immediately — it is only shown once
5. You will need to add a payment method under **Settings → Billing** before the key will work. OpenAI requires a small minimum top-up (typically $5) to activate API access

The key looks like: `sk-proj-...` and should be pasted into the `openai_api_key` field in the add-on configuration.

---

### Intervals.icu API key and athlete ID

Intervals.icu API access is completely free for personal use.

**Finding your athlete ID:**
1. Log in to [intervals.icu](https://intervals.icu)
2. Look at the URL in your browser — it will contain your athlete ID, for example:
   `https://intervals.icu/athletes/i12345/calendar`
3. Your athlete ID is the part starting with `i` — in this example `i12345`

**Generating an API key:**
1. In Intervals.icu, click your profile icon → **Settings**
2. Scroll down to **Developer Settings** near the bottom of the page
3. Click **Generate** next to API Key
4. Copy the key that appears

Paste your athlete ID into `intervals_athlete_id` and your API key into `intervals_api_key` in the add-on configuration. Keep your API key private — anyone with it can read and modify your training data.

---

## Configuration

The following settings are entered in the add-on Configuration tab in Home Assistant:

| Setting | Description |
|---|---|
| `openai_api_key` | Your OpenAI API key |
| `intervals_athlete_id` | Your Intervals.icu athlete ID (e.g. `i12345`) |
| `intervals_api_key` | Your Intervals.icu API key |
| `days_back` | How many days of past activities to fetch (default: 14) |
| `days_ahead` | How many days ahead to fetch planned workouts (default: 21) |
| `hrv_min` | Lower end of your normal HRV range in ms (0 = not configured) |
| `hrv_max` | Upper end of your normal HRV range in ms (0 = not configured) |
| `rhr_min` | Lower end of your normal resting HR in bpm (0 = not configured) |
| `rhr_max` | Upper end of your normal resting HR in bpm (0 = not configured) |
| `hard_intervals_per_week` | Number of hard interval sessions to plan per week (default: 2) |
| `block_start_date` | Monday that started your current training season, e.g. `2026-04-07`. Set once and leave it — the 4-week cycle repeats automatically |
| `group_ride_days` | Comma-separated days with regular group rides, e.g. `Saturday` or `Saturday,Wednesday` |
| `group_ride_weekday_tss` | Estimated TSS for a weekday group ride (default: 60) |
| `group_ride_weekend_tss` | Estimated TSS for a weekend group ride (default: 90) |
| `group_ride_keywords` | Keywords to auto-detect group rides in activity names/tags (default: `group,club,fondo,race`) |

---

## Data and privacy

All conversation history and notifications are stored locally on your Raspberry Pi in the Home Assistant config directory (`/config/training-coach/`). This location survives add-on updates and reinstalls. Nothing is stored externally except the API calls made to OpenAI (for AI responses) and Intervals.icu (for training data). Your API keys never leave your Home Assistant instance.

---

## Requirements

- Home Assistant OS or Supervised
- Raspberry Pi 4 (or any aarch64/amd64 device running HA)
- An OpenAI account with API access and a funded balance
- An Intervals.icu account (free)
