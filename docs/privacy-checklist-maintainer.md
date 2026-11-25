# Crash reports: the maintainer's checklist

The code keeps crash reports off by default, asks for consent, and strips personal data
before anything leaves the machine (`desktop/src-tauri/src/telemetry.rs`). That is not the
whole job. The items below are things only the maintainer can do, outside the code. The build
follows the strictest reasonable reading of the GDPR, the ePrivacy Directive and the
CCPA/CPRA, but it is not legal advice, and none of this has been reviewed by a lawyer.

Do every item marked **before release** before shipping a build that contains the Privacy
tab. The Settings text and `docs/privacy.md` make promises that depend on them.

## Before release

- [ ] **Fill in the controller.** Replace every `TODO-MAINTAINER` in `docs/privacy.md`: your
      name (or the name you publish under), a postal address, and an email address for
      privacy requests. Why: the GDPR (Articles 13 and 14) requires the controller's identity
      and contact details in the notice.
- [ ] **Accept Sentry's DPA** for the `stemdeckapp` organisation
      (<https://sentry.io/legal/dpa/>; it is accepted electronically) and keep a copy with
      the date. Why: Article 28 requires a contract with a processor.
- [ ] **Organisation, Security & Privacy:** turn on data scrubbing ("Data Scrubber" and "Use
      Default Scrubbers"). Why: a second line of defence if the app's own scrubber misses a
      pattern.
- [ ] **Turn on "Prevent Storing of IP Addresses"** (organisation and project). Why: the
      Settings text tells users the report server is set not to store their IP address.
      This item makes that sentence true.
- [ ] **Set the data retention** to the shortest period that still lets you fix bugs (for
      example 30 or 90 days, as your plan allows), then write the number of days in
      `docs/privacy.md` under "How long reports are kept". Why: the GDPR's storage
      limitation principle, and the notice must state the period.
- [ ] **Set a rate limit** on the project's client key (DSN), for example a few hundred
      events per hour. Why: a crash loop on many machines should not exhaust the quota or
      store more than needed. The app already sends at most 10 a minute per machine.
- [ ] **Lock down access** to the Sentry organisation: you as the only member, two-factor
      authentication on, no public sharing of issues, no integrations that copy events
      elsewhere (Slack, GitHub issue sync with event bodies, and so on). Why: the notice says
      only the developer can read reports.
- [ ] **Confirm the EU region** is the one in use (`ingest.de.sentry.io` in the DSN, `de.sentry.io`
      in the dashboard). Why: the notice says reports are stored in the EU.
- [ ] **Keep the DSN in the `STEMDECK_SENTRY_DSN` org secret**, visible to the `stemdeck`
      repository only. The three release workflows pass it to the step that builds the app,
      and the shell compiles it in (`option_env!` in `telemetry.rs`). It is never in the
      repository, never in a pull request check, and a build from source has none, so source
      builds never report. Why: the notice says only official release builds can send. A DSN
      only allows sending reports, not reading them; if one leaks, rotate the client key in
      Sentry and update the secret.
- [ ] **Publish the notice.** Merge `docs/privacy.md` so the link in Settings, Privacy
      (`https://github.com/stemdeckapp/stemdeck/blob/main/docs/privacy.md`) works. Why: the
      switch links to it, and consent must be informed.
- [ ] **Lawyer review** of `docs/privacy.md`, the Settings, Privacy text, and this checklist.
      Why: none of this is legal advice.

## Records

- [ ] **Record of processing activities** (GDPR Article 30). One entry is enough: purpose
      (fix crashes), categories of data (scrubbed error messages and stack traces, app
      version, OS name, CPU architecture, GPU class), data subjects (desktop users who opted
      in), recipient (Sentry, processor), transfers (EU storage, US access under DPF and SCCs),
      retention (the number you set), security (scrubbing in the app and in Sentry, access
      control). Small organisations are often exempt, but processing that is not occasional
      may not be; keeping the entry costs little.
- [ ] **Keep the consent design on file**: the consent text version (`CONSENT_VERSION` in
      `telemetry.rs`, `CRASH_CONSENT_VERSION` in `static/js/crashReports.js`) and the text that
      version showed. Why: you may need to show what people agreed to.

## When the code changes

- [ ] **Changed what a report holds, or the Settings text about it?** Bump both consent
      versions together. Every user's switch then reads as off until they turn it on again.
      Update `docs/privacy.md` in the same change.
- [ ] **Added a Sentry feature** (logs, sessions, performance, attachments, user feedback,
      replays)? Stop: that is a new purpose or new data. It needs a new consent version, an
      updated notice, and a new look at this checklist.
- [ ] **Upgraded the `sentry` crate?** Re-run the telemetry tests, and read the new version's
      `ClientOptions` and default integrations. The event is rebuilt from an allow-list, so a
      new field is dropped, but a new transport-level behaviour would not be.

## On a deletion request

1. Ask for the `event_id` from Settings, Privacy, Last report, or the date, time and StemDeck
   version of the crash.
2. In Sentry, find the event (search `id:<event_id>`, or filter by release and time).
3. Delete the event, or the whole issue if it only holds that person's reports.
4. Reply to confirm, within one month (GDPR Article 12(3)).
5. Note the request and the date in your own records, without copying the report itself.

## On an access request

Reports hold no identifier, so you can only find a report the person points to. Explain that,
offer the steps above, and send them the matching event's JSON if they give an `event_id`.

## If something leaks

If a report turns out to contain personal data (a path or a name the scrubber missed):

1. Delete the affected events in Sentry.
2. Add the pattern to `scrub_text` in `telemetry.rs`, with a test, and ship the fix.
3. Decide, with your lawyer, whether it is a personal data breach that must be reported to
   the supervisory authority within 72 hours (GDPR Article 33). Write the decision down
   either way.
