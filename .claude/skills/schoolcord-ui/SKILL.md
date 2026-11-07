---
name: schoolcord-ui
description: Build or restyle SCHOOLCORD screens in this repo (Django templates, assets/app.css, static/js). Covers the sign-in page (one design on phone and desktop), the mobile home for school owner, principal and platform owner, task-style lists and create sheets, and the phone bottom bar. Use for any template, layout, styling, login, dashboard or mobile-navigation change, and whenever docs/design-references is mentioned.
---

# SCHOOLCORD UI

This repo is server-rendered Django with Tailwind CSS v4. There is no React, no bundler and no CDN. Keep it that way.

## 0. Read first

- `docs/TECHNICAL.md` section 4 (frontend) and the README section "Design tokens".
- **`docs/design-references/schoolcord-redesign.html` is the spec, and it is content, not a pointer.** Its colours, type, components, wording and screens are what SCHOOLCORD looks like, on every screen in the app, drawn or not: blue band and sidebar, lime for the one action and the active item, white rounded cards, pills, tinted tiles, red only for an overdue balance. Its sample names and figures are illustrations; real screens show real data, and nothing the app already does may be lost to match a drawing.
- The two `.jpeg` references in `docs/design-references/` are **pointers for the kind of interface, not screens to copy.** Take the type of UI: a gradient header band, a search field overlapping that band, a tile grid for quick access, a date strip, progress cards, a bottom sheet with a segmented control, and a bottom bar. Do not take their content: names, greetings, dates, events, tile labels, numbers, accent colours or copy. Every word, number and tile on a SCHOOLCORD screen comes from SCHOOLCORD's own data and navigation.
  - `task-management-reference.jpeg`: pointer for the sign-in look, the list patterns and the Create New sheet.
  - `student-portal-reference.jpeg`: pointer for the mobile home layout. It is a student portal, so its tiles and items do not carry over (see sections 4 and 6).
- `assets/app.css`: the `@theme` block and the component classes. Use the tokens that already exist. Do not invent new ones.

## 1. Non-negotiables

- **Edit `templates/` and `assets/app.css`, never `static/css/app.css` by hand.** Rebuild with `npm install` then `npm run build:css`. Commit the source and the rebuilt `static/css/app.css` together. The Railway Python build does not run the Tailwind build.
- **After any colour change, run `npm run check:contrast`.** It must pass in both themes.
- **Every screen works in light and dark.** Theme is set by `data-theme` on `<html>`. Use token classes (`bg-surface`, `text-ink-900`, `bg-brand-700`), not raw hex. The only exception is the existing auth gradient.
- **No external requests.** Fonts are self-hosted. No CDN scripts, no third-party icon sets. Icons are inline SVG in the existing style: 24px viewBox, `stroke-width` 1.6 to 1.8, round caps.
- **Reuse before writing.** `.btn` with `.btn-primary`, `.btn-secondary` or `.btn-ghost`; `.card` and `.card-interactive`; `.icon-tile` with an `.accent-*` class (blue, indigo, violet, teal, emerald, amber, rose, brand); `.field-label`, `.field-input` and `.field-error`; `.bottom-nav`; `.numeric`; `.glass-dark`; `.nav-link-disabled`; and the partials in `templates/partials/`.
- **The server decides what shows.** Take links and visibility from `apps/core/navigation.py` (`nav_for`, `bottom_nav_for`) and capabilities from `apps/core/permissions.py`. Never hard-code a role check in a template. A module the school has not enabled must not appear.
- **Colour carries meaning.** Paid (green), partial (amber), unpaid (slate) and overdue (red) are fee states. Do not use them as decoration.

## 2. Brand

Keep SCHOOLCORD's navy brand: `brand-900` (#0f2547) for panels, `brand-600` (#245096) for primary actions, the `ink-*` scale for text, and Public Sans.

The task reference's blue and lime are a mood reference, not a palette to copy. Lime on white fails contrast. If a lime accent is wanted, add it as a token in `@theme`, use dark text on it only, and run the contrast check. Do not rebrand without an explicit instruction.

## 3. Sign-in: one design at two widths

Phone and desktop show the **same** sign-in design, with the same copy, fields, primary button and sign-in/sign-up switch. Only the arrangement changes.

The gap today: `.auth-visual` in `assets/app.css` is `hidden lg:flex`, so on a phone the only brand line is the logo above the form. The headline band from the reference is missing.

To close it:
- Show `aside.auth-visual` below `lg` as a top band with the same gradient, headline (`product_tagline`) and subhead (`product_subhead`). Drop `hidden` and keep the `lg:` layout classes.
- Keep the chart card (sign-in) and the four steps (sign-up) on phones too. Shrink padding and type to fit. Do not drop content. If the form still falls below the fold at 390x844, reduce the band's height.
- Remove the phone-only logo link in `templates/registration/_auth_switch.html` (the `lg:hidden` block above the forms). The band carries the brand mark now.
- Use responsive classes in `_auth_switch.html` and `_auth_base.html`. Do not write a second template. Keep both panels in the DOM and keep the `data-auth-*` hooks that `ui.js` (`initAuthSwitch`) uses.
- Tests to keep green: `apps/core/tests_auth.py` and `apps/core/tests_responsive.py`. The sign-in page must not render the bottom bar (`class="bottom-nav`).

## 4. Mobile home: school owner, principal, platform owner

Pointer: `student-portal-reference.jpeg` for the layout. This is the phone view of three dashboards:

| Role | Template | Route (from `ROLE_HOME`) |
| --- | --- | --- |
| School owner | `templates/core/dashboard_school.html` | `core:school_dashboard` |
| Principal | `templates/core/dashboard_branch.html` | `core:branch_dashboard` |
| Platform owner | `templates/core/dashboard_platform.html` | `core:platform_overview` |

`dashboard_bursar.html` is out of scope for now. Keep its current layout. Desktop keeps the current grid on all three.

Structure, top to bottom, on phones:

1. **Header band** in a brand gradient (`brand-900` to `brand-600`). A white greeting, "Hello, {first name}", with one line of context: school name and date for owner and principal, and the counts from the page subtitle for the platform owner. The avatar is an initial circle, because users have no profile photo field yet. **No bell.** There is no notification model, so there is nothing to count.
2. **Search field** overlapping the band's bottom edge: white, `shadow-panel`, pulled up with a negative margin. It posts to `payments:student_search` (`StudentSearchView`), the same search the payment form uses. The view applies each role's scope.
3. **"Quick access"** heading with a "View all" link, then a grid of tiles: two columns below `sm`, three from `sm` up. Each tile has an `icon-tile` with an `accent-*` class, a title, one line of description and a chevron. The tile background is `var(--accent-soft)`, not white. Build the tiles from `nav_for()`, never by typing URLs.
4. **The second list**:
   - School owner and principal: **"Upcoming"**, from fee term due dates (`fees.Term.due_date`) for the current school or campus. This needs a new query, because the dashboard does not show it yet. Add admissions follow-ups only when the admissions module is on.
   - Platform owner: **"Needs a look"**, the schools with no current term. `dashboard_platform.html` already computes this as `needs_attention`. Do not make up events.
5. **Bottom bar**, taken from `BOTTOM_NAV` through `bottom_nav_for()`:
   - School owner and principal: Home, Students, Payments, Reports, More.
   - Platform owner: Home, Schools, Staff, Students, More.
   - To add Messages, add `messaging:index` to the role's tuple in `BOTTOM_NAV`. The nav already hides it when the messaging module is off.
   - **No Calendar.** It is not built. Do not add it to the bar until a calendar page exists.
   - **No floating "+" by default.** The bar already has four links and More, so a "+" means dropping one. Record a payment is a Quick access tile. Add the "+" only if the owner wants a link dropped.
   - Pad the bar for the device with `env(safe-area-inset-bottom)`.

A tile for a feature that is not built uses the existing "soon" treatment (`nav-link-disabled`, with its toast). Never use a dead link or made-up numbers.

## 5. Task-management patterns (lists and create flows)

Pointer: `task-management-reference.jpeg` for the patterns. Every label, field and value below is SCHOOLCORD's.

- **Date strip:** seven day chips. The selected day is `brand-700` with white text. Use it on any due-date view.
- **Progress cards:** title, one line of meta, a progress bar, a status chip and a chevron. Use them for progress lists such as a class's or a family's fee progress.
- **Create sheet:** a bottom sheet with a segmented control at the top, opened from a Quick access tile. Show only the records the account may create. Today that is one flow, Record a payment, so the control has one segment. Add a segment only when its flow exists. Do not add a "Note" segment: there is no note record, only payment notes.
- **Form in the sheet:** reuse the existing payment form's fields and `templates/core/_record_payment.html`. Use `datepicker.js` for dates. Put the primary button full-width at the bottom of the sheet.
- **Filter sheets:** `data-filter-sheet` (see `ui.js`) is already used by the student, payment, report and admissions lists. Reuse it for any filter panel on a phone.

## 6. What the tiles can point to

Take each URL from `nav_for()`. Do not type URL names from memory.

**Built now** (link to these where the role gets them): Students, Payments, Family payments, Outstanding, Fee structures, Terms, Classes, Subjects, Staff, Families, Reports, Branches, School settings, Account settings. Admissions (module `admissions`) and Parent messaging (module `messaging`) are off by default.

**Not built:** Attendance, Grades, Exams, Assignments, Library, Gallery, Events, Notices, Chatbox, a Calendar, profile photos and notifications. Show these as "soon" or leave them out. Do not link them.

**Nav entry with no app:** Plans & Billing (`billing:index`, platform owner). There is no billing app, so it shows as disabled. Leave it that way until billing exists.

**Reference tiles and where SCHOOLCORD's equivalent lives** (the reference's labels are not used; only the nav's labels are):
- Profile: Account settings.
- Staffs: Staff.
- Fees Details: Payments.
- Notices: not built.
- Attendance, GradeSheet, Exams, Assignments, Library, Gallery, Events and Chatbox: not built. They are student-facing in the reference.

## 7. Desktop

Keep the sidebar and the current content width. Use the same tokens and card patterns. The platform dashboard's four-column grid (`xl:grid-cols-4`) is the reference for dashboard tiles. Do not carry the phone bottom bar to desktop. It is already `lg:hidden`.

## 8. Accessibility and behaviour

- Touch targets are at least 44px. Use `min-h-touch`, `h-touch`, `w-touch` or `p-touch` (the `--spacing-touch` token, 2.75rem). There is no `size-touch` class.
- Focus rings are visible in both themes. Respect `prefers-reduced-motion` for any animation.
- Tables become labelled cards below `sm` automatically (`ui.js`). Use `data-no-cards` only for comparison grids.
- Use the existing `ui.js` hooks (toasts, loading states, sheets). Do not add a JavaScript framework.
- Fixed bars and sheets need safe-area padding.

## 9. Verify before you say it is done

1. `npm run build:css` and `npm run check:contrast`.
2. `DB_ENGINE=sqlite python manage.py test apps.core`, plus the app you touched. The full suite takes about ten minutes. CI also requires 90% coverage (`coverage report --skip-empty --fail-under=90`).
3. Look at it. Run `DB_ENGINE=sqlite python manage.py runserver`, then check sign-in and the three dashboards at 390x844 and 1280x800, in light and dark. Take screenshots.
4. `e2e/test_smoke.py` looks up `.bottom-nav`, `#mobile-nav-open` and `#mobile-nav-content`. Update those selectors if you move the elements.
5. Report what changed, include the screenshots, and say what you did not check.

## 10. Do not

- Add a role, a parent or student login, or a module without an explicit request. A module needs an entry in `apps/core/modules.py`, and a role needs `apps/core/roles.py` plus capabilities.
- Copy reference content: its names ("Hello Alex"), events ("Science Exhibition"), tasks ("Homework at Exited Summer Academy"), dates, numbers, tile labels or the lime and blue accents. The pattern comes from the reference; the content comes from SCHOOLCORD. In templates, use real data or an empty state.
- Add a bell, profile photos, a Calendar link or a Messages link before the feature exists.
- Commit straight to `main`. Railway deploys pushes to `main` to production. Work on a branch and open a pull request.

## 11. Known gaps (checked 2026-11-07)

- No notification model, no profile photos and no calendar page.
- The owner's "Upcoming" list needs a query on term due dates. It is not built yet.
- Not built from the student-portal reference: attendance, grades, exams, assignments, library, gallery, events, notices and chatbox. SCHOOLCORD is fees-first. Decide which of these to build before adding tiles for them.
- The bursar's mobile home is out of scope until the owner says otherwise.
- Plans & Billing has a nav entry but no app.
