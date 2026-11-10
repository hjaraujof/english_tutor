# Implementation checklist: status review of 2026-11-10

Source: the status review of 2026-11-10. The owner approved all three groups, in this sequence.

## Group 1: CI pin and PR queue

- [x] Pin Python 3.13 for CI with `.python-version` (#22; the run used CPython 3.13.18)
- [x] Merge the CI pin PR and remove its worktree
- [x] Squash-merge #21, #19, #11. Dependabot closed #15 itself: "httpcore2 is up-to-date now"
- [x] Close #14 as obsolete (#21 moves httpx2 to 2.16.0)
- [x] Re-run #17 (certifi) and #18 (filelock) on the pinned base, merge each only if CI is green (both merged)
- [x] Read the open Dependabot alerts again and record the count: 0 open, from 19

## Group 2: local runtime

- [skip] Run skill-disk-cleanup on `/dev/sda5`: the dry run showed that each target and `/nix/store` are on `/dev/nvme0n1p3` (11 GB free), so a clean frees nothing on `/dev/sda5` and the roots restore does not need it
- [ ] Restore `.nix-gc-roots/` with `nix-shell shell.nix --run true`
- [x] Move the tutor default port from 8765 (held by `agento11y`) to 8766, and make `start_backend.sh` read it from `config.toml`
- [x] Put `piper` on `PATH` for `start_backend.sh`: `piper-tts` joins the `phase2` extra
- [ ] Start `llama-server` and the backend, and run both smoke requests from the README

## Group 3: code items, each with a test

- [x] `/api/review` returns 503 when `llama-server` is down
- [x] Live route: the learner can interrupt the tutor (server test passes; no run with a real microphone and speakers)
- [x] Live route: trim history by tokens, not by turns (`/props` and `/tokenize` read from the pinned llama.cpp source, not called on a live server)
- [x] Live route: the session gets a grammar review with typed corrections; spoken corrections stay under `live_corrections`
- [x] `frontend/app.js`: the offline hint names `./start_backend.sh`
- [x] Adversarial review of #24, findings F1, F2 and F4: a reply waits for queued speech, a skipped reply is sent when the next utterance is noise or is cut off, and four tests cover those paths
- [skip] Adversarial review of #24, finding F3 (`live_active` cleared before the session review): the validator did not confirm harm, and the fix it names would refuse a restart during the review
