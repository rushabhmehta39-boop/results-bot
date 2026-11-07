# Results Desk — live NSE, NSE SME & BSE result cards on Telegram

Every 5 minutes, round the clock, this bot:

1. Checks for new quarterly / half-yearly results on **NSE, NSE SME (Emerge) and BSE**.
2. Checks the company's **market cap — only ₹700 Cr and above** is posted.
3. Sends a **🔔 alert** the moment the results PDF is out on NSE.
4. Reads the numbers from the company's official **XBRL filing** (the machine-readable copy of the
   results) and sends a **card** as a reply to the alert:
   Revenue · Other income · EBITDA · EBITDA margin · PBT · Exceptional items · PAT,
   each with the previous quarter, the same quarter last year, **YoY** and **QoQ** (SME: half-years, HoH).

No AI guessing and no paid data: the numbers come straight from the exchange filings.
It runs free on GitHub, so your phone or PC does not need to be on.

**What it watches**
- 🔔 alerts: the announcement feeds behind
  [NSE announcements](https://www.nseindia.com/companies-listing/corporate-filings-announcements)
  (main board and SME) and [BSE announcements](https://www.bseindia.com/corporates/ann) (Result category).
  A company listed on both gets one alert, not two.
- Cards: NSE's results XBRL listing (main board and SME) and BSE's results XBRL feed.

---

## Setup from your phone (about 10 minutes)

### 1. Telegram bot
1. In Telegram search **@BotFather** → Start → send `/newbot`.
2. Pick a name (e.g. *My Results*) and a username ending in `bot`.
3. BotFather sends a **token** like `8123456789:AAH...`. Copy it. Keep it private.
4. Open your new bot → **Start** → send `hi`.
5. In Safari open `https://api.telegram.org/bot<TOKEN>/getUpdates` (put your token in place of `<TOKEN>`).
   Find `"chat":{"id":123456789` — that number is your **chat ID**.

### 2. GitHub repository
1. Sign up free at **github.com**.
2. **+ → New repository** → name `results-bot` → **Public** → tick *Add a README* → **Create**.

### 3. Put the files in
*(Skip this if Claude is uploading the files for you.)*
1. On iPhone, tap the downloaded `results-bot.zip` in the **Files** app to unzip it.
2. In your repository: **Add file → Upload files** → select `results_bot.py`, `requirements.txt`,
   `README.md`, `config.example.ini` and the three `Inter-*.ttf` font files → **Commit changes**.
3. **Add file → Create new file** → in the name box type exactly `.github/workflows/results.yml`
   → paste the text from the **Workflow file** section at the bottom of this page → **Commit changes**.

### 4. Add your Telegram details as secrets
In Safari tap **aA → Request Desktop Website** if you don't see the Settings tab.
Repository **Settings → Secrets and variables → Actions → New repository secret**:

| Name | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | your bot token |
| `TELEGRAM_CHAT_ID` | your chat ID |

### 5. Switch it on
1. **Actions** tab → tap **Results bot** → **Run workflow** → choose `test-telegram` → **Run**.
   About a minute later a test card (sample company) arrives in Telegram.
2. Run it once more with `check`. You'll get a "✅ Results Desk is live" message.
   From then on it runs by itself every 5 minutes.

---

## Settings (optional)
Repository **Settings → Secrets and variables → Actions → Variables tab → New repository variable**:

| Variable | Default | Meaning |
|---|---|---|
| `MIN_MCAP_CR` | `700` | Minimum market cap in ₹ crore |
| `INSTANT_ALERTS` | `on` | `off` = no 🔔 alert, only the numbers card |
| `WEBSITE` | `off` | `on` = also build a web page of all cards (see below) |
| `CARD_FOOTER` | (empty) | Text at the bottom of every card, e.g. `@autoresults2026_bot` |

**Website (optional):** set `WEBSITE` to `on`, wait for the next run, then **Settings → Pages → Deploy from
a branch → main → /docs → Save**. Your page: `https://<your-username>.github.io/results-bot/`.

---

## Faster: check every 40 seconds (optional)
GitHub can only start a scheduled run every 5 minutes, and its rules don't allow using it as an
always-on server. For a 40-second refresh, run the same bot on something that stays switched on.
A spare **Android phone** on home Wi-Fi and a charger works well, and home internet is also less likely to
be blocked by NSE than cloud servers.

1. Install **Termux** (from F-Droid) and open it.
2. Run:
   ```
   pkg install -y python python-pillow git
   pip install requests
   git clone https://github.com/<your-username>/results-bot
   cd results-bot
   cp config.example.ini config.ini
   nano config.ini
   ```
   In `config.ini` fill `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`, then save (Ctrl+O, Enter, Ctrl+X).
3. Start it: `termux-wake-lock && python results_bot.py --loop`
   It checks every 40 seconds from 8 AM to 2:30 AM and every 10 minutes overnight.
4. In Android settings, turn off battery optimisation for Termux.
5. **Use one or the other:** on GitHub open **Actions → Results bot → ⋯ → Disable workflow**,
   otherwise you get every card twice.

A Windows or Mac PC works the same way (`pip install -r requirements.txt`, then
`python results_bot.py --loop`).

---

## Good to know
- **Two steps per company.** The 🔔 alert comes from the results PDF (immediate). The card needs the
  XBRL copy, which most companies file within minutes to a few hours, a few only the next day. The card
  always arrives as a reply to its alert.
- **Consolidated first.** If a company files both, you get the consolidated numbers. A standalone-only
  filing is posted after 15 minutes if no consolidated one appears.
- **Market cap** = shares (from the filing) × last closing price (exchange bhavcopy).
- **Comparison columns** come from the company's earlier filings. For a few BSE-only companies they may
  show "–" until the bot has seen their earlier quarters.
- **If an exchange blocks the cloud** you get one ⚠️ message that day; results from the other exchange
  keep coming. NSE sometimes blocks cloud servers; if that happens a lot, use the 40-second option above
  on a device at home.
- GitHub can start scheduled runs a few minutes late at busy times.
- If GitHub ever emails that the workflow was disabled, open **Actions** and tap **Enable workflow**.
- Uses unofficial exchange web addresses; they can change without notice. For personal use.
  **Not investment advice.**

---

## Workflow file
Create `.github/workflows/results.yml` with exactly this:

```yaml
name: Results bot

on:
  schedule:
    # Every 5 minutes, 24 hours a day, every day (GitHub may start runs a few minutes late)
    # ok
    - cron: "*/5 * * * *"
  workflow_dispatch:
    inputs:
      mode:
        description: "What to do"
        type: choice
        options: [check, test-telegram, skip-backlog, resend-today]
        default: check

permissions:
  contents: write

concurrency:
  group: results-bot
  cancel-in-progress: false

jobs:
  check:
    runs-on: ubuntu-latest
    timeout-minutes: 20
    steps:
      - uses: actions/checkout@v5

      - uses: actions/setup-python@v6
        with:
          python-version: "3.12"
          cache: pip

      - name: Install
        run: pip install -r requirements.txt

      - name: Check for new results
        env:
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
          MIN_MCAP_CR: ${{ vars.MIN_MCAP_CR || '700' }}
          INSTANT_ALERTS: ${{ vars.INSTANT_ALERTS || 'on' }}
          WEBSITE: ${{ vars.WEBSITE || 'off' }}
          CARD_FOOTER: ${{ vars.CARD_FOOTER || '' }}
          MODE: ${{ github.event.inputs.mode || 'check' }}
        run: |
          if [ "$MODE" = "test-telegram" ]; then
            python results_bot.py --test-telegram
          elif [ "$MODE" = "skip-backlog" ]; then
            python results_bot.py --skip-backlog
          elif [ "$MODE" = "resend-today" ]; then
            python results_bot.py --resend-today && python results_bot.py
          else
            python results_bot.py
          fi

      - name: Save progress
        if: always()
        run: |
          git config user.name "results-bot"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          for p in state docs; do [ -e "$p" ] && git add -A "$p"; done
          if ! git diff --cached --quiet; then
            git commit -q -m "Update results state"
            for i in 1 2 3 4 5; do git pull -q --rebase && git push -q && break; sleep 5; done
          fi
```
