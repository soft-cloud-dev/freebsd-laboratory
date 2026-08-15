from __future__ import annotations

from pathlib import Path

from jupyter_client.kernelspec import KernelSpecManager


KERNELSPECS = (
    "freebsd-python",
    "freebsd-python-bhyve",
)


def main() -> None:
    root = Path(__file__).parent / "kernels"
    manager = KernelSpecManager()

    for kernel_name in KERNELSPECS:
        source = root / kernel_name
        if not source.is_dir():
            raise RuntimeError(f"Bundled kernelspec not found: {source}")

        destination = manager.install_kernel_spec(
            str(source),
            kernel_name=kernel_name,
            user=True,
            replace=True,
        )
        print(f"Installed {kernel_name} at {destination}")


if __name__ == "__main__":
    main()
