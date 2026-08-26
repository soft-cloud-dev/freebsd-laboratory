"""Contract test for FreeBSD Laboratory kernel walkaround notebooks."""

import json
from pathlib import Path
import unittest

from scripts.generate_walkaround_notebooks import KERNEL_WALKAROUNDS, generate_all_walkarounds


class TestWalkaroundNotebooksContract(unittest.TestCase):
    """Verifies format, educational structure, and security properties of walkaround notebooks."""

    @classmethod
    def setUpClass(cls):
        cls.repo_root = Path(__file__).resolve().parent.parent
        cls.notebooks_dir = cls.repo_root / "notebooks"
        cls.kernels_dir = cls.repo_root / "freebsd_laboratory" / "kernels"

    def test_kernel_discovery_and_generation_consistency(self):
        """Assert that notebooks are generated for all discoverable kernels."""
        generated_paths = generate_all_walkarounds(self.repo_root)
        self.assertGreater(len(generated_paths), 0)

        for kernel_name in KERNEL_WALKAROUNDS:
            kernel_json = self.kernels_dir / kernel_name / "kernel.json"
            if kernel_json.exists():
                spec = KERNEL_WALKAROUNDS[kernel_name]
                base_name = spec.title.replace(" Walkaround", "").replace(" ", "_")
                expected_path = self.notebooks_dir / f"Walkaround_{base_name}.ipynb"
                self.assertTrue(
                    expected_path.exists(),
                    f"Expected generated notebook {expected_path.name} to exist for kernel {kernel_name}",
                )

    def test_notebook_json_and_kernelspec_contract(self):
        """Assert that each generated notebook is valid nbformat v4 and has matching kernelspec."""
        for kernel_name, spec in KERNEL_WALKAROUNDS.items():
            kernel_json_path = self.kernels_dir / kernel_name / "kernel.json"
            if not kernel_json_path.exists():
                continue

            with open(kernel_json_path, "r", encoding="utf-8") as f:
                kernel_spec_data = json.load(f)

            base_name = spec.title.replace(" Walkaround", "").replace(" ", "_")
            notebook_path = self.notebooks_dir / f"Walkaround_{base_name}.ipynb"
            self.assertTrue(notebook_path.exists())

            with open(notebook_path, "r", encoding="utf-8") as f:
                nb = json.load(f)

            self.assertEqual(nb.get("nbformat"), 4)
            kernelspec = nb.get("metadata", {}).get("kernelspec", {})
            self.assertEqual(kernelspec.get("name"), kernel_name)
            self.assertEqual(kernelspec.get("display_name"), kernel_spec_data.get("display_name"))

    def test_no_absolute_file_urls(self):
        """Assert no absolute file:/// paths exist in the generated notebooks."""
        for nb_file in self.notebooks_dir.glob("Walkaround_*.ipynb"):
            with open(nb_file, "r", encoding="utf-8") as f:
                content = f.read()

            self.assertNotIn("file:///", content, f"Found absolute file:/// link in {nb_file.name}")
            self.assertNotIn("Users/", content, f"Found local macOS user path in {nb_file.name}")

    def test_educational_progression_sections(self):
        """Assert all required architectural sections and Mermaid diagrams are present."""
        required_headers = [
            "## 0. You Are Here",
            "## 1. Architecture: The Three Planes",
            "## 2. Kernel Contract",
            "## 3. How It Is Launched",
            "## 4. Runtime Security Boundary",
            "## 5. Inspect the Runtime",
            "## 8. What You Cannot See",
            "## 9. Summary & Design Invariants",
        ]

        for nb_file in self.notebooks_dir.glob("Walkaround_*.ipynb"):
            with open(nb_file, "r", encoding="utf-8") as f:
                nb = json.load(f)

            markdown_content = "\n".join(
                "".join(c.get("source", [])) for c in nb.get("cells", []) if c.get("cell_type") == "markdown"
            )

            for header in required_headers:
                self.assertIn(
                    header,
                    markdown_content,
                    f"Missing required educational section '{header}' in {nb_file.name}",
                )

            # Assert three-plane Mermaid diagram exists
            self.assertIn("graph LR", markdown_content)
            self.assertIn("Browser[JupyterLab / Notebook document]", markdown_content)
            self.assertIn("Daemon[Root runtime daemon]", markdown_content)

            # Assert first code cell is an identity assertion
            code_cells = [c for c in nb.get("cells", []) if c.get("cell_type") == "code"]
            self.assertTrue(len(code_cells) > 0, f"No code cells in {nb_file.name}")
            first_code_source = "".join(code_cells[0].get("source", []))
            self.assertTrue(
                first_code_source.startswith("%%sh"),
                f"First code cell in {nb_file.name} must be a %%sh identity probe",
            )


if __name__ == "__main__":
    unittest.main()
