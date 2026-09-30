# HassControl BLE protocol (v2)

The watch talks to Home Assistant directly over Bluetooth Low Energy. Home
Assistant (the `garmin_ble` custom integration in `custom_components/`) is the
GATT **peripheral**; the watch is the **central**. No phone, no Wi-Fi, no
bridge device.

Every frame in both directions fits in **20 bytes**. Connect IQ throws
`InvalidRequestException` for characteristic writes longer than 20 bytes and
does not implement long reads or long writes, and the ATT MTU on the watch is
not negotiable from Connect IQ, so notifications are also kept to 20 bytes.

## Pairing (required)

Standard LE Secure Connections pairing with Numeric Comparison; HA's side of
the comparison is a card in Settings > Devices & services.

1. Unpaired, the watch app sends the phone (via Garmin Connect) a link to
   HA's integrations page and starts pairing.
2. The watch shows the 6-digit code; at the same moment HA opens a card
   "Allow Garmin watch XX:XX:XX:XX:XX:XX? Code NNNNNN". HA's pairing agent
   answers BlueZ when the card is submitted (accept) or Ignored (refuse).
   The user confirms on the watch too. If the attempt ends otherwise (the
   watch disconnects because the user declined or its pairing timed out,
   or BlueZ cancels the request) the card closes; pairing again from the
   watch opens a new one.
3. A completed pairing is the approval: HA stores the watch with a random
   16-byte command key. The first HELLO without the key flag gets MSG_KEY
   over the encrypted link, then CHALLENGE; every command is signed with it.

Enforcement on HA: CMD is `secure-write` and EVT's CCCD `secure-notify`
(BlueZ rejects both on links without an LE Secure Connections key); Just
Works pairing is refused; HELLO from an address that is not a paired watch
gets NOT_PAIRED; pairing from an address ignored in HA is refused until it
is un-ignored. The integration's options can forget watches (key and bond
removed).

Watch side: with no bond, the app connects with Connect IQ's secure pairing
strategy (the system pairs while connecting). That connection lists no
services on a Fenix 7, so when BlueZ reports the pairing complete HA drops
the link and the watch reconnects normally (default strategy, bonded).
Neither side runs a pairing timer: the attempt ends on a confirmation, a
refusal, a disconnect or a BlueZ cancel.

HA cannot start pairing itself: Connect IQ apps can only act as a BLE
central, so the watch never advertises anything HA could connect to.

## GATT layout

| Item | UUID | Properties |
|---|---|---|
| Service | `6a1e0001-4c7d-4b4e-9a2b-3c8f1d2e5a01` | primary, advertised |
| CMD characteristic (watch to HA) | `6a1e0002-4c7d-4b4e-9a2b-3c8f1d2e5a01` | write (with response), encrypted + authenticated link required |
| EVT characteristic (HA to watch) | `6a1e0003-4c7d-4b4e-9a2b-3c8f1d2e5a01` | notify, encrypted + authenticated link required to subscribe |

The advertisement carries only the 128-bit service UUID (no name, so HA does
not appear by name in other devices' Bluetooth lists); the watch finds HA by
that UUID. The characteristics need a bonded, encrypted link
(see above); the application-layer MAC below additionally binds every command
to the per-watch key and the session.

Only one central can be connected at a time on the tested controller: while a
watch is connected the advertisement is not visible to others. HA therefore
drops a connection that sends no authenticated frame for `idle_timeout`
seconds (default 30), and the watch disconnects as soon as the app closes.

## Watch to HA: command frames (CMD writes)

```
byte 0        op
byte 1        ctr8    low 8 bits of the session counter (authenticated ops only)
bytes 2..n-5  payload (max 14 bytes)
last 4 bytes  tag = HMAC-SHA256(key, nonce || ctr_be32 || op || payload)[0:4]
```

`HELLO` is the only unauthenticated op and has no `ctr8` or tag.

| op | name | payload | HA reply |
|---|---|---|---|
| 0x01 | HELLO | `version u8` (= 2), `flags u8` (bit 0: watch has a key) | KEY if the watch has none, then CHALLENGE; or RESULT |
| 0x02 | LIST | (none) | ENTITY per exposed entity, then LIST_END |
| 0x03 | GET | `idx u8` | ENTITY |
| 0x04 | ACTION | `idx u8, action u8, arg...` | RESULT, then ENTITY with the new state when it changes |
| 0x05 | BATTERY | `percent u8, charging u8` | RESULT |
| 0x06 | PING | (none) | nothing (keeps the session from the idle timeout; the app sends it every 20 s while open) |
| 0x07 | BYE | (none) | HA disconnects |

ACTION codes (the first eight equal hasscontrol's `Client.ENTITY_ACTION_*`):

| code | meaning | HA service |
|---|---|---|
| 0 | turn on | `<domain>.turn_on` (scene/script: `turn_on`) |
| 1 | turn off | `<domain>.turn_off` |
| 2 | lock | `lock.lock` |
| 3 | unlock | `lock.unlock` |
| 4 | close | `cover.close_cover` / `valve.close_valve` |
| 5 | open | `cover.open_cover` / `valve.open_valve` |
| 6 | cover toggle | `cover.toggle` / `valve.toggle` |
| 7 | press | `button.press` / `input_button.press` |
| 0x10 | select option | `select.select_option` / `input_select.select_option`; arg `option_index u8` |
| 0x11 | set value | `number.set_value` / `input_number.set_value`; arg `float32 big-endian` |

HA only accepts an action that fits the entity's domain, and only for
entities carrying the configured label (default `garmin`). Everything else is
rejected with status `NOT_ALLOWED`.

### Session and replay protection

1. On connect the watch writes `HELLO`.
2. HA answers `CHALLENGE` with a fresh random 8-byte `nonce` for this
   connection and resets the session counter to 0.
3. Every later frame uses the next counter value (1, 2, 3, ...). The full
   32-bit value is covered by the tag; only its low byte is sent. HA
   reconstructs it as the smallest value greater than the last accepted one
   with that low byte, and accepts it only if it is at most 32 ahead, so a
   lost write does not wedge the session but an old frame cannot be replayed.
4. `key` is the 16-byte per-watch key HA issued with MSG_KEY. On BAD_AUTH
   the watch drops its key and sends HELLO without the key flag.

HA to watch messages are not authenticated (a spoofed state display is the
worst outcome); commands are.

## HA to watch: messages (EVT notifications)

Messages are split into fragments of at most 20 bytes:

```
byte 0     header: bit 7 = last fragment, bits 0..6 = message sequence (mod 128)
bytes 1..  up to 19 bytes of message data
```

Fragments of one message are sent back to back; the watch appends data until
it sees the last-fragment bit.

| type | name | body |
|---|---|---|
| 0x81 | CHALLENGE | `nonce[8], version u8, entity_count u8` |
| 0x82 | ENTITY | `idx u8` then TLV fields |
| 0x83 | LIST_END | `count u8` |
| 0x84 | RESULT | `ctr8 u8, status u8` |
| 0x85 | KEY | `key[16]` (answer to HELLO without the key flag) |

ENTITY TLV fields are `tag u8, len u8, UTF-8 bytes` (each string cut to 255
bytes):

| tag | field | source |
|---|---|---|
| 1 | entity_id | `entity_id` |
| 2 | state | `state` |
| 3 | friendly_name | `attributes.friendly_name` |
| 4 | unit | `attributes.unit_of_measurement` |
| 5 | device_class | `attributes.device_class` |
| 6 | icon | `attributes.icon` |
| 7 | options | `attributes.options` joined with 0x1F |
| 8 | min | `attributes.min` (decimal text) |
| 9 | max | `attributes.max` |
| 10 | step | `attributes.step` |

Fields that are absent are omitted. Entity indices are stable for the
connection (entities sorted by entity_id when the connection authenticates).

RESULT status codes: 0 OK, 1 BAD_AUTH, 2 BAD_INDEX, 3 NOT_ALLOWED,
4 SERVICE_ERROR, 5 BAD_FRAME (also HELLO with another protocol version),
6 NO_SESSION, 7 NOT_PAIRED (HELLO from a device that is not a paired
watch), 8 NOT_APPROVED (unused since pairing itself is confirmed in HA). HELLO answers
use ctr8 0.

After authentication HA also pushes an ENTITY message whenever an exposed
entity changes state, so the watch does not need to poll.
