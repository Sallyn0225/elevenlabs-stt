# Quality Guidelines

> Code quality standards for frontend development.

---

## Overview

<!--
Document your project's quality standards here.

Questions to answer:
- What patterns are forbidden?
- What linting rules do you enforce?
- What are your testing requirements?
- What code review standards apply?
-->

(To be filled by the team)

---

## Forbidden Patterns

<!-- Patterns that should never be used and why -->

### Entrance animations that change final position

Do not reuse a keyframe that animates `transform` on elements whose final layout
position is owned by flex/grid/absolute/fixed positioning unless the keyframe's
`to` transform exactly matches the element's normal final transform.

Wrong:

```css
@keyframes pop{from{transform:translateX(-50%) translateY(6px)}to{transform:translateX(-50%)}}
/* Used on a normal flex child: it renders offset, then snaps when animation ends. */
```

Correct:

```css
@keyframes pop{from{opacity:0}to{opacity:1}}
/* If motion is needed, create a keyframe scoped to that element's real final transform. */
```

---

## Required Patterns

<!-- Patterns that must always be used -->

- First-render UI affordances such as selection bars, dropdowns, modals, toasts,
  and floating buttons must appear at their final position immediately. Fade-only
  entrance is the safe default.

---

## Testing Requirements

<!-- What level of testing is expected -->

(To be filled by the team)

---

## Code Review Checklist

<!-- What reviewers should check -->

(To be filled by the team)
