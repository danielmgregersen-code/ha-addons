# Training Coach — Home Assistant Add-on

An AI-powered cycling coach that lives inside Home Assistant and connects directly to your Intervals.icu training data. Chat with it like a real coach — ask questions, request feedback, plan workouts, and have it automatically review rides as they sync from your device.

---

## What it does

### Conversational coaching
The coach understands natural language. You don't fill in forms or click buttons — you just talk to it. Ask broad questions like *"How has my training looked this month?"* or specific ones like *"Did I hit my targets on Tuesday's intervals?"* and it will fetch your actual data, analyse it, and respond with real coaching feedback.

### Multiple sessions
The sidebar lets you create named sessions with separate histories — for example one for calendar planning, one for workout feedback, and one for general questions. Sessions are stored on your Raspberry Pi and shared across all devices (phone, tablet, PC) that connect to the same Home Assistant instance. Session names must match across devices to share the same history.

### Automatic workout review
When a new ride syncs to Intervals.icu, the coach detects it within a few minutes and automatically writes a short review — covering training load, zone distribution, decoupling, key efforts, and recovery recommendations. The review is posted as a description on the activity in Intervals.icu and marked with a coach tick to indicate it has been reviewed.

A notification card appears in the chat UI showing the activity name and a preview of the comment. From the notification you can request a more detailed analysis with one tap, which opens a pre-filled message in your current session. Notifications persist across restarts so you never miss an auto-review, and unseen ones are shown when you next open the app.

All auto-reviews are also logged chronologically in a dedicated **⚡ Auto-reviews** session, giving you a permanent record of every automated comment the coach has posted.

### Deep interval analysis
For structured workouts the coach can break down individual intervals — comparing power, heart rate, and cadence across each rep, identifying drift or fatigue within a set, checking whether targets were hit, and commenting on work-to-rest ratios. It uses Intervals.icu's detected interval data, so any ride with structured efforts can be analysed this way.

### Calendar management
The coach can read your upcoming planned workouts and make changes to them on your behalf. You can ask it to add a new session, modify an existing one, reschedule something, or remove a workout entirely. When creating workouts it writes full descriptions including warm-up, main set with power or HR targets, and cool-down, and calculates approximate TSS.

### Coach ticks
Whenever the coach posts a comment — whether automatically or when asked — it also sets a coach tick on the activity in Intervals.icu. This marks the workout as reviewed and, if you have coached activity notifications enabled in Intervals.icu, triggers a notification to you as the athlete.

### Wellness-aware feedback
The coach reads your wellness data alongside your training — HRV, resting heart rate, sleep, fatigue, and form scores. When you set your personal HRV and resting HR ranges in the add-on configuration, the coach interprets your daily wellness values in context rather than in isolation. A suppressed HRV or elevated resting HR alongside a heavy training week will factor into its recommendations.

### Training metrics
For each activity the coach receives a comprehensive set of metrics including:
- **Zone times** — seconds spent in each power zone (Z1–Z7) and HR zone (Z1–Z5)
- **Aerobic decoupling** — HR drift relative to power, indicating aerobic fatigue
- **Efficiency factor** — power-to-HR ratio, a long-term aerobic fitness indicator
- **Variability index** — normalised power divided by average power, showing how steady the effort was
- **RPE and feel** — subjective feedback logged in Intervals.icu (feel scale: 1 = Strong through 5 = Weak)
- **Polarisation index** — distribution of training stress between low and high intensity
- **Strain score** — overall session strain

### Race calendar awareness
The coach reads your race calendar including event category (A, B, or C priority) and sport type (Road, Gravel, etc.), so it can factor upcoming races into training load recommendations and taper planning.

---

## Configuration

The following settings are entered in the add-on Configuration tab in Home Assistant:

| Setting | Description |
|---|---|
| `openai_api_key` | Your OpenAI API key (platform.openai.com) |
| `intervals_athlete_id` | Your Intervals.icu athlete ID (e.g. `i12345`, visible in the URL) |
| `intervals_api_key` | Your Intervals.icu API key (Settings → Developer) |
| `days_back` | How many days of past activities to fetch when asked (default: 14) |
| `days_ahead` | How many days ahead to fetch planned workouts (default: 21) |
| `hrv_min` | Lower end of your normal HRV range in ms (0 = not configured) |
| `hrv_max` | Upper end of your normal HRV range in ms (0 = not configured) |
| `rhr_min` | Lower end of your normal resting HR range in bpm (0 = not configured) |
| `rhr_max` | Upper end of your normal resting HR range in bpm (0 = not configured) |

---

## Data and privacy

All conversation history and notifications are stored locally on your Raspberry Pi in the Home Assistant `/data` directory. Nothing is stored externally except the API calls made to OpenAI (for AI responses) and Intervals.icu (for training data). Your OpenAI API key and Intervals.icu credentials never leave your Home Assistant instance.

OpenAI API usage is pay-per-use. A typical coaching conversation costs a few cents. The auto-review feature generates one API call per new activity.

---

## Requirements

- Home Assistant OS or Supervised
- Raspberry Pi 4 (or any aarch64/amd64 device running HA)
- An OpenAI API key with access to `gpt-5.4-mini`
- An Intervals.icu account with API access enabled (free)
