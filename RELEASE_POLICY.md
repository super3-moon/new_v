# Release Policy (Packaging Thread)

## Scope
- This thread is dedicated to release packaging only.
- Source-code feature changes are handled in a separate thread.

## Fixed Output Root
- Use one fixed release root folder:
- `E:\test\release`

## Folder Rule
- Each release must be created under a dated subfolder:
- `E:\test\release\YYYY-MM-DD`
- If multiple releases are generated on the same date, use:
- `E:\test\release\YYYY-MM-DD_v2`, `..._v3`

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
- Do not write release outputs outside `E:\test\release\...`.
- Do not commit release binaries to Git; publish binaries through GitHub Releases when needed.
- Do not mix packaging changes with source-code feature changes in the same commit or branch.
