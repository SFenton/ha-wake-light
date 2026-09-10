# Wake Light Home Assistant Integration

`custom_components/wake_light/` is a config-entry custom
integration that owns wake-alarm scheduling while delegating all leased light
turn-on commands to Presence Based Lighting (PBL). Each config entry represents
one room profile. The React dashboard reads one summary sensor and sends every
write through `wake_light.command`.

Install this custom integration through HACS, then restart Home Assistant before adding or reloading its config entries.

## Prerequisites

1. Install the PBL build that provides the response services
   `presence_based_lighting.acquire_control`,
   `presence_based_lighting.dispatch_control`, and
   `presence_based_lighting.release_control`, including the
   `cancelled` / `external_targets_off` release semantic. That PBL build must be
   present before this Wake build is enabled; an older release schema is not
   compatible with the state-only external-OFF handoff.
2. In the PBL controlled-light configuration for the exact room root:
   - set **External Control Lease Mode** to **Enforce**;
   - confirm the root resolves to the intended light group;
   - add the room Adaptive Lighting switch as a PBL control-lease blocker;
   - confirm all intended leaf targets are current members of the root.
3. Identify the required vacation entity. Wake Light permits execution only
   while its state is exactly `off`.
4. Patch or retire any existing brightness-255 lifecycle before acknowledging
   it in Wake Light. Until then, leave
   **Legacy brightness-255 lifecycle has been retired or patched** off. The
   sensor reports `legacy_brightness_255_lifecycle_unresolved`, and preflight
   cannot acquire a lease.

For each room, configure only the intended wake-light leaves, the matching PBL switch, the required vacation entity, and every competing lighting controller as a fail-closed blocker. Keep legacy lifecycle acknowledgement disabled until any external full-brightness automation has been reviewed for wake-lease compatibility.

## Scheduling policy

- Weekly alarms are resolved as wall times in `hass.config.time_zone`.
- One-time alarms use their configured local date and time once.
- A nonexistent DST gap time moves forward to the first valid minute.
- An ambiguous DST fold uses the first physical instant and never runs twice.
- The default missed-alarm catch-up ceiling is 10 minutes after the wake
  deadline.
- Stale one-time records are retained but disabled and reported as missed.
- SleepyPod source data is cached. `unknown`, `unavailable`, or a missing side
  retains the last known alarms rather than treating them as deletions.
- SleepyPod keeps the compatible `idle` / `ringing` / `snoozed` alarm state
  and supplies `terminal_reason`. Natural `expired` silence does not dismiss
  the wake-light hold. Explicit `stopped` carries the source occurrence ID;
  Wake Light attributes it to the captured schedule row and execution window
  before recording the consumed ID. Same-ID replay suppression alone is not
  occurrence attribution.
- A retained source alarm does not start while that source is unavailable; it
  remains visible and retries only within the normal catch-up window.
- A native one-time alarm is deleted only after its exact configured occurrence
  reaches the wake deadline and receives the final light dispatch. Clean hold
  completion and a user Stop after that deadline both perform the cleanup.
  Pre-wake cancellation or failure retains the alarm in a disabled state so the
  missed wake remains inspectable. Rescheduling the same alarm ID to another
  date or time protects the replacement through occurrence-identity matching.
- SleepyPod alarm weekdays are **execution days**, exactly as its database and
  cron scheduler use them. Power-on/off rows never change alarm weekdays. The
  React bed editor selects this provider contract explicitly; its older
  FreeSleep provider retains its separate bedtime-day conversion.
- The SleepyPod v2 schedule mirror publishes `provider: sleepypod`,
  `alarm_day_semantics: execution`, and each alarm's database `id`. Normalized
  weekly records keep a per-day `source_schedule_ids` mapping, including
  separate slots for same-time duplicates. Missing or duplicate identities
  fail closed for new source execution rather than enabling side-wide guessing.
- Source-bound alarms are read-only. Wake Light never edits a SleepyPod alarm
  that it did not create.
- A native dated one-time alarm may include `bed_sides` (`left` and/or
  `right`). Home Assistant then creates a temporary matching SleepyPod alarm
  only when that side/day/time has no existing alarm, records the exact
  provider identity, and removes only that newly created alarm ten minutes
  after the dated wake time. Ambiguous identity or unavailable schedule data
  fails closed and retries without deleting unrelated alarms.

## Execution and ownership

Preflight requires all of the following:

- vacation is exactly `off`;
- the PBL switch is `on`;
- the root and every explicit target are present and not
  `unknown`/`unavailable`;
- every configured blocker is exactly `off`;
- the schedule is valid;
- the external brightness-255 lifecycle has been acknowledged safe.

Occupancy is status-only and cannot override a failed safety gate.

One room run owns one occurrence set and one PBL lease. The initial acquire TTL
is bounded to cover the longest loadable legacy 60-minute ramp, the fixed
five-minute hold, the source-snooze budget, and recovery. New configuration
writes accept only 0, 5, 10, 15, or 30 ramp minutes. Lease updates never extend
the initial absolute expiry.

Overlapping execution intervals form one connected room wake episode. Wake
Light keeps the physical brightness monotonic across that episode; it does not
guess that two alarms from the same source side are duplicates or backup
alarms, and it never automatically dims or turns the room off between them.

Wake Light:

- starts at 1% when the observed root is off;
- never commands below the highest observed explicit wake-target brightness;
- computes a stepped linear ramp every 30 seconds;
- uses the maximum desired brightness across overlapping occurrences;
- sends every leased turn-on through `dispatch_control` with explicit
  `brightness_pct` and `transition`;
- defers an in-flight dispatch revocation to the correlated PBL revoke event,
  preserving the user's cancellation cause and connected-episode relight
  fence; if no event arrives, retry cadence is bounded and the existing
  dispatch failure path resumes after one minute;
- ignores remembered/target brightness echoes during each light command's
  settle window, then samples the explicit wake leaves before the next step;
- does not set color temperature in version 1;
- sends an explicit 100% dispatch at the wake deadline;
- holds 100% for the configured post-wake duration, then releases with
  `completed`/`hold_complete`.

The final dispatch is issued at the deadline, but Home Assistant, the network,
and physical devices cannot guarantee zero-latency convergence to 100%.
Dispatch failures remain visible as stable failure reason codes.

A foreign leaf-off removes that leaf from the current lease target set and it
is never re-added during that occurrence. If foreign OFF feedback removes the
last leased wake leaf, it ends the connected user episode and preserves its
no-relight fence even when an excluded light keeps the aggregate root on.
Wake records observed ON-to-OFF transitions against the current lease before
queuing asynchronous handlers. Reconciliation and both ramp/snooze dispatch
paths process those observations before issuing another owner command. A
complete observed batch ends the episode; a single observed leaf is released.
Initial OFF states alone are not user intent, and a queued observation from a
retired episode cannot release a genuinely disconnected later episode.

The exact-generation `cancelled` / `external_targets_off` release asks PBL to
apply its own normal unknown-source external policy before persistence yields.
An already admitted asleep/manual baseline remains unchanged. Without that
baseline, the normal unknown-source pause policy creates qualified external
suppression, not an administrative pause: ordinary presence changes stay
suppressed, while a disconnected future wake can still acquire a lease.
Unknown/unavailable target loss remains a distinct fail-closed interruption.
A root/group off, explicit user brightness control, the `end_episode` command,
or a matching PBL lease-revoked event ends the connected episode immediately.
It preserves the asleep PBL baseline, turns off only confirmed wake-owned
targets when the dashboard initiated the stop, and persists a bounded
auto-relight fence through the connected episode end. No restart or newly due
overlapping occurrence may relight during that fence. Vacation becoming
anything other than `off` releases
non-clean, directly turns off only targets previously confirmed wake-owned
using a zero-transition safe path, and preserves PBL's fail-dark baseline.

The cancelled component is expanded from newly observed calendar intervals
before expiring its live fence. Its retained cancellation interval also prevents
a delayed source refresh or restart after the original cutoff from reviving a
still-connected occurrence. Disconnected later alarms remain independent.
One-shot cleanup compares the currently configured occurrence identity: an old
completion cannot alter a same-ID alarm rescheduled to another date/time.
Fired native one-shots are deleted after final dispatch and post-wake cleanup;
pre-wake terminal one-shots remain present but disabled.

After an HA restart, persisted active state attempts to reacquire the same
recovering lease ID with the remaining original TTL. It resumes the current
max/monotonic curve only within catch-up; otherwise it records a missed
recovery and releases safely.

### Supported lifetime and interruption policy

The acquire-only budget is 105 minutes. Configuration commands reject connected
execution windows longer than that budget; passive unsupported source
configuration is exposed as a current blocker. A running lease never silently
extends its absolute expiry, and newly arriving occurrences/snoozes cannot
exceed its remaining ownership window.

Target or ownership interruptions terminate that occurrence conservatively.
They do not silently retry with a new lease ID. PBL retires released/revoked
lease IDs; Wake stops recovery retries immediately when that ownership is gone.
Existing restart recovery remains conditional on PBL's 120-second recovery
grace, Wake's bounded retry window, original lease expiry, and current safety
inputs. Configuration-entry reload is an administrative interruption, not a
promise to resume an old target configuration.

PBL resolves original HA OFF selectors before dispatch and expands nested
HA/Z2M group scope. Turning off all wake leaves revokes even if an excluded
room member keeps the aggregate root on. User, unknown, HomeKit, and parented
helper control retain conservative no-relight protection. A diagnostic
`context_classification` is not proof of human versus automatic intent;
only registered owner contexts receive owner treatment.

Owner contexts remain identifiable while their service dispatch is in flight,
even beyond the ordinary context-cache TTL. Pending context capacity is bounded,
and an expired lease is rejected again at the pre-dispatch guard. These are
software ordering guarantees, not a claim that an already accepted physical
device command can be recalled. The coupled state-only tests prove observed
software queue ordering and policy/generation behavior, not physical in-flight
delivery or ambiguous latest-manual-ON ordering.

### Source lifecycle and snooze

Source events must match the side, captured schedule row, and execution-time
window. Once bound, their opaque occurrence ID and publication ordering are
also checked. An unseen old stop, wrong row, or another co-time alarm does not
dismiss unrelated intents. Retained state is reconciled before first/recovered
dispatch, not only when a new state-change event happens.

Source snooze consumes the validated absolute `snoozed_until`. Repeated
publications do not charge another interval, including at the budget limit.
The source budget counts deadline extensions from the original wake deadline
and preserves current/max brightness within the lease expiry. Wake Light has
no native snooze command or user-facing Snooze control; only an authoritative
linked source can move an active occurrence into the snoozed phase.

The Pod accepts commands only as non-retained MQTT messages. Retained **state**
remains supported; retained **commands** are discarded so reconnect cannot
reapply an old stop/snooze/configuration command to a later occurrence.

## `wake_light.command`

Every call requires:

```yaml
profile_id: master-bedroom
expected_revision: 4
request_id: 7c7310c8-opaque-client-token
operation: update_defaults
defaults:
  ramp_minutes: 30
  post_wake_hold_minutes: 5
```

Active controls may additionally send the public opaque `episode_ref`. A
matching episode anchor permits Stop through an unrelated configuration
revision change; a late request for a different episode is rejected. This is
not a raw lease token. Configuration writes still require the exact revision.

Supported operations:

| Operation | Additional fields |
| --- | --- |
| `upsert_alarm` | `alarm` |
| `delete_alarm` | `alarm_id` |
| `update_defaults` | `defaults` |
| `bind_source` | `source_ref`, `enabled` |
| `dismiss` | `occurrence_id` |
| `cancel_occurrence` | `occurrence_id` |
| `end_episode` | None |

An alarm uses:

```yaml
id: weekday-wake
label: Weekday Wake
kind: weekly
local_time: "06:30"
date:
weekdays:
  - monday
  - tuesday
enabled: true
ramp_minutes: 30
revision: 0
source: native
source_ref:
```

Defaults use `ramp_minutes` and `post_wake_hold_minutes`. New writes accept
exactly `0`, `5`, `10`, `15`, or `30` for the ramp; `0` means no pre-wake
fade, so the room is commanded to 100% at the wake deadline. New post-wake hold
writes accept exactly `5`, `10`, `15`, or `30` minutes. Legacy 45- or 60-minute
ramp values remain readable but must be replaced with a supported choice before
saving.

Stable service outcomes include `accepted`, `no_change`,
`revision_conflict`, `request_id_conflict`, `invalid_request`, `not_found`,
`read_only_source`, and `no_active_occurrence`. Source-snooze budget exhaustion
is retained as runtime failure telemetry rather than a native command outcome.
Reusing a request ID with the same payload returns the stored result with
`idempotent: true`; reusing it with a different payload returns
`request_id_conflict`.

The React client requests and consumes the response. It retains drafts until
acceptance, exposes pending/error state, and does not treat a queued request as
a successful save. Object-valued optimistic inputs retain stable identities
through unrelated telemetry. Stop is not queued behind a configuration edit.
The v3 frontend refuses writes against an incompatible summary contract.

The modal exposes only **Wake Alarms** and **Defaults**. New alarms default to
**One-Time**. Add titles use `Add Wake Alarm · <room>`; edit titles use
`<name> · <room>`. The editor actions are **Delete** and **Save**. Save remains
disabled for an unchanged existing alarm, becomes available after a normalized
editable field changes, and disables again when every edit is reverted.

### React state/service matrix

| Displayed state or action | Semantic | Service operation | Optimistic intent | Fail-closed behavior |
| --- | --- | --- | --- | --- |
| Individual native alarm enabled/disabled | Toggle | `upsert_alarm` | Show requested alarm state | No call while the summary entity is unavailable; backend rejects stale revisions |
| Add or edit native alarm | Modal command | `upsert_alarm` | Insert or replace the draft alarm | Backend rejects stale profile/alarm revisions and source-owned records |
| Delete native alarm | Destructive command | `delete_alarm` | Remove the selected alarm | Backend returns `not_found` or a revision conflict without mutation |
| Ramp and post-wake hold defaults | Radio selections | `update_defaults` | Show the selected supported durations | Backend rejects unsupported new values |
| Individual SleepyPod alarm wake-light link | Toggle | `link_alarm` | Show requested per-alarm link state | Backend accepts only configured source sides and validated alarm-link keys |
| Stop wake-light episode | Command | `end_episode` | Retain live state until HA confirms the stop | Ends the connected episode, preserves asleep PBL, and blocks auto-relight through the episode end |
| Summary unavailable | Modal/state | None until available | None | Modal remains a useful setup/status entry point |

Home Assistant remains the sole owner of schedule persistence, occurrence
execution, PBL leasing, and cascading light effects.

## Summary sensor contract

The sensor state is one of:

```text
idle
scheduled
blocked_vacation
ramping
snoozed
holding
recovering
degraded
unavailable
```

Its attributes are exactly:

```yaml
available: true
contract_version: 4
command_available: true
profile_id: master-bedroom
revision: 4
alarms: []
defaults:
  ramp_minutes: 30
  post_wake_hold_minutes: 5
next_wake_at: "2030-06-10T06:30:00-07:00"
next_ramp_minutes: 30
episode_ref:
progress: 0
commanded_brightness_pct: 0
active_occurrences: []
auto_relight_blocked_until:
last_outcome:
last_failure:
last_cancellation:
failures: []
current_blockers: []
limits:
  maximum_episode_minutes: 105
  interruption_policy: fail_closed
safety:
  vacation_state: "off"
  pbl_state: "ready"
  occupancy_state: "off"
  light_state: "ready"
  light_target_name: "Master Bedroom Lights"
source_bindings:
  sleepypod:left: true
  sleepypod:right: false
```

Each `active_occurrences` item exposes only the opaque occurrence ID required
for runtime controls plus `alarm_id`, `source_ref`, `wake_at`,
`snoozed_until`, and its own `phase`/`progress`. Top-level `progress` is
episode/output progress and never resets when an earlier overlapping
occurrence ends; `commanded_brightness_pct` is the last successful room
brightness command. Lease IDs and raw request IDs are never exposed.

`last_cancellation` contains only bounded, non-personal cancellation
telemetry: outcome, time, cancelled occurrence count/refs, and suppression
cutoff. A user-ended episode reports `cancelled_by_user`; it is not added to
the failure list and does not put the sensor in `degraded`.

Current blockers are separate from historical issues. A recovered input does
not keep the room degraded solely because an older failure remains recorded.
The UI displays requested active brightness, not measured bulb brightness,
and identifies the latest stop's affected alarm count rather than a lifetime
cancellation count.

No health data is parsed, stored, logged, or exposed. Store data retains only
the versioned alarm/config state, bounded hashed request correlations,
normalized source alarms/freshness, active occurrence/lease recovery state,
terminal occurrence IDs, and bounded outcome/failure records.

Logs use the stable profile ID plus hashed request/occurrence/lease
correlations; they never include alarm labels or SleepyPod health attributes.
The internal `wake_light_outcome` event carries only `profile_id`, `outcome`,
`occurrence_ref`, and `lease_ref`. PBL denial details are retained as bounded
`pbl_<action>:<reason>` failure codes so lease-mode, membership, blocker, and
enforcement problems can be diagnosed before enabling a room.

## Local validation

The local changes do not deploy any integration, alter existing Pod rows, or
change live alarm enablement or source bindings. In particular, no automatic
weekday data migration accompanies the execution-day correction.

The pure scheduler and state machine do not import Home Assistant:

```bash
python -m unittest discover -s home-assistant/tests -p 'test_wake_light*.py' -v
python -m compileall -q home-assistant/custom_components/wake_light home-assistant/tests
```

React contract validation:

```bash
npm run test:run -- \
  src/components/hass/wakeLights/wakeLightContract.test.ts \
  src/components/hass/wakeLights/WakeLightModalContent.test.tsx \
  src/pages/DashboardViewPage.wakeLight.test.tsx
npm run i18n:check
npm run design:check
npm run test:design
npm run lint
npm run build
```

The shared JSON fixture is
`home-assistant/tests/fixtures/sleepypod-alarm-v2.json`, mirrored in the Pod's
MQTT tests. Coupled software ordering/recovery coverage uses the PBL checkout's
existing pytest environment and in-memory HA boundaries:

```bash
PBL_SOURCE_PATH=/path/to/pbl \
  /path/to/pbl/.venv/bin/python -m pytest \
  home-assistant/tests/coupled/test_wake_pbl.py -q
```

The dedicated LAN preview is started with `vite.wake-preview.config.ts` in
`--mode test`. For household visual review, set
`WAKE_PREVIEW_REAL_CONTROLS=1`: the installed HAKit controls and theme render
normally, while the connection, entity, user, and service state remain mocked.
The default mode retains the repository's deterministic component stand-ins,
which omit toggle chrome and replace circular dials with simple ranges; those
stand-ins are not evidence of production control appearance.

Both preview variants have no HA proxies or tokens and carry the catalog-backed
mock identity. `X-Dashboard-Data: mock` identifies the backend and
`X-Dashboard-Controls: real|test-stubs` identifies the rendering scope. Neither
preview can control production through an undeployed backend protocol.

Current-base integration follows `docs/ux/layouts.md`. The registry declares
Wake alarm/default, native-editor, and authoritative-source journeys; the
guarded runner owns both immutable-baseline and candidate mock builds and
their endpoint identities. Run `layout:check`, `layout:plan`, `layout:run`,
the actual manual review worklist, and `layout:verify`. Supplementary actual
HAKit paint checks use `playwright.wake-real-controls.config.ts`, an explicit
unused port, locally supplied icon paths, and the same network guard.

All route parity thresholds remain strict. The registry explicitly describes
the Master Bedroom addition: one standard 120px tile/section and unchanged
inherited cards. Its geometry and semantics are asserted and its visible
layout is captured before only that section is removed for inherited-content
pixel comparison. This is not a whole-route exclusion or a claim that a new
section has zero unmodified full-page pixel delta.

The current-base port retains the released shared `ModalSheet` flow-root
padding fix. The source bed editor no longer drops both its body and pane
scroll owners below the former 799px-height boundary. Normal dialogs retain
bounded panes; sheets and short landscape use an intrinsic flex panel stack,
preventing a stale WebKit grid height from consuming end clearance on return.
Named boundary profiles, exact inset assertions and source-day/detail/back
checkpoints cover that coupled correction.

Wake's own body-scrolling layout also uses intrinsic block/flex flow. The exact
WebKit device-descriptor regression showed an alert/viewport change retaining
an ancestor grid height about 13px shorter than its contents. Replacing only
the inner grid was insufficient; the body-owned ancestor chain now sizes from
its content, preserving the strict end inset through repeated readiness-alert
rotations. Normal-dialog bounded grid panes remain unchanged.

## Explicitly deferred platform scope

Dedicated waking-only Siri/Shortcut/HomeKit entry, generic iOS Clock
interception, Apple Health sleep-stage scheduling, and biometric wake windows
are not implemented. Existing SleepyPod HomeKit power/temperature/snooze/stop
controls remain intact. A future HA-owned adapter could obtain command
revisions and request IDs; absence of that adapter is not a platform
impossibility claim. No REM or deep-sleep scheduling is inferred from occupancy.
