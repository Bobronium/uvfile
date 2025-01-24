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
    """
    Represents a single requirement, with all its possible sources.
    Instead of separate git/editable/directory fields, we unify them under `url`
    and a boolean `editable`.
    """
    name: Optional[str] = None
    version: Optional[str] = None
    extras: List[str] = field(default_factory=list)
    url: Optional[str] = None  # Could be a VCS URL, local directory, etc.
    editable: bool = False

    def to_install_args(self, as_with: bool = False) -> List[str]:
        """
        Convert the requirement into an install command fragment.
        - If `as_with` is True, formats this as a `--with` or `--with-editable` argument.
        """
        if self.url:
            # If we have a URL, handle editable vs. non-editable
            if self.editable:
                # Editable install
                if as_with:
                    return ["--with-editable", f"{self.name}@{self.url}"]
                else:
                    return [f"{self.name}@{self.url}", "--editable"]
            else:
                # Non-editable install from a URL
                if as_with:
                    return ["--with", f"{self.name}@{self.url}"]
                else:
                    return [f"{self.name}@{self.url}"]
        else:
            # No URL: typical requirement with optional extras and version
            base = self.name or ""
            if self.extras:
                base += f"[{','.join(self.extras)}]"
            if self.version:
                base += self.version
            return ["--with", base] if as_with else [base]

    def __eq__(self, other: "RequirementSpec") -> bool:
        """Check if two requirements match in all aspects."""
        return (
            self.name == other.name
            and self.version == other.version
            and sorted(self.extras) == sorted(other.extras)
            and self.url == other.url
            and self.editable == other.editable
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

    def __eq__(self, other: "Tool") -> bool:
        """Check if two tools match, including their dependencies."""
        if self.primary != other.primary:
            return False
        if sorted(self.additional, key=lambda r: (r.name or "", r.version or "")) != \
           sorted(other.additional, key=lambda r: (r.name or "", r.version or "")):
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
    """
    Parse a single requirement dictionary from uv-receipt.toml.
    We unify git/directory/other URL types into `url`,
    and store editable as a boolean.
    """
    # If multiple are present, just pick one in priority order:
    url = requirement.get("git") or requirement.get("directory") or requirement.get("editable")
    editable = bool(requirement.get("editable", False))

    return RequirementSpec(
        name=requirement.get("name"),
        version=requirement.get("specifier"),
        extras=requirement.get("extras", []),
        url=url,
        editable=editable,
    )


def get_installed_tools(uv_tools_dir: Path) -> List[Tool]:
    """Fetch the list of installed tools and their versions using `uv tool list --show-paths`."""
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
        # The line typically looks like: 'mypkg 1.2.3 (/path/to/mypkg)'
        name_version, path = line.rsplit(" ", 1)
        receipt_path = Path(path.strip("()")) / "uv-receipt.toml"
        receipt = parse_uv_receipt(receipt_path)
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
        parser.add_argument("--editable", action="store_true")
        parser.add_argument("--with", action="append", dest="additional", default=[])
        parser.add_argument(
            "--with-editable", action="append", dest="additional_editable", default=[]
        )

        namespace = parser.parse_args(extra_args)

        # Primary requirement
        primary = RequirementSpec(
            name=req.name,
            version=str(req.specifier) if req.specifier else None,
            extras=list(req.extras),
            url=req.url,                 # unify directory/git/etc. into url
            editable=namespace.editable  # whether it's editable
        )

        # Additional requirements
        additional = []
        for requirement_str in namespace.additional:
            additional_req = Requirement(requirement_str)
            additional.append(
                RequirementSpec(
                    name=additional_req.name,
                    version=str(additional_req.specifier) if additional_req.specifier else None,
                    extras=list(additional_req.extras),
                    url=additional_req.url,
                    editable=False,
                )
            )
        for requirement_str in namespace.additional_editable:
            additional_req = Requirement(requirement_str)
            additional.append(
                RequirementSpec(
                    name=additional_req.name,
                    version=str(additional_req.specifier) if additional_req.specifier else None,
                    extras=list(additional_req.extras),
                    url=additional_req.url,
                    editable=True,
                )
            )

        tools.append(
            Tool(
                primary=primary,
                additional=additional,
                python_version=namespace.python
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
            and tool == matching_installed_tool
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


def generate_uvfile_env_script():
    """Generate the Bash script for wrapping the uv command."""
    script = """
uv () {
  local exe=("command" "uv")

  # Check if uvfile exists
  if ! type uvfile >/dev/null 2>&1; then
    "${exe[@]}" "$@"
    return
  fi

  local nargs=0
  local cmd=$1
  for arg in "$@"; do
    if [[ ! "$arg" =~ ^- ]]; then
      ((nargs++))
    fi
  done

  case "$cmd" in
    tool)
      local cmd2=$2
      if [[ "$cmd2" =~ ^(install|upgrade)$ ]]; then
        "${exe[@]}" "$@"
        local ret=$?
        if [ $ret -eq 0 ]; then
          uvfile init --force
        fi
        return $ret
      fi
      ;;
    file)
      shift
      uvfile "$@"
      return $?
      ;;
  esac

  "${exe[@]}" "$@"
}

# Enable uv command completion
if type -a _uv >/dev/null 2>&1; then
  _uv_completion_wrap() {
    local cword=$COMP_CWORD
    local cur=${COMP_WORDS[cword]}
    local cmd=${COMP_WORDS[1]}

    if [ "$cmd" = "tool" ]; then
      COMPREPLY=($(compgen -W "install upgrade list uninstall" -- "$cur"))
    else
      _uv
    fi
  }
  complete -o bashdefault -o default -F _uv_completion_wrap uv
fi
"""
    return script


def env():
    """Handle the uvfile env command to output the wrapper script."""
    script = generate_uvfile_env_script()
    print(script)


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
    subparsers.add_parser(
        "env", help="Generate a Bash script for wrapping the uv command."
    )

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
    elif args.command == "env":
        env()


if __name__ == "__main__":
    main()