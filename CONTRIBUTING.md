# Contributing to Leonidas AI

`main` is what every install auto-pulls, so only the owner (the user) merges to it. Everyone else has read access and changes through a pull request (PR).

## Send a change

1. Fork once: `gh repo fork ItzJsimp/leonidas-scribe-private --remote --remote-name fork` (inside your clone; `origin` stays the main repo so auto-update keeps working).
2. Branch: `git switch -c my-change`.
3. Make the change, run the tests for the files you touched (`python -m pytest test_<area>*.py -q`).
4. Push to your fork: `git push fork my-change`, then `gh pr create --repo ItzJsimp/leonidas-scribe-private --fill`.
5. the user reviews the diff, merges or answers. Merged changes reach every install on its next update check (about a minute, applied when idle).

## Rules

- Never put keys, tokens, real server or person names, or your own data in code, tests or docs. `secrets.json`, `config.json`, `history.jsonl` and `dictionary.json` are local only.
- One topic per PR. Say what it changes and why in two lines.
- Never push to `main` and never force-push.
