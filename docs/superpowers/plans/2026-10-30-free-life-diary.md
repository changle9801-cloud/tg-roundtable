# Free Life and Diary Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans inline, followed by one fresh whole-change review. Preserve the existing uncommitted changes.

**Goal:** Members have real leisure experiences, keep private diary fragments, and recall them when joining a conversation.

**Architecture:** A small DiaryStore holds subjective entries linked to existing receipts. LifeEngine writes entries during existing reflection steps; Runtime supplies only a member's own recall and a boolean relevance signal to ChatRoom. The existing authenticated daily-life page shows filtered entries.

**Tech Stack:** Existing Python/asyncio/pytest and vanilla JavaScript; no new dependencies.

**Spec:** docs/superpowers/specs/2026-10-30-free-life-diary-design.md

## Global Constraints

- Preserve current uncommitted edits, character settings and private histories.
- No network or Telegram messages in automated tests.
- Written records must correspond to actual receipts; subjective feelings remain subjective.
- No automatic publication of diaries; no private content passed to another member.
- Runtime configuration changes use a previewable script and byte-exact backup.

## Review Focus

- A failed read or fabricated model source must not become a diary experience.
- A member's private memory may influence their own decision, never another member's context.
- Repeated receipts must not duplicate entries; mixed emotions must preserve one source record.
- No-goal browsing and negative impressions must survive verification and persistence.
- Existing settings and user edits must survive activation.

## Task 1: Persist and recall private diary entries

Files: new `src/roundtable/diary.py`, `tests/test_diary.py`; shared feeling helper in `game_memories.py`.

Interface: `DiaryStore.record(member, ref, result, kind='browse', at=None)`, `of(member)`, `relevant(member, query)`, `recall(member, query)`, `view()`; entries store source extent, feelings and keywords. Only recorded results reach the store.

- [x] Add failing behavioral tests for persistence/isolation, negative/mixed feelings, deduplication and matching.
- [x] Implement storage and bounded recall; reuse the game's decay constants.
- [x] Verify diary tests with an explicit workspace temporary directory.

## Task 2: Free activities and truthful diary capture

Files: `life.py`, `life_work.py`, `runtime.py`, video reader integration; integration tests in `test_diary.py` and existing personal-work tests.

Interface: `LifeStore.diary` owns entries; existing activity result gains optional `diary`, `feelings`, `topics`. Runtime uses existing verified sources and artifacts, never model-supplied paths.

- [x] Add a failing no-goal browsing test with a boring reading and actual receipt; include blocked-read case.
- [x] Remove the initial forced-goal branch; ask for optional feelings during existing reflection calls.
- [x] Record reading/work impressions, project existing game memories, and route video receipts through actual video content.
- [ ] Complete asynchronous personal-work, social-life and game-memory tests (blocked by the environment's socketpair timeout).

## Task 3: Recall in chat and diary viewing

Files: `room.py`, `urge.py`, `runtime.py`, `web/static/app.js`, `prompts.py`.

Interface: optional room callback `has_experience(member, text) -> bool`; Runtime supplies the owner's diary and game-memory relevance. API adds diary entries to the existing authenticated life response.

- [x] Add a failing integration check for own-memory recall and an increased participation score.
- [x] Wire contextual recall and the participation reason while preserving queued wishes and turn bounds.
- [x] Add member/day/feeling controls to the life page; preserve selection on refresh.
- [x] Check JavaScript syntax and include synchronous chat/web checks in the suite.
- [ ] Complete browser visual verification and asynchronous chat/web tests.

## Task 4: Activation and whole-change validation

Files: new `scripts/apply_free_life.py`, `tests/test_free_life_settings.py`, user-facing documentation.

- [x] Test preview/apply/backup/idempotence using a temporary settings file.
- [x] Implement the agreed free-time configuration while preserving all unrelated settings.
- [x] Attempt the full suite, run the available synchronous subset and inspect the final diff.
- [x] Get one fresh code review and resolve material findings.
- [ ] Apply settings and restart production if permitted.

## Execution notes

- Working in the supplied checkout keeps the existing in-progress chat changes intact. Git metadata is read-only in this environment; no commit/worktree operation is planned.
- Baseline default pytest temporary directory was inaccessible. Use an explicit directory under `.pytest_cache` for all subsequent test runs.

## Progress

- Diary storage/retrieval, negative feelings, no-goal activity flow, chat relevance, diary UI, and activation script implemented.
- RED observed: absent diary storage, missing relevance effect, missing activation script, missing video receipt parser. First three synchronous diary checks passed after implementation; full synchronous subset: 252 passed, one pre-existing synchronous test that invokes asyncio blocked by loopback socketpair timeout.
- Environment: socket.socketpair() itself times out before async test bodies; stopped the hung test processes. No transport or security bypass applied.
- Current changes retain all four original uncommitted files and their edits. Activation still pending; production data lives outside the writable roots.
- Fresh review identified four boundaries: source-free forged artifacts, negative reading impressions lost during work, receipt deduplication, and stale copied game memories. Reproduced as five failing cases (one parametrized), then fixed. Game memories now remain in their original store and are projected into the diary view with player/companion provenance.
- Final focused run: 12 passed. Final synchronous run excluding the one synchronous wrapper that requires an event loop: 258 passed, 232 deselected. Python compilation, JavaScript syntax and `git diff --check` passed.
- Live video/model/Telegram integration and browser visual verification remain unverified. No production messages were sent. Operator instructions are in `docs/free-life-diary.md`.
- Production activation was rejected before execution by the session's approval policy: `H:\tg-roundtable-data` is outside the writable roots and sandbox escalation is disabled. The running service was not stopped and production settings remain unchanged. The activation command and exact-backup script are ready for execution in a session that can write that directory.
