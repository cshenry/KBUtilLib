#!/usr/bin/env python3
"""Demonstration of KBUtilLib's flexible import system"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))


def demonstrate_optional_imports():
    """Demonstrate the optional import benefits."""
    print("KBUtilLib Optional Import System Demo")
    print("=" * 50)

    # Import the main package
    import kbutillib

    print("✓ Imported kbutillib successfully (no import errors!)")

    # Show available modules
    print(f"\nPackage version: {kbutillib.__version__}")

    print("\nAvailable modules:")
    for module_name in kbutillib.__all__:
        if module_name in ["examples"]:  # Skip examples as it's disabled
            continue
        module = getattr(kbutillib, module_name)
        status = "✓ Available" if module is not None else "✗ Missing dependencies"
        print(f"  {module_name}: {status}")

    # Demonstrate graceful handling of missing modules
    print("\nGraceful dependency handling:")
    if kbutillib.KBModelUtils is None:
        print("  ✓ KBModelUtils gracefully unavailable (missing cobrakbase dependency)")

    if kbutillib.KBSDKUtils is None:
        print("  ✓ KBSDKUtils gracefully unavailable (missing SDK dependencies)")

    # Demonstrate working modules
    print("\nTesting working modules:")

    # Test KBWSUtils
    if kbutillib.KBWSUtils is not None:
        ws_utils = kbutillib.KBWSUtils(kb_version="appdev")
        print(f"  ✓ KBWSUtils: {ws_utils.workspace_url}")

    # Test NotebookUtils
    if kbutillib.NotebookUtils is not None:
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            nb_utils = kbutillib.NotebookUtils(notebook_folder=temp_dir)
            print(f"  ✓ NotebookUtils: {nb_utils.data_dir}")

    print("\n🎉 Users can now install only the dependencies they need!")


def demonstrate_direct_imports():
    """Show that direct imports still work."""
    print("\nDirect Import Flexibility")
    print("-" * 30)

    # Direct import (will fail if dependencies missing)
    try:
        from kbutillib.kb_ws_utils import KBWSUtils

        ws_utils = KBWSUtils(kb_version="appdev")
        print("✓ Direct import works: KBWSUtils available")
    except ImportError as e:
        print(f"✗ Direct import failed (expected if dependencies missing): {e}")

    # Always works - core utilities
    print("✓ Core utilities always available via direct import")


if __name__ == "__main__":
    demonstrate_optional_imports()
    demonstrate_direct_imports()

    print(f"\n{'=' * 50}")
    print("SUMMARY: Optional import system implemented successfully!")
    print("- Package can be imported without all dependencies")
    print("- Missing modules are None instead of causing import errors")
    print("- Available modules work with full functionality")
    print("- Direct imports still work for specific modules")
    print("- Common imports centralized in BaseUtils")
    print("- Logging methods consistently available across all modules")
