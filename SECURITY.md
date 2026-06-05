# Security Policy

## Supported Versions

StemDeck is in active alpha. Only the latest release receives security fixes -
there are no long-term-support branches yet. Please update to the newest
release before reporting an issue.

| Version         | Supported |
| --------------- | --------- |
| Latest release  | Yes       |
| Any older build | No        |

## Reporting a Vulnerability

Please report security issues privately, not in a public issue. Open the
repository's **Security** tab and click **Report a vulnerability** (GitHub
Private Vulnerability Reporting). This keeps the details private until a fix is
available.

Include where you can:

- Affected version and operating system
- Steps to reproduce
- Impact (what an attacker could do)
- Any relevant logs or proof of concept

What to expect:

- Acknowledgement within about 5 business days (best-effort; small team).
- We confirm the report, assess severity, and keep you updated.
- Fixes ship in the next release. We credit reporters unless you prefer not to
  be named.

## Scope and threat model

StemDeck is local-first and single-user by design: it runs on your own machine,
has no authentication, and is same-origin only. Reports that it "has no login"
or "no per-user access control" describe intended behavior, not vulnerabilities.

We are most interested in reports about:

- Malicious media files or URLs (SSRF, command or argument injection)
- Cross-site scripting (XSS) or Content-Security-Policy bypass in the desktop
  webview
- Path traversal in the backend file APIs
- Integrity of downloaded binaries (FFmpeg, the runtime pack)

## Good-faith research

We will not pursue action against good-faith security research that respects
user privacy and avoids data destruction or service disruption. Thank you for
helping keep StemDeck users safe.
