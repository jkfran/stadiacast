# certs/

Put the certificate chain the receiver serves to the controller here:

| file            | what                                                           |
|-----------------|----------------------------------------------------------------|
| `fullchain.pem` | leaf + intermediate, issued by Google Trust Services (GTS)     |
| `privkey.pem`   | its private key (keep it `chmod 600`)                          |

Everything in this directory except this README is ignored by git. **Never commit a
private key**, not even to a private repository.

The controller validates the chain against its built-in GTS roots and does not check the
hostname or the validity dates (confirmed on a real controller), so a GTS certificate for any
domain you own works for every Google name the receiver stands in for and never needs
renewing. [docs/setup.md](../docs/setup.md#certificate) explains how to get one for free with
Google's public ACME endpoint.

For the tests and the fake controller a self-signed pair is generated on the fly
(`--self-signed`); the real controller rejects it with `unknown_ca`.
