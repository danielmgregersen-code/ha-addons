# Training Coach — Home Assistant Add-on

An AI-powered cycling coach that lives inside Home Assistant and connects directly to your Intervals.icu training data. Chat with it like a real coach — ask questions, request feedback, plan workouts, and have it automatically review rides as they sync from your device.

---

## What it does

### Conversational coaching
The coach understands natural language. You don't fill in forms or click buttons — you just talk to it. Ask broad questions like *"How has my training looked this month?"* or specific ones like *"Did I hit my targets on Tuesday's intervals?"* and it will fetch your actual data, analyse it, and respond with real coaching feedback. The coach is objective and direct — it will tell you when a session was poor and why, without unnecessary flattery.

### Multiple sessions
The sidebar lets you create named sessions with separate histories — for example one for calendar planning, one for workout feedback, and one for general questions. Sessions are stored on your Home Assistant device and shared across all devices (phone, tablet, PC) that connect to the same instance. Double-click any session name to rename it.

### Automatic workout review
When a new ride syncs to Intervals.icu and you have logged both an RPE and a feel score, the coach detects it within a few minutes and automatically writes a concise review — covering training load, zone distribution, decoupling, key efforts, and one or two concrete takeaways. The review is posted as a description on the activity in Intervals.icu and marked with a coach tick. A notification card appears only if the auto-review fails; successful reviews go straight to the **⚡ Auto-reviews** session and to the activity on Intervals.icu without interrupting you. From the auto-reviews session you can request a deeper analysis at any time.

### Deep interval analysis
For structured workouts the coach can break down individual intervals — comparing power, heart rate, and cadence across each rep, identifying drift or fatigue within a set, checking whether targets were hit, and commenting on work-to-rest ratios. For matched workouts (rides that completed a planned session) the coach focuses on interval execution and compares against the planned structure; for unstructured rides it gives equal weight to intervals and zone distribution.

### Calendar management
The coach can read your upcoming planned workouts and make changes to them on your behalf. You can ask it to add a new session, modify an existing one, reschedule something, or remove a workout entirely. Workouts are created in the native Intervals.icu structured format — the description renders as a visual bar graph with automatic TSS calculation and can be pushed to a Garmin or other device.

Workout structure follows a standard Warmup / Main set / Cooldown layout with raw percentages or watt values. Each workout includes **press lap markers** at key transitions so you can mark clean lap splits on your device — at the end of the warmup, before each interval rep, and at the start of the cooldown.

### Fueling plan
Every time the coach plans a workout it includes a fueling recommendation in its reply, scaled to the session duration and intensity:

| Duration | On-bike carbs/hr |
|---|---|
| Under 1 h | 0–30 g — water or electrolytes only |
| 1.0–2.5 h | 30–60 g — introduce exogenous carbs to spare glycogen |
| 2.5–4.0 h | 60–90 g — fat oxidation alone is insufficient |
| 4.0+ h | 80–120 g — entirely reliant on exogenous fuel |

At Zone 2 intensity, solid food and complex carbs are fine. At sweet spot, threshold, VO2max, or racing intensity, blood is shunted to the legs — switch entirely to liquid carbohydrates and easily digestible gels and push toward the upper end of the range.

### Weekly planning note
The coach reads and writes a planning note on Monday of each week. If no note exists it writes one automatically, outlining the week's focus, planned sessions with their type and duration, and expected total TSS. It is updated (never duplicated) whenever the plan changes.

### Block periodization with race awareness
Set a `block_start_date` once and the coach tracks your position in a repeating 4-week cycle automatically — Base/Re-introduction, Progressive Overload, Peak Stress, and Deload. The cycle repeats indefinitely from the date you set.

When an A-priority race is within 8 weeks, the coach overrides the normal block cycle to ensure optimal preparation:

- **Race days** — activation only, minimal TSS
- **1–3 weeks out** — taper, cut volume 40–60%, maintain sharpness
- **4–5 weeks out** — forced sharpening/peak phase
- **6–8 weeks out** — no recovery weeks, build extended if needed
- **Beyond 8 weeks** — normal 1-2-3-4 block cycle
- **Post-race** — recovery week regardless of block position

Stage races (multiple consecutive RACE_A events) are automatically detected and treated as a single block.

### Group ride handling
The coach auto-detects group rides by keywords in activity names or tags. When planning a week it checks the calendar for upcoming events matching those keywords and asks you how each group ride should be treated — whether it counts as one of your hard interval sessions, and if so what type (VO2max, threshold, sweet spot, etc.). It also asks about any group rides not yet on the calendar. If a group ride turns out harder than expected (decoupling >8% or RPE ≥8) the following day's session is softened.

### Coach ticks
Whenever the coach posts a comment it also sets a coach tick on the activity — 1 = Really bad through 5 = Amazing — based on TSS, RPE, feel score, interval execution, and decoupling. This marks the workout as reviewed and triggers a notification in Intervals.icu if you have coached activity alerts enabled.

### Wellness-aware feedback
The coach reads your wellness data alongside your training — HRV, resting heart rate, sleep, fatigue, and form scores. Set your personal HRV and resting HR ranges in the configuration and the coach interprets daily values in context rather than in isolation.

### Token counter
A daily token counter in the sidebar shows how many tokens have been used today across all chat and auto-review calls. It resets at midnight and persists across restarts.

---

## Getting your API keys

### OpenAI API key

The coach uses OpenAI's GPT-5.5 model by default. Usage is pay-per-use — a typical coaching conversation costs a few cents, and each auto-review generates one API call.

1. Go to [platform.openai.com](https://platform.openai.com) and sign in or create an account
2. Click your profile icon → **API keys**
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
| `days_back` | Maximum days of past activities the agent can fetch. Default: 28 |
| `days_ahead` | How many days ahead to fetch planned workouts. Default: 21 |
| `max_hours` | Weekly training volume ceiling in hours — the coach will not plan above this |
| `max_tss` | Weekly TSS ceiling — the coach will not plan above this |
| `hrv_min` | Lower end of your normal HRV range in ms (0 = not configured) |
| `hrv_max` | Upper end of your normal HRV range in ms (0 = not configured) |
| `rhr_min` | Lower end of your normal resting HR in bpm (0 = not configured) |
| `rhr_max` | Upper end of your normal resting HR in bpm (0 = not configured) |
| `hard_intervals_per_week` | Number of hard interval sessions to target per week (default: 3) |
| `block_start_date` | Monday that started your current training season in `YYYY-MM-DD` format. Set once — the 4-week cycle repeats automatically |
| `group_ride_keywords` | Keywords to auto-detect group rides in activity names/tags (default: `group,klub,klubtur`) |
| `chat_model` | OpenAI model used for chat and auto-review (default: `gpt-5.5`) |
| `auto_review_model` | OpenAI model used for auto-review (default: `gpt-5.5`) |

---

## Data and privacy

All conversation history, notifications, and token usage are stored locally on your Home Assistant device in the add-on's `/data` directory. This survives version updates and restarts — it is only cleared on a full manual uninstall. Nothing is stored externally except API calls to OpenAI (for AI responses) and Intervals.icu (for training data). Your API keys never leave your Home Assistant instance.

---

## Requirements

- Home Assistant OS or Supervised
- Raspberry Pi 4 or any aarch64/amd64 device running HA
- An OpenAI account with API access and a funded balance
- An Intervals.icu account (free)
