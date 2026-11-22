# Agent instructions (MoodFlow)

Read by every coding agent working in this repo. `CLAUDE.md` imports this file.

## Commit as you go

- Commit each finished, working change right away; don't wait to be asked and don't
  batch unrelated work into one commit. Run the tests first (`just test`).
- Several agents may share this working tree at once. Stage only the files *you*
  changed, by name (never `git add -A` / `git add .`), and check `git diff --cached`
  before committing. Never stash, reset, rebase or switch branches.
- Write descriptive messages: a short summary line, then what changed and why.
- Pushing still needs the user's explicit go-ahead.
- Keep `README.md` in sync: update the sections a change affects (intro, "How it works" rows,
  API cache notes, known limits) in the same commit as the code.
- Never commit secrets: `.env` is git-ignored and must stay that way. Leave
  `.vscode/settings.json` (local `window.title`) out of commits.

## Useful commands

- `just dev` runs backend and frontend with auto-reload; `just --list` shows the rest.
- `just test` runs the offline test suite (pytest).
- `npm --prefix frontend run build` type-checks and builds the frontend.
