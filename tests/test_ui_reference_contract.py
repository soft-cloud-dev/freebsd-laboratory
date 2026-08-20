from __future__ import annotations

import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE = REPO_ROOT / "labextension/src/index.ts"
STYLE = REPO_ROOT / "labextension/style/index.css"
SHELL_STYLE = REPO_ROOT / "labextension/style/shell-fidelity.css"
PACKAGE = REPO_ROOT / "labextension/package.json"
NOTEBOOK = REPO_ROOT / "notebooks/Intro.ipynb"
KERNEL = REPO_ROOT / "freebsd_laboratory/kernels/freebsd-python/kernel.json"


def test_reference_shell_composition_is_preserved() -> None:
    source = SOURCE.read_text(encoding="utf-8")

    assert "shell.mode = 'multiple-document';" in source
    assert "app.shell.add(masthead, 'header', { rank: 0 });" in source
    assert "app.shell.add(statusBar, 'bottom', { rank: 0 });" in source
    assert "app.commands.execute('filebrowser:activate')" in source
    assert "const enforceReferenceShell = (): void => {" in source
    assert "shell.collapseDown();" in source
    assert "shell.expandLeft();" in source
    assert "shell.expandRight();" in source
    assert "for (const widget of shell.widgets('bottom'))" in source
    assert "if (widget !== statusBar)" in source
    assert "enforceReferenceShell();" in source
    assert "panel.contentHeader.addWidget(pathBar);" in source
    assert "label: '⇩ Export evidence'" in source


def test_reference_geometry_and_brand_rails_are_preserved() -> None:
    style = STYLE.read_text(encoding="utf-8")

    assert ".freebsdLab-Masthead" in style
    assert "min-height: 80px" in style
    assert "width: 325px !important" in style
    assert "width: 270px !important" in style
    assert '.jp-SideArea[data-side="left"]' in style
    assert '.jp-SideArea[data-side="right"]' in style
    assert ".freebsdLab-IntroCard" in style
    assert ".freebsdLab-StatusBar" in style
    assert "--freebsd-lab-red: #b31b21" in style


def test_shell_fidelity_overrides_are_loaded_and_lock_split_geometry() -> None:
    package = json.loads(PACKAGE.read_text(encoding="utf-8"))
    style = SHELL_STYLE.read_text(encoding="utf-8")

    assert package["style"] == "style/shell-fidelity.css"
    assert "@import url('./index.css');" in style
    assert "#jp-main-split-panel" in style
    assert "inset: 0 271px 0 326px !important" in style
    assert "> .jp-SideArea[data-side='left']" in style
    assert "> .jp-SideArea[data-side='right']" in style
    assert "> .lm-SplitPanel-handle" in style
    assert "#jp-bottom-panel > .freebsdLab-StatusBar" in style


def test_intro_notebook_contains_reference_hero() -> None:
    notebook = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
    first_source = "".join(notebook["cells"][0]["source"])

    assert "freebsdLab-IntroCard" in first_source
    assert "Welcome to the FreeBSD Laboratory" in first_source
    assert "The Power To Serve — in code, and in systems." in first_source
    assert "About this environment" in first_source


def test_visible_kernel_name_matches_reference() -> None:
    kernel = json.loads(KERNEL.read_text(encoding="utf-8"))

    assert kernel["display_name"] == "FreeBSD (Python 3)"
