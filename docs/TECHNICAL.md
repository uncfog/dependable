# SCHOOLCORD — Technical documentation

The handover reference: how the system is put together, where each piece lives,
how it is deployed, and every setting it reads. It describes the code as of the
`docs/handover` branch (October 2026).

The [README](../README.md) is the companion to this file. It holds the
long-form design notes — *why* each feature works the way it does — and this
file links to those sections rather than repeating them. Backups and restores
have their own guide: [BACKUP_AND_RESTORE.md](BACKUP_AND_RESTORE.md).

## Contents

1. [Architecture overview](#1-architecture-overview)
2. [Backend: Django project and apps](#2-backend-django-project-and-apps)
3. [Key models](#3-key-models)
4. [Frontend: templates, Tailwind and JavaScript](#4-frontend-templates-tailwind-and-javascript)
5. [Database and multi-tenancy](#5-database-and-multi-tenancy)
6. [Authentication, roles and capabilities](#6-authentication-roles-and-capabilities)
7. [Modules (per-school feature switches)](#7-modules-per-school-feature-switches)
8. [Deployment on Railway](#8-deployment-on-railway)
9. [Environment variables — complete reference](#9-environment-variables--complete-reference)
10. [Third-party integrations](#10-third-party-integrations)
11. [Management commands](#11-management-commands)
12. [Scheduled jobs](#12-scheduled-jobs)
13. [Operations runbook](#13-operations-runbook)
14. [Known gaps and open items](#14-known-gaps-and-open-items)

---

## 1. Architecture overview

SCHOOLCORD is a multi-tenant school-management SaaS: one deployment serves many
schools, each with one or more branches (campuses). It is a server-rendered
Django monolith. There is no separate frontend app and no public API.

```
                    Browser (staff, parents on the public enquiry page)
                                     │  HTTPS
                                     ▼
          ┌──────────────── Railway (one web service) ────────────────┐
          │  gunicorn → Django 5.2 (config.settings.prod)             │
          │    WhiteNoise serves /static/ from the container          │
          │    TenantMiddleware scopes every ORM query to the user    │
          └─────┬───────────────┬─────────────────┬──────────────┬────┘
                │ SQL           │ S3 API          │ HTTPS        │ HTTPS
                ▼               ▼                 ▼              ▼
        Supabase Postgres  Supabase Storage    Resend        Termii (SMS)
        (all app data)     bucket "media"      (email)       BulkSMS Nigeria (kept)
                           (logos, receipts,                 Africa's Talking (stub)
                            admissions docs)
                                                Have I Been Pwned
                                                (password breach check)
```

| Layer | Technology | Where |
| --- | --- | --- |
| Language / runtime | Python 3.11 | `.python-version`, `runtime.txt` |
| Web framework | Django 5.2.17 | `requirements.txt` |
| App server | gunicorn 23 | `Procfile` |
| Static files | WhiteNoise (compressed manifest storage) | `config/settings/base.py` |
| Database | PostgreSQL (Supabase) in production; SQLite locally | `config/settings/database.py` |
| Uploaded files | django-storages S3 backend → Supabase Storage | `config/settings/base.py` |
| Email | django-anymail → Resend | `config/settings/prod.py` |
| SMS | Own provider layer, stdlib `urllib` → Termii | `apps/messaging/providers/` |
| CSS | Tailwind CSS v4, compiled and committed | `assets/app.css` → `static/css/app.css` |
| JS | Small vanilla scripts plus Driver.js (guided tour), vendored | `static/js/` |
| Spreadsheets | openpyxl (bulk import/export) | `apps/core/spreadsheets.py` |
| PDF | ReportLab (collections report) | `apps/reports/pdf.py` |

There is **no task queue, no Redis and no cache server.** Everything runs in
the request, except the two nightly admissions commands (see
[Scheduled jobs](#12-scheduled-jobs)).

## 2. Backend: Django project and apps

```
manage.py                  defaults to config.settings.dev
config/
  settings/base.py         everything shared; reads the environment
  settings/database.py     picks Postgres or SQLite (see §5)
  settings/dev.py          DEBUG on, ALLOWED_HOSTS=*, console email, plain static
  settings/prod.py         DEBUG off, HTTPS/HSTS, Resend, no SQLite fallback
  urls.py                  root URL map
  wsgi.py / asgi.py        default to config.settings.prod
apps/                      the project's own apps (below)
templates/                 all templates, one folder per app
assets/app.css             Tailwind source
static/                    compiled CSS, JS, images (committed)
scripts/check-contrast.mjs colour-contrast checker for the design tokens
```

### The apps

Order matters in `INSTALLED_APPS`: `core` first, `admissions` and `reports`
last, because they read the others.

| App | URL prefix | What it owns |
| --- | --- | --- |
| `apps.core` | `/`, `/dashboard/`, `/platform/`, `/school/`, `/branch/`, `/finance/`, `/welcome/`, `/settings/`, `/signup/`, `/about/`, `/privacy/` | Tenancy (`tenancy.py`, `models.py`, `middleware.py`), roles and capabilities (`roles.py`, `permissions.py`), module registry (`modules.py`), navigation and per-role dashboards, landing page, signup and onboarding, branding (`branding.py`, the product's one name), SEO (`seo.py`: robots.txt, sitemap.xml, llms.txt), deploy checks (`checks.py`), shared spreadsheet-import rails (`imports.py`, `import_flow.py`, `spreadsheets.py`), money formatting (`templatetags/money.py`). No tables of its own beyond abstract bases. |
| `apps.schools` | `/branches/` | `School` (the tenant), `Branch`, `SchoolModule`. The admin-only school teardown (`teardown.py`). |
| `apps.accounts` | `/accounts/`, `/staff/` | Custom `User`, email-or-username login backend, Account settings, temporary-password hold, staff invites, breach-checked password validation. |
| `apps.academics` | `/academics/` | `Class`, `Subject`; class-ladder curriculum (`curriculum.py`); bulk/spreadsheet entry. |
| `apps.fees` | `/fees/` | `Term`, `FeeStructure`, `FeeComponent`; pricing seed (`pricing.py`). |
| `apps.students` | `/students/` | `Student`; derived fee position (`fees.py`); Excel import (`importer.py`, `workbook.py`). |
| `apps.payments` | `/payments/` | `Payment`; every derived balance (`balances.py`). |
| `apps.messaging` | `/messaging/` | `SchoolMessagingConfig` (per-school Sender ID), `WhatsAppTemplate`, `Message`, `MessageRecipient`; audiences, identity resolution, dispatch, gateway providers. |
| `apps.admissions` | `/admissions/`, public `/<school-slug>/enquiry/` | The paid add-on: `Applicant` (enquiry → enrolment), `Assessment`, `RequirementSet`/`Requirement`, `AdmissionFeeSchedule`/`AdmissionFeeItem`, `AdmissionPayment`, `AdmissionsConfig`; parent emails; walk-in enrolment. |
| `apps.reports` | `/reports/` | Collections report as HTML and PDF. **No models** — reads the other apps. |

Third-party apps installed: `django.contrib.admin`, `auth`, `contenttypes`,
`sessions`, `messages`, `staticfiles`, `sitemaps`, `anymail`, `storages`.
`django.contrib.sites` is deliberately **not** installed.

### Middleware order (and why it matters)

From `config/settings/base.py`:

1. `SecurityMiddleware`, `WhiteNoiseMiddleware`, sessions, common, CSRF
2. `AuthenticationMiddleware`
3. **`apps.core.middleware.TenantMiddleware`** — must follow authentication;
   publishes the tenant context for the request (see §5)
4. messages, clickjacking
5. **`apps.core.middleware.SchoolStatusMiddleware`** — locks out a suspended or
   cancelled school's staff and makes a past-due school read-only (see §6)
6. **`apps.accounts.middleware.RequirePasswordChangeMiddleware`** — holds an
   account on a temporary password at Account settings
7. **`apps.core.middleware.ModuleAccessMiddleware`** — answers 403 for any URL
   under a switched-off module's prefix (see §7)

### Context processors

Beyond Django's defaults: `apps.core.context_processors.tenancy` (role,
capabilities, enabled modules), `apps.core.branding.branding` (product name),
`apps.core.seo.seo` (meta/canonical tags), and
`apps.accounts.context_processors.password_policy`.

### Root URLs worth knowing

| Path | What |
| --- | --- |
| `/admin/` | Django admin, tenant-narrowed by `TenantScopedAdminMixin` |
| `/accounts/login/`, `/accounts/logout/` | Custom role-aware login/logout |
| `/accounts/password_reset/`, `/accounts/reset/<uid>/<token>/` | Branded reset flow |
| `/accounts/settings/` | Own account: name, email, password |
| `/<school-slug>/enquiry/` | A school's public admissions enquiry form (only when admissions is on) |
| `/robots.txt`, `/sitemap.xml`, `/llms.txt` | Crawler files, from `apps/core/seo.py` |
| `/schools/<school-slug>/logo/?v=…` | A school's logo, streamed by the app. Public (it is on the enquiry page). |
| `/payments/<id>/receipt/file/` | A payment's attached slip. Signed-in, payments capability, own school only. |
| `/admissions/<id>/documents/<kind>/` | An applicant's upload (`passport_photo`, `birth_certificate`, …). Signed-in, may read the pipeline, own school/branch only. |
| `/media/…` | Served by Django **only when `DEBUG` is on**, for local disk storage. Nothing in the app links to it. |

## 3. Key models

All tenant-owned models inherit from one of two abstract bases in
`apps/core/models.py`:

- **`TenantScopedModel`** — adds `school` (required FK, `CASCADE`), `branch`
  (nullable FK, `SET_NULL`), `created_at`, `updated_at`, a scoped default manager
  `objects`, an unscoped `all_objects`, and a `(school, branch)` index. `save()`
  stamps `school`/`branch` from the active request when left blank.
- **`BranchScopedModel`** — the same, but `branch` is required (`CASCADE`) and
  `school` is derived from the branch on save.

| Model | Base | Notes |
| --- | --- | --- |
| `schools.School` | `TimeStampedModel` | The tenant. `name`, unique `slug` (used in the public enquiry URL), `plan` (trial/starter/growth/enterprise), `status` (active/trialing/past_due/suspended/cancelled), `contact_email`, `contact_phone`, `logo` (file). |
| `schools.Branch` | `TimeStampedModel` | A campus. FK `school`, `name`, `city`, `state`, `address`, `head` (FK User, nullable), `is_active`. |
| `schools.SchoolModule` | `TimeStampedModel` | One row per *decision* to switch a module on/off for a school. Absent row = registry default. |
| `accounts.User` | `AbstractUser` | Adds `school`, `branch`, `role`, `job_title` (free text, grants nothing), `phone`, `must_change_password`. `User.objects` is **unscoped** (login needs it); `User.scoped` is the tenant-filtered manager. |
| `academics.Class` | Branch | A teaching group, e.g. "SSS 2" + arm. `level` is Nursery / Primary / Junior Secondary / Senior Secondary. |
| `academics.Subject` | Branch | M2M to `Class`. |
| `fees.Term` | Branch | `name` ("First Term 2025/2026"), `academic_year`, `sequence` (1–3), `is_current`, `due_date`. |
| `fees.FeeStructure` | Branch | What one class owes for one term. FK `school_class` (`PROTECT` path via students), FK `term`. The total is **never stored** — it is the sum of its components. |
| `fees.FeeComponent` | Branch | One line item on a fee structure. |
| `students.Student` | Branch | FK `school_class`. Expected fees are read from the class's fee structure, not stored on the student. Soft-removed via status. |
| `payments.Payment` | Branch | FK `student`, FK `term`, amount, method, source, `status` (only *confirmed* counts toward balances), `receipt` (file), `recorded_by`/`confirmed_by`/`voided_by`. Voided, never deleted. |
| `messaging.SchoolMessagingConfig` | Tenant | One per school: `sender_id`, Sender ID approval status, `provider`, optional per-school `api_key` (stored in plain text), WhatsApp device/approval fields. |
| `messaging.WhatsAppTemplate` | Tenant | Meta-approved template per school. |
| `messaging.Message` / `MessageRecipient` | Branch | One send, and one row per parent with its own provider reference and status. |
| `admissions.AdmissionsConfig` | Tenant | Per-school admissions policy (validity window, reminder timing, who may decide). |
| `admissions.RequirementSet` / `Requirement` | Tenant | What each level asks applicants for. |
| `admissions.Applicant` | Branch | One record from enquiry to enrolment. Reference like `ENQ-2026-0001`. Five document `FileField`s (passport photo, birth certificate, previous results, transfer letter, immunisation record). `OneToOne` to the `Student` it became. |
| `admissions.Assessment` | Branch | Entrance-exam outcome (`OneToOne` applicant). |
| `admissions.AdmissionFeeSchedule` / `AdmissionFeeItem` | Branch | Intake (one-off) fees per class — separate from termly fees. |
| `admissions.AdmissionPayment` | Branch | Money for admission; may link to the term `Payment` that took its term share. |

`PROTECT` foreign keys guard history: a class with students, a student with
payments, a term with payments, and a WhatsApp template that has carried
messages cannot be deleted. Closing a whole school goes through the admin's
teardown — see README → [Deleting a school](../README.md#deleting-a-school-is-one-confirmation-not-forty).

Default table names are Django's: `<app>_<model>`, e.g. `schools_school`,
`payments_payment`, `admissions_applicant`, `accounts_user`.

Migration counts per app: accounts 3, schools 4, academics 1, fees 2,
students 2, messaging 5, payments 1, admissions 3.

## 4. Frontend: templates, Tailwind and JavaScript

- **Server-rendered Django templates**, all under `templates/` (not inside
  apps). `templates/base.html` is the signed-in shell (sidebar from
  `partials/_nav.html`, toasts from `partials/_toasts.html`).
  `templates/registration/_auth_base.html` is the signed-out split panel.
  `templates/admissions/public_base.html` is the **school-branded** public shell
  and deliberately does not extend `base.html`.
- **Email templates**: `templates/email/` (platform mail, signed SCHOOLCORD) and
  `templates/admissions/email/` (parent mail, signed by the school). Each has an
  `.html` and a `.txt` half; mail is always multipart.
- **Tailwind CSS v4.** Source: `assets/app.css` (design tokens, components,
  dark mode). Output: `static/css/app.css`, **minified and committed**, so the
  app runs and deploys without Node. Rebuild only when you change the source:

  ```bash
  npm install
  npm run build:css      # or: npm run watch:css while editing
  npm run check:contrast # verifies token colour contrast
  ```

  Commit the rebuilt `static/css/app.css` with the source change. Railway does
  not run the Tailwind build.
- **JavaScript** (`static/js/`), no bundler, no framework:
  `ui.js` (toasts, loading states, theme), `password.js` (reveal/generate/policy
  checklist), `datepicker.js`, `tour.js` (guided tour on Driver.js).
  Driver.js is vendored at `static/js/vendor/` and `static/css/vendor/`.
- **Charts are CSS**, not a chart library (`apps/core/templatetags/charts.py`).
- **Phones.** Below `lg` the sidebar gives way to a bottom bar
  (`partials/_bottom_nav.html`; `BOTTOM_NAV` in `apps/core/navigation.py` chooses each
  role's screens) with More opening the full menu, which is copied from the sidebar
  on first use so it is only sent once. Below `sm`, every table in `#main` becomes a
  stack of labelled cards: `ui.js` copies the column headings onto the cells, so a
  new table is covered automatically. Opt out with `data-no-cards`, as the report
  grids do because they exist to be compared down a column. Add
  `data-filter-sheet="Label"` to a filter form to fold it behind a button below `lg`.
  With scripting off, tables scroll sideways and filters stay open.
- **Fonts are self-hosted** in `static/fonts/` (SIL Open Font License) and declared at
  the top of `assets/app.css`. No page requests anything from a font host.
- **Error pages** `404.html`, `500.html` and `403_csrf.html` extend
  `_error_page.html`, which is styled inline and uses no database, because it may be
  showing because one of those is down.
- **Design-system notes** — tokens, dark mode, toasts, navigation, layout — are
  in README → [Design tokens](../README.md#design-tokens) and following sections.
- `SCHOOLCORD_Landing_Page.html` in the repo root is the **design reference**
  the landing page (`templates/core/landing.html`) was rebuilt from. It is not
  served.

## 5. Database and multi-tenancy

### Engine selection

`config/settings/database.py` decides, from the environment:

| Situation | Result |
| --- | --- |
| `DB_ENGINE=sqlite` | SQLite at `./db.sqlite3`, always |
| No `DATABASE_URL` and no `DB_NAME` | SQLite (error instead if `DB_ENGINE=postgres`) |
| Postgres configured, `DB_ENGINE=postgres` **or** prod settings | Postgres, no fallback — a bad URL crashes |
| Postgres configured, dev settings, server unreachable (0.75 s probe) | SQLite, with a warning on stderr |

A host containing `pooler`/`pgbouncer`, or port `6543`, is treated as a
transaction pooler and gets `DISABLE_SERVER_SIDE_CURSORS = True`. Force it with
`DB_POOLED=1`/`0`. Production sets `CONN_MAX_AGE` from `DB_CONN_MAX_AGE`
(default 60 s).

### Schema

Every app table lives in Postgres's **`public` schema**. Supabase's own schemas
(`auth`, `storage`, `realtime`, …) are not used by Django — the app uses Django
auth, not Supabase Auth. This is what makes the backup guide's
`--schema=public` dump complete for the application.

### The multi-tenancy model

**Shared database, shared schema, row-level isolation.** Every tenant-owned row
carries `school_id` and (usually) `branch_id`, and isolation is enforced in the
**model layer**, not in views:

1. `TenantMiddleware` builds a `TenantContext(school_id, branch_id, role)` from
   `request.user` and installs it in a `contextvars.ContextVar` for the duration
   of the request, resetting it in a `finally` so a reused worker thread can
   never carry one request's school into the next.
2. Every tenant model's default manager (`TenantManager`) calls
   `apply_scope()` in `get_queryset()`. So `Student.objects.all()` inside a view
   is already filtered — there is no per-view filtering to forget.
3. `apply_scope()` (in `apps/core/tenancy.py`) applies the role's **scope**:

   | Scope | Roles | Filter applied |
   | --- | --- | --- |
   | `platform` | Platform Owner, any Django superuser | none |
   | `school` | School Owner | `school_id = user.school_id` |
   | `branch` | Principal, Bursar | `school_id = user.school_id AND (branch_id = user.branch_id OR branch_id IS NULL)` |

   Rows with `branch = NULL` are *school-wide* and visible to every branch.

**Fail-closed rules:**
- unauthenticated request → empty queryset;
- non-platform account with no school → empty queryset;
- branch-scoped role with no branch → empty queryset;
- unknown or blank role → narrowest scope (`branch`).

**No context = no filtering.** Outside a request (shell, migrations, management
commands, tests) there is no context and querysets are unfiltered. That is what
lets `migrate` and `createsuperuser` work, and it means **a shell session sees
every school**. Code that runs in the background *as* a tenant must say so:

```python
from apps.core.tenancy import scope_to, unscoped

with scope_to(school_id=7, branch_id=3, role="bursar"):
    ...   # behaves like that bursar's request

with unscoped():
    ...   # deliberate cross-tenant access, e.g. platform reporting
```

**Other isolation points:**
- `Model.all_objects` is the unscoped manager, used deliberately (e.g. the
  public enquiry page, where an anonymous parent has no tenant context, scopes
  by the school in the URL instead). Every use is spelled out at the call site.
- `School` and `Branch` have their own scoped managers (`School.objects` shows a
  school owner only their own school).
- `User.objects` is **not** scoped (authentication must find users before any
  context exists); use `User.scoped` for staff listings.
- The admin is narrowed separately by `TenantScopedAdminMixin` (changelists and
  FK dropdowns).
- Uploaded files are namespaced by school in their storage path
  (`logos/<school-slug>/…`, `receipts/<school-id>/…`,
  `admissions/<school-id>/<reference>/…`), live in a **private** bucket, and
  reach a browser only through the app's views — which look the record up
  through the tenant-scoped manager, so another school's id is a 404. See
  `apps/core/files.py`.

Adding a new tenant-owned model: subclass `TenantScopedModel` (school-wide
records allowed) or `BranchScopedModel` (must belong to one campus). See README →
[How the tenancy works](../README.md#how-the-tenancy-works).

## 6. Authentication, roles and capabilities

### Authentication

- **Django's session auth** with a custom user model (`AUTH_USER_MODEL =
  "accounts.User"`). No Supabase Auth, no JWT, no OAuth/social login, no API
  tokens.
- **Sign in by email or username** — `apps.accounts.backends.EmailOrUsernameBackend`
  (username tried first; an email matches case-insensitively and only if exactly
  one account has it). Deactivated accounts (`is_active=False`) cannot sign in.
- **Password policy** (NIST 800-63B style): minimum 8 characters, Django's
  similarity/common/numeric validators, and a **Have I Been Pwned** breach check
  via k-anonymity (`apps/accounts/password_validation.py`, 1.5 s timeout, falls
  through to the local validators if the API is slow or down). No composition
  rules, no forced rotation.
- **Password reset**: Django's token flow with branded views; links valid for
  3 days (`PASSWORD_RESET_TIMEOUT`). A "your password was changed" email follows
  every change.
- **Temporary passwords**: accounts created with one carry
  `must_change_password=True`, and `RequirePasswordChangeMiddleware` keeps them
  on Account settings until they choose their own.
- **Production cookies**: `SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE`, HTTPS
  redirect, HSTS (30 days, include subdomains, preload), `X_FRAME_OPTIONS=DENY`.
- Login redirects through `core:dashboard`, which forwards to the role's home:
  Platform Owner → `/platform/`, School Owner → `/school/`, Principal →
  `/branch/`, Bursar → `/finance/`.

### Rate limits (`apps/core/ratelimit.py`)

| Door | Limit | When exceeded |
| --- | --- | --- |
| Sign-in, one account from one address | 10 failures in 15 minutes | 429 with a message, including the Django admin's sign-in, which refuses without the message |
| Sign-in, one account from anywhere | 50 failures in 15 minutes | Same |
| Sign-in, one address, any accounts | 100 failures in 15 minutes | Same |
| Password reset, one mailbox | 3 requests an hour | Nothing sent; the page looks the same, so it reveals nothing |
| Password reset, one address | 20 requests an hour | Same |
| Signup, one address | 20 attempts an hour | 429 with a message |
| Public enquiry, one address per school | 20 attempts an hour | 429 with a message |

Counts live in Django's cache. No `CACHES` is configured, so that is each worker's
own memory: with three workers the real ceiling can be up to three times these
figures, and a restart resets them. A shared cache would make them exact with no
code change. The client address comes from `X-Real-IP`, then `X-Forwarded-For`,
then the socket. **After a deploy, confirm `X-Real-IP` holds real client addresses
on Railway**: if a proxy address appears instead, everyone shares one address count.
`RATE_LIMITS_ENABLED=false` switches every limit off.

### Two-step sign-in (`apps/accounts/mfa_views.py`)

Platform accounts and superusers need an authenticator-app code as well as a
password, on every new session, the Django admin included. Their first sign-in after
this ships walks them through setting it up and gives ten one-use recovery codes.
School staff are not asked. Built on `django-otp`, whose secrets are stored in the
database, so treat database backups as sensitive. Lost phone and no recovery codes:
`reset_platform_mfa --email …`, from a shell, after checking who is asking.
`MFA_REQUIRED_FOR_PLATFORM=false` turns it off in an emergency.

### Audit log (`apps/audit/`)

`AuditEvent` is an append-only record of changes to money and access: who, when,
from where, and each field's old and new values. Tracked: `Payment`,
`AdmissionPayment`, `FeeComponent`, `User` (role, school, campus, active and staff
flags, email; a password change is noted without its value), `School` (status,
plan) and `SchoolModule`, plus two-step sign-in being set up, reset or recovered
from. Add a model with one entry in `TRACKED` in `apps/audit/recording.py`.

- **Read it:** each payment's page has a History card; the whole log is read-only
  in the Django admin, for the platform.
- **Limits:** it records `save()`, so `QuerySet.update()` and `bulk_create()` on a
  tracked model bypass it. Do not change tracked models that way. It begins when
  this ships: earlier edits were never recorded. Protection is in the application,
  so someone with direct database access is not stopped, and closing a school
  removes its events.

### Each child's ledger (`apps/students/fees.py`)

A child's balance is calculated on request from these, and stored nowhere:

| Source | What it gives |
| --- | --- |
| `StudentTermEnrolment` | Which class the child was in for a term, and that the term counts for them |
| `FeeStructure` for that class and term | The fees |
| `StudentFeeAdjustment` | Opening balances, charges, discounts, scholarships and waivers for one child |
| Confirmed `Payment` rows | The money |

`FeePosition` gives `expected` and `outstanding` for this term, `arrears` (from earlier
terms; negative is credit), `arrears_outstanding`, `credit` and `total_owed`. The rules:

- **Money settles the oldest debt first.** A payment is always credited to the current
  term, then the position works out what it covers.
- **Arrears start from the first term change after this ships.** An earlier term only
  counts with an enrolment or adjustment on record, and the migration creates enrolments
  for the current term alone. So no figure moves on deploy, and history nobody can vouch
  for is neither a debt nor a credit. A balance from before that goes in as an opening
  balance.
- **Enrolments maintain themselves** (`apps/fees/enrolment.py`): starting a term records
  every active child there, and a change of class updates the current term only.
  **Promote after starting the new term.** Moving a child before that changes the class
  recorded for the term that is ending, and so its fees.
- **Earlier terms' fees are locked**, on the screens and the import, once the campus has
  moved on (`Term.is_past`). Correct one child with an adjustment.
- **Adjustments are never edited.** The owner adds or voids them from the child's page,
  and every change is in the audit log.
- **Reports keep their meaning.** Expected, collected, outstanding and collection rate
  are still this term's, with arrears as their own figure. The Outstanding screen and
  fee reminders use `total_owed`, so a child whose only debt is from last term is chased.

### Shared payments and families (`apps/payments/allocation.py`, `apps/families/`)

`PaymentTransaction` is the money received. `Payment` is still one child's row and the
only thing balances read, so a family payment changes nothing about how a balance is
worked out, and no figure can be counted twice.

- **Individual payments work as before.** `Payment.save()` wraps a new payment in a
  one-child transaction and keeps the two in step, so every existing writer stays correct.
  `0003_backfill_transactions` did this for old payments and checks its own totals.
- **Shared money changes only through `allocation.py`.** Each change locks the
  transaction row first, so allocations can never exceed the amount received. What is
  left is worked out each time and shown: on the family page, and as "Not yet allocated"
  in reports. It is not part of "Collected".
- **The rules.** Paying a child more than they owe needs `allow_credit=True`. Moving money
  between siblings is `reallocate()`, a logged void and a new allocation in one step.
  Campus staff allocate only to children at their campus; a school owner can reach any.
  A share cannot be edited on its own.
- **A family groups records and owns nothing.** A person always makes the link; nothing
  matches on surname or guardian name. Removing a child ends the membership and moves no
  money. Family totals add up each child's position, and one child's credit is not set
  against a sibling's debt. Campus staff see their own campus's children and a note that
  others exist.
- **Off by default.** The `families` module is enabled per school, and covers
  `/families/` and `/payments/family/`. Individual payments don't depend on it. Bursars
  can read families; owners and principals can change them.
- **The screens (`apps/payments/shared_views.py`).** A bursar records the money, then
  shares it on the next page. All of a form's shares save or none do. A page opened
  before the shares changed is refused, which also stops a second press repeating them.
  What is left waits under Family payments, and the Payments page says when anything
  is. Voiding or moving a share, or voiding the whole payment, needs `void_payments`.
  One sheet covers the sum the parent handed over, and each child still has a receipt.
- **Checking it.** `check_payment_transactions` exits 1 if anything disagrees.

### Records import (`apps/onboarding/`)

One workbook brings a school's existing records in. Any of its six sheets can be left
out: Families, Students, Family Links, Fee Records, Payment Records, Payment Allocations.
It is off per school (the `onboarding` module), and only a school owner can run it,
because it writes money across campuses.

- **Staged in tables.** `ImportBatch` and `ImportRow` hold the file and rows, so review can
  take days. Nothing else is written until someone presses Import, and every row is judged
  again as it is written.
- **Nothing here overwrites.** A row adds a record or points at one already there. The only
  thing a matched child can gain is a family.
- **What goes to a person.** The same number with different details, the same name and
  birthday under another number, a Family ID with other contact details, and a payment with
  the same date, amount and reference. Rows are never merged, and answers are remembered in
  `MatchDecision`. Surnames and guardian names are never used.
- **Money.** A payment and its shares save together or not at all. Shares above the payment
  refuse it; shares below leave the rest visibly unallocated. `legacy_reference` stops a
  second run adding it again. Imported rows use `source=import`, keep their date as the
  confirmation date, keep the old receipt number, and send no messages.
- **Large files.** `commit_batch` and `undo_batch` work to a time budget and resume, because
  of gunicorn's 60-second limit. `busy_until` stops a second press writing the same rows.
- **Undo.** Payments and fee records are voided and kept, and family links end. A new child
  with nothing else on them is removed. One with only voided imported records is marked
  withdrawn, and one with other payments stays on the roll. A corrected file can then be
  imported, though a withdrawn child has to be made active again by hand.
- **Personal data.** `purge_import_files` removes old files and copied rows. Questions are
  stored as hashes, so `MatchDecision` keeps no names.

`/families/suggestions/` asks about children sharing a phone or email, among all children
without a family. It flags a number with more than five children, and remembers a "no".

### Error reporting (`config/settings/monitoring.py`)

Off until `SENTRY_DSN` is set. Reports exclude user details, cookies, stack-frame
variables, form contents and query strings. An exception's message can still mention
what it was about, so keep names out of messages raised in this code.

### School status (`apps/core/middleware.py`)

| `School.status` | Staff can |
| --- | --- |
| `active`, `trialing` | Everything their role allows |
| `past_due` | Read and export. Only their own Account settings and signing out can change anything |
| `suspended`, `cancelled` | See a notice and sign out. Public pages still open |

Platform accounts and superusers are exempt. Nothing is deleted. A school that is
not `active` or `trialing` also has its public enquiry page hidden and sends no
enquiry reminders.

### Roles (`apps/core/roles.py`)

| Role (`User.role`) | Scope | Typical person |
| --- | --- | --- |
| `platform_owner` | every school | SCHOOLCORD staff. Also every Django superuser. Has no school. |
| `school_owner` | one school, all branches | The proprietor. One per school. |
| `principal` | one branch | Branch admin. Reads setup, runs day-to-day work. |
| `bursar` | one branch | Records payments. Reads setup, changes none of it. |

`job_title` is separate, free text, and grants nothing.

### Capabilities (`apps/core/permissions.py`)

Scope answers *which rows*; capabilities answer *which actions*. The grant table
`ROLE_CAPABILITIES` is the single place grants live:

| Capability group | Platform | School owner | Principal | Bursar |
| --- | :-: | :-: | :-: | :-: |
| View academics / fees / students | ✓ | ✓ | ✓ | ✓ |
| Manage students | ✓ | ✓ | ✓ | — |
| Manage academics / fees / school profile / logo / branches / staff / admission requirements | ✓ | ✓ | — | — |
| View & send messages; view, record payments | ✓ | ✓ | ✓ | ✓ |
| Void payments | ✓ | ✓ | ✓ | — |
| View/manage/decide admissions | ✓ | ✓ | ✓ | — |
| View/record admission payments | ✓ | ✓ | ✓ | ✓ |
| View messaging identity, branches, staff | ✓ | ✓ | ✓ | — |
| Manage messaging identity (approve Sender IDs), switch school modules, deactivate accounts | ✓ | — | — | — |

`capabilities_of(user)` = the role's grants **minus** capabilities that belong to
modules the user's school does not have. Superusers get everything.

Enforcement: views use `CapabilityRequiredMixin` (`capability = Capability.X`),
which returns 403 for a signed-in user lacking it (GET and POST alike) and
redirects anonymous users to login. Templates hide controls with
`{% if "manage_fees" in capabilities %}` — presentation only; the mixin is the
enforcement.

Account creation:
- **Signup** (`/signup/`) creates exactly one school, one branch and its owner.
- School owners (and the platform) invite **Principals and Bursars** from
  `/staff/`. Nobody can be invited as School Owner or Platform Owner from the UI.
- Platform owners and additional school owners are created from the shell
  (`create_platform_owner`, `invite_school_owner` — see §11).
- The account named by `PLATFORM_OWNER_EMAIL` can never be deactivated, so the
  platform cannot lock itself out. The address is read from the environment
  only; unset, no account is protected and check `schoolcord.W005` says so.

## 7. Modules (per-school feature switches)

`apps/core/modules.py` is the registry:

| Key | Label | Default |
| --- | --- | --- |
| `academics` | Classes and subjects | always on |
| `students` | Student records | always on |
| `fees` | Fees, payments and reports | always on |
| `messaging` | Parent messaging | **off** |
| `admissions` | Admissions and enquiries | **off** |

Only a platform owner can switch modules, on `/platform/schools/<id>/`. A switched-off module answers 403 on every URL under its prefix
(`ModuleAccessMiddleware`), its capabilities are withdrawn, its navigation is
hidden, and — for admissions — the public enquiry page 404s. Switching off
deletes nothing. Details: README →
[Modules](../README.md#modules-what-each-school-has).

## 8. Deployment on Railway

### What is in the repository

| File | Role |
| --- | --- |
| `railpack.json` / `nixpacks.toml` | Tell Railway's builder (Railpack, or Nixpacks on older services) this is a Python app |
| `.python-version`, `runtime.txt` | Python 3.11 |
| `requirements.txt` | Installed at build time |
| `Procfile` | The start command (below) |

The build installs dependencies and nothing else. **No Node step** — the
compiled CSS is committed.

### The start command (`Procfile`)

```
python manage.py migrate --noinput --settings=config.settings.prod \
 && python manage.py collectstatic --noinput --settings=config.settings.prod \
 && gunicorn config.wsgi:application --bind 0.0.0.0:${PORT:-8000} \
      --workers ${WEB_CONCURRENCY:-3} --timeout 60 \
      --access-logfile - --error-logfile -
```

So **every deploy and every restart runs migrations first**, then builds the
static manifest under the same settings that will read it, then starts 3
gunicorn workers. Settings are pinned to `config.settings.prod` explicitly.

Consequences worth knowing:
- A migration that fails stops the service from starting — check the deploy
  logs first when a deploy will not come up.
- Keep the service at **one replica**: two replicas would run `migrate`
  concurrently.
- Migrations must be backwards-compatible with the code still serving during
  the deploy if you ever enable zero-downtime overlap.

### Releasing

1. Merge to the branch Railway deploys from (normally `main`, via Railway's
   GitHub integration — confirm in the service's **Settings → Source**).
2. Railway builds and runs the `Procfile` command.
3. Watch **Deployments → logs** for the `migrate` output and gunicorn's
   `Listening at`.
4. Run the deploy checks once (Railway shell or `railway run`):

   ```bash
   python manage.py check --deploy --settings=config.settings.prod
   python manage.py mailcheck --settings=config.settings.prod
   ```

### Running management commands in production

`manage.py` defaults to **dev** settings. In a Railway shell, either pass
`--settings=config.settings.prod` to every command, or set
`DJANGO_SETTINGS_MODULE=config.settings.prod` as a service variable. Without it
a command runs with the console email backend and with the SQLite fallback
allowed — mail "sent" from such a command goes nowhere.

### Domains and hosts

- `ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS` are set to the owned domains
  (`theschoolcord.com`, `www.theschoolcord.com`).
- Railway's generated domain (`RAILWAY_PUBLIC_DOMAIN`) is appended to both
  automatically by `prod.py`.
- TLS terminates at Railway's edge; `SECURE_PROXY_SSL_HEADER` trusts
  `X-Forwarded-Proto` so the HTTPS redirect does not loop.

### Production variables

Set in Railway → service → **Variables**. The full list with meanings is in
[§9](#9-environment-variables--complete-reference). The minimum for a working
production service:

```
SECRET_KEY=<50+ random characters>   # required: prod refuses to start without a real one
PLATFORM_OWNER_EMAIL=<the platform owner's login email>
DB_ENGINE=postgres
DATABASE_URL=postgresql://…supabase…
ALLOWED_HOSTS=theschoolcord.com,www.theschoolcord.com
CSRF_TRUSTED_ORIGINS=https://theschoolcord.com,https://www.theschoolcord.com
PUBLIC_BASE_URL=https://www.theschoolcord.com
TIME_ZONE=Africa/Lagos
RESEND_API_KEY=re_…
DEFAULT_FROM_EMAIL=SCHOOLCORD <noreply@send.theschoolcord.com>
MEDIA_BUCKET_NAME=media
MEDIA_S3_ENDPOINT_URL=https://<project-ref>.supabase.co/storage/v1/s3
MEDIA_ACCESS_KEY_ID=…
MEDIA_SECRET_ACCESS_KEY=…
MEDIA_S3_REGION=us-east-1
MEDIA_PRIVATE_BUCKET=true            # the default; stated so nobody flips it by accident
MESSAGING_PROVIDER=console          # until Termii approves the Sender IDs; then set it empty
TERMII_API_KEY=…                    # once messaging goes live
DJANGO_SETTINGS_MODULE=config.settings.prod   # recommended, see above
```

If a build fails with `secret MEDIA_… not found`, a variable is set to a Railway
*reference* that does not resolve — see README →
[Every one of them is read at runtime](../README.md#every-one-of-them-is-read-at-runtime-never-at-build-time).

## 9. Environment variables — complete reference

Every variable the code reads, found by searching `config/` and `apps/` for
`os.environ`. `.env` in the project root is loaded by `python-dotenv` in every
environment; on Railway, service variables take its place. Nothing else is read.

**Legend** — *Where*: **all** = read in dev and prod; **prod** = read only by
`config/settings/prod.py`; **dev** = only affects local behaviour.

### Core

| Variable | Where | Default | Purpose |
| --- | --- | --- | --- |
| `DJANGO_SETTINGS_MODULE` | all | `config.settings.dev` for `manage.py`; `config.settings.prod` for `wsgi.py`/`asgi.py` | Which settings module loads. Django's own variable. |
| `SECRET_KEY` | all | `dev-insecure-change-me` (dev only) | Django signing key (sessions, password-reset tokens, CSRF). **Required in production**: `prod.py` raises `ImproperlyConfigured` and the service will not start if it is missing, blank, the dev placeholder, or a `django-insecure-…` key. Generate one with `python -c "import secrets; print(secrets.token_urlsafe(50))"`. Changing it signs everyone out and invalidates outstanding reset links. |
| `DEBUG` | dev | `True` in dev | Debug pages and `/media/` serving. Ignored in prod (`DEBUG = False` is hard-coded). |
| `ALLOWED_HOSTS` | all | `localhost,127.0.0.1` (base); dev overrides to `*`; prod: empty | Comma-separated hostnames Django will answer for. |
| `CSRF_TRUSTED_ORIGINS` | prod | empty | Comma-separated origins **with scheme** (`https://…`) allowed to POST. |
| `RAILWAY_PUBLIC_DOMAIN` | prod | — | Injected by Railway. Appended to `ALLOWED_HOSTS` and (as `https://…`) to `CSRF_TRUSTED_ORIGINS`. Do not set by hand. |
| `TIME_ZONE` | all | `UTC` | Django time zone. Production uses `Africa/Lagos`. |
| `LOG_LEVEL` | all | `INFO` | Root logger level (console handler only). |
| `PUBLIC_BASE_URL` | all | empty | Absolute site URL, e.g. `https://www.theschoolcord.com`, no trailing slash. Needed for links inside emails (welcome email, admissions reminders, invites). Unset → those links are omitted and the welcome email is skipped; check `schoolcord.W001` warns. |
| `PLATFORM_OWNER_EMAIL` | all | empty | The one account (by email, case-insensitive) that can never be deactivated. Environment-only: unset means **no account is protected**, and check `schoolcord.W005` warns on every deploy. Set it in Railway. |
| `RATE_LIMITS_ENABLED` | all | on, except under `manage.py test` | The limits on sign-in, password reset, signup and enquiries (§6). Set `false` only as an emergency off switch. |
| `MFA_REQUIRED_FOR_PLATFORM` | all | on in production; off in dev and tests | Two-step sign-in for platform accounts (§6). Set `false` only as an emergency off switch. |
| `SENTRY_DSN` | prod | empty | Where error reports go. Empty means nothing is sent anywhere (§6). |
| `SENTRY_ENVIRONMENT` | prod | `production` | The name reports are filed under. |

### Database

| Variable | Where | Default | Purpose |
| --- | --- | --- | --- |
| `DB_ENGINE` | all | empty | `postgres` = Postgres required, no fallback; `sqlite` = always SQLite; empty = Postgres if reachable else SQLite (dev only). Set `postgres` in production. |
| `DATABASE_URL` | all | — | `postgres://` or `postgresql://` URL. Takes precedence over the `DB_*` parts. |
| `DB_NAME` | all | — | Database name (discrete form, used only when `DATABASE_URL` is unset). |
| `DB_USER` | all | `postgres` | Discrete form. |
| `DB_PASSWORD` | all | empty | Discrete form. |
| `DB_HOST` | all | `localhost` | Discrete form. |
| `DB_PORT` | all | `5432` | Discrete form. |
| `DB_POOLED` | all | auto | `1`/`0` forces transaction-pooler mode on/off (disables server-side cursors). Auto-detected from host/port 6543. |
| `DB_CONN_MAX_AGE` | prod | `60` | Seconds a DB connection is reused. |

### Email

| Variable | Where | Default | Purpose |
| --- | --- | --- | --- |
| `RESEND_API_KEY` | prod | empty | Resend API key for Anymail. Empty → check `schoolcord.W002`, and mail fails. Dev uses the console backend and ignores it. |
| `DEFAULT_FROM_EMAIL` | all | `SCHOOLCORD <noreply@send.theschoolcord.com>` | From: on all mail. Must be on the Resend-verified domain `send.theschoolcord.com`. |

There are **no** `EMAIL_HOST`/`EMAIL_PORT`/SMTP variables; any left on the host
are ignored.

### Passwords

| Variable | Where | Default | Purpose |
| --- | --- | --- | --- |
| `PWNED_PASSWORDS_ENABLED` | all | on, except under `manage.py test` | Turns the Have I Been Pwned breach check on/off. |
| `PWNED_PASSWORDS_TIMEOUT` | all | `1.5` | Seconds before the breach check gives up and falls through. |

### Messaging (SMS / WhatsApp)

| Variable | Where | Default | Purpose |
| --- | --- | --- | --- |
| `MESSAGING_PROVIDER` | all | `console` | Platform-wide override of every school's gateway. `console` logs messages and marks them delivered (nothing is sent). Empty = each school uses the gateway on its own messaging config. Valid keys: `console`, `termii`, `termii_whatsapp`, `bulksmsnigeria`, `africastalking`. |
| `TERMII_API_KEY` | all | empty | Platform's master Termii key. A school's own `api_key` on its config overrides it. |
| `TERMII_BASE_URL` | all | `https://api.ng.termii.com` | Termii API base. |
| `BULKSMSNIGERIA_API_TOKEN` | all | empty | BulkSMS Nigeria master token (gateway kept, not default). |
| `BULKSMSNIGERIA_BASE_URL` | all | `https://www.bulksmsnigeria.com` | BulkSMS Nigeria API base. |
| `BULKSMSNIGERIA_DND` | all | `2` | BulkSMS Nigeria DND route (`2` = corporate route that reaches DND numbers). |
| `AFRICASTALKING_USERNAME` | all | empty | Africa's Talking (stub provider — no HTTP call yet). |
| `AFRICASTALKING_API_KEY` | all | empty | Same. |
| `AFRICASTALKING_BASE_URL` | all | `https://api.africastalking.com` | Same. |

There is no platform-wide Sender ID variable by design: each school's Sender ID
lives in the database on its `SchoolMessagingConfig`.

### Uploaded files (object storage)

| Variable | Where | Default | Purpose |
| --- | --- | --- | --- |
| `MEDIA_BUCKET_NAME` | all | empty | **The switch.** Set → uploads go to this S3 bucket; empty → `./media` on local disk (dev/tests). In production, local disk is lost on every deploy (check `schoolcord.W004`). |
| `MEDIA_S3_ENDPOINT_URL` | all | empty | S3 endpoint, e.g. `https://<ref>.supabase.co/storage/v1/s3`. |
| `MEDIA_ACCESS_KEY_ID` | all | empty | S3 access key (Supabase → Project Settings → Storage → S3 access keys). |
| `MEDIA_SECRET_ACCESS_KEY` | all | empty | Its secret. |
| `MEDIA_S3_REGION` | all | `us-east-1` | Region string boto3 requires. |
| `MEDIA_PRIVATE_BUCKET` | all | `true` | The bucket is private: any storage URL Django builds is signed and expiring. The app itself never sends a browser to a storage URL — it streams every file through its own views — so this mainly affects the "Currently:" link on file fields in `/admin/`. Set `false` only for a deliberately public bucket. |
| `MEDIA_PUBLIC_BASE_URL` | all | empty | Only for `MEDIA_PRIVATE_BUCKET=false`: the public base URL, e.g. `https://<ref>.supabase.co/storage/v1/object/public/media`. Ignored for a private bucket. Leave unset. |
| `MEDIA_PREFIX` | all | empty | Key prefix inside the bucket, to share it with something else. Do not also put it in `MEDIA_PUBLIC_BASE_URL`. |

### Process / platform

| Variable | Where | Default | Purpose |
| --- | --- | --- | --- |
| `PORT` | Procfile | `8000` | Injected by Railway; gunicorn binds to it. |
| `WEB_CONCURRENCY` | Procfile | `3` | Number of gunicorn workers. |

## 10. Third-party integrations

### Resend (email) — live

- **How**: `django-anymail[resend]` as Django's `EMAIL_BACKEND` in production
  (`anymail.backends.resend.EmailBackend`), HTTPS API. SMTP was removed because
  the host blocks outbound port 587.
- **Config**: `RESEND_API_KEY`, `DEFAULT_FROM_EMAIL`.
- **Domain**: `send.theschoolcord.com` is the verified sending domain in Resend.
  Sending from the root domain fails until it too is verified.
- **What is sent**: welcome email on signup, password reset, password-changed
  notice, staff/owner invites (reset link), admissions emails to parents
  (enquiry acknowledgement, "continue your application" reminder, offer,
  rejection).
- **Dev**: console backend prints every email (including reset links) to the
  terminal.
- **Diagnose**: `python manage.py mailcheck [--to you@x.com [--welcome]]`, and
  deploy checks `schoolcord.W001`–`W003`. README →
  [When mail does not arrive](../README.md#when-mail-does-not-arrive).
- **Not wired**: Resend delivery/bounce webhooks. The `[resend]` extra is
  installed but no tracking-webhook URL is routed.

### Supabase (Postgres + Storage)

- **Postgres** holds all application data in the `public` schema. Connected
  with psycopg 3 using `DATABASE_URL`. Supabase offers three connection
  strings — use the one that matches the job:

  | Connection | Host:port | Use for |
  | --- | --- | --- |
  | Direct | `db.<ref>.supabase.co:5432` | App (if the host has IPv6), `pg_dump`/`pg_restore` |
  | Session pooler | `aws-…pooler.supabase.com:5432`, user `postgres.<ref>` | `pg_dump`/`psql` from an IPv4-only machine |
  | Transaction pooler | `aws-…pooler.supabase.com:6543` | App only. **Never** run `pg_dump`, `psql` admin work, or any session-level `SET` through it: the pooler shares backends with the live app and session settings leak into production connections. |

- **Storage** holds uploads in the bucket named by `MEDIA_BUCKET_NAME`
  (`media`), reached over Supabase's **S3-compatible API** by `django-storages`
  (boto3). Three non-default settings are required for Supabase:
  path-style addressing, no ACL header, never overwrite. What is stored:
  - `logos/<school-slug>/…` — school logos
  - `receipts/<school-id>/…` — payment receipt uploads
  - `admissions/<school-id>/<ENQ-reference>/…` — applicant documents
    (passport photo, birth certificate, previous results, transfer letter,
    immunisation record)

  The bucket is **private**. Files are read with the app's S3 credentials and
  streamed to the browser by three views (`apps/core/files.py` holds the shared
  part): the school logo (public), payment receipts and applicant documents
  (both gated by capability and tenant scope, `Cache-Control: private,
  no-store`). Every response carries `nosniff` and a sandboxing
  `Content-Security-Policy`, so an SVG logo opened directly cannot run script.

  The database stores only each file's **key** (e.g. `logos/fulfilled/crest.png`),
  so moving to another bucket needs no database change — see the backup guide.
- Supabase Auth, Realtime, Edge Functions and the Supabase client libraries are
  **not** used.

### Termii (SMS) — integrated, gated off in production

- **Code**: `apps/messaging/providers/termii.py`. A complete HTTP integration
  (`POST {TERMII_BASE_URL}/api/sms/send`, one request per recipient, stdlib
  `urllib`), not a stub. It is covered by tests against recorded requests.
- **Routing**: transactional messages go on Termii's `dnd` channel (reaches
  Do-Not-Disturb numbers, no overnight block); only messages explicitly marked
  promotional use `generic`.
- **Identity**: the platform holds one master Termii account
  (`TERMII_API_KEY`) and every school sends under **its own** registered
  alphanumeric Sender ID, stored on `SchoolMessagingConfig` and approved by a
  platform owner under *Sender IDs*. A school with no approved Sender ID is
  refused; there is no shared fallback name.
- **Status semantics**: Termii accepting a message = `SENT`, never `DELIVERED`.
  A balance under 100 units logs a warning on each send.
- **Production state**: `.env.example` documents running production with
  `MESSAGING_PROVIDER=console` until Termii clears the Sender IDs. While that is
  set, **no SMS leaves the platform** — messages are logged and marked
  delivered. To go live: get the Sender ID approved at Termii, approve it in
  SCHOOLCORD, set `TERMII_API_KEY`, and set `MESSAGING_PROVIDER` to empty.
  Confirm the current value in Railway before assuming either way.
- **Not wired**: Termii delivery-report webhook (no endpoint exists), so
  statuses stay at `SENT`.

### Termii WhatsApp — built, not live

`apps/messaging/providers/termii_whatsapp.py` sends Meta-approved templates via
`/api/send/template`. Nothing reaches it until a school's WhatsApp Business
number is approved and a platform owner marks it so on the school's messaging
identity.

### BulkSMS Nigeria — kept, not default

Working integration (`providers/bulksmsnigeria.py`). They declined the
one-account-many-Sender-IDs arrangement, so it is no longer the default.
`MESSAGING_PROVIDER=bulksmsnigeria` moves the whole platform back.

### Africa's Talking — stub

`providers/africastalking.py` reads settings and assembles a payload but makes
**no HTTP call**. Do not select it for a real school.

### Console provider (development)

Default everywhere. Logs each message with its Sender ID and marks it delivered.

### Have I Been Pwned

`pwned-passwords-django` checks new passwords against breached-password hashes
(k-anonymity: only a 5-character hash prefix leaves the server). Disabled under
the test runner.

## 11. Management commands

Run with `python manage.py <command>` (add `--settings=config.settings.prod` on
the server). `--help` on each lists its options.

| Command | App | Safe on production? | What it does |
| --- | --- | --- | --- |
| `create_platform_owner --email … --name "…"` | accounts | yes | Creates a platform owner (superuser, no school). Password typed at a hidden prompt, or `--email-link` to email a reset link, or `--temporary-password`. |
| `invite_school_owner --school <slug> --email … --name "…"` | accounts | yes | Adds an owner account to an **existing** school; emails a reset link. `--temporary-password`, `--no-email` available. |
| `mailcheck [--to addr] [--welcome]` | core | yes | Prints the mail configuration; optionally sends a test or the real welcome email. |
| `send_enquiry_reminders [--dry-run]` | admissions | yes — nightly | Emails a parent once, `reminder_after_days` (default 7) after an enquiry that has not become an application. |
| `expire_enquiries [--dry-run]` | admissions | yes — nightly | Marks enquiries past their validity window expired. |
| `settle_enrolment_payment [--commit]` | admissions | yes, carefully | One-off repair: moves the term share of an old all-in admission payment onto the term ledger. **Dry run unless `--commit`.** |
| `reset_platform_mfa --email …` | accounts | yes, carefully | Clears a platform account's authenticator when the phone and recovery codes are lost. Recorded in the audit log. |
| `report_wrong_child_risk [--school <slug>]` | payments | yes, read-only | CSV of past payments a since-fixed picker bug may have credited to the wrong child. Says what to check, not what is wrong. Treat the output as confidential. |
| `check_payment_transactions` | payments | yes, read-only | Checks every payment has a transaction, one-child transactions match their payment, and no shared transaction has handed out more than it received. Exits 1 and names the rows if not. |
| `purge_import_files [--days 30]` | onboarding | yes — nightly | Removes uploaded workbooks and their copied rows from imports finished that long ago, and deletes abandoned drafts. Imported records are kept. |
| `bootstrap_tenant --password …` | core | **NO** | Demo school + one user per role, including a superuser. Local only. |
| `seed_academics`, `seed_fees`, `seed_students`, `seed_payments`, `seed_admission_fees` | various | **NO** | Demo data. Local only. |
| `seed_messaging_identity` | messaging | only deliberately | Registers **and approves** a school's Sender ID without the UI review. |

## 12. Scheduled jobs

Two admissions commands are designed to run nightly, in this order:

```bash
python manage.py send_enquiry_reminders --settings=config.settings.prod
python manage.py expire_enquiries --settings=config.settings.prod
```

**Nothing in this repository schedules them.** The `Procfile` defines only the
web process. On Railway, add a second service from the same repo with:

- **Cron schedule**: e.g. `0 6 * * *` (Railway cron is UTC; 06:00 UTC is 07:00 Lagos)
- **Start command**: the two lines above joined with `&&`
- **Variables**: the same as the web service (at least the database, mail and
  `PUBLIC_BASE_URL` ones)

The command must exit when done (both do). Missing runs are harmless — the
public board computes `has_lapsed` live — but reminders will not be sent and
expired enquiries will not be closed until the job runs. Only needed for schools
with the admissions module on.

## 13. Operations runbook

| Task | How |
| --- | --- |
| Check deployment health | `python manage.py check --deploy --settings=config.settings.prod` — look for `schoolcord.W001`–`W006` and Django's `security.*` warnings. |
| Mail not arriving | `mailcheck` (above); then confirm `PUBLIC_BASE_URL`, `RESEND_API_KEY`, and the domain in Resend. |
| Logos/receipts 404 or vanish after deploy | `MEDIA_BUCKET_NAME` unset in Railway → uploads went to the container's disk. Set the `MEDIA_*` variables; affected files must be re-uploaded. If the variables are set, check the S3 key pair can read the bucket (`python manage.py shell` → `default_storage.exists("<key>")`). |
| Add a platform owner | `create_platform_owner` (above). |
| A school needs a second owner | `invite_school_owner --school <slug> …` (not possible from the UI by design). |
| Lock out an account | `/staff/` (Users, at platform scope) → deactivate. Nothing is deleted; reactivating restores access. |
| Switch a feature on for a school | `/platform/schools/<id>/` → module switches. |
| Approve a school's Sender ID | Sidebar → Sender IDs (`/messaging/identity/`). |
| Pause a school | Set its **status** in the admin: Past due makes it read-only, Suspended or Cancelled locks its staff out (§6). Everything is kept and changing it back restores access. |
| Close a school | Prefer setting its **status** to Suspended/Cancelled (keeps everything). Permanent deletion: Django admin → Schools → delete (one confirmation, one transaction). Take a backup first. |
| Upgrade dependencies | Bump pins in `requirements.txt`, run the full test suite with `DB_ENGINE=sqlite`, deploy. |
| Backups | [BACKUP_AND_RESTORE.md](BACKUP_AND_RESTORE.md). |
| Logs | Railway → service → Deployments → logs (gunicorn access log and Django's console logging). |

## 14. Known gaps and open items

Found while writing this handover.

**Resolved in the handover PR**

- Uploaded documents were reachable at public, predictable URLs
  (`…/object/public/media/admissions/<school-id>/ENQ-<year>-<n>/…`). The bucket
  is now private and every file goes through a gated view (§10).
- Production started on the placeholder `SECRET_KEY`; it now refuses to.
- `PLATFORM_OWNER_EMAIL` defaulted to a personal address in code; it is now
  environment-only, with check `schoolcord.W005`.
- `venv/` was tracked in git despite `.gitignore`; it no longer is.

**Known limitations — documented, not fixed**

1. **No delivery webhooks** for Termii or Resend: SMS stays `SENT`; email
   bounces are invisible in the app. The `[resend]` Anymail extra is installed
   but no tracking-webhook URL is routed.
2. **Per-school gateway API keys are stored in plain text** in
   `messaging_schoolmessagingconfig.api_key`, so database dumps contain secrets.
   Treat every backup as sensitive (see the backup guide).

**Open items**

3. **No scheduled job is configured in the repo** for the two nightly admissions
   commands (§12).
4. **SMS is in console mode in production** per `.env.example` until Termii
   approves Sender IDs — confirm the Railway value.
5. **Africa's Talking** is a stub.
6. **CI runs on pull requests** — `.github/workflows/ci.yml`: the suite on Postgres,
   browser smoke tests, dependency and secret scans. GitHub has to be told to require
   it before merging (Settings → Branches).
7. **Backups** depend on the Supabase plan (the Free plan has no automated
   backups) and Storage objects are **not** included in Supabase database
   backups. See the backup guide for a manual routine.
8. **Four of the five applicant documents are not linked anywhere in the staff
   screens.** Parents upload a birth certificate, previous results, a transfer
   letter and an immunisation record; only the passport photo is shown. The
   gated view already serves all five (`/admissions/<id>/documents/<kind>/`), so
   linking them is a template change.
9. **`/admin/` file links** ("Currently: …" on a logo, receipt or document
   field) are signed storage URLs, valid for an hour. They work only if the
   storage host honours S3 pre-signed URLs; the app's own screens do not depend
   on that.
