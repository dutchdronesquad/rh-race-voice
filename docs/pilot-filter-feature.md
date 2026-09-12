# Pilot Filter Feature — Design Document

A feature that lets each connected Sendspin player choose which audio it receives, filtered by pilot. A device set to "My pilot" only hears announcements relevant to that pilot (crossings, lap times) plus general race events (race start/stop, heat winner).

---

## User experience

The player settings panel gains a **"My pilot"** dropdown below the existing sync mode selector:

```
My pilot
[ All pilots ▾ ]        ← default, current behaviour
```

When expanded:

```
[ All pilots          ]
  ──────────────────
  Alice
  Bob
  Charlie
  ...
```

- Default is **All pilots** — identical to current behaviour, no audio is filtered.
- Selecting a pilot stores the choice in `localStorage` (`raceVoice.player.pilotId`).
- The player re-connects automatically after changing the pilot (needed to join the right server-side group).
- The pilot list is fetched from the Sendspin service when a connection is established.

---

## Architecture overview

```
RotorHazard plugin
  │  knows pilot names + IDs at all times
  │  tags each AudioJob with pilot_id (or None for general events)
  ▼
Sendspin service (Python)
  │  maintains one Sendspin group per unique pilot filter
  │  serves GET /pilots  →  [{id, name}, ...]
  │  on ClientAdded: reads pilot preference from clientName, joins group
  ▼
Sendspin player (TypeScript)
  │  fetches /pilots on connect
  │  shows dropdown, stores choice in localStorage
  │  encodes choice in clientName when connecting
  │  receives only audio for its group
```

---

## Audio tagging

Each `AudioJob` gets a new optional field:

```python
# audio_queue.py
@dataclass(order=True)
class AudioJob:
    ...
    pilot_id: str | None = field(compare=False, default=None)
```

`None` means the audio is general (race start, race stop, heat winner) and goes to **all** groups. A non-`None` value means only groups subscribed to that pilot (plus the "all pilots" group) receive it.

The RotorHazard plugin sets `pilot_id` when calling `enqueue()`:

```python
# crossing callout
queue.enqueue(text, wav_items, priority=Priority.LOW, pilot_id="abc-123")

# heat winner (general)
queue.enqueue(text, wav_items, priority=Priority.HIGH, pilot_id=None)
```

---

## Sendspin service changes

### 1. Pilot list endpoint

The service adds a lightweight HTTP endpoint alongside the Sendspin WebSocket server. `aiosendspin` does not support custom routes natively, so a small `aiohttp` (already available) server runs on the same port via `aiohttp.web.Application` with a shared runner, or on a dedicated sidecar port (e.g. `8928`).

```
GET /pilots
→ 200 [{"id": "abc-123", "name": "Alice"}, ...]
```

The pilot list is kept in memory, updated by the RotorHazard plugin whenever pilots change (RH fires `pilot_alter` and `pilot_delete` events).

### 2. Group management

Replace the single `_stream_group` / `_stream` / `_next_play_start_us` / `_idle_stop_task` attributes with a `dict` keyed by pilot filter:

```python
# sendspin.py

_FILTER_ALL = "__all__"  # sentinel for "all pilots" group


@dataclass
class _ChannelState:
    group: SendspinGroup | None = None
    stream: _PushStream | None = None
    next_play_start_us: int | None = None
    idle_stop_task: asyncio.Task[None] | None = None


class SendSpinServer:
    def __init__(self, ...):
        ...
        self._channels: dict[str, _ChannelState] = {}
        # pilot_id -> filter key the client is subscribed to
        self._client_filter: dict[str, str] = {}
```

### 3. Client routing on connect

Register an event listener during `_start_server()`:

```python
server.add_event_listener(self._on_server_event)
```

```python
async def _on_server_event(
    self, server: AioSendspinServer, event: SendspinEvent
) -> None:
    if isinstance(event, ClientAddedEvent | ClientUpdatedEvent):
        await self._route_client(event.client)


async def _route_client(self, client: SendspinClient) -> None:
    filter_key = _parse_pilot_filter(client.name)  # reads clientName
    self._client_filter[client.client_id] = filter_key
    channel = self._channels.setdefault(filter_key, _ChannelState())
    if channel.group is None or client not in channel.group.clients:
        if channel.group is None:
            channel.group = client.group  # first client creates the group
        else:
            await channel.group.add_client(client)
```

### 4. Audio routing in `_append_to_stream`

```python
async def _append_to_stream(
    self, clips, pilot_id, expires_at, play_at, duration_s, volume
):
    target_keys = {_FILTER_ALL}  # always send to "all pilots" group
    if pilot_id is not None:
        target_keys.add(pilot_id)  # also send to pilot-specific group

    for key in target_keys:
        channel = self._channels.get(key)
        if channel and channel.group:
            await self._append_to_channel(
                channel, clips, expires_at, play_at, duration_s, volume
            )
```

### 5. `_parse_pilot_filter` helper

```python
def _parse_pilot_filter(client_name: str) -> str:
    """Extract pilot ID from clientName, e.g. 'Race Voice [pilot:abc-123]'."""
    import re

    m = re.search(r"\[pilot:([^\]]+)\]", client_name)
    return m.group(1) if m else _FILTER_ALL
```

---

## Sendspin player (TypeScript) changes

### 1. Fetch pilot list

After a successful `player.connect()`:

```typescript
async function fetchPilots(baseUrl: string): Promise<Pilot[]> {
  const res = await fetch(`${baseUrl}/pilots`);
  if (!res.ok) return [];
  return res.json();
}

type Pilot = { id: string; name: string };
```

Store result in `useState<Pilot[]>`.

### 2. Pilot state

```typescript
const STORE_PILOT_ID = "raceVoice.player.pilotId";

const [pilots, setPilots] = useState<Pilot[]>([]);
const [pilotId, setPilotId] = useState<string | null>(
  () => localStorage.getItem(STORE_PILOT_ID)
);
```

### 3. Encode preference in `clientName`

```typescript
const clientName = pilotId
  ? `Race Voice Browser Player [pilot:${pilotId}]`
  : "Race Voice Browser Player";
```

This value is passed to `new SendspinPlayer({ clientName, ... })`.

### 4. Re-connect on pilot change

When the user picks a different pilot while connected, call `disconnect()` then `connect()` automatically so the server can re-route the client to the new group.

### 5. UI

Add below the sync mode selector in the controls section:

```tsx
<div className="flex flex-col gap-1.5">
  <Label className="text-[0.75rem] text-muted-foreground">My pilot</Label>
  <Select
    value={pilotId ?? ""}
    onValueChange={(v) => updatePilot(v || null)}
    disabled={pilots.length === 0}
  >
    <SelectTrigger className="!h-9 w-full text-[0.82rem]">
      <SelectValue placeholder="All pilots" />
    </SelectTrigger>
    <SelectContent>
      <SelectItem value="">All pilots</SelectItem>
      {pilots.map((p) => (
        <SelectItem key={p.id} value={p.id}>{p.name}</SelectItem>
      ))}
    </SelectContent>
  </Select>
</div>
```

---

## RotorHazard plugin changes

- Subscribe to `pilot_alter` and `pilot_delete` RH events to keep the pilot list in the service up to date.
- Pass `pilot_id` to `queue.enqueue()` for events that are pilot-specific:
  - Crossing beep → `pilot_id = node.pilot_id`
  - Lap callout → `pilot_id = node.pilot_id`
  - Race winner → `pilot_id = None` (general)
  - Race start / stop → `pilot_id = None` (general)

---

## Open questions

1. **Sidecar port or shared runner?** Decide whether `/pilots` runs on a dedicated port (simpler, one extra firewall rule) or shares port 8927 via `aiohttp` alongside `aiosendspin`.
2. **Pilot list refresh** — does the player poll `/pilots` periodically, or only on connect? Pilots rarely change during an event so on-connect is probably sufficient.
3. **Empty group cleanup** — when all clients leave a pilot-specific group, should the `_ChannelState` be removed from `_channels`? Avoids unbounded growth across a long event day.
4. **General audio to pilot-filtered clients** — confirm the desired behaviour: a device set to "Alice" should still hear race start, race stop, and heat winner callouts. This is the current design (`pilot_id=None` → all groups), but worth validating with users.
