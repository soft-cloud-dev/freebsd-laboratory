#!/usr/bin/env python3
"""Declarative generator for FreeBSD Laboratory kernel walkaround notebooks."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional


@dataclass
class KernelWalkaroundSpec:
    title: str
    provisioner_file: str
    identity_cmd: str
    security_boundary: str
    boundary_diagram: str
    agent_cmd: str
    startup_timeout_seconds: int
    extra_constraints: Optional[str] = None


KERNEL_WALKAROUNDS: Dict[str, KernelWalkaroundSpec] = {
    "freebsd-python": KernelWalkaroundSpec(
        title="FreeBSD VNET Jail Walkaround",
        provisioner_file="freebsd_laboratory/provisioner.py",
        identity_cmd=(
            "%%sh\n"
            "printf 'OS: '; uname -srm\n"
            "printf 'Jailed: '; sysctl -n security.jail.jailed\n"
            "printf 'Hostname: '; hostname\n"
        ),
        security_boundary=(
            "Separate jail userspace and VNET networking, but a shared FreeBSD host kernel. "
            "Privileged lifecycle operations remain outside the jail and are delegated to the "
            "root-owned runtime daemon."
        ),
        boundary_diagram=(
            "graph TD\n"
            "    Host[Host System] -->|SSH / Loopback ZMQ| PF[PF Firewall]\n"
            "    PF -->|TCP/22 only| Bridge[labbridge0]\n"
            "    Bridge --> epair[epair interface]\n"
            "    epair --> Jail[VNET Jail]\n"
            "    Jail --> Kernel[Python / ipykernel]"
        ),
        agent_cmd=(
            "%%agent\n"
            "Perform a read-only inspection of this runtime.\n\n"
            "Determine:\n"
            "- operating system and kernel\n"
            "- whether jail virtualization is observable\n"
            "- current user\n"
            "- memory availability\n"
            "- network interfaces\n"
            "- listening TCP sockets\n\n"
            "Do not use sudo, modify files, install packages, or make external network requests.\n"
            "Summarize how the observations relate to this notebook's stated security boundary."
        ),
        startup_timeout_seconds=30,
    ),
    "freebsd-python-bhyve": KernelWalkaroundSpec(
        title="FreeBSD bhyve VM Walkaround",
        provisioner_file="freebsd_laboratory/bhyve.py",
        identity_cmd=(
            "%%sh\n"
            "printf 'OS: '; uname -srm\n"
            "printf 'VM type: '; sysctl -n kern.vm_guest\n"
            "printf 'Hostname: '; hostname\n"
        ),
        security_boundary=(
            "Separate guest kernel and virtual hardware provide a stronger kernel isolation "
            "boundary than a jail. Jupyter still does not connect directly to guest ZMQ sockets; "
            "the provisioner uses SSH forwarding over the private labbridge0 network."
        ),
        boundary_diagram=(
            "graph TD\n"
            "    Host[Host System] -->|SSH / Loopback ZMQ| PF[PF Firewall]\n"
            "    PF -->|TCP/22 only| Bridge[labbridge0]\n"
            "    Bridge --> Tap[tap interface]\n"
            "    Tap --> VM[bhyve VM]\n"
            "    VM --> Kernel[Python / ipykernel]"
        ),
        agent_cmd=(
            "%%agent\n"
            "Perform a read-only inspection of this runtime.\n\n"
            "Determine:\n"
            "- operating system and kernel\n"
            "- whether virtualization is observable\n"
            "- current user\n"
            "- memory availability\n"
            "- network interfaces\n"
            "- listening TCP sockets\n\n"
            "Do not use sudo, modify files, install packages, or make external network requests.\n"
            "Summarize how the observations relate to this notebook's stated security boundary."
        ),
        startup_timeout_seconds=90,
    ),
    "linux-python-bhyve": KernelWalkaroundSpec(
        title="Linux bhyve VM Walkaround",
        provisioner_file="freebsd_laboratory/bhyve.py",
        identity_cmd=(
            "%%sh\n"
            "uname -srm\n"
            "cat /etc/os-release\n"
            "printf 'Hypervisor: '\n"
            "cat /sys/class/dmi/id/product_name 2>/dev/null || true\n"
        ),
        security_boundary=(
            "Separate Linux guest kernel and virtual hardware provide a strong hypervisor boundary. "
            "The target ABI is Linux, not FreeBSD. The provisioner explicitly asks the runtime daemon "
            "for its capabilities and refuses startup unless bhyve.linux is present."
        ),
        boundary_diagram=(
            "graph TD\n"
            "    Host[Host System] -->|SSH / Loopback ZMQ| PF[PF Firewall]\n"
            "    PF -->|TCP/22 only| Bridge[labbridge0]\n"
            "    Bridge --> Tap[tap interface]\n"
            "    Tap --> VM[Linux bhyve VM]\n"
            "    VM --> Kernel[Python / ipykernel]"
        ),
        agent_cmd=(
            "%%agent\n"
            "Perform a read-only inspection of this runtime.\n\n"
            "Determine:\n"
            "- operating system and kernel\n"
            "- whether virtualization is observable\n"
            "- current user\n"
            "- memory availability\n"
            "- network interfaces\n"
            "- listening TCP sockets\n\n"
            "Do not use sudo, modify files, install packages, or make external network requests.\n"
            "Summarize how the observations relate to this notebook's stated security boundary."
        ),
        startup_timeout_seconds=90,
        extra_constraints="Requires runtime daemon capability: bhyve.linux",
    ),
}


def get_snippet(filepath: Path, search_strings: List[str], lines_after: int = 15) -> str:
    """Extract a concise snippet around target search strings."""
    if not filepath.exists():
        return "# Implementation file not found locally"
    with open(filepath, "r", encoding="utf-8") as f:
        lines = f.readlines()
    for search_str in search_strings:
        for i, line in enumerate(lines):
            if search_str in line:
                return "".join(lines[i : i + lines_after]).strip()
    return "".join(lines[:lines_after]).strip()


def get_kernel_json_excerpt(filepath: Path) -> str:
    """Read the kernel.json file content."""
    with open(filepath, "r", encoding="utf-8") as f:
        return f.read().strip()


def make_walkaround(
    repo_root: Path,
    kernel_name: str,
    spec: KernelWalkaroundSpec,
    kernel_json_path: Path,
) -> dict:
    """Generate a complete Jupyter notebook document conforming to the educational contract."""
    display_name = spec.title
    try:
        with open(kernel_json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            display_name = data.get("display_name", display_name)
    except Exception:
        pass

    provisioner_file = repo_root / spec.provisioner_file
    prov_excerpt = get_snippet(
        provisioner_file,
        ["def _request_create", "class FreeBSDJailProvisioner", "class LinuxBhyveProvisioner", "class "],
        16,
    )
    kernel_json_excerpt = get_kernel_json_excerpt(kernel_json_path)

    rel_kernel_json = kernel_json_path.relative_to(repo_root)
    rel_provisioner = provisioner_file.relative_to(repo_root)

    has_ai_magics = (repo_root / "freebsd_laboratory" / "magics.py").exists()

    constraints_text = (
        f"- **Startup Timeout:** `{spec.startup_timeout_seconds}s`\n"
        "- **Networking:** Loopback ZMQ forwarded over SSH; no direct guest port exposure\n"
        "- **Control Plane:** Privileged operations delegated to root `runtime.sock`\n"
    )
    if spec.extra_constraints:
        constraints_text += f"- **Capability Requirement:** `{spec.extra_constraints}`\n"

    cells = [
        {
            "cell_type": "markdown",
            "metadata": {},
            "source": [
                f"# {spec.title}\n",
                "\n",
                f"Interactive architectural walkaround for the `{kernel_name}` ({display_name}) runtime.\n",
            ],
        },
        {
            "cell_type": "markdown",
            "metadata": {},
            "source": [
                "## 0. You Are Here\n",
                "Execute an immediate runtime identity probe inside this kernel to verify the execution environment.\n",
            ],
        },
        {
            "cell_type": "code",
            "execution_count": None,
            "metadata": {},
            "outputs": [],
            "source": [spec.identity_cmd],
        },
        {
            "cell_type": "markdown",
            "metadata": {},
            "source": [
                "## 1. Architecture: The Three Planes\n",
                "The notebook document is hosted by JupyterLab on the host, while code cells execute in the guest runtime. Provisioning occurs on the host, outside this execution environment.\n\n",
                "```mermaid\n",
                "graph LR\n",
                "    Browser[JupyterLab / Notebook document]\n",
                "    Server[Jupyter Server<br/>unprivileged host process]\n",
                "    Provisioner[Kernel Provisioner]\n",
                "    Daemon[Root runtime daemon]\n",
                "    Runtime[Selected runtime]\n",
                "    Kernel[ipykernel]\n\n",
                "    Browser --> Server\n",
                "    Server --> Provisioner\n",
                "    Provisioner -->|Unix socket| Daemon\n",
                "    Daemon --> Runtime\n",
                "    Provisioner -->|SSH + port forwards| Runtime\n",
                "    Runtime --> Kernel\n",
                "```\n",
            ],
        },
        {
            "cell_type": "markdown",
            "metadata": {},
            "source": [
                "## 2. Kernel Contract\n",
                f"**Security Boundary:** {spec.security_boundary}\n\n",
                "**Constraints & Requirements:**\n",
                constraints_text,
            ],
        },
        {
            "cell_type": "markdown",
            "metadata": {},
            "source": [
                "## 3. How It Is Launched\n",
                f"From [`{rel_kernel_json}`](../{rel_kernel_json}):\n\n",
                "```json\n",
                kernel_json_excerpt + "\n",
                "```\n\n",
                f"From [`{rel_provisioner}`](../{rel_provisioner}):\n\n",
                "```python\n",
                prov_excerpt + "\n",
                "```\n",
            ],
        },
        {
            "cell_type": "markdown",
            "metadata": {},
            "source": [
                "## 4. Runtime Security Boundary\n",
                f"{spec.security_boundary}\n\n",
                "```mermaid\n",
                f"{spec.boundary_diagram}\n",
                "```\n",
            ],
        },
        {
            "cell_type": "markdown",
            "metadata": {},
            "source": [
                "## 5. Inspect the Runtime\n",
                "Execute safe, read-only shell observations inside this runtime.\n",
            ],
        },
        {
            "cell_type": "code",
            "execution_count": None,
            "metadata": {},
            "outputs": [],
            "source": [
                "%%sh\n",
                "uname -srm\n",
                "sockstat -4 -l 2>/dev/null || netstat -tln 2>/dev/null\n",
                "ifconfig 2>/dev/null || ip addr 2>/dev/null\n",
            ],
        },
    ]

    if has_ai_magics:
        cells.extend([
            {
                "cell_type": "markdown",
                "metadata": {},
                "source": [
                    "## 6. Interpret the Evidence\n",
                    "Query the in-notebook AI assistant to synthesize findings.\n",
                ],
            },
            {
                "cell_type": "code",
                "execution_count": None,
                "metadata": {},
                "outputs": [],
                "source": [
                    "%ai Based on the output above, explain which observations demonstrate the identity "
                    "of this runtime and which observations do NOT prove anything about the host-side PF policy or daemon."
                ],
            },
            {
                "cell_type": "markdown",
                "metadata": {},
                "source": [
                    "## 7. Bounded Investigation\n",
                    "Delegate a bounded, read-only verification task to the autonomous agent.\n",
                ],
            },
            {
                "cell_type": "code",
                "execution_count": None,
                "metadata": {},
                "outputs": [],
                "source": [spec.agent_cmd],
            },
        ])
    else:
        cells.append({
            "cell_type": "markdown",
            "metadata": {},
            "source": [
                "## 6. Interpret the Evidence & 7. Bounded Investigation\n",
                "*Note: In-notebook AI and autonomous agent extensions (`magics.py`) are not active in this environment.*\n",
            ],
        })

    cells.extend([
        {
            "cell_type": "markdown",
            "metadata": {},
            "source": [
                "## 8. What You Cannot See\n",
                "Explicitly distinguishing guest-observable facts from control-plane facts:\n\n",
                "- **Guest-Observable Facts:** `uname`, network interfaces (e.g. `epair`, `tap`, `vnet`), IP addresses, installed packages, guest OS identity.\n",
                "- **Control-Plane Facts:** Host PF firewall rules, Jupyter server token validation, bhyve/jail creation commands executed by the daemon, host-side lease allocation, loopback port translation, provisioner decisions.\n\n",
                "A notebook executing inside this runtime cannot directly prove that the host used `create_bhyve()` or `jail -c`. Those are control-plane facts supported by the implementation excerpts in Section 3.\n",
            ],
        },
        {
            "cell_type": "markdown",
            "metadata": {},
            "source": [
                "## 9. Summary & Design Invariants\n",
                "Core architectural invariants:\n\n",
                "1. **Document vs Runtime:** The notebook document is managed by JupyterLab on the host; code cells execute in the isolated runtime.\n",
                "2. **Transport Security:** Jupyter TCP channels are tunneled exclusively over loopback SSH port forwards.\n",
                "3. **Privilege Separation:** The unprivileged Jupyter Server delegates privileged lifecycle operations to the root runtime daemon via `/var/run/freebsd-laboratory/runtime.sock`.\n",
                "4. **Network Policy:** Guest environments cannot observe or alter host-side packet filtering (PF) policies.\n",
                "5. **Ownership Scoping:** Destructive lifecycle operations (GC/cleanup) are strictly scoped to the authenticated owner's UID.\n",
            ],
        },
    ])

    return {
        "cells": cells,
        "metadata": {
            "kernelspec": {
                "display_name": display_name,
                "language": "python",
                "name": kernel_name,
            },
            "language_info": {
                "codemirror_mode": {"name": "ipython", "version": 3},
                "file_extension": ".py",
                "mimetype": "text/x-python",
                "name": "python",
                "nbconvert_exporter": "python",
                "pygments_lexer": "ipython3",
                "version": "3.12.0",
            },
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }


def generate_all_walkarounds(repo_root: Path) -> List[Path]:
    """Discover installed kernel specifications and generate corresponding walkaround notebooks."""
    notebooks_dir = repo_root / "notebooks"
    notebooks_dir.mkdir(exist_ok=True)
    generated_files = []

    for kernel_name, spec in KERNEL_WALKAROUNDS.items():
        kernel_json_path = repo_root / "freebsd_laboratory" / "kernels" / kernel_name / "kernel.json"
        if not kernel_json_path.exists():
            continue

        notebook_data = make_walkaround(repo_root, kernel_name, spec, kernel_json_path)
        base_name = spec.title.replace(" Walkaround", "").replace(" ", "_")
        target_path = notebooks_dir / f"Walkaround_{base_name}.ipynb"

        with open(target_path, "w", encoding="utf-8") as f:
            json.dump(notebook_data, f, indent=2)
            f.write("\n")

        generated_files.append(target_path)
        print(f"Generated: {target_path.relative_to(repo_root)}")

    return generated_files


if __name__ == "__main__":
    current_repo_root = Path(__file__).resolve().parent.parent
    generate_all_walkarounds(current_repo_root)
