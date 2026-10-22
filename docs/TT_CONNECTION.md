# TT UAT connection

The `FixGateway` now uses the native Python FIX 4.2 connection code extracted
from `../backup_v1fixapp.py`. It runs without importing Streamlit or QuickFIX.
It connects separate Order Routing and Market Data sessions, sends TT's
password in Logon tag 96, handles heartbeats and TestRequest, and logs out on
shutdown. Both sessions must acknowledge logon before the desk reports LOGGED_ON.

From this directory:

```powershell
python -m pip install -r requirements.txt
python import_backup_config.py
.\run_fix.ps1
```

Create and activate a virtual environment first if Python dependencies are not
already installed. `run_fix.ps1` uses `.venv` in this repository when present,
then a sibling `.venv`, then `python` on `PATH`. Run the importer once. It
reads the existing backup's
`../.streamlit/secrets.toml`, writes passwords to `.env`, and creates the
separate `config.tt-uat.json`. Existing simulator configuration is preserved.
The imported configuration has no example instruments and has the master algo
disabled. The backup uses direct TCP; its importer therefore sets `use_tls`
false. Set this according to the TT-provisioned endpoint, not its port alone.

Open http://127.0.0.1:8000/ for the FIX connection dashboard (also available at
`/connection`). It shows each session's logon, heartbeat, message counts,
sequence numbers, and recent activity. Connect FIX, Disconnect and Reconnect
act on the engine's sessions; reconnection includes a ten-second cooldown.
Stale engine status disables the controls and invalidates connected badges.
The trading desk remains at `/desk`. The Algo's orders use the same Order
Routing session (PAPER until LIVE is armed and confirmed).

Use `/exchanges` to edit configuration or Test for current session status.
Connect asks the engine to start its persistent sessions; web requests never
open additional real FIX sessions. Do not connect the same CompIDs from the
backup terminal at the same time.

Equivalent launch command:

```powershell
python start.py --fix --no-browser --config config.tt-uat.json --status status.tt-uat.json --commands commands.tt-uat.jsonl --results results.tt-uat.json --port 8000
```

The default `start.py` mode remains simulated. Stop the launcher with Ctrl+C.
Changing session settings or credentials requires restarting the launcher.

The adapter supports [instrument lookup, quotes and reviewed manual
orders](INSTRUMENTS_AND_ORDERS.md), and the Algo's own orders (`FT-` ids,
77=O/C, 1028=N, cancel-on-disconnect). Account positions are requested at
each logon (Request For Positions); until TT answers they are `None` — the
gateway never claims the account is flat.
The adapter accepts TT UAT FIX.4.2 with sequence reset enabled, matching the
backup. It does not implement replay recovery: a recovery request closes the
session with a visible error. Production is refused.
