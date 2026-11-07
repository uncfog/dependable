---
name: schoolcord-ui
description: Build or restyle SCHOOLCORD screens in this repo (Django templates, assets/app.css, static/js). Covers the sign-in page on phone and desktop, the mobile home dashboard, task-style lists and create sheets, and the bottom navigation. Use for any template, layout, styling, login, dashboard or mobile-navigation change, and whenever docs/design-references is mentioned.
---

# SCHOOLCORD UI

This repo is server-rendered Django with Tailwind CSS v4. There is no React, no bundler and no CDN. Keep it that way.

## 0. Read first

- `docs/TECHNICAL.md` section 4 (frontend) and the README section "Design tokens".
- The two references in `docs/design-references/`:
  - `task-management-reference.jpeg`: the sign-in screen, the Today screen and the Create New sheet. This is the source for the login look and the list and create patterns.
  - `student-portal-reference.jpeg`: the mobile home. This is the source for the mobile home layout.
- `assets/app.css`: the `@theme` block and the component classes. Use the tokens that are already there. Do not invent new ones.

## 1. Non-negotiables

- **Edit `templates/` and `assets/app.css`, never `static/css/app.css` by hand.** Rebuild with `npm install` then `npm run build:css`. Commit the source and the rebuilt `static/css/app.css` together. Railway does not run the Tailwind build.
- **After any colour change, run `npm run check:contrast`.** It must pass in both themes.
- **Every screen works in light and dark.** Theme is set by `data-theme` on `<html>`. Use token classes (`bg-surface`, `text-ink-900`, `bg-brand-700`, `bg-accent-*`), not raw hex. The only exception is the existing auth gradient.
- **No external requests.** Fonts are self-hosted. No CDN scripts, no third-party icon sets. Icons are inline SVG in the existing style: 24px viewBox, `stroke-width` 1.6 to 1.8, round caps.
- **Reuse before writing.** Check for these first: `.btn` with `.btn-primary`, `.btn-secondary` or `.btn-ghost`; `.card` and `.card-interactive`; `.icon-tile` with an `.accent-*` class; `.field-label`, `.field-input` and `.field-error`; `.bottom-nav`; `.numeric`; `.glass-dark`; and the partials in `templates/partials/`.
- **The server decides what shows.** Take links and visibility from `apps/core/navigation.py` (`nav_for`, `bottom_nav_for`) and capabilities from `apps/core/permissions.py`. Never hard-code a role check in a template. A module the school has not enabled must not appear.
- **Colour carries meaning.** Paid (green), partial (amber), unpaid (slate) and overdue (red) are fee states. Do not use them as decoration. Priority-style chips in the task reference map to fee states in SCHOOLCORD.

## 2. Brand

Keep SCHOOLCORD's navy brand: `brand-900` (#0f2547) for panels, `brand-600` (#245096) for primary actions, the `ink-*` scale for text, and Public Sans.

The task reference's blue and lime are a layout and mood reference, not a palette to copy. Lime on white fails contrast. If a lime accent is wanted, add it as a token in `@theme`, use dark text on it only, and run the contrast check. Do not rebrand without an explicit instruction.

## 3. Sign-in: one design, two widths

Phone and desktop show the **same** sign-in design. Only the arrangement changes.

- The visual panel (headline, subhead, the four-state chart card) stays in the DOM on phones. Below `lg` it becomes a top band with the same gradient and headline, and the form card sits under it. Today the phone view shows only the logo, with no headline band. That difference is the gap to close.
- Same copy, same fields, same primary button, same switch between sign-in and sign-up.
- Do this with responsive classes in `templates/registration/_auth_switch.html` and `_auth_base.html`, not with a second template. Keep both panels in the DOM, because the swap animation depends on it, and keep the `data-auth-*` hooks.
- Tests to keep green: `apps/core/tests_auth.py` and `apps/core/tests_responsive.py`. The sign-in page must not render the bottom bar.

## 4. Mobile home (staff dashboards)

Reference: `student-portal-reference.jpeg`. This applies to the phone view of `templates/core/dashboard_school.html`, `dashboard_bursar.html` and `dashboard_branch.html`. Desktop keeps its current grid.

Structure, top to bottom:

1. **Header band** in a brand gradient (`brand-900` to `brand-600`). It has a white greeting ("Hello, {first name}") with one line of context (school name and date), a bell with an unread count, and an avatar. The avatar is an initial circle until profile photos exist. Show the bell only when a notification source exists.
2. **Search field** that overlaps the bottom edge of the band: a white field with `shadow-panel`, pulled up with a negative margin. Until a global search exists, it searches students, using the same search the payment form uses.
3. **"Quick access"** heading with a "View all" link, then a grid of tiles: 2 columns below `sm`, 3 from `sm` up. Each tile has an `icon-tile` in an `accent-*` colour, a title, one line of description and a chevron. The tile background is the matching `accent-*-soft`, not white.
4. **"Upcoming"** list. Each row has a date chip (month and day, colour by type), a title, a time and place line, and a status chip. The source is real data: fee term due dates and admissions follow-ups. No placeholder events.
5. **Bottom bar** with four items: Home, Calendar, Messages, More. The items come from `BOTTOM_NAV`, and More opens the existing drawer. Pad the bar for the device safe area with `env(safe-area-inset-bottom)`.

A tile for a feature that is not built uses the existing "soon" treatment (`nav-link-disabled` with its toast). Never use a dead link or made-up numbers.

## 5. Task-management patterns (lists and create flows)

Reference: `task-management-reference.jpeg`, the Today screen and the Create New sheet.

- **Date strip:** seven day chips, with the selected day filled `brand-700` and white text. Use it on Calendar and on any due-date view.
- **Progress cards:** title, one line of meta, a progress bar, a status chip and a chevron. Use them for progress lists such as a class or a family's fee progress.
- **Create sheet:** a bottom sheet with a segmented control at the top. For SCHOOLCORD the segments are the records the signed-in role may create (for example Payment, Fee, Note), limited by capability. Reuse the existing filter-sheet approach (`data-filter-sheet`) where it fits.
- **Form inside the sheet:** Title, Description, due date (use `datepicker.js`), time, priority chips and a full-width primary button fixed to the bottom of the sheet.
- **Floating "+" in the bottom bar** opens Record a payment for bursar and owner. Use the existing `templates/core/_record_payment.html` partial. Show the button only where the capability exists.

## 6. What the tiles can point to today

Take each URL from `nav_for()`. Do not type URL names from memory.

- **Built now:** Students, Payments, Outstanding, Fee structures and terms, Classes and subjects, Staff, Families, Reports, Campuses, Admissions (module `admissions`, off by default), Parent messaging (module `messaging`, off by default).
- **Not built:** Attendance, GradeSheet, Exams, Assignments, Library, Gallery, Events, Notices, Chatbox (in-app chat), a calendar of events, and profile photos. Show these as "soon" or leave them out. Do not link them.

## 7. Desktop

Keep the sidebar and the current content width. Use the same tokens and card patterns. Dashboard tiles can use a 3-column grid. Do not carry the phone bottom bar to desktop. It is already `lg:hidden`.

## 8. Accessibility and behaviour

- Touch targets are at least 44px (`size-touch`). Focus rings are visible in both themes. Respect `prefers-reduced-motion` for any animation.
- Tables become labelled cards below `sm` automatically (`ui.js`). Use `data-no-cards` only for comparison grids.
- Use the existing `ui.js` hooks (toasts, loading states, sheets). Do not add a JavaScript framework.
- Fixed bars and sheets need safe-area padding.

## 9. Verify before you say it is done

1. `npm run build:css` and `npm run check:contrast`.
2. `DB_ENGINE=sqlite python manage.py test apps.core`, plus the app you touched. The full suite takes about ten minutes. CI also requires 90% coverage (`coverage report --fail-under=90`).
3. Look at it. Run `DB_ENGINE=sqlite python manage.py runserver`, then check sign-in and the dashboards at 390x844 and 1280x800, in light and dark. Take screenshots.
4. `e2e/test_smoke.py` looks up `.bottom-nav`, `#mobile-nav-open` and `#mobile-nav-content`. Update those selectors if you move the elements.
5. Report what changed, include the screenshots, and say what you did not check.

## 10. Do not

- Add a role, a parent or student login, or a module without an explicit request. A module needs an entry in `apps/core/modules.py`, and a role needs `apps/core/roles.py` plus capabilities.
- Use sample names or data from the references ("Hello Alex", "Science Exhibition") in templates. Use real data or an empty state.
- Commit straight to `main`. Railway deploys every push to `main` to production. Work on a branch and open a pull request.
