# Running Cairnward on Windows (WSL2)

Cairnward runs inside WSL2, Windows' Linux virtual machine. Windows chat apps connect to it over
`localhost`. Native Windows has no OS isolation, so Balanced and Strict can't apply there.

**Status: not yet run on real WSL2.** The rules below are tested on plain Linux, using folders as
stand-ins for the Windows drive and `/init`. Follow the [checklist](#check-it-on-your-machine)
before relying on them.

## Install
In PowerShell on Windows, install a Debian WSL2 distribution:

```powershell
wsl --install -d Debian
```

If it already exists, make sure it's WSL2. Cairnward's sandbox refuses WSL1:

```powershell
wsl --list --verbose
```

Then, inside Debian, follow [Getting started](getting-started.md), including Ollama. Run Ollama
inside WSL, not on Windows. WSL's `127.0.0.1` isn't Windows' own unless you turn on
`networkingMode=mirrored`.

## Settings to change
**Memory.** WSL2 gets half of Windows' memory by default, and the default main model needs about
18 GB. To raise it, put this in `%UserProfile%\.wslconfig`, then run `wsl --shutdown`:

```ini
[wsl2]
memory=24GB
```

**Windows programs and drives (recommended).** Sandboxes hide these anyway, but unsandboxed
programs in WSL can use them. If you don't need them from WSL, put this in `/etc/wsl.conf`, then
run `wsl --shutdown`:

```ini
[interop]
enabled=false
appendWindowsPath=false
```

**Disk encryption.** WSL's disk is a file on the Windows drive, so turn on BitLocker or Device
Encryption in Windows. `cairnward doctor` can't see this from inside WSL.

## What's different from Linux
- **Windows drives are hidden from every sandbox**, including the core and plugins. Keep
  projects, papers and Cairnward's own folders in the Linux file system (`~/...`). `cairnward
  serve` refuses to start if its own folders are on a Windows drive, and a run warns if its
  workspace is on one.
- **Starting Windows programs is blocked in every sandbox.** `/init`, which starts them, is hidden,
  as is the socket in `/run/WSL`.
- **Windows programs can reach your Linux files.** Anything running as you on Windows can open
  `\\wsl.localhost\Debian\home\...`, and Linux file permissions don't stop it. Locking still
  protects locked items. This is an accepted risk: see T-28 in the
  [threat model](threat-model.md).
- **Windows programs can reach the gateway and dashboard ports** through WSL's `localhost`
  forwarding. They still need a client key or a sign-in link.
- **Backup and replication folders may be on a Windows drive** (a Syncthing folder on `C:`, say).
  Their contents are encrypted, and those jobs run outside the core's sandbox. File permissions
  don't apply there, and it's slower.
- **`keyring:` references usually fail**, because WSL has no secret service by default. Use `env:`
  references.
- **WSL stops when idle.** Cairnward stops with it, and so do scheduled backups and replication.
  Keep a terminal running `cairnward serve` open while you need them.

## Connecting Windows apps
Point Windows apps at `http://localhost:8484/v1` with a key from `cairnward client add`. Open the
dashboard link it prints in a Windows browser. If a browser doesn't open by itself, copy the link.

## Check it on your machine
Run these inside WSL. Each is something the Linux tests can't show.

1. **The self-test.** `isolation self-test` must report `Windows drives, Windows programs and VM
   sockets ... blocked`. A failure means the isolation isn't holding, so don't rely on it.

   ```bash
   uv run cairnward doctor
   ```

2. **Start the server** with isolation on. The doctor's isolation checks must pass, and the log
   must not say `isolation = on but inactive`.

   ```bash
   uv run cairnward serve
   ```

3. **From Windows:** open `http://localhost:8484/v1/models` with a client key, and open the
   dashboard sign-in link in a Windows browser.
4. **A Windows chat app** gets a reply through the gateway.
5. **An agent run from WSL** can't read your Windows files:

   ```bash
   uv run cairnward run -- sh -c 'ls -A /mnt/c; cmd.exe /c echo reached'
   ```

   Expect an empty listing, and no `reached`.
6. **A workspace on `/mnt/c`** gives the Windows drive warning, and still works.
7. **Replication** between this machine and your Linux one, through a real shared folder.
8. **Measure** gateway overhead and start time. The numbers in the README are from the Linux
   reference machine, and crossing the VM boundary may change them.

## Known gaps
- **VM sockets from the core:** ordinary sandboxes block them, but the core's doesn't, because it
  has to allow Unix sockets (tested in the real sandbox on Linux). Under WSL2 a VM socket can reach
  the Windows side, past the network rules. What's listening there hasn't been checked.
- CI doesn't run WSL2, so a change that breaks it would only show up on a real machine.
