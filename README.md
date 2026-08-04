# GeoAttend — geo-verified student attendance

A complete Django web application for recording classroom attendance that **cannot be
marked from outside the room**. A teacher generates a time-limited attendance link; the
server stores the teacher's GPS fix and only accepts a student's mark if that student is
physically inside the geo-fence (50 m by default) while the link is still alive.

It also chases the students who stop turning up: staff can email students and WhatsApp
their guardians when attendance falls below a threshold they set at send time.

Stack: **Django 5 · SQLite/PostgreSQL · Bootstrap 5 · Font Awesome 6 · jQuery 3 · Chart.js 4**.
Every create/read/update/delete happens over **AJAX** — page reloads are avoided throughout.

---

## 1. Quick start

```bash
python -m venv .venv
# Windows:  .venv\Scripts\activate
source .venv/bin/activate

pip install -r requirements.txt
copy .env.example .env          # (cp .env.example .env on macOS/Linux) — optional

python manage.py migrate
python manage.py seed_demo      # optional: 100 students, ~180 classes of history
python manage.py runserver
```

Open <http://127.0.0.1:8000/>.

**Demo logins** (password `Passw0rd!23`) after `seed_demo`:

| Role | Email |
|---|---|
| Head of institute | `head@demo.edu` |
| Head of department | `hod.cse@demo.edu`, `hod.ece@demo.edu` |
| Teacher | `teacher1.cse@demo.edu` … `teacher3.ece@demo.edu` |
| Student | `cse2022001@demo.edu` … |

With the default `.env`, **emails are printed to the terminal** — OTP codes and invitation
links appear there, so you can complete every flow without an SMTP server.

> **Geolocation needs a secure context.** Browsers only expose GPS on `https://` or on
> `localhost`/`127.0.0.1`. To test on a phone, tunnel with `ngrok http 8000` (or similar)
> and set `SITE_URL` + `CSRF_TRUSTED_ORIGINS` to the https URL.

---

## 2. How the system works

### 2.1 Account creation — invitation only

Nobody can self-register except the head of the institute. Every other account must have
had its email added by someone above it; this is enforced in the backend, not the UI.

```
Head of institute            → registers the college, verified by a 6-digit email OTP
   └─ adds a Department + HoD email     → HoD receives an invitation link
        └─ HoD adds Subjects & Batches
        └─ HoD adds Teacher email + subject/batch allocation → teacher receives link
        └─ HoD imports the student roster (.xlsx/.csv)       → each student receives link
```

* The invitee opens the link and sets **only** their name, phone and password.
  Email, role and department are fixed by the inviter and cannot be changed.
* Invitations are single-use, expire after `INVITE_TTL_DAYS` (7), and can be resent or
  revoked from the UI.
* OTPs are stored **hashed**, expire after 10 minutes, and lock out after 5 wrong tries.

### 2.2 Taking attendance

1. Teacher opens **Take attendance**, picks a **batch** — the page AJAX-loads only the
   subjects that *this teacher* is assigned to teach *that batch*.
2. Teacher picks the subject, sets link validity (**default 5 minutes**), optionally the
   geo-fence radius and a room note, and hits **Generate**.
3. The browser supplies `latitude/longitude/accuracy`; the server stores them as the
   centre of the geo-fence and creates an `AttendanceSession` with a random 32-char token.
4. Every enrolled, activated student in that batch is emailed the link. A **QR code** is
   also rendered so the teacher can project it.
5. The teacher sees a **live board**: countdown, present/absent lists, percentage bar,
   refreshed by polling every 4 s. They can extend (+5 min), resend, close early, or mark
   a student present manually (recorded as `MANUAL` with an audit entry).

### 2.3 Marking attendance (student)

Tapping the link hits `/attendance/mark/<token>/`. If the student is not signed in, Django
bounces them to login with `?next=…` and returns them automatically afterwards — no
retyping the link. The page then requests GPS and posts it. The server checks, **in order**:

| # | Rule | Failure message |
|---|---|---|
| 1 | Account is a student with a profile | Only student accounts can mark attendance |
| 2 | Session is not cancelled | Cancelled by the teacher |
| 3 | `now < expires_at` | This attendance link has expired |
| 4 | Student's batch == session batch | This request is for batch X, not yours |
| 5 | Student is enrolled in the subject | You are not enrolled in DSA |
| 6 | Not already marked | Already marked |
| 7 | Device matches the bound device | Not the device registered to your account (see below) |
| 8 | Device not already used by another student this session | Another student used this device |
| 9 | Coordinates are valid | Could not read your location |
| 10 | GPS accuracy ≤ 100 m | Your GPS fix is too imprecise (±N m) |
| 11 | **Haversine distance ≤ radius** | **You are not present in the class — you appear to be N m away** |

#### Device binding, and unlinking a lost phone

The first device a student uses becomes *their* device, and everything else is refused
until staff release it. Two switches control how far that goes:

| Setting | Default | Blocks |
|---|---|---|
| `ATTENDANCE_ENFORCE_DEVICE_LOCK` | True | **Marking** attendance from another device |
| `ATTENDANCE_ENFORCE_LOGIN_DEVICE_LOCK` | True | **Signing in** from another device |

Both compare the same `User.device_id`, so a student has exactly one device, and both apply
to **students only** — a HoD works from a desktop, a laptop and a phone, and never marks
their own attendance.

> **What the signature actually proves: nothing, on its own.** It is computed *by the
> browser* (canvas render + screen geometry + timezone + language + user-agent) and posted
> to the server, so the server cannot distinguish a real phone from a forged value. What it
> gives you is **trust on first use**: whichever device is used first becomes the
> registered one, and a second device is refused. That defeats the ordinary case — handing
> your login to a friend so they can mark you present, since their phone hashes
> differently — but a student comfortable with browser dev tools can replay the value from
> their own phone. The **geo-fence is the control that actually requires a body in the
> room**; device binding is a deterrent layered under it, and `BLOCK_SHARED_DEVICE` catches
> the crudest attack of all (two students, one handset, one session).
>
> Note also that the signature is not perfectly stable: a browser update, a new external
> monitor or travelling across a timezone can change it and lock a student out. With the
> login lock on, that means no access at all until staff unlink them — which is why the
> setting exists. Turn it off and a stale signature only stops marking, not access.

When a phone is lost or replaced, **the head, the HoD, or any teacher who teaches that
student** releases the binding from *Manage → Students*: a **Device** column shows who is
linked, and the unlink button opens a short dialog with an optional reason. The next device
the student marks from becomes the new registered one.

A blocked sign-in is recorded as `LOGIN_DEVICE_BLOCKED` in the activity log with both
signatures, and the login page explains what to do rather than just failing.

Because releasing the binding is exactly what someone would do to enable proxy attendance,
the action is deliberately traceable: it is written to the activity log with the actor and
reason, and **the student is emailed** ("Didn't expect this? Contact your department
office"). A teacher can only unlink students they actually teach — the endpoint is scoped
by `students_qs_for`, so a teacher from another department gets a 404.

Students **cannot** unlink their own device by default. Self-service would make the
one-device rule meaningless — anyone could unlink, lend their login out, and re-bind
afterwards — so their profile page tells them who to ask instead. Set
`ALLOW_STUDENT_SELF_DEVICE_RESET=True` to restore the old self-service button.

Only then is an `AttendanceRecord` written (with distance, accuracy, IP, user-agent and
device fingerprint). **Every rejected attempt is stored** in `MarkAttempt` and shown to the
teacher under "Rejected attempts" — useful when investigating proxy attendance.

The student sees a green **"Attendance marked"** card with the subject, time and distance.

---

## 3. Dashboards & reports

All four roles get an interactive dashboard with sortable/searchable/paginated tables and
Chart.js graphs. Filters: **date range (default 1 Jan → today)**, department, batch,
subject, teacher, plus quick ranges (today / 7d / 30d / this month / this year).

**Head & HoD & Teacher** (`/app/` and `/app/reports/`)

* KPIs: overall %, classes conducted, students in scope, students below threshold.
* Daily attendance trend (line + classes bar, dual axis).
* Attendance-band doughnut (<40 %, 40–55 %, 55–70 %, 70–85 %, 85–100 %).
* Department comparison (head only) and batch comparison.
* Classes by hour of day.
* **Student-wise**: per-subject % *and* overall % for the range, per-subject chips,
  click through to a full student record (daily + cumulative trend, class history).
* **Subject-wise**: classes conducted, enrolled count, distinct students who attended,
  average present per class, attendance %, teachers.
* **Teacher activity**: classes conducted vs. average attendance achieved.
* **At-risk**: everyone below a configurable threshold, with their weakest subjects.
* CSV export of the student and subject reports, and of any single session.

**Student** (`/app/`, `/attendance/me/`)

* Overall %, classes held / attended / missed.
* Daily and cumulative trend lines; per-subject bar chart and table.
* Full class history with present/absent and the distance recorded.
* A green banner appears the moment one of their classes opens attendance.

---

## 4. Excel roster import

`Manage → Students → Import roster`. Download the generated template or use any sheet with
these headers (spelling is flexible — `Email`/`Email ID`/`e-mail` all work; extra columns
are ignored):

| name | email | batch | subjects enrolled | guardian mobile | mobile number *(opt.)* | guardian name *(opt.)* | roll number *(opt.)* |
|---|---|---|---|---|---|---|---|
| Ananya Sharma | ananya@… | 2022-26 | DSA, DBMS, AI | +919812345670 | 9876543210 | Mr. R. Sharma | CSE22001 |
| Rahul Verma | rahul@… | 2022-26 | DSA, DBMS, CNS | +919812345671 | 9876500011 | Mrs. S. Verma | CSE22002 |

**Guardian mobile is required** — it is the WhatsApp number that receives low-attendance
alerts. A blank or malformed number fails that row (the rest still import). Numbers are
normalised on the way in: spaces, dashes and brackets are stripped, `00` becomes `+`, and
a missing country code is added at send time from `WHATSAPP_DEFAULT_COUNTRY_CODE`.

* **Preview** runs the real importer inside a transaction that is rolled back, so you see
  exactly what would happen — row by row — before anything is written.
* Batches are created automatically from labels like `2022-26`. A row naming an
  **archived** batch is rejected — restore the batch first (see §6).
* Unknown subject codes, malformed batches, bad/duplicate emails are reported per row and
  skipped; valid rows still import.
* Re-importing the same file **updates** students and re-syncs their enrolments instead of
  creating duplicates.
* Every import is recorded in `ImportJob` with a full report (see *Import history*).
* **Export roster** downloads the current students in exactly the shape the importer
  accepts, so you can export → edit in Excel → re-import. It honours the batch and subject
  filters on screen. Re-importing an untouched export creates nothing and updates
  everything, so it is safe to use as a backup.
* The student list has a **Missing guardian no.** filter so you can find and fix the gaps
  before sending alerts.
* Every number in the **Mobile** and **Guardian (WhatsApp)** columns carries a one-tap
  **call** (`tel:`) and **WhatsApp** (`wa.me`) button, on both *Students* and a teacher's
  *My students*. Links are built server-side from the normalised `+E.164` number, so a
  roster entry stored as `98765 43210` still dials correctly; `wa.me` gets the same digits
  without the `+`. A number too malformed to link is shown in amber with the reason rather
  than as a dead link.

---

## 5. Low-attendance alerts

`Alerts` in the sidebar, open to **head, HoD and teacher** (each limited to their own
students). Three independent channels — pick any combination:

* **Email → the student**, using their login address.
* **WhatsApp → the student**, using their own `mobile` from the roster (falling back to
  the phone number they set on their profile).
* **WhatsApp → the guardian**, using `guardian_mobile` from the roster.

Each channel has its **own editable message**. The student WhatsApp default addresses them
directly ("Hi Ananya, your overall attendance is 61.3%…") while the guardian default is
written for a parent ("Your ward *Ananya Sharma* (CSE22022, 2022-26) has…") — sending both
does not send the same text twice.

### The flow

1. **Choose the scope.** *Overall attendance* uses each student's combined percentage;
   *One subject* uses that subject alone — pick it from the subjects you're allowed to
   report on.
2. **Set the threshold** at send time (50/60/65/75/85 shortcuts, or type any value), plus
   date range, department and batch.
3. **Find students.** The screen lists everyone below the line with their percentage,
   attended/held, and whether **each** number — the student's and the guardian's — is
   usable. Untick anyone you don't want to contact.
4. **Edit the message.** One tab per channel (*Student email*, *Student WhatsApp*,
   *Guardian WhatsApp*), each pre-filled with its own default for the chosen scope. Click
   any placeholder chip to insert it at your cursor. *Reset to default* undoes your edits.
5. **Preview** renders all three drafts against a real recipient — the exact email and both
   WhatsApp bubbles. Misspelled placeholders are listed as warnings rather than silently
   blanked, so `{{studnet_name}}` is caught before a parent sees it.
6. **Send.** A delivery report shows per-recipient status; everything is kept in
   *History*.

### WhatsApp templates (head of institute)

Because WhatsApp refuses business-initiated free-form text, the WhatsApp channels do not
use the editable message boxes — they use wording WhatsApp has approved in advance.

**`WhatsApp templates` in the sidebar, head only.** Write a template for students and one
for guardians (both boxes start pre-filled with the shipped defaults), pick UTILITY as the
category, and submit. In one action the app:

1. converts your `{{placeholder}}` names to WhatsApp's numbered slots —
   `Hi {{first_name}}, you are at {{percentage}}%` → `Hi {{1}}, you are at {{2}}%`,
   remembering which name owns which slot
2. `POST /v1/Content` to register the wording, returning a `HX…` content SID
3. `POST /v1/Content/{sid}/ApprovalRequests/whatsapp` to ask WhatsApp to review it

The list then shows each template's live status — **Received → Pending review → Approved**,
or **Rejected** with WhatsApp's reason. *Refresh statuses* polls
`GET /v1/Content/{sid}/ApprovalRequests` for everything still awaiting a verdict. A
submission that never reached Twilio shows as **Submission failed** and can be edited and
resubmitted; anything already at WhatsApp is locked, because altering approved wording
invalidates the approval.

You can register as many as you like — one per scope, per language, per tone.

Validation runs before anything leaves the building: unknown placeholders, bodies over
1024 characters, and templates that are *only* placeholders (WhatsApp rejects those) are
all refused with an explanation.

**In the alerts screen** the *Student WhatsApp* and *Guardian WhatsApp* tabs become
read-only pickers listing only **approved** templates, with the exact wording shown
beneath and the variable slots labelled. If no approved template exists for an audience,
that channel's checkbox is disabled and points at the templates page. Email keeps full
free-form editing throughout.

The server never trusts the picked id: it is re-checked against this institute's approved
list, for the right audience, on every send. A pending, rejected, deactivated,
wrong-audience or other-institute template returns 403. The campaign then records which
template went out **and copies its wording**, so the audit trail survives the template
later being edited or deleted.

With no Twilio credentials the whole workflow still runs — submissions are simulated and
auto-approve instantly, so you can rehearse it before you have an account.

### Placeholders

`{{student_name}}` `{{first_name}}` `{{roll_number}}` `{{batch}}` `{{department}}`
`{{institute}}` `{{guardian_name}}` `{{student_email}}` `{{student_mobile}}`
`{{percentage}}` `{{threshold}}`
`{{shortfall}}` `{{held}}` `{{attended}}` `{{missed}}` `{{subject_code}}`
`{{subject_name}}` `{{subject_list}}` `{{from_date}}` `{{to_date}}` `{{sender_name}}`
`{{sender_role}}`

Rendering is plain string substitution — deliberately **not** the Django template engine,
so a `{% raw %}{% ... %}{% endraw %}` tag typed into the textarea is sent as literal text
rather than executed.

### What the server guarantees

Per your choice, **edits apply to that send only** — the built-in defaults are never
overwritten, and every send starts from them.

The recipient list is **recomputed server-side at send time** from the same analytics the
dashboards use. The ticked ids can only *narrow* that list, never widen it: asking for a
student who is above the threshold, or outside your scope, changes nothing. Students with
no classes held in the range are never alerted (0/0 is not 0%).

Each channel skips independently and records why: un-activated accounts are skipped for
email, a missing or malformed **student** number skips only their WhatsApp, a missing
**guardian** number skips only the guardian's — the other channels still go out. One failed
send never aborts the rest of a campaign, and the delivery report counts
`student_whatsapp_sent` and `whatsapp_sent` separately so you can see which leg failed.

### WhatsApp delivery (Twilio)

`notifications/whatsapp.py` is plain functions — no classes to subclass, no backend
registry. The whole surface is:

```python
from notifications.whatsapp import send_whatsapp

send_whatsapp("+919812345670", "Your attendance is 61%.")
send_whatsapp(number, "", content_sid="HX…", content_variables={"1": "Ana", "2": "61.3"})
```

It returns a `Result` with `.ok`, `.provider_id` (Twilio message SID), `.status` and
`.error`. It never raises — a dead provider becomes a failed row in the delivery report,
not a 500.

With no credentials it runs in **console mode**: messages go to the server log and report
as sent, so the whole flow works before you have an account. To go live:

```env
TWILIO_ACCOUNT_SID=ACxxxxxxxx
TWILIO_AUTH_TOKEN=your-token
TWILIO_WHATSAPP_FROM=+14155238886      # Twilio's shared sandbox number
TWILIO_CONTENT_SID=HXxxxxxxxx          # see the warning below
```

> ### ⚠️ The 24-hour window will catch you out
>
> WhatsApp only accepts **free-form text** within 24 hours of the recipient messaging
> *you* first. A guardian who has never written to your number is outside that window, so a
> free-form alert to them is **rejected by WhatsApp** — Twilio error `63016` — even though
> your credentials are perfectly valid.
>
> Business-initiated notifications must use a **pre-approved Content Template**: build one
> in Twilio's Content Template Builder, wait for WhatsApp to approve it, then set
> `TWILIO_CONTENT_SID`. Every alert is then sent as that template.
>
> **The sandbox does not exempt you.** Twilio's docs are explicit: *"for business-initiated
> messages from the Sandbox, you can use only pre-approved templates."* Free-form appears
> to work there only because sending `join <code>` opens a 24-hour window — let that lapse
> and sandbox free-form fails exactly like production. The sandbox also ships just three
> fixed test templates (appointment / order / verification) and **will not accept custom
> ones**, so your alert template has to be registered against a real WhatsApp sender.
>
> Other sandbox limits worth knowing: only numbers that sent `join <code>` can receive
> anything (else error 63015), the join expires after **3 days**, and the shared sandbox
> number is throttled to **one message every 3 seconds** — a 500-student two-channel run
> would take ~50 minutes there regardless of how the code is written.
>
> The alerts page shows a red banner when Twilio is connected but no template SID is set,
> and error 63016 is translated into a message that names the setting to fix.
>
> This constrains the "edit the message before sending" feature: staff-authored free-form
> text only reaches people inside the window. With a Content Template, the wording is fixed
> at approval time and only the variables change. Decide which matters more before rolling
> out to guardians.

Other Twilio error codes (`20003` bad credentials, `63007` sender not WhatsApp-enabled,
`63015` not opted in, `63003` unreachable, `21211` invalid number) are translated into
plain English in the delivery report rather than surfaced as raw API text.

`WHATSAPP_ENABLED=False` stops all WhatsApp delivery instantly without touching code.

---

## 6. Archiving a batch

Setting a batch to **inactive** (Batches → the archive button) makes that cohort and
everything derived from it disappear from the entire application:

* student lists, roster exports and the student edit screen
* every batch/subject dropdown and report filter
* dashboard KPIs — student counts, classes conducted, overall percentage
* department student and batch counts; subject enrolment counts
* attendance sessions, their rosters, detail pages and CSV exports
* student-, subject-, batch-, department- and teacher-wise reports and trends
* low-attendance alert recipients, and the weekly `notify_low_attendance` command

**Nothing is deleted.** Restore the batch and every figure comes straight back — there is
a test asserting the KPI payload is byte-identical before archiving and after restoring.

Write paths refuse archived batches too: a teacher cannot open attendance for one
(`BATCH_ARCHIVED`), a student holding an old link cannot mark into one, a student cannot be
moved into one, a teacher cannot be allocated to one, and the importer rejects rows whose
batch is archived rather than silently creating invisible students.

The rule lives in `academics/selectors.py` — `batches_for()`, `students_qs_for()`,
`subjects_for()` and `enrolled_students()` all filter on `batch__is_active` — plus
`scoped_sessions()` in `dashboard/services.py` and `_visible_sessions()` in
`attendance/views.py`. Because every screen derives its data from those helpers, a new
screen cannot forget the rule.

**Two deliberate exceptions:**

*The Batches admin page* still lists archived batches — it passes
`batches_for(user, include_inactive=True)`, since that is where a batch is brought back.

*A student whose own batch is archived* is shown an explicit banner ("Batch 2019-23 has
been archived by your institute…") rather than a silent 0%, which would look like their
attendance had collapsed overnight.

---

## 7. Email — one function, three transports

**Every** outbound message in the project goes through a single function:

```python
from notifications.mailer import send_mail

send_mail(To="student@college.edu", Subject="Hello", Text="plain", HTML="<b>rich</b>")
```

Nothing else may touch `django.core.mail` — there's a test that walks the source tree and
fails if any module outside `mailer.py` imports it, so the rule can't quietly rot.

Behind that one door sit three transports. `EMAIL_PROVIDER` picks one:

| `EMAIL_PROVIDER` | Transport | Key |
|---|---|---|
| `sendgrid` | **SendGrid v3 REST API** — tracking, ganalytics, custom args, 1000-recipient batching. Key goes in an `Authorization: Bearer` header. | `SENDGRID_API_KEY` |
| `mailchimp` (or `mandrill`) | **Mailchimp Transactional**, the product formerly called Mandrill. `POST {MAILCHIMP_API_URL}/messages/send`. | `MAILCHIMP_API_KEY` |
| `django` *(default)* | **Django's `EMAIL_BACKEND`** — console in dev, locmem in tests | — |

Each provider has its own key setting, so both can stay configured and switching is a
one-word change. `settings.py` lowercases the name, accepts `mandrill` as an alias for
`mailchimp`, and raises `ImproperlyConfigured` on anything else — a typo must not quietly
degrade to the console backend, because a "successful" send that only printed to stdout is
worse than a loud failure. Same reasoning applies when the provider is set but its key is
blank: that is an error, not a silent fallback.

> **Mailchimp keys come in two kinds.** A key ending in `-us10` (or any `-usNN`) is a
> *Marketing* API key and will not authenticate against Transactional. Transactional keys
> start with `md-` and are issued under Transactional → Settings → API keys.

So the project still runs and tests still pass with no mail account at all; add a key and
everything switches over with no code change.

**Two things differ between the vendors**, both handled inside `mailer.py`:

- Mandrill authenticates with the key **in the JSON body** (`{"key": "..."}`), not a header.
- Mandrill has no separate `cc`/`bcc` fields or `reply_to` field. cc/bcc are entries in the
  same `to` array tagged `{"type": "cc"}`, and Reply-To is a raw header. Because cc/bcc share
  that array, they count against the 1000-recipient cap the same way they do on SendGrid.

Mandrill also answers `HTTP 200` with a per-recipient array even when a message was
*rejected* (bounced address, blacklist). `sent`, `queued` and `scheduled` count as success;
anything else is turned into a failed `MailResult` carrying the reject reason — so a hard
bounce does not read as a delivered alert.

### Signature

```python
send_mail(From=None, To=None, Subject="", Text=" ", HTML=" ",
          cc=None, bcc=None, Attachments=None, From_Name=None,
          reply_to=None, reply_to_list=None, uniqueID=None, messageGroup=None,
          Sandbox_Mode=None, utm_source="Sent Email", wait=False)
```

`From`, `From_Name` and `reply_to` default to `DEFAULT_FROM_EMAIL` / `EMAIL_SENDER_NAME`,
so most calls are just `To`, `Subject` and a body. `To`/`cc`/`bcc` accept a bare string, a
list of strings, SendGrid's `{"email", "name"}` dicts, or model instances with an `.email`.

### Async and results

`send_mail` returns a `Future` and sends on a **shared** thread pool, so a slow provider
never blocks a request — a teacher generating an attendance link doesn't wait on SMTP.

It resolves to a `MailResult`, which subclasses `str` (so printing/logging it works as
before) but carries `.ok`, `.status_code` and `.error`. That matters because **SendGrid
answers a success with `202` and an empty body** — the response text alone can't tell you
whether anything happened.

```python
result = send_mail(To=…, Subject=…, wait=True).result()   # or .result() on the future
if not result.ok:
    log.error(result.error)
```

The alert campaign uses `wait=True` precisely so its delivery report reflects what was
actually delivered rather than what was merely queued. `notify_session()` deliberately
does *not* wait.

### Templated mail

`send_template_mail(subject, to, "invitation", context)` renders `emails/invitation.html`
+ `.txt` and funnels into `send_mail`. This is what `accounts/emails.py` uses for OTPs,
invitations and welcome mails.

### Notes on the original implementation

The SendGrid payload, tracking settings, batching and attachment encoding are as supplied.
Four things were changed deliberately:

* **Mutable default arguments** (`cc=[]`, `Attachments=[{...}]`) became `None` sentinels.
  A default list is created once and shared by every call — a latent aliasing bug.
* **One shared thread pool** instead of a new `ThreadPoolExecutor` per call, and clamped to
  `max(1, …)`. `cpu_count() - 2` is **0 or negative on a 1–2 core box**, which raises
  `ValueError` outright; a 500-student alert run would also have created 500 pools.
* **`uniqueID` defaults to a fresh UUID** rather than a constant, so it can actually
  identify one message in a SendGrid event webhook. `messageGroup` remains the grouping
  field and defaults to `SITE_NAME`.
* **Failures return rather than raise**, and a rejected batch stops the run instead of
  hammering a failing API.

---

## 8. Project layout

```
config/         settings, root urls, wsgi/asgi
core/           shared helpers: haversine, JSON envelope, role decorators,
                middleware, management commands
accounts/       User (email login, 4 roles), Institute, EmailOTP, Invitation,
                ActivityLog, auth views, transactional email templates
academics/      Department, Batch, Subject, TeacherAssignment, StudentProfile,
                Enrollment, ImportJob, the xlsx/csv importer, row-level `selectors.py`
attendance/     AttendanceSession, AttendanceRecord, MarkAttempt,
                services.py  ← create_session() and mark_attendance() live here
dashboard/      filters.py + services.py (all analytics) + thin JSON views
notifications/  mailer.py — THE single email function (SendGrid + fallback);
                low-attendance alerts: AlertCampaign/AlertDelivery, the default
                message templates + placeholder engine, and whatsapp.py — the
                pluggable delivery backend you drop your API into
templates/      layouts, partials, per-app screens, HTML+text email templates
static/         app.css (design system), app.js (GA.* AJAX/table/chart/geo toolkit),
                reports.js (shared filter bar)
```

**Authorisation lives in one place.** `academics/selectors.py` derives every queryset a
user may see (`departments_for`, `subjects_for`, `students_qs_for`, …) and
`core/decorators.role_required` guards every view. A HoD literally cannot query another
department's rows, and each AJAX endpoint re-checks — the UI is never the gatekeeper.

### The jQuery toolkit (`static/js/app.js`)

`GA.get/post/submit` (auto CSRF, JSON envelope, error mapping onto form fields),
`GA.table()` (client-side sort/search/paginate/render), `GA.chart()` (styled Chart.js),
`GA.location()` (promise-based geolocation with human error messages),
`GA.deviceHash()`, `GA.countdown()`, `GA.confirm()`, `GA.toast()`, `GA.copy()`.

---

## 9. Configuration

Everything is environment-driven (see `.env.example`):

| Setting | Default | Meaning |
|---|---|---|
| `ATTENDANCE_DEFAULT_RADIUS_M` | 50 | Geo-fence radius |
| `ATTENDANCE_MIN/MAX_RADIUS_M` | 10 / 500 | Bounds a teacher may choose |
| `ATTENDANCE_DEFAULT_EXPIRY_MIN` | 5 | Default link validity |
| `ATTENDANCE_MIN/MAX_EXPIRY_MIN` | 1 / 180 | Bounds a teacher may choose |
| `ATTENDANCE_MAX_GPS_ACCURACY_M` | 100 | Reject fuzzier GPS fixes |
| `ATTENDANCE_ENFORCE_DEVICE_LOCK` | True | One device per student, for **marking** |
| `ATTENDANCE_ENFORCE_LOGIN_DEVICE_LOCK` | True | One device per student, for **signing in** |
| `ALLOW_STUDENT_SELF_DEVICE_RESET` | False | Let students unlink their own device; off so staff control it |
| `ATTENDANCE_BLOCK_SHARED_DEVICE` | True | Two students, one phone → blocked |
| `LOW_ATTENDANCE_THRESHOLD` | 75 | At-risk threshold, and the default in the alert screen |
| `EMAIL_PROVIDER` | `django` | `sendgrid` \| `mailchimp` \| `django`; unknown values raise at startup |
| `SENDGRID_API_KEY` | *(blank)* | Used when `EMAIL_PROVIDER=sendgrid` |
| `MAILCHIMP_API_KEY` | *(blank)* | Used when `EMAIL_PROVIDER=mailchimp`; must be a `md-…` **Transactional** key |
| `MAILCHIMP_API_URL` | `https://mandrillapp.com/api/1.0` | Mailchimp Transactional base URL |
| `EMAIL_TIMEOUT` | 20 | HTTP timeout for the Mailchimp transport |
| `EMAIL_SENDER_NAME` | `GeoAttend` | Display name on outgoing mail |
| `SENDGRID_SANDBOX_MODE` | False | SendGrid validates but never delivers |
| `EMAIL_ASYNC` | True | Send off the request thread |
| `EMAIL_MAX_WORKERS` | 0 (auto) | Mail thread-pool size |
| `WHATSAPP_ENABLED` | True | Kill switch — blocks all WhatsApp messaging |
| `WHATSAPP_DEFAULT_COUNTRY_CODE` | 91 | Prepended to local-format student *and* guardian numbers |
| `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` | *(blank)* | Blank = console mode |
| `TWILIO_WHATSAPP_FROM` | *(blank)* | Your WhatsApp-enabled Twilio sender |
| `TWILIO_CONTENT_SID` | *(blank)* | Fallback template SID; normally unused now that templates are managed in-app |
| `TWILIO_STATUS_CALLBACK` | *(blank)* | Webhook Twilio calls as delivery status changes |
| `OTP_TTL_MINUTES` / `OTP_MAX_ATTEMPTS` | 10 / 5 | OTP policy |
| `INVITE_TTL_DAYS` | 7 | Invitation link lifetime |
| `DB_ENGINE`/`DB_NAME`/… | *(blank → SQLite)* | Set to `django.db.backends.postgresql` for Postgres |
| `EMAIL_BACKEND` | console | Swap for `django.core.mail.backends.smtp.EmailBackend` |

When `DEBUG=False`, secure cookies, HSTS, SSL redirect and the proxy SSL header switch on
automatically.

### Management commands

```bash
python manage.py seed_demo [--reset] [--days 90]      # demo data
python manage.py create_head --name … --code … …      # bootstrap without OTP
python manage.py close_expired_sessions               # housekeeping (cron)
python manage.py notify_low_attendance --dry-run      # weekly student alerts (cron)
python manage.py prune_invitations --days 30          # delete dead links (cron)
```

#### Pruning expired invitation links

Expiry alone never deletes anything — a stale link simply stops working (the accept
endpoint returns **410 Gone**), and the row stays for the audit trail. `prune_invitations`
does the actual clean-up:

```bash
python manage.py prune_invitations --days 30 --dry-run      # preview first
python manage.py prune_invitations --days 30                # apply
python manage.py prune_invitations --days 90 --include-revoked --purge-users --yes
```

| Flag | Effect |
|---|---|
| `--days N` | Delete invitations that expired **more than N days ago** (default 30) |
| `--dry-run` | Print what would go, write nothing |
| `-v 2` | List every affected email with its age |
| `--institute CODE` | Limit to one institute |
| `--include-revoked` | Also delete manually revoked invitations |
| `--purge-users` | Also delete the never-activated accounts left behind |
| `--otps` | Also delete `EmailOTP` rows older than the cutoff |
| `--yes` | Skip the confirmation prompt for `--purge-users` |

It also flags any `PENDING` row whose expiry has passed as `EXPIRED` — the app only does
that lazily, when somebody actually opens a dead link.

**`ACCEPTED` invitations are never deleted**, whatever the age: they record who joined and
when. `--purge-users` is deliberately conservative and refuses any account that has
attendance records, subject enrolments, conducted classes, teaching allocations, a
department-head seat, or a newer pending invitation — those must be re-invited (which
reuses the same user and keeps their data) rather than deleted. It skips activated
accounts entirely. The command is idempotent and safe to run on a schedule.

---

## 10. Tests

```bash
python manage.py test           # 239 tests, ~7 seconds
```

Covered: haversine maths and batch parsing; institute signup with a *wrong* OTP then the
right one; invitation acceptance, reuse and revocation; the `prune_invitations` guards;
login edge cases; the importer (happy path, unknown subjects, bad batches, missing
columns, **required/malformed guardian mobile**, idempotent re-import); department
scoping; session creation rules; **every geo-fence rejection path**; device binding and
shared-device blocking; manual mark/unmark; close/extend; cross-teacher isolation; CSV
export; and a check that the percentages a student sees match what staff see for them.

For alerts specifically: placeholder substitution including typos and the "no template
tags are evaluated" guarantee; phone normalisation across local/`+`/`00` formats; the
Twilio transport with the network mocked (free-form vs Content Template payloads, the
`whatsapp:` prefix not being doubled, status callbacks, console mode, the kill switch, and
that error 63016 and auth failures become actionable messages rather than exceptions); recipient
selection by threshold and by subject; that a selection cannot widen the list; skip
reasons for un-activated students and missing student/guardian numbers; that the student
channel resolves the roster mobile then falls back to the profile phone; that the student
and guardian bodies differ; that all three channels can run at once (2 students × 3
channels = 6 delivery rows); that one channel failing doesn't stop the others and each has
its own failure counter; and role/institute scoping of the alert screens and history.

Device binding has its own file, `academics/test_device_unlink.py` (23 tests): that all
three staff roles can unlink but a teacher from another department cannot; that students
can't unlink their own or each other's; that the setting re-enables self-service while
staff keep their own reset; that the action is logged with actor and reason and the student
is emailed (but never a student who hasn't activated yet); and an end-to-end check that a
new phone is refused with `DEVICE_MISMATCH`, then works and rebinds once staff release it.
For the login lock: that the first sign-in binds, the same device is let back in, a
different device (or a different browser on the same phone) is refused with no session
created, staff are never locked, activating an invitation binds that device, unlinking lets
the new phone in, and the setting switches the whole thing off.

WhatsApp templates get `notifications/test_templates.py` (32 tests): placeholder→slot
conversion including a repeated placeholder reusing its slot and every shipped default
converting cleanly; the validation guards; the exact Content API payloads with the network
mocked; a Twilio error leaving a retryable failure; status sync for approved, rejected and
unrecognised verdicts; head-only permissions and institute scoping; and the approved-only
rule — pending, rejected, wrong-audience, deactivated and cross-institute templates all
rejected, plus proof the send uses `content_sid` rather than free-form text.

Archived batches get a dedicated file, `academics/test_archived_batches.py` (29 tests): it
walks every selector, every analytics function, every JSON endpoint and every write path to
prove an archived cohort leaks nowhere, that restoring is exact, and that nothing was
deleted along the way.

The mailer has its own coverage: SendGrid payload shape, 1000-recipient batching (and how
cc/bcc shrink each batch), HTTP errors and network exceptions becoming results rather than
exceptions, base64 attachments, the Django fallback, `MailResult` string behaviour, unique
message ids, the shared pool never being built with zero workers, mutable-default
isolation, and the tree-walk that proves nothing bypasses `mailer.py`.

Tests run with MD5 password hashing, `EMAIL_ASYNC=False`, `EMAIL_PROVIDER=django` and an
in-memory mail backend (see the `"test" in
sys.argv` block in settings), which is what keeps 100 tests under two seconds.

---

## 11. Production notes

* Set `DJANGO_SECRET_KEY`, `DJANGO_DEBUG=False`, `DJANGO_ALLOWED_HOSTS`, `SITE_URL`.
* Serve over **HTTPS** — geolocation will not work otherwise.
* Switch to PostgreSQL (`DB_ENGINE=django.db.backends.postgresql`) and a real SMTP host.
* `python manage.py collectstatic`, then serve behind gunicorn + nginx (or WhiteNoise).
* Schedule `close_expired_sessions` every 5 minutes and `notify_low_attendance` weekly.
* Consider moving `notify_session()` and `send_campaign()` onto a queue (Celery/RQ) once
  volumes grow — both currently send inline, so a large alert run holds the request open.
  The alert screen caps a single send at 500 students for this reason.
* Set `WHATSAPP_ENABLED=False` to stop all guardian messaging instantly without touching
  code — useful during exam season or while switching providers.
