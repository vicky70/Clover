# TODOS

## Slice 2 (before a second student joins)

### Uptime ticks and hang detection
- **What:** a per-minute `uptime_tick` table (separate from `events`), the 5-min gap rule for outage windows, and an alert to the founder when the server hangs while still running.
- **Why:** the Slice 1 startup rule (eng review D1) only catches full restarts, not a stuck process that never decides windows.
- **Pros:** closes the hang gap before other students rely on Clover's alerts.
- **Cons:** about a day of timing code (human: ~1 day / CC: ~1 hr).
- **Context:** `docs/designs/clover-v1.md` Hosting and scheduler recovery; spec-review R3-1, R3-10; eng review D1.
- **Depends on / blocked by:** Slice 1 shipped and fired a real alert.

### Witness removal, leave-Clover and data deletion
- **What:** 72h witness removal and leave-Clover flows with start/end notices; immediate deletion from live data; nightly backups on a 7-day rotation; a deleted-account ID list kept outside backups and re-applied after any restore; deletion covers all personal data (student record, witness name/phone, device tokens, message log, outbox).
- **Why:** protects consent and privacy for students other than the founder.
- **Pros:** completes the consent lifecycle; closes spec-review R3-8 and R3-9.
- **Cons:** human: ~4 hrs / CC: ~30 min, plus 3 more WhatsApp templates to get approved.
- **Context:** `docs/designs/clover-v1.md` Witness messages, Accounts and data; eng review D2.
- **Depends on / blocked by:** Slice 2 Android app (removal UI).

## Before user #10 or the first paying user

### Signed verify results
- **What:** the server issues a nonce per window; the CLI signs result + nonce with a per-device key from pairing; the server rejects unsigned, mismatched or replayed results.
- **Why:** a local `clover verify` can be faked by anyone who reads the code.
- **Pros:** makes "skill proved" records credible to parents and colleges.
- **Cons:** human: ~1 day / CC: ~1 hr; a patched CLI can still lie about local state.
- **Context:** `docs/designs/clover-v1.md` Open Question 3; eng review finding A4.
- **Depends on / blocked by:** device pairing (Slice 2).
