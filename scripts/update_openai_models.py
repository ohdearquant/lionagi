#!/usr/bin/env python3
"""
Update OpenAI Models Script

This script updates the OpenAI models by regenerating them from the latest
OpenAPI specification. It ensures all required dependencies are installed
and applies the necessary post-processing.

Usage:
    python scripts/update_openai_models.py
"""

import os
import subprocess
import sys
from pathlib import Path


def run_command(cmd, description=""):
    """Run a command and handle errors."""
    print(f"🔄 {description}")
    try:
        result = subprocess.run(cmd, shell=True, check=True, capture_output=True, text=True)
        if result.stdout.strip():
            print(f"   {result.stdout.strip()}")
        return True
    except subprocess.CalledProcessError as e:
        print(f"❌ Error: {e}")
        if e.stderr:
            print(f"   {e.stderr.strip()}")
        return False


def install_dependencies():
    """Install required dependencies."""
    print("📦 Installing dependencies...")
    return run_command(
        "uv pip install 'datamodel-code-generator[http]==0.30.1'",
        "Installing datamodel-code-generator",
    )


def generate_models():
    """Generate OpenAI models from the schema."""
    print("🔧 Generating OpenAI models...")

    # Ensure output directory exists
    output_path = Path("lionagi/service/third_party")
    output_path.mkdir(parents=True, exist_ok=True)

    cmd = [
        "datamodel-codegen",
        "--url",
        "https://app.stainless.com/api/spec/documented/openai/openapi.documented.yml",
        "--output",
        "lionagi/service/third_party/openai_models.py",
        "--allow-population-by-field-name",
        "--output-model-type",
        "pydantic_v2.BaseModel",
        "--field-constraints",
        "--use-schema-description",
        "--input-file-type",
        "openapi",
        "--use-field-description",
        "--use-one-literal-as-default",
        "--enum-field-as-literal",
        "all",
        "--use-union-operator",
        "--no-alias",
    ]

    return run_command(" ".join(cmd), "Generating models from OpenAPI schema")


def add_post_processing():
    """Add required imports and warning suppressions."""
    print("🔧 Adding post-processing...")

    models_file = Path("lionagi/service/third_party/openai_models.py")
    if not models_file.exists():
        print("❌ Generated models file not found!")
        return False

    # Read the current content
    with open(models_file) as f:
        content = f.read()

    # Check if post-processing is already applied
    if "warnings.filterwarnings" in content and "bytes_aliased" in content:
        print("   ✓ Post-processing already applied")
        # Still need to check for discriminator fixes
        if "List[Outputs] = Field(..., discriminator=" not in content:
            print("   ✓ Discriminator fixes already applied")
            return True

    # Find the imports section and add our modifications
    lines = content.split("\n")

    # Find where to insert our modifications
    insert_index = -1
    for i, line in enumerate(lines):
        if line.startswith("from __future__ import annotations"):
            insert_index = i
            break

    if insert_index == -1:
        # Look for first import line
        for i, line in enumerate(lines):
            if line.startswith("from typing") or line.startswith("import"):
                insert_index = i
                break

    if insert_index == -1:
        print("❌ Could not find where to insert post-processing code")
        return False

    # Prepare the new imports and warning suppression
    new_lines = lines[:insert_index]

    # Add our header
    new_lines.extend(
        [
            "from __future__ import annotations  # noqa: D401,F401",
            "",
            "import warnings",
            "from typing import Annotated, Any, Dict, List, Literal",
            "",
            "from pydantic import AnyUrl, BaseModel, ConfigDict, Field, RootModel",
            "",
            "# Filter out Pydantic alias warnings",
            "warnings.filterwarnings(",
            '    "ignore",',
            '    message=".*`alias` specification on field.*must be set on outermost annotation.*",',
            "    category=UserWarning,",
            '    module="pydantic._internal._fields",',
            ")",
            "",
            "# Type aliases for special field names",
            'bytes_aliased = Annotated[bytes, Field(alias="bytes")]',
            'float_aliased = Annotated[float, Field(alias="float")]',
            "",
        ]
    )

    # Process the rest of the content and fix discriminator issues
    skip_until_class = True
    for i in range(insert_index, len(lines)):
        line = lines[i]
        if line.startswith("class ") or (
            line.strip() and not line.startswith(("from ", "import "))
        ):
            skip_until_class = False

        if not skip_until_class:
            # Fix discriminated union issues - remove discriminator from List fields
            if "List[Outputs] = Field(..., discriminator=" in line:
                # Remove the discriminator argument from List fields
                line = line.replace(", discriminator='type'", "")
                print("   ✓ Fixed discriminated union in List field")

            new_lines.append(line)

    # Write the modified content
    with open(models_file, "w") as f:
        f.write("\n".join(new_lines))

    print("   ✓ Added imports, warnings suppression, type aliases, and discriminator fixes")
    return True


def verify_generation():
    """Verify the generated models can be imported."""
    print("🔍 Verifying generated models...")

    try:
        # Try to import a class from the generated models
        test_cmd = """
import sys
sys.path.insert(0, '.')
from lionagi.service.third_party.openai_models import AddUploadPartRequest
print("✓ Successfully imported OpenAI models")
"""
        result = subprocess.run(
            [sys.executable, "-c", test_cmd],
            capture_output=True,
            text=True,
            cwd=".",
        )

        if result.returncode == 0:
            print("   ✓ Models import successfully")
            return True
        else:
            print(f"   ❌ Import failed: {result.stderr}")
            return False

    except Exception as e:
        print(f"   ❌ Verification failed: {e}")
        return False


def get_file_info():
    """Get information about the generated file."""
    models_file = Path("lionagi/service/third_party/openai_models.py")
    if models_file.exists():
        size_mb = models_file.stat().st_size / (1024 * 1024)
        print(f"📊 Generated file: {models_file}")
        print(f"📊 File size: {size_mb:.1f} MB")

        # Count lines
        with open(models_file) as f:
            line_count = sum(1 for _ in f)
        print(f"📊 Lines: {line_count:,}")


def main():
    """Main execution function."""
    print("🚀 OpenAI Models Update Script")
    print("=" * 40)

    # Change to project root directory
    script_dir = Path(__file__).parent
    project_root = script_dir.parent
    os.chdir(project_root)
    print(f"📁 Working directory: {project_root}")

    # Step 1: Install dependencies
    if not install_dependencies():
        print("❌ Failed to install dependencies")
        sys.exit(1)

    # Step 2: Generate models
    if not generate_models():
        print("❌ Failed to generate models")
        sys.exit(1)

    # Step 3: Add post-processing
    if not add_post_processing():
        print("❌ Failed to add post-processing")
        sys.exit(1)

    # Step 4: Verify generation
    verification_passed = verify_generation()
    if not verification_passed:
        print("⚠️  Warning: Generated models have Pydantic compatibility issues")
        print("   Known issues:")
        print("   - Discriminated union conflicts in complex schemas")
        print("   - This is a compatibility issue between datamodel-code-generator")
        print("     and the current OpenAI schema complexity")
        print("   - Models can still be used for type hints and basic validation")
        print("   - Consider using openai-python package directly for runtime usage")

    # Step 5: Show file information
    get_file_info()

    print("\n✅ OpenAI models update completed!")
    print(f"✅ Verification: {'PASSED' if verification_passed else 'FAILED (see warnings above)'}")
    print("\n📝 Notes:")
    print("   - File is configured to be ignored by git")
    print("   - Will be regenerated during CI/CD build processes")
    print("   - Use for type hints; consider openai-python for runtime validation")


if __name__ == "__main__":
    main()
