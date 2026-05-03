# Contributing

Thanks for helping improve `stackchan-mcp`.

This repository contains both the Python MCP gateway and the ESP32 firmware
used by the M5Stack official StackChan kit.

## Development Flow

For most changes, please work on a topic branch:

```bash
git switch main
git pull
git switch -c issue-123-short-description
# edit files
git status
git diff
```

Then run the relevant checks, commit, push the branch, and open a pull request.
Link the related issue in the PR body when there is one.

## Gateway Checks

```bash
cd gateway
uv sync
uv run pytest
uv run ruff check .
```

Add tests under `gateway/tests/` for behavior changes.

## Firmware Checks

Firmware changes do not yet have a full automated hardware test flow. At
minimum, build the StackChan firmware through the board-aware release script:

```bash
cd firmware
docker run --rm -v "$PWD":/project -w /project espressif/idf:v5.5.2 \
  python ./scripts/release.py stackchan
```

Avoid using a plain `idf.py build` as proof that the StackChan target works; it
may build a different board configuration.

## Do Not Commit Local Secrets

Please keep private or local machine state out of commits:

- `.env` files
- tokens, passwords, WiFi credentials
- private LAN IP addresses
- captured photos or user media
- local `firmware/sdkconfig`
- temporary build settings that force a personal gateway URL/token

Use placeholder examples in documentation instead. Firmware developers can put
personal Kconfig overrides in `firmware/sdkconfig.defaults.local`; it is ignored
by git and loaded by the firmware build.

## License Notes

Most of this repository is MIT licensed. The SCServo-lib-derived files under
`firmware/main/boards/stackchan/` are GPL-3.0. Please preserve the existing
license headers and avoid moving GPL-derived code into unrelated MIT-only
areas.
