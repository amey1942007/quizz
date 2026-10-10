# Live Quiz

Kahoot-style quiz. Phones open the GitHub Pages site. The host runs one Python server on a Windows laptop. That server owns the clock, the scores, and the admin login. Results are stored in SQLite and written to CSV. If Supabase is configured, the leaderboard also updates through Realtime.

Pages site: https://amey1942007.github.io/quizz/

## Run on Windows

1. Install Python 3 from [python.org](https://www.python.org/downloads/) and check **Add python.exe to PATH**.
2. Install the Cloudflare tunnel client:

   ```bat
   winget install Cloudflare.cloudflared
   ```

3. In this folder, install the server packages:

   ```bat
   pip install -r requirements.txt
   ```

4. Optional, for the instant leaderboard: open `supabase/schema.sql` in the Supabase SQL editor and run it once. Copy `server/.env.example` to `server/.env` and paste `SUPABASE_SERVICE_ROLE_KEY`. Leave the key off the Pages site. The game still runs if this step is skipped.
5. Start the server:

   ```bat
   python server/server.py
   ```

   Or double-click `run.bat`. That installs requirements and starts a Cloudflare quick tunnel (`cloudflared tunnel --url http://localhost:PORT`, no account).

6. The window prints a player link and an admin link. Share the player link. Open the admin link on the laptop.

Host login (checked on the server, not in the page): username `robo`, password `orientation`.

Add questions (2–4 options, one correct, 20s by default), create a game, and press **Start** when players are in the lobby. **Show results** closes a question early. **Next** advances. **End** finishes and writes the CSV.

## Scoring

Correct answers score `round(1000 * (1 - (t / limit) / 2))`, measured on the server from the question start. A wrong or missing answer scores 0. Each extra correct answer in a row adds 100 points, capped at 500. Late or duplicate answers are rejected.

CSV columns: player, question number, question, answer, correct, time ms, points, total. Files land in `server/data/exports/`. Admins can also open `/export/{pin}`.

## Supabase

Schema file: `supabase/schema.sql`

Tables, the view, and the realtime publication are all named `quiz_*` so they do not touch the other app in the same project. The anon key can only read `quiz_scores` / `quiz_leaderboard`. The service-role key stays in `server/.env`.

Pages settings: GitHub **Settings → Pages → Build and deployment → GitHub Actions**. The workflow uploads the `docs` folder.
