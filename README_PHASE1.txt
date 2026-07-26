BRACKET BOARD - PHASE 1 SETUP
==============================
You already did the hard part in Step 0 (account, repository, API key
secret). Phase 1 adds three things with a few clicks.

PART A - Upload the engine
  1. Open your  bracket-board  repository on GitHub.
  2. Click "Add file" -> "Upload files".
  3. Drag in:  sweep.py    -> "Commit changes".
  4. Click "Add file" -> "Create new file".
  5. Name it exactly:
        .github/workflows/sweep.yml
  6. Open sweep.yml from this zip in Notepad, copy everything,
     paste into GitHub's text box -> "Commit changes".

PART B - Allow the robot to save its results (one time)
  The sweep writes its database and dashboard back into the
  repository, so it needs write permission:
  1. In the repository: Settings -> Actions -> General.
  2. Scroll to "Workflow permissions".
  3. Select "Read and write permissions" -> Save.

PART C - Turn on the dashboard website (one time)
  1. In the repository: Settings -> Pages.
  2. Under "Build and deployment":
       Source: "Deploy from a branch"
       Branch: "main", Folder: "/docs"  -> Save.
  3. After the first successful sweep run, your dashboard will be at:
       https://YOUR-USERNAME.github.io/bracket-board/
     (GitHub shows the exact address on that same Pages screen.)

PART D - First run
  1. Actions tab -> "Phase 1 - Sweep and Bracket" -> "Run workflow".
  2. It runs for ~15 minutes (deliberately slow - free tier pacing),
     then commits its results. After that it runs itself 4x a day.
  3. Open the dashboard address from Part C. Early on it will show
     partial coverage - that's the budget-friendly crawl working
     through 129 subnets over a few days. Biggest subnets come first.

WHAT PHASE 1 GIVES YOU
  - Every subnet's holders discovered and stored in YOUR database
  - Wallets grouped in your size brackets (1-10 ... 1000+ TAO)
  - Subnet owners flagged (operators, not investors)
  - A daily snapshot per wallet - the raw material for the
    7/14/30/90-day return leaderboards (Phase 3)
  - Automatic budget guard: stays under ~9,000 calls/month, so the
    free tier is never exceeded

WHAT TO SEND BACK IN THE CHAT
  1. endpoint_index.txt  (from your Step 0 artifact zip) - unlocks
     Phase 2: the live event collector + Telegram/email alerts.
  2. After a day or two: your dashboard address, and anything that
     looks wrong or confusing. Odd entries in the early days are
     expected - we tune the filters together.
