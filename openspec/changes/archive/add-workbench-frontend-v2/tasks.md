# add-workbench-frontend-v2 Tasks

- [x] Add project/chat/library API client types.
- [x] Add Dashboard route and Project Workspace route.
- [x] Add Evidence Board, Citation Map, Report Studio, and Chat Panel components.
- [x] Wire project paper search-add and delete actions.
- [x] Add empty/loading/error states.
- [x] Verify with `npm run build`.
- [x] Browser desktop/mobile checks.
- [x] Upgrade Report Studio with version selection, manual save, copy feedback, and evidence-anchor audit.

Verification recorded on 2026-07-31:

- `npm run build` -> passed.
- Browser desktop check at `http://127.0.0.1:5176/projects/3` -> Dashboard, Project Workspace, Evidence Board, Agent Chat SSE passed.
- Browser mobile check at 375px width -> Citation Map overflow fixed and rechecked, `overflowX=false`.

Additional verification recorded on 2026-07-31:

- `npm run build` -> passed.
- Browser desktop check at `http://127.0.0.1:5176/projects/16` -> Report Studio controls, version list, Evidence Audit, manual save, and source-marker coverage passed with `overflowX=false`.
- Browser mobile check at 360px width -> Report Studio, Evidence Audit, version list, and controls passed with `overflowX=false`.

Interaction review recorded on 2026-08-01:

- Replaced fake topbar text navigation with real routes for Projects and Research Task.
- Added `/research` route for the single-task research workflow.
- Added labels, retry/error recovery, and disabled-state feedback to Dashboard, Project Workspace, and research-task forms.
- Added tab `role`, `aria-selected`, and segmented-control `aria-pressed` states.
- Added inline confirmation before removing a paper from Evidence Board.
- Added unsaved-draft protection and cancel editing in Report Studio.
- Changed Agent Chat status labels to user-facing Chinese and made Enter send, Shift+Enter newline.
- Added a selectable graph node list and keyboard-accessible graph canvas/detail close path.
- Collapsed Run Inspector event details by default.
- Removed unused Vite/Vue template component and assets.
- `npm run build` -> passed.
- Browser desktop checks:
  - `/` -> `overflowX=false`, real nav links, project labels present, create button disabled while title is empty.
  - `/research` -> `overflowX=false`, form exists, no visible keyboard-shortcut instruction, button types correct.
  - `/projects/26` -> `overflowX=false`, workspace tabs expose selected state, Agent Chat textarea exists and Enter sends.
- Browser mobile checks at 360px:
  - `/` -> `overflowX=false`, project labels present, topnav hidden.
  - `/research` -> `overflowX=false`, form exists, submit disabled while empty.
  - `/projects/26` -> `overflowX=false`, workspace tabs and Agent Chat prompt chips remain visible.
