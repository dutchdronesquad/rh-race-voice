# Independent Sendspin group feasibility

Prototype for #302, part of #296. Tested with the installed `aiosendspin` 9.1.0
using its actual server, persistent clients, player roles, groups, push streams,
and PCM transformation/delivery. Only network/device delivery callbacks are
replaced, so this does not establish WindowsSpin or browser audio quality.

`tests/test_sendspin_groups.py` demonstrates that two groups receive distinct PCM
and that stopping one group's stream leaves the other usable. It also moves one
listener out of a shared active group: the remaining player keeps receiving audio,
while the moved listener receives no further PCM from the old group.

Use `server.get_or_create_client`, `client.group`, `group.add_client`, and
`group.start_stream`. Do not instantiate `SendspinGroup` directly; the SDK manages
group lifetime and ensures every client belongs to a group. The existing Race
Voice backend automatically collects clients into one group, so independent
selection routing must replace that policy before this works in the application.

The prototype supports proceeding with on-demand groups for compatible listener
selections. Keep timeline and cancellation state per group. Append ordinary PCM
using the stream's next timestamp; repeatedly assigning `now + lead` can overlap
already buffered chunks. Reserve explicit timestamps for actual scheduled targets
and have the planner resolve conflicts before committing audio.

Remaining #302 implementation and validation:

- Add bounded selection/group lifecycle management and per-group playback state.
- Bind selection controls to the requesting player's authenticated session.
- Define switching during already audible/buffered speech and constrain late-join
  catch-up to current useful audio, without replaying past lap callouts.
- Verify reconnects, last-member departures, slow clients and encoding differences.
- Preserve default WindowsSpin playback and general race signals in every group.
- Measure CPU, memory, bandwidth and practical selection counts on Raspberry Pi 4.

The operator's client is WindowsSpin 2.2.6. Pi RAM and remote access are not yet
known. The tests open no production listener, discover no real clients, and play
no sound. They validate SDK feasibility, not complete personalized playback.
