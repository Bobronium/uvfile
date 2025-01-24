import sys
from pathlib import Path
import subprocess
import tomllib as toml
import argparse
from dataclasses import dataclass, field
import shlex
import os
from argparse import ArgumentParser

from packaging.requirements import Requirement

# Constants
DEFAULT_UVFILE_PATH = Path("UVFile")


def debug(*args, verbose: bool):
    if verbose:
        print(*args, file=sys.stderr)


@dataclass
class Tool:
    # FIXME: should've went with UV's representation
    """Represents a single tool installed via uv tool."""

    name: str
    version: str | None = None
    extras: list[str] = field(default_factory=list)
    additional: list[str] = field(
        default_factory=list
    )  # Flat list of args (e.g., ["--with", "ruff[fast]>=0.7"])
    python_version: str | None = None

    def matches(self, other: "Tool") -> bool:
        """Check if this tool matches another tool in all aspects."""
        return (
            self.name == other.name
            and self.version == other.version
            and sorted(self.extras) == sorted(other.extras)
            and self._compare_additional(other.additional)
            and self.python_version == other.python_version
        )

    def _compare_additional(self, other_additional: list[str]) -> bool:
        """Compare additional arguments using normalized parsing."""
        parser = ArgumentParser()
        parser.add_argument("--with", action="append", dest="with_deps", default=[])
        parser.add_argument(
            "--with-editable", action="append", dest="editable_deps", default=[]
        )

        # Parse this tool's additional arguments
        this_namespace, _ = parser.parse_known_args(self.additional)

        # Parse the other tool's additional arguments
        other_namespace, _ = parser.parse_known_args(other_additional)

        # Compare the parsed arguments
        return sorted(this_namespace.with_deps) == sorted(
            other_namespace.with_deps
        ) and sorted(this_namespace.editable_deps) == sorted(
            other_namespace.editable_deps
        )

    def install_command(self, reinstall: bool = False) -> list[str]:
        """Construct the installation command for this tool as argv."""
        command = [self.name]
        if self.extras:
            command[0] += f"[{','.join(self.extras)}]"
        if self.version:
            command[0] += self.version
        if self.additional:
            command.extend(self.additional)
        if self.python_version:
            command.append(f"--python {self.python_version}")
        if reinstall:
            command.append("--reinstall")
        return command


def get_installed_tools() -> list[Tool]:
    """Fetch the list of installed tools and their versions using uv tool list."""
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
        receipt = parse_uv_receipt(Path(path.strip("()")))
        if receipt:
            requirements = receipt["tool"]["requirements"]

            # Parse the primary requirement
            primary_requirement = requirements[0]
            additional_requirements = requirements[1:]  # Remaining are --with

            # Flat list of additional arguments
            additional = []
            for req in additional_requirements:
                if "editable" in req:
                    additional.extend(["--with-editable", req["editable"]])
                elif "directory" in req:
                    additional.extend(["--with", req["directory"]])
                elif "git" in req:
                    additional.extend(["--with", f"{req['name']}@{req['git']}"])
                else:
                    extras_str = (
                        f"[{','.join(req.get('extras', []))}]"
                        if "extras" in req
                        else ""
                    )
                    specifier_str = req.get("specifier", "")
                    additional.extend(
                        ["--with", f"{req['name']}{extras_str}{specifier_str}"]
                    )

            tools.append(
                Tool(
                    name=primary_requirement["name"],
                    version=primary_requirement.get("specifier"),
                    extras=primary_requirement.get("extras", []),
                    additional=additional,
                    python_version=receipt["tool"].get("python"),
                )
            )
    return tools


def parse_uv_receipt(tool_path: Path) -> dict | None:
    """Parse the uv-receipt.toml file for a given tool."""
    receipt_path = tool_path / "uv-receipt.toml"
    if not receipt_path.exists():
        return None
    return toml.loads(receipt_path.read_text())


def collect_tool_metadata(uvfile_path: Path) -> list[Tool]:
    """Parse the UVFile and return a list of tools using packaging.requirements."""
    tools = []
    if not uvfile_path.exists():
        return tools

    for line in uvfile_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        # Split the line into requirement and additional options
        requirement, *extra_args = shlex.split(line)
        req = Requirement(requirement)
        parser = argparse.ArgumentParser()
        parser.add_argument("--python")
        namespace, additional = parser.parse_known_args(extra_args)
        tools.append(
            Tool(
                name=req.name,
                version=str(req.specifier) if req.specifier else None,
                extras=list(req.extras),
                additional=additional,
                python_version=namespace.python,
            )
        )
    return tools


def write_uvfile(tools: list[Tool], uvfile_path: Path) -> None:
    """Write the UVFile with the list of tools and their metadata."""
    lines = [" ".join(tool.install_command()) for tool in tools]
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
    if not uvfile_path.exists():
        print(f"UVFile not found at {uvfile_path}")
        return

    installed_tools = get_installed_tools()
    uvfile_tools = collect_tool_metadata(uvfile_path)

    # Handle strict mode (uninstall tools not in the UVFile)
    if strict or uninstall:
        installed_names = {tool.name for tool in installed_tools}
        uvfile_names = {tool.name for tool in uvfile_tools}
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
            (t for t in installed_tools if t.name == tool.name), None
        )
        if matching_installed_tool and not (reinstall or strict):
            # Skip installation if the tool matches and no reinstall is requested
            debug(
                f"Skipping {tool.name}, already installed and no --reinstall or --strict specified.",
                verbose=verbose,
            )
            continue
        elif tool.matches(matching_installed_tool):
            debug(
                f"Skipping {tool.name}, the same version is already installed.",
                verbose=verbose,
            )
            continue

        # If reinstall or strict is specified, or there's no match, run install
        command = (
            ["uv", "tool", "install"]
            + (["--reinstall"] if reinstall or strict else [])
            + tool.install_command()
        )
        if dry_run:
            print(f"Would run: {' '.join(command)}")
        else:
            debug(f"Installing: {tool.name}", verbose=verbose)
            subprocess.run(command, check=True)


def init_uvfile(force: bool, uvfile_path: Path) -> None:
    """Generate a new UVFile from currently installed tools."""
    # Check if the UVFile already exists
    if uvfile_path.exists() and not force:
        confirmation = (
            input(f"{uvfile_path} already exists. Overwrite? [y/N]: ").strip().lower()
        )
        if confirmation != "y":
            print("Aborted.")
            return

    # Get the currently installed tools
    installed_tools = get_installed_tools()

    # Write the UVFile with metadata from the installed tools
    write_uvfile(installed_tools, uvfile_path)
    print(f"UVFile updated with {len(installed_tools)} tools at {uvfile_path}.")


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


def handle_env_command():
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
    subparsers.add_parser(
        "env", help="Generate a Bash script for wrapping the uv command."
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
        handle_env_command()


if __name__ == "__main__":
    main()


"""
if [ -f /opt/homebrew/etc/brew-wrap ];then
  source /opt/homebrew/etc/brew-wrap
fi

"""
