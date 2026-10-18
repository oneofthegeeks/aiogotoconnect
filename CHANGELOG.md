# Changelog

| Version | Date | Notes |
|---|---|---|
| v0.2.0 | 2026-10-18 | Renamed package gotoconnect → aiogotoconnect (unofficial-client naming convention); no functional changes. |
| v0.1.0 | 2026-10-17 | First release: async OAuth/token layer, Messaging v2 SMS, SIP voice calls (REGISTER + digest INVITE, PCMA/PCMU RTP, UDP/TLS, retransmits, Route sets, cancel-safe hangup). 110 tests vs a strict fake GoTo server; live-smoked against real GoTo (answered call, remote hangup, voicemail delivery, confirmed BYEs). Known deferrals: CANCEL not retransmitted; no GoTo SIP-TLS endpoint found for these devices. |
