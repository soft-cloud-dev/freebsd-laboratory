# Prototype architecture

## Principle

The browser reports observations. It does not manufacture them.

```text
JupyterLab
    |
    | Jupyter protocol + laboratory commands
    v
Laboratory Server
    |-- runtime manager
    |-- evidence recorder
    |-- verifier
    `-- progression state
    |
    +--> FreeBSD jail (default userspace labs)
    `--> bhyve VM (kernel/privileged labs)
```

## Prototype boundary

`prototype/runtime.py` defines the executor boundary. The local executor exists only for development. `FreeBSDExecutor` refuses to claim a FreeBSD runtime when the process is not actually running on FreeBSD.

`prototype/evidence.py` records commands, exit codes, stdout/stderr, timestamps, and content hashes. Notebook output remains presentation; the evidence stream is the record.

`prototype/progression.py` derives trust stages from evidence/state rather than treating the UI as a collection of manually checked boxes.

`lab.yaml` is the declarative laboratory contract.

## Production path

1. Implement a Jupyter Server extension that owns the evidence stream.
2. Implement a JupyterLab extension for the progression panel and Export evidence command.
3. Add a jail executor that creates a disposable jail from a known FreeBSD base snapshot.
4. Add a bhyve executor for experiments requiring a separate kernel or privileged device/network access.
5. Execute notebooks in fresh environments and export deterministic evidence bundles.
6. Add verification that reruns the notebook from a clean runtime and evaluates declared assertions.

## Evidence bundle

```text
evidence/
|-- manifest.json
|-- evidence.json
|-- notebook.ipynb
|-- notebook.executed.ipynb
|-- environment.json
|-- commands.jsonl
|-- stdout/
|-- stderr/
|-- assertions.json
`-- SHA256SUMS
```

The prototype intentionally does not fake jail or bhyve provisioning on non-FreeBSD systems. Those executors are the next implementation boundary.
