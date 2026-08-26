import ast
import json
import unittest
from pathlib import Path

NOTEBOOK_PATH = Path("notebooks/Build_Linux_VM_Agent.ipynb")


class TestLinuxVMAgentNotebookContract(unittest.TestCase):
    def setUp(self) -> None:
        self.assertTrue(NOTEBOOK_PATH.is_file(), f"Notebook not found at {NOTEBOOK_PATH}")
        with open(NOTEBOOK_PATH, "r", encoding="utf-8") as f:
            self.nb_data = json.load(f)

    def test_notebook_metadata_and_softcloud_schema(self) -> None:
        self.assertEqual(self.nb_data.get("nbformat"), 4)
        metadata = self.nb_data.get("metadata", {})
        self.assertIn("softcloud", metadata)
        self.assertEqual(metadata["softcloud"].get("lab"), "freebsd-linux-vm-agent")
        self.assertEqual(metadata["softcloud"].get("schema"), "softcloud.lab-notebook/v1")

    def test_notebook_contains_hero_card(self) -> None:
        first_cell = self.nb_data["cells"][0]
        self.assertEqual(first_cell["cell_type"], "markdown")
        source = "".join(first_cell["source"])
        self.assertIn("freebsdLab-IntroCard", source)
        self.assertIn("Autonomous Linux VM Golden Image Builder Agent", source)
        self.assertIn("FreeBSD Laboratory", source)

    def test_all_code_cells_are_valid_python_syntax(self) -> None:
        code_cells = [c for c in self.nb_data["cells"] if c["cell_type"] == "code"]
        self.assertGreaterEqual(len(code_cells), 5)
        for i, cell in enumerate(code_cells):
            code_str = "".join(cell["source"])
            # Remove any notebook magic lines before checking Python AST
            non_magic_lines = [
                line for line in code_str.splitlines()
                if not line.strip().startswith("%") and not line.strip().startswith("!")
            ]
            clean_code = "\n".join(non_magic_lines)
            try:
                ast.parse(clean_code)
            except SyntaxError as exc:
                self.fail(f"Code cell {i+1} has invalid Python syntax: {exc}")

    def test_all_supplied_tools_present_in_notebook(self) -> None:
        all_code = "\n".join("".join(c["source"]) for c in self.nb_data["cells"] if c["cell_type"] == "code")
        required_tools = [
            "ToolCheckPrerequisites",
            "ToolBuildLinuxKernel",
            "ToolCreatePartitionedDisk",
            "ToolStageAlpineRootfs",
            "ToolBootstrapPackages",
            "ToolConfigureSystemServices",
            "ToolPopulateExt4Rootfs",
            "ToolRegisterVmTemplate",
            "ToolVerifyLinuxVm",
            "ToolExecuteShell",
            "ToolReadFile",
        ]
        for t in required_tools:
            self.assertIn(f"class {t}", all_code, f"Missing tool definition: {t}")

        required_tool_names = [
            "tool_check_prerequisites",
            "tool_build_linux_kernel",
            "tool_create_partitioned_disk",
            "tool_stage_alpine_rootfs",
            "tool_bootstrap_packages",
            "tool_configure_system_services",
            "tool_populate_ext4_rootfs",
            "tool_register_vm_template",
            "tool_verify_linux_vm",
            "tool_execute_shell",
            "tool_read_file",
        ]
        for name in required_tool_names:
            self.assertIn(f'name = "{name}"', all_code, f"Missing tool name registration: {name}")

    def test_agent_controller_and_loop_execution(self) -> None:
        # Extract and execute the framework, tools, and controller definitions
        code_cells = [c for c in self.nb_data["cells"] if c["cell_type"] == "code"]
        combined_source = []
        for cell in code_cells:
            for line in "".join(cell["source"]).splitlines():
                # Filter out interactive IPython display calls
                if "IPython.display" in line or "display(" in line:
                    continue
                if line.strip().startswith("%") or line.strip().startswith("!"):
                    continue
                combined_source.append(line)

        namespace = {}
        exec("\n".join(combined_source), namespace)

        self.assertIn("ToolRegistry", namespace)
        self.assertIn("LinuxVMAgentController", namespace)

        registry = namespace["registry"]
        self.assertGreaterEqual(len(registry.list_tools()), 11)

        controller = namespace["LinuxVMAgentController"](registry=registry, max_steps=12)
        goal = "Build a bootable Linux bhyve golden VM image from scratch."
        result = controller.run(goal)

        self.assertIn("Linux bhyve golden VM image built successfully", result)
        self.assertGreaterEqual(len(controller.evidence_events), 8)


if __name__ == "__main__":
    unittest.main()
