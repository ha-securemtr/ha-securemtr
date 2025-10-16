# Secure Controls / Beanbag — WebSocket Function Catalog (v0.4.0)

**Scope:** Confirmed message shapes and semantics from live captures. Two immersion circuits:
- **slot 1** → primary immersion (**state block `SI:33`**), on/off via mode write.
- **slot 2** → boost immersion (**state block `SI:16`**), timed boost & feature toggle.

## Connection (confirmed)

- URL: `wss://app.beanbag.online/api/TransactionRestAPI/ConnectWebSocket`
- Subprotocol: `BB-BO-01`
- Required upgrade headers: `Authorization: Bearer {token}`, `Session-id: {sessionId}`, `Request-id: 1`

## Envelopes (confirmed)

- **Client request**:
  ```json
  { "V":"1.0","DTS":{epoch},"I":"{sessionId}-{rand32}","M":"Request","P":[{"GMI":"{gatewayID}","HI":<op>,"SI":<subop>}, [/*args*/]] }
  ```
- **Server reply**: adds `"R"`; **Server notify** uses `"M":"Notify"` with `P:[{GMI,SI,HI:4}, [slot, {I,V,OT,D}]]`

---

## Function Table

| Name | HI/SI | Dir | Slot | Args (second tuple) | Response (`R`) | Notify (block/items) | Notes |
|---|---|---|---:|---|---|---|---|
| **zones.read** | 49/11 | C→S | — | — | `[{ZT,CN,ZN,ZNM},…]` | — | Issued on connect & Refresh |
| **time.tick** | 2/103 | C→S | — | `[ <DTS> ]` | `0` | — | Keepalive / clock sync used in bursts |
| **schedules.summary** | 5/1 | C→S | — | — | `{"V":[{"I":1,"SI":16,"V":[{"ALI","OR","AB","TS"}…]}]}` | — | Program overview |
| **device.metadata.read** | 17/11 | C→S | — | — | `{"BOI","UI","N","SN","FV","MD","AS",…}` | — | Device info |
| **device.config.read** | 14/11 | C→S | — | — | `{"V":[{"BOI":1,"SI":16,"V":[{"CI","CV"}…]}]}` | — | Config params |
| **state.read** | 3/1 | C→S | — | — | `{"V":[{"I":1,"SI":33,…},{"I":2,"SI":16,…}]}` | — | Live state blocks (primary/boost) |
| **energy.history.read** | 9/36 | C→S | — | `[ <windowIndex> ]` | series | — | Map index ↔ UI window TBD |
| **program.read** | 22/17 | C→S | — | `[ <index> ]` | `[{ "I": index, "D":[ {"O","T"}, … ]}]` | — | **Observed:** index **2** = boost schedule |
| **program.write** | 21/17 | C→S | — | `[ { "I": index, "D":[ {"O","T"}, … ] } ]` | `0` | (opt) readback | `O`=minutes since midnight; `T`: `1=Boost ON`, `0=Boost OFF`, `255=sentinel` |
| **mode.write.primary** | 2/15 | C→S | 1 | `[ 1, { "I":6, "V":0|2 } ]` | `0` | `SI:33` / `{I:6,V:0|2}` | `0=Off`, `2=On` (primary immersion) |
| **boost.timed.start** | 2/16 | C→S | 2 | `[ 2, { "D":<minutes>, "I":4, "OT":2, "V":0 } ]` | `0` | `SI:16` / `{I:4,V:1,OT:2,D:<min>}`, `{I:9,V:<endMin>}`, `{I:10,V:0}` | End time (`I:9`) is minutes since midnight |
| **boost.timed.stop** | 2/16 | C→S | 2 | `[ 2, { "D":0, "I":4, "OT":2, "V":0 } ]` | `0` | `SI:16` / `{I:4,V:0,D:0}`, `{I:9,V:<baseline>}`, `{I:10,V:0}` | Cancels active boost |
| **boost.timed.toggle** | 2/16 | C→S | 2 | `[ 2, { "I":27, "V":0|1 } ]` | `0` | `SI:16` / `{I:27,V:0|1}` | Toggle does **not** gate manual `D:minutes` start |

### Schedule model (confirmed)
- **Index 2** corresponds to the **boost program**.
- `O` = **minutes since midnight**; `T` codes:
  - `1` = **Boost ON** at `O`.
  - `0` = **Boost OFF** at `O`.
  - `255` = **sentinel/unused** padding (appears as `{O:65535,"T":255}`).
- Each day has a fixed number of slots; unused entries are padded by sentinel tuples.

**Example (Sunday):** _boost on 01:45, off 08:45_
```json
{ "I": 2, "D": [ { "O": 105, "T": 1 }, { "O": 525, "T": 0 } /* + padding ... */ ] }
```

---

## Versioning

- Catalog: **v0.4.0** (matches OpenAPI & AsyncAPI files).
- Changes vs v0.3.0: program index **2 = boost** confirmed; schedule `T` code map added; `O` minutes confirmed; examples updated.
