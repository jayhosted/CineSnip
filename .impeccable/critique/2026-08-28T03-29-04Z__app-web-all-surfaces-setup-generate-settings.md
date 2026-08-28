---
target: "app/web (all surfaces: /setup, /generate, /settings)"
total_score: 20
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 3
timestamp: 2026-08-28T03-29-04Z
slug: app-web-all-surfaces-setup-generate-settings
---
**Method: dual-agent (A: a1e9a8fa60f980486 · B: afb4470b5232423b3)**

Both assessments hit real infrastructure gaps worth naming upfront rather than papering over: Assessment A had no browser tools this run and reviewed all three surfaces from source + full CSS trace against DESIGN.md. Assessment B's browser tool was network-isolated from the live container (curl from this shell succeeded at 200 while the controlled Chrome instance couldn't reach 127.0.0.1:1919 at all), so it ran the CLI detector cleanly but fell back to static class/CSS cross-referencing instead of live screenshots. /setup is genuinely unreachable right now (already-completed installs 404 the wizard route), so both assessments reviewed it from source only.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 2 | Wizard has spinners; /generate's "Generate clip" button and match-row picks have none, despite renders that can silently take minutes |
| 2 | Match System / Real World | 1 | Settings' "Edit ___" copy promises a scoped change; the code actually re-runs the full wizard tail (Plex re-pairing, Libraries, Validate) |
| 3 | User Control and Freedom | 2 | Wizard has a real Back; but entering the wizard from Settings has no path back to Settings, only the wizard's own internal Back |
| 4 | Consistency and Standards | 2 | Token system is followed faithfully; but the wizard shell and app-shell don't actually bridge as "two panels of one instrument" |
| 5 | Error Prevention | 2 | Server-side validation is solid; but "Edit Plex connection" unconditionally forces a fresh PIN re-pairing even when a valid token already exists |
| 6 | Recognition Rather Than Recall | 2 | Search results show title/year/library inline; but timecode/season/episode fields use placeholder-only text with no persistent label |
| 7 | Flexibility and Efficiency | 1 | No shortcuts, no jump-to-known-title; the style-preset picker is fully mouse-only on a surface framed as repeat-use |
| 8 | Aesthetic and Minimalist Design | 3 | Genuinely restrained — one accent, flat interiors, matches DESIGN.md's stated intent |
| 9 | Error Recovery | 3 | Error copy is specific and actionable throughout, not generic |
| 10 | Help and Documentation | 2 | Strong in the wizard; /settings' Render tab has zero explanatory copy for 10 numeric fields |
| **Total** | | **20/40** | **Acceptable — significant improvements needed** |

## Design Specificity Verdict

**LLM assessment**: Reads as authored for CineSnip, not a generic admin skin — the clip-preview component, the Plex-PIN-specific waiting UI, and the path-mapping row editor are all bespoke. /settings is the weak link: five of its six tabs are indistinguishable from any config-CRUD screen.

**Deterministic scan**: detect.mjs ran in degraded regex mode against app/web/templates — exit 2, 3 findings: two overused-font warnings on Space Grotesk (page.html:9, shell.html:9) and one design-system-font-size advisory (26px in panel_settings_cache.html:7, off the documented type ramp). Static cross-referencing also turned up a real bug: .kind-toggle (panel_generate_left.html:16, the Film/TV Show radio toggle) has zero matching CSS rule anywhere in style.css.

**Visual overlays**: not available this run — no live browser evidence was obtainable.

## Overall Impression

The token system and the one genuinely distinctive component (clip preview) prove the design language is real and followed with discipline. But the product has two structural seams that undercut it: the wizard and the post-setup app-shell were built as separate systems stitched together with one-way doors (Settings → wizard, no path back), and /generate is missing feedback exactly where the product's own documented pain point lives (cold-cache renders taking minutes).

## What's Working

1. **Plex PIN-auth resilience** — expiry auto-refresh, secure-context-aware clipboard with an execCommand fallback, and a bounded call timeout documenting a real hung-thread-starvation incident.
2. **The Clip Preview component** — the one place DESIGN.md licenses imagery-first weight, and it earns it: blurred badge overlay, gradient-scrim caption with text-shadow.
3. **Error copy** — nearly every failure path names the actual cause and next step instead of a generic failure message.

## Priority Issues

- **[P1] "Edit ___" links from Settings contradict their own copy and force redundant re-auth.** GET /wizard/plex always fetches a fresh PIN even when a valid plex_account_token already exists, forcing a full plex.tv re-pairing just to fix a URL typo, then marches through Libraries + Validate + Finish regardless.
  Why it matters: a returning admin fixing a small config detail gets the full first-run ordeal again.
  Fix: branch plex_step on an already-valid token and skip straight to URL entry; give Settings' edit links a scoped-edit mode that returns to /settings on save.
  Suggested command: /impeccable harden

- **[P1] Style-preset picker is keyboard-unreachable.** panel_generate_left.html:92 hides the radio input with style="display:none", removing it from the tab order entirely.
  Why it matters: a keyboard-only or screen-reader user cannot select five of six style presets at all.
  Fix: swap to a visually-hidden technique (clip-path/absolute-position, not display:none) so the input stays focusable.
  Suggested command: /impeccable audit

- **[P1] .kind-toggle (Film/TV Show radio toggle) has no CSS at all.** Confirmed by grep against style.css — zero matching rules — while every sibling control on the same screen is custom-styled.
  Why it matters: sits directly above the fully-styled search field and style-pill grid; bare browser-default radios would be the single most visible inconsistency on the app's most-used screen.
  Fix: add a .kind-toggle rule using the existing pill/chip vocabulary.
  Suggested command: /impeccable layout

- **[P2] No loading feedback on the primary render action.** Neither "Generate clip" nor match-row picks carry the .btn-spinner markup the wizard already uses, on a flow that can silently take minutes on a cold subtitle cache.
  Why it matters: reads as a dead button exactly at the product's own documented worst-case latency.
  Fix: add .btn-spinner to both, and surface a /subtitle-status-driven warning before submit.
  Suggested command: /impeccable harden

- **[P2] settings_rerun_card.html uses the danger hue at rest, not just on error/hover.** Its border and button are permanently danger-styled for a non-destructive action, breaking DESIGN.md's own Danger Is Foreign Rule.
  Why it matters: trains users to read a persistent "something's wrong" signal for a routine action.
  Fix: restyle to a neutral/secondary card; reserve danger styling for confirmation-of-destructive-intent only.
  Suggested command: /impeccable colorize

## Persona Red Flags

**Alex (Power User)**: Flexibility scored 1/4 largely because of exactly Alex's use case — no keyboard shortcuts, no jump-to-known-title, and the fully mouse-only style picker blocks a whole primary decision from the keyboard on a surface framed as repeat/daily use.

**Sam (Accessibility-Dependent)**: The style-pill keyboard trap is the headline failure. Secondary: timecode/season/episode fields rely on placeholder-only text with no label for, so a screen reader announces nothing meaningful once the field has content. --text-tertiary on --bg is used for footnote/meta/badge text at 11-14px and is worth a real contrast check against WCAG AA's 4.5:1.

**Jordan (First-Timer)**: The /setup wizard itself is handled well — linear steps, explicit Public Bot warning, PIN flow with copy-to-clipboard and refresh toast. The one gap: if Jordan later returns through Settings to fix a typo, they get unexpectedly walked through full Plex re-pairing.

## Minor Observations

- Both overused-font detector findings (Space Grotesk) are false positives against this project's own design system — DESIGN.md explicitly documents it as the deliberate display face.
- The 26px design-system-font-size finding in panel_settings_cache.html:7 is real. Note .pin-badge-text (34px, style.css:135) is off-ramp too but invisible to the detector since it only scanned templates.
- .copy-label (panel_generate_result.html:33) is also CSS-undefined but harmless — inherits its parent button's styling.
- An MP4/WebM clip result silently has no Copy button with zero UI explanation for why.
- settings_tabs.html's active-tab underline and .navitem.active's soft-background both correctly apply Phosphor Teal per the One Signal Rule.

## Questions to Consider

1. CLAUDE.md decision #6 and PRODUCT.md's capabilities list both describe /generate including a "Post to channel" hookup back to Discord, but the result template only has Copy/Download — was this descoped and the docs never updated, or is it an actual build gap?
2. DESIGN.md frames the wizard shell and app-shell as "two panels of the same instrument" — but the only bridge between them (Settings' edit links) is a one-way door with no return path. Was that deliberate, or did the sidebar redesign simply never get reconciled with the pre-existing wizard routes?
3. Given CLAUDE.md documents _slow_subtitle_warning specifically because a cold render can silently take minutes on Discord — why hasn't that warning traveled to /generate, despite it being built as "a thin second client" of the same worker API?
