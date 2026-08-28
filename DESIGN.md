---
name: CineSnip
description: Self-hosted Plex quote/clip tool — a dark control-room instrument panel for setup and generation.
colors:
  bg: "oklch(0.16 0.012 250)"
  surface: "oklch(0.205 0.013 250)"
  surface-2: "oklch(0.245 0.014 250)"
  sidebar: "oklch(0.135 0.011 250)"
  border: "oklch(0.33 0.015 250)"
  border-subtle: "oklch(0.27 0.013 250)"
  text: "oklch(0.93 0.006 250)"
  text-secondary: "oklch(0.68 0.012 250)"
  text-tertiary: "oklch(0.50 0.012 250)"
  phosphor-teal: "oklch(0.84 0.14 174)"
  phosphor-teal-strong: "oklch(0.72 0.16 174)"
  phosphor-teal-soft: "oklch(0.84 0.14 174 / 0.13)"
  phosphor-teal-ink: "oklch(0.16 0.02 174)"
  danger: "oklch(0.68 0.18 25)"
  danger-soft: "oklch(0.68 0.18 25 / 0.14)"
typography:
  display:
    fontFamily: "Space Grotesk, system-ui, sans-serif"
    fontSize: "22px"
    fontWeight: 600
    lineHeight: 1.2
    letterSpacing: "-0.01em"
  body:
    fontFamily: "Public Sans, system-ui, sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.6
    letterSpacing: "normal"
  label:
    fontFamily: "Public Sans, system-ui, sans-serif"
    fontSize: "12px"
    fontWeight: 600
    lineHeight: 1.4
    letterSpacing: "0.02em"
  mono:
    fontFamily: "IBM Plex Mono, ui-monospace, monospace"
    fontSize: "13px"
    fontWeight: 400
    lineHeight: 1.4
    letterSpacing: "normal"
  stat:
    fontFamily: "Space Grotesk, system-ui, sans-serif"
    fontSize: "26px"
    fontWeight: 700
    lineHeight: 1.1
    letterSpacing: "normal"
rounded:
  sm: "6px"
  md: "8px"
  lg: "10px"
  xl: "14px"
  pill: "999px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "14px"
  lg: "22px"
  xl: "40px"
components:
  button-primary:
    backgroundColor: "{colors.phosphor-teal}"
    textColor: "{colors.phosphor-teal-ink}"
    rounded: "{rounded.lg}"
    padding: "12px 20px"
  button-ghost:
    backgroundColor: "transparent"
    textColor: "{colors.text-tertiary}"
    padding: "8px 0"
  button-secondary:
    backgroundColor: "{colors.surface-2}"
    textColor: "{colors.text}"
    rounded: "{rounded.md}"
    padding: "8px 14px"
  card:
    backgroundColor: "{colors.surface}"
    rounded: "{rounded.xl}"
    padding: "40px 36px"
  input:
    backgroundColor: "{colors.surface-2}"
    textColor: "{colors.text}"
    rounded: "{rounded.md}"
    padding: "10px 12px"
  navitem-active:
    backgroundColor: "{colors.phosphor-teal-soft}"
    textColor: "{colors.phosphor-teal}"
    rounded: "{rounded.md}"
---

# Design System: CineSnip

## Overview

**Creative North Star: "The Control Room"**

CineSnip's web app is a precision instrument panel, not a marketing surface: a near-black chassis with a single glowing indicator color reserved for what's active, valid, or selected right now. Every other surface — cards, inputs, the sidebar — sits in a tight band of near-neutral dark tones, so the one accent (Phosphor Teal) reads unmistakably as "this is the thing that matters" whether that's a completed wizard step, a selected style preset, or the currently-playing clip preview. The two shells it runs — the wizard's centered single-card flow and the app-shell's persistent sidebar — are two panels of the same instrument, not two different products.

The mood is technical and precise, not glossy. Borders are thin and exact rather than soft; hover states are a firm lift-and-brighten, not a fade; motion is short and functional (150ms transitions, no easing flourishes beyond what a physical control needs). This deliberately steers away from two common self-hosted-tool traps: the generic blue/purple gradient SaaS-admin look, and glossy corporate marketing polish. CineSnip should read like a tool an enthusiast built for their own Plex server, not a startup's dashboard product.

**Key Characteristics:**
- Near-black chassis (bg/surface/surface-2/sidebar) with one reserved accent color, never a secondary or tertiary hue
- Thin, exact borders over soft shadows; the one heavy shadow in the system is reserved for cards/panels floating above the chassis
- Tactile, confident interactive states — buttons lift on hover, inputs get a focus ring, selected items get an accent border, never just a color swap alone
- Monospace used narrowly and meaningfully: timecodes, path mappings, PIN codes — anywhere the value is a precise machine-readable token, not prose

## Colors

A near-monochrome dark chassis broken by exactly one accent hue — the palette's entire expressive range lives in how that one teal is used, not in how many colors exist.

### Primary
- **Phosphor Teal** (`oklch(0.84 0.14 174)`): the system's only accent. Used for active/selected state (active nav item, active settings tab, checked style pill, completed wizard step), primary buttons, focus rings, links, and the one glowing dot on the "waiting for Plex" pill. Never used for anything at rest or unselected.
- **Phosphor Teal Strong** (`oklch(0.72 0.16 174)`): the hover/pressed state of Phosphor Teal — links and the "add row" button darken to this on hover.
- **Phosphor Teal Soft** (`oklch(0.84 0.14 174 / 0.13)`): translucent accent wash for selected-state backgrounds (active nav item, checked style pill, selected film/show chip, save-confirmation banner) — lets the accent mark "selected" without competing with Phosphor Teal itself for attention.
- **Phosphor Teal Ink** (`oklch(0.16 0.02 174)`): near-black text color used only on top of solid Phosphor Teal buttons/badges, for contrast.

### Neutral
- **Chassis Black** (`oklch(0.16 0.012 250)`, `--bg`): the outermost background.
- **Sidebar Black** (`oklch(0.135 0.011 250)`, `--sidebar`): one step darker than the chassis — the persistent nav column reads as recessed, not floating.
- **Panel Surface** (`oklch(0.205 0.013 250)`, `--surface`): cards and the wizard's panel.
- **Raised Surface** (`oklch(0.245 0.014 250)`, `--surface-2`): inputs, style pills, library-mapping rows — anything nested one level inside a card.
- **Border** (`oklch(0.33 0.015 250)`) / **Border Subtle** (`oklch(0.27 0.013 250)`): Border is for interactive-element edges (inputs, buttons); Border Subtle is for structural dividers between static rows/sections.
- **Text** (`oklch(0.93 0.006 250)`) / **Text Secondary** (`oklch(0.68 0.012 250)`) / **Text Tertiary** (`oklch(0.50 0.012 250)`): primary content, supporting copy/descriptions, and metadata/labels/timestamps respectively.

### Named Rules
**The One Signal Rule.** Phosphor Teal marks exactly one thing at a time per view: what's active, selected, valid, or complete. It is never used decoratively or for anything at rest — if nothing is selected/active/complete, nothing on screen is teal.

**The Danger Is Foreign Rule.** `oklch(0.68 0.18 25)` (red-orange) exists solely for errors and destructive actions (error banners, remove-row hover, failed checks). It is the only other saturated hue in the system and must never be reused for anything else, so its appearance is always unambiguous.

## Typography

**Display Font:** Space Grotesk (with system-ui, sans-serif fallback)
**Body Font:** Public Sans (with system-ui, sans-serif fallback)
**Label/Mono Font:** IBM Plex Mono (with ui-monospace, monospace fallback)

**Character:** A geometric, slightly technical display face (Space Grotesk) paired with a clean, highly-legible body face (Public Sans) — confident headings over quiet, readable body text. IBM Plex Mono appears only where a value is a precise, copyable token, reinforcing the instrument-panel feel without turning the whole UI into a terminal.

### Hierarchy
- **Display** (600, 19–22px, 1.2): card titles (`.card h1`), page headers (`.page-header h1`), the wordmark. Tight letter-spacing (-0.01em).
- **Title** (600, 14–16px): section/card sub-headers, sidebar logo text, clip titles, library names.
- **Body** (400, 13.5–14px, 1.6): descriptive paragraph text (`.desc`), match text, general prose.
- **Label** (600, 11–12px, 0.02em tracking): field labels, uppercase library-type tags, badges — small, deliberate, always paired with a functional control.
- **Mono** (400–500, 12.5–16px): timecodes, path-mapping prefixes, restore-row chips — anywhere the value must be read character-by-character.
- **Stat Display** (700, 26–34px, Display or Mono family): the small set of moments where one number or code needs to be read at a glance from across the room — the Plex PIN badge (34px mono) and the cached-title-count stat (26px display). Deliberately above the Display step; these aren't headings, they're the one thing on their card.

### Named Rules
**The Stat Display Exception.** Display's 19–22px ceiling is for headings. A card whose entire purpose is one glanceable number or code (a PIN, a live count) may go larger — up to 34px — but only when that number/code is the card's single subject, never as emphasis on body or label text.

**The Precision Token Rule.** Monospace is reserved for values a user might type, copy, or compare character-by-character (timecodes, paths, PINs). It is never used for prose or labels, even short ones — that's what Label weight is for.

## Layout

Two coexisting shells, deliberately different in shape because they serve different moments:

- **Wizard shell** (`.shell`/`page.html`): a single centered column, max-width 460px (620px for wide steps), generous vertical rhythm (56px top padding, 40px between sections), one step-nav dot-strip at the top. This is a linear, one-thing-at-a-time flow — width is deliberately constrained so there's never more than one decision on screen.
- **App shell** (`.app-shell`/`shell.html`): a persistent 220px sidebar plus a fluid main content column (max-width 1160px, 32px/40px padding), for the repeat-use `/generate` and `/settings` surfaces. `/generate` itself is a two-column grid (400px form column + fluid preview column) — the form stays a fixed, scannable width while the clip preview gets the remaining space.

**Responsive behavior**: below 860px, the app shell collapses to a single column — the 220px sidebar becomes an off-canvas drawer (slides in from `left: -240px` to `left: 0`, 200ms ease, with a dimming backdrop) behind a sticky mobile top bar, and the generate grid drops to one column with the preview card moving below the form. Below 480px, the wizard's step-nav drops its text labels (dots + shortened rules only) and paired form fields (`.field-row`) stack vertically instead of sitting side by side. Both breakpoints were set from measured real overflow, not guessed round numbers.

## Elevation & Depth

Flat-by-default with one deliberate exception: cards floating over the chassis get a single soft, wide shadow (`0 20px 60px -20px rgb(0 0 0 / 0.5)`) to read as "the one thing you're looking at," while every nested surface inside a card (inputs, pills, rows) stays flat and differentiates purely through the surface/surface-2/border tonal steps. The mobile sidebar drawer is the only other shadow user (`0 0 40px rgb(0 0 0 / 0.45)`), marking it as a temporary overlay rather than part of the base layout.

### Shadow Vocabulary
- **Card Float** (`box-shadow: 0 20px 60px -20px rgb(0 0 0 / 0.5)`): the wizard/settings card and the generate-grid cards. Use once per view, on the outermost content container only.
- **Drawer Overlay** (`box-shadow: 0 0 40px rgb(0 0 0 / 0.45)`): the off-canvas mobile sidebar only.
- **Result Dropdown** (`box-shadow: 0 12px 32px -8px rgb(0 0 0 / 0.5)`): the floating search-results/match-list dropdown under an input.

### Named Rules
**The Flat Interior Rule.** Nothing nested inside a card casts its own shadow. Depth inside a card comes only from the surface/surface-2 tonal step and a border — shadows are reserved for things floating above the chassis itself.

## Shapes

A tight, consistent radius scale that increases with a surface's size: 6px for small controls (selects, mapping-row pills), 8px for standard controls (inputs, buttons, badges, pills), 10px for primary buttons and media containers, and 14px for cards — the largest surface gets the most rounding, everything nested inside gets less. Borders are always 1px (1.5px only for emphasis: step-nav dots, checked style pills) and hairline-thin rather than heavy. Circular/pill shapes (`border-radius: 999px`) are reserved for status indicators — step-nav dots, the waiting pulse dot, badges, the PIN's copy button.

### Named Rules
**The Radius-Follows-Size Rule.** A surface's corner radius scales with its own footprint: small controls (6–8px) sit inside larger containers (10–14px) — never the reverse, and never a uniform radius across every level.

## Components

Buttons, cards, and inputs are tactile and confident: hover states lift and brighten rather than fading, focus states get a visible glow ring, and selection is always marked by both a border-color shift and a soft background wash together, never one alone.

### Buttons
- **Shape:** 10px radius (`.btn`), 8px for the compact `.btn-secondary`.
- **Primary:** solid Phosphor Teal background, Phosphor Teal Ink text, 700 weight, 12px/20px padding.
- **Hover / Focus:** `translateY(-1px)` lift plus `brightness(1.06)` — a firm, physical response, not a fade. Active state returns to `translateY(0)`.
- **Ghost:** no background, tertiary text color — used for secondary/cancel actions (`.btn-ghost`).
- **Secondary:** `.surface-2` background with a border, used for non-primary actions in the generate result footer (Post to channel, etc.).

### Chips / Pills
- **Style Pill:** `.surface-2` background, subtle border; checked state swaps to Phosphor Teal Soft background with a 1.5px Phosphor Teal border and 600-weight accent text — border-and-wash together, never a plain color swap.
- **Badge:** `.surface` background, tertiary text, subtle border, pill radius — used for metadata tags (library type, quality).
- **Selected Chip:** Phosphor Teal Soft background with a full-strength Phosphor Teal border, marking a confirmed film/show pick.

### Cards / Containers
- **Corner Style:** 14px radius, 620px wide variant for multi-column wizard steps.
- **Background:** Panel Surface, 1px Border Subtle.
- **Shadow Strategy:** Card Float shadow (see Elevation & Depth) — the only shadow inside the card's own contents is none.
- **Internal Padding:** 40px/36px (card default), tighter (14px/16px) for nested library/mapping blocks.

### Inputs / Fields
- **Style:** Raised Surface background, 1px Border, 8px radius, 16px font size (prevents iOS Safari's auto-zoom-on-focus).
- **Focus:** border shifts to Phosphor Teal plus a 3px Phosphor Teal Soft glow ring (`box-shadow: 0 0 0 3px var(--accent-soft)`) — never a bare outline.
- **Disabled:** 0.6 opacity, `cursor: not-allowed`.

### Navigation
- **Sidebar nav item:** 8px radius, secondary text color at rest; hover moves to Raised Surface background and full text color; active state gets Phosphor Teal Soft background, Phosphor Teal text, 600 weight — no icon-only treatment, label always visible.
- **Settings tabs:** underline style — 2px bottom border, transparent at rest, Phosphor Teal on active, tertiary text moving to full text on hover.
- **Mobile:** sidebar becomes a fixed off-canvas drawer (see Layout) behind a hamburger button in a sticky top bar; nav items keep identical styling once open.

### Clip Preview (signature component)
The generate flow's result card is CineSnip's one distinctive custom component: a 16:9 media box with a dark gradient background (visible before media loads), a translucent blurred badge pill overlaid top-left (format/status indicator with a Phosphor Teal dot), and a bottom gradient-scrim caption overlay for the burned-in quote text preview. This is the only place in the system where imagery, not chrome, is the primary visual weight — everything else stays deliberately quiet so this component can be the thing users actually look at.

## Do's and Don'ts

### Do:
- **Do** treat Phosphor Teal as a single reserved signal — active, selected, valid, complete — never decorative (The One Signal Rule).
- **Do** pair every selection/active state with both a border-color change and a background wash together.
- **Do** use monospace only for precise, literal tokens (timecodes, paths, PINs), never for prose (The Precision Token Rule).
- **Do** scale corner radius up with container size — small controls inside larger ones always get a smaller radius (The Radius-Follows-Size Rule).
- **Do** keep card interiors flat; reserve shadow for the outermost floating surface per view (The Flat Interior Rule).
- **Do** use firm, physical hover motion (lift + brighten) rather than opacity fades.

### Don't:
- **Don't** introduce a second or third accent hue — the palette's entire expressive range is one teal against near-neutral dark tones.
- **Don't** reuse the danger red-orange for anything other than errors/destructive actions (The Danger Is Foreign Rule).
- **Don't** add gradient-heavy, glossy, or corporate-SaaS styling — cards, buttons, and chrome stay flat and precise.
- **Don't** give a nested surface (input, pill, row) its own shadow — depth there comes from tonal steps only.
- **Don't** use a uniform border-radius across every component regardless of size.
