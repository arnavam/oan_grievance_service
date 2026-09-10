"""Version 1 of the public API.

This package is frozen once released: additive changes only. A breaking change opens
`api/v2/` rather than editing anything here. See `oan_grievance_service.api` for the
policy and the reasoning.

Endpoints
---------
    v1.grievance.submit     FSD FR-02 / 4.1   lodge a grievance on any channel
    v1.grievance.track      FSD FR-04         status lookup by ticket number
    v1.grievance.reply      FSD Appendix C    answer a More Info Needed request
    v1.grievance.confirm    FSD FR-06 / UC-03 confirm the resolution
    v1.grievance.reopen     FSD FR-06         reopen with a mandatory reason
    v1.grievance.escalate   FSD FR-07         escalate once the SLA has elapsed
"""

VERSION = "v1"
