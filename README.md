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

## 1a. Two response tabs in one spreadsheet (Form Responses 1 + School)

The portal can read **two** response tabs and add them together, so both forms count towards the
same students (a student's points, history, room ranking, GStar, weekly report and ceremonies all
include both tabs).

- **Tab 1** - `Google Sheet link` on `/admin` (here: *Form Responses 1*, the older
  `Name (Hostel) | Name (DAY SCHOOL)` layout).
- **Tab 2** - `Second responses tab link` + `Second tab name` on `/admin`
  (here: the *School* tab, `Timestamp | Name | Activities (Positive) | Activities (Negative)` with
  `ID | NAME | CLASS | SECTION | ROOM | CUPBOARD` names). Both are already filled in `config.json`.
- Paste the tab's own link (the one ending in `#gid=533124421`) and/or its exact tab name. The second tab
  never falls back to "the first tab", so a wrong link can't double-count tab 1.
- An empty tab simply adds nothing. If tab 2 can't be read, tab 1 keeps working and `/admin` shows the reason.
- On Render / Vercel use the environment variables `GOOGLE_SHEET_URL_2` and `GOOGLE_SHEET_NAME_2`.

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
- **Single Name column (current form):** `Timestamp | Name | Activities (Positive) | Activities (Negative)`,
  where every student is one dropdown entry `ID | NAME | CLASS | SECTION | ROOM | CUPBOARD`:
  - Hostel: `AH0879 | OMKAR SANDIPKUMAR CHEDAGE | VII | AQUA | 202 | 4` → ID `AH0879` · class `VII-AQUA` · room **202** · cupboard **4**
  - Day school: `AG6403 | PRATEEK PAL | VII | AQUA |  |` → room and cupboard blank, so the student counts as day school
  The same `|` cells also work in the Student Master (one column, with or without a header row).
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
- **Weekly ceremony (`/rank/weekly`, setup at `/rank/weekly/admin`):** a second ceremony with its own names and photo
  slots, so it never touches the main `/rank`. Its **Fill from leaderboard** defaults to *This week's points*
  (Monday-Sunday, hostel + day school together; only students with points above 0 are picked, and it resets
  every Monday). Photos come from the student photo library.
- Replace `static/rank/bg.png`, `suspense.mp3` or `fanfare.mp3` to change the look or music.
- **Built-in photos:** put `rank1.png`, `rank2.png`, `rank3.png` (or `.jpg` / `.webp` / `.gif`) in `static/rank/default/`.
  The first time the ceremony is opened, every rank that has no photo yet gets the one from that folder.
  Uploading in `/rank/admin` replaces it.
- **Storage:** names, settings and the photos themselves are saved in **Supabase** (see below) - not on the
  server's own disk. Everyone who opens the server sees the same ceremony, and it survives restarts and
  multiple server instances (Render free plan, Vercel). `ADMIN_TOKEN`, if set, is required to save.

## 1d-2. Room ceremony (`/rank/rooms`)

A second full-screen ceremony, in the same red-curtain style, built **live from the leaderboard** -
there is nothing to set up or type in.

1. **Room ranking** - every room is revealed from last place up to 1st. Rooms in 4th place and below
   flip in straight away; the 3rd, 2nd and 1st rooms get the typed label with the suspense music, then
   the reveal with the fanfare and confetti. A slide with the final room standings follows.
2. **Star students** - for every room, its **3rd, 2nd and 1st** students are revealed the same way
   (photo from the [student photo library](#1e-student-photo-library-supabase), initials if there is none),
   followed by that room's podium. Rooms go from the lowest-ranked to the champion room, so the best comes last.
3. **Finale** - the champion room and the 1st student of every room.

- Rooms are ranked exactly like the *Room ranking* tab: average per student when the Student Master has
  rooms, otherwise total. Force one with `/rank/rooms?rank=avg` or `/rank/rooms?rank=total`
  (the **🎬 Start room ceremony** button on the Room ranking tab passes the current choice).
- `/rank/rooms?order=rank` shows the champion room's students first instead of last.
- Only students with points above 0 can stand on a room's podium. Ties go to more good entries, then fewer
  deductions, then name. A room with no such students shows a short "no points yet" slide.
- Results are read once when the page opens, so they can't change mid-ceremony; reload the page for fresh numbers.
- Controls are the same as `/rank`: click / `->` / `Space` / `Enter` = next, `<-` = back, `F` fullscreen, `M` mute.
- JSON behind it: `/api/rank/rooms`.
- **Photos - `/rank/rooms/admin`** (or ⚙ on the ceremony, or **📷 Ceremony photos** on the Room ranking tab):
  lists every room in the ceremony with its 1st / 2nd / 3rd students. Upload a **room photo** (a group photo works well;
  without one the room number is shown) and student photos, all in one **SAVE PHOTOS**. Tick *Remove this photo* to clear one.
  Room photos are stored in Supabase under `rooms/`; student photos go into the student photo library.

## 1d-3. GStar Top 10 ceremony (`/rank/gstar`)

A star ceremony for the **top 10 of the GStar tab** (hostel + day school together), live from the
leaderboard - nothing to set up. Same curtain, music and controls as `/rank`.

1. **Our Stars 10-4** - ranks 10 down to 4 are revealed one click each (photo flip, a little confetti, no drumroll),
   then a slide with all of them together.
2. **The Top 3** - 3rd, 2nd and 1st get the typed label with the suspense music, then the reveal with the fanfare.
3. **Finale** - the top 3 on the podium with ranks 4-10 below.

- Order is exactly the GStar tab's order, and only students with points above 0 are included.
- Photos come from the [student photo library](#1e-student-photo-library-supabase); initials are shown when there is none.
- `/rank/gstar?top=5` (any number from 3 to 20) changes how many stars are revealed.
- `/rank/gstar?source=weekly` runs the same ceremony from the **Weekly** tab (this Monday-Sunday's points).
- **Photos - `/rank/gstar/admin`** (`?source=weekly` for the weekly one; or ⚙ on the ceremony, or **📷 Top 10 photos** on the GStar tab):
  lists the current Top 10 with a photo slot each - choose photos and press **SAVE PHOTOS** once. They go into the
  student photo library, so they also show on GStar, the student's page and the other ceremonies.
- Buttons: **🎬 Start Top 10 ceremony** on the GStar tab, **🎬 Weekly Top 10 ceremony** on the Weekly tab,
  and **🎬 GStar Top 10** in the top menu. JSON: `/api/rank/gstar`.

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
| `GOOGLE_SHEET_URL_2` / `GOOGLE_SHEET_NAME_2` | Second responses tab, e.g. `School` (optional) |
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
| `GOOGLE_SHEET_URL_2` / `GOOGLE_SHEET_NAME_2` | Second responses tab, e.g. `School` (optional) |
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
- `/rank/gstar` — GStar Top 10 star ceremony (`?source=weekly` for this week, `?top=5` for another size)
- `/rank/rooms` — room ranking ceremony + 1st/2nd/3rd student of every room (live from the leaderboard)
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
