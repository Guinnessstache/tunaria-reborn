# Web client

The browser client is not ours: it is DabDavis's MIT-licensed Rust port of the PS2
client (`eqoa-rust-client-vanilla`), compiled to WebAssembly by
`.github/workflows/webclient.yml` from the repository and commit in `UPSTREAM`. The
build job runs that code with a read-only token; a separate job commits the built page
(with the upstream LICENSE) to the `webclient-dist` branch. `eqoa_server/webhost.py`
serves it and runs the WebSocket relay the page needs.

Players pick their own Frontiers disc image (SLUS-20744) in the page; it is read in the
browser and never uploaded. No game data is built or served.

To move to a newer client: review its changes, put its commit in `UPSTREAM` and push to
main.
