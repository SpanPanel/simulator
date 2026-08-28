# The emulator's certificate authority

`ca.crt` and `ca.key` are a fixed, deliberately public certificate authority, committed on
purpose. **This is not a leaked secret.** It is test material, in the tradition of Debian's
`ssl-cert-snakeoil` key, and secret scanners flagging it should be allowlisted rather than
obeyed.

SHA-256 of the certificate's DER bytes — the value the Home Assistant integration pins,
reports under `panel_ca` in diagnostics, and displays in a certificate-authority-changed
repair:

```
3cf8c14a78900b8736870c95adcc931cdcb3a51bc3029c96efafd0a4cb790d97
```

## Why it is fixed rather than generated

The simulator emulates SPAN firmware before r202633 and panelbench emulates r202633 and
later, so stopping one and starting the other rehearses a firmware upgrade on a single
panel. A firmware upgrade does not rotate a panel's certificate authority, and a consumer
that pins the authority is right to treat a change as worth stopping for. When each
emulator minted its own CA the swap looked like a panel substitution, which is the one
thing the rehearsal must not simulate.

Both repositories ship these bytes identically. They are the only shared state between two
otherwise decoupled projects, and nothing but this directory couples them: neither imports
the other, and the simulator's eventual archival leaves panelbench unaffected.

## What the public key does and does not cost

Anyone holding `ca.key` can mint a certificate that an integration entry pinned to *this*
authority will trust, and so can impersonate an emulated panel or read its traffic. What
that buys is control of synthetic circuits on an entry someone created for testing, and
the broker credentials it would expose are already public constants in `run.sh`.

It buys nothing at all against a real panel. A real panel mints its own authority in
firmware, the integration pins per config entry with no shared trust store, and this key
signs nothing that chains to it. A real panel's entry reporting the fingerprint above
would be conclusive evidence of tampering — a check the generated-CA arrangement could
never offer.

## Validity

Valid from 2026-01-01 to 2126-01-01. A static CA's expiry is a same-day, fleet-wide event
for every install at once, and no replacement can ship once the simulator is archived, so
the window is set far past the point where either emulator could still be in use.

The server certificate signed by it is *not* static: each install mints its own leaf and
key, because a leaf has to name that install's own address and hostname in its SAN.
