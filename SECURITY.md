# Security policy

## Supported versions

Only the latest commit on the default branch is supported.

## Reporting a vulnerability

Please do **not** open a public issue. Use GitHub's private vulnerability
reporting ("Security" tab → "Report a vulnerability") on
`Nova20004/self-service`. Include the affected component, reproduction steps
and impact. You should get an acknowledgement within 7 days; fixes are
coordinated with the reporter before disclosure.

## Scope and design

This project is meant for local and internal use on loopback; it is not built to
be exposed to the internet. The threat model, accepted residual risks and the
limits of the Docker socket proxy are documented in
[docs/THREAT-MODEL.md](docs/THREAT-MODEL.md). Accepted scanner findings are
listed with a justification and an expiry date in `.trivyignore.yaml`.

## Handling secrets

`gmake env` generates all tokens and secrets locally into a gitignored `.env`
(mode 600). Never commit `.env`; `.env.example` contains placeholders only. To
rotate, stop the stack, edit or delete `.env`, run `gmake env` and `gmake up`.
