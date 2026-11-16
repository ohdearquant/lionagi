### Fixed

- The remote legs guide starts a leg host with `li studio --host <address>`. The variable it named before was overridden by the `--host` default, so the documented command listened on `127.0.0.1` only.
