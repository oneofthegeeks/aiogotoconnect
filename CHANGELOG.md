# Changelog

| Version | Date | Notes |
|---|---|---|
| v0.4.0 | 2026-10-29 | New `aiogotoconnect.sms` (optional `[sms]` extra): `create_goto_sms(...)`, a Python port of `@oneofthegeeks/goto-sms` as a framework-free ASGI app with the guided setup page (v0.2.1), encrypted credential storage, `send()`, inbound via webhook or polling, STOP/START/HELP opt-outs, and the same HTTP contract and C1-C25 behaviours (see `docs/conformance.md`). Existing modules are unchanged. |
| v0.3.1 | 2026-10-18 | Call-quality polish from the first live G.722 calls: the preferred codec is pre-encoded while the phone rings (no more dead air after pickup), and 0.3 s of silence is appended after the message so the BYE no longer clips the last word in the far-end jitter buffer. |
| v0.3.0 | 2026-10-18 | G.722 wideband (pure-Python encoder, locked to an ffmpeg golden vector) and SDP codec negotiation: the INVITE offers several codecs and the payload is encoded with the one GoTo answers. **Breaking:** `call_and_play(codec=...)` is now `call_and_play(codecs=(...))`, default offer `("g722", "pcma")`; PCMU is `codecs=("pcmu",)`. |
| v0.2.0 | 2026-10-18 | Renamed package gotoconnect → aiogotoconnect (unofficial-client naming convention); no functional changes. |
| v0.1.0 | 2026-10-17 | First release: async OAuth/token layer, Messaging v2 SMS, SIP voice calls (REGISTER + digest INVITE, PCMA/PCMU RTP, UDP/TLS, retransmits, Route sets, cancel-safe hangup). 110 tests vs a strict fake GoTo server; live-smoked against real GoTo (answered call, remote hangup, voicemail delivery, confirmed BYEs). Known deferrals: CANCEL not retransmitted; no GoTo SIP-TLS endpoint found for these devices. |
