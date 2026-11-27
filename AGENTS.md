# Aruba 2930F Config Backup

The Windows desktop application lives in `src/aruba2930f_backup`. The portable
browser application lives in `local-web/`, with its own copy of the SSH core.
Use Python 3.14. Start the local web UI with `local-web/Start-Local-Web.cmd` in
the portable distribution or run `python local-web/server.py` from source.

Run `python -m pytest` for the desktop application. From `local-web`, set
`PYTHONPATH=core` and run `python -m unittest discover -s tests`. The Windows
CI also exercises browser flows and the packaged runtime. Use temporary data
directories, simulated switches and documentation addresses in tests. Keep
credentials, device output, inventories and generated packages out of Git.

Read `local-web/AGENTS.md` for packaging and release instructions. Develop Ping
on the existing feature PR using `docs/PING_TEST_DEVELOPMENT_PLAN_KO.md`.
Run device commands only on explicit operator action or within a bounded test
the operator started. Preserve existing SSH trust, authorization and shared job
coordination. Field verification and publication are separate from development.
