"""Version bump script for gpumesh.

Usage:
    python scripts/bump_version.py major    # 0.1.0 -> 1.0.0
    python scripts/bump_version.py minor    # 0.1.0 -> 0.2.0
    python scripts/bump_version.py patch    # 0.1.0 -> 0.1.1
    python scripts/bump_version.py 1.2.3   # Set specific version
"""

import re
import sys
from pathlib import Path


def get_current_version() -> str:
    """Read current version from __init__.py."""
    init_file = Path(__file__).parent.parent / "gpumesh" / "__init__.py"
    content = init_file.read_text()
    match = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', content)
    if not match:
        raise ValueError("Could not find version in __init__.py")
    return match.group(1)


def parse_version(version: str) -> tuple:
    """Parse version string into (major, minor, patch)."""
    parts = version.strip().split(".")
    if len(parts) != 3:
        raise ValueError(f"Invalid version format: {version}")
    return tuple(int(p) for p in parts)


def bump_version(current: str, bump_type: str) -> str:
    """Bump version based on type (major/minor/patch) or set specific version."""
    if bump_type in ("major", "minor", "patch"):
        major, minor, patch = parse_version(current)
        if bump_type == "major":
            return f"{major + 1}.0.0"
        elif bump_type == "minor":
            return f"{major}.{minor + 1}.0"
        else:  # patch
            return f"{major}.{minor}.{patch + 1}"
    else:
        # Assume it's a specific version
        parse_version(bump_type)  # Validate format
        return bump_type


def update_version_in_file(filepath: Path, new_version: str) -> bool:
    """Update version in a file. Returns True if updated."""
    if not filepath.exists():
        return False
    
    content = filepath.read_text()
    
    # Update __version__ in Python files
    if filepath.suffix == ".py":
        new_content = re.sub(
            r'__version__\s*=\s*["\'][^"\']+["\']',
            f'__version__ = "{new_version}"',
            content
        )
    # Update version in pyproject.toml
    elif filepath.name == "pyproject.toml":
        new_content = re.sub(
            r'version\s*=\s*"[^"]+"',
            f'version = "{new_version}"',
            content
        )
    # Update the version in the Dockerfile (ARG default + image label)
    elif filepath.name == "Dockerfile":
        new_content = re.sub(
            r'ARG VERSION=[0-9.]+',
            f'ARG VERSION={new_version}',
            content
        )
        new_content = re.sub(
            r'LABEL version="[^"]+"',
            f'LABEL version="{new_version}"',
            new_content
        )
    else:
        return False
    
    if new_content != content:
        filepath.write_text(new_content)
        return True
    return False


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    
    bump_type = sys.argv[1]
    current = get_current_version()
    
    print(f"Current version: {current}")
    
    new_version = bump_version(current, bump_type)
    print(f"New version:     {new_version}")
    
    # Update files
    project_root = Path(__file__).parent.parent
    files_to_update = [
        project_root / "gpumesh" / "__init__.py",
        project_root / "pyproject.toml",
        project_root / "Dockerfile",
    ]
    
    for filepath in files_to_update:
        if update_version_in_file(filepath, new_version):
            print(f"  [OK] Updated {filepath.relative_to(project_root)}")
        else:
            print(f"  [--] Skipped {filepath.relative_to(project_root)}")
    
    print(f"\nVersion bumped to {new_version}")
    print("Next steps:")
    print("  1. Run tests: python -m pytest tests/ -v")
    print("  2. Build: python -m build")
    print("  3. Upload: python -m twine upload dist/*")


if __name__ == "__main__":
    main()
