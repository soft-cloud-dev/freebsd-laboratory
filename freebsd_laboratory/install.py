from __future__ import annotations

from pathlib import Path

from jupyter_client.kernelspec import KernelSpecManager


def main() -> None:
    source = Path(__file__).parent / "kernels" / "freebsd-python"
    if not source.is_dir():
        raise RuntimeError(f"Bundled kernelspec not found: {source}")

    destination = KernelSpecManager().install_kernel_spec(
        str(source),
        kernel_name="freebsd-python",
        user=True,
        replace=True,
    )
    print(f"Installed FreeBSD Laboratory kernelspec at {destination}")


if __name__ == "__main__":
    main()
