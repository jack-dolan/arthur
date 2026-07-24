# Rental Automation — Domain Glossary

## Booking
A confirmed reservation on a listing platform (Airbnb, VRBO) that triggers the automation workflow. Booking details include guest name, phone number, check-in date, checkout date, and (on VRBO only) guest email.

## Check-in
The moment a guest physically arrives at the property (scheduled 4:00 PM on arrival day). Terminal event of the automation scope.

## Checkout
The scheduled departure time (11:00 AM on departure day). No automated handling after this point.

## Scope
Automation covers **Booking → Check-in**, inclusive of all pre-arrival tasks. Post-check-in issues are handled manually via the listing platform's native messaging.

## Manual Data Entry
Some booking fields are not included in confirmation emails and must be entered manually by the owner via the dashboard.

| Platform | Missing field | How to obtain |
|---|---|---|
| Airbnb | Guest phone number | Open the booking page in the Airbnb app or website |
| Airbnb | Guest email address | Ask the guest via Airbnb messaging; paste their reply |
| VRBO | Guest email address | Open the booking details page in the VRBO app or website |

On booking detection, the system immediately sends an alert email to the owners with a deep link to the booking's detail page on the dashboard. The owners enter the missing fields there. If fields remain empty, reminder alerts fire at 7 days and 4 days before check-in (same cadence as DocuSign follow-up reminders). Downstream tasks (DocuSign send, access code creation) are blocked until their required fields are present.

## Listing Platform
A third-party marketplace where the property is listed. Currently: **Airbnb** and **VRBO**. Key behavioral differences per data source:

| Field | Airbnb email | Airbnb website | VRBO email | VRBO website |
|---|---|---|---|---|
| Guest full name | ✓ | ✓ | ✓ | ✓ |
| Check-in / checkout dates | ✓ | ✓ | ✓ | ✓ |
| Confirmation / reservation ID | ✓ | ✓ | ✓ | ✓ |
| Guest phone number | ✗ | ✓ (requires login) | ✓ | ✓ |
| Guest email address | ✗ | ✗ (guest replies to platform message) | ✗ | ✓ (requires login) |

Data availability notes:
- **Airbnb**: phone number and email address are not in the confirmation email and require manual entry by the owner (see Manual Data Entry).
- **VRBO**: phone number is in the confirmation email. Email address is not and requires manual entry by the owner (see Manual Data Entry).

## Beyond Pricing
Dynamic pricing software used by the co-owner to automatically adjust listing prices on Airbnb and VRBO. Not a channel manager — does not sync calendars or provide booking data. Calendar deconfliction between platforms is handled natively via iCal sharing (each platform subscribes to the other's iCal feed).

## Property
A rentable unit managed by the system. Currently one property; designed for multi-property extensibility. Each Property is a configuration object containing:

- **Id**: the key a booking is attributed to. With a single property every booking maps to `properties[0]`; per-property listing matching is a multi-property concern and is not implemented (the `listing_name_pattern` field was removed as dead config in the go-live cleanup)
- **Lock**: Seam device ID (Schlage Encode Smart WiFi preferred across properties)
- **HOA config** (nullable): recipient email, email window rules, DocuSign template ID
- **Cleaner notification adapter**: Google Sheets (property 1) or a pluggable alternative (web form, SMS, etc.) for future properties — the adapter interface is fixed even if the implementation varies

Things shared across all properties: Airbnb co-host account, VRBO account, Seam account, booking feed inbox, alerts inbox.

## Guest Form (HOA Packet Request)
A DocuSign document required by the property's HOA. The guest must sign it before arrival. Once signed, it is emailed to the HOA so they can prepare a physical packet (amenity access tags, etc.) for the cleaner to pick up.

Implementation: Sent via DocuSign eSign API using an existing template (property-specific fields pre-filled; guests complete the remaining fields). Account is DocuSign Standard Annual — API access included. Send quota is finite (annual limit, not yet exceeded but approaching); automated sends will consume from this quota.

## HOA Packet
A physical packet prepared by the HOA containing amenity access materials. The cleaner retrieves it from the HOA office and brings it to the property before the guest arrives.

## HOA Email Window
The constraint governing when the signed Guest Form must be emailed to the HOA:
- **No earlier than** 7 days before check-in (hard rule — never send early).
- **No later than** the last day that still leaves **2 full HOA-open days**
  strictly between the send day and check-in (the HOA's packet lead time). The
  send day itself never counts as lead time, and may itself be a closed day —
  the email just waits in the HOA inbox.
- HOA is open **Monday–Saturday** (closed Sunday).
- Example: Sunday check-in → last open day is Saturday → email must be sent by
  **Thursday at latest** (Friday + Saturday are the two open days of lead time).
- **Late signatures still send** (owner decision 2026-07-22): the "no later
  than" bound is the last *acceptable* day for the scheduled send, not a
  send-blocker. If the guest signs after it, the automation sends immediately
  anyway — late is better than never — and logs a warning (the HOA may need a
  call to expedite the packet).
- All window comparisons use the **US/Eastern calendar date**, never the server
  clock (the production container runs UTC, which is already "tomorrow" from
  8 PM ET).

HOA email details:
- **Recipient**: configured per property in `config.yaml` → `properties[n].hoa.email`
- **Sender**: the public-facing automation email address (not the booking feed inbox). The `From` header carries a personal display name — `{owners.primary_full_name} <alerts address>` — so it reads as a real person (helps HOA-side spam filtering).
- **Subject**: `Guest Registration Form - Arriving {M/D/YY}` (check-in date, no zero-padding, 2-digit year — e.g. `Guest Registration Form - Arriving 7/6/26`)
- **Body**: Time-of-day greeting matched to US Eastern time ("Good morning/afternoon/evening"), then:
  > Attached, please find the registration form for my guest, arriving {M/D/YY}.
  >
  > Thank you,
  > {owners.primary_full_name}

  (`primary_full_name` from `config.yaml`, falling back to `primary_name` if unset)
- **Attachment**: The signed DocuSign PDF retrieved via DocuSign eSign API
- **Deliverability**: sent via the Gmail API from a real Gmail account, so SPF/DKIM/DMARC are aligned by Google automatically. The single most reliable safeguard against the HOA's spam filter is a one-time allow-list: the HOA should add the sender address to their contacts / mark the first message "not spam". Because the HOA is a fixed known recipient, this reliably lands future automated mail in their inbox.

## Cleaner Schedule
A Google Sheet shared with the cleaning team. Each booking adds one row. Sheet name is configured per property in `config.yaml` → `properties[n].cleaner_schedule.sheet_name`. Owned by the cleaning company; shared with the owners.

**Columns (system writes only the starred ones):**
| Column | System Writes? | Default / Format |
|---|---|---|
| Cleaning Scheduled | No | Checkbox (cleaners) |
| ★ Guest | Yes | "{First} {Last}" |
| ★ Check In | Yes | MM/DD/YYYY |
| ★ Check Out | Yes | MM/DD/YYYY |
| ★ Time In | Yes | HH:MM:SS AM/PM — default 4:00:00 PM |
| ★ Time Out | Yes | HH:MM:SS AM/PM — default 11:00:00 AM |
| Cleaning Date | No | Filled by cleaners |
| Packet Cost: Received | No | Filled by cleaners |
| Packet Cost: Balance | No | Filled by cleaners |
| Notes | No | Filled by cleaners |
| Cleaning Complete | No | Checkbox (cleaners) |

**Row insertion rules:**
- Rows are in ascending chronological order by check-in date.
- New rows are inserted at the correct chronological position, never appended blindly.
- A sentinel row ("Do Not Use This Row - Leave This Row At Bottom", styled red) is always the last row. New rows are inserted above it.
- Default check-in/out times (4 PM / 11 AM) are used unless manually overridden after the fact.

## Dashboard
A web UI served from the VPS, behind per-user "Sign in with Google" (OIDC) plus an email allowlist in `config.yaml` → `dashboard.allowed_emails` (fail-closed when empty). V1 is read-only apart from the contact-entry form: it shows active bookings, completed tasks, pending items, and the current state of each booking's workflow.

Post-V1 (on roadmap): full admin UI with task override/trigger capability and **provenance tracking** — each piece of data (guest name, phone number, email address, dates) is annotated with its source (email parse, web scrape, DocuSign webhook, manual entry) so the operator can see exactly how the system assembled its picture of a booking.

## Email Addresses (System-Managed)
Two dedicated Gmail accounts serve distinct roles:
- **Booking Feed Inbox**: Receives auto-forwarded Airbnb/VRBO booking confirmation emails and guest reply emails. This is a system-only input — not monitored by humans.
- **Alerts Inbox**: Receives human-actionable notifications sent to both owners. Each alert includes: the pre-drafted message text to send, the guest's name and stay dates for context, and the platform routing instruction (e.g., "paste this into Airbnb in the conversation with Bob M.").

## Access Code
A time-bound door code programmed into the Schlage smart lock via the Seam API. Derived from the last 4 digits of the guest's phone number. Active from **4:00 PM on check-in day** to **11:00 AM on checkout day**.

## Cancellation Handling
Cancellation emails are sent by both Airbnb and VRBO and are detectable by the system the same way booking confirmations are. On cancellation:

| Component | Action | Who |
|---|---|---|
| DocuSign envelope (if unsigned) | Void the envelope via API | System (automatic) |
| Schlage access code (if created) | Delete via Seam API | System (automatic) |
| HOA email (if already sent) | Alert owners to follow up with HOA manually | System → human |
| Cleaner schedule row | Alert owners to remove the row manually | System → human |

## DocuSign Follow-up Cadence
Triggered when the Guest Form remains unsigned:

| Timing | Action | Mechanism |
|---|---|---|
| 7 days after envelope sent | First reminder to guest | DocuSign built-in reminder (configured on send via API) |
| 7 days before check-in (if still unsigned) | Alert to owners with copy-pasteable platform message | Alerts inbox email |
| 4 days before check-in (if still unsigned) | Urgent alert to call the guest; includes guest phone number | Alerts inbox email |

"Danger zone" = when the unsigned form puts the HOA Email Window at risk. The 4-day alert is also the effective deadline for the HOA packet to be achievable.

## Task Graph (Booking → Check-in)
Ordered by trigger, not strict sequence. Tasks marked **[PLATFORM]** are already automated by Airbnb/VRBO and are outside this system's scope.

| # | Task | Trigger | Dependency |
|---|------|---------|------------|
| P1 | Send initial welcome message (incl. email request for Airbnb) | Immediately on booking | **[PLATFORM]** |
| P2 | Send check-in instructions | ~2 days before check-in | **[PLATFORM]** |
| 1 | Detect booking from forwarded confirmation email | Email received | — |
| 2 | Alert owners with deep link to enter missing fields | Immediately on booking | Booking detected |
| 3 | Send DocuSign Guest Form | Guest email entered | Guest email |
| 4 | Add row to Cleaner Schedule sheet | Immediately on booking | Booking data |
| 5a | Remind owners: missing phone/email not yet entered | 7 days before check-in, then 4 days | Field(s) still empty |
| 5b | Follow up: guest hasn't signed DocuSign | Periodic check | Form sent, still unsigned |
| 6 | Email signed form to HOA | Within HOA Email Window | Form signed |
| 7 | Program Access Code in Schlage | Anytime before check-in | Guest phone number |
