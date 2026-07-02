# Fix WebUI selection bar jitter

## Goal

Fix the WebUI account-management bulk selection bar so it appears in its final position immediately when account checkboxes are selected, and audit the rest of `webui.html` for the same class of first-render positional shift.

## User Value

Selecting accounts should feel stable and polished. Elements must not briefly render offset and then snap/animate into place unless that movement is intentionally requested.

## Confirmed Facts

- The reported issue is on the account management page.
- The affected element is the bulk action bar above the account list showing `已选择 <n> 个账号`.
- `webui.html` currently renders that bar from `accountsPageHTML()` as `bulkBar` with inline `animation:pop .14s ease`.
- `webui.html` also uses `animation:pop .16s ease` for modal content and `animation:fadein .12s ease` for dropdown/backdrop elements.
- The likely root cause is entrance animation on an element whose layout should remain fixed when it appears.

## Requirements

- R1: Remove the initial-position shift/jitter from the account bulk selection bar.
- R2: Keep the bar visually consistent with the existing UI once displayed.
- R3: Audit `webui.html` for other first-render positional entrance animations or layout shifts that can cause elements to appear in the wrong place and then settle.
- R4: Do not add a frontend framework, build step, dependency, or broad redesign.
- R5: Preserve existing account selection, refresh, export, delete, and clear-selection behavior.

## Acceptance Criteria

- [ ] Selecting an account checkbox makes the `已选择 <n> 个账号` bar appear without visible offset/snap-back.
- [ ] Changing selected account count does not move the bar except for normal layout changes caused by content width/wrapping.
- [ ] Other WebUI elements with entrance animations are reviewed; any same-class unintended positional shifts are removed or documented as intentionally out of scope.
- [ ] `python stt.py selfcheck` passes.
- [ ] Local WebUI loads successfully and `/api/state` returns HTTP 200.

## Out of Scope

- Redesigning the WebUI.
- Replacing inline styles with a full CSS architecture.
- Adding animation libraries or new dependencies.
