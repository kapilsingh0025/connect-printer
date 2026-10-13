# Office Print Cloud

Cloud queue + Windows print agent. Keep the server's environment secrets private.

## Server deployment (Render)

1. Deploy this folder as a Python web service using the included `Dockerfile`.
2. Set these environment variables in the hosting dashboard:
   - `ADMIN_USER`: admin login username
   - `ADMIN_PASS`: strong admin password
   - `AGENT_KEY`: long random key shared with the Windows agent
   - `SECRET_KEY`: a separate long random session secret
   - `DATABASE_URL`: recommended PostgreSQL connection string for persistent users and job history
   - `INSECURE_COOKIES`: only set to `1` for local HTTP testing; leave unset on HTTPS hosting
3. Optional `USERS` seed list format: `rahul:password1,amit:password2`. Prefer creating users in the admin panel.
4. Health check: `/healthz`.

Without `DATABASE_URL`, SQLite is used at `DATA_DIR/print.db`; that directory must be persistent on the host. Ephemeral hosting storage can lose data on restart.

## Windows print agent

1. Install Python on the office PC, then install dependencies: `pip install requests pywin32`.
2. Put `agent.py`, `SumatraPDF.exe`, and a `config.json` beside each other. Copy `config.example.json` to `config.json` and edit the values.
3. Set `AGENT_KEY` to exactly the same value as the server's `AGENT_KEY`.
4. Run `python agent.py`. For an unattended office PC, configure it to run at Windows startup.
5. `PRINTERS: []` auto-detects physical local printers, filtering common virtual printers. Or list exact Windows printer names, e.g. `["HP LaserJet Pro"]`.

The agent sends a one-time boot marker. Jobs interrupted while printing are marked as errors rather than silently re-queued, because re-queueing can print a duplicate. Check the physical printer before manually resending such a job.

## Queue behavior

- Waiting jobs can be cancelled by their owner.
- Admin can clear all waiting jobs and remove a printer from the dashboard.
- Jobs that time out while printing or whose printer PC goes offline become errors and are not automatically re-queued.
- The print agent's `done` endpoint reports success/failure and the server removes the uploaded PDF afterward.

## Local server test

Set the four required environment variables, install `requirements.txt`, then run:

```bash
python server.py
```

For the Windows agent, `pywin32` and a working Windows printer are required; it cannot be physically tested on the cloud server.
