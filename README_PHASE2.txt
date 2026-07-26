BRACKET BOARD - PHASE 2 SETUP (Collector + Alerts)
===================================================
Phase 2 adds: a collector that checks the network's stake/unstake
feed every ~20 minutes, and alerts (Telegram + email) whenever a
wallet on YOUR watchlist buys or sells.

PART A - Upload the new files (same drill as before)
  1. In your bracket-board repository: "Add file" -> "Upload files".
     Drag in BOTH:  collector.py  and  watchlist.txt
     Also drag in:  sweep.py   (yes, again - this version is tuned to
     share the monthly budget with the collector). Commit changes.
  2. "Add file" -> "Create new file", name it exactly:
        .github/workflows/collect.yml
     Paste the contents of collect.yml from this zip. Commit.

PART B - Telegram alerts (optional but recommended, ~5 minutes)
  1. In the Telegram app, search for:  BotFather  (verified account).
  2. Send it the message:  /newbot
     Follow its two questions (a display name, then a username ending
     in "bot"). It replies with a TOKEN like 123456:ABC-xxxx. Copy it.
  3. Find YOUR chat id: search Telegram for  userinfobot , press
     Start - it replies with your numeric id. Copy it.
  4. IMPORTANT: open a chat with your new bot and press Start
     (bots cannot message you until you do this once).
  5. In GitHub: Settings -> Secrets and variables -> Actions.
     Add two secrets:
        TELEGRAM_BOT_TOKEN   = the token from step 2
        TELEGRAM_CHAT_ID     = the number from step 3

PART C - Email alerts (optional, ~5 minutes)
  Uses a Gmail "app password" - a special password that only allows
  sending mail, and which you can revoke anytime.
  1. Your Google account must have 2-Step Verification ON:
     myaccount.google.com -> Security -> 2-Step Verification.
  2. Then go to:  myaccount.google.com/apppasswords
     Create one (name it "bracket board"). Google shows a 16-letter
     password ONCE - copy it.
  3. In GitHub add secrets:
        GMAIL_ADDRESS        = your full gmail address
        GMAIL_APP_PASSWORD   = the 16-letter password (no spaces)
     Optional:
        ALERT_EMAIL_TO       = a different receiving address
                               (defaults to your gmail)

PART D - First run and how it behaves
  1. Actions tab -> "Phase 2 - Collector and Alerts" -> Run workflow.
  2. The FIRST run is a baseline: it records the current position in
     the event feed but deliberately sends no alerts (so you don't
     get spammed with old history). Alerts begin from the next run.
  3. After that it runs itself every ~20 minutes. GitHub schedules
     are best-effort: sometimes a run is late or skipped. Normal.

PART E - Using your watchlist
  1. Browse your dashboard, pick a wallet, copy its full address.
  2. In the repository, click watchlist.txt -> pencil icon ->
     paste the address on its own line -> Commit changes.
  3. From then on, every buy/sell by that wallet triggers one
     combined Telegram + email alert with subnet, direction, size,
     price, and a link to inspect the wallet.

NOTES
  - Channels you don't configure are simply skipped - the collector
    works fine with Telegram only, email only, both, or neither
    (events still get stored for Phase 3 analysis either way).
  - The collector shares the same monthly API budget guard as the
    sweep: combined they stay under ~9,000 of your 10,000 calls.
  - Alert timing: expect a delay of roughly 5-25 minutes after the
    wallet's transaction. Treat alerts as "review this", never as
    "act instantly" - by the time any copier can react, the price
    has already moved.

WHAT TO SEND BACK IN THE CHAT
  - The log output of your first collector run (Actions -> the run ->
    click the "Run the collector" step). I especially want the line
    starting with "Feed sample keys:" - it confirms the event feed's
    exact shape, and I'll tune the parser if anything differs.
