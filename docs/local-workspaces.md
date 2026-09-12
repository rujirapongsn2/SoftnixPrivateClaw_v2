# Local Workspace (phase 1)

## Softnix Local Agent desktop preview

### Folder selection in 2.2

Install Softnix Local Agent from **Settings → Client & Extention**. Move the app to Applications and open it once.

In Chat choose **Work in a folder → Open local folder…**. This opens the native folder picker directly. Its **Allow Read & Write** button connects the selected folder with file creation enabled, without separate consent or success dialogs. Browser external-app and macOS protected-folder prompts may still appear. Existing files remain protected from overwrite.

The web app automatically selects the connected folder for the initiating chat. Pairing completion is owner-scoped and expires after ten minutes. Selecting an older read-only connection opens the picker to reconnect with write access; choosing the same folder preserves its workspace ID. The trash button beside each folder removes its connection and clears chat bindings without deleting local files.

The per-user LaunchAgent is `ai.softnix.local-agent`. It starts at login, restarts after a crash, and retries network failures. It does not run while the Mac is asleep, shut down, or the user is logged out. macOS may ask for permission to access protected folders. A revoked folder stops polling until paired again. Adding another folder preserves previous folder connections. For an old Terminal agent, stop that process and pair each folder again in the app; legacy credentials are not automatically migrated.

Open the app and choose **Restart service**, or use:

```sh
~/.local/bin/softnix-local-agent status
~/.local/bin/softnix-local-agent restart
~/.local/bin/softnix-local-agent stop
~/.local/bin/softnix-local-agent start
```

The binary is named `softnix-local-agent`. Credentials live in `~/Library/Application Support/Softnix Local Agent` with owner-only permissions; the service definition is `~/Library/LaunchAgents/ai.softnix.local-agent.plist`. Keep the app in its installation location. A project folder containing the credential directory is rejected.

Distribution is currently an **ad-hoc signed developer preview**, not an Apple-notarized public release. Gatekeeper may block an internet-downloaded app. Do not disable Gatekeeper; release distribution requires a Developer ID Application certificate and Apple notarization. Only the Apple silicon archive is built here; Intel and Windows require separate builds and testing.

Build source: `desktop/local-agent/build.sh` and `desktop/local-agent/main.swift`. Use a portable Python build (the Homebrew runtime on the development machine requires macOS 26), PyInstaller 6.19.0, and certifi 2026.7.22. The script targets macOS 12 for the Swift launcher. Set `SOFTNIX_PYINSTALLER` and `SOFTNIX_AGENT_BUILD_DIR` to explicit build-tool and scratch paths; the result is `dist/local-agent/softnix-local-agent-macos-arm64.zip`. For public release, sign the embedded binary and app with hardened runtime, notarize and staple the app, then package and verify on a clean Mac. Merely setting a certificate name in the build script does not perform notarization.

Bot Mode can access an explicitly paired folder on a user's Mac or Linux computer. The agent makes outbound HTTPS requests; no inbound port or local web server is needed. Local files requested by a bot are transmitted to the Sbot server and may be processed by its configured model provider.

## Connect

1. Open Bot Mode and choose **Connect local folder** beside Workspace in the chat composer.
2. Download Local Agent. Use Python 3.9 or later on macOS/Linux.
3. Run `python3 sbot_local_agent.py --server https://claw2.softnix.ai --folder "/path/to/folder"`. Omit `--folder` to use the native folder picker where Tk is installed.
4. Paste the single-use pairing code into Terminal within ten minutes.
5. Keep the process running and select the connected folder in the Workspace menu.

Read-only is the default. Add `--write` during initial pairing to permit creation of new output files. Existing files cannot be overwritten. The default credential file is `~/.sbot-local-agent.json`; keep it private and outside the shared folder. To pair another folder or change permissions, disconnect the old pairing and use a different `--state /private/path/agent.json` file. Existing state is bound to the original server, folder, and permissions.

Example request: “Use the quotation template in this workspace to prepare a quotation for these items, publish the completed document, and save a new copy as quotation-2026-09-08.docx.”

## Data flow and boundaries

- Selection is stored per Sbot session. PrivateClaw sessions are not accepted by this API.
- `local_workspace` lists or reads the selected folder, imports files to the cloud workspace, and exports cloud files to new local filenames. Bots with explicit tool allowlists must be allowed to use this tool.
- Office editing runs on an imported cloud copy using existing document tools. Imported files are registered as downloadable chat artifacts; the bot must publish its completed output using the existing artifact tool.
- Individual transfers are limited to 8 MB; text reads return at most 50,000 characters. Parent folders for exports must already exist.
- The agent rejects absolute paths, parent traversal, and symlinks. It never executes shell commands. Pairing tokens are stored hashed on the server; disconnect revokes access.
- Agent shutdown, computer sleep, or network loss makes the folder unavailable. `list`, `read`, and `import` fail immediately; only outbound writes are queued. After an interrupted write, inspect the output before retrying with a new name.

## Offline folders and queued deliveries

The Workspace menu marks an unreachable folder **Offline** and says what that means for new files. Writes produced while a folder is offline are queued on the server and delivered automatically once the Local Agent polls again. A queued file is *not* saved to the computer yet, and the bot is instructed to report it as pending rather than delivered.

Queued deliveries are re-authorized at send time, not at queue time. A delivery is dropped if the folder was disconnected, downgraded to read-only, blocked by guardrails, or if the cloud file changed or was removed after queueing — the stored SHA-256 is compared before any bytes leave the server. Delivery compares the destination's existing contents first, so a retry never duplicates a file and never overwrites a different one. Background deliveries yield to any queued or running interactive request, so they cannot delay a live turn.

The queue is deliberately bounded: 50 pending files per folder, 5 delivery attempts, and a 7-day expiry, after which a delivery is marked failed and shown in the menu. Retries back off exponentially (one minute, doubling to thirty), so a single bad minute cannot spend the whole attempt budget; a folder that merely went offline again does not consume an attempt at all. A delivery interrupted by a server crash is re-queued at startup, and one abandoned by a failed worker is reclaimed after five minutes. Pending and failed entries can be cancelled from the Workspace menu; disconnecting a folder discards its queue.

## Current limits

This phase does not provide local Docker, terminal execution, directory synchronization, or Windows support. Deferred delivery covers outbound writes only — a read issued while the folder was offline is not replayed later. Specialist/mission workflows that create their own tool registries still need separate Local Workspace integration; direct chat access does not imply every autonomous team path supports it. File Preview uses the existing format-specific preview implementation; unsupported formats remain downloadable.

Validation: `pytest tests/test_local_workspaces.py tests/test_sbot_mode.py` covers pairing, ownership, revocation, isolation, Unicode filenames, file delivery, and read-only/no-overwrite enforcement. `npm run build -- --emptyOutDir false` validates the frontend while retaining assets used by already-open browser tabs.
