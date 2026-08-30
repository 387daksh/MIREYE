# Mireye — Design System (MASTER)

> **Authority:** The brand book (`mireye_brand_book_by_pomelli.pdf`) is the source of truth.
> Values below OVERRIDE anything produced by the generator. Do not reintroduce
> slate/green/indigo tokens.

---

## 01 · Color — closed palette

Five colors. There is no sixth. Any hue not on this list is a bug.

| Token | Hex | Role |
|---|---|---|
| Jet Black | `#000000` | Primary surface (dark mode is primary) |
| Pure White | `#FFFFFF` | Max-contrast text, inverted surfaces |
| Chalk White | `#F4F4F4` | Body text on black; light-mode surface |
| Denim Blue | `#71717A` | Neutral grey. Borders, secondary text, mono metadata |
| Mandarin Orange | `#FF6600` | **Accent only** |

### Orange law
Orange is a *signal*, not a *surface*.

- ✅ Live data values, active state, latency counter, focus ring, 1px underline, small dot/tick, hover accent on a single glyph.
- ❌ Button fills, section backgrounds, card fills, large text, gradients, glows, icon fills.
- Budget: **≤ 3 orange elements visible in any viewport.** If you can see four, delete one.

### Applied scale (CSS vars, `--mi-*` namespace)

```
--mi-black:      #000000
--mi-white:      #FFFFFF
--mi-chalk:      #F4F4F4
--mi-grey:       #71717A
--mi-orange:     #FF6600

--mi-bg:         #000000   /* page */
--mi-surface:    #08080A   /* bento cell — near-black, NOT a grey tint */
--mi-surface-2:  #0D0D10   /* raised cell */
--mi-fg:         #F4F4F4   /* body */
--mi-fg-strong:  #FFFFFF   /* headings, data values */
--mi-fg-muted:   #A1A1AA   /* ≥4.5:1 on #000 — use this, not #71717A, for text */
--mi-border:     rgb(113 113 122 / 0.22)   /* hairline */
--mi-border-2:   rgb(113 113 122 / 0.40)   /* hover / active hairline */
```

**Contrast trap:** `#71717A` on `#000000` is **4.35:1 — it FAILS the 4.5:1 gate.**
Use `#71717A` for *borders and rules only*. For muted text use `#A1A1AA` (8.2:1).
This is the single most common way to break the brand while thinking you're following it.

Verified ratios on `#000000`: `#FFFFFF` 21:1 · `#F4F4F4` 19.1:1 · `#A1A1AA` 8.2:1 ·
`#FF6600` 7.2:1 (orange is safe as text, it is just rationed by the orange law) ·
`#71717A` 4.35:1 ✗.

---

## 02 · Typography

**Geist** (Vercel) primary · **Geist Mono** for every machine-produced value.

Mono is mandatory for: coordinates, elevation/slope/flood values, units, source
tags (`USGS_3DEP`), timestamps, confidence, latency, endpoint paths, JSON,
terminal logs, agency names in the ticker.

Sans is for: headlines, prose, nav, button labels.

| Role | Font | Size | Tracking | Weight |
|---|---|---|---|---|
| Display | Geist | `clamp(44px, 6.5vw, 88px)` | `-0.045em` | 500 |
| H2 | Geist | `clamp(28px, 3.4vw, 44px)` | `-0.03em` | 500 |
| H3 | Geist | 20px | `-0.02em` | 500 |
| Body | Geist | 15px / 1.6 | `-0.005em` | 400 |
| Label | Geist Mono | 10px | `0.14em` uppercase | 500 |
| Data value | Geist Mono | 13–28px | `-0.01em` | 500 |
| Citation | Geist Mono | 9–10px | `0.08em` | 400 |

Never letterspace lowercase sans. Only uppercase mono gets tracking.

---

## 03 · Layout — bento grid

- 12-column, `max-width: 1280px`, gutter `clamp(16px, 4vw, 40px)`.
- Bento cells are **asymmetric** — never a uniform 3×2 of equal boxes.
- Cell separation is a **1px hairline** in `--mi-border`. Not a shadow, not a gap-only.
- Radius: `2px`. Effectively square. **No `rounded-2xl`, no pills.** The logo is hard-edged trapezoids; the UI matches.
- Negative space is generous: section padding `clamp(72px, 10vw, 140px)` vertical.

### Achromatic depth
Soft-UI depth is allowed but must be **pure black/white**, no hue:

```
--mi-shadow-raised: 0 1px 0 rgb(255 255 255 / 0.04) inset,
                    0 12px 32px rgb(0 0 0 / 0.6);
--mi-shadow-inset:  inset 0 1px 2px rgb(0 0 0 / 0.8);
```

Depth may never reduce text contrast below 4.5:1. Test the shadowed card, not the flat one.

---

## 04 · Motion

Mechanical, not bouncy. **No spring overshoot on layout.**

| Use | Duration | Easing |
|---|---|---|
| Hover / micro | 150ms | `cubic-bezier(0.2, 0, 0, 1)` |
| Reveal / expand | 260ms | `cubic-bezier(0.2, 0, 0, 1)` |
| Section scroll-in | 350ms | ease-out, 12px translate max |
| Count-up | 800ms | linear-ish, mono digits |

- Text reveals **character by character in mono**, ~18ms/char.
- Numbers **count up**, they don't fade in.
- Loading = **terminal log lines**, never a spinner:
  `resolving parcel…` → `querying USGS_3DEP…` → `confidence: medium`
- `prefers-reduced-motion: reduce` ⇒ no canvas, no typewriter (final string
  renders immediately), no marquee, no count-up. Content must be complete and
  legible with zero animation.

---

## 05 · Signature interactions

1. **Live map hero** — dark US map, fly-to on query, crosshair snap, fields stream in with source tags.
2. **Dithered terrain** — heightmap rendered as B/W ordered-dither dots on canvas, drifts on scroll.
3. **Citation pill** — every value carries a mono tag; hover expands to source + fetched + confidence bar.
4. **Draggable comparison** — ungrounded LLM (grey, vague) vs Mireye (sharp, cited).
5. **Sector cards** — click expands, types the real agent query, field chips light one at a time.
6. **API playground** — editable JSON in, streamed response out, orange latency counter.
7. **Proximity radar** — sweep from center, drive-time rings, markers pop as the sweep passes.
8. **Terminal loading states** — everywhere. No spinners exist in this product.

---

## 06 · Voice

Professional · Technical · Direct · Authoritative.
Values: Provenance, Accuracy, Reliability, Integrity.

Write like an API reference, not a pitch deck. State the capability, cite the
source, stop. No "unlock", "supercharge", "revolutionize", "seamless". No
exclamation marks. Numbers over adjectives.

---

## 07 · Banned

Purple/blue AI gradients · glassmorphism · glowing blobs · pastels · emoji as
icons · floating 3D shapes in the hero · rounded-everything · pill buttons ·
spinners · colored shadows · stock "team collaborating" photography ·
`bg-gradient-to-r from-purple-500 to-pink-500` in any form.

If it looks like every other AI startup, it is wrong.

---

## 08 · Quality gates

- `npm run build` clean.
- Three.js / canvas / window-touching components: `dynamic(..., { ssr: false })`.
- Every rAF and listener cancelled on unmount.
- Breakpoints verified: 375 / 768 / 1024 / 1440. Bento → single column on mobile.
- Contrast ≥ 4.5:1 on every surface **including shadowed cards**.
- Visible focus ring (orange, 2px, 2px offset) on every interactive element.
- Icons are SVG (Lucide/Phosphor). Never emoji.
- `cursor-pointer` on everything clickable.
- Lighthouse performance ≥ 85 mobile.
