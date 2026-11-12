# Broadcast scale protocols

How the scales this integration supports put readings in their Bluetooth
advertisements. Only the FG260 layout has been confirmed on a real scale.
The others were worked out from how the FitDays app handles them and are
**untested**.

Notation: `mfr[0..]` are the manufacturer-specific data bytes as sent on air.
The first two bytes are the company ID (little-endian), so Home Assistant
shows `mfr[0] | mfr[1] << 8` as the key and `mfr[2..]` as the payload.
`BE16` means a big-endian 16-bit value.

## 1. iComon broadcast (the FG260 and relatives)

**Recognition**
- `mfr[0]` is `0xAC` (or `0xCA`).
- `k = mfr[1]` describes the scale:
  - bit 7: broadcast mode (must be set);
  - bits 4–6: category: 1 weight scale, 2 body-fat scale, 3 balance scale;
  - bits 0–3: sub-type. Sub-types 3–6 ("A4" scales) only work over a connection.
- At least 14 bytes. `mfr[2..7]` is the scale's MAC address, reversed.

**Data**
`mfr[8..13]` XOR `k` gives `d0 d1 d2 d3 cmd cks`.
- Checksum: `(d0+d1+d2+d3+cmd) & 0x1F == cks & 0x1F`.
- Display unit: `cks >> 5` (0 kg, 1 lb, 2 st, 3 jin).

| cmd | meaning | fields |
|---|---|---|
| `0xAD` | weight (FG260) | `v = BE32(d0..d3)`: grams `v & 0x3FFFF`; stable bit 31; kg display-step code bits 18–20; lb display-step code bits 21–23; 8-electrode bit 24; heart-rate support bit 25 |
| `0xA6` | impedance (FG260) | raw ADC `BE16(d0,d1)`, heart rate `d2`, body-fat formula number `d3` (3 = WLA03, 0 = the user's choice) |
| `0xAF` | impedance | heart rate `d1`, impedance `BE16(d2,d3) / 10` |
| `0xAA` / `0xCA` | stable weight | `BE16(d0,d1)` × 10 g |
| `0xAE` / `0xCE` | live weight | `BE16(d0,d1)` × 10 g |
| `0xA9` | live weight + temperature | weight as above; `(BE16(d2,d3) & 0xFFF) / 10` °C |
| `0xAB` / `0xCB` | weight + impedance | weight as above; impedance `BE16(d2,d3)` |
| `0xA0` / `0xA4` | left/right balance | ignored |

For `0xCA`/`0xCE` from a scale with `k == 0x01`, `d2,d3` hold the temperature
as for `0xA9`.

Impedance: for `0xA6`, a raw value of 1500 or more is corrected using the
weight (see `body/wla03.py`). For the other frames, values above 1500 are
divided by 10.

Display-step codes: 0 → 0.01, 1 → 0.02, 2 → 0.05, 3 → 0.1, 4 → 0.2 units.

## 2. "BM15"

**Recognition:** `mfr[0] == 0xBC`, exactly 15 bytes, and no service UUIDs in
the advert.

| bytes | content |
|---|---|
| `mfr[2]` | status `S` |
| `mfr[3..10]` | one 8-byte TEA block (two little-endian 32-bit words) |
| `mfr[11..14]` | low four bytes of the MAC address |

The block is decrypted with standard TEA (32 rounds) using a fixed key, which
is the same for every scale (see `parser.py`). The plaintext holds:
- weight `BE16(P0,P1) / 10` kg;
- impedance `BE16(P2,P3)` ohms;
- temperature `BE16(P4,P5) // 10` °C.

State: `r = (S >> 3) & 0x1F`. If `r & 0x1E == 0`, then `r = 1`.
`r - 1` is 0 while measuring, 1 when the weight is stable, and 2 when done.
The impedance is 0 when none was measured. There is no checksum.

## 3. "BM02"

**Recognition:**
- `mfr[0]` is `0xAC` (or `0xCA`) and `mfr[1]` is 0 or 1 (1 = has a thermometer).
- The advert lists service UUID `FFB0`, `FEB0` or `C0FF`.
- At least 20 bytes.

`F = mfr[12..19]` is a frame: `F[1]` flag, `F[2..5] = d0..d3`, `F[6]` cmd,
`F[7] = (F[2]+…+F[6]) & 0xFF`.

| cmd | meaning |
|---|---|
| `0xCA` / `0xCE` | stable / live weight, `BE16(d0,d1) / 10` kg; temperature as above when `F[1] == 3` |
| `0xAA` / `0xAE` | stable / live weight, `BE16(d0,d1) / 100` kg |
| `0xAB` | weight `BE16(d0,d1) / 100` kg, impedance `BE16(d2,d3)` |
| `0xCB`, `d0 = 0xFD` | `d1 == 1`: impedance `BE16(d2,d3)`; `d1 == 0xFF`: finished without impedance |
| `0xCB`, `d0 = 0xFE` | one value the scale calculated: index `d1`, value `BE16(d2,d3)`; `d1 == 0xFC` ends the list |

Value indexes:
- 0 weight /10
- 1 BMI /10
- 2 body fat % /10
- 3 subcutaneous fat /10
- 4 visceral fat
- 5 muscle /10
- 6 BMR
- 7 bone /10
- 8 water /10
- 9 body age
- 10 protein /10

## Body-fat formulas

Scales name a formula WLA01–WLA44 (there is no WLA21). Only **WLA03** is
implemented. Readings that ask for another formula, or none, are stored with
their impedance and shown as weight and BMI only.
