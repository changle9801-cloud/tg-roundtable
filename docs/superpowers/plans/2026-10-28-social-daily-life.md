# Social Daily Life Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans in this session. Steps use checkbox syntax for tracking.

**Goal:** Members casually discover relevant content, ask friends for help, and sustain bounded conversations.

**Architecture:** Extend the existing LifeStore with scoped discoveries and a bounded browsing action. Reuse sources, the journal, private threads and the room scheduler; no separate orchestration service.

**Tech Stack:** Python 3.11+, asyncio, pytest, existing model adapters and Telegram transport.

**Spec:** ../specs/2026-10-28-social-daily-life-design.md

## Global Constraints

- No new dependencies, live model calls or Telegram test messages.
- Preserve pre-existing edits and all historical chat/memory data.
- Keep sleep, per-member quotas, share limits and source visibility rules.
- Execute in the authorized existing workspace; do not stage or commit unrelated edits.

## Review Focus

- A discovery must not leak into another member's prompt before explicit sharing.
- A guessed or unselected link must not be attached to a public message.
- Toolless/private recipients and failed tool results must not fabricate knowledge.
- A sleepy, silent or failed participant and a new user message must stop automatic continuation.
- Legacy saved interests and public messages must load without migration loss.

## Task 1: Casual discoveries and bounded interests

**Files:** life.py, runtime.py, tests/test_life.py, tests/test_shared_sources.py.
**Interfaces:** LifeStore.remember_discovery(member, result); discoveries_of(member); LifeEngine.browse(member, peers); found accepts item=None.

- [ ] Write behavior tests for a browse action without interests, persistence/visibility, explicit source sharing, failed discovery and pausing/resuming after three explorations.
- [ ] Run the new tests and observe missing-behavior failures.
- [ ] Implement bounded scoped discoveries, browse action/prompt, publish source selection and runtime journal integration.
- [ ] Run life and source tests; expect all pass.

## Task 2: Bounded private help

**Files:** life.py, runtime.py, tests/test_life.py.
**Interfaces:** LifeEngine can_browse callback; private reply optional lookup field; thread messages optional sources.

- [ ] Test a toolless requester asking a capable peer, persistence of the peer's read scope, no third-party disclosure, no lookup without capability and at most one lookup per thread.
- [ ] Observe RED, implement using existing helper tools and private scope, then run life/source tests to GREEN.
- [ ] Supply public peer interests/capabilities to background decisions without including private memories.

## Task 3: Relevant and sustained group discussion

**Files:** moderator.py, room.py, runtime.py, tests/test_reply_chain.py.
**Interfaces:** start_proactive(..., discussion=False); bounded pair state within one round; interest-aware moderator weights.

- [ ] Test relevant first response, explicit mention precedence, two extra pair replies, PASS, sleep and interruption.
- [ ] Observe RED; implement pair continuation only for opted-in proactive discussions, respecting existing quotas.
- [ ] Run reply/parallel/room tests to GREEN.

## Task 4: Preferences, documentation and verification

**Files:** existing admin UI/settings integration as needed; README; tests for any behavior-changing settings integration.

- [ ] Apply the agreed interests through the app if available; otherwise prepare a bounded, backed-up apply command and state the activation limitation.
- [ ] Document casual browsing, helper lookup and discussion limits; update the existing admin daily-life view if needed for discoveries.
- [ ] Run the full pytest suite and inspect diff; use a fresh reviewer as required by executing-plans.
- [ ] Fix material findings with RED/GREEN tests and record final activation status.

## Progress

- Implementation now includes scoped casual discoveries, bounded private lookup, interest-aware selection (including shared source excerpts), and at most two pair continuation replies for daily-life/journal shares. Shared friend rules and sharing prompts allow different reactions and concrete detail. Private-thread context reminds only participants to honor relevant commitments.
- Existing configured interests were inspected and preserved. The active custom template still enforces short teasing replies. `scripts/apply_social_defaults.py` previews the replacement and applies only that field with an exact-byte backup; activation is pending because H:\tg-roundtable-data is outside the writable workspace and local admin HTTP is blocked by the sandbox.
- Verification: 183 synchronous tests passed. Async tests cannot start: Windows asyncio blocks in socket._fallback_socketpair / accept under sandbox socket restrictions (confirmed with faulthandler). The full suite is NOT verified. New async tests cover bounded replies, PASS, sleep, per-member quota, user interruption and failures. No live model calls or Telegram messages were sent.

- Baseline: existing uncommitted followup/prompt/room/routine/runtime edits retained; full baseline pytest started.
- Execution decision: user authorized immediate implementation; proceed in this workspace without another approval round. Git metadata is read-only, so keep changes uncommitted.
