# Implementation checklist: status review of 2026-11-10

Source: the status review of 2026-11-10. The owner approved all three groups, in this sequence.

## Group 1: CI pin and PR queue

- [x] Pin Python 3.13 for CI with `.python-version` (#22; the run used CPython 3.13.18)
- [x] Merge the CI pin PR and remove its worktree
- [x] Squash-merge #21, #19, #11. Dependabot closed #15 itself: "httpcore2 is up-to-date now"
- [x] Close #14 as obsolete (#21 moves httpx2 to 2.16.0)
- [ ] Re-run #17 (certifi) and #18 (filelock) on the pinned base, merge each only if CI is green
- [ ] Read the open Dependabot alerts again and record the count

## Group 2: local runtime

- [skip] Run skill-disk-cleanup on `/dev/sda5`: the dry run showed that each target and `/nix/store` are on `/dev/nvme0n1p3` (11 GB free), so a clean frees nothing on `/dev/sda5` and the roots restore does not need it
- [ ] Restore `.nix-gc-roots/` with `nix-shell shell.nix --run true`
- [x] Move the tutor default port from 8765 (held by `agento11y`) to 8766, and make `start_backend.sh` read it from `config.toml`
- [ ] Put `piper` on `PATH` for `start_backend.sh`
- [ ] Start `llama-server` and the backend, and run both smoke requests from the README

## Group 3: code items, each with a test

- [x] `/api/review` returns 503 when `llama-server` is down
- [ ] Live route: the learner can interrupt the tutor
- [ ] Live route: trim history by tokens, not by turns
- [ ] Live route: corrections carry a type from the closed list, and the session gets a grammar review
- [x] `frontend/app.js`: the offline hint names `./start_backend.sh`
