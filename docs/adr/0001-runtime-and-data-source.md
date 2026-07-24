# ADR 0001: Runtime Environment and Booking Data Source

## Status
Partially superseded by ADR 0004 (booking data source section only — runtime decision stands)

## Context
The system needs a reliable runtime and a way to receive booking data from Airbnb and VRBO. Direct platform APIs require approval and are restricted for individual hosts. A channel manager (name unknown) is already in use.

## Decision
- **Runtime**: Existing VPS (the host running this Claude session). Python service with cron-based scheduling and a lightweight database for booking state.
- **Primary data source**: Email polling — Airbnb/VRBO booking confirmation emails are auto-forwarded to a dedicated booking feed Gmail account. VRBO emails contain name, dates, phone, and reservation ID; email address requires an authenticated scrape of the VRBO booking page (co-host account). Airbnb emails contain name and dates only; phone number requires an authenticated scrape of the Airbnb booking page (owner's co-host account); email address is extracted from forwarded guest replies using the Claude API.
- **Fallback data source**: Hostex — to be pursued if scraping of either platform proves too brittle.

## Consequences
- Email polling introduces a small lag (minutes) between booking and automation trigger — acceptable for this workflow.
- Scraping Airbnb/VRBO authenticated pages carries maintenance risk (layout changes, session expiry, bot detection). Hostex eliminates this risk if it exposes the needed fields.
- The service Gmail account becomes a critical dependency; forwarding rules must be configured in the owner's personal Gmail.
