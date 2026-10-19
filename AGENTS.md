# Maintenance instructions

- Maintain `original` and `network` as separate variants. Do not copy changes across variants without checking their different configuration, history and GPU behavior.
- Keep edits scoped to the requested behavior. Use paths relative to the repository and `sys.executable` for Python subprocesses.
- Never commit user settings, real process lists, real hardware/network samples, screenshots, logs, crash dumps, credentials, personal directories or old binaries. Use fabricated fixtures. Inspect new media and metadata before adding it.
- Before every commit, review the staged file list and run `python tools/check_privacy.py` and `python tools/check_syntax.py`. Run the relevant existing functional checks too. Use a GitHub noreply author email.
- Windows UI checks require a desktop session. Keep settings isolated, avoid changing autostart, and never stop an existing user application as part of a test.
- Add no telemetry, credential storage, automatic upload or external service calls without a specific user request.
- Preserve real-data semantics: unknown sensors stay unknown; unavailable samples must not be fabricated. Keep the low-overhead idle path, Tk image lifetimes and native callback cleanup intact.
- Summarize the behavior changed, validation performed and any checks that could not run. Link a created pull request to the current Codex chat using the available attachment tool.
