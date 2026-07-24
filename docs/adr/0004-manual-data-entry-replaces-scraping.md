# ADR 0004: Manual Data Entry Replaces Scraping for Missing Booking Fields

## Status
Accepted — supersedes ADR 0001 (booking data source section)

## Context
Two booking fields are not included in platform confirmation emails and must be obtained another way:
- Airbnb: guest phone number and email address
- VRBO: guest email address

ADR 0001 specified authenticated headless browser scraping of the Airbnb and VRBO booking pages to retrieve these fields. Airbnb guest email was additionally handled by parsing forwarded guest replies via the Claude API.

Both Airbnb and VRBO explicitly prohibit automated scraping in their terms of service. Running scraping automation against either platform risks account suspension — for Airbnb, the owner's personal co-host account; for VRBO, the co-host's account. Losing either account would disrupt the entire rental operation, not just this automation system. The risk is disproportionate to the benefit, given that the missing fields are a small, predictable set that an owner already looks up manually for each booking.

## Decision
Drop scraping entirely. Replace with manual data entry via the dashboard:
- On booking detection, the system immediately sends an alert email to the owners with a deep link to the booking's detail page.
- The owners retrieve the missing fields from the platform and enter them via the dashboard form.
- Reminder alerts fire at 7 days and 4 days before check-in if required fields are still empty.
- Downstream tasks (DocuSign send, access code creation) are blocked until their required fields are present.

Hostex (or equivalent channel manager API) remains the documented fallback if manual entry proves operationally unworkable, but is deferred indefinitely.

## Consequences
- No scraping maintenance burden. No account-ban risk.
- Each booking requires a small amount of manual work (opening the platform page, copying 1–2 fields). Acceptable given the booking volume.
- The dashboard must be built before the task integrations (Phase 3), rather than after (formerly Phase 5), because data entry is a prerequisite for DocuSign and access code creation.
- The Claude API guest-reply parsing flow is removed entirely. Simpler system, one fewer external dependency.
