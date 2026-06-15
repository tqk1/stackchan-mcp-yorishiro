# Security Policy

Thanks for helping keep `stackchan-mcp-yorishiro` users safe.

This repository is a personal hard fork maintained on a best-effort basis. There
is no dedicated security team and no service-level guarantee on response time,
but security reports are taken seriously and triaged as soon as is practical.

## Supported versions

Only the latest `develop` branch is actively maintained. Fixes land there first;
older tags and release archives are not patched retroactively. If you are running
an older build, please reproduce against the current `develop` head before
reporting.

## Reporting a vulnerability

Please **do not** open a public issue, discussion, or pull request for a
suspected vulnerability. Public reports expose other users before a fix is
available.

Instead, use GitHub's **Private Vulnerability Reporting**:

1. Go to the **Security** tab of this repository.
2. Click **Report a vulnerability** to open a private security advisory.
3. Include a description, affected component (`gateway/` or `firmware/`),
   reproduction steps, and impact.

This keeps the report private between you and the maintainer until a fix is
ready to disclose. If Private Vulnerability Reporting is unavailable for any
reason, open a minimal public issue that says only that you have a security
report to share (with no details) and wait to be contacted.

## Scope

- **`gateway/`** — Python MCP gateway (stdio MCP server, WebSocket MCP client,
  HTTP capture server).
- **`firmware/`** — ESP32 firmware for the StackChan board.

The firmware is a fork that includes code derived from
[78/xiaozhi-esp32](https://github.com/78/xiaozhi-esp32) and
[kisaragi-mochi/stackchan-mcp](https://github.com/kisaragi-mochi/stackchan-mcp).
A vulnerability inherited from upstream likely affects those projects too; please
consider reporting it to the relevant upstream as well so the fix can propagate.

## After you report

You can expect an acknowledgement and, where a fix is warranted, a private
advisory and patch on `develop`. Credit is offered to reporters who want it.
Thank you for reporting responsibly.
