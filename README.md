# Training Coach — Home Assistant Add-on

This add-on runs your Intervals.icu AI coaching agent entirely inside Home Assistant OS.
No separate processes, no external servers — managed fully from the HA UI.

---

## How it works

Once installed, the add-on:
- Runs a Python backend (FastAPI + GPT-4o agent) as a Docker container inside HA
- Serves a chat UI accessible from your browser or HA sidebar
- Reads your API keys from the HA add-on configuration UI
- Starts automatically when HA boots

---

## Installation

### Step 1 — Put the files on GitHub

HA loads add-ons from a Git repository. You need to host this on GitHub:

1. Create a free GitHub account if you don't have one
2. Create a new repository called `ha-addons` (can be private)
3. Upload the entire `ha-addon` folder contents to it:
   - `repository.json` goes in the root
   - `training-coach/` folder goes in the root

Your repo should look like:
```
ha-addons/
├── repository.json
└── training-coach/
    ├── config.yaml
    ├── Dockerfile
    ├── requirements.txt
    └── app/
        ├── main.py
        ├── agent.py
        ├── intervals.py
        └── static/
            └── index.html
```

Edit `repository.json` and replace `YOUR_USERNAME` with your GitHub username.

### Step 2 — Add the repository to HA

1. In HA, go to **Settings → Add-ons → Add-on Store**
2. Click the **⋮ menu** (top right) → **Repositories**
3. Add your repo URL: `https://github.com/YOUR_USERNAME/ha-addons`
4. Click **Add** → **Close**
5. Scroll down in the store — you'll see **"Training Coach Add-ons"** section appear

### Step 3 — Install the add-on

1. Click **Training Coach** in the store
2. Click **Install** (it will build the Docker image — takes 2–3 minutes)

### Step 4 — Configure API keys

1. Go to the add-on's **Configuration** tab
2. Fill in:
   ```
   openai_api_key: sk-...
   intervals_athlete_id: i12345
   intervals_api_key: your-key-here
   days_back: 14
   days_ahead: 21
   ```
3. Click **Save**

**Where to find your Intervals.icu keys:**
- Log in to intervals.icu
- Go to **Settings → Developer**
- Copy your Athlete ID (shown in the URL, like `i12345`) and generate an API key

### Step 5 — Start it

1. Go to the **Info** tab
2. Toggle **Start on boot** ON
3. Click **Start**
4. Check the **Log** tab — you should see `Uvicorn running on http://0.0.0.0:8000`

---

## Accessing the chat UI

Open your browser and go to:
```
http://homeassistant.local:8000
```

Or from outside your home (if you use Nabu Casa / HA Cloud), you can set up an iFrame panel:

Add to `configuration.yaml`:
```yaml
panel_iframe:
  training_coach:
    title: "Training Coach"
    icon: mdi:bike-fast
    url: "http://homeassistant.local:8000"
```

Then restart HA and it appears in your sidebar.

---

## Updating

1. Push changes to your GitHub repo
2. In HA, go to the add-on → **Info** tab → **Update** (or uninstall/reinstall)

---

## Troubleshooting

**Add-on won't start:**
Check the Log tab. Common issues:
- Missing API keys — make sure all three fields are filled in Configuration
- Port 8000 already in use — check if something else is using it

**"Repository not found" in HA:**
- Make sure `repository.json` is in the root of your GitHub repo (not inside a subfolder)
- The repo must be public, or you need to use a personal access token

**API errors in chat:**
- Verify your OpenAI key is valid and has credit
- Verify your Intervals.icu athlete ID starts with `i` (e.g. `i12345`)
