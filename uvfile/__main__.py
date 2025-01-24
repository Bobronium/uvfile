import sys
from pathlib import Path
import subprocess
import tomllib as toml
import argparse
from dataclasses import dataclass, field
from typing import Optional, List, Dict
import shlex
import os
from packaging.requirements import Requirement


DEFAULT_UVFILE_PATH = Path("UVFile")


def debug(*args, verbose: bool):
    if verbose:
        print(*args, file=sys.stderr)


@dataclass
class RequirementSpec:
    """Represents a single requirement, with all its possible sources."""

    name: Optional[str] = None
    version: Optional[str] = None
    extras: List[str] = field(default_factory=list)
    git: Optional[str] = None
    editable: Optional[str] = None
    directory: Optional[str] = None

    def to_install_args(self, as_with: bool = False) -> List[str]:
        """
        Convert the requirement into an install command fragment.
        - If `as_with` is True, formats this as a `--with` or `--with-editable` argument.
        """
        if self.editable:
            return (
                ["--with-editable", self.editable]
                if as_with
                else ["--editable", self.editable]
            )
        if self.directory:
            return ["--with", self.directory] if as_with else [self.directory]
        if self.git:
            fragment = f"{self.name}@{self.git}" if self.name else self.git
            return ["--with", fragment] if as_with else [fragment]
        base = self.name or ""
        if self.extras:
            base += f"[{','.join(self.extras)}]"
        if self.version:
            base += self.version
        return ["--with", base] if as_with else [base]

    def matches(self, other: "RequirementSpec") -> bool:
        """Check if two requirements match in all aspects."""
        return (
            self.name == other.name
            and self.version == other.version
            and sorted(self.extras) == sorted(other.extras)
            and self.git == other.git
            and self.editable == other.editable
            and self.directory == other.directory
        )


@dataclass
class Tool:
    """Represents a single tool with all its dependencies and metadata."""

    primary: RequirementSpec
    additional: List[RequirementSpec] = field(default_factory=list)
    python_version: Optional[str] = None

    def install_command(self, reinstall: bool = False) -> List[str]:
        """Construct the full installation command for this tool."""
        command = self.primary.to_install_args()
        for req in self.additional:
            command.extend(req.to_install_args(as_with=True))
        if self.python_version:
            command.extend(["--python", self.python_version])
        if reinstall:
            command.append("--reinstall")
        return command

    def matches(self, other: "Tool") -> bool:
        """Check if two tools match, including their dependencies."""
        if not self.primary.matches(other.primary):
            return False
        if sorted(self.additional, key=lambda r: (r.name, r.version)) != sorted(
            other.additional, key=lambda r: (r.name, r.version)
        ):
            return False
        return self.python_version == other.python_version


def parse_uv_receipt(receipt_path: Path) -> Optional[Tool]:
    """Parse a uv-receipt.toml file into a Tool object."""
    if not receipt_path.exists():
        return None
    receipt = toml.loads(receipt_path.read_text())
    requirements = receipt["tool"]["requirements"]
    primary_req = parse_requirement(requirements[0])
    additional_reqs = [parse_requirement(req) for req in requirements[1:]]
    python_version = receipt["tool"].get("python")
    return Tool(
        primary=primary_req, additional=additional_reqs, python_version=python_version
    )


def parse_requirement(requirement: Dict) -> RequirementSpec:
    """Parse a single requirement dictionary from uv-receipt.toml."""
    return RequirementSpec(
        name=requirement.get("name"),
        version=requirement.get("specifier"),
        extras=requirement.get("extras", []),
        git=requirement.get("git"),
        editable=requirement.get("editable"),
        directory=requirement.get("directory"),
    )


def get_installed_tools(uv_tools_dir: Path) -> List[Tool]:
    """Fetch the list of installed tools and their versions using `uv tool list`."""
    result = subprocess.run(
        ["uv", "tool", "list", "--show-paths"],
        text=True,
        capture_output=True,
        check=True,
    )
    tools = []
    for line in result.stdout.strip().splitlines():
        if line.startswith("-"):  # Skip entrypoints
            continue
        name_version, path = line.rsplit(" ", 1)
        receipt = parse_uv_receipt(Path(path.strip("()")) / "uv-receipt.toml")
        if receipt:
            tools.append(receipt)
    return tools


def collect_tool_metadata(uvfile_path: Path) -> List[Tool]:
    """Parse the UVFile and return a list of tools."""
    tools = []
    if not uvfile_path.exists():
        return tools

    for line in uvfile_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        requirement, *extra_args = shlex.split(line)
        req = Requirement(requirement)
        parser = argparse.ArgumentParser()
        parser.add_argument("--python")
        namespace, additional_args = parser.parse_known_args(extra_args)

        primary = RequirementSpec(
            name=req.name,
            version=str(req.specifier) if req.specifier else None,
            extras=list(req.extras),
        )
        additional = []
        for arg in additional_args:
            if arg.startswith("--with"):
                additional.append(parse_requirement({"name": arg.split()[1]}))
            elif arg.startswith("--with-editable"):
                additional.append(parse_requirement({"editable": arg.split()[1]}))
        tools.append(
            Tool(
                primary=primary, additional=additional, python_version=namespace.python
            )
        )
    return tools


def write_uvfile(tools: List[Tool], uvfile_path: Path) -> None:
    """Write the UVFile with the list of tools and their metadata."""
    lines = []
    for tool in tools:
        command = tool.install_command()
        lines.append(" ".join(command))
    uvfile_path.write_text(
        "# UVFile: Auto-generated file to track installed uv tools\n\n"
        + "\n".join(lines)
    )


def install_from_uvfile(
    reinstall: bool,
    uninstall: bool,
    strict: bool,
    dry_run: bool,
    verbose: bool,
    uvfile_path: Path,
) -> None:
    """Install dependencies listed in the UVFile."""
    installed_tools = get_installed_tools(Path.home() / ".local/share/uv/tools")
    uvfile_tools = collect_tool_metadata(uvfile_path)

    # Handle strict mode (uninstall tools not in the UVFile)
    if strict or uninstall:
        installed_names = {tool.primary.name for tool in installed_tools}
        uvfile_names = {tool.primary.name for tool in uvfile_tools}
        tools_to_remove = installed_names - uvfile_names
        for tool_name in tools_to_remove:
            command = ["uv", "tool", "uninstall", tool_name]
            if dry_run:
                print(f"Would run: {' '.join(command)}")
            else:
                debug(f"Uninstalling: {tool_name}", verbose=verbose)
                subprocess.run(command, check=True)

    # Install or skip tools from the UVFile
    for tool in uvfile_tools:
        matching_installed_tool = next(
            (t for t in installed_tools if t.primary.name == tool.primary.name), None
        )
        if (
            matching_installed_tool
            and tool.matches(matching_installed_tool)
            and not (reinstall or strict)
        ):
            debug(f"Skipping {tool.primary.name}, already installed.", verbose=verbose)
            continue
        command = ["uv", "tool", "install"] + tool.install_command(
            reinstall=(reinstall or strict)
        )
        if dry_run:
            print(f"Would run: {' '.join(command)}")
        else:
            debug(f"Installing: {tool.primary.name}", verbose=verbose)
            subprocess.run(command, check=True)


def init_uvfile(force: bool, uvfile_path: Path) -> None:
    """Generate a new UVFile from currently installed tools."""
    if uvfile_path.exists() and not force:
        confirmation = (
            input(f"{uvfile_path} already exists. Overwrite? [y/N]: ").strip().lower()
        )
        if confirmation != "y":
            print("Aborted.")
            return

    installed_tools = get_installed_tools(Path.home() / ".local/share/uv/tools")
    write_uvfile(installed_tools, uvfile_path)
    print(f"UVFile initialized with {len(installed_tools)} tools.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage uv tools with a UVFile.")
    parser.add_argument(
        "--uvfile",
        type=Path,
        default=Path(os.getenv("UVFILE_PATH", DEFAULT_UVFILE_PATH)),
        help="Path to the UVFile (default: UVFile in the current directory or $UVFILE_PATH).",
    )
    parser.add_argument("--verbose", action="store_true", help="Enable verbose output.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Init command
    init_parser = subparsers.add_parser(
        "init", help="Generate a UVFile from currently installed tools."
    )
    init_parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite the UVFile if it already exists.",
    )

    # Sync command
    sync_parser = subparsers.add_parser(
        "sync", help="Install dependencies from the UVFile."
    )
    sync_parser.add_argument(
        "--reinstall", action="store_true", help="Reinstall all tools."
    )
    sync_parser.add_argument(
        "--uninstall",
        action="store_true",
        help="Uninstall tools not listed in the UVFile.",
    )
    sync_parser.add_argument(
        "--strict",
        action="store_true",
        help="Includes both --reinstall and --uninstall.",
    )
    sync_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be installed/uninstalled.",
    )

    args = parser.parse_args()
    if args.command == "init":
        init_uvfile(force=args.force, uvfile_path=args.uvfile)
    elif args.command == "sync":
        install_from_uvfile(
            reinstall=args.reinstall,
            uninstall=args.uninstall,
            strict=args.strict,
            dry_run=args.dry_run,
            verbose=args.verbose,
            uvfile_path=args.uvfile,
        )


if __name__ == "__main__":
    main()
