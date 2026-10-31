# SCHOOLCORD — Database backup & restore

How to take a full copy of SCHOOLCORD's data and bring it up in a fresh
PostgreSQL or Supabase instance. Two things make up "the data":

| What | Where | Tool |
| --- | --- | --- |
| Every record — schools, users, students, fees, payments, messages, admissions | Supabase Postgres, schema `public` | `pg_dump` / `pg_restore` |
| Uploaded files — school logos, payment receipts, applicant documents | Supabase Storage, bucket `media` | `rclone` (or the AWS CLI) over the S3 API |

**Supabase's own database backups do not include Storage files.** A complete
backup always has both halves.

> **Handle backups as sensitive data.** A dump contains password hashes, staff
> and parent names, parent phone numbers, payment history, and per-school
> messaging API keys (stored in plain text). The bucket contains children's
> birth certificates and photos. Encrypt backups at rest, keep them out of the
> git repository, and delete old copies you no longer need.

## Contents

1. [Before you start](#1-before-you-start)
2. [Back up the database](#2-back-up-the-database)
3. [Back up the storage bucket](#3-back-up-the-storage-bucket)
4. [Restore the database into a fresh instance](#4-restore-the-database-into-a-fresh-instance)
5. [Restore / migrate the storage bucket](#5-restore--migrate-the-storage-bucket)
6. [Point the app at the new instance](#6-point-the-app-at-the-new-instance)
7. [Verify](#7-verify)
8. [Full migration checklist](#8-full-migration-checklist)
9. [Routine backups](#9-routine-backups)
10. [Troubleshooting](#10-troubleshooting)

---

## 1. Before you start

### Tools

| Tool | Why | Install |
| --- | --- | --- |
| `pg_dump`, `pg_restore`, `psql` | Database dump/restore | Must be the **same major version as the Supabase server, or newer**. Check the server with the `psql` query below. Windows: the PostgreSQL installer from postgresql.org, choosing *Command Line Tools* only. macOS: `brew install libpq && brew link --force libpq`. Debian/Ubuntu: `apt install postgresql-client-17` from the PGDG apt repository. Or use Docker (below). |
| `rclone` | Copy the storage bucket | https://rclone.org/install/ |
| AWS CLI v2 (optional) | Alternative to rclone | https://aws.amazon.com/cli/ |

With Docker you need no local install; replace `pg_dump …` with:

```bash
docker run --rm -v "$PWD:/backup" -w /backup postgres:17 pg_dump …
```

(use the image tag that matches the server's major version).

### Get the right connection string

In the Supabase dashboard: **Connect** (top bar) → **Connection string**.
Supabase offers three. For backup and restore use **Direct** or **Session
pooler** — both on port **5432**:

| Option | Looks like | Use when |
| --- | --- | --- |
| Direct | `postgresql://postgres:<PASSWORD>@db.<ref>.supabase.co:5432/postgres` | Your machine has IPv6 (direct connections are IPv6-only unless the project has the IPv4 add-on) |
| Session pooler | `postgresql://postgres.<ref>:<PASSWORD>@aws-0-<region>.pooler.supabase.com:5432/postgres` | IPv4-only networks (most home and office connections) |
| ~~Transaction pooler~~ | `…pooler.supabase.com:6543/postgres` | **Never for backup/restore.** |

> **Do not use port 6543 for any of this.** The transaction pooler shares
> server connections with the live app. `pg_dump` and `pg_restore` issue
> session-level `SET` commands (`search_path`, `statement_timeout`, …), and
> through that pooler those settings can stay on a shared connection and be
> handed to production requests. This has already broken production once with
> a read-only setting. The local `.env` on the original developer's machine
> points at port 6543 — do not reuse it here.

Put the URLs in shell variables so passwords stay out of the commands you
paste. Wrap them in single quotes; URL-encode special characters in the
password (`@` → `%40`, `#` → `%23`, `/` → `%2F`).

```bash
# bash / Git Bash / macOS / Linux
export SOURCE_DB_URL='postgresql://postgres.<ref>:<PASSWORD>@aws-0-<region>.pooler.supabase.com:5432/postgres'
```

```powershell
# PowerShell
$env:SOURCE_DB_URL = 'postgresql://postgres.<ref>:<PASSWORD>@aws-0-<region>.pooler.supabase.com:5432/postgres'
```

The commands below use bash syntax; in PowerShell write `$env:SOURCE_DB_URL`
for `"$SOURCE_DB_URL"` and use a backtick instead of `\` for line breaks.

### Check the server version

```bash
psql "$SOURCE_DB_URL" -c "select version();"
pg_dump --version
```

`pg_dump`'s major version must be **≥** the server's. An older client aborts
with `server version mismatch`.

## 2. Back up the database

All of SCHOOLCORD's tables are in the `public` schema (the app uses Django's
own auth, not Supabase Auth). Dumping `public` is therefore a complete copy of
the application, and it is the dump that restores cleanly into **any**
Postgres — a new Supabase project or a plain server.

```bash
STAMP=$(date +%Y%m%d-%H%M)

pg_dump "$SOURCE_DB_URL" \
  --schema=public \
  --format=custom \
  --no-owner \
  --no-privileges \
  --verbose \
  --file="schoolcord-db-$STAMP.dump"
```

| Flag | Why |
| --- | --- |
| `--schema=public` | Only the application's tables. Leaves Supabase's managed schemas (`auth`, `storage`, `realtime`, …) out — they belong to the platform and conflict on restore. |
| `--format=custom` | Compressed, and lets `pg_restore` pick, reorder and filter items. |
| `--no-owner --no-privileges` | Role names and grants differ between instances; the restoring user becomes the owner. |

`pg_dump` takes a consistent snapshot without locking writers out, so the
site can stay up. For a **migration** (not a routine backup), stop writes first
— see [§8](#8-full-migration-checklist).

Check the dump is readable and lists the app's tables:

```bash
pg_restore --list "schoolcord-db-$STAMP.dump" | grep "TABLE DATA" | head -40
```

You should see `django_migrations`, `accounts_user`, `schools_school`,
`schools_branch`, `students_student`, `payments_payment`,
`admissions_applicant`, `messaging_message`, and the rest.

Optional, for a human-readable copy as well:

```bash
pg_dump "$SOURCE_DB_URL" --schema=public --no-owner --no-privileges \
  --format=plain --file="schoolcord-db-$STAMP.sql"
```

### Record row counts (to verify the restore later)

```bash
psql "$SOURCE_DB_URL" -At -c "
select 'schools', count(*) from schools_school
union all select 'branches', count(*) from schools_branch
union all select 'users', count(*) from accounts_user
union all select 'students', count(*) from students_student
union all select 'payments', count(*) from payments_payment
union all select 'applicants', count(*) from admissions_applicant
union all select 'messages', count(*) from messaging_message
union all select 'migrations', count(*) from django_migrations;" \
  > "schoolcord-counts-$STAMP.txt"
cat "schoolcord-counts-$STAMP.txt"
```

## 3. Back up the storage bucket

The bucket name is the Railway variable `MEDIA_BUCKET_NAME` (`media`). Inside
it, files are kept under:

```
logos/<school-slug>/…                      school logos
receipts/<school-id>/…                     payment receipt uploads
admissions/<school-id>/<ENQ-reference>/…   applicant documents
```

(Under an extra prefix if `MEDIA_PREFIX` is set in Railway.)

### Get S3 credentials

Supabase dashboard → **Project Settings → Storage → S3 Connection**: note the
**Endpoint** (`https://<ref>.supabase.co/storage/v1/s3`) and **Region**, then
**New access key** to get an access key ID and secret. These are the same kind
of keys as Railway's `MEDIA_ACCESS_KEY_ID` / `MEDIA_SECRET_ACCESS_KEY`; you can
reuse those.

### Option A — rclone (recommended)

Create a remote once. Add to your rclone config (`rclone config file` shows
where it is):

```ini
[schoolcord-old]
type = s3
provider = Other
endpoint = https://<old-ref>.supabase.co/storage/v1/s3
region = <region shown on the S3 Connection page>
access_key_id = <access key id>
secret_access_key = <secret>
force_path_style = true
no_check_bucket = true
```

Then copy the whole bucket to a local folder:

```bash
rclone copy schoolcord-old:media ./schoolcord-media-$STAMP --progress --checksum
rclone size schoolcord-old:media           # object count + bytes in the bucket
rclone size ./schoolcord-media-$STAMP      # must match
```

### Option B — AWS CLI

```bash
aws configure --profile schoolcord-old      # paste the key id + secret; region as above
aws s3 sync s3://media ./schoolcord-media-$STAMP \
  --profile schoolcord-old \
  --endpoint-url https://<old-ref>.supabase.co/storage/v1/s3
```

If AWS CLI v2.23+ fails with a checksum error, set
`AWS_REQUEST_CHECKSUM_CALCULATION=when_required` and
`AWS_RESPONSE_CHECKSUM_VALIDATION=when_required` in the environment and retry.

A complete backup is now: `schoolcord-db-<STAMP>.dump`,
`schoolcord-counts-<STAMP>.txt` and the `schoolcord-media-<STAMP>/` folder.
Archive and encrypt them together, for example:

```bash
tar czf - schoolcord-db-$STAMP.dump schoolcord-counts-$STAMP.txt schoolcord-media-$STAMP \
  | gpg --symmetric --cipher-algo AES256 -o schoolcord-backup-$STAMP.tar.gz.gpg
```

## 4. Restore the database into a fresh instance

### 4a. Create the target

**New Supabase project**: create it in the dashboard (pick the region near
your users), note the database password, and copy its **Direct** or **Session
pooler** URL (port 5432) from **Connect**.

**Plain PostgreSQL** (self-hosted, RDS, Neon, Railway Postgres, …): create an
empty database and a user that owns it:

```sql
CREATE ROLE schoolcord LOGIN PASSWORD '<strong password>';
CREATE DATABASE schoolcord OWNER schoolcord;
```

Use a server of the same or a newer major version than the source.

```bash
export TARGET_DB_URL='postgresql://…target…:5432/postgres'
```

The target database must be **empty of SCHOOLCORD tables**. Do not run
`manage.py migrate` against it first; the dump creates the tables and its own
`django_migrations` history.

### 4b. Restore

A fresh database already has a `public` schema, and the dump contains a
`CREATE SCHEMA public` entry that would fail with *schema "public" already
exists*. Build a restore list without it, then restore from the list:

```bash
pg_restore --list schoolcord-db-$STAMP.dump \
  | grep -vE ' SCHEMA - public | COMMENT - SCHEMA public ' \
  > restore.list

pg_restore \
  --dbname="$TARGET_DB_URL" \
  --use-list=restore.list \
  --no-owner \
  --no-privileges \
  --single-transaction \
  --exit-on-error \
  --verbose \
  schoolcord-db-$STAMP.dump
```

`--single-transaction` with `--exit-on-error` means the restore either lands
completely or leaves the target untouched — rerun after fixing whatever it
reports. Table data, primary keys, foreign keys, indexes and sequence positions
all come from the dump, so new rows continue numbering where the old database
left off.

For a plain-SQL dump instead: `psql "$TARGET_DB_URL" -v ON_ERROR_STOP=1 -1 -f schoolcord-db-$STAMP.sql`
(delete its `CREATE SCHEMA public;` line first if present).

### 4c. Check the schema is current

From a checkout of the code that will run against it, with **no** `.env`
pointing elsewhere:

```bash
DB_ENGINE=postgres DATABASE_URL="$TARGET_DB_URL" \
  python manage.py showmigrations --settings=config.settings.prod | grep '\[ \]'
```

No output means every migration in the code is already applied. If the code is
newer than the backup, the next deploy's `migrate` (in the `Procfile`) applies
the rest.

## 5. Restore / migrate the storage bucket

1. In the **target** Supabase project: **Storage → New bucket**, name it exactly
   as before (`media`). Make it **public** if the app runs with
   `MEDIA_PRIVATE_BUCKET` unset/false, **private** if it is `true`.
2. **Project Settings → Storage → S3 Connection → New access key.**
3. Add a second rclone remote for it:

   ```ini
   [schoolcord-new]
   type = s3
   provider = Other
   endpoint = https://<new-ref>.supabase.co/storage/v1/s3
   region = <new project's region>
   access_key_id = <new key id>
   secret_access_key = <new secret>
   force_path_style = true
   no_check_bucket = true
   ```

4. Upload, preserving every key exactly:

   ```bash
   # from the local backup
   rclone copy ./schoolcord-media-$STAMP schoolcord-new:media --progress --checksum

   # or straight from bucket to bucket during a migration
   rclone copy schoolcord-old:media schoolcord-new:media --progress --checksum

   rclone size schoolcord-new:media     # must match the source
   ```

   With the AWS CLI: `aws s3 sync ./schoolcord-media-$STAMP s3://media --profile schoolcord-new --endpoint-url https://<new-ref>.supabase.co/storage/v1/s3`.

**Why no database change is needed.** Each `FileField` column stores only the
object key — `schools_school.logo`, `payments_payment.receipt`, and
`admissions_applicant.passport_photo`, `birth_certificate`, `previous_results`,
`transfer_letter`, `immunisation_record` hold strings like
`logos/fulfilled/crest.png`. The URL a browser gets is built at request time
from `MEDIA_PUBLIC_BASE_URL`. Copy the keys unchanged, change the variables,
and every link points at the new bucket. Keep `MEDIA_PREFIX` the same as before
(or move the objects under the new prefix).

**Moving to a non-Supabase bucket** (S3, Cloudflare R2, Backblaze B2): same
rclone copy with that provider's remote. Then set `MEDIA_S3_ENDPOINT_URL` to
its endpoint and either leave `MEDIA_PUBLIC_BASE_URL` empty (django-storages
builds the standard URL) or set it to the bucket's public URL.

**Moving from local disk** (a machine that ran without `MEDIA_BUCKET_NAME`):
`rclone copy ./media schoolcord-new:media` — the folder layout under `./media`
is the same key layout.

## 6. Point the app at the new instance

In Railway → the web service → **Variables** (and the cron service, if you
added one), change:

| Variable | New value |
| --- | --- |
| `DATABASE_URL` | The target's URL. For the app, the Direct URL (if Railway can reach IPv6) or the pooler URL. |
| `DB_ENGINE` | `postgres` (unchanged) |
| `MEDIA_S3_ENDPOINT_URL` | `https://<new-ref>.supabase.co/storage/v1/s3` |
| `MEDIA_ACCESS_KEY_ID` / `MEDIA_SECRET_ACCESS_KEY` | The new key pair |
| `MEDIA_S3_REGION` | The new region (or keep `us-east-1`; Supabase does not check it) |
| `MEDIA_PUBLIC_BASE_URL` | `https://<new-ref>.supabase.co/storage/v1/object/public/media` (public bucket only) |

Saving variables triggers a redeploy; the `Procfile` runs `migrate`
(a no-op if the schema is current), `collectstatic`, then gunicorn.

If the app's `DATABASE_URL` uses the transaction pooler (port 6543), it is
auto-detected and server-side cursors are disabled — that is correct and
expected for the app itself.

## 7. Verify

1. **Row counts** — run the count query from [§2](#record-row-counts-to-verify-the-restore-later)
   against `$TARGET_DB_URL` and compare with `schoolcord-counts-<STAMP>.txt`.
   They must match exactly.
2. **Deploy checks** (Railway shell):

   ```bash
   python manage.py check --deploy --settings=config.settings.prod
   ```

   `schoolcord.W004` means uploads are going to local disk — the `MEDIA_*`
   variables are not set.
3. **Every stored file exists in the new bucket** (Railway shell; read-only):

   ```bash
   python manage.py shell --settings=config.settings.prod
   ```

   ```python
   from django.core.files.storage import default_storage
   from apps.schools.models import School
   from apps.payments.models import Payment
   from apps.admissions.models import Applicant

   # No request is active in a shell, so these managers are unscoped: every school.
   checks = [(School, ["logo"]), (Payment, ["receipt"]),
             (Applicant, ["passport_photo", "birth_certificate", "previous_results",
                          "transfer_letter", "immunisation_record"])]
   missing = []
   total = 0
   for model, fields in checks:
       for obj in model.all_objects.all().iterator():
           for f in fields:
               name = getattr(obj, f).name
               if name:
                   total += 1
                   if not default_storage.exists(name):
                       missing.append((model.__name__, obj.pk, f, name))
   print(total, "files referenced;", len(missing), "missing")
   for row in missing[:50]:
       print(row)
   ```

   Files that were already missing in the old bucket (for example, logos
   uploaded before object storage was switched on) will show here too.
4. **By hand**: sign in as a platform owner; open a school with a logo; open a
   payment with a receipt; sign in as a school owner and check a dashboard's
   totals; reset a password to prove mail still sends.

## 8. Full migration checklist

Moving production to a new Supabase project (or any new Postgres + bucket):

1. Create the target database and bucket (§4a, §5 steps 1–2).
2. Announce a short maintenance window.
3. **Stop writes**: in Railway, stop the web service (remove its active
   deployment, or scale it down) and pause the cron service. Nothing should be
   able to record a payment between the dump and the switch.
4. Dump the database (§2) and record row counts.
5. Restore into the target (§4b) and check migrations (§4c).
6. Copy the bucket (§5 step 4).
7. Update Railway variables (§6) and redeploy.
8. Verify (§7).
9. Keep the old project **read-only and untouched** for at least two weeks,
   then delete it once you are sure nothing is missing.

## 9. Routine backups

- **Supabase's built-in backups** cover the database only, and depend on the
  plan: the Free plan has none; paid plans keep daily backups for a limited
  number of days, with point-in-time recovery as an add-on. Check
  **Database → Backups** in the dashboard to see what this project has.
  Storage objects are never included.
- **Recommended routine**: run §2 (database dump + counts) and §3 (bucket copy)
  at least weekly and before every risky change — a migration that rewrites
  data, a dependency upgrade, deleting a school. Keep the encrypted archive
  somewhere other than Supabase (an external drive, a separate cloud account).
- **Test a restore** into a scratch Postgres every few months. A backup that has
  never been restored is a hope, not a backup. A local Docker Postgres is
  enough:

  ```bash
  docker run -d --name schoolcord-restore-test -e POSTGRES_PASSWORD=test -p 55432:5432 postgres:17
  export TARGET_DB_URL='postgresql://postgres:test@localhost:55432/postgres'
  # then §4b and the row-count check from §7
  docker rm -f schoolcord-restore-test
  ```

## 10. Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| `pg_dump: error: aborting because of server version mismatch` | Client older than server. Install the matching client or use the `postgres:<major>` Docker image. |
| `could not translate host name "db.<ref>.supabase.co"` / `Network is unreachable` | The direct host is IPv6-only. Use the **Session pooler** URL (port 5432). |
| `prepared statement … does not exist`, odd session errors | You used the transaction pooler (6543). Switch to 5432. |
| `schema "public" already exists` | Restore without the filtered list from §4b. Use `--use-list=restore.list`. |
| `relation "…" already exists` | The target is not empty — someone ran `migrate` against it, or a previous restore half-ran without `--single-transaction`. Drop and recreate the target database (or create a new project) and restore again. |
| `role "…" does not exist` | Missing `--no-owner --no-privileges` on dump or restore. |
| `password authentication failed` | Special characters in the password not URL-encoded, or (session pooler) the user is not written `postgres.<ref>`. |
| rclone `AccessDenied` / `SignatureDoesNotMatch` | Wrong key pair or endpoint; `force_path_style = true` missing. |
| rclone tries to create the bucket and fails | Add `no_check_bucket = true` to the remote. |
| Images 404 after the move | `MEDIA_PUBLIC_BASE_URL` still points at the old project, the new bucket is private while the app expects public (or the reverse), or `MEDIA_PREFIX` differs. |
