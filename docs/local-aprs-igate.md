# Local APRS — receive-only Dire Wolf iGate (M2.2)

How aether ingests the operator's own 144.39 MHz APRS reception: Dire Wolf
demodulates and decodes RF off an RTL-SDR, exposes decoded frames on a TCP KISS
socket, and **receive-only** gates valid RF-heard packets up to APRS-IS itself.
aether's local APRS adapter reads that KISS socket, normalizes each frame to a
schema-v2 `TrackRecord`, and publishes it to the bus with `local_rf` provenance
(PRD §11.6, §18.3).

> **Receive-only — the load-bearing guardrail (decision 1 / PRD §2.3,
> APRS-FR-003/004).** The station may forward valid packets it *hears* over RF
> to APRS-IS, but it never transmits over RF: no beacon, no digipeat, no RF
> message ack, and no Internet-to-RF path. Dire Wolf owns the RF→APRS-IS gating
> and its counts; aether does **not** implement custom packet gating
> (APRS-FR-004) and is **strictly read-only** on the KISS socket — it never
> writes a frame back, because writing a KISS data frame asks Dire Wolf to
> transmit (PRD §18.3: "Never send packets back to KISS/AGW for transmission.").

---

## Responsibility split (PRD §18.3)

**Dire Wolf** does the radio work: audio demodulation, AX.25/APRS decoding, CRC
validation, APRS-IS login, eligible RF→Internet packet gating, and the
duplicate/loop protections that come with Dire Wolf / APRS-IS behavior.

**aether** consumes Dire Wolf's KISS output for display only: it parses the
AX.25/APRS fields it needs, normalizes track records (SI units, identity keys),
and reports its *own* connection health (`records_received` / `records_rejected`)
as a source-status record. aether cannot reconstruct Dire Wolf's gating counts
(KISS carries decoded frames, not gating decisions), so those stay Dire Wolf's.

---

## 1. Run Dire Wolf receive-only

Copy the sample config and supply your own callsign + APRS-IS passcode (the repo
ships neither):

```bash
cp config/direwolf.conf.example direwolf.conf
# edit direwolf.conf: set MYCALL and IGLOGIN <callsign> <passcode>
```

The sample is a receive-only iGate: receive chain (`ADEVICE null null`,
`CHANNEL 0`, `MODEM 1200`, `KISSPORT 8001`) plus the RF→APRS-IS gate
(`IGSERVER`, `IGLOGIN`). It intentionally contains **no** `IGTXVIA`, `PBEACON`,
`TBEACON`, `DIGIPEAT`, `PTT`, or any other transmit/beacon/digipeat/
Internet-to-RF directive — see the comment block in the file for the full list
and why each must stay out. With no `IGTXVIA` there is no Internet-to-RF path;
with no `PTT` Dire Wolf cannot key a transmitter.

Launch it against an RTL-SDR with the audio piped in (the input half is
overridden on the command line, which is why `ADEVICE` is `null null`):

```bash
rtl_fm -f 144.39M -o 4 -s 24000 -g 49 -p <ppm> - | \
    direwolf -c direwolf.conf -r 24000 -D 1 -t 0 -
```

`-r 24000` matches `rtl_fm -s 24000`; `-t 0` disables color; the trailing `-`
tells Dire Wolf to read audio from stdin. Dire Wolf now decodes 144.39 MHz APRS,
serves decoded frames on TCP KISS port **8001**, and relays eligible RF-heard
packets to APRS-IS. Use a dedicated dongle addressed by serial — one RTL-SDR per
continuous RF service, no antenna switching.

---

## 2. Point the adapter at KISS port 8001

The local APRS adapter is off by default and reads the KISS socket only when
enabled. All keys have safe loopback defaults (PRD §22):

| Env var | Default | Meaning |
| --- | --- | --- |
| `AETHER_LOCAL_APRS` | `0` | Run the local APRS adapter alongside the backend. |
| `AETHER_LOCAL_APRS_HOST` | `127.0.0.1` | Dire Wolf KISS host (loopback). |
| `AETHER_LOCAL_APRS_PORT` | `8001` | Dire Wolf `KISSPORT`. |
| `AETHER_LOCAL_APRS_THROTTLE_S` | `1.0` | At most one ordinary update per station per window; emergencies bypass it. |
| `AETHER_LOCAL_APRS_TIMEOUT_S` | `5.0` | KISS socket read/connect timeout. |

With Dire Wolf running on the same host:

```bash
AETHER_DEMO_SOURCE=0 AETHER_LOCAL_APRS=1 \
    uvicorn aether.backend.main:app --app-dir src
```

Local APRS tracks now render with `local_rf` provenance, alongside a
`source_status:local_aprs` record reporting connection health. A dropped socket
or a malformed frame is logged and the loop backs off / continues — one bad
frame or a downed decoder never crashes the backend (PRD §17.4, §37).

---

## 3. No-hardware path (the verification gate)

No SDR and no Dire Wolf required. The `aprs_fake_feeder` stands in for Dire Wolf
by running a **fake TCP KISS server** that streams canned KISS+AX.25 frames, so
the adapter exercises the real socket + framing + parser path end to end (PRD §6
no-hardware gate, §34 "every source ships a fake/replay feeder"). It is a server
that emits frames only — it never transmits or touches a radio.

```bash
# Terminal 1 — fake KISS server on 127.0.0.1:8001
python -m aether.adapters.aprs_fake_feeder 127.0.0.1 8001

# Terminal 2 — backend reads it as if it were Dire Wolf
AETHER_DEMO_SOURCE=0 AETHER_LOCAL_APRS=1 \
    AETHER_LOCAL_APRS_HOST=127.0.0.1 AETHER_LOCAL_APRS_PORT=8001 \
    uvicorn aether.backend.main:app --app-dir src
```

Then confirm the canned APRS tracks are in live state and on the websocket:

```bash
curl -s localhost:8000/api/state | python -m json.tool   # local_aprs tracks present
```

The integration test drives this same fake-server path and asserts records
arrive on `/ws/v2`. As with every source, the simulated path must be green
before any hardware or live feed (PRD §34).

---

## Scope (first cut)

The parser handles uncompressed/compressed positions, objects, items, status,
and weather; Mic-E, telemetry, messages, and raw-GPS frames are recognized and
skipped (not mis-parsed) — deferred follow-up work. APRS-IS *display* (network
APRS, distinct from this local RF reception) and local↔network fusion land in M3
(PRD §18.4, §11.7).

---

## References

- PRD §2.3 — decisions that remain prohibited (no Internet-to-RF gating,
  beaconing, digipeating, RF acks, PTT/transmitter control).
- PRD §11.6 — local APRS and iGate requirements (APRS-FR-001…010).
- PRD §18.3 — APRS local adapter and iGate responsibility split; "Never send
  packets back to KISS/AGW for transmission"; "Configuration shall require
  explicit receive-only iGate settings."
- PRD §17.1/§17.4/§37 — common adapter contract, backoff, failure isolation.
- PRD §6/§34 — no-hardware verification gate and per-source fake/replay feeder.
- `config/direwolf.conf.example` — the shipped receive-only sample config.
