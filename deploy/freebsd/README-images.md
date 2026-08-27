# Laboratory OS Distributions

Operating system image build definitions and tooling have moved to dedicated OS distribution repositories under `soft-cloud-dev`:

- **FreeBSD OS Distribution**: [`soft-cloud-dev/os-freebsd`](https://github.com/soft-cloud-dev/os-freebsd)
  - VNET Jail ZFS template builder: `runtime/jail/build.sh`
  - bhyve VM raw disk image builder: `runtime/bhyve/build.sh`
  - Manifest format: `softcloud.artifact/v1`

- **Linux OS Distribution**: [`soft-cloud-dev/os-linux`](https://github.com/soft-cloud-dev/os-linux)
  - Linux EFI stub kernel builder: `kernel/build.sh`
  - bhyve Alpine/Debian raw disk image builder: `runtime/bhyve/build-image.sh`
  - Linuxulator compatibility target: `runtime/jail-linuxulator/`
  - Manifest format: `softcloud.artifact/v1`

- **Template Repository & Schemas**: [`soft-cloud-dev/os-laboratory-template`](https://github.com/soft-cloud-dev/os-laboratory-template)
  - Canonical JSON schemas: `schemas/artifact-v1.schema.json`, `schemas/os-v1.schema.json`

## FreeBSD Laboratory Ingestion

`freebsd-laboratory` is a pure execution engine that ingests declarative artifact manifests (`softcloud.artifact/v1`) through its `ArtifactStore` (`freebsd_laboratory.artifact_store`).
