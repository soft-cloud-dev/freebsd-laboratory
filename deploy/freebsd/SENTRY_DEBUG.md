# Sentry delivery diagnostics

Sentry events are emitted by the FreeBSD host processes, not by the isolated jail or bhyve runtime. Run diagnostics in the same environment and with the same `SENTRY_DSN` used by Jupyter Server or the runtime daemon.

## Read-only check

```sh
cd ~/freebsd-laboratory
export SENTRY_DSN='<project DSN>'
.venv/bin/freebsd-lab-sentry-diagnose
```

The command checks, in order:

1. `sentry-sdk` importability in the current Python environment.
2. DSN structure without printing the public key, secret, or full DSN.
3. Presence of proxy-related environment variables without printing their values.
4. DNS resolution of the Sentry ingest hostname through the same Python/libc resolver path used by the application.
5. TCP connectivity to one of the addresses returned by that DNS lookup, without performing a second hostname lookup.
6. TLS certificate verification for HTTPS DSNs, requiring TLS 1.2 or newer.

The DNS check performs three bounded attempts for transient `EAI_AGAIN` or `EAI_NONAME` failures. A failed check exits with status 1. A read-only successful run reports the event check as `SKIP`.

## End-to-end test event

```sh
.venv/bin/freebsd-lab-sentry-diagnose --send-test-event --sdk-debug
```

This initializes the existing project telemetry configuration, emits one event tagged with `component=sentry-diagnostics` and `diagnostic=true`, flushes the SDK transport, and prints only the resulting event ID. The DSN credentials are never printed by the diagnostic command.

`--sdk-debug` enables Sentry SDK internal diagnostic logging for this standalone process. It does not change normal Jupyter Server or runtime-daemon logging.

## Machine-readable output

```sh
.venv/bin/freebsd-lab-sentry-diagnose --json
```

This is suitable for attaching diagnostic evidence without exposing the DSN credentials.

## Fresh FreeBSD bootstrap

The host virtual environments use `--system-site-packages` for FreeBSD-native dependencies such as `certifi` and `urllib3`, but `sentry-sdk` itself is installed directly into each virtual environment at the explicit `LAB_SENTRY_SDK_VERSION` (default `2.66.1`). The bootstrap uses `--ignore-installed` so a system-level `py*-sentry-sdk` package cannot satisfy or contaminate the virtual-environment installation.

After installation, bootstrap verifies both the package version and that `sentry_sdk.__file__` resolves below the expected virtual-environment directory. This prevents a partially upgraded or internally inconsistent `/usr/local/lib/python*/site-packages/sentry_sdk` package from breaking the daemon or Jupyter environment.

The DSN itself remains runtime configuration and is intentionally not stored in the public repository.
