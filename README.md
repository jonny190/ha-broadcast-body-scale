# Broadcast Body Scale for Home Assistant

A Home Assistant custom integration for Bluetooth body composition scales that
broadcast their readings passively, such as the FG260 as sold for the FitDays
app. It reads the scale's Bluetooth advertisements, works out who stepped on,
calculates body composition and exposes the results as sensors per person.

The integration never connects to the scale. It only listens.

## Supported scales

Only the FG260 has been tested with a real scale. The other scale types are
decoded from the protocol the FitDays app uses, but nobody has tried them
yet, so they are marked **untested** during setup and in the device's model.

| Scale | How it is recognised | Status | Body composition |
| --- | --- | --- | --- |
| FG260 / `AAA006` | company ID `0xA0AC` | **Tested** | WLA03, calculated here |
| Other iComon broadcast scales and balance scales | company ID `0x??AC` or `0x??CA` with the broadcast bit set (72 IDs, discovered automatically) | Untested | WLA03 if the scale asks for it, else weight and BMI |
| "BM15" broadcast scales | company ID `0x??BC`, encrypted payload, no service UUIDs | Untested | Weight and BMI |
| "BM02" broadcast scales | company ID `0x00AC`/`0x01AC`, plus service UUID `FFB0`/`FEB0`/`C0FF` | Untested | Values the scale calculates itself, else weight and BMI |

Untested scales: if one works, or doesn't, please open an issue with the
integration's **Download diagnostics** file. It includes the scale's last 50
distinct raw broadcasts (with the scale's address blanked), which is what is
needed to confirm or fix support.

Body fat and the other composition values need the scale's body-fat formula.
Only WLA03 is implemented so far. Scales using another formula (or not saying
which) get weight and BMI only; their impedance is still stored, so results
can be recalculated if more formulas are added.

"A4" iComon scales and anything that needs a Bluetooth connection are not
supported. Protocol notes are in [docs/protocol.md](docs/protocol.md).

## Installation (HACS custom repository)

1. In HACS, open the menu, choose **Custom repositories** and add
   `https://github.com/jonny190/ha-broadcast-body-scale` with category **Integration**.
2. Install **Broadcast Body Scale** and restart Home Assistant.

Requirements: Home Assistant with Python 3.13 or newer (the body composition
maths uses `math.fma`). Home Assistant 2026.x ships Python 3.13+.

## Setup

1. Step on the scale. Home Assistant should discover it through Bluetooth; accept
   the discovery under **Settings > Devices & services**. You can also add
   **Broadcast Body Scale** manually and pick the scale from the list. BM15
   scales are not discovered automatically (their company ID varies), so
   add those manually while stepping on the scale.
2. Setup then guides you through adding people. For each person enter a name,
   height (100 to 250 cm), sex, date of birth, athlete mode, and the notify
   services used to ask who weighed in (for example `notify.mobile_app_phone`;
   at least one is required). After each person choose **Add another person**
   or **Finish setup**. At least one person is required.
3. Later, open the integration's **Configure** (options) menu to change things:
   - **Add person**, **Edit person** and **Remove person**.
   - **Matching settings**: matching tolerance, 0.5 to 20 kg (default 3 kg).

Each person becomes a device with their own sensors.

## How "who weighed in" works

The scale does not say who is standing on it, so the integration decides:

1. If a reading is within the tolerance of exactly one person's last recorded
   weight, it is assigned to that person automatically. That person gets a
   "Saved to <name>" notification with the change since their last weigh-in and
   buttons **It was <someone else>** (up to two people, nearest weight first)
   and **Not me** (puts the reading back and asks everyone). Turn this off with
   **Confirm automatic matches** in the settings.
2. Otherwise (no match, or several people match) an actionable notification
   "Who just weighed in?" is sent to every notify service configured for any
   person. With up to two people it has one button per person plus
   **Guest / discard**. Android shows at most three buttons, so with more
   people it shows the two nearest by weight plus **Someone else…**, which
   opens the scale's device page. The weight is shown in stone and pounds and
   in kilograms. When the reading is resolved the notification is cleared
   again. The first answer wins. Action buttons are supported by the Home
   Assistant companion app.
3. Until resolved, the reading waits in an unassigned queue (at most 50 readings;
   the oldest is dropped first; readings never expire by time). The queue is
   visible on the **Unassigned readings** sensor, whose `readings` attribute
   lists each reading's `id`, `timestamp` and `weight_kg`.
4. You can also resolve the newest reading with the scale's **Assign latest
   reading** select, or any reading with the services below.

A person's very first reading has no last weight to compare against, so it is
always asked about.

## Services

| Service | Fields | Description |
| --- | --- | --- |
| `fg260_scale.assign_reading` | `reading_id`, `person` | Assign a reading to a person (by ID or name); an assigned reading is moved. |
| `fg260_scale.discard_reading` | `reading_id` | Discard a reading, queued or assigned, for example a guest's. |

## Sensors

Per person, enabled by default: **Weight** (kg), **Weight (lb)**,
**Weight (st lb)** (text such as `18 st 1.6 lb`, as the scale shows it in
st:lb mode), **Change since last** (kg and lb, with the previous weight and
time as attributes), **Body fat**, **BMI**, **Last measured**.

Each person also gets a **Height** setting on their device page. Changing it
saves the new height to their profile and recalculates their latest BMI and
body composition from the stored weights and impedances (each person keeps
their last 50 results). Editing a person in **Configure** recalculates the
same way.

Per person, disabled by default (enable them in the entity settings): Impedance,
Water, Muscle mass, Bone mass, Visceral fat, Basal metabolic rate, Body age,
Protein, Subcutaneous fat, Skeletal muscle.

On the scale device: **Unassigned readings**, **Assign latest reading**, and
the diagnostics **Signal strength** (best proxy, with the proxy as the
`source` attribute) and **Last seen**. If weigh-ins go missing, check these:
a scale heard at around −95 dBm or weaker needs a proxy closer to it.

Body composition needs an impedance value, which the scale only sends when you
stand on it barefoot. Without it the body composition sensors keep showing
your last barefoot reading.

## Repairs and diagnostics

If a person's notify service disappears (for example the phone app was
reinstalled), a repair appears under **Settings → System → Repairs**. The
other people are still notified.

**Download diagnostics** on the integration gives the scale's state and recent
readings for bug reports, without names, birth dates, notify services or the
scale's address.

## Event

Every assigned reading fires `fg260_scale_measurement` with these data keys:
`reading_id`, `person_id`, `person_name`, `timestamp`, `weight_kg`, `impedance`, `bmi`,
`body_fat`, `subcutaneous_fat`, `visceral_fat`, `muscle`, `bmr`, `bone`,
`water`, `body_age`, `protein`, `skeletal_muscle`. Composition values are
`null` when no impedance was available.

### Example automation

```yaml
automation:
  - alias: "Weigh-in summary"
    triggers:
      - trigger: event
        event_type: fg260_scale_measurement
    actions:
      - action: notify.mobile_app_phone
        data:
          title: "{{ trigger.event.data.person_name }} weighed in"
          message: >-
            {{ trigger.event.data.weight_kg | round(1) }} kg, BMI
            {{ trigger.event.data.bmi | round(1) }}
            {% if trigger.event.data.body_fat is not none %}
            , body fat {{ trigger.event.data.body_fat | round(1) }}%
            {% endif %}
```

## Bluetooth proxy placement

The scale's signal is weak. Use a Bluetooth adapter or ESPHome Bluetooth proxy
within a few metres of the scale, ideally in the same room. Passive scanning is
enough; the integration makes no connection to the scale.

## Accuracy and provenance

Body composition is an independent reimplementation of the WLA03 algorithm used
by the FitDays app, verified against reference vectors. The integration's
internal domain is still `fg260_scale`, so existing setups keep working. No vendor code or
binaries are included in this repository. If FitDays was configured with a
different per-user algorithm, values can differ from the app.

## Links

- Issues: https://github.com/jonny190/ha-broadcast-body-scale/issues
- Licence: MIT
