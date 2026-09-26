# G-Status — Student Status Portal

A read-only portal that turns a Google Form's linked **Form Responses** sheet into a
live status page for every student, one page per Member ID.

Google Form → Google Sheet → G-Status → `/G001`

The app never writes to your sheet, never submits forms and never asks for a Google
password. It reads the sheet over HTTPS and caches it in memory for ~20 seconds.

---

## 1. Prepare the Google Sheet

1. Open the responses spreadsheet of your Google Form.
2. **Share → General access → Anyone with the link → Viewer**
   (or **File → Share → Publish to web**).
3. Copy the link, e.g.
   `https://docs.google.com/spreadsheets/d/1AbC.../edit#gid=0`

### Columns

Headers are auto-detected, in any order, with flexible naming:

| Purpose      | Accepted header examples                   |
| ------------ | ------------------------------------------ |
| Member ID    | Member ID, MemberID, Student ID, ID        |
| Name         | Student Name, Name, Full Name              |
| Status       | Status, Current Status, Category           |
| Activity     | Activity, Activity Name, Event, Reason     |
| Points       | Points, Point, Score, Marks (may be `-5`)  |
| Description  | Description, Remarks, Note, Comment        |
| Timestamp    | Timestamp, Date, Submitted At              |
| Extra fields | Class, Section, Room No, Hostel, Phone …   |

Every extra column is shown automatically on the student card.
Negative point values are subtracted from the total.

## 1b. Student Master (full student roll, optional)

Add a second Google Sheet that lists **every** student. Students who have no
form responses yet are still found and show **0 points** ("Not marked yet")
instead of "Student Not Found".

- Open `/admin` → **Student Master sheet link** → paste the link → **SAVE & TEST**
  (or set the `STUDENT_MASTER_URL` / `STUDENT_MASTER_NAME` environment variables).
- Share the master sheet as **Anyone with the link → Viewer**, like the responses sheet.
- The master can be either separate columns (`Member ID`, `Student Name`,
  `Room No`, `Cupboard No`, `Class` …) **or** one column of combined cells, with or
  without a header row:

  `AG7890 NAYANDEEP PRAJAPATI 202 6`
  → Member ID `AG7890` · Name `NAYANDEEP PRAJAPATI` · Room `202` · Cupboard `6`

- A master **without a header row** also works, e.g. `AF9846 | DAKSHIT ARORA | VI | VI-AQUA | | HOSTEL | | NEW | | | GURUGRAM`:
  the ID, name, class-section and Hostel/Day-School columns are recognised by their contents.
  Optional `Room No` (e.g. 203) and `Cupboard No` (1-8) columns are recognised the same way.
- The same combined format is understood in the Form Responses sheet (for example a
  "Select student" dropdown), so the Member ID, name, room and cupboard are split
  automatically.
- Room / Cupboard / Class / Type from the master are shown on the student card.
- `/admin` shows how many students are marked and how many are not (showing 0).
- If the master sheet cannot be read, the portal keeps working for students who
  already have responses.

## 1c. Hostel / Day-school form layout & leaderboard

The portal recognises this response sheet layout automatically:

| Timestamp | Name (Hostel) | Name (DAY SCHOOL) | Activities (Positive) | Activites (Negative) |
| --- | --- | --- | --- | --- |

- **Hostel name cell:** `AG6748 VI-AQUA YUG GARG 203 6` → ID · class · name · room **203** · cupboard **6**
  (rooms 201–204, cupboards 1–8)
- **Day-school name cell:** `VII-AQUA AG5993 KARTIK` → class · ID · name (no room / cupboard)
- **Hostel discipline column** (optional, hostel students only): a score **out of 10**, typed as
  `8`, `8/10` or `8 out of 10`. It always **adds** that many points (maximum 10; `15` counts as 10)
  and appears in the history as *Hostel discipline · 8 / 10*. A cell with no number is ignored.
- **Activity cell:** `Late for Prayer -5` → activity + points. The *Positive* column always
  adds points, the *Negative* column always deducts. Several activities in one cell
  (`Helping Others +3, Prayer Leader +2`) become separate records. An item with no number
  (`Disobeying instructions.`) is recorded with 0 points.
- If both name columns are filled in one row, the activity is recorded for both students.

`/leaderboard` shows
- **Room ranking** - total, earned, deducted and average per student (toggle
  *Per student* / *Total points*). Room sizes come from the Student Master when connected.
- **Most disciplined** - students who earned points and have **never** had a deduction,
  ranked by points. Students with any deduction are never listed by name here.
- **Hostel Students** (`/leaderboard?view=hostel`) - every hostel student with room, status
  (Good / Average / Bad) and points. Search by name or Member ID, filter by room or status,
  sort by name / points / room. **Tap a name to open `/<MemberID>`.** A student counts as hostel
  when the Student Master's *Hostel / Day School* column says HOSTEL; without that column,
  a room number or an entry in the *Name (Hostel)* form column is used.
- **Day School** (`/leaderboard?view=day`) - the same idea for day scholars: search, filter by
  class or status, sort by name / points. Everyone who isn't counted as hostel (see above) lands here.
- **GStar** (`/leaderboard?view=gstar`) - hostel and day school students ranked together in one
  list by total points, each with their photo if one is saved in the
  [student photo library](#1e-student-photo-library-supabase). The top 3 can be used to fill a
  *separate* rank ceremony (pick **GStar** as the source in `/rank/admin`) without disturbing
  whatever the main ceremony is set to.
- **Weekly Report** (`/leaderboard?view=weekly`) - the same combined hostel + day school ranking
  as GStar, but only counting activity from **this Monday through Sunday**. It resets itself
  automatically at the start of each week (nothing to click, nothing to clear) and never affects
  the totals shown on the other tabs, which stay all-time.

## 1d. Rank ceremony (`/rank`)

A full-screen, PowerPoint-style reveal of the top 3 students on the red-curtain background.

- **Set up:** open `/rank/admin`, type each winner's name (plus an optional small line such as class / room),
  upload a photo (JPG, PNG, WebP or GIF, up to 10 MB) and press **SAVE**. The reveal order is the countdown
  3rd -> 2nd -> 1st by default (switchable to 1st -> 2nd -> 3rd), and it ends with a slide showing all three. A rank with no name is skipped.
- **Fill from the leaderboard:** instead of typing names by hand, pick a source and press
  **Fill from leaderboard** - *Most disciplined students*, *Room ranking* (text only, no photo), or
  *GStar* (hostel + day school combined). For student sources, if that Member ID has a photo saved in
  the [student photo library](#1e-student-photo-library-supabase) it is attached automatically.
- **Present:** open `/rank` and click **Begin**. For every rank, *Next* first **writes the rank** ("First rank") while the
  suspense music (`static/rank/suspense.mp3`, the Death Note theme) loops, then the next *Next* **reveals** the photo, name
  and rank medal with the victory fanfare (`static/rank/fanfare.mp3`) and confetti. A final slide shows all three winners.
- **Controls:** click / `->` / `Space` / `Enter` = next, `<-` = back, `F` fullscreen, `M` mute.
  (Browsers only allow audio after a click, which is why the first slide waits for *Begin*.)
- Replace `static/rank/bg.png`, `suspense.mp3` or `fanfare.mp3` to change the look or music.
- **Built-in photos:** put `rank1.png`, `rank2.png`, `rank3.png` (or `.jpg` / `.webp` / `.gif`) in `static/rank/default/`.
  The first time the ceremony is opened, every rank that has no photo yet gets the one from that folder.
  Uploading in `/rank/admin` replaces it.
- **Storage:** names, settings and the photos themselves are saved in **Supabase** (see below) - not on the
  server's own disk. Everyone who opens the server sees the same ceremony, and it survives restarts and
  multiple server instances (Render free plan, Vercel). `ADMIN_TOKEN`, if set, is required to save.

## 1e. Student photo library (Supabase)

`/students/admin` is a small, standalone page: type a Member ID, upload a photo, done. It's the
photo source that `/rank/admin`'s "Fill from leaderboard" reuses automatically, so a photo is
uploaded once per student rather than once per ceremony. The same photo also shows next to that
student's name on the GStar leaderboard tab and on their own status page.

## 1f. Supabase setup (required for photos + the rank ceremony)

Both the rank ceremony and the student photo library are stored in
[Supabase](https://supabase.com) - a free hosted Postgres database plus file storage - instead of a
local file, so they work correctly on hosts with an ephemeral disk (Render free plan, Vercel) and
stay in sync across every server / every visitor.

1. Create a free project at [supabase.com](https://supabase.com).
2. **Storage -> New bucket** - create a bucket (the app expects it to be named `gstatus` by default;
   change `SUPABASE_BUCKET` below if you name it something else). It does not need to be public -
   the app fetches photos itself and serves them through `/rank/photo/<n>` and `/students/photo/<id>`.
3. **SQL Editor** - run once:
   ```sql
   create table if not exists gstatus_kv (
       key text primary key,
       value jsonb not null,
       updated_at timestamptz not null default now()
   );
   ```
4. **Project Settings -> API** - copy the **Project URL** and the **`service_role`** key (not the
   public `anon` key - the app writes from the server and needs the key that bypasses Row Level Security).
5. Set these environment variables wherever the app runs (Render / Vercel / locally in a `.env` or
   your shell):

   | Variable          | Meaning                                          |
   | ----------------- | ------------------------------------------------ |
   | `SUPABASE_URL`    | Project URL, e.g. `https://xxxxx.supabase.co`     |
   | `SUPABASE_KEY`    | The `service_role` API key                        |
   | `SUPABASE_BUCKET` | Storage bucket name (optional, default `gstatus`) |

Until these are set, `/rank/admin` and `/students/admin` show a notice and nothing can be saved -
everything else in the portal (status pages, leaderboards, the ceremony *display* at `/rank`) keeps
working normally.

## 2. Run locally

```sh
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python app.py            # http://127.0.0.1:5000
```

Open `/admin`, paste the sheet link, save — it prints the detected columns and the
number of response rows.

## 3. Deploy on Render

1. Push this folder to GitHub.
2. In Render: **New → Blueprint**, pick the repo (`render.yaml` is included), or
   create a **Web Service** with:
   - Build: `pip install -r requirements.txt`
   - Start: `gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 60`
   - Health check path: `/healthz`
3. Environment variables:

| Variable            | Meaning                                      |
| ------------------- | -------------------------------------------- |
| `GOOGLE_SHEET_URL`  | Sheet link (recommended on Render)           |
| `GOOGLE_SHEET_NAME` | Tab name, e.g. `Form Responses 1` (optional) |
| `STUDENT_MASTER_URL` | Student Master sheet link (optional)         |
| `STUDENT_MASTER_NAME` | Student Master tab name (optional)          |
| `REFRESH_SECONDS`   | Auto-refresh interval, default `20`          |
| `ADMIN_TOKEN`       | Password required by `/admin` in production  |

Environment variables win over anything saved in `config.json` (Render's disk is
ephemeral, so prefer env vars there).

## 3b. Deploy on Vercel

`vercel.json` and `.vercelignore` are included; the Flask app (`app.py`) is deployed as one
Vercel Function and serves `templates/`, `static/` and `public/` itself.

1. Push this folder to GitHub and **Import** the repo in Vercel (or run `vercel` / `vercel --prod`
   with the Vercel CLI). No framework preset or build command is needed.
2. Add these under **Project → Settings → Environment Variables**, then redeploy:

| Variable              | Meaning                                               |
| --------------------- | ----------------------------------------------------- |
| `GOOGLE_SHEET_URL`    | Form Responses sheet link                             |
| `STUDENT_MASTER_URL`  | Student Master sheet link (needed for the hostel roll) |
| `GOOGLE_SHEET_NAME` / `STUDENT_MASTER_NAME` | Tab names (optional)             |
| `REFRESH_SECONDS`     | Auto-refresh interval, default `20`                   |
| `SUPABASE_URL` / `SUPABASE_KEY` / `SUPABASE_BUCKET` | Rank ceremony + student photo storage - see [1f](#1f-supabase-setup-required-for-photos--the-rank-ceremony) |

If they are not set, the links in `config.json` are used.

Vercel's filesystem is read-only, so `/admin` is **view-only** there (it still shows the detected
columns and student counts). Change settings through the environment variables above.
Each serverless instance keeps its own ~20 second in-memory cache of the sheets.

## 4. Using the portal

- `/` — search by Member ID
- `/G001` — that student's page (also `/?memberId=G001`)
- `/leaderboard` — room ranking + most disciplined
  `?view=hostel` / `?view=day` / `?view=gstar` / `?view=weekly` — all hostel / all day school / combined GStar / this week only
- `/rank` — the rank ceremony slideshow; `/rank/admin` — set it up
- `/students/admin` — upload/remove a photo per Member ID (reused by the rank ceremony)
- `📜 VIEW HISTORY` — half-screen drawer with all activities, filters by activity,
  date range and free-text search, plus earned / deducted / total points
- The page refreshes itself every 20 seconds and shows a note when a new activity
  arrives; `↻ Refresh` forces an immediate re-read
- `/api/student/G001` — JSON used by the live refresh
- `/healthz` — health check for Render

## Notes

- Unknown Member IDs get a friendly "not found" page, never a traceback.
- If the sheet is unreachable, the last known data stays on screen and the badge
  switches to `RETRYING`.
- Logo and styles live in `static/`; page markup in `templates/`.
