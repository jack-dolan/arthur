# ADR 0002: Schlage Lock Integration via Seam

## Status
Accepted

## Context
The system must programmatically create and delete time-bound access codes on a Schlage Encode Smart WiFi Deadbolt (BE489WB2). Two options were evaluated:

- **pyschlage**: community-maintained Python library that reverse-engineers Schlage's cloud API. Requires storing Schlage account credentials on the server. Time-bound code support unconfirmed in docs. No official backing.
- **Seam**: unified smart device API platform with official Schlage Encode support. Time-bound access codes are a first-class feature. Includes webhooks. Free tier covers up to 3 devices with full API access.

## Decision
Use **Seam** for lock access code management.

## Consequences
- Free tier (≤3 devices) covers current and near-term portfolio size at zero cost.
- Seam's multi-brand abstraction future-proofs the system if additional properties use different lock hardware.
- Adds Seam as an external dependency; if Seam shuts down or changes pricing, migration is required.
- Avoids storing raw Schlage credentials on the server (Seam uses OAuth-style device connection).
