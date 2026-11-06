
# SCHOOLCORD

Multi-tenant school management SaaS.

> **On the name.** This was built under two earlier names — "Dependable" (an
> internal codename) and "Fulfilled Lite" (a working title). Both are gone from
> everything a user or an admin sees. The product is **SCHOOLCORD**, and the
> name now lives in exactly one file: `apps/core/branding.py`. A handful of
> internal identifiers still carry the codename — the Postgres database name,
> a contextvar, and `DependableUserManager`, which is serialised into
> `accounts/0001_initial.py` — and are left alone deliberately: nobody sees
> them, and rewriting an applied migration to rename a manager class buys
> nothing. See [Branding and identity](#branding-and-identity).

- **Step 1 — foundation:** tenancy, a custom user model, the query-scoping
  pattern, the design-system shell, and the admin.
- **Step 2 — academic setup:** classes and subjects per branch, with management
  screens.
- **Step 3 — fee structures:** terms, and what each class owes per term.
- **Step 4 — student records:** the roster, entered by hand or imported from a
  spreadsheet, with each student's fee position derived from their class.
- **Step 5 — the public side:** landing page, signup, login with a per-role
  landing, password reset, and the onboarding checklist that ties the four
  setup steps together.
- **Step 6 — parent messaging:** SMS/WhatsApp to parents, each school sending
  under its own registered Sender ID.
- **Step 7 — payments:** money recorded against a student, with every balance
  derived from confirmed payments.

## Documentation

| Document | What it covers |
| --- | --- |
| This README, from here to [Tests](#tests) | Installing, running, testing and maintaining it locally |
| [docs/TECHNICAL.md](docs/TECHNICAL.md) | Architecture, apps and models, frontend, database and multi-tenancy, authentication and roles, Railway deployment, **every environment variable**, third-party integrations, management commands, scheduled jobs, runbook, known gaps |
| [docs/BACKUP_AND_RESTORE.md](docs/BACKUP_AND_RESTORE.md) | Full `pg_dump` of the Supabase database, restoring into a fresh Postgres or Supabase project, and moving the storage bucket |
| The rest of this README, from [How the tenancy works](#how-the-tenancy-works) on | Design notes: why each feature works the way it does |

## Running it

### Prerequisites

| Tool | Version | Needed for |
| --- | --- | --- |
| Python | **3.11** (pinned in `.python-version` and `runtime.txt`) | Everything |
| Git | any | Cloning |
| PostgreSQL | 15+ | Optional. Without it the project runs on SQLite. |
| Node.js | 20+ | Only to rebuild the CSS. The compiled stylesheet is committed. |

No credentials are needed to run it locally: with an unedited `.env`, mail
prints to the terminal, SMS is logged rather than sent, and uploads go to
`./media`.

### Install

```bash
git clone https://github.com/ayebasuokante669-cpu/dependable.git schoolcord
cd schoolcord

python -m venv venv
venv\Scripts\activate            # macOS/Linux: source venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt

copy .env.example .env           # macOS/Linux: cp .env.example .env
```

### Configure `.env`

`.env` is gitignored and read on every start. For local work, edit three things
in the copy you just made:

1. **Pick a database.** The example's `DATABASE_URL` points at a local Postgres
   called `dependable`. Either create it
   (`createdb -U postgres dependable`), or add `DB_ENGINE=sqlite` and use SQLite.
   If Postgres is configured but not running, dev settings fall back to SQLite
   and print a warning.
2. **Never point a local `.env` at the production database.** `migrate`,
   `runserver` and the demo-data commands would write to the live site. If you
   need production data locally, restore a backup into a local Postgres
   ([backup guide](docs/BACKUP_AND_RESTORE.md)).
3. **`PLATFORM_OWNER_EMAIL`** can stay as it is locally.

Every variable, what it does and its default:
[docs/TECHNICAL.md → Environment variables](docs/TECHNICAL.md#9-environment-variables--complete-reference).

### Create the tables and run

```bash
python manage.py migrate
python manage.py runserver
```

Then open http://127.0.0.1:8000/. Sign up a school at `/signup/`, or load the
demo data below. `manage.py` uses `config.settings.dev` unless told otherwise:
`DEBUG` on, any host allowed, email printed to the console.

To get into `/admin/` with a fresh database:
`python manage.py createsuperuser` (a superuser is treated as a platform owner).

### Local demo data

> **Local only.** Everything in this section creates invented schools, students,
> fees and payments, and `bootstrap_tenant` creates a **superuser**. Run it
> against your own SQLite database, never a shared or production one. Real
> accounts are made with the commands in
> [Creating real accounts](#creating-real-accounts).

```bash
python manage.py bootstrap_tenant --password "<a local password>"   # Demo School + one user per role
python manage.py seed_academics --create-school       # a demo "Fulfilled Academy" / Main Campus
python manage.py seed_fees                            # First Term 2025/2026 pricing
python manage.py seed_students                        # 60 invented students across 9 classes
```

`bootstrap_tenant` creates a school, a branch, and four accounts, all with the
password you pass as `--password` (always pass one):

| Username                  | Role                      | Sees                       |
| ------------------------- | ------------------------- | -------------------------- |
| `platform.owner`          | Platform Owner            | every school               |
| `demo-school.owner`       | School Owner              | one school, all branches   |
| `demo-school.principal`   | Principal / Branch Admin  | one branch                 |
| `demo-school.bursar`      | Bursar                    | one branch                 |

Run it again with `--name "Rival College"` to create a second tenant and watch
the isolation hold.

### CSS

> `static/css/vendor/` and `static/js/vendor/` hold Driver.js's built files, copied
> from `node_modules` and committed. They are not part of the Tailwind build —
> `driver.css` is loaded on its own and every one of its selectors is `.driver-*`
> prefixed, so it resets nothing and collides with nothing of ours. The overrides
> that put it on the design system are in `assets/app.css`.


The compiled stylesheet is committed, so the project runs without Node. To
change the design tokens, edit `assets/app.css` and rebuild:

```bash
npm install
npm run build:css     # or: npm run watch:css
```

### Database

Postgres is the target; SQLite is the local fallback.

- **Neither `DATABASE_URL` nor `DB_NAME` set** → SQLite.
- **Postgres configured but unreachable** → SQLite, with a warning on stderr.
- **`DB_ENGINE=postgres`** → Postgres required, no fallback (what `config.settings.prod` uses).
- **`DB_ENGINE=sqlite`** → SQLite always.

Connection poolers (Supabase, PgBouncer) are detected by host or port `6543` and
get `DISABLE_SERVER_SIDE_CURSORS = True`, because transaction-mode pooling hands
each statement a different backend and breaks Django's server-side cursors.
Override the detection with `DB_POOLED=1` / `DB_POOLED=0`.

Settings live in `config/settings/`: `base.py`, then `dev.py` (the default) and
`prod.py`. `manage.py` defaults to `config.settings.dev`; `wsgi.py`/`asgi.py`
default to `config.settings.prod`.

### Uploaded files

School logos and payment receipts go to an **S3-compatible bucket**, not to the
container's disk. Static files are untouched and stay on whitenoise.

That split is not a preference. Uploads on local disk failed twice over, and both
failures were silent:

- **nothing served them.** `config/urls.py` routes `/media/` only when `DEBUG` is
  on, because Django refuses to serve uploads in production and whitenoise covers
  `static/` by design. So in production every uploaded file 404'd — which is why a
  logo looked right on a developer's machine and was missing from the live platform
  screens. The templates were never wrong;
- **nothing kept them.** Railway's container filesystem is ephemeral. Even with a
  route, every logo and receipt vanished on the next deploy.

One variable decides which backend is used, so a half-configured deployment cannot
end up writing to a bucket it has no credentials for:

```python
AWS_STORAGE_BUCKET_NAME = os.environ.get("MEDIA_BUCKET_NAME", "").strip()
USE_OBJECT_STORAGE = bool(AWS_STORAGE_BUCKET_NAME)
```

Leave it unset and uploads write to `./media` as before — right for a developer's
machine and for the test suite, and it means **nobody needs credentials to run this
project**.

#### What to set in Railway

| Variable | Value |
| --- | --- |
| `MEDIA_BUCKET_NAME` | `media` |
| `MEDIA_S3_ENDPOINT_URL` | `https://<project-ref>.supabase.co/storage/v1/s3` |
| `MEDIA_ACCESS_KEY_ID` | from Supabase → Project Settings → Storage → S3 access keys |
| `MEDIA_SECRET_ACCESS_KEY` | the secret from that same pair |
| `MEDIA_S3_REGION` | `us-east-1` (Supabase ignores it; boto3 insists on one) |
| `MEDIA_PRIVATE_BUCKET` | `true` (the default; set it anyway so nobody flips it by accident) |

In Supabase first: **Storage → New bucket**, named `media`, **not** public.

#### The bucket is private, and the app hands out every file

The bucket holds children's birth certificates and parents' bank slips. It used
to be public, and the links were guessable:
`…/object/public/media/admissions/<school-id>/ENQ-2026-0043/…`. Now nothing in
the app links to a storage URL. Every file is read with the app's own S3
credentials and streamed by a view that decides who may have it:

| File | URL | Who gets it |
| --- | --- | --- |
| School logo | `/schools/<slug>/logo/?v=…` | Anyone. It is on the school's public enquiry page. Cached for a year per version. |
| Payment receipt | `/payments/<id>/receipt/file/` | Signed-in accounts with the payments capability, at that school |
| Applicant document | `/admissions/<id>/documents/<kind>/` | Signed-in accounts that can read the pipeline, at that school and campus |

The shared part, including the `nosniff` and sandboxing headers that stop an SVG
logo from running script, is in `apps/core/files.py`. Since nothing depends on
signed URLs, it does not matter whether a given S3-compatible host supports
them; `MEDIA_PRIVATE_BUCKET` now only changes the file links in `/admin/`.

`MEDIA_PUBLIC_BASE_URL` applies only to a deliberately public bucket
(`MEDIA_PRIVATE_BUCKET=false`); leave it unset. `MEDIA_PREFIX` puts uploads under
a prefix so the bucket can be shared.

#### Every one of them is read at runtime, never at build time

`MEDIA_BUCKET_NAME` is read once, in `config/settings/base.py`, with
`os.environ.get(..., "")` — so an absent variable is not an error, it is the
filesystem backend. Nothing in `railpack.json`, `nixpacks.toml` or the `Procfile`
asks for it: the build installs dependencies and nothing else, and `collectstatic`
runs in the start command rather than at build time (see the `Procfile`'s own note
on why).

That matters because of how it fails. If a Railway build stops with **`secret
MEDIA_BUCKET_NAME not found`**, nothing in this repository caused it and nothing
here can fix it — the builder is handed the service's variables, and one of them
cannot be resolved. In the Railway dashboard, under the service's **Variables**,
look for `MEDIA_*` entries whose value is a *reference* (`${{shared.…}}`,
`${{SomeService.…}}`) pointing at a shared variable or service that does not exist
in that environment. Replace the reference with the literal value from the table
above, or delete the entry and re-add it. A variable that is simply unset is
harmless; a variable that is set to an unresolvable reference is not.

#### Three settings that are not boto3's defaults

Each one is a thing Supabase does differently, and each one fails confusingly if
left alone:

- `AWS_S3_ADDRESSING_STYLE = "path"` — boto3 prefers virtual-host URLs
  (`https://bucket.endpoint/key`), which is not how Supabase routes;
- `AWS_DEFAULT_ACL = None` — Supabase rejects a request carrying an ACL header,
  and bucket-level public/private is how it decides access anyway;
- `AWS_S3_FILE_OVERWRITE = False` — two schools uploading `logo.png` must not
  become one school's logo on both of their receipts.

#### Migrating what is already there

Nothing to migrate: every file previously written to the container is already gone.
A school that uploaded a logo before this change needs to upload it again, and the
fallback initial has been showing in the meantime rather than a broken image.

## Maintaining it

| Task | How |
| --- | --- |
| Change a model | Edit it, then `python manage.py makemigrations <app>`, review the generated file, commit it with the model change. Production applies it on the next deploy, because the `Procfile` runs `migrate` before starting. |
| Add a tenant-owned model | Subclass `TenantScopedModel` or `BranchScopedModel` — see [Adding a tenant-scoped model](#adding-a-tenant-scoped-model). |
| Change styles | Edit `assets/app.css`, `npm run build:css`, commit **both** `assets/app.css` and `static/css/app.css`. Railway does not build CSS. |
| Upgrade a dependency | Change its pin in `requirements.txt`, `pip install -r requirements.txt`, run the full test suite, deploy. Django should stay on a supported release (5.2 LTS is supported until April 2028). |
| Build work that is not ready to go live | Target `integration/pre-launch`, not `main`, which is what Railway normally deploys from. |
| Deploy | Merge to the branch Railway deploys from. Details and the production variables: [docs/TECHNICAL.md → Deployment](docs/TECHNICAL.md#8-deployment-on-railway). |
| Check a deployment | `python manage.py check --deploy --settings=config.settings.prod` and `python manage.py mailcheck --settings=config.settings.prod` in a Railway shell. |
| Run a command on production | Always add `--settings=config.settings.prod` (or set `DJANGO_SETTINGS_MODULE` in Railway). Never run `bootstrap_tenant` or any `seed_*` command there. |
| Nightly admissions jobs | `send_enquiry_reminders` then `expire_enquiries` — needs a Railway cron service, see [docs/TECHNICAL.md → Scheduled jobs](docs/TECHNICAL.md#12-scheduled-jobs). |
| Back up / restore | [docs/BACKUP_AND_RESTORE.md](docs/BACKUP_AND_RESTORE.md). Take one before any risky migration or before deleting a school. |
| Change the product name, contact addresses, social links | `apps/core/branding.py` only. |

## Tests

```bash
# bash
DB_ENGINE=sqlite python manage.py test apps

# PowerShell
$env:DB_ENGINE = "sqlite"; python manage.py test apps
```

**Always set `DB_ENGINE=sqlite`.** Without it the test runner uses whatever
`DATABASE_URL` your `.env` holds, and creates and drops a test database on that
server. Run the suite **serially**: `--parallel` has exhausted memory on the
development machine and leaves `default_*.sqlite3` files behind when killed. A
full serial run takes around ten minutes, so while working run only the apps
you touched:

```bash
DB_ENGINE=sqlite python manage.py test apps.payments apps.core
DB_ENGINE=sqlite python manage.py test apps.core.tests_auth      # one module
```

CI runs on every pull request (`.github/workflows/ci.yml`): this suite on Postgres
with a coverage floor, the browser tests, dependency and secret scans, and the
colour-contrast check.

The browser tests in `e2e/` sign in as each role at desk and phone size. They need
`requirements-dev.txt` and a browser, and are separate from the suite above:

```bash
pip install -r requirements-dev.txt
playwright install chromium
python manage.py test e2e
```

The suite has roughly 1,600 test methods (`grep -r "def test_" apps | wc -l`).
The first of them cover the parts that must never regress:
what each role can see, what each role may change,
that a fee total always equals its live line items,
that a student's expected fee is always read from their class rather than stored
on them, that each senior arm carries exactly the subjects the school named,
that a spreadsheet import reports every bad row, imports the good ones, and
writes all-or-nothing, and that a signup builds exactly one school, branch and
owner while each role lands on its own dashboard.

Admissions adds 106 of its own: that a public enquiry writes to the school in
its URL and refuses another school's branch or class, that an enquiry becomes an
application without a single field being re-asked, that KG skips the entrance
exam while Primary and Senior sit it, that enrolment produces a Student in the
right class and branch and keeps the link back, that admission fees and termly
fees never touch, that a lapsed enquiry is closed rather than deleted, and that
one school cannot see another's applicants.

The `fix/bugs3` batch adds 55: that no onboarding step points at the Django admin
and that the owner can follow every one of them to a 200, that only the proprietor
and the platform may invite and that neither "School Owner" nor "Platform Owner"
can be assigned — from the dropdown or by a hand-made POST — that the platform's
Dashboard and Schools are two screens with exactly one active row between them, that
an uploaded logo renders on all five screens that reference it, that uploads default
to the filesystem while static files stay on whitenoise, that each deployment check
fires only when it should, that `mailcheck` reports and sends and fails loudly when
the welcome would be skipped, and that the date picker reaches every date field,
leaves the real input alone and bows out on touch.

Closing a school from the admin adds 24, and the ones that matter are the
negatives: that every `PROTECT` is still a `PROTECT` -- a class with students on
it, a student with payments, a term with payments and a plain `School.delete()`
are all still refused -- that a teardown which cannot finish leaves the school
and its payments exactly as they were, that one school's teardown cannot reach
another's rows, that a staff account without delete permission on payments is
refused the page *and* the POST, and that a school owner cannot reach the delete
view at all. Writing them turned up a landmine: the tenancy suite registers a
stand-in model with a `school` foreign key and used to create its table for the
length of one test case, so any `School` cascade elsewhere in the suite walked
into a table that did not exist. Nothing had ever deleted a school, so nothing
had found out.

Extending the import to classes, subjects and fee structures adds 124, and the
three that matter most are structural: that every column key on every sheet is a
field on the model it feeds (the check that caught the fee sheet keying its item
column `component` while the field is `name`), that every guidance marker sits in
a required column so the template's own example row is always recognised on the
way back in, and that every template reads back through our own reader. Then per
sheet: that a class name with no band in it is refused rather than filed under a
guess while "Basic 7" and "Grade 4" are read correctly, that a subject's
`Taught In` cell turns a level name into that level's classes and reports every
name it could not resolve rather than the first, that an amount is refused rather
than coerced — words, negatives and accounting parentheses alike — that a charge
a class already has for the term is refused rather than doubled, that a class
whose only row failed gets no empty fee structure, that each import stamps its
records to the campus it was given and ignores a forged one, and that a bursar
reaches none of the twelve screens.

The `fix/bugs2` batch adds 79: that the welcome email sends on signup with its
logo, its absolute links and its four steps — and that signup still succeeds when
the mail backend throws or the site has no public address — that the loading state
never reaches for the `disabled` attribute while submit buttons still carry a name
the view reads, that the reveal is offered to every password field while Generate
stays on the "choose a password" ones, that the toast's icon and message cannot be
resized or reflowed by the collapse, that the tour's assets are vendored and
deferred, that its config carries the role and account and holds back over a
temporary password, that a "Take a tour" button is on every screen for every role,
that every anchor a step asks for exists in some template, and that the
continuation appears after its own step, names the next one, and goes when the
checklist is done.

This round adds 47 more: that one named account can never be deactivated while
every platform owner still can deactivate anybody else, that the platform's sidebar
carries no Admissions or Messaging and does carry Sender IDs, that exactly one row
is highlighted on every screen each role can reach — and that two entries sharing
an href can never both light up again — that the About page carries its copy, its
real addresses and both social accounts, that the footer's icons point at those same
URLs and none at a placeholder, and that the reset email leaves as multipart with an
absolute PNG logo, brand colours, no SVG, and a link present in both halves.

The navigation and settings pass adds 71: that a person can rename themselves and
cannot promote themselves doing it, that the logo is the proprietor's in the form as
well as on the page, that "School settings" is gone from the one sidebar it 404'd in
and present in the two where it works, that no footer link goes nowhere and About is
in the sitemap, that the platform's Users screen shows a School column a school's
Staff screen does not, that deactivation locks an account out without deleting
anything and cannot be turned on yourself — and that `ui.js` and `app.css` still
agree about how long each stage of a toast takes.

Modules add 67: that fees and student records cannot be switched off and that
messaging and admissions are off until somebody says otherwise, that the platform
owner can toggle and that a school owner cannot — not even for their own school,
and not by POSTing to the endpoint directly — that every URL a switched-off module
owns answers 403 with a page rather than a 500, including one that does not exist
yet, that the school's public enquiry page 404s while admissions is off and opens
again when it is on, that switching a module off deletes no rows and switching it
back on restores the screens, that toggling school A never moves school B, and that
Fulfilled Academy's recorded state is the one the client asked for.

Reports adds 46: that one school's report never contains another's campus, class
or child — including when the other school's id is typed into the query string
— that expected, collected and outstanding are the same numbers the dashboards
show, that confirming or voiding a payment moves every figure at once, that an
overdue term turns unpaid balances overdue, that the export is a PDF and arrives as
a download, that every amount on it carries both spellings and no substitute font
ever reaches the file, and that the tables which came off the dashboards are gone
from them and present on the report.

Messaging adds a class of its own for the gateway: that a batch goes out under
the school's own Sender ID on Termii's `dnd` route, that a success and a refusal
map onto the right `MessageRecipient` rows, that nothing but an explicitly
promotional message reaches the `generic` route -- a blank, a typo or a purpose
nobody thought about all fall to `dnd` -- and that two schools sending in the
same run never borrow each other's name. Termii is exercised against a recorded
request rather than the network, so the assertions are about what the gateway
would actually receive. The negative-path tests deliberately trigger
403s and 404s, so Django logs tracebacks during a passing run.

---

## How the tenancy works

Isolation is enforced in the model layer, not per view.

1. `TenantMiddleware` reads `request.user` and publishes a `TenantContext`
   (school, branch, role) into a `contextvars` variable for the request.
2. Tenant-scoped managers read that context in `get_queryset()` and filter
   before any view code runs.

So a view writes `Student.objects.all()` and gets only its own tenant's rows.
There is nothing to remember and nothing to forget.

### Adding a tenant-scoped model

Subclass `TenantScopedModel` and you're done:

```python
from apps.core.models import TenantScopedModel

class Payment(TenantScopedModel):     # brings school + branch FKs and indexes
    reference = models.CharField(max_length=64)
```

Use `BranchScopedModel` instead when the record must live at one campus, as
`Class`, `Subject` and `Student` do. It makes `branch` required and derives
`school` from it, so a platform owner — who has no school of their own — can
still create rows.

You get:

- `school` and `branch` foreign keys (`branch` nullable — some records belong to
  the school as a whole).
- `Payment.objects` — filtered by the active tenant automatically.
- `Payment.all_objects` — never filtered, for jobs and cross-tenant reporting.
- `save()` stamps `school`/`branch` from the active context when left blank.
- `created_at` / `updated_at`.

### Roles and what they see

| Role                         | Scope    | Rows visible                                   |
| ---------------------------- | -------- | ---------------------------------------------- |
| `platform_owner`             | platform | everything                                     |
| `school_owner`               | school   | all branches of their school                   |
| `principal` (branch admin)   | branch   | their branch + school-wide rows (`branch=NULL`) |
| `bursar`                     | branch   | their branch + school-wide rows                |

Defined once in `apps/core/roles.py`. Two rules worth knowing:

- **No context = no filtering.** Outside the request cycle (shell, migrations,
  management commands) queries are unfiltered — otherwise `migrate` could not
  see its own rows. Background jobs that should act as a tenant must say so:
  `with scope_to(school_id=..., branch_id=..., role=...):`.
- **Misconfiguration fails closed.** An unknown role, or a branch-scoped role
  with no branch assigned, sees nothing rather than defaulting to wider access.

Escape hatches, both deliberately visible in review:

```python
from apps.core.tenancy import unscoped, scope_to

with unscoped():                  # cross-tenant reporting, support tooling
    Student.objects.count()

with scope_to(school_id=7, branch_id=3, role=Role.BURSAR):
    ...                           # background job acting as a tenant
```

### Capabilities: what a role may *do*

> Capabilities are the ceiling, and a school's [modules](#modules-what-each-school-has)
> take some of it back: `capabilities_for(role)` is the pure role table, and
> `capabilities_of(user)` is that table minus anything belonging to a module the
> account's school does not have.


Scoping answers "which rows?". Capabilities (`apps/core/permissions.py`) answer
"which actions?" — so a bursar can have full visibility of the academic setup
while still being unable to change it.

| Capability         | Platform owner | School owner | Principal | Bursar |
| ------------------ | :------------: | :----------: | :-------: | :----: |
| `view_academics`   | ✓ | ✓ | ✓ | ✓ |
| `manage_academics` | ✓ | ✓ | — | — |
| `view_fees`        | ✓ | ✓ | ✓ | ✓ |
| `manage_fees`      | ✓ | ✓ | — | — |
| `view_students`    | ✓ | ✓ | ✓ | ✓ |
| `manage_students`  | ✓ | ✓ | ✓ | — |
| `view_school_profile`   | ✓ | ✓ | ✓ | — |
| `manage_school_profile` | ✓ | ✓ | — | — |

A bursar collects against the fee structure but does not decide it, and finds
students in order to take money from them rather than to enrol or remove them —
so they read all three layers and change none.

A principal reads the school's setup — its classes, fee structures and School
settings — and changes none of it; that is the school owner's (and the
platform's, on their behalf). Pilot schools asked for that line. The principal
keeps the day-to-day work: recording and confirming payments, reading them and
their attached receipts, and the roster. The money-*movement* capabilities
arrive with the payments layer.

Grants live in one table, `ROLE_CAPABILITIES`, never in an ad-hoc check inside a
view. Enforce with the mixin, and hide the controls to match:

```python
class ClassCreateView(CapabilityRequiredMixin, CreateView):
    capability = Capability.MANAGE_ACADEMICS
```

```html
{% if "manage_academics" in capabilities %} ... {% endif %}
```

The mixin raises 403 for a signed-in account that lacks the capability (rendered
by `templates/403.html`) and redirects anyone signed out to the login page.
Hiding a button is presentation; the mixin is the enforcement, and it blocks POST
as well as GET.

### Who may invite, and as what

`MANAGE_STAFF` is in `_SCHOOL_ADMIN`, so **the proprietor and the platform invite;
a principal and a bursar do not.** A principal still reads the list — they need to
know who works there — and the screens agree with the capability, so the sidebar
never offers them a button that would 403.

The role dropdown offers **Principal and Bursar only**. Two roles are absent for
different reasons:

- `PLATFORM_OWNER` was never a school's to give. Platform staff are made with
  `create_platform_owner`, by somebody with a shell;
- `SCHOOL_OWNER` is the newer rule. There is one proprietor per school, and an
  owner who could invite a second would be handing over the only role that can
  invite and the only one that can deactivate an account — with no way to take it
  back from that screen. Until there is a reason for two, "can I add another
  owner?" is a conversation rather than a dropdown.

Both are refused in `clean_role` as well as left out of the choices, because a
`<select>` is a suggestion and a POST is not.

### Permission role vs. job title

Two separate fields on `User`, on purpose:

- **`role`** — the permission role. Controls what the account can see. One of the
  four above.
- **`job_title`** — free text, e.g. "Head of Mathematics". Descriptive only,
  grants nothing.

Promoting someone's job title is an HR change, not an access change.

### Why `User.objects` is not scoped

Authentication backends and `createsuperuser` resolve users through the default
manager, before any tenant context exists — scoping it would break login. Use
`User.scoped` for tenant-facing staff listings; the admin is narrowed separately
by `TenantScopedAdminMixin`.

## Academic setup

Step 2 of onboarding, at `/academics/`. Both models are `BranchScopedModel`, so
every list and lookup is tenant-filtered without a line of filtering in any view.

**`Class`** — a teaching group. `name` holds the year without its arm ("SSS 2")
and `stream` holds the arm ("Science", "A"); `display_name` joins them the way
staff say it, appending a one-letter arm ("JSS 1A") and spacing a word one
("SSS 2 Science"). Splitting them is what lets every arm of a year sort together
and lets a subject attach to one arm but not the other.

Ordering is by `level` then `year_in_level`, so classes always list in admission
order — Pre-KG first, SSS 3 last. `Level` values are spaced (10/20/30/40) to
leave room for a band in between later without a data migration.

**`Subject`** — attached to classes many-to-many, because one subject genuinely
spans several. Mathematics is *one* row linked to Primary 1 through both arms of
SSS 3, not fifteen near-duplicates.

Class names are unique per branch, not per school, so every campus can run its
own "JSS 1".

### Bulk entry: nineteen classes, one submission

A school sets up around nineteen classes, and used to do it nineteen times:
open the form, fill five fields, save, land back on the list, press New again.
The screens at `/academics/classes/add-many/`,
`/academics/subjects/add-many/` and `/staff/invite-many/` are the same records
entered as a table.

Three things remove the round trips:

- **The campus is asked once**, at the top, instead of on every row.
- **Presets fill a whole band** from one press. The rungs come from
  `apps/academics/curriculum.py` — the same list `seed_academics` uses — so the
  button and the seed can never describe different ladders. "The whole ladder"
  is exactly the nineteen classes above.
- **Level and year are read off the name.** `infer_level("JSS 2")` is the junior
  band, `infer_year` is 2; "Basic 8" is junior secondary, "Basic 3" primary.
  These are *defaults offered to a visible form field*, never silent writes — a
  wrong guess costs one correction, and a name that cannot be read asks rather
  than guessing.

Nothing about what gets saved changes: these are ordinary ModelForms over the
same models with the same constraints. Each batch is one transaction, because
nineteen classes half-created with no indication of which nine is a worse state
to hand back than nothing at all.

`/academics/classes/edit-all/` is the other half — every class editable where it
sits, for renaming and deactivating without one page load each. Edit-only on
purpose: no blank rows at the bottom, because a table whose last three rows
behave differently from the rest is one people stop trusting.

The client-side half is one implementation shared by all four screens, plus the
fee line items: `data-repeat` in `js/ui.js`. A container declares
`data-repeat-prefix`, its rows carry `data-repeat-row`, and a `<template>`
supplies a blank one. Enter moves down the table instead of submitting it.
Without the script the server still renders its `extra` rows and the form still
posts — you simply get the rows you were given.

Single-record forms that people fill several of in a row (a class, a subject, a
term, one staff account) carry **Save and add another**, which returns to a
blank form instead of the list. The record written is identical either way.

### The third route: bring your own ladder

The presets are the Nigerian ladder, and that fits most schools here and some
not at all. A school running Grade 1 to Grade 12, or Basic 1 to Basic 9, or
classes named after houses, would otherwise be editing nineteen pre-filled rows
into thirty different ones.

So classes and subjects can arrive by spreadsheet instead — see [Bulk import
from a spreadsheet](#bulk-import-from-a-spreadsheet). The choice is put where
the decision is made rather than hidden behind a menu: a school with no classes
yet is shown two cards on `/academics/classes/`, "the standard Nigerian set" and
"import your own list", and the bulk-add screen carries the escape hatch next to
its preset buttons. A school that cannot find the import gives up and types.

### Seeding

`seed_academics` builds the ladder and subject list from
`apps/academics/curriculum.py` — one module, edit it and re-run:

```bash
python manage.py seed_academics --create-school
python manage.py seed_academics --replace-subjects   # after editing curriculum.py
```

It is idempotent, matching on (branch, name). For Fulfilled Academy that is 19
classes (13 single-arm plus three senior years × Arts/Science) and 34 subjects.

Thirty-four, not sixty-odd, because a subject taught at several levels is one
row: Religion Studies spans 13 classes, Mathematics spans 15. Where the name
differs by level it stays a separate subject — "Social Studies" (primary) and
"Social & Citizenship Studies" (secondary), "History" and "Nigeria History",
"Home Economics" (primary/junior) and "Home Management" (SSS Arts).

| Level | Subjects per class |
| ----- | -----------------: |
| Nursery | 8 |
| Primary | 10 |
| Junior Secondary | 10 |
| Senior Secondary | 11 per arm |

### The senior arms teach different subjects

Nursery through junior secondary is one list per band. Senior secondary is not:
SSS Science and SSS Arts carry **eleven subjects each, and only five of them
overlap**.

| | Subjects |
| --- | --- |
| Shared core | Mathematics, English Studies, Economics, Marketing, Civic Education |
| Science only | Chemistry, Biology, Physics, Agriculture, Geography, Livestock |
| Arts only | Commerce, Accounting, Government, Literature in English, Christian Religious Knowledge, Home Management |

Arm-specific subjects narrow their placement — `at(SENIOR, "Science")` — while
the shared five stay **one row each** at `at(SENIOR)` with no arm named, which
puts them on both. Duplicating them per arm would mean renaming a subject twice
and would make "how many subjects does SSS 2 Science take?" a question about our
data model rather than about the school.

The consequence worth knowing: several subjects that run through junior
secondary stop at JSS 3, because neither senior list includes them — Digital
Literacy, Nigeria History, Social & Citizenship Studies, Basic Science
Technology & PHE, Cultural and Creative Art, Religion Studies and Home
Economics. The senior arms pick their own equivalents up by name where the
school teaches one, which is why Christian Religious Knowledge and Home
Management are separate rows rather than senior placements of the junior
subjects.

## Fee structures

Step 3, at `/fees/`. Three models, all branch-owned:

```
Term  --<  FeeStructure  --<  FeeComponent
```

**`Term`** — one term of an academic year at a branch. Exactly one can be
current per branch, enforced by a partial unique index *and* by `save()`
standing the previous one down, so a stray script cannot leave two.

**`FeeStructure`** — the fee set for one class in one term, unique on
(class, term).

**`FeeComponent`** — a line item: name and amount, ordered by `position`.

### The total is never stored

`FeeStructure.total` is a property that sums the live components. There is no
total column, and a test asserts there never is one. A stored total is a second
source of truth that drifts the moment someone edits a line item, and
reconciling a payment against a stale figure costs a school real money.

The editor's live sum is display-only — the browser never posts a total, so a
JavaScript bug can degrade the typing experience but cannot change what a class
owes. The form and its line-item formset are validated together inside one
transaction, so a bad line can't leave a structure behind with no components.

### Screens

The list is grouped by level with per-level subtotals, defaults to the current
term, and ends with a "not yet priced" section — the question a school owner
actually has during onboarding. Terms have screens of their own at
`/fees/terms/`, gated on the same fee capabilities as the structures priced
against them.

A whole fee schedule can also arrive by spreadsheet — `Class | Fee Item |
Amount`, one row per charge, with the term chosen on the upload screen. See
[Bulk import from a spreadsheet](#bulk-import-from-a-spreadsheet).

### Seeding

```bash
python manage.py seed_fees            # needs seed_academics to have run
python manage.py seed_fees --replace  # drop structures the pricing file dropped
```

`apps/fees/pricing.py` is the single source, the same way `curriculum.py` is for
academics. Fulfilled Academy's First Term 2025/2026 comes to 19 structures, 61
line items, ₦1,796,000 across the term.

Two things the pricing file handles deliberately:

- **One structure per class, never shared.** Primary 2–6 charge the same today,
  so they are written as one spec listing five class names — but the seeder
  creates five independent structures with their own components. Repricing
  Primary 3 leaves Primary 2 alone, and a test proves it.
- **`client_total` is a cross-check, never stored.** Where the school's written
  total disagrees with the line items, the seeder charges the line items and
  prints a warning. Primary 1 currently trips this: the line items sum to
  ₦102,000 against a written total of ₦104,000. The components are seeded as
  supplied and **no balancing line has been invented** — the ₦2,000 gap is a
  real question about the source data, and burying it in a fake component would
  make it unanswerable later.

## Student records

Step 4, at `/students/`. One model, `BranchScopedModel` like the rest, so a
principal's roster is their own campus without a line of filtering in any view.

**`Student`** — admission number, names, sex, dates of birth and admission,
status, and the parent/guardian contact the school actually calls about fees.

### Admission numbers are unique per branch, not globally

Two campuses of the same school both number their intake from 001, and neither
should have to renumber because the other got there first. The constraint is
`UniqueConstraint(Upper("admission_number"), "branch")` — case-insensitive, so
`FA/2025/001` and `fa/2025/001` collide, but the school's own casing is stored
as typed rather than rewritten. The form catches the clash first and reports it
on the field; the constraint is what makes it true regardless of how the row
arrives, which is what the Excel import leans on.

### The fee position is derived, never stored

A student's expected fee is the total of their class's `FeeStructure` for the
term their branch is currently in. It is computed in `apps/students/fees.py`,
and there is no fee column on `Student` — a test asserts there never is one.
Copying the figure onto the student would go stale the first time a class is
repriced and would then have to be corrected for every child in it by hand.

The list screen would make that derivation expensive if done per row, so
`fees.load(students)` fetches the current terms and the class totals in two
queries and answers for the whole page in memory. A test asserts a page of
twelve students costs the same as a page of two.

Payments do not exist yet, so `paid` is zero and every priced student reads as
**Unpaid** with the full term fee outstanding. That is honest rather than
decorative, and it is the seam the payments layer plugs into: fill
`FeeSchedule.payments` and the pills, balances and the "Payment history" card
on the detail page start telling the real story without those screens changing.
A student in a class with no structure for the term reads **No fees set**,
which is a setup gap the school needs to see — not a zero balance to be
reassured by.

### Screens

- **List** — searchable by name or admission number (whole or fragment, because
  staff quote either "FA/2025/014" or just "014"), filterable by class and
  status, 25 to a page. Defaults to active students; a roster that has run for
  years is mostly former ones.
- **Detail** — identity, class, guardian, and the fee position broken down to
  the line items behind it, with the payment-history slot laid out and labelled.
- **Add / edit** — manual entry. Branch is not a field: the class carries it, and
  asking for both invites the two to disagree. Removing a student offers
  "mark withdrawn" first, because deleting throws away the record that a payment
  will later need to hang off.
- **Import** — the bulk path for a school arriving with a roster already in a
  spreadsheet. Template, per-row validation report, then the write. See below,
  and [Bulk import from a
  spreadsheet](#bulk-import-from-a-spreadsheet) for the machinery it shares with
  the class, subject and fee imports.

Parent phone numbers are validated as Nigerian mobiles and stored in one
canonical form (`08034129876`), whichever of `0803 412 4567`, `+234 803 …` or
`234 …` was typed; the screens group them for reading and link them for calling.

### Excel import

At `/students/import/`, owner- and principal-level only (`MANAGE_STUDENTS`, so a
bursar gets a 403). The shape of it is shared with the class, subject and fee
imports and is written up under [Bulk import from a
spreadsheet](#bulk-import-from-a-spreadsheet) — this was the first of the four
and set the pattern.

What is specific to students: thirteen columns with the aliases a school's own
roster uses (`Adm No`, `Surname`, `D.O.B.`, `Guardian`, `Phone`); drop-downs for
Sex and Status written into the file; a reference tab listing *that branch's*
classes with the Class column validated against it; the admission number checked
against the roster and the rest of the file at once; and a phone column Excel
turned numeric (`8031122334`) getting its leading zero back — not a typo the
school made and not one they can see.


### Seeding

```bash
python manage.py seed_students            # needs seed_academics; seed_fees for fee positions
python manage.py seed_students --replace  # drop students the roster file dropped
```

`apps/students/roster.py` is the single source. Fulfilled Academy's pilot roster
is 60 students across 9 classes, ₦5,802,000 expected for First Term 2025/2026.

The data is deliberate rather than filler, because a roster of "Student One /
Student Two" hides the problems these screens exist to surface: admission
numbers carry their year of admission (so the same `001` recurs each year and
across branches), dates of birth match the class, two Okonkwo siblings sit in
Primary 1 and KG 2 sharing one guardian, and one student is withdrawn and one
inactive so the status filter has something real to filter.

## Bulk import from a spreadsheet

Four things arrive by spreadsheet — students, classes, subjects and fee
structures — and they arrive the same way, because they are the same job:

| What | Where | Capability | Columns declared in |
| --- | --- | --- | --- |
| Students | `/students/import/` | `MANAGE_STUDENTS` | `apps/students/importer.py` |
| Classes | `/academics/classes/import/` | `MANAGE_ACADEMICS` | `apps/academics/importers.py` |
| Subjects | `/academics/subjects/import/` | `MANAGE_ACADEMICS` | `apps/academics/importers.py` |
| Fee structures | `/fees/import/` | `MANAGE_FEES` | `apps/fees/importers.py` |

Three screens each — download the template, upload and see the verdict, confirm
— and the middle one is the product.

### The template is the contract

A school fills in **our** columns. Nothing here tries to work out the structure
of a document it was handed, because a guess about which column held the amount
is a guess about money. What *is* forgiving is everything around that: columns
are matched by heading rather than position, with aliases, so a reordered sheet
or a school's own spelling still reads; a title row above the headings is found;
blank rows anywhere are skipped; headings we have no field for are ignored and
listed back.

### Three modules, one set of rails

- **`apps/core/imports.py`** — the contract and the verdict. `Column`, `Sheet`,
  `SourceRow`, `FieldError`, `RowResult`, `ImportReport`, `Judgement`, and the
  cell parsers (`parse_date`, `parse_amount`, `parse_list`). Knows nothing about
  openpyxl and nothing about any particular model.
- **`apps/core/spreadsheets.py`** — the openpyxl boundary. Takes a `Sheet` and
  returns `.xlsx` bytes, or takes an upload and returns plain strings. Knows
  nothing about validation.
- **`apps/core/import_flow.py`** — the three screens, the upload form and the
  session hand-off. An `ImportFlow` subclass per entity says which sheet, what
  the file belongs to, and how to build, validate and write.

Each entity's own module holds only what is true of it. The student importer is
the thirteen columns, the admission number and the phone-number repair; it is no
longer where the machinery lives.

### What the file belongs to

Asked once, at the top, and only when there is a choice: a principal has one
campus and is not made to confirm it. The field is **left out** rather than
hidden, so a forged POST cannot name something the account cannot see. Students,
classes and subjects are scoped by campus; fee structures by **term**, which
settles the campus too, because a term belongs to one branch.

A school owner is offered one template per campus, because the class list on the
reference tab differs. The fee import offers one per *campus* rather than per
term for the same reason — a school in its third year would otherwise get nine
identical buttons.

### A bad row is not a bad file

All-or-nothing sends the user back to fix one cell at a time, so failure is per
row. The report names the spreadsheet's own row number, the column, and the
reason — *"Row 14 — Admission Number 'FA/2025/001' already belongs to Chinaza
Okonkwo; Class 'JS1' is not a class at North"* — and the rows that passed can
still be imported while the rest are corrected.

A row reports **all** of its problems at once, including the ones the model
finds, so the trip back to the spreadsheet happens once. `Judgement.refuse()` is
what makes that safe: it records the error *and* marks the field as spoken for,
and `ask_the_model()` excludes exactly those fields from `full_clean`, so the
user is never told about one cell twice in two different voices.

That pairing has a sharp edge, and it has already drawn blood: a column key that
is not the model's field name silently stops the exclusion working. The fee sheet
keyed its item column `component` while the field is `name`, and an empty cell
was refused twice — once in the sheet's words and once in the model's.
`apps/core/tests_imports.py` now walks every sheet and asserts that every column
key is a field on the model it feeds.

### What each sheet checks

- **Students** — required fields; the admission number against the branch's
  roster *and* the other rows of the same file, case-insensitively like the
  constraint; the class against the branch's active classes, with separate
  messages for unknown, inactive and ambiguous; sex, status and dates by parsing.
- **Classes** — `Level` and `Year` are **optional**, and read off the name:
  "Grade 4" is year four, "Basic 7" is junior secondary. A name with no band in
  it ("Reception", "Alpha Class") is **refused** with the four band names in the
  message, never filed under a guess. Duplicates are caught on the name and arm
  as a person reads them, so "Primary 1" and "primary 1" cannot both be created.
- **Subjects** — one cell, `Taught In`, holds a *list*: class names, a band name
  ("Primary" means the six primary classes), or `All`. That is the one place a
  fixed template cannot give each value a column of its own, because the number
  of them differs per school. Every unresolved name is reported, not just the
  first. Blank is allowed — a school importing subjects before classes has not
  made a mistake.
- **Fee structures** — `Class | Fee Item | Amount`, one row per charge, so a
  class with four charges takes four rows. Fixed columns per charge would mean us
  deciding what a school calls its fee items, and no two agree. The amount is
  parsed from `₦45,000`, `NGN 45000` or `45000`; accounting parentheses are read
  as negative so a credit is *refused* rather than silently charged; words are
  refused rather than coerced to zero. A charge the class already has for that
  term is refused rather than doubled. A structure is created **only** when
  something is going into it, so a file whose Primary 1 rows all failed leaves no
  empty structure reading ₦0 behind — the fee list would show that as "priced"
  and bill nobody.

### Two things worth knowing about the shape

- **Validation never touches openpyxl; the reader never validates.** The reader
  hands over plain strings — dates as ISO, whole floats without their `.0` — so
  validation is testable without building a workbook, and so the parsed rows
  survive the session.
- **The rows wait in the session, not a table.** An abandoned import leaves
  nothing to clean up. Validation then runs *twice*: once to draw the report, and
  again on confirm, because the data can move while the report is on screen.
  Tests cover exactly that race for each import, and that a failure partway
  through a batch leaves the earlier rows unwritten.

A file that is not a workbook, is not the template, or has no rows under its
headings produces a sentence on the form, never a 500.


## Parent messaging

At `/messaging/`. Entirely staff-side and outbound: **parents never sign in.**
The platform's whole relationship with a parent is an SMS or WhatsApp message
going out, and a row recording what became of it.

### The provider interface comes first

```
apps/messaging/providers/
  base.py             MessagingProvider: send(recipient, message, channel,
                      purpose) + SenderIdentity, the school it sends as,
                      + MessagePurpose, which route the message may take
  console.py          the default -- logs it, Sender ID included, and marks
                      it delivered
  termii.py           the gateway: POST /api/sms/send, dnd routing
  bulksmsnigeria.py   kept and working, no longer the default
  africastalking.py   stub: reads AFRICASTALKING_*, builds the payload
  __init__.py         PROVIDERS registry and get_provider()
```

Nothing above `providers/` imports a vendor. Who a message comes from is not the
provider's decision either — see [Each school sends under its own
name](#each-school-sends-under-its-own-name).

The console provider is not a mock — it is the default, deliberately. The whole
feature has to work end to end before anyone signs an SMS contract, and a demo
that dies on a missing API key demos nothing. Every screen, count and delivery
row you see running on it is the same code path a real gateway will drive. An
unknown `MESSAGING_PROVIDER` raises rather than falling back, because a school
that thinks it is sending real SMS must not quietly be writing to a log file.

Africa's Talking remains a stub, complete except for the HTTP call: it reads
its settings, assembles the real payload (it wants `+2348031234567`), takes its
sender from the school's identity like the live providers do, and refuses to
send without credentials instead of returning a false success.

**The abstraction earned itself.** BulkSMS Nigeria declined to support
send-on-behalf-of-schools — one master account, many schools, each under its own
Sender ID — which is the arrangement this platform is built on. Moving to Termii
was a provider file, a default, and a data migration. No view, form, model or
template changed, and `providers/bulksmsnigeria.py` is still there and still
works, because a gateway relationship that ended once can end again.

### Two models

```
Message  --<  MessageRecipient
```

**`Message`** — one press of Send: body, channel, audience description, how the
audience was chosen, sender, provider, a rollup status, and `sent_as` — the
Sender ID the batch actually went out under, snapshotted like the recipients'
phone numbers. A school that renames its Sender ID next term has not
retroactively sent last term's reminders under the new name.

**`MessageRecipient`** — one row per parent, with the phone number and parent
name *snapshotted at send time*, the per-recipient delivery status
(pending / sent / delivered / failed), the provider's reference and any error.
This is what makes the log real: a bursar needs to know that Mrs. Okonkwo's
number bounced, not merely that "JSS 1A parents were messaged".

`sent` and `delivered` are deliberately different states — a gateway accepting a
message says nothing about whether a handset received it.

`Message.status` is the one figure on the platform that is a stored rollup
rather than derived, so the log can be listed and filtered without a join. It is
never set by hand: `refresh_status()` recomputes it from the recipient rows, and
a delivery report arriving days later calls it again. The rows stay the source
of truth.

### Audiences resolve in one place

`apps/messaging/audiences.py` answers three questions that must never disagree —
the live count on the compose screen, the rows actually written, and the
description stored on the message. All three come out of one `resolve()` call,
so the number the bursar saw *is* the batch that went out.

Four filters: all parents at a campus, one class, hand-picked students, and
parents who owe. Students who are not active, students with no number on file,
and every other branch's parents are excluded — and a filter with nothing chosen
resolves to *nobody*, never to everybody.

### "Parents who owe" is the point

The core use case is fee reminders, so `/messaging/reminders/` is the compose
screen with that audience preset and a draft in the body. It selects students
whose confirmed payments do not cover their class's fee structure for the
current term — derived live, like every other fee figure here.

A class with no fee structure this term is **not** chased. Nobody has said what
it costs, so there is no balance, and texting those parents would be the
platform inventing a debt.

`apps/students/fees.py` gets those confirmed amounts from the payments app
through `_paid_amounts()`, which resolves it via the app registry rather than
importing it — so the roster keeps rendering whether or not payments is
installed. See [Payments](#payments).

### Screens

| URL | What it is |
| --- | --- |
| `/messaging/` | The log: audience, channel, when, and "32 sent, 30 delivered, 2 failed". |
| `/messaging/compose/` | Write, pick a channel and an audience, watch the count. |
| `/messaging/reminders/` | The same screen, preset to the parents who owe. |
| `/messaging/<id>/` | One batch, and what happened to every parent's copy. |
| `/messaging/recipients/count/` | JSON, for the live count. |

The live count is a small endpoint rather than a framework, and it runs the same
`resolve()` the send path runs. With scripting off, the count still renders on
load and the form still sends — the endpoint only saves a round trip.

### Each school sends under its own name

The platform (SCHOOLCORD) holds the account with the SMS gateway and pays for
the units. Each school registers its **own** alphanumeric Sender ID against it,
so a parent at Dap Group of Schools sees a message from `Dapgroup`, not from the
platform and not from another school.

**`SchoolMessagingConfig`** — one per school: the Sender ID, the gateway it is
registered with, an approval status, and optional credentials.

```
Platform:   SCHOOLCORD  (holds the Termii master account)
School:     Dap Group of Schools
Sender ID:  Dapgroup
Provider:   Termii
```

`api_key` and `account_reference` are nullable and blank for the pilot: the
master account sends on every school's behalf, under the school's own name. A
school that later takes out its own gateway account fills them in and the send
path does not change — `TermiiProvider.api_key` already prefers the school's key
over the platform's.

`branch` is nullable and part of the uniqueness rule, so a school that later
wants a different Sender ID per campus can have one. A row with no branch is the
school's default; a row with one overrides it for that campus.
`apps/messaging/identity.py` is the only place that ordering is written down.

### Nothing sends under a name that is not the school's

A Sender ID is not usable until a gateway has approved it, so `resolve()` either
returns the school's identity or raises `SenderIdentityUnavailable` with a
sentence naming who fixes it:

* no config at all — "Dap Group of Schools has no Sender ID set up yet…"
* pending — "…is still awaiting approval, so nothing can be sent under it yet."
* rejected / suspended — the same, plus the note the platform recorded.

There is deliberately **no fallback**. Not the platform's name, not a blank
sender, and certainly not another school's: the first two get the batch rejected
by the gateway or ignored by parents, and the third is a tenancy breach a parent
would see on their own handset.

Resolution happens *before* anything is written, so a school that cannot send
gets no `Message` row at all — the log never shows a batch that was never really
sendable. The compose screen catches the same exception and keeps the draft.

### Which gateway carries it

Two decisions, not one:

| | Where it lives | What it means |
| --- | --- | --- |
| The school's gateway | `SchoolMessagingConfig.provider` | Where its Sender ID is registered. Two schools can be on two different gateways. |
| The platform override | `MESSAGING_PROVIDER` | When set, it wins over every school's choice. |

`MESSAGING_PROVIDER` defaults to `console`, so a fresh checkout sends nothing
anywhere. Clear it (`MESSAGING_PROVIDER=`) in production and each school goes out
through its own gateway. An override is not a fallback — it is the switch for
development, for tests, and for the day a gateway is down.

The Sender ID travels regardless: on the console provider the log line reads

```
[SMS] from Fulfilled [Fulfilled Academy] via Termii to 08118836702 (transactional): ...
```

— the school's name, the gateway that *would* have carried it, and the message.
That is why per-tenant identity is testable with no credentials and no money
spent.

### Termii

`providers/termii.py` is the live gateway. `POST /api/sms/send` with the school's
Sender ID as `from`, the API key in the body, `type: plain`, and the route in
`channel`. It uses `urllib` from the standard library rather than pulling in
`requests`: one POST with a JSON body does not justify a dependency the school's
server then has to keep patched, and `urllib` gives us the timeout, which is the
part that actually matters when a gateway is slow.

**One recipient per request, deliberately.** Termii has a bulk endpoint taking
an array of numbers, and it returns a single `message_id` for the whole batch.
Every `MessageRecipient` row here carries its own reference and its own status,
and collapsing thirty-two rows onto one id would mean a bursar chasing one
parent could not tell whether that parent's copy was the one that failed.

Their API answers `200` for messages it then refuses, so a 2xx is not on its own
a success: a `code` that is not `ok` is a failure, and so is a success with no
`message_id` — without one there is nothing to reconcile a later delivery report
against. Both response shapes are accepted, since the bulk endpoint answers with
`code` and the single-send endpoint historically has not.

Accepting a message means **sent**, never **delivered** — whether a handset
received it arrives later on their webhook, and reporting it as delivered would
make the log lie about the one thing a bursar chasing a parent needs it for.

A master-account balance under 100 units logs a warning on every send. Units
running out stops every school on the platform at once, and the first anyone
would otherwise know is a batch of failures.

### Transactional or promotional — the routing that has to be right

Nigerian gateways carry two routes, and picking the wrong one does not fail
loudly. It silently fails to arrive.

| | Termii channel | Reaches DND numbers | 8pm–8am |
| --- | --- | --- | --- |
| Transactional | `dnd` | yes | allowed |
| Promotional | `generic` | no | refused |
| *(WhatsApp)* | `whatsapp` | n/a | n/a |

Most Nigerian subscribers have Do-Not-Disturb switched on. A fee reminder put on
`generic` reaches perhaps a third of the parents it was addressed to, at an hour
of the school's choosing, and reports success for all of them — the school
believes it has chased the debt. So `Message.purpose` is a stored field, it
defaults to transactional, and `TermiiProvider.termii_channel` is written as
"generic **if** promotional, else dnd" rather than as a lookup table: every value
that is not the one promotional case — a blank, a typo, a purpose added later
and not thought about — lands on the route that arrives.

The compose screen asks, with transactional preselected, and the form's
`clean_purpose` applies the same rule so the stored purpose agrees with what was
actually sent. `purpose` is read off the batch at delivery, not passed in, so a
send resumed days later is routed exactly as its first half was. The message log
shows the route it went out on, which is what answers "why did only half these
parents get it?".

BulkSMS Nigeria expresses the same split as its numeric `dnd` flag rather than
as a named route, so a promotional send there drops to `dnd=0`.

**The channel is the first question, the purpose the second.**
`termii_channel` took a `channel` argument and ignored it, so a message on
`Channel.WHATSAPP` was handed an SMS route. `dnd` and `generic` are two
delivery classes of SMS; `whatsapp` is a different destination, and none of
what separates the SMS pair — the Do-Not-Disturb register, the overnight
curfew — exists on it. What WhatsApp prices and polices is the template's own
category, fixed when the template was approved, so a purpose has nothing to
say about it.

Nothing shipped through that bug: WhatsApp goes out through
`TermiiWhatsAppProvider`, whose endpoint and payload are different and which
never calls this method. It was wrong waiting to be reached.

### BulkSMS Nigeria — kept, not the default

`providers/bulksmsnigeria.py` is complete and still tested. A school already
registered there keeps sending there, and `MESSAGING_PROVIDER=bulksmsnigeria`
moves the whole platform back. Keeping a working integration that has stopped
being the default is the point of the interface — deleting it is how you find
out a year later that you cannot go back.

Migration `0004_move_configs_to_termii` moved existing school configs across.
**It resets approved Sender IDs to pending**, on purpose: an approval is a fact
about one gateway, and BulkSMS Nigeria having approved "Fulfilled" says nothing
about whether Termii has. Carrying the status over would have the platform claim
a registration that does not exist and bounce the first batch. Each moved row
keeps its Sender ID and gets a note telling the school what happened; a platform
administrator re-approves once Termii has registered the name.

### The identity screen

`/messaging/identity/` serves two readers at different scopes. A school sees its
own Sender ID, the gateway, the approval status, and a preview of how it lands
on a parent's handset. The platform owner sees every school — **including the
ones with nothing registered**, since a roll that only lists the schools already
set up hides exactly the ones needing action — and registers or approves from
`/messaging/identity/<school>/edit/`.

| | View own identity | Register & approve |
| --- | --- | --- |
| Bursar | | |
| Principal | yes | |
| School owner | yes | |
| Platform owner | yes | yes |

Approving is platform-only because the platform is the party that actually
submits a Sender ID to the gateway. A school approving its own would be marking
its own homework, and the gateway would reject the first message anyway.

Two schools cannot register the same Sender ID: gateways do let unrelated
accounts hold similar names, but on one platform it means parents cannot tell
which school texted them, and it is almost always a copy-paste from the row
above.

### Seeding

```bash
python manage.py seed_messaging_identity          # Fulfilled Academy -> "Fulfilled"
python manage.py seed_messaging_identity --school "Dap Group of Schools" \
    --sender-id Dapgroup --provider termii
python manage.py seed_messaging_identity --pending  # seed the blocked state
```

### Permissions

Bursar, principal and owner can all send. Messaging is the one place a bursar is
not read-only, and the reason is the job: chasing a fee is their work. Nothing in
messaging edits school data — it only reads the roster they can already see.

Nothing new is seeded; messaging runs off the existing students and their parent
contacts.

## Payments

At `/payments/`. The core of the product, and for the pilot it is **manual**:
the school keeps banking into its own account and the bursar records each
payment against a student. No gateway, no card, no settlement.

### One model

**`Payment`** — branch-owned, belongs to a student and a term. Amount, date
paid, label, method, reference, source, status, an optional receipt
attachment, and the audit trail (recorded / confirmed / voided, by whom and
when).

Two fields exist now specifically so a later change is not a rewrite:

* **`source`** — manual / gateway / bank-import. Every row is manual today.
  When a gateway or a bank-statement import starts creating rows, the ones a
  human typed stay distinguishable from the ones a machine reconciled, which is
  the first question anyone will ask when the two disagree.
* **`gateway_provider` / `gateway_reference`** — null on every row, waiting.

`term` is a foreign key rather than something worked out from the date: a
payment made in the holidays for next term is still that term's money, and only
the person recording it knows which.

### Balance is derived, never stored

```
expected = the class's FeeStructure total for the term
paid     = the sum of that student's CONFIRMED payments for that term
balance  = expected - paid
```

Not on the student, not on the term, not on the payment. It lives in
`apps/payments/balances.py` and is computed every time it is asked. Because
`paid_by_student()` is the single definition of "paid", confirming a receipt or
voiding a payment moves the student's balance, the outstanding list, the
bursar's dashboard and messaging's "parents who owe" in the same instant, with
no recalculation step anywhere.

Status derives too: **paid** (balance ≤ 0), **partial**, **unpaid**, and
**overdue** — which is not a fifth state but unpaid-or-partial past the term's
`due_date`. A term with no due date set never produces it: a deadline nobody
stated is not one a parent can have missed. The four existing payment-status
colours carry all of them.

An overpayment is a credit, not a negative balance.

### Only confirmed money counts

`pending` is the receipt handed in but not yet checked — the client's flow of
"someone uploads a receipt, the bursar confirms who and what it is for". A
pending payment sits in the queue, shows on the student's page, and changes no
balance at all until someone confirms it. The confirm step is the *same form*
as recording, deliberately: the bursar has to be able to correct the student,
the label and the amount against the slip at the moment they confirm.

### Labelling is descriptive

School Fees / Uniform / Books / Development / Other, set from the receipt or
from what the parent said. It does **not** split the balance — that stays one
combined figure. Per-label accounting would mean deciding what happens when a
parent pays 50,000 against a 30,000 uniform charge, and nobody has asked for
that answer.

### A voided payment is never deleted

It keeps its row, stops counting, and records who voided it and why. Voiding
asks for a reason and a tickbox, and the screen shows what the balance will
become before you press it. Money that was recorded and then reversed is a fact
about the account; erasing it is how a ledger stops being one.

Students are protected too: a student with payments against them cannot be
deleted from the roster — the screen says so and offers "withdraw instead",
which keeps the history and stops the billing.

### Screens

| URL | What it is |
| --- | --- |
| `/payments/` | Branch-wide list, filterable by label, status, method and date, with confirmed and pending totals for the filter. |
| `/payments/record/` | Record a payment. Student picked by typing; balance shown beside the amount. |
| `/payments/pending/` | The receipt queue: the slip alongside the figures it has to agree with. |
| `/payments/outstanding/` | Who owes, most owed first, with one click to message them. |
| `/payments/<id>/` | One payment, its attachment, its trail, and the balance it moved. |
| `/payments/<id>/receipt/` | A printable receipt with the running balance. |
| `/payments/<id>/void/` | Reverse it, with a reason. |
| `/payments/student/<id>/` | A student's full history. Their detail page shows the most recent eight. |

The student picker is a text input backed by a native `<datalist>`, not a long
`<select>` or a JS autocomplete: it is how the job is actually done — the bursar
has a receipt with an admission number on it and types it — and the browser's
own search matches a surname too. The receipt is plain HTML with a print
stylesheet rather than a generated PDF; the browser already knows how to print
and how to save as PDF, and a PDF library would be a dependency the school has
to keep working for a page that is one page long. That judgement still stands for
this page. It does not extend to [Reports](#reports), where the export is a filed
document rather than a single page.

### Permissions

| | View | Record & confirm | Void |
| --- | --- | --- | --- |
| Bursar | yes | yes | |
| Principal | yes | yes | yes |
| School owner | yes | yes | yes |

Voiding is supervisory: it reverses confirmed money. If the school would rather
the bursar could undo their own mistakes, add `VOID_PAYMENTS` to `_BURSAR` in
`apps/core/permissions.py` — the screens follow the capability table and nothing
else changes.

### Seeding

```bash
python manage.py seed_payments            # needs seed_students to have run
python manage.py seed_payments --replace  # clear this term's payments and reseed
```

The spread is chosen so every state is actually on screen: students paid in
full (in one payment and in two instalments), part paid, untouched, two pending
receipts with attachments, and one voided payment with its reason. Amounts come
from each student's own class fee structure rather than being hard-coded, so
re-pricing a class re-seeds sensible figures.

**Overdue is the one status the seed does not produce, and cannot.** It is a
property of the *term*, not of a student: the moment a term's `due_date` is in
the past, every unpaid and part-paid balance at that branch turns overdue at
once. Seeding one would therefore hide the unpaid and part-paid states behind a
wall of red rather than adding a fourth colour beside them. Set a due date on
the term (Django admin → Terms) and the overdue pill appears everywhere it
should. The behaviour itself is covered by tests in
`apps/payments/tests.py::OverdueTests`.

## Guiding a new school

Two things, and they answer the same question at different moments: *what do I do
next?*

### The continuation

Finishing a setup step used to be a dead end. You added your classes, landed back
on the class list, and the checklist you came from was two clicks behind you.

`templates/core/_next_step.html` closes it. Each of the four onboarding
destinations declares which step it *is*, and the prompt appears only when that
step is now done and something else is not:

| Screen | Declares |
| --- | --- |
| `/academics/classes/` | `after="academics"` |
| `/fees/` | `after="fees"` |
| `/students/` | `after="students"` |
| `/staff/` | `after="staff"` |

So it is never shown before the step it congratulates is finished, it names the
next step by reading the checklist rather than by hardcoding an order, and it goes
by itself once setup is complete rather than becoming a permanent strip of chrome
on four screens.

**Inline, not a modal.** Most visits to the class list are not the first one, and a
dialog would interrupt somebody who came to do something else. The point is to
offer a next step, not to insist on one.

The checklist comes from `{% onboarding_progress as setup %}`
(`apps/core/templatetags/onboarding.py`) rather than a context processor:
`setup_progress` is half a dozen counts, and a processor would run them on every
page in the app to serve four — including for a school that finished months ago.

> **`pluralize` cannot pluralise "class".** It appends a letter, so the onboarding
> page had been printing "2 classs" since it was written. Each step now carries a
> `noun_plural` and `setup_progress` builds the phrase (`step.tally`) beside the
> noun it belongs to, rather than three templates each assembling it with a filter.

### The tour

A guided walk through the shell, built on [Driver.js](https://driverjs.com) (MIT,
25KB, no dependencies) — **vendored**, at `static/js/vendor/`, not pulled from a
CDN. The rest of this app works offline behind whitenoise, and a tour that depends
on a third party being up is a tour that breaks the day somebody's network blocks
it. `npm install` keeps the source in `package.json`; the built file is committed.

`static/js/tour.js` holds the stops. Two decisions in it are worth knowing:

**Targets are `data-tour` names, never classes or positions.** A class is a styling
decision and moves; `data-tour` is a promise that a step depends on that element.
Nav rows get theirs from `NavItem.tour`, so **a row a role does not have carries no
marker, and the tour drops that stop rather than pointing at nothing** — which is
what makes one step list safe across four roles.

**The tour is role-aware from one list, not four.** Each step declares its `roles`,
and a couple vary their wording by role rather than existing twice. A bursar's tour
stops at Payments and talks about recording money; a proprietor's stops at Fees and
talks about pricing classes; the platform owner's skips both, because neither is
their work.

| When | How |
| --- | --- |
| Automatically, once | First time an account reaches the app, held back while they are still on a temporary password — a tour of a sidebar you cannot use yet is a tour of a locked door. |
| On demand, always | **Take a tour** in the sidebar footer, on every screen. |

"Seen it" lives in `localStorage`, keyed by account id so two people sharing an
office computer each get their own first run. Deliberately not a column on the
user: this is a UI preference of the same kind as the theme and the collapsed
sidebar, both of which already live there. The cost is that clearing site data
offers the tour again, which is a tour rather than a loss.

The popover is re-pointed at the design tokens in `assets/app.css`, so a stop looks
like the app it is describing and follows both themes. Driver.js paints its overlay
onto an SVG mask rather than an element, so its colour is set in JS — and under
`prefers-reduced-motion` it is told not to animate at all.

## Modules: what each school has

Three questions decide whether an account can reach a screen, and they are
deliberately separate:

| Question | Answered by |
| --- | --- |
| *Which rows?* | Tenancy — `apps/core/tenancy.py` |
| *Which actions?* | Capabilities — `apps/core/permissions.py` |
| *Which features does this school have at all?* | Modules — `apps/core/modules.py` |

A module is a commercial fact about a school, not a permission. Admissions is a
paid add-on; parent messaging costs the platform money per message. A school that
has not taken them should not see them and should not be able to reach them by
typing a URL — but none of that is a statement about the principal doing the
typing, which is why it cannot live in the capability table.

### The registry is the only declaration

`apps/core/modules.py` holds one `Module` per feature: its URL prefixes, the
capabilities it owns, its default, and whether it can be switched off at all.
Adding a module means adding an entry there and nothing else — no migration, no
second list of names anywhere in the codebase.

| Module | Paths | Default |
| --- | --- | --- |
| `academics` | `/academics/` | always on |
| `students` | `/students/` | always on |
| `fees` | `/fees/`, `/payments/`, `/reports/` | always on |
| `messaging` | `/messaging/` | **off** |
| `admissions` | `/admissions/`, and the school's public enquiry page | **off** |

The always-on three are what the product *is*: a school with no student records,
no fees and no classes is not a school. `SchoolModule.set_state` refuses to record
a decision about them, and a stale row claiming otherwise is ignored.

### Absent means "nobody has decided"

`SchoolModule` stores *decisions*, not state. With no row, a school gets the
registry default — so a school that signs up this morning needs nothing written
for it, and changing a default later moves every school that never chose. Writing
a row is how the platform owner overrides that, in either direction, and the screen
shows which of the two a switch currently is (`Platform default` against a chip).

A key the registry does not recognise — a module since renamed or removed — is
ignored rather than obeyed. The registry is the authority on what exists.

### Enforcement is in four places, each because it knows something the others don't

1. **`ModuleAccessMiddleware`** gates the URL prefixes for the caller's own school.
   A middleware rather than a view mixin, deliberately: the requirement is that a
   switched-off module is *never* reachable by typing its URL, and a mixin delivers
   that only for as long as everybody remembers to add one. Matching on the prefix
   means a screen added under `/admissions/` tomorrow is gated today. A test asserts
   exactly that, against a URL that does not exist.
2. **`capabilities_of(user)`** withdraws the module's capabilities. This is what
   closes the doors drawn on *other* modules' screens — the fee-reminder button on
   the bursar's dashboard is a messaging action sitting on a payments page, and the
   middleware guarding `/messaging/` cannot reach it. Withdrawal, not absence:
   `capabilities_for(role)` stays the pure answer, because a capability is a fact
   about a role.
3. **`nav_for(...)`** drops the module's nav entries, so the sidebar never offers a
   door the middleware would shut. `NavItem.module` is the third gate beside
   `roles` and `capability`, and like `capabilities` it defaults to empty — a
   caller that does not pass it gets less, not more.
4. **The school's public enquiry page** checks for itself, in
   `SchoolFromSlugMixin`. It belongs to the school named in the URL rather than to
   the reader, and usually nobody is signed in at all: the middleware knows the
   caller's school, and only that mixin knows the page's.

### What a refusal looks like

A staff URL answers **403** with `templates/403_module.html` — which names the
feature, says who can switch it on, and says that nothing has been deleted, because
that is the first thing a school asks. Deliberately not the ordinary 403 page: a
permission refusal is about the account, and this one is not.

The public enquiry page answers **404**. To a parent with a link from Instagram
there is no such page, and a signed-out stranger is owed no account of which
features a school has bought.

### Nothing is deleted

Switching a module off hides it and closes its screens. The rows stay, the code
stays, the URLs stay routed. Switching it back on the same afternoon restores every
screen exactly as it was — one row written, no migration, no redeploy, and the
answer moves inside the same request cycle. A test switches admissions off with an
applicant on the board and finds them still there when it comes back on.

### Only the platform owner switches them

`MANAGE_SCHOOL_MODULES` is granted in `_PLATFORM` and nowhere else. It is the one
capability deliberately absent from a school owner's set, and for a commercial
reason rather than a technical one: a proprietor who could turn on the admissions
add-on would be deciding their own bill. A school owner gets a 403 from the school
detail screen — including for their own school — and from the toggle endpoint.

### The platform owner's screens

| URL | What it is |
| --- | --- |
| `/platform/` | Their dashboard: the size of the platform, and the schools that need a look — a campus with no current term, a school with no students, a suspended account. |
| `/platform/schools/` | Every school as a card: its own logo, its plan and status, its counts, and a chip per switchable module showing on *or* off. The whole card is the link. |
| `/platform/schools/<id>/` | One school: identity, figures, the module switches, its campuses and their current terms, who can sign in, and a link to its collections report. |
| `/platform/schools/<id>/modules/` | POST only. Switches one module. |
| `/platform/schools/<id>/profile/` | POST only. The school's name, office contacts and logo — the same `SchoolProfileForm` a proprietor uses. A rejected form comes back on the whole detail page, not a stub. |
| `/staff/` | **Users** at platform scope: every account, with its school, and a deactivate control. |
| `/staff/<id>/activation/` | POST only. Switches an account off, or back on. |

**Those are two URLs on purpose.** They were one for a while, which gave the
platform sidebar a Dashboard row and a Schools row both resolving to `/platform/` —
and `_keep_only_the_closest_match` compares href *lengths*, so a tie cleared
neither and both rows stayed lit. The first attempt at that was to drop the
Dashboard row; splitting the screens is the better fix, because the two genuinely
answer different questions. A test asserts exactly one row is active on every
platform screen.

`/platform/schools/` used to be a table of counts, which was the wrong shape for
the job: the platform owner does not arrive to read numbers, they arrive to pick a
school and do something to it. **Schools** in the sidebar points here now rather than at the
Django admin changelist — the admin is neither tenant-aware nor the place to make a
commercial decision from, though `SchoolModule` is registered there for the question
the admin is good at: when did this change, and who changed it.

Each switch is its own form, and it POSTs the state it wants rather than "flip it".
A toggle that flipped whatever it found would do the wrong thing the moment somebody
double-submitted or pressed the switch in a tab that had gone stale. A POST rather
than a link, because a GET that changed what a school pays for would be followed by
every crawler and prefetcher on the internet — a test asserts the endpoint answers
405 to a GET.

### Deactivating an account

`DEACTIVATE_ACCOUNTS` is platform-only, for the same reason the modules are: it is
how a school's access is suspended, and a proprietor who could do it could also
lock out the platform's own support account at their school.

It sets `is_active = False`, which is Django's own "may not sign in" — so the
lockout happens at the authentication backend rather than in anything this codebase
has to remember to check. **Nothing is deleted:** the payments the account recorded
and the messages it sent keep its name, and reactivating restores access with the
same password.

Three guards worth knowing.

The POST names the state it wants rather than flipping whatever it finds, so a
stale tab cannot reactivate somebody by pressing what it thinks says deactivate.

You cannot deactivate your own account — not a permission question but a footgun
one: switching off the account you are signed in as locks you out of the screen
that switches it back on.

**And one account is out of reach of it entirely.** Every platform owner holds the
capability, which means each can switch the others off — a state the platform can
lock itself out of, with nobody left who can undo it. So `PLATFORM_OWNER_EMAIL`
names one address that is never a valid target:

```
# Railway service variables (read in config/settings/base.py)
PLATFORM_OWNER_EMAIL=<the platform owner's login email>
```

Matched case-insensitively on the trimmed address. It is **environment-only**: an
address belongs to whoever runs the platform, not to the code. So unset means no
account is protected, and check `schoolcord.W005` says so on every deploy rather
than letting the protection lapse silently.

The rule is a *target*, not a role: the protected account still holds the capability
and can deactivate anybody, including another platform owner. What changes is that
nobody, itself included, can deactivate it. `permissions.may_deactivate(actor,
target)` is the single answer to "may this happen?", and both the view and the
template that draws the button read it — so a row can never show a control the view
would refuse. A row that is protected says so, rather than leaving a gap to wonder
about.

### The pilot

Fulfilled Academy is recorded with messaging and the full admissions pipeline
**off**, and fees, student records and classes **on** — by
`schools/migrations/0004_pilot_module_state.py`, which is keyed on the school's name,
guarded by an `exists()` so it is inert where that school is absent, idempotent, and
reversible.

It writes rows for a state the registry defaults would produce anyway, and that is
the point: a default is what the platform does when nobody has decided, and somebody
*has* decided here. Leaving it implied would mean a later change of default silently
switching a live school's features on, mid-term, with an SMS bill attached.

## Reports

The dashboards answer "how are we doing?" at a glance. Reports answers "show me".
Everything detailed enough to need a table or a second chart moved off the four
dashboards and onto `/reports/`, which each one now links to.

### One structure, two renderers

`apps/reports/reporting.py` builds a `Report` — headline figures, two charts, the
breakdowns, and the notes that keep them from being misread. The page and the PDF
each walk that same structure and neither computes anything, which is the only
reason a printed figure can be trusted to match the screen. A third renderer would
need no changes to the builder.

`apps/reports/structure.py` is the contract: `Stat`, `Chart`, `Segment`, `Table`,
`Column`, `Cell`. It imports nothing that knows about output. A `Cell` carries its
figure already formatted in both spellings it needs — `₦102,000` for the
screen, `NGN 102,000` for print — and `prose()` does the same for a sentence
with money in it.

### It defines nothing of its own

| Question | Answered by |
| --- | --- |
| What is a child expected to pay, and which state are they in? | `apps/students/fees.py` |
| What counts as money received? | `apps/payments/` via `apps/core/finance.py` |
| Who still owes? | `apps/payments/balances.py::outstanding` |

The report only *groups* those answers — by school, campus, class, method, month
— and formats them. `apps/core/finance.py` is new but not: `collection_summary`,
`with_outstanding` and `status_breakdown` moved there out of `core/views.py` and
grew a plural, so the four dashboards and the report read the same three functions.
`from apps.core.views import status_breakdown` still works.

### Two figures that are deliberately not the same thing

The report says this in its own notes rather than quietly picking one:

- **Collected**, wherever money is totalled, is every confirmed payment credited
  to the term. The till question, the same figure the bursar's dashboard shows,
  and it adds up: the campus rows sum to the headline.
- **Still owed**, on the outstanding table, is what the *current roster* has left
  to pay. The chase question. It parts company with expected-less-collected when a
  child who has paid has since left, or a parent has overpaid.

### Scope, not permission

`VIEW_PAYMENTS` gates the screen, and every role holds it — a bursar collects
against these figures, a principal and a proprietor answer for them. What differs
between roles is what the report *covers*, and the scoped managers have already
settled that before `resolve_scope` runs:

| Role | Covers | Picker |
| --- | --- | --- |
| Bursar / Principal | their campus | none — a select with one option is not a choice |
| School owner | every campus they run | campus |
| Platform owner | every school | school, then campus |

A school or branch id typed into the query string is matched against the
tenant-scoped querysets. An id that is not there counts as *not chosen* rather than
as an error, so a caller can neither widen their scope nor earn a 404 for trying
— they get their own report.

**There is no term picker, on purpose.** Every campus is measured against its own
current term. A proprietor mid-transition has two terms in flight, and pricing one
campus against another's calendar would overstate one and understate the other.

### Screens

| URL | What it is |
| --- | --- |
| `/reports/` | The collections report: six figures, two charts, seven breakdowns, and the small print. |
| `/reports/collections.pdf` | The same report as a download, carrying the same query string. |

The breakdowns are: by school (platform scope), by campus, by class, payment status
by class, how the money came in, what it was recorded against, when it arrived, and
the twenty largest outstanding balances. Each carries a footnote saying what it
leaves out, because every one of them leaves something out.

### The PDF, and the receipt that is not one

`ReceiptView` prints from the browser and deliberately does not generate a PDF, on
the grounds that a browser already knows how to print one page. That reasoning
holds for one page and stops holding for a report, which somebody files, emails to
a bank or hands to a board: it needs a fixed page size, repeating table headers,
page numbers and a filename, and none of those survive "Ctrl-P, save as PDF" intact
across four browsers. So ReportLab is a dependency, confined to
`apps/reports/pdf.py` — nothing else in the codebase imports it. Pillow arrives
with it and is not otherwise used.

**Why the PDF says `NGN` where the screen says `₦`.** The fourteen fonts every
PDF reader has built in are Latin-1, and `₦` (U+20A6) is not in Latin-1.
ReportLab does not refuse a character it cannot encode — it silently substitutes
ZapfDingbats — so the first draft of this export rendered every amount as a
dingbat, with nothing in any log to say so. Embedding a font for one character is a
file the school then has to keep shipped and licensed, so the report prints the ISO
code, which is what a bank statement does too. A test asserts no substitute font
ever appears in the output.

The charts are drawn: `StackedBar` is a ReportLab `Flowable` filling rectangles in
the app's own status colours, copied into `pdf.py` as hex because a PDF has no CSS
custom properties. They are the light-theme values, since paper is white. Every
segment drawn is also named and counted in a legend underneath — on paper as on
screen, hue is never the only signal.

### What the dashboards kept

Each keeps its summary figures and exactly one chart, plus anything that is an
*alert* rather than a breakdown — a campus with no current term, a class carrying
children with no fee against their name. Those are holes in the figures beside
them, not detail to go and look up.

| Dashboard | Keeps | Moved to Reports |
| --- | --- | --- |
| `/finance/` | Three money figures, the collection meter | The roster's four states, "What each class is worth" |
| `/school/` | Three counts, the four-state breakdown | "Your campuses" |
| `/branch/` | Three counts, the four-state breakdown | "Classes on this campus" |

## Admissions and enquiries

A paid add-on beyond the original scope. It is the pipeline a prospective child
travels from the moment their parent taps a link on Instagram to the moment they
appear on the register.

### An applicant is its own entity, not a draft student

This is the decision everything else follows from. A `Student` is a child on the
roll: billed, counted in the head count, present in every report. A child whose
parent filled in a form at midnight is none of those things, and the moment the
two share a table every `Student.objects` query in the platform starts returning
people who have never set foot in the school.

So `Applicant` is its own model, and enrolment is a **conversion**:
`apps/admissions/enrolment.py` creates the Student and links back to the
applicant, which keeps its whole history. `applicant.student` is the link;
`student.applicant` is the reverse.

### One record, many stages

An enquiry does not become a different row when it becomes an application. The
status advances and the application stage *adds* fields:

```
enquiry ── application ── assessed ── offered ── enrolled
   │                                     └───── rejected
   ├── expired
   └── withdrawn (from any open stage)
```

`ApplicationForm` is a ModelForm over the same row, and every field the parent
already gave is deliberately **absent** from it — there is nowhere on that
screen to retype a date of birth, so there is nowhere for the two copies to
disagree. What the screen does show, at the top, is a read-only "carried over
from the enquiry" panel, so the person filling it in can see the data is there.

Nothing is ever deleted. A rejected applicant, a lapsed enquiry and a family who
changed their mind are all facts about the intake — a school asking in March how
many enquiries January's post brought and how many converted needs the ones that
did not.

### The public enquiry page

`/<school-slug>/enquiry/` — the URL a school puts in its Instagram bio, its
WhatsApp status or on its own website.

It is the second place on the platform, after signup, that runs with **no tenant
context at all**. A parent is anonymous, so every scoped manager would return
nothing: an empty campus list, an empty class list. Every queryset there
therefore goes through `all_objects` and is narrowed by hand to the school in
the URL — and *that narrowing is the security boundary*. The school comes from
the slug, never from anything the browser posts, which is why posting another
school's branch or class id is refused rather than honoured.

**The page is branded to the school and to nothing else.** It does not extend
`base.html`, because that template puts the product name in the title and the
product mark in the header — right for staff, wrong for a parent who has come to
enquire about *their school*. The school's name is the title, its uploaded logo
is the mark, and SCHOOLCORD appears nowhere. A test asserts that.

The form is deliberately light — top of funnel only. Child's name, date of
birth, sex, campus, class wanted, parent name, phone, email, previous school and
how they heard about the school. A parent still deciding whether to enquire will
abandon a form that asks for a birth certificate.

Branch is a real, required field here, because the parent is outside the system
and nothing else can know which campus they mean. On the staff walk-in form at
`/admissions/new/` it is not asked at all: the person at the desk is standing in
the campus the parent walked into, so it comes from their account. Both forms
write to the same model, and differ only in `source`.

### The validity window

A fresh enquiry is good for a number of days set per school (21 by default), and
`expires_at` is stamped on the first save. Two nightly commands act on it:

```bash
python manage.py send_enquiry_reminders   # run this one first
python manage.py expire_enquiries
```

`send_enquiry_reminders` emails the parent once, after the school's own
`reminder_after_days` (7 by default). `reminder_sent_at` is stamped **only on
success**, so a send that failed is retried tomorrow rather than silently marked
done — and never more than once per enquiry, because a second and third
identical email is how a school's mail starts being marked as spam.

`expire_enquiries` closes what has lapsed. It only ever expires rows still at
the *enquiry* stage: once a family has filled in the application the window has
done its job, and expiring somebody mid-assessment would be the platform closing
a door the school is holding open. Both commands take `--dry-run`.

`applicant.has_lapsed` is derived, not stored, so the board is honest the moment
a window closes rather than only after the sweep has run.

### Requirements are data, per level

The client's rule is not one list. A passport photograph is asked of every
applicant; an entrance examination is asked of Primary and Secondary and never
of Nursery; the supporting documents differ again between a four-year-old
starting Pre-KG and a fifteen-year-old transferring into SSS 2.

So the requirement set is data rather than a chain of `if level ==`. Three
layers answer "what does this applicant owe us?", narrowest first:

1. the branch's own `RequirementSet` for that level,
2. the school's set for that level (`branch` null — one policy, every campus),
3. the defaults in `apps/admissions/requirements.py`.

`requirements.profile_for(level, school=, branch=)` is the only function any
caller needs. A school that has configured nothing gets a complete, sensible
answer; a school that disagrees with one line changes that line.

| Level | Photo | Birth cert. | Previous results | Transfer letter | Entrance exam |
| --- | --- | --- | --- | --- | --- |
| Nursery | required | required | not asked | not asked | **no** |
| Primary | required | required | required | optional | yes |
| Junior Secondary | required | required | required | required | yes |
| Senior Secondary | required | required | required | required | yes |

Nursery does not list previous results *at all* — not even as optional. A
four-year-old has no previous school to produce a report from, and offering the
upload would be asking for something that does not exist.

The assessment screen 404s for a level that does not sit one. Skipping the exam
is a URL that does not exist for that applicant, not a hidden button, so a
bookmarked link cannot create a record the level has no use for.

**Document fields are never hard-required on the form.** The front desk types
the application while the parent goes to photocopy the birth certificate, and a
form that refused to save until every paper was in hand would send them back to
the paper book this feature exists to replace. "Required" is enforced where it
matters — `applicant.missing_requirements`, which the detail screen lists and
the decision screen shows the principal before they decide.

#### The school edits them, not the admin

`/admissions/requirements/` lists what each level asks for and, for the school
owner, edits it. Previously the only way to change a `RequirementSet` was the
Django admin — so the policy belonged to whoever had a shell rather than to the
proprietor whose school it is.

Each line has **three** states, not two, because "optional" and "not asked" are
different answers: an optional transfer letter is requested and shown on the
checklist but never blocks a decision, while one that is not asked for is off
the form altogether. The entrance exam is the exception — it offers only
*sits it* / *does not*, since `requires_assessment` reads `is_required` and an
"optional exam" would be a line the pipeline ignores while the screen implies
it matters.

Saving writes the **school-wide** set (`branch` null). Campus-level overrides
remain a thing the model supports and `profile_for` resolves, but they are not
offered from a screen — one policy across every campus is the case schools
actually have. **Reset to default** deletes the set, so the built-in policy
applies again; it is POST-only, because a link a prefetch could follow must not
undo a school's configuration.

Only the school owner writes it (`manage_admission_requirements`, held by
school owner and platform owner). A principal runs the pipeline *under* the
rules and reads them; that is the line the client drew.

### Admission fees are not termly fees

`fees.FeeStructure` is what a class owes **per term** and is repriced every
term. What a family pays to come in is charged **once**, at the door, and
includes items — uniform, tracksuit, the book list — a returning child never
pays again. Sharing a table would mean every termly total silently gaining an
admission fee.

So they are separate models with separate seeders, and neither command can touch
the other's rows:

| | Termly | Admission |
| --- | --- | --- |
| Set | `fees.FeeStructure` | `admissions.AdmissionFeeSchedule` |
| Line | `fees.FeeComponent` | `admissions.AdmissionFeeItem` |
| Money | `payments.Payment` (needs a Student) | `admissions.AdmissionPayment` (needs only an Applicant) |
| Source | `apps/fees/pricing.py` | `apps/admissions/admission_pricing.py` |
| Seeder | `seed_fees` | `seed_admission_fees` |

`AdmissionPayment` exists because `payments.Payment` requires a `Student`, and
the whole point of an admission fee is that it is paid by a family who does not
have one yet. They stay separate after enrolment too, so a term's collection
figure never quietly includes intake money. Tests assert the separation from
both sides.

Line items carry a `kind`: `admission` (the one-time fee, and the only thing
enrolment is gated on), `intake` (tuition, uniform, sportswear, exam/dossier,
tracksuit) and `books` (the compulsory book list, quoted as a separate add-on
and excluded from "due at intake").

Totals are never stored. `compulsory_total`, `admission_total`, `books_total`
and `total` are all summed from the live line items, the same rule the termly
structure follows.

```bash
python manage.py seed_admission_fees --school "Fulfilled Academy" --branch "Main Campus"
```

**The seeder refuses to price a sheet whose figures it does not have**, and
names the missing lines instead. Seeding a zero would put a real-looking 0 in
front of a parent and inventing a plausible figure would put a wrong one there;
both are worse than a command that says which number it is waiting for. Fill the
amounts into `admission_pricing.py` (replace each `PENDING` with `naira(...)`)
and re-run.

Where the school's own written total disagrees with its line items, the seeder
charges the line items and prints a warning — no balancing line is invented.
That is how Primary 1's 2,000 gap is handled in the termly file, and it is how
the Pre-KG–KG3 gap is handled here: the sheet's written total is 81,000 while
its own items sum to 82,000, and `client_total` records that so the warning
fires the moment the figures land.

### Enrolment

On an offer plus a confirmed admission fee, `enrolment.enrol()` creates the
Student in one transaction: names, sex, date of birth, parent contact and
address copied across, admission number suggested from the branch's own series
(`AS/2026/008` — the school's initials, the year, the next free number), class
taken from the offer unless the front desk places them elsewhere.

`enrol` calls `student.full_clean()` with **nothing excluded**, and that is
load-bearing. The roster's uniqueness rule is
`UniqueConstraint(Upper("admission_number"), "branch")`, and Django skips a
constraint that mentions an excluded field — excluding `branch` (the obvious
thing to do, since it is derived rather than typed) would silently turn the
check off and let a duplicate through to the database as a 500.

`enrolment.can_enrol()` returns *every* blocker as a list rather than raising on
the first, so the screen shows all of them at once instead of revealing them one
refresh at a time. The payment gate is a per-school setting: a school that
admits first and collects afterwards turns it off.

### Who does what

| | Owner / Principal | Bursar |
| --- | --- | --- |
| See the pipeline | yes | **configurable**, off by default |
| Move an applicant along | yes | never |
| Offer or refuse a place | yes | **never**, whatever is configured |
| See admission fees | yes | yes |
| Record an admission payment | yes | yes |

Every other capability on the platform is a fixed property of the role. This one
is not, because whether a bursar sees applicants is the *school's* decision —
some run the front desk out of the bursary, most do not. So
`AdmissionsConfig.bursar_can_view_applicants` widens *viewing* only, and
`apps/admissions/access.py` is the one place that ordering is expressed: the
role table is the ceiling, and the school's toggle can only ever move a bursar
up to reading the board.

The sidebar consults the same answer the views do (`NavItem.capability`), so it
never offers a bursar a link that would 403.

### What the parent receives

Three emails, all signed by the school. The platform is named nowhere in the
body, and the one place it cannot be hidden — the envelope sender — carries the
school's *display name* over our address, with `Reply-To` set to the school's
own office email so a parent hitting reply reaches the school. Sending as
`@theschool.com` from our servers would fail that domain's SPF and land in spam,
which helps nobody.

| Email | When |
| --- | --- |
| Acknowledgement | immediately on a public submission — carries the reference and the closing date |
| Reminder | once, inside the window |
| Decision | on an offer or a refusal, if the person deciding asks for it |

The offer and the refusal are two templates, not one with the adjectives
swapped. An offer tells the family what to do next and what is payable at
intake; a refusal is short, says the decision is final for this intake, and does
not dangle a reconsideration the school has not offered.

Set `PUBLIC_BASE_URL` so the reminder can build an absolute link back to the
school's enquiry page. Left blank the link is omitted rather than sent broken.

### The screens

| URL | What it is |
| --- | --- |
| `/<school-slug>/enquiry/` | The school's public enquiry page. Unauthenticated. |
| `/admissions/` | The pipeline board — every applicant grouped by stage, filterable by campus, level, class and status. |
| `/admissions/new/` | Walk-in enquiry, branch taken from the staff account. |
| `/admissions/<id>/` | One applicant: what is captured, what is outstanding, what is next. |
| `/admissions/<id>/application/` | Enquiry to application. Adds fields, re-asks for none. |
| `/admissions/<id>/assessment/` | Schedule or record the entrance exam. 404s for a level that does not sit one. |
| `/admissions/<id>/decision/` | Offer or refuse. Owner and principal only. |
| `/admissions/<id>/enrol/` | The conversion. |
| `/admissions/<id>/payment/` | Record an admission payment. |
| `/admissions/fees/` | The intake fee sheets and what has been collected. |
| `/admissions/settings/` | The window, the reminder, and who sees the pipeline. |

## The public side and signing in

Everything above assumed you were already signed in. This is how you get there.

| URL | What it is |
| --- | --- |
| `/` | Landing page. Public; a signed-in visitor is redirected to their dashboard. |
| `/signup/` | Creates a School, its first Branch and the owner User, then signs them in. |
| `/welcome/` | The four-step onboarding checklist. |
| `/accounts/login/` | Sign in with an email address or a username, landing each role on its own dashboard. |
| `/accounts/settings/` | Account settings: anyone's own email address and password. In every role's sidebar. |
| `/privacy/` | The privacy policy. Public, and readable signed in as well as out. |
| `/accounts/password_change/` | Redirects to Account settings. |
| `/accounts/password_reset/` | Django's reset flow; the console backend prints the link in dev. |
| `/dashboard/` | Not a screen — forwards to whichever dashboard the role belongs on. |

### The landing page

`templates/core/landing.html`, in order: hero, the problem, the answer to it,
how setup goes, questions, the ask, the footer. The sticky bar
(`partials/_lp_nav.html`) jumps to the middle four, so the page is both a scroll
and a menu. Smooth scrolling and the offset that keeps a heading clear of the
bar are declared in CSS (`scroll-behavior` / `scroll-padding-top`), so the links
work with the keyboard, in a new tab, and with the page's JavaScript blocked;
`js/ui.js` only adds the bar's scrolled state and the `aria-current` that marks
the section being read.

The hero's floating cards (`core/_hero_cards.html`) are **not screenshots**:
they are real fragments of the interface, built from the same tokens,
components and payment-status colours the app itself uses. A screenshot goes
stale the week after it is taken and cannot follow the theme; this follows both
for free.

The classroom background is treated entirely in CSS, so swapping the
photograph changes one attribute and nothing else. `.hero-photo` throws the
original colour away (`grayscale`), re-tints it to a single hue and rotates
that hue to the brand blue (`sepia` + `hue-rotate` — a plain `saturate` cannot
move a hue, only weaken it), then darkens and blurs. The supplied photograph is
lit warm gold; none of that survives.

`.hero-wash` adds a left-hand scrim over the result, and that one is load-
bearing rather than decorative: the headline column sits on the left and the
classroom's sunlit window wall lands directly under it, so without the scrim
the brightest part of the picture is exactly where the white type goes.

The blur is **atmosphere, not a privacy mechanism** — a CSS filter is a
rendering choice and can be switched off. The rule lives on the asset instead,
documented on `LandingView.HERO_IMAGE`: an empty room, desks or a chalkboard,
never an identifiable child. Set that constant to `None` and the template omits
the `<img>` rather than pointing at a placeholder; the gradient underneath is a
finished hero on its own.

One upside of blurring at that radius: the asset does not need to be large.
The committed file is 1024×685 and ~123 KB, and upscaling it across a wide
display is invisible under a 10px blur.

Every section below the hero carries `.lazy-section` (`content-visibility:
auto`), so the browser skips its layout and paint until it is nearly on screen
— the cheapest form of lazy-loading a section, with no script and no
placeholder. Reveals are armed from JS against markup that ships **visible**, so
the failure mode of the whole effect is "no animation", never "no content".

### Branches, Staff and Terms are screens, not admin links

> **This happened a third time.** Onboarding step 4 — "Invite staff" — still
> pointed at `admin:accounts_user_changelist`, so a proprietor following their own
> setup was asked to authorise, on a site they were already signed in to. It points
> at `/staff/` now, which is capability-gated, so a role that may not invite gets a
> 403 with a toast rather than a login form. `tests_bugs3.OnboardingStepFourTests`
> walks every step as the owner and asserts each one answers 200.


These three sat in the sidebar as Django admin URLs, which meant the proprietor
— whose account is deliberately **not** `is_staff` — followed a link in their
own sidebar to the admin login page. Granting `is_staff` would have been worse:
the admin is not tenant-scoped, so it would have put another school's rows one
URL away.

| URL | What it is | Who |
| --- | --- | --- |
| `/branches/` | The school's campuses, with add and edit. | Read: owner, principal. Change: owner and platform (`manage_branches`). |
| `/staff/` | Who can sign in, and as what. | Read: owner, principal (`view_staff`). |
| `/fees/terms/` | Terms, including which one the school is in now. | Read: everyone who reads fees. Change: `manage_fees`. |

Fee structures needed no change — both the platform owner and the school owner
already held `view_fees` and `manage_fees`. `apps/core/tests_role_access.py`
signs in as each role and follows the sidebar links, asserting on what comes
back and that no href is an `/admin/` URL; that is the check the suite lacked,
because every existing test asked whether the *view* allowed the role and the
view in question was Django's.

#### Creating staff accounts

`/staff/new/` is where a proprietor gives a colleague a login. Before it, every
account after the first came from somebody with a shell running
`invite_school_owner` — fine for the first owner, since there is nobody at the
school yet, and wrong for everyone after.

* **No password field.** The account gets a long random password nobody sees —
  not even the proprietor creating it — and the person sets their own from the
  emailed link, the same `send_set_password_email` the invite command uses and
  the same message "forgot password" produces. Random rather than unusable:
  Django's reset form skips accounts with an unusable password, which would
  shut the only door.
* **Permission role and job title stay separate.** The role decides access; the
  title is what they do. A bursar promoted to "Head of Finance" gains nothing.
* **Assignable roles are school owner, principal and bursar.** `platform_owner`
  is absent from the choices *and* rejected in `clean_role`, so a hand-rolled
  POST cannot mint one. Platform staff come from `create_platform_owner`.
* **Tenant-scoped by construction.** The school is never a form field — it is
  read from the signed-in account — so a posted `school` is ignored, and the
  campus dropdown runs through the scoped manager, so another school's campus
  is not selectable and is rejected if sent by hand.
* A branch-scoped role (principal, bursar) must name a campus; an owner account
  stores none, because an owner sees them all.

The invite email is sent **outside** the transaction that creates the account:
a mail failure must not roll back a user the proprietor has just been told
about. If it fails they are warned, and **Resend invite** on the staff list is
the fix — itself scoped, so an id from another tenant is a 404 rather than an
email to a stranger.

`apps/accounts/tests_staff_provisioning.py` follows the emailed link to the
end: sets a password, signs in with it, checks the new bursar lands on the
screens their role allows and is refused the ones it does not, and that the
link is single-use.

### The privacy policy

`/privacy/` is written for Nigeria's NDPA: the **school is the data
controller**, SCHOOLCORD is the **processor**, plus what is held, who it is
shared with, how long it is kept and how to ask to see, correct or delete it.
The text lives in `templates/core/_privacy_body.html` and is included twice by
`core/privacy.html` — once inside the signed-in shell, once on the signed-out
page — so it exists in one place.

It is linked from the landing footer and the signup form, and listed in
`sitemap.xml` and `llms.txt` through `seo.PUBLIC_PAGES`. "Last updated" is
`PrivacyView.POLICY_UPDATED`, a constant: a template printing today's date
would claim a review that never happened. `PRIVACY_EMAIL` in
`apps/core/branding.py` is the contact address, deliberately not the no-reply
sender.

> It is a plain-language baseline, not legal advice — have it reviewed before
> the school relies on it.

### One dashboard per role

"The dashboard" is a different question for each role, so there are four
screens rather than one with three quarters hidden:

| Role | Lands on | Answers |
| --- | --- | --- |
| Platform owner | `/platform/` | Every school as a card you open — see [the platform owner's screens](#the-platform-owners-screens). |
| School owner | `/school/` | Every campus they run, side by side. |
| Principal | `/branch/` | Their campus: enrolment, classes, what is unpriced. |
| Bursar | `/finance/` | What the term is worth, and how much of it is in. |

Each keeps its summary figures and one chart; the breakdowns live on
[Reports](#reports), which every dashboard links to.

`ROLE_HOME` in `apps/core/navigation.py` is the single source for that mapping.
Both the post-login redirect and the sidebar's "Dashboard" entry read it, so
the link in the shell and the page you land on after signing in cannot drift
apart — a test asserts they agree for every role. `next=` still wins when
present, so a deep link survives the login page.

Reaching another role's dashboard by typing its URL is not a leak — the scoped
managers narrow it either way — but it is a screen answering someone else's
question, so `RoleDashboardMixin` sends you to your own instead.

The effective role comes from `TenantContext.from_user`, not `user.role`, which
is what sends a Django superuser to the platform overview rather than to
whatever role happens to be stored on their row.

### Signup is the one place without a tenant

There is no tenant yet, so the scoped managers would return nothing and a
uniqueness check that cannot see existing rows is not a uniqueness check.
`SchoolSignupForm` therefore uses `all_objects` throughout, and creates the
school, the branch and the owner in one transaction — a signup that produced a
School with no owner would leave a tenant nobody can sign into and no screen
from which to fix it.

The owner is created with **no branch**: they see every campus, and pinning them
to the first one would narrow them the day they open a second. The username is
derived from the email (they never type one), with a numeric suffix on collision
rather than a rejected signup.

### Onboarding progress is derived

`views.setup_progress()` asks the data — are there classes, fee structures,
students, colleagues? — rather than reading a "setup complete" flag. A flag
would go stale the moment someone deleted their last class, and the school would
be told it had finished a step it had not.

### Your own account

`/accounts/settings/` carries three forms, told apart by a hidden `form` field so
a rejected one does not wipe a half-typed other:

| Form | Fields |
| --- | --- |
| **You** | First name, surname, job title |
| **Password** | Old, new, new again — against `AUTH_PASSWORD_VALIDATORS` |
| **Email address** | New address, confirmed with the current password |

The name and job title used to be editable only in the Django admin, which a
proprietor cannot reach — so a school whose owner was invited as "Owner" with no
surname had no way to correct it, and the sidebar, the staff list and every report
footer carried the wrong name indefinitely.

**Role, school and campus are deliberately absent** from that form. They decide
what the account can see, so they belong to whoever manages staff; a profile form
you could promote yourself from would not be a profile form. A test posts
`role=platform_owner` and `is_superuser=on` to it and asserts nothing moves.

`job_title` stays free text and is never read to decide access — see
[Permission role vs. job title](#permission-role-vs-job-title).

### Signing in, Account settings and temporary passwords

**The split panel.** Sign in, sign up and the four password-reset stages all
sit in `registration/_auth_base.html`: a branded gradient panel on one side, the
form on the other. Sign-in keeps the visual panel on the left; sign-up reverses
it. That reversal *is* the swap — both pages give the same two halves the same
`view-transition-name`, so navigating between them makes the browser move each
panel from where it was to where it now is. Below `lg` there is no split at all
and the form column takes over, so nothing depends on the two-column shape
existing.

**Email or username.** `apps/accounts/backends.py` signs people in with either.
The username is tried first, so every existing login keeps working. An email
matches case-insensitively, but only when exactly one account has it. Email is
not unique at the database level, and a login that picked one of two accounts
would sign someone in as a stranger. The username is derived from the email at
signup and does **not** follow a later email change. Matching on email is what
keeps a corrected address from stranding anyone's login.

**Account settings** (`/accounts/settings/`, "Account settings" in every role's
sidebar) is where anyone changes their own:

* **email address.** It asks for the current password, so an unattended session
  cannot point the account's reset emails elsewhere. An address another account
  already uses is refused.
* **password.** Django's `PasswordChangeForm`, so the new password goes through
  `AUTH_PASSWORD_VALIDATORS`, the same NIST-aligned policy signup and the reset
  flow use. Django's own `/accounts/password_change/` redirects here. The
  admin's password page is separate and unchanged.

**Temporary passwords.** `User.must_change_password` marks an account that was
given a password it did not choose. `RequirePasswordChangeMiddleware` sends
every request from such an account, the admin included, to Account settings,
except:

* Account settings itself
* the password generator
* signing out
* the reset-by-email flow
* static files

The flag is cleared inside `User.set_password`, so choosing a password by any
route ends it: Account settings, the reset email, or the admin. A platform owner
can also tick it in the admin to force a change.

### Creating real accounts

A password is never a command-line argument, where it would sit in shell
history and process listings.

| For | Command | How the password is set |
| --- | --- | --- |
| Yourself, as platform owner | `create_platform_owner --email … --name "…"` | Typed at a hidden prompt, twice, and validated. |
| A platform owner whose real email you have | `create_platform_owner --email … --name "…" --email-link` | Random and never shown; they are emailed the reset link. |
| A school owner whose real email you have | `invite_school_owner --school <slug> --email … --name "…"` | Random and never shown; they are emailed the reset link. |
| Anyone whose real email you do not have yet | add `--temporary-password` (and `--username` for a neutral one) | You type a temporary password at the prompt. It must be changed at first sign-in, and they correct the email on Account settings. |

Platform owners are superusers with no school, and land on `/platform/`.
`invite_school_owner` only attaches to a school that exists and never creates
one, so a mistyped slug is an error, not a duplicate tenant. Emailed links use
`PUBLIC_BASE_URL`; in production that is `https://www.theschoolcord.com`, and
the mail itself goes out through Resend (see "How mail is sent"). The
dev settings print mail to the console. So either send from production, or pass
`--no-email` and have the person use "Forgot password" on the live sign-in page.

### Accounts in production

Cleaned up on 2026-09-14:

* **Demo accounts removed:** `platform.owner` and `fulfilled-academy.owner`,
  `.principal` and `.bursar`. Their shared password had been published in this
  README.
* **Fulfilled Academy's seed data deleted:** students, fee structures and
  components, the term, classes, subjects and payments.
* **Kept for real onboarding:** the school itself, its Main Campus branch, and
  its messaging identity (Sender ID "Fulfilled").

Do not run `bootstrap_tenant` or any `seed_*` command against production.

### Trying it locally

```bash
python manage.py bootstrap_tenant --password "<a local password>"
```

Then sign in as `demo-school.owner`, `.principal`, `.bursar` or `platform.owner`
with that password, by username or by the `@example.com` address each one is
given. In dev the console backend prints password-reset emails, link included.

## Branding and identity

### Addresses and accounts

Three constants in `apps/core/branding.py`, handed to every page — signed out
included — by the branding context processor:

| Constant | For |
| --- | --- |
| `CONTACT_EMAIL` | `hello@theschoolcord.com`. The general way in: the footer, the About page, the system-email footer. |
| `PRIVACY_EMAIL` | `privacy@theschoolcord.com`. A statutory contact point named in the policy, kept separate so it does not collect sales questions. |
| `SOCIAL_LINKS` | X, Instagram and email, in the order the footer draws them. |

`SOCIAL_LINKS` is one list read by both the footer and the About page, so an
account added there appears on both and neither can carry a link the other does
not. Each glyph is `templates/partials/_social_icon.html`.

The URLs are the **share links as supplied**, query strings and all — `?s=11` on X
and `stkn=…&utm_source=qr` on Instagram. Both are what those apps hand you when you
share a profile from a phone, and both are kept on purpose: the tail is the
platform's own attribution, and stripping it would quietly drop whatever it reports
back.

The `&` in the Instagram URL is a literal ampersand in `branding.py`. Django escapes
it to `&amp;` on the way into the `href`, which is correct HTML and resolves to the
same address — so a test asserting on the rendered page has to escape it too, and
they do.

### One name, one file

`apps/core/branding.py` holds `PRODUCT_NAME` and nothing else does. Settings
imports it for `DEFAULT_FROM_EMAIL`, `config/urls.py` for the admin header, and
a context processor puts it on every page — including the signed-out ones,
which is why it is a separate processor from `core.context_processors.tenancy`:
that one returns early for anonymous visitors, and the landing page and login
form are exactly where the product's name matters most.

The password-reset **email** is the one surface a context processor cannot
reach. Django renders it from the form, with a plain context and no request, so
`{{ product_name }}` there would silently render as nothing and ship "Reset
your  password" to a customer. `BrandedPasswordResetView` hands it in through
`extra_email_context`, and a test asserts the subject has no double space in it.

### The comment bug this pass fixed

Django's `{# ... #}` is **single-line only**. A comment that wraps onto a second
line is not a comment at all — the lexer never recognises it, and the browser
prints the developer's prose to the user. Twelve templates had one; the login
page was telling every visitor what Django does with a bad password.

Nothing fails when this happens: the template is valid, the page renders, the
tests pass. So there are two guards, and they work differently on purpose:

* `TemplateCommentSyntaxTests` reads every template's **source** and fails with
  the file and line number of any `{#` whose `#}` is on another line;
* `RenderedPageTests` reads the **rendered HTML** of every public page and
  fails on any `{#`, `#}`, `{%`, old brand name, `TODO` or lorem ipsum.

Multi-line comments belong in `{% comment %}...{% endcomment %}`, which is what
all twelve became.

### The school logo is the proprietor's

`MANAGE_SCHOOL_LOGO` is granted in `_SCHOOL_ADMIN` — the school owner and the
platform — and not in `_LEADERSHIP`, so a **principal may correct the school's
name or its office number and may not change the mark that goes on every receipt
a parent is handed.**

`SchoolProfileForm` takes `can_manage_logo` and *removes* the field rather than
disabling it. A disabled input still looks like a control and still posts nothing,
so a principal would meet a file picker that silently did not work; removing it
means the form cannot accept a logo from them even if one is posted by hand —
which a test does.

**Finding it was the reported problem**, not doing it. Two changes:

* `templates/core/_logo_field.html` replaces `{{ form.logo }}`. Django's
  `ClearableFileInput` rendered "Currently: logos/…/file.png [ ] Clear  Change:
  …" as loose text and inputs with nothing wrappable around them — a file path
  where a school expected to see its own logo. The control now shows the logo at
  the size it is used, with a `<label for>` styled as the button beside it. The
  label *is* the button, so a pointer and a keyboard land on the same real input
  with no click handler.
* The school dashboard carries a prompt while there is no logo and the account may
  add one, linking to `/settings/#logo`. It disappears the moment one is uploaded.

Removal is our own `remove_logo` checkbox rather than `ClearableFileInput`'s,
because the widget it belongs to is gone. A new file wins over the tick: somebody
who chose a replacement and left the box checked meant to replace it.

### Logos

Two different marks, never interchangeable: **SCHOOLCORD's**, which says whose
software this is, and **the school's**, which says whose school you are looking
at.

| Partial | What it draws |
| --- | --- |
| `partials/_brand_mark.html` | SCHOOLCORD's official logo, from `static/img/`. |
| `partials/_school_logo.html` | A tenant's uploaded logo, or its initial square. |

#### The product mark

`static/img/schoolcord-logo.svg` is the primary asset, with
`schoolcord-logo.png` as the fallback. Two points with one cord between them —
the school and the parent on the same line — drawn in the brand tokens
(`#EEF3FB` ground; `#0F2A52` / `#1B4680` / `#2E63B0` marks), so it sits inside
the palette rather than beside it. A test asserts those four hex values are
still in the file, so a logo swapped in later cannot quietly drift off-palette.

It appears on the landing page, login, signup, password reset, onboarding, the
platform dashboard, the sidebar of every signed-in screen, the favicon and the
password-reset email.

Served from `static/` rather than inlined: it is a real asset with a filename
now, so one file gets replaced when the mark is revised, and browsers cache one
request instead of re-parsing the same markup on every page. It carries its own
light ground, so unlike the placeholder it replaced it needs no dark/light
variant — it reads on the brand-900 sidebar and the auth panel's gradient
alike.

Format per surface, because the constraint differs:

| Surface | Format | Why |
| --- | --- | --- |
| Screens | SVG, `onerror` → PNG | Crisp at any size; the fallback covers a blocked or missing SVG. |
| Favicon | SVG, `alternate icon` → PNG | Every current browser prefers SVG and stays sharp at 16px. |
| iOS home screen | PNG (`apple-touch-icon`) | Schools pin the dashboard on phones. |
| Email | PNG only, absolute URL | Mail clients do not render SVG, and have no site to be relative to. |

#### The school's mark

`School.logo` is an optional upload; without one, a school renders as its
initial in a coloured square — the same pattern the roster already uses for
students. That fallback is the answer, not a placeholder: most schools will
never upload a logo, and an initial square looks deliberate where a broken
image does not.

A school's own logo appears in its sidebar, on the platform roll, and on
printed payment receipts — where the print stylesheet forces
`print-color-adjust` so the fallback square is not dropped as a background
colour.

### How mail is sent

Production sends through **Resend**, over HTTPS, via `django-anymail`. It used
to use Django's SMTP backend, which did not fail so much as *hang*: the host
blocks outbound port 587, so a password reset held a web worker until the
request timed out and read to the user as a broken site.

| Setting | Value |
| --- | --- |
| `EMAIL_BACKEND` | `anymail.backends.resend.EmailBackend` (production) |
| `ANYMAIL["RESEND_API_KEY"]` | From `RESEND_API_KEY` in the environment. |
| `DEFAULT_FROM_EMAIL` | `SCHOOLCORD <noreply@send.theschoolcord.com>` |

The From: address must be on **`send.theschoolcord.com`** — the subdomain
verified in Resend. The root domain is not verified, and Resend refuses to send
from an unverified sender, so `@theschoolcord.com` would fail every reset. The
address lives with the rest of the product's identity, in
`apps/core/branding.py`.

There are deliberately no `EMAIL_HOST`/`EMAIL_PORT`/`EMAIL_HOST_USER`/
`EMAIL_HOST_PASSWORD` settings any more: Django reads those only for the SMTP
backend, so keeping them would be live-looking configuration that nothing uses.
Any left on the host are ignored. Dev still prints mail to the console.

`apps/core/tests_email.py` asserts all of it, including a password reset
arriving at Resend's API with the network stubbed and `smtplib` rigged to fail
the test if anything reaches for a socket.

### Branded system email

`templates/email/_base.html` is the shell for every message SCHOOLCORD sends **as
itself** — a password reset, an invitation, anything about somebody's own account.
Deliberately not the same base as `templates/admissions/email/_base.html`, which is
signed by the *school* and where the platform appears nowhere: a parent who found a
school on Instagram has never heard of us.

It is written like email rather than like the rest of the app, and each of those is
a constraint rather than a preference:

- **tables for layout** — Outlook does no flexbox or grid, and a `<div>` column
  collapses in it;
- **every style inline** — mail clients strip `<style>` blocks and class
  attributes, so the design tokens are repeated as literal hex. Same trade the
  admin skin makes, for the same reason: there is no stylesheet to read from;
- **no web font** — Public Sans is named first for clients that have it locally,
  then the stack falls back;
- **`width` attributes *and* inline widths** on images, because Outlook reads the
  attribute and ignores the style.

The header is a brand-navy band with the **logo as a PNG at an absolute URL**. SVG
is unsupported in almost every mail client, and a relative path has no site to
resolve against once the message has left. The address is built from `protocol` and
`domain`, which every sender supplies. The wordmark sits beside it as live text and
the alt text reads "SCHOOLCORD" rather than "logo", so a client that blocks remote
images still leaves the brand on the page.

Mobile: one 560px column that shrinks to the viewport, 16px body copy (anything
smaller and iOS Mail zooms, which reflows the table), and a button with a 44px tap
area built the `mso-padding-alt` way so Outlook gets the same rounded fill.

Every message stays **multipart** — the text half is the body, the HTML is the
alternative — so a client that refuses HTML gets a complete message rather than a
blank one. The sender name stays `SCHOOLCORD`.

### When mail does not arrive

The welcome email is best effort by design — a failed send must not turn a
completed signup into an error page — and the first version of that was a mistake:
"best effort" came to mean the misconfiguration stayed hidden too. Two things fixed
that.

**`apps/core/checks.py`** turns the silent cases into deploy-time warnings, run by
`manage.py check --deploy` and by every `manage.py` command:

| Check | Fires when |
| --- | --- |
| `schoolcord.W001` | `PUBLIC_BASE_URL` is unset, so mail cannot build links — and the welcome email is skipped entirely |
| `schoolcord.W002` | the backend is Anymail/Resend but `RESEND_API_KEY` is empty |
| `schoolcord.W003` | `DEFAULT_FROM_EMAIL` has no address in it |
| `schoolcord.W004` | uploads are going to local disk in production |
| `schoolcord.W005` | `PLATFORM_OWNER_EMAIL` is unset, so no account is protected from deactivation |

All warnings, never errors: none of them stops the app serving pages, and a check
that refused to boot over an unsendable email would be a worse failure than the one
it reports.

**`manage.py mailcheck`** answers the question from the machine that was supposed
to send:

```bash
python manage.py mailcheck                              # what is configured
python manage.py mailcheck --to you@example.com         # send a plain test
python manage.py mailcheck --to you@example.com --welcome   # send the real thing
```

It prints the backend, the key, the sender domain and the base URL, then — with
`--to` — sends through the real backend with `fail_silently` off, so the
provider's own error surfaces instead of a swallowed one. `--welcome` renders the
genuine welcome email against an unsaved school, so what lands in the inbox is what
a new owner gets and nothing is left in the database.

**The three things that actually stop it arriving**, in the order they turn up:
`PUBLIC_BASE_URL` not set on the host; `RESEND_API_KEY` not set; and the sender
domain not verified with Resend, which refuses the send outright. The spam folder
is worth checking before any of them.

### The welcome email

Sent on signup by `apps/core/emails.py`, on the same shell as the reset mail. It
welcomes the owner, names their school and first campus, lists the four setup steps
and links to `/welcome/` absolutely.

**Not a verification step, and that is the whole design.** The account works the
moment signup finishes — the owner is signed in before this is even called — and
nothing in the message gates anything. A "confirm your email" mail that confirmed
nothing would teach people to expect one that does.

**Best effort.** The school exists, the owner is signed in, and the email is the
least important thing that happened in that request. A backend that throws is
logged with its traceback and swallowed; a deployment with no `PUBLIC_BASE_URL` is
warned about and the message is *not* sent, because every link in it would be
relative and a relative link in an email goes nowhere. Signup succeeds either way,
and a test asserts it.

The four steps are listed in `WELCOME_STEPS` rather than read from
`onboarding_steps`, because that function counts rows to decide what is *done* and
this message goes to a school that has none of them yet — it needs the ladder, not
a progress report. A test asserts the two lists still describe the same four steps.

### The password-reset email

Now multipart. The plain-text body Django has always sent is unchanged and
still stands on its own; alongside it goes an HTML half with the logo in the
header, written like email rather than like the rest of the app — tables for
layout, every style inline, palette as literal hex, because mail clients strip
`<style>` blocks and Outlook does not do flexbox.

Clients that block remote images fall back to the `alt`, which is why it reads
`SCHOOLCORD` and not "logo".

It is a `FileField` with an image validator rather than an `ImageField`, which
would pull in Pillow to read a header. The receipt upload on `payments.Payment`
made the same trade.

Upload it at **Settings** (`/settings/`), which is also what the sidebar's
Settings entry has been pointing at since the shell was built. The onboarding
page prompts for it from the school card at the top.

The school profile is deliberately **not** an onboarding checklist step: signup
already sets the name, and a logo is optional, so any "done" test would either
tick itself the moment the account existed or could never be ticked at all.

### The admin

Lightly skinned in `templates/admin/base_site.html` — brand blue through
Django's own theme variables, the product name from `site_header`, and the
house 8px corner. Not a rebuild: it is an internal tool, and the hex values are
literal because the admin does not load `app.css`.

### Demo data is obviously demo

The seed files are demo data carrying the pilot school's name, not a record of
the school: nobody in `roster.py` is a real person. That covers the roster,
`pricing.py`, `curriculum.py` and `seed_payments`. They exist so the screens have
something realistic to show locally, and production no longer holds any of it
(see [Accounts in production](#accounts-in-production)). What changed in this
pass is everything that was *filler* dressed as real:

* `bootstrap_tenant` now creates **"Demo School"**, not "Northgate Academy".
* The signup form's example is "Bright Future Academy", not the pilot school's
  actual name.
* Student and parent form placeholders are role words ("Surname", "Parent or
  guardian name") rather than invented people.
* **Every demo parent phone number moved to a sequential `0800` block.** They
  were valid eleven-digit numbers on live Nigerian mobile prefixes — point
  messaging at a real gateway with the seed loaded and fee reminders go to
  strangers. `0800` is a Nigerian toll-free service range, not a handset range:
  it satisfies the phone validator, reads as obviously invented, and cannot be
  delivered to. A test asserts every one of them starts `0800`.

Parent emails were already on `example.com`, which is reserved for this.

## Search engines and AI assistants

What crawlers see is driven by the tables in `apps/core/seo.py`, so the page
`<head>`, `robots.txt`, `sitemap.xml` and `llms.txt` cannot drift apart.

| URL | What it is |
| --- | --- |
| `/robots.txt` | Allows the public pages; disallows the admin, every signed-in area, `/media/` and each school's `/<slug>/enquiry/` page. Googlebot, Bingbot, Google-Extended, OAI-SearchBot, ClaudeBot, PerplexityBot and friends are named, with the same rules as `*`. |
| `/sitemap.xml` | The public pages, from Django's sitemap framework. |
| `/llms.txt` | A short Markdown summary of the product and links to its public pages. |

* **Public pages** are `PUBLIC_PAGES`: the home page, signup and sign-in. Each
  has its own `<title>`, meta description, Open Graph and Twitter tags. The
  home page also carries schema.org JSON-LD for the Organization and the
  SoftwareApplication. Other pages fall back to `PRODUCT_DESCRIPTION`.
* **A new app** needs its URL prefix in `PRIVATE_PATHS`, or its page in
  `PUBLIC_PAGES`. `apps/core/tests_seo.py` walks the URLconf and fails on a
  route that is in neither list.
* **Absolute URLs** (canonical, `og:image`, the sitemap) use `PUBLIC_BASE_URL`,
  so the Railway domain points search engines at `https://www.theschoolcord.com`
  instead of competing with it. Left blank, they fall back to the request.

## Navigation

`nav_for(role)` in `apps/core/navigation.py` is the server-side equivalent of a
client `navFor(role)`. A context processor injects the result, and
`templates/partials/_nav.html` renders it for both the desktop sidebar and the
mobile drawer. Links the role may not use are never emitted; destinations that
don't exist yet render greyed out with a "soon" tag and light up on their own
once the URL name exists.

Two entries point at one screen on purpose. `staff:list` is **Staff** for a
school and **Users** for the platform, because the same list at platform scope is
every account on the product across every school — and it then shows a School
column and the deactivate control. The view reads the caller's scope; nothing in
the template reads a role.

**A platform owner's menu is not a school's menu with extra rows on it.**
Admissions and Messaging are a school's own work — running a pipeline, writing to
its parents — so neither appears in the platform sidebar. Sender IDs does, and
moves up into the Platform section beside Schools, because registering one with the
gateway and approving it is the platform's job and a school can only read its own.
The routes stay reachable: it is the *menu* that is scoped, not the permission, so
the platform can still open a school's pipeline to help them through it.

**Two entries pointing at one href used to light up both rows.** The platform
owner's Dashboard and their Schools entry both resolved to `/platform/`, and
`_keep_only_the_closest_match` compares href *lengths* — so a tie cleared neither,
and clicking through left the previous row highlighted beside the new one. The
duplicate row is gone (their home *is* Schools, so they have one entry for it, not
two), and the tie-break now keeps the first declared rather than keeping both. A
test asserts exactly one row is active on every screen each role can reach.

**School settings** is offered to the school-side roles only. A platform owner has
no school of their own, so `/settings/` raised a 404 for exactly the account whose
sidebar was offering it; they reach each school's profile from Platform › Schools
instead, and a bookmark or a typed URL now redirects there rather than hitting a
wall.

Three gates, and an entry has to pass all three: the **role**, the **capability**
it declares (if any), and the **module** it belongs to (if any) — see
[Modules](#modules-what-each-school-has). Both sets default to empty, so a caller
that does not pass them gets the role-only menu with every gated entry hidden. That
is the safe direction: a menu offering too little is a bug somebody reports, and one
offering too much is a door the middleware then has to slam.

## Design tokens

All declared in `assets/app.css` under `@theme` (Tailwind v4), so each token
is both a utility and a CSS custom property.

- **Brand** — deep blue, `brand-50` … `brand-950` (`brand-900` = `#0f2547`).
- **Payment status** — `paid` (green), `partial` (amber), `unpaid` (slate),
  `overdue` (red), each with `-soft` and `-strong` variants. Use the
  `status-pill status-paid` pattern: colour is always paired with a label, never
  the only signal.
- **Icon accents** — `acc-blue`, `acc-indigo`, `acc-violet`, `acc-teal`,
  `acc-emerald`, `acc-amber`, `acc-rose`, each with a `-soft` tile tint. The one
  place colour is allowed to be loud, and only on an icon's tile
  (`class="icon-tile accent-violet"`) — never on body text or a control. Every
  pair is contrast-checked in both themes by `npm run check:contrast`.
- **Type** — Public Sans for UI, IBM Plex Mono for money and IDs (`.numeric`
  adds tabular figures).
- **Radii** — soft geometry: 14px is the house corner (`rounded-md`), 18px for
  cards (`rounded-lg`), up to 32px (`rounded-2xl`). `.squircle` upgrades to a
  real superellipse where `corner-shape` is supported and stays a generous
  rounded rectangle everywhere else.
- **The date picker** — `static/js/datepicker.js` draws a calendar over the real
  `<input type="date">`, which keeps its name, its value and its validation; the
  type is never switched to `text`, so the browser's own checks and its mobile
  keyboard survive. The shape is **day → month → year**: press the "October 2026"
  header for twelve months, press the year for twelve years, so two presses cross a
  decade. That is the whole design, and it answers the actual complaint — a child's
  date of birth is years back and the native picker steps a month at a time. It
  respects the field's own `min`/`max`, supports arrows, PageUp/PageDown, Home/End,
  Enter and Escape, and renders as a `role="grid"` table so a screen reader reads a
  calendar rather than forty-two buttons. **On touch it does nothing at all**: a
  phone's own date wheel is better than any popover, it is what people there expect,
  and it does not take the viewport. Opt out per field with `data-no-datepicker`.
- **Buttons in flight** — `.btn.is-loading` swaps the button's own glyph for a
  spinner and stops it taking clicks. One `submit` listener on the document covers
  every form in the app, including ones written later; `event.submitter` picks the
  button that was actually pressed. It sets **`pointer-events: none`, never the
  `disabled` attribute** — several submit buttons here carry a name and value the
  view reads (`save_and_add_another`, `action=import`), and a button disabled inside
  its own submit handler can be dropped from the payload, silently changing what the
  form did. `aria-busy` carries the state; `data-no-loading` opts out. A `pageshow`
  listener clears anything the back button left spinning.
- **The password reveal** — on every `input[type=password]`, injected by
  `static/js/password.js`, so no template has to know it exists and screens nobody
  has written yet are covered. Generate and the live checklist stay on
  `[autocomplete=new-password]` only: offering to generate a password into a
  *sign-in* box would be offering to lock somebody out.
- **The collapsed sidebar** — one `--sidebar-pad` token, dropped to zero when the
  rail narrows so every glyph sits on one vertical axis. Anything the collapsed
  state has to *undo* is declared in the components layer rather than as a utility
  in the markup (`sidebar-grow`, `nav-soon`, `sidebar-toggle-inline`), because
  Tailwind's utilities sit in a later layer and win over these rules whatever their
  specificity — which is why `flex-1` and `ml-auto` on folded labels went on
  expanding and pushing glyphs off centre no matter what was written here. Rows
  also drop their `gap`, since a flex gap is still drawn beside an item that has
  folded to zero width.
- **Motion** — `--duration-quick` / `--duration-settle` / `--duration-slow` for
  the general case, plus `--toast-rise` / `--toast-open` / `--toast-fade`, which a
  toast has to itself because it is the one thing on screen whose job is to be read
  before it goes. `ui.js` sequences the stages and names those tokens in its
  constants; a test asserts the two files have not drifted, since a stage cut short
  is exactly the jank that was reported. The icon, its glyph and the close button
  are each pinned with an explicit `flex`, width and height — the toast is a flex
  row whose own width animates, and an item sized only by its content gets
  re-measured on every frame of that. The message is held `white-space: nowrap`
  for the whole of both width animations: `opacity: 0` hides text but does not take
  it out of layout, so it went on reflowing into a narrow column as the track
  closed, the toast grew tall to fit it, and the pill distorted anyway. One `prefers-reduced-motion` opt-out near
  the top of the stylesheet covers every component, including the status-dot pulse
  — whose keyframes end transparent so the cut leaves no ring behind.
- **Glass** — `--glass-bg`, `--glass-border`, `--glass-edge`, `--glass-blur`,
  plus a `--glass-dark-*` set for panes on the navy gradient. Used through
  `.glass` / `.glass-dark` / `.glass-lit`, and deliberately sparingly: a sticky
  bar, a hero card, a modal — never behind body copy.
- **Motion** — two curves (`--ease-out`, `--ease-spring`) and three durations,
  so every transition is one of a handful of recognisable movements.
- **Touch targets** — 44px minimum via `--spacing-touch` (`min-h-touch`,
  `size-touch`), applied to every button, input and nav link.

Components (`.btn`, `.card`, `.field-input`, `.nav-link`, `.status-pill`,
`.icon-tile`, `.bento-item`, `.chart-stack`) compose in markup —
`class="btn btn-primary"` — because Tailwind v4's `@apply` only accepts real
utilities.

### Charts are CSS, not a library

The dashboards draw collection, outstanding balances and the payment-status
split with `.chart-stack`, `.chart-bar-*` and `.chart-columns` — a width written
as a custom property, nothing loaded, nothing to keep working. They follow the
theme for the same reason a status pill does: the colours are tokens.

The four payment-status colours are a **status** palette, not a categorical one,
and they are never re-pointed to mean something else. Checked with a palette
validator, the amber/green pair separates by only ΔE 6.4 under protanopia in
light mode — inside the floor band, which is legal only with secondary encoding.
So every chart that uses them carries a labelled legend with counts and a 2px
gap between segments: identity never rests on hue alone.
`apps/core/templatetags/charts.py` does the proportion arithmetic and nothing
else — no template is allowed to total a column.

### Toasts

Every action in the app reports itself the same way: a small circle rises from
the bottom of the screen, opens sideways into a pill carrying an icon and a
sentence, holds, closes back into the circle and drops away.

**Views do not know toasts exist.** They call ordinary
`messages.success(...)` / `.error(...)` / `.warning(...)` / `.info(...)` and
`partials/_toasts.html` turns whatever is on the request into toasts. That is
the whole integration: the write paths across academics, fees, students, staff,
payments and admissions were already flashing messages before this existed and
lit up without being touched.

Four categories, and the colours are not new ones — three reuse the
payment-status soft/strong pairs that `npm run check:contrast` already checks
in both themes, and the fourth is glass:

| Level | Colour | Used for |
| --- | --- | --- |
| `success` | green | the action completed |
| `error` | red | it failed, or was refused |
| `warning` | amber | it needs attention first |
| `info` | glass | neither good nor bad news |

Mechanics worth knowing:

- The movement is two interpolable properties: `transform`/`opacity` for the
  rise and drop, and a grid track going `0fr → 1fr` for the open and close —
  `width: auto` cannot be animated, a grid track can. It also means a long
  message wraps to two lines instead of being clipped.
- **The markup ships open.** `js/ui.js` closes each server-rendered toast and
  replays the entrance, so a blocked script costs the animation and never the
  message — the same bargain `.reveal` makes.
- The region lives at the end of `<body>` in `base.html`, *outside* the
  signed-in branch. Before this, `messages` rendered only inside the shell, so
  anything flashed on the way to a signed-out screen — a failed sign-in, a
  sign-out — was consumed by the template engine and shown to nobody.
- Colour is never the only signal: every toast has an icon and a
  visually-hidden category word ("Success: …"), inside a polite live region.
- Errors and warnings hold longer and carry a dismiss button; success and
  info simply go. Hovering or focusing a toast restarts its clock rather than
  freezing it, so one can never be parked on screen forever.
- At most four are visible; older ones retire early rather than stacking off
  the top of the screen.

Anything on a page can raise one with `window.toast(message, level)` (or
`toast.success(...)`). Markup can too: `data-toast-on-click="…"` plus
`data-toast-on-click-level="…"`, which is how the sidebar's not-yet-built rows
explain themselves instead of silently doing nothing.

A rejected form is announced without any view being involved: `.field-error`
only exists on a form that has been submitted and refused, so `js/ui.js` counts
them and raises one amber toast — "2 fields need attention. Nothing was saved."

A redirect nobody asked for says why it happened.
`RequirePasswordChangeMiddleware` flashes a warning as it sends an account on a
temporary password to Account settings, because otherwise pressing "Students"
and arriving at Account settings is a mystery. The persistent notice on that
page stays: it is something the user must act on, and a toast that disappears
is the wrong carrier for that.

`admissions/public_base.html` — the school-branded enquiry shell, which
deliberately does not extend `base.html` — includes the region too. Nothing
flashes a message on that flow today; the point is that adding one later
reaches the parent reading the page instead of being swallowed.

### Authorisation is answered, not re-asked

`handler403` is `apps.core.views.permission_denied`. It leaves the refusal
exactly as it was — the view never ran, the status is still **403**, and every
test asserting that a bursar cannot reach an academic-setup URL still asserts
that — and adds the reason as an error toast, so a refusal arrives where every
other answer in the app arrives.

What it deliberately does not do is ask for credentials. A signed-in principal
who lacks a capability is already who they are; a password prompt would answer
a question nobody asked. `templates/403.html` carries no password field and no
link to the sign-in page, and a test asserts both — the page is the brand
gradient with a glass panel on it, a plain sentence ("Your role doesn't have
access to this"), the specific reason underneath, and one button back to
`core:dashboard`, which forwards each role to its own home.

The signed-out half of that template *does* offer a sign-in link, because
there the thing standing between the reader and the page really is
authentication.

That prompt had a real source, now fixed: the owner dashboard and the fee
structure list linked "Manage branches", "Set a term" and "Add a term" to the
**Django admin**, and a proprietor's account is deliberately not `is_staff` —
so those links answered a click with the admin's username-and-password form on
a site they were already signed in to. They point at the real tenant-scoped
screens (`schools:branch_list`, `fees:term_list`, `fees:term_create`) instead.
The only remaining `/admin/` link is on the platform overview, whose role *is*
a superuser.

### Dark mode is a second design, not a filter

The ramps invert for dark mode: `brand-50` is the palest blue in light mode and
a deep navy in dark, `ink-900` is near-black then near-white. That is what lets
`text-ink-900` mean "primary text" in both themes without a single `dark:`
variant in a template — and it is a trap on any surface that does **not**
invert.

The navy plane is exactly that surface. The sidebar, the hero, the problem
cards, the closing CTA, the auth visual panel and the 403 band are navy in
*both* themes, so a brand-ramp colour on them moves while the ground stays
still. `text-brand-100` on the hero measured **1.01:1** in dark mode —
invisible, not merely dim.

Four tokens exist for that plane and nothing else should be used there:

| Token | For |
| --- | --- |
| `panel-fg` | headings, white |
| `panel-copy` | body copy |
| `panel-muted` | secondary text, figures |
| `panel-subtle` | small uppercase labels |

All four are checked against the panel in both themes by
`npm run check:contrast`, so the regression cannot come back quietly.

The hero photograph's dim is a token too (`--hero-photo-dim`): the dark-mode
gradient beneath it is darker, so the same multiplier composites the classroom
down to nearly nothing. It is lifted in dark rather than shared.

### Page transitions and reduced motion

Navigation between screens is a cross-document view transition declared in CSS
(`@view-transition { navigation: auto }`), so there is no router and nothing to
go wrong where it is unsupported. `templates/base.html` names the sidebar
`vt-sidebar` and the content column `vt-page`, which is what makes the shell
stay put while the page inside it changes. The auth split panel names its two
halves on **both** the sign-in and the sign-up page, so the browser animates the
panel physically crossing the screen; `js/ui.js` applies a CSS-animation
fallback only where view transitions are missing, never both.

`prefers-reduced-motion` is honoured globally in `@layer base`, restated for
view-transition pseudo-elements (which `*` cannot reach), and checked in JS
before any observer is armed. Nothing is hidden and no state becomes
unreachable — motion is removed, content is not.

### Scrollbars, and a prefix that was doing harm

Scrollbars are themed from `--scrollbar-thumb` / `--scrollbar-track` — both
`scrollbar-color` (the standard property) and the `::-webkit-scrollbar`
pseudo-elements, driven by the same tokens. The navy panes take
`.scroll-on-panel` instead, for the same reason their text takes panel tokens.
12px, not 6px: shrinking a scrollbar because it looks tidier is a real cost to
anyone who drags one.

One thing worth knowing if you add another frosted surface: **write
`backdrop-filter` only, never the `-webkit-` alias.** Lightning CSS prefixes
from its own browser targets, and seeing both spellings made it treat the
unprefixed declaration as redundant and drop it — leaving ten of fourteen glass
rules `-webkit-` only, which is the one spelling Firefox does not support. The
blur was silently missing there.

### The low-end device tier

`js/ui.js` sets `<html data-perf="lite">` when the device reports few cores,
little memory, or Data Saver. The stylesheet's `[data-perf="lite"]` block then
turns off every `backdrop-filter`, the hero's blur and the card drift in one
place, rather than each component guessing. Everything still renders — glass
becomes a solid tint — so "lite" costs texture, never content or legibility.

## Layout

`templates/base.html` is the shell every role dashboard extends: a
**collapsible** sidebar from `lg` up, off-canvas drawer below it, sticky
header, skip link, dependency-free JS.

Collapsing is one attribute — `<html data-sidebar="collapsed">` — which drives
both the rail's width and the content column's padding through
`--sidebar-w`. They have to move together or the page tears down the middle,
and one variable is what guarantees it. The choice is stored in localStorage
and re-applied by the inline script in `<head>`, the same no-flash trick the
theme uses, so a collapsed rail is already collapsed in the first frame.

The rail's scroll position is remembered in sessionStorage per navigation. A
long menu scrolled to Payments used to jump back to the top on every click,
which on a rail this tall means hunting for your place every time. It is
written on scroll rather than on `beforeunload`, which is unreliable on mobile
and blocks the back-forward cache.

**Which row is highlighted** is the *most specific* match, not every match.
`_resolve` marks an item active when the path starts with its href — right for
`/students/41/` lighting up Students, wrong when one entry's href is a prefix
of another's. Four pairs here are (`/fees/` and `/fees/terms/`, `/payments/`
and `/payments/outstanding/`, …), so opening Terms lit Fee Structures too and
read exactly like a row that had failed to clear. Signed-out pages fill `{% block anonymous_content %}` and
skip the shell entirely.

```
{% extends "base.html" %}
{% block page_title %}Fees{% endblock %}
{% block content %}...{% endblock %}
```

## Admin

Schools, branches and users are editable at `/admin/` immediately.
`TenantScopedAdminMixin` narrows both the changelists and the foreign-key
dropdowns, so a school owner cannot see — or reassign a row to — another tenant.
Django superusers bypass it.

### Deleting a school is one confirmation, not forty

Four foreign keys in this codebase are `PROTECT`, and each is right: a student
with payments against their name must not vanish from under the money, a term
with payments against it is history, a class with students on it must not
disappear from under them, and a WhatsApp template that has carried messages is
retired by status rather than deleted.

Those rules made deleting a *school* unusable. Django's delete view walks the
graph, meets the first `PROTECT`, lists the rows and offers **no button** — so
the only way through was to go and empty the payments by hand, then the students,
then the classes, one changelist at a time.

Closing a tenant is a different decision from tidying one, so the exception lives
in `apps/schools/teardown.py` and nowhere near the models:

- **The question is asked once.** `SchoolAdmin.get_deleted_objects` returns
  counts and an empty `protected`, so the page confirms instead of refusing. It
  counts rather than lists, because a school with four hundred students and two
  thousand payments turns "are you sure" into a page nobody reads to the bottom
  of.
- **The order is read off the database, not written down here.**
  `ProtectedError` names exactly which rows stood in the way, so the teardown
  deletes those and tries again. A fifth `PROTECT` added next year is handled
  without anybody remembering this file — which is the failure being fixed, so
  it is the one worth designing out rather than re-creating one list further
  down.
- **All or nothing**, in one transaction. A school half closed — students alive
  with no school to belong to — is worse than either outcome. A bounded pass
  count means a cycle fails loudly and changes nothing.
- **`School.delete()` itself is untouched**, and still raises `ProtectedError`.
  Only the teardown goes through, so nothing that calls `delete()` by accident
  takes a tenant with it.
- **No new power.** `perms_needed` is still computed Django's way, so a staff
  account that may not delete payments does not acquire that right by going
  through a school. And `bootstrap_tenant` grants a school owner `view` and
  `change` on their own school, never `delete` — the teardown is the platform's.

The confirmation page points at the reversible alternative, because the screen
people reach by accident is this one: setting a school's **status** to Cancelled
or Suspended keeps every record.

## Layout of the code

```
config/settings/     base / dev / prod, plus the database fallback logic
apps/core/           branding.py (the product's one name), tenancy.py,
                     permissions.py, models.py, roles.py,
                     navigation.py (incl. ROLE_HOME), middleware.py, admin.py,
                     forms.py (incl. SchoolSignupForm), views.py (landing,
                     signup, onboarding, the four role dashboards)
apps/schools/        School (the tenant) and Branch
apps/accounts/       the custom User (incl. must_change_password), backends.py
                     (sign in by email or username), Account settings (views,
                     forms), middleware.py (the temporary-password hold),
                     invites.py, create_platform_owner, invite_school_owner
apps/academics/      Class and Subject, their screens, and curriculum.py
apps/fees/           Term, FeeStructure, FeeComponent, screens, and pricing.py
apps/students/       Student, its screens, the derived fee position (fees.py),
                     validators.py, roster.py, and the Excel import
                     (importer.py validates, workbook.py reads and writes .xlsx)
apps/messaging/      SchoolMessagingConfig (each school's Sender ID),
                     Message and MessageRecipient, audiences.py (who a filter
                     means), identity.py (who a message is from),
                     dispatch.py (record then deliver), validators.py, and
                     providers/ (the interface, console, Termii, BulkSMS
                     Nigeria, Africa's Talking)
apps/payments/       Payment, balances.py (every derived figure), its screens,
                     and seed_payments
apps/admissions/     the paid add-on: Applicant (enquiry through enrolment),
                     Assessment, RequirementSet, AdmissionFeeSchedule and
                     AdmissionPayment; requirements.py (what each level asks
                     for), enrolment.py (applicant -> Student), access.py (the
                     one capability a school configures), notifications.py
                     (the parent emails), admission_pricing.py, the public
                     enquiry page (public_urls.py) and the staff pipeline
apps/reports/        the collections report (HTML and PDF); no models
docs/                TECHNICAL.md (the handover reference) and
                     BACKUP_AND_RESTORE.md
templates/           base.html, 403.html, partials/ (including _toasts.html,
                     the notification region every Django message renders
                     into), core/ (landing, signup,
                     onboarding, dashboards), academics/, fees/, students/,
                     messaging/, payments/, accounts/ (Account settings),
                     registration/ (_auth_base.html -- the split panel every
                     signed-out screen sits in -- plus login, password reset
                     and password change), admissions/
                     (public_base.html -- the school-branded shell that
                     deliberately does not extend base.html -- the pipeline,
                     and email/ for the three parent emails)
assets/app.css       design tokens and components (Tailwind source)
static/css/app.css   compiled output (committed)
static/img/          the SCHOOLCORD logo, SVG and PNG
```
