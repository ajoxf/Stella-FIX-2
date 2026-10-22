# Hosting FIX-Trader on another machine

## What is live today

The app can connect its TT FIX sessions and consume real market data. The
algorithm's statistics, signals, and risk checks are present, and its orders
go to TT over Order Routing (`AlgoOrderRouter`) — **TT UAT only**. Every
restart comes back on PAPER; LIVE is armed from the Algo desk taskbar and
confirmed every time. TT is asked for the account's positions at each logon;
where it does not answer, arming LIVE is the operator's confirmation that the
book's own fills are the record. The adapter refuses anything but UAT FIX
4.2: do not point this build at a production account.

The launcher now selects FIX mode by default. `--simulated` is an explicit
development option; it must not be used to validate live connectivity or live
execution. Starting FIX mode does not itself enable or validate live algo
trading.

## Prepare the host

1. Install Python 3.9 or newer and the FIX-Trader source on the target Windows
   machine. Do not copy `.venv`, `__pycache__`, `.pytest_cache`, or old runtime
   databases as application source.
2. In the project directory, create an isolated environment and install
   dependencies:

   ```powershell
   py -3 -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install --upgrade pip
   python -m pip install -r requirements.txt
   python -m pytest tests/ -q
   ```

3. Copy `.env.example` to `.env`. Enter venue passwords on the target machine
   through the Exchanges page or a protected secret-transfer method. Do not
   paste `.env` into chat, email, source control, or an ordinary deployment
   archive. The app's configured environment-variable names are shown in
   Exchanges; keep those names and values in `.env` only.
4. Configure the correct UAT venue, FIX endpoints, session identifiers,
   contracts, and market-data identity in the UI. Verify the venue's network
   allowlist/VPN and Windows firewall from that machine. Keep UAT and PROD
   separate.
5. Start with `python start.py --fix --no-browser` and open the displayed local
   UI address from the host. Verify both FIX session states and that real bid
   and ask updates arrive. Do not enable the algo for live orders with this
   build; automated FIX execution and live position/order reconciliation are
   still unfinished.

## Transfer a real `.env` securely

The preferred setup is to create `.env` directly on the target machine and
enter its values there. If transferring is necessary, use an approved encrypted
channel and a restricted account. For a Windows-to-Windows host reachable by
OpenSSH, copy it directly without displaying its contents:

```powershell
scp .env USER@HOST:/path/to/FIX-DEV/.env
```

Replace `USER`, `HOST`, and the destination with the target machine's details.
Restrict access to the destination file to the service account that runs the
app, and remove any temporary transfer copy after verifying the target setup.
Never include `.env` in a zip or source-control commit.

## Runtime records and cleanup

Keep `runtime/*.db` and `logs/fix/*.jsonl` when migrating an existing active
account: databases hold the local book/order history and FIX JSONL files are
audit evidence. Copy them only while the app is stopped, and back them up
before migration. Do not delete them as ordinary logs. `logs/*.log` files,
Python bytecode caches, and `.pytest_cache` are disposable diagnostics/cache;
the app can recreate them. Keep the target host's runtime directory private
and back it up according to the desk's record-retention policy.
