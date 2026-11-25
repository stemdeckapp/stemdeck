# Privacy notice: crash reports

This notice covers the one thing StemDeck can send about itself: crash reports from the
desktop app. They are **off until you turn them on**, and they stay off unless you do.

Everything else StemDeck does with your music stays on your machine. Your audio, stems,
recordings and library are never uploaded. Looking up song details and lyrics is a separate
feature, described in the [README](../README.md#disclaimer).

## Who is responsible

TODO-MAINTAINER: name of the person responsible for this data (the controller), a postal
address, and an email address for privacy requests. Do not publish this notice, or ship a
build that links to it, until this is filled in.

StemDeck is made and run by one person. "The developer" below means that person.

## What crash reports are for

One purpose only: to find and fix crashes and errors in StemDeck. Reports are not used for
anything else: not for analytics, not for advertising, not to profile anyone, and not to make
any decision about you.

## When anything is sent

- Only in the **desktop app** (Windows, macOS, Linux), and only in the official release builds
  from the StemDeck releases page. They carry the address reports go to, added when they are
  built. A desktop app built from the source code has no such address, so crash reports do
  not exist in it: Settings, Privacy says "Crash reports are not available in this build", the
  switch cannot be turned on, and nothing is ever sent.
- Only after you turn on **Settings, Privacy, Send crash reports**. The switch is off on every
  new install. There is no pop-up asking you, no pre-ticked box, and no feature that needs it.
- Only when the app crashes, or the app window meets an error it did not handle.
- At most 10 reports a minute, and the same error once per run.

The Docker image, the Unraid template, a server, and running StemDeck from source in a browser
never send crash reports. The code that sends them exists only in the desktop app.

## What a report holds

**Nothing personal is sent.** A report never holds your name, email address, file or song names, folder paths, or any ID that points to you. Personal details are removed on your computer, before anything is sent, and the Settings screen shows the exact report.

- The error message, and where in StemDeck's code it happened: the names of the functions
  that were running, with their line and column numbers, and whether each is StemDeck's own
  code. No file name and no path of any kind, not even of StemDeck's own source files. Before
  anything is sent, the app takes out of the message folder paths, file and song names, web
  addresses and their query strings, email addresses, IP addresses, long ID numbers, and your
  user name, home folder and computer name.
- The StemDeck version, and whether the build is a release or a test build.
- The operating system (`windows`, `macos` or `linux`) and the processor type (for example
  `x86_64`). Not the operating system version.
- How stems are separated on this machine: `cuda`, `mps` or `cpu`. Never the graphics card's
  name or its driver version. (`cuda` covers NVIDIA cards and AMD cards on Linux.)
- Whether the error came from the app window (`web`) or from the part of the app that starts
  it (`shell`).
- A random report ID (`event_id`) and the time of the report. The ID is new for every report
  and is not linked to you, to your machine, or to any other report.

### Example report

Settings, Privacy shows an **Example report** before anything is turned on. It is made-up
data, not from your computer: a fake error that names a fake home folder, a fake song file, a
fake email address and a fake web address, put through the same scrubbing a real report goes
through. Its ID is all zeros and its time is fixed at 2026-01-01 00:00 UTC, so it is plainly
an example. Making it sends nothing. In the app, the `contexts`, `tags` and `release` lines
show your own machine's values; below they are from a test build. The fake input was:

```text
Could not open C:\Users\example-user\Music\Example Artist - Example Song.mp3 for example@example.com (from https://example.com/song?token=example-token)
```

What a report made from it holds, exactly:

```json
{
  "contexts": {
    "device": {
      "arch": "x86_64",
      "type": "device"
    },
    "os": {
      "name": "windows",
      "type": "os"
    }
  },
  "environment": "desktop",
  "event_id": "00000000000000000000000000000000",
  "exception": {
    "values": [
      {
        "mechanism": {
          "handled": false,
          "type": "onerror"
        },
        "stacktrace": {
          "frames": [
            {
              "colno": 1,
              "in_app": false,
              "lineno": 1
            },
            {
              "colno": 9,
              "function": "openSong",
              "in_app": true,
              "lineno": 120
            }
          ]
        },
        "type": "TypeError",
        "value": "Could not open <path> <email> (from <url>)"
      }
    ]
  },
  "platform": "native",
  "release": "stemdeck@1.2.3",
  "sdk": {
    "name": "sentry.rust",
    "version": "0.49.6"
  },
  "tags": {
    "component": "web",
    "gpu": "cuda",
    "os": "windows"
  },
  "timestamp": 1767225600
}
```

The word "for" went with the path: a path is taken to run on until a clear separator, because
folder and song names contain spaces. A report may say less than it could; it never says more.
The frames hold a function name (the first one, outside StemDeck's code, has none), a line, a
column and whether the code is StemDeck's own. No frame holds a file name or a path.

## What is never sent

- Your name, user name, email address, computer name, or device name.
- Any install ID, user ID or other identifier that could link one report to another, or to you.
- Your songs, stems, recordings, lyrics, or the name of any file or folder.
- What you click, play or search for. There is no usage tracking and no history of actions.
- Logs, session data, performance data, or the list of software on your machine.

**Your IP address.** It is never put in a report. Like every internet connection, the report
server sees the address a report comes from while it receives it. The Sentry project is set
not to store it (see "Processor" below).

You can see exactly what was sent: Settings, Privacy, **Last report** shows the most recent
report word for word. It is kept in memory only and is gone when StemDeck closes.

## Legal basis

Your consent (EU GDPR Article 6(1)(a)). Because a report is sent from your device, the
consent also covers storing and reading information on your device under the ePrivacy
Directive, Article 5(3). Turning the switch on is the consent; it is recorded on your machine
together with the date and the version of the text you agreed to. If that text changes what is
collected, the version changes and the switch turns off until you turn it on again.

## How to withdraw

Turn off **Settings, Privacy, Send crash reports**. It takes effect at once, without a
restart: the app stops sending, and reports that were waiting to be sent are dropped. A report
that was already on its way at that same moment cannot be called back. Withdrawing is as easy
as giving consent, and it does not change how any feature works.

"Reset app data" in Settings, Library also removes the consent and turns reports off.

## Deleting reports

A report holds nothing that identifies you, so the developer cannot find your reports by your
name. To have a report deleted:

1. Open Settings, Privacy, Last report, and copy the `event_id`.
2. Send it to the contact under "Who is responsible", asking for deletion.

For an older report, give the date and time of the crash and the StemDeck version instead. The
developer will delete the matching reports and confirm.

## Processor and where data is stored

Reports are sent to and stored by **Sentry** (Functional Software, Inc. d/b/a Sentry), a crash
reporting service, acting for the developer as a processor. The Sentry project uses Sentry's
**EU data region**, so report data is stored in Frankfurt, Germany.

- Sentry's Data Processing Addendum: <https://sentry.io/legal/dpa/>
- Sentry's privacy policy: <https://sentry.io/privacy/>

TODO-MAINTAINER: confirm the DPA is accepted for the `stemdeckapp` organisation, then remove
this line.

## Transfers outside the EU

Report data is stored in the EU region. Sentry is a US company, and its staff or
sub-processors may access data from outside the EU, for example for support. Sentry's DPA
covers such transfers with the EU-U.S. Data Privacy Framework and, as a fallback, the Standard
Contractual Clauses.

TODO-MAINTAINER: verify that this still matches Sentry's current DPA and sub-processor list.

## How long reports are kept

TODO-MAINTAINER: the retention period configured in Sentry (for example "90 days"), stated as
a number of days. Reports are deleted when it ends.

## Who can read reports

Only the developer. Reports are not sold, rented or shared with anyone, and they are not used
for advertising.

## Your rights

Where the GDPR applies, you can ask for access to, correction of, or deletion of data about
you, ask to restrict its use, and object. You can complain to the data protection authority
of the EU country where you live or work. Because reports hold no identifier, the developer
can only find a report you point to, as described under "Deleting reports".

## California (CCPA/CPRA) and other US state laws

StemDeck does not sell or share personal information, as those words are defined in the
California Consumer Privacy Act as amended by the CPRA, and it does not use it for
cross-context behavioural advertising. Reports are used only to fix StemDeck. There are no
financial incentives tied to them.

## Children

StemDeck is not aimed at children. Reports hold no information about anyone's age, and none
is asked for.

## Automated decisions

None. Reports are read by the developer to fix bugs. No decision about any person is made from
them, automated or otherwise.

## Changes to this notice

Changes are published in this file, in the StemDeck repository. A change to what reports hold
also changes the consent version in the app, which turns crash reports off until you turn them
on again.

Version of the consent text this notice describes: 1.
