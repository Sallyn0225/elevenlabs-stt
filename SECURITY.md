# Security Policy

## Reporting a Vulnerability

This tool handles ElevenLabs login tokens and (optionally) a self-hosted
temp-email backend. If you find a security issue — e.g. credential leakage,
token mishandling, or injection — **do not open a public issue**.

Use GitHub's private vulnerability reporting instead: **Security** tab →
**Report a vulnerability**. (Maintainer email also welcome if you have it.)

Please include:

- A description of the issue and its impact
- Steps to reproduce / a minimal proof of concept
- Affected commit hash

Expect a response within a few days. Please do not disclose the issue
publicly until it has been addressed or a fix window is agreed.

## Scope

In scope: `stt.py`, `web.py`, `audio_split.py`, and the WebUI (`webui.html`).

Out of scope: this project replays ElevenLabs' own web internal API. Abuse
of ElevenLabs' service, ToS violations, or account-farming are **not**
security vulnerabilities and will not be treated as such.

## Supported Versions

Only the latest `main` branch is supported.
