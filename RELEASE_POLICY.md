# Release Policy (Packaging Thread)

## Scope
- This thread is dedicated to release packaging only.
- Source-code feature changes are handled in a separate thread.

## Fixed Output Root
- Use the current project folder's `release` directory: `<project>\release`.

## Folder Rule
- Each release must be created under a dated subfolder:
- `<project>\release\YYYY-MM-DD`
- If multiple releases are generated on the same date, use:
- `<project>\release\YYYY-MM-DD_v2`, `..._v3`
- `build_release.ps1` selects the next available suffix automatically and never overwrites an existing dated folder.
- After the newest same-day build passes validation, run `cleanup_project.ps1`.
- Keep only the highest same-day suffix locally; keep one release for dates without duplicate builds.

## Naming Rule
- Executable name stays unified:
- `VMD_Multiwfn_StyleGenerator.exe`

## Sync Rule With Source Thread
1. Source thread writes a `DONE` record to `THREAD_SYNC_LOG.md`.
2. Packaging thread reads latest `DONE` record before packaging.
3. Packaging thread writes `START` and `DONE` records to `THREAD_SYNC_LOG.md`.
4. Packaging thread does not modify business logic unless explicitly requested.

## Safety Rule
- Do not delete core source files during packaging.
- Do not write release outputs outside `<project>\release\...`.
- Do not commit release binaries to Git; publish binaries through GitHub Releases when needed.
- Do not mix packaging changes with source-code feature changes in the same commit or branch.
- Do not package local custom-style data or custom cover images.
- Archive same-day intermediate releases outside the project before removing them.

## GitHub Retention
- Keep current source, tests, resources, and documentation on the default branch.
- Publish the executable as a GitHub Release asset rather than committing it.
- Keep only the newest GitHub Release and its matching tag.
