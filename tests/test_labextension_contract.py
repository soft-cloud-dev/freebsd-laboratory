from __future__ import annotations

import json
from pathlib import Path


PACKAGE = Path("labextension/package.json")
SOURCE = Path("labextension/src/index.ts")


def test_labextension_targets_freebsd_jupyterlab_46_family() -> None:
    package = json.loads(PACKAGE.read_text(encoding="utf-8"))

    assert package["dependencies"] == {
        "@jupyterlab/application": "4.6.1",
        "@jupyterlab/apputils": "4.7.1",
        "@jupyterlab/coreutils": "6.6.1",
        "@jupyterlab/notebook": "4.6.1",
        "@jupyterlab/services": "7.6.1",
        "@lumino/widgets": "2.8.0",
    }
    assert package["devDependencies"]["@jupyterlab/builder"] == "4.3.0"
    assert package["scripts"]["build"].startswith("npm run clean &&")


def test_plugin_activation_does_not_wait_for_workspace_restoration() -> None:
    source = SOURCE.read_text(encoding="utf-8")

    assert "activate: (" in source
    assert "activate: async (" not in source
    assert "void tracker.restored" in source
    assert "await tracker.restored" not in source
    assert "void progression.refresh();" in source
